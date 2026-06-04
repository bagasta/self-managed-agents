"""Planning tools for Arthur builder."""
from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from langchain_core.tools import tool

from app.core.engine.google_mcp_support import _is_plain_google_form_link_reference
from app.core.tools.builder_catalog import AGENT_PRESETS, RUNTIME_LIMITATIONS, _DEFAULT_MODEL
from app.core.tools.builder_google import (
    enable_google_workspace_tools as _enable_google_workspace_tools,
    google_workspace_option as _google_workspace_option,
    negates_google_workspace as _negates_google_workspace,
)
from app.core.tools.builder_identity import blocked_agent_policy_reason as _blocked_agent_policy_reason
from app.core.tools.builder_intent import (
    _combined_context_text,
    _detect_preset,
    _looks_like_approval_gated_service,
    _looks_like_file_delivery_workflow,
    _looks_like_generated_file_workflow,
    _looks_like_payment_approval_workflow,
)


def _get_post_create_steps(preset_id: str, channel: str, tc: dict) -> list[str]:
    """Return required actions user/operator must take after agent creation."""
    steps = []
    if channel == "whatsapp" or tc.get("whatsapp_media"):
        steps.append("Kirim QR ke user: gunakan send_agent_wa_qr(agent_id, caption, phone)")
        steps.append("Tunggu user scan QR, lalu cek ulang dengan send_agent_wa_qr jika butuh QR baru")
    if tc.get("rag"):
        steps.append("Upload dokumen: POST /v1/agents/{id}/documents/upload (PDF/DOCX/TXT)")
    if preset_id == "coding_deploy_agent":
        steps.append("Pastikan Docker socket tersedia di server sebelum test deploy")
    return steps


PreviewEntitlement = Callable[..., Awaitable[dict[str, Any]]]


def build_builder_planning_tools(
    *,
    preview_agent_creation_entitlement: PreviewEntitlement,
) -> dict[str, Any]:
    _preview_agent_creation_entitlement = preview_agent_creation_entitlement

    @tool
    async def plan_agent(
        user_goal: str,
        agent_name: str = "",
        channel: str = "",
        requested_features: str = "",
        persona: str = "",
        business_context: str = "",
        operator_phone: str = "",
    ) -> str:
        """
        Buat rencana agent terstruktur berdasarkan goal user sebelum create.
        Mengembalikan preset yang cocok, tools_config yang direkomendasikan,
        validation warnings, dan langkah selanjutnya.

        Gunakan ini SEBELUM create_agent untuk memastikan config sudah tepat.
        Ini bukan approval gate. Setelah plan siap, lanjutkan ke compose_agent_blueprint,
        compose_agent_instructions, validate_agent_config, lalu create_agent tanpa bertanya
        "setuju/lanjut?" kecuali ada validation_errors atau data kritis yang benar-benar
        wajib dari user.

        Args:
            user_goal: Deskripsi singkat apa yang user ingin agentnya lakukan
            agent_name: Nama agent yang diinginkan (opsional)
            channel: Channel yang diinginkan: 'whatsapp', 'webchat', atau kosong
            requested_features: Fitur-fitur yang diminta, dipisah koma (misal: 'coding,deploy,http')
            persona: Persona/gaya bicara agent (opsional)
            business_context: Konteks bisnis untuk agent CS/FAQ (opsional)
            operator_phone: Nomor operator/admin untuk eskalasi (opsional)
        """
        goal_lower = user_goal.lower()
        policy_reason = _blocked_agent_policy_reason(
            user_goal,
            agent_name,
            requested_features,
            persona,
            business_context,
        )
        if policy_reason:
            return json.dumps({
                "plan_status": "blocked_by_policy",
                "validation_errors": [policy_reason],
                "next_action": "Tolak permintaan ini dengan singkat dan tawarkan jenis agent non-politik/non-buzzer.",
            }, ensure_ascii=False, indent=2)

        features = [f.strip().lower() for f in requested_features.split(",") if f.strip()]
        feature_text = _combined_context_text(user_goal, requested_features, business_context)
        google_context_text = f"{user_goal} {requested_features} {business_context}"

        # Auto-detect preset from goal keywords
        detected_preset = _detect_preset(feature_text, features, channel)

        preset = AGENT_PRESETS.get(detected_preset, {})
        tools_config = dict(preset.get("tools_config", {
            "memory": True, "skills": True, "escalation": True
        }))
        tools_config.setdefault("tavily", True)

        # Override with explicitly requested features
        feature_map = {
            "rag": "rag", "dokumen": "rag", "faq": "rag", "document": "rag",
            "scheduler": "scheduler", "reminder": "scheduler", "jadwal": "scheduler",
            "http": "http", "api": "http",
            "tavily": "tavily", "browse": "tavily", "browser": "tavily", "search": "tavily",
            "sandbox": "sandbox", "coding": "sandbox", "kode": "sandbox", "prototype": "sandbox", "website": "sandbox",
            "deploy": "deploy",
            "whatsapp_media": "whatsapp_media", "media": "whatsapp_media", "gambar": "whatsapp_media",
            "file": "whatsapp_media", "pdf": "whatsapp_media", "excel": "whatsapp_media", "docx": "whatsapp_media",
        }
        for feat in features:
            mapped = feature_map.get(feat)
            if mapped and mapped in tools_config:
                tools_config[mapped] = True

        approval_gated_service = _looks_like_approval_gated_service(feature_text)
        payment_approval_workflow = _looks_like_payment_approval_workflow(feature_text)
        file_delivery_workflow = _looks_like_file_delivery_workflow(feature_text)
        generated_file_workflow = _looks_like_generated_file_workflow(feature_text)
        wants_coding = any(k in feature_text for k in ("coding", "kode", "prototype", "website", "deploy", "sandbox"))
        wants_cv_document = any(
            k in feature_text
            for k in (
                "bikin cv",
                "buat cv",
                "cv ats",
                "resume ats",
                "bikin resume",
                "buat resume",
                "kirim cv",
                "kirim resume",
            )
        )
        wants_files = file_delivery_workflow or wants_cv_document
        wants_generated_files = generated_file_workflow or wants_cv_document
        plain_google_form_link = _is_plain_google_form_link_reference(google_context_text)
        google_negated = _negates_google_workspace(feature_text)
        wants_google = (
            any(k in feature_text for k in ("google", "gmail", "calendar", "drive", "docs", "sheets", "workspace"))
            and not plain_google_form_link
            and not google_negated
        )
        google_workspace_option = (
            {
                "should_offer": False,
                "enabled": False,
                "suggested_apps": [],
                "reasons": [],
                "user_facing_pitch": "",
                "if_user_declines": "Lanjutkan tanpa integrasi Google.",
            }
            if google_negated
            else _google_workspace_option(feature_text, wants_google)
        )
        if wants_coding:
            tools_config["sandbox"] = True
            tools_config["deploy"] = True
            tools_config["subagents"] = {"enabled": True}
        if wants_files:
            tools_config["whatsapp_media"] = True
        if wants_generated_files:
            tools_config["sandbox"] = True
            tools_config["subagents"] = {"enabled": True}
        explicit_media_request = any(
            feat in features
            for feat in ("media", "gambar", "foto", "file", "pdf", "excel", "xlsx", "docx", "dokumen")
        )
        if not wants_files and not wants_generated_files and not explicit_media_request:
            tools_config["whatsapp_media"] = False
        if approval_gated_service or payment_approval_workflow:
            tools_config["escalation"] = True
            tools_config["whatsapp_media"] = True
        needs_human_handoff = bool(operator_phone) or any(
            k in feature_text
            for k in (
                "admin",
                "operator",
                "owner",
                "pemilik",
                "eskalasi",
                "approval",
                "approve",
                "harga final",
                "stok",
                "booking",
                "kepastian",
                "komplain",
                "refund",
                "bukti transfer",
                "dp",
                "pelunasan",
            )
        )
        if needs_human_handoff:
            tools_config["escalation"] = True
        if wants_google:
            tools_config = _enable_google_workspace_tools(tools_config)

        # Validate tool dependencies
        validation_errors: list[str] = []
        validation_warnings: list[str] = []

        if tools_config.get("deploy") and not tools_config.get("sandbox"):
            tools_config["sandbox"] = True
            validation_warnings.append("deploy membutuhkan sandbox — sandbox otomatis diaktifkan")

        if tools_config.get("tool_creator") and not tools_config.get("sandbox"):
            tools_config["sandbox"] = True
            validation_warnings.append("tool_creator membutuhkan sandbox — sandbox otomatis diaktifkan")

        # Channel validation
        effective_channel = channel or preset.get("default_channel", "webchat")
        if effective_channel == "whatsapp" and not tools_config.get("escalation"):
            validation_warnings.append("Agent WhatsApp sebaiknya mengaktifkan escalation untuk operator handoff")

        effective_model = preset.get("default_model", _DEFAULT_MODEL)
        creation_entitlement_check = await _preview_agent_creation_entitlement(
            tools_config=tools_config,
            model=effective_model,
            channel_type=effective_channel if effective_channel != "webchat" else "",
        )
        entitlement_blocked = bool(
            creation_entitlement_check.get("checked")
            and not creation_entitlement_check.get("allowed", True)
        )
        if entitlement_blocked:
            entitlement_message = (
                creation_entitlement_check.get("user_message")
                or creation_entitlement_check.get("reason")
                or "Paket kamu belum bisa membuat agent ini."
            )
            validation_errors.append(entitlement_message)
            for violation in creation_entitlement_check.get("violations") or []:
                validation_errors.append(str(violation))
        elif not creation_entitlement_check.get("checked"):
            validation_warnings.append(
                "Cek tier/slot awal belum bisa diverifikasi; create_agent tetap akan melakukan hard gate sebelum menyimpan agent."
            )

        # Surface critical limitations
        critical_limitations = []
        for lid in preset.get("runtime_limitations", []):
            lim = RUNTIME_LIMITATIONS.get(lid)
            if lim:
                if lim["severity"] == "critical":
                    critical_limitations.append(lim["user_message"])
                elif lim["severity"] == "warning":
                    validation_warnings.append(lim["user_message"])

        # Build recommended config
        plan_status = (
            "blocked_by_subscription"
            if entitlement_blocked
            else "ready" if not validation_errors else "has_errors"
        )
        plan = {
            "plan_status": plan_status,
            "detected_preset": detected_preset,
            "preset_label": preset.get("label", "Custom"),
            "agent_name": agent_name or f"Agent {detected_preset.replace('_', ' ').title()}",
            "business_goal": user_goal,
            "channel": effective_channel,
            "persona": persona or "ramah dan profesional",
            "business_context": business_context,
            "blueprint_seed": {
                "agent_summary": f"{agent_name or 'Agent ini'} dibuat untuk {user_goal}",
                "customization_goal": (
                    "Gunakan compose_agent_blueprint jika agent perlu SOP/workflow spesifik per bisnis, "
                    "produk, tim, atau industri. Jangan hanya mengandalkan persona generik."
                ),
                "known_business_context": business_context,
                "requested_features": features,
                "design_considerations": [
                    "Langkah kerja ideal agent dari awal sampai selesai.",
                    "Data yang wajib dikumpulkan dari user/pelanggan.",
                    "Pengetahuan produk/SOP yang wajib agent tahu.",
                    "Kapan agent harus eskalasi ke manusia.",
                    "Apakah ada pembayaran, approval admin, atau deliverable yang baru boleh dikirim setelah disetujui.",
                ],
            },
            "recommended_config": {
                "model": effective_model,
                "temperature": preset.get("default_temperature", 0.7),
                "max_tokens": preset.get("default_max_tokens", 1024),
                "tools_config": tools_config,
                "channel_type": effective_channel if effective_channel != "webchat" else "",
                "escalation_config": (
                    {"channel_type": "whatsapp", "operator_phone": operator_phone}
                    if operator_phone else {}
                ),
            },
            "required_post_create_steps": _get_post_create_steps(detected_preset, effective_channel, tools_config),
            "validation_errors": validation_errors,
            "validation_warnings": validation_warnings,
            "critical_limitations": critical_limitations,
            "creation_entitlement_check": creation_entitlement_check,
            "google_workspace_option": google_workspace_option,
            "smoke_test_guidance": preset.get("smoke_test", {}).get("steps", []),
            "next_action": (
                "Jelaskan limit paket dengan bahasa sederhana dan tawarkan upgrade/top up sebelum lanjut membuat agent."
                if entitlement_blocked
                else
                (
                    "Tawarkan opsi integrasi Google Workspace dengan bahasa awam memakai google_workspace_option.user_facing_pitch. "
                    "Jika user setuju, panggil plan_agent lagi dengan requested_features berisi google sebelum create. "
                    "Jika user menolak, lanjutkan compose_agent_blueprint/compose_agent_instructions tanpa Google."
                )
                if google_workspace_option.get("should_offer") and not validation_errors
                else "Untuk agent bisnis/custom, panggil compose_agent_blueprint lalu compose_agent_instructions. "
                "Setelah itu validate_agent_config dan create_agent tanpa minta approval mikro."
                if not validation_errors
                else "Perbaiki validation_errors sebelum create."
            ),
        }
        return json.dumps(plan, ensure_ascii=False, indent=2)


    return {"plan_agent": plan_agent}
