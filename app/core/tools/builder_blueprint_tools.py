"""Blueprint writer tool for Arthur builder."""
from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

import structlog
from langchain_core.tools import tool

from app.core.tools.builder_catalog import AGENT_PRESETS
from app.core.tools.builder_fallbacks import _fallback_agent_blueprint
from app.core.tools.builder_json import parse_llm_json_object as _parse_llm_json_object

logger = structlog.get_logger(__name__)

_BLUEPRINT_WRITER_MODEL = "deepseek/deepseek-v4-pro"

InstructionWriter = Callable[..., Awaitable[str]]
LoggerProvider = Callable[[], Any]


def build_builder_blueprint_tools(
    *,
    call_instruction_writer: InstructionWriter,
    get_logger: LoggerProvider | None = None,
) -> dict[str, Any]:
    _call_instruction_writer = call_instruction_writer
    _get_logger = get_logger or (lambda: logger)

    @tool
    async def compose_agent_blueprint(
        preset_id: str,
        user_goal: str,
        agent_name: str = "",
        business_context: str = "",
        target_users: str = "",
        channel: str = "webchat",
        requested_features: str = "",
        known_constraints: str = "",
    ) -> str:
        """
        Rancang blueprint agent yang spesifik untuk kebutuhan user sebelum menulis instructions.

        Blueprint berisi workflow, data yang wajib dikumpulkan, knowledge yang dibutuhkan,
        aturan eskalasi, tool plan, dan checklist validasi. Gunakan ini untuk agent bisnis
        yang butuh SOP/custom workflow, terutama CS, ecommerce, HR, data, dan personal assistant.
        Jangan tampilkan blueprint ke user untuk minta approval mikro. Setelah tool ini sukses,
        lanjutkan langsung ke compose_agent_instructions kecuali ada data kritis yang benar-benar
        tidak bisa diinfer dari pesan, dokumen, atau konteks percakapan.

        Args:
            preset_id: Preset yang dipilih dari plan_agent
            user_goal: Tujuan utama user
            agent_name: Nama agent jika sudah ada
            business_context: Detail bisnis/produk/SOP yang user sudah jelaskan
            target_users: Siapa yang akan ngobrol dengan agent ini
            channel: whatsapp, webchat, atau channel lain
            requested_features: Fitur yang diminta user, dipisah koma
            known_constraints: Batasan penting, compliance, gaya komunikasi, atau larangan
        """
        preset = AGENT_PRESETS.get(preset_id, {})
        tc = preset.get("tools_config", {})

        system_msg = (
            "Kamu adalah solution architect untuk AI agent bisnis. "
            "Tugasmu membuat blueprint yang operasional, spesifik, dan tidak generik. "
            "Rancang agent seperti pekerja manusia sungguhan: punya role, SOP, state kerja, data wajib, batas wewenang, "
            "handoff manusia, dan kriteria selesai yang terukur. "
            "Untuk agent bisnis/jasa, wajib pikirkan alur pembayaran, approval manusia, deliverable, dan after-sales jika relevan. "
            "Return HANYA JSON valid, tanpa markdown dan tanpa penjelasan di luar JSON. "
            "Pakai double quote, koma antar-field yang valid, tanpa trailing comma, dan jangan potong objek JSON."
        )
        user_msg = (
            "Buat Agent Blueprint dari data berikut.\n\n"
            f"preset_id: {preset_id}\n"
            f"preset_label: {preset.get('label', 'Custom')}\n"
            f"agent_name: {agent_name or 'belum ditentukan'}\n"
            f"user_goal: {user_goal}\n"
            f"business_context: {business_context or 'belum ada detail bisnis'}\n"
            f"target_users: {target_users or 'belum jelas'}\n"
            f"channel: {channel}\n"
            f"requested_features: {requested_features or '-'}\n"
            f"known_constraints: {known_constraints or '-'}\n"
            f"available_tools_config: {json.dumps(tc, ensure_ascii=False)}\n\n"
            "Schema JSON wajib:\n"
            "{\n"
            '  "agent_summary": "...",\n'
            '  "assumptions": ["..."],\n'
            '  "workflow_steps": [{"step": 1, "name": "...", "agent_action": "...", "required_user_data": ["..."], "success_criteria": "..."}],\n'
            '  "knowledge_plan": {"must_have": ["..."], "nice_to_have": ["..."], "needs_upload": true},\n'
            '  "tool_plan": [{"tool": "...", "why": "...", "when_to_use": "..."}],\n'
            '  "memory_plan": [{"key": "...", "value_to_store": "..."}],\n'
            '  "state_plan": [{"state": "...", "entry_condition": "...", "allowed_actions": ["..."], "exit_condition": "..."}],\n'
            '  "human_approval_points": [{"when": "...", "operator_action": "...", "agent_next_action": "..."}],\n'
            '  "escalation_rules": [{"condition": "...", "action": "..."}],\n'
            '  "conversation_examples_needed": ["..."],\n'
            '  "validation_checklist": ["..."],\n'
            '  "missing_info_questions": ["maks 3 pertanyaan paling penting jika data belum cukup"]\n'
            "}\n\n"
            "Pastikan workflow berbeda untuk tiap konteks bisnis. Jangan isi generik seperti 'jawab pertanyaan user' saja. "
            "Jika ada pembayaran/approval/deliverable, state_plan harus memuat minimal: intake, waiting_payment, payment_review, approved, delivery, aftercare. "
            "Jika tidak relevan, buat state_plan yang sesuai preset dan tujuan user."
        )

        def _fallback_response(parse_status: str) -> str:
            fallback = _fallback_agent_blueprint(
                preset_id=preset_id,
                user_goal=user_goal,
                agent_name=agent_name,
                business_context=business_context,
                target_users=target_users,
                channel=channel,
                requested_features=requested_features,
                known_constraints=known_constraints,
                tools_config=tc,
            )
            return json.dumps({
                "blueprint": fallback,
                "parse_status": parse_status,
                "next_step": "Gunakan blueprint fallback ini untuk compose_agent_instructions jika konteks user sudah cukup; jangan minta approval mikro.",
            }, ensure_ascii=False, indent=2)

        try:
            raw = await _call_instruction_writer(
                user_msg,
                system_msg,
                model=_BLUEPRINT_WRITER_MODEL,
                max_tokens=6000,
                temperature=0.2,
                json_mode=True,
            )
        except Exception as exc:
            _get_logger().error("builder_tools.compose_agent_blueprint.error", error=str(exc))
            return _fallback_response("deterministic_fallback")

        try:
            blueprint, repaired_json = _parse_llm_json_object(raw)
        except Exception as exc:
            _get_logger().warning(
                "builder_tools.compose_agent_blueprint.parse_failed",
                error=str(exc),
                preset_id=preset_id,
                agent_name=agent_name,
                output_preview=(raw or "")[:240],
            )
            return _fallback_response("deterministic_fallback")

        payload = {
            "blueprint": blueprint,
            "next_step": (
                "Gunakan blueprint ini sebagai agent_blueprint saat compose_agent_instructions. "
                "Jangan minta user menyetujui blueprint. Tanya user hanya jika missing_info_questions "
                "berisi blocker kritis yang tidak bisa diinfer; selain itu lanjutkan create flow."
            ),
        }
        if repaired_json:
            _get_logger().warning(
                "builder_tools.compose_agent_blueprint.json_repaired",
                preset_id=preset_id,
                agent_name=agent_name,
            )
            payload["parse_status"] = "json_repaired"
        return json.dumps(payload, ensure_ascii=False, indent=2)

    return {"compose_agent_blueprint": compose_agent_blueprint}
