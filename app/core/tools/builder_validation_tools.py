"""Configuration validation tool for Arthur builder."""
from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import tool

from app.core.tools.builder_catalog import AGENT_PRESETS, _DEFAULT_MODEL, _RECOMMENDED_MODELS
from app.core.tools.builder_identity import blocked_agent_policy_reason as _blocked_agent_policy_reason
from app.core.tools.builder_intent import (
    _has_approval_state_contract,
    _looks_like_approval_gated_service,
    _looks_like_file_delivery_workflow,
    _looks_like_generated_file_workflow,
    _looks_like_payment_approval_workflow,
    file_delivery_contract_issues,
)
from app.core.tools.builder_text import find_unfilled_placeholders as _find_unfilled_placeholders


def build_builder_validation_tools() -> dict[str, Any]:
    @tool
    async def validate_agent_config(
        name: str,
        instructions: str,
        tools_config: str = "{}",
        model: str = "",
        channel_type: str = "",
        preset_id: str = "",
    ) -> str:
        """
        Validasi konfigurasi agent sebelum disimpan ke database.
        Cek nama, instructions, tools_config, model, channel requirements, dan best practices.

        Args:
            name: Nama agent yang akan dibuat
            instructions: System prompt yang akan divalidasi
            tools_config: JSON string dari tools_config yang direncanakan
            model: Model LLM yang akan digunakan (kosong = pakai default gpt-4.1-mini)
            channel_type: Channel agent: 'whatsapp', 'webchat', atau kosong
            preset_id: ID preset yang digunakan (opsional — untuk validasi preset-specific rules)
        """
        warnings: list[str] = []
        errors: list[str] = []
        suggestions: list[str] = []

        effective_model = model or _DEFAULT_MODEL

        # Validasi nama
        if not name or len(name.strip()) < 2:
            errors.append("Nama agent terlalu pendek (minimal 2 karakter)")
        if len(name) > 255:
            errors.append("Nama agent terlalu panjang (maksimal 255 karakter)")

        policy_reason = _blocked_agent_policy_reason(name, instructions, tools_config, preset_id)
        if policy_reason:
            errors.append(policy_reason)

        # Validasi instructions
        instruction_len = len(instructions)
        if instruction_len < 100:
            errors.append("Instructions terlalu pendek — agent tidak akan punya cukup konteks. Gunakan compose_agent_instructions untuk generate yang baik.")
        if instruction_len > 32000:
            errors.append(f"Instructions terlalu panjang ({instruction_len} karakter) — bisa melebihi context window model")
        elif instruction_len > 16000:
            warnings.append(f"Instructions cukup panjang ({instruction_len} karakter) — pertimbangkan memindahkan detail ke RAG documents")

        # Deteksi placeholder yang belum diisi
        unfilled = _find_unfilled_placeholders(instructions)
        if unfilled:
            errors.append(
                f"Instructions masih mengandung {len(unfilled)} placeholder yang belum diisi: {unfilled}. "
                "Panggil compose_agent_instructions untuk generate instructions yang lengkap."
            )

        # Cek few-shot examples untuk WA agent
        if channel_type == "whatsapp" or (not channel_type and "escalat" in instructions.lower()):
            has_example = (
                "user:" in instructions.lower()
                or "contoh" in instructions.lower()
                or "example" in instructions.lower()
                or "percakapan" in instructions.lower()
            )
            if not has_example:
                warnings.append("Instructions tidak punya contoh percakapan — tambahkan 1-2 few-shot examples untuk meningkatkan kualitas respons agent")

        # Validasi tools_config
        try:
            tc = json.loads(tools_config) if isinstance(tools_config, str) else tools_config
        except json.JSONDecodeError:
            errors.append("tools_config bukan JSON yang valid")
            tc = {}
        if isinstance(tc, dict):
            tc.setdefault("tavily", True)

        approval_gated_service = _looks_like_approval_gated_service(
            name,
            instructions,
            tools_config,
            preset_id,
        )
        payment_approval_workflow = _looks_like_payment_approval_workflow(
            name,
            instructions,
            tools_config,
            preset_id,
        ) or preset_id == "approval_gated_service_agent" or approval_gated_service
        file_delivery_workflow = _looks_like_file_delivery_workflow(
            name,
            instructions,
            tools_config,
            preset_id,
        )
        generated_file_workflow = _looks_like_generated_file_workflow(
            name,
            instructions,
            tools_config,
            preset_id,
        )
        if payment_approval_workflow:
            if instruction_len < 1200:
                errors.append(
                    "Instructions terlalu pendek untuk workflow pembayaran/admin approval — "
                    "wajib memuat state intake, waiting_payment, payment_review, approved, delivery, dan aftercare."
                )
            if not _has_approval_state_contract(instructions):
                errors.append(
                    "Workflow pembayaran belum lengkap — instructions wajib memuat state "
                    "intake -> waiting_payment -> payment_review -> approved -> delivery -> aftercare."
                )
            if not tc.get("escalation"):
                errors.append("Workflow pembayaran/admin approval wajib mengaktifkan escalation: true.")
            if "escalate_to_human" not in instructions:
                errors.append("Workflow bukti transfer wajib menginstruksikan pemanggilan escalate_to_human.")
        if file_delivery_workflow:
            if not tc.get("whatsapp_media"):
                errors.append("Workflow delivery file via WhatsApp wajib mengaktifkan whatsapp_media: true.")
            errors.extend(file_delivery_contract_issues(instructions, file_delivery=True))
        if generated_file_workflow:
            subagents_cfg = tc.get("subagents", {})
            subagents_enabled = bool(
                subagents_cfg.get("enabled") if isinstance(subagents_cfg, dict) else subagents_cfg
            )
            if not tc.get("sandbox") or not subagents_enabled:
                errors.append("Workflow pembuatan file final wajib mengaktifkan sandbox dan subagents.")

        # Dependency checks — machine-enforced
        if tc.get("tool_creator") and not tc.get("sandbox"):
            errors.append("tool_creator membutuhkan sandbox: true — aktifkan sandbox juga")

        if tc.get("deploy") and not tc.get("sandbox"):
            errors.append("deploy membutuhkan sandbox: true — agent deploy tidak akan bisa deploy tanpa sandbox aktif")

        # Coding/deploy-specific: enforce output contract in instructions
        if tc.get("sandbox") or tc.get("deploy"):
            instr_lower = instructions.lower()
            if "status:" not in instr_lower and "deploy_url" not in instr_lower:
                warnings.append(
                    "Agent coding/deploy sebaiknya memiliki output contract (STATUS: / DEPLOY_URL: / BLOCKER:) di instructions"
                )
            if "get_deployment_status" not in instructions:
                suggestions.append("Tambahkan instruksi untuk panggil get_deployment_status() sebelum deploy ulang")

        # WhatsApp-specific checks
        effective_channel = channel_type or ""
        if effective_channel == "whatsapp" or tc.get("whatsapp_media"):
            if "*" in instructions or "**" in instructions or "##" in instructions:
                warnings.append("Instructions mengandung markdown — tidak akan dirender di WhatsApp, tampil sebagai karakter literal")
            if not tc.get("escalation"):
                warnings.append("Agent WhatsApp sebaiknya mengaktifkan escalation: true untuk operator handoff")
            if "escalate_to_human" not in instructions and tc.get("escalation"):
                warnings.append("escalation aktif tapi instructions tidak menyebut escalate_to_human — agent mungkin tidak tahu cara eskalasi")

        # RAG-specific
        if tc.get("rag"):
            if "search_documents" not in instructions:
                suggestions.append("Tambahkan instruksi untuk menggunakan search_documents saat menjawab pertanyaan")
            warnings.append("Ingat: dokumen harus diupload via /v1/agents/{id}/documents/upload setelah agent dibuat")

        # Scheduler-specific
        if tc.get("scheduler"):
            if "set_reminder" not in instructions and "reminder" not in instructions.lower():
                suggestions.append("Tambahkan instruksi kapan/bagaimana agent menggunakan set_reminder")

        # General best practices
        if "eskalasi" not in instructions.lower() and "escalat" not in instructions.lower() and tc.get("escalation"):
            suggestions.append("Tambahkan instruksi eskalasi: kapan agent harus memanggil operator manusia")
        if len(instructions) > 100 and "contoh" not in instructions.lower() and "example" not in instructions.lower():
            suggestions.append("Pertimbangkan menambahkan 1-2 contoh percakapan (few-shot) untuk meningkatkan kualitas respons")

        # Preset-specific validation
        if preset_id and preset_id in AGENT_PRESETS:
            p = AGENT_PRESETS[preset_id]
            for req_tool in p.get("required_tools", []):
                if not tc.get(req_tool):
                    errors.append(f"Preset '{preset_id}' membutuhkan {req_tool}: true di tools_config")
            for forbidden_tool in p.get("forbidden_tools", []):
                if tc.get(forbidden_tool):
                    warnings.append(f"Preset '{preset_id}' sebaiknya tidak mengaktifkan {forbidden_tool}")

        # Validasi model
        known_models = [m["model"] for m in _RECOMMENDED_MODELS]
        if effective_model not in known_models:
            suggestions.append(f"Model '{effective_model}' tidak ada di daftar rekomendasi — pastikan nama model benar")

        quality_score = 100
        quality_score -= len(errors) * 25
        quality_score -= len(warnings) * 10
        quality_score -= len(suggestions) * 5
        quality_score = max(0, quality_score)

        return json.dumps({
            "valid": len(errors) == 0,
            "quality_score": quality_score,
            "effective_model": effective_model,
            "errors": errors,
            "warnings": warnings,
            "suggestions": suggestions,
            "summary": (
                "Konfigurasi siap dibuat." if len(errors) == 0
                else f"Ada {len(errors)} error yang harus diperbaiki sebelum membuat agent."
            ),
        }, ensure_ascii=False, indent=2)

    return {"validate_agent_config": validate_agent_config}
