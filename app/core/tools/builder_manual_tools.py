"""Operating manual writer tool for Arthur builder."""
from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

import structlog
from langchain_core.tools import tool

from app.core.domain.agent_sop_service import (
    build_agent_operating_manual_from_blueprint,
    format_operating_manual_for_prompt,
    summarize_operating_manual,
)
from app.core.tools.builder_catalog import AGENT_PRESETS
from app.core.tools.builder_fallbacks import _fallback_agent_blueprint
from app.core.tools.builder_json import parse_llm_json_object as _parse_llm_json_object

logger = structlog.get_logger(__name__)

_BLUEPRINT_WRITER_MODEL = "deepseek/deepseek-v4-pro"

InstructionWriter = Callable[..., Awaitable[str]]
LoggerProvider = Callable[[], Any]


def build_builder_manual_tools(
    *,
    call_instruction_writer: InstructionWriter,
    get_logger: LoggerProvider | None = None,
) -> dict[str, Any]:
    _call_instruction_writer = call_instruction_writer
    _get_logger = get_logger or (lambda: logger)

    @tool
    async def compose_agent_operating_manual(
        preset_id: str,
        user_goal: str,
        agent_name: str = "",
        business_context: str = "",
        agent_blueprint: str = "",
        target_users: str = "",
        channel: str = "whatsapp",
        requested_features: str = "",
        known_constraints: str = "",
        domain: str = "",
    ) -> str:
        """
        Susun Agent Operating Manual/SOP terstruktur dari kebutuhan user dan blueprint.

        SOP ini adalah kontrak kerja runtime agent: workflow, data wajib, state,
        eskalasi, approval manusia, larangan, dan definisi selesai. Gunakan hasil
        `operating_manual` sebagai parameter create_agent/update_agent.

        Args:
            preset_id: Preset yang digunakan dari plan_agent.
            user_goal: Tujuan utama user.
            agent_name: Nama agent.
            business_context: Detail bisnis/SOP/kebijakan yang user berikan.
            agent_blueprint: JSON/string hasil compose_agent_blueprint.
            target_users: Siapa yang akan menggunakan agent.
            channel: whatsapp.
            requested_features: Fitur yang diminta user.
            known_constraints: Batasan penting/compliance.
            domain: Domain bisnis jika diketahui.
        """
        preset = AGENT_PRESETS.get(preset_id, {})
        tc = preset.get("tools_config", {})

        def _fallback_response(parse_status: str) -> str:
            manual = build_agent_operating_manual_from_blueprint(
                agent_blueprint,
                name=agent_name or "Agent",
                description=user_goal,
                business_context=business_context,
                domain=domain,
                tools_config=tc,
            )
            if manual is None:
                manual = build_agent_operating_manual_from_blueprint(
                    _fallback_agent_blueprint(
                        preset_id=preset_id,
                        user_goal=user_goal,
                        agent_name=agent_name,
                        business_context=business_context,
                        target_users=target_users,
                        channel=channel,
                        requested_features=requested_features,
                        known_constraints=known_constraints,
                        tools_config=tc,
                    ),
                    name=agent_name or "Agent",
                    description=user_goal,
                    business_context=business_context,
                    domain=domain,
                    tools_config=tc,
                )
            manual = manual or {}
            return json.dumps({
                "operating_manual": manual,
                "summary": summarize_operating_manual(manual),
                "parse_status": parse_status,
                "prompt_preview": format_operating_manual_for_prompt(manual)[:1800],
                "next_step": (
                    "Gunakan operating_manual ini sebagai parameter create_agent/update_agent. "
                    "Jangan membuat agent bisnis tanpa SOP ini kecuali user hanya meminta agent coding sederhana."
                ),
            }, ensure_ascii=False, indent=2)

        system_msg = (
            "Kamu adalah senior operations designer untuk AI agent. "
            "Tugasmu mengubah kebutuhan user dan Agent Blueprint menjadi Agent Operating Manual/SOP yang konkret, spesifik, dan siap dipakai runtime. "
            "Jangan membuat SOP generik. Tulis seperti SOP pekerja manusia: state kerja, data wajib, langkah tindakan, decision points, handoff manusia, larangan, dan output akhir. "
            "Jika ada pembayaran, bukti transfer, approval admin, booking, refund, deliverable, file, atau integrasi akun, SOP harus menyebut kapan agent boleh lanjut dan kapan wajib berhenti/eskalasi. "
            "Return HANYA JSON valid, tanpa markdown dan tanpa penjelasan di luar JSON."
        )
        user_msg = (
            "Buat Agent Operating Manual/SOP dari data berikut.\n\n"
            f"preset_id: {preset_id}\n"
            f"preset_label: {preset.get('label', 'Custom')}\n"
            f"agent_name: {agent_name or 'belum ditentukan'}\n"
            f"user_goal: {user_goal}\n"
            f"business_context: {business_context or 'belum ada detail bisnis'}\n"
            f"target_users: {target_users or 'belum jelas'}\n"
            f"channel: {channel}\n"
            f"requested_features: {requested_features or '-'}\n"
            f"known_constraints: {known_constraints or '-'}\n"
            f"domain: {domain or '-'}\n"
            f"tools_config: {json.dumps(tc, ensure_ascii=False)}\n"
            f"agent_blueprint: {agent_blueprint or '-'}\n\n"
            "Schema JSON wajib:\n"
            "{\n"
            '  "manual_id": "agent_operating_manual",\n'
            '  "version": 1,\n'
            '  "source": "arthur_operating_manual_writer",\n'
            '  "domain": "domain bisnis spesifik",\n'
            '  "domain_confidence": "high|medium|low",\n'
            '  "maturity": "usable",\n'
            '  "owner_review_required": false,\n'
            '  "missing_context": [],\n'
            '  "assumptions": ["asumsi operasional yang dibuat"],\n'
            '  "workflows": [{\n'
            '    "workflow_id": "snake_case_id",\n'
            '    "name": "Nama workflow",\n'
            '    "trigger": "Kapan workflow dimulai",\n'
            '    "goal": "Tujuan workflow",\n'
            '    "required_inputs": ["data wajib"],\n'
            '    "steps": ["langkah konkret berurutan"],\n'
            '    "decision_points": ["kondisi dan pilihan keputusan"],\n'
            '    "allowed_tools": ["tool yang boleh dipakai"],\n'
            '    "escalation_rules": ["kapan dan cara eskalasi"],\n'
            '    "prohibited_actions": ["hal yang tidak boleh dilakukan"],\n'
            '    "final_output": "Definisi selesai yang nyata",\n'
            '    "examples": ["contoh pendek jika perlu"]\n'
            "  }],\n"
            '  "knowledge_plan": {"must_have": ["..."], "nice_to_have": ["..."], "needs_upload": false},\n'
            '  "memory_plan": [{"key": "...", "value_to_store": "..."}],\n'
            '  "validation_checklist": ["..."]\n'
            "}\n\n"
            "Aturan kualitas:\n"
            "- workflows minimal 2 untuk agent bisnis/custom, kecuali agent sangat sederhana.\n"
            "- Untuk payment/approval/delivery, workflow wajib memisahkan intake, payment_review, approved/fulfillment, delivery, dan aftercare.\n"
            "- Jika business_context cukup, maturity harus usable dan owner_review_required false.\n"
            "- Jika ada data kritis belum ada, isi missing_context dengan data itu dan set maturity needs_review.\n"
            "- Jangan menaruh SOP lengkap di instructions; SOP ini disimpan sebagai operating_manual terpisah."
        )

        try:
            raw = await _call_instruction_writer(
                user_msg,
                system_msg,
                model=_BLUEPRINT_WRITER_MODEL,
                max_tokens=7000,
                temperature=0.15,
                json_mode=True,
            )
        except Exception as exc:
            # Manual generation has a deterministic fallback. A transient
            # writer timeout/error should therefore not look like a failed
            # agent run, and ``str(asyncio.TimeoutError())`` is empty; retain
            # the exception type so the fallback remains diagnosable.
            _get_logger().warning(
                "builder_tools.compose_agent_operating_manual.writer_fallback",
                error_type=type(exc).__name__,
                error=str(exc) or repr(exc),
            )
            return _fallback_response("deterministic_fallback")

        try:
            manual, repaired_json = _parse_llm_json_object(raw)
        except Exception as exc:
            _get_logger().warning(
                "builder_tools.compose_agent_operating_manual.parse_failed",
                error=str(exc),
                preset_id=preset_id,
                agent_name=agent_name,
                output_preview=(raw or "")[:240],
            )
            return _fallback_response("deterministic_fallback")

        manual.setdefault("manual_id", "agent_operating_manual")
        manual.setdefault("version", 1)
        manual.setdefault("source", "arthur_operating_manual_writer")
        manual.setdefault("domain", domain or "generic")
        manual.setdefault("domain_confidence", "medium")
        manual.setdefault("maturity", "usable")
        manual.setdefault("owner_review_required", manual.get("maturity") in {"draft", "needs_review"})
        manual.setdefault("missing_context", [])
        manual.setdefault("assumptions", [])
        if not isinstance(manual.get("workflows"), list) or not manual["workflows"]:
            _get_logger().warning(
                "builder_tools.compose_agent_operating_manual.empty_workflows",
                preset_id=preset_id,
                agent_name=agent_name,
            )
            return _fallback_response("deterministic_fallback")

        summary = summarize_operating_manual(manual)
        payload = {
            "operating_manual": manual,
            "summary": summary,
            "prompt_preview": format_operating_manual_for_prompt(manual)[:1800],
            "next_step": (
                "Gunakan operating_manual ini sebagai parameter create_agent/update_agent. "
                "Setelah itu validate_agent_config dan create_agent/update_agent tanpa minta approval mikro."
            ),
        }
        if repaired_json:
            _get_logger().warning(
                "builder_tools.compose_agent_operating_manual.json_repaired",
                preset_id=preset_id,
                agent_name=agent_name,
            )
            payload["parse_status"] = "json_repaired"
        return json.dumps(payload, ensure_ascii=False, indent=2)

    return {"compose_agent_operating_manual": compose_agent_operating_manual}
