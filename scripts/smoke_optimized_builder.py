"""Live-writer smoke test for the optimized builder without database writes."""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any

from app.config import get_settings
from app.core.tools.builder_blueprint_tools import build_builder_blueprint_tools
from app.core.tools.builder_instruction_tools import build_builder_instruction_tools
from app.core.tools.builder_manual_tools import build_builder_manual_tools
from app.core.tools.builder_pipeline_tools import build_builder_pipeline_tools
from app.core.tools.builder_planning_tools import build_builder_planning_tools
from app.core.tools.builder_soul_tools import build_builder_soul_tools
from app.core.tools.builder_tools import _call_optimized_instruction_writer
from app.core.tools.builder_validation_tools import build_builder_validation_tools


class StubTool:
    def __init__(self, result: dict[str, Any]):
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def ainvoke(self, payload: dict[str, Any]) -> str:
        self.calls.append(payload)
        return json.dumps(self.result, ensure_ascii=False)


class SmokeLogger:
    def bind(self, **kwargs):
        return self

    def info(self, event: str, **kwargs):
        print(json.dumps({"event": event, **kwargs}, ensure_ascii=False))

    def warning(self, event: str, **kwargs):
        print(json.dumps({"event": event, "level": "warning", **kwargs}, ensure_ascii=False))

    def error(self, event: str, **kwargs):
        print(json.dumps({"event": event, "level": "error", **kwargs}, ensure_ascii=False))


async def _entitlement_preview(**kwargs) -> dict[str, Any]:
    return {"checked": True, "allowed": True, "plan": "smoke-test"}


async def main() -> None:
    log = SmokeLogger()
    plan = build_builder_planning_tools(
        preview_agent_creation_entitlement=_entitlement_preview,
    )["plan_agent"]
    blueprint = build_builder_blueprint_tools(
        call_instruction_writer=_call_optimized_instruction_writer,
        get_logger=lambda: log,
    )["compose_agent_blueprint"]
    manual = build_builder_manual_tools(
        call_instruction_writer=_call_optimized_instruction_writer,
        get_logger=lambda: log,
    )["compose_agent_operating_manual"]
    instructions = build_builder_instruction_tools(
        call_instruction_writer=_call_optimized_instruction_writer,
        get_logger=lambda: log,
    )["compose_agent_instructions"]
    soul = build_builder_soul_tools(
        call_instruction_writer=_call_optimized_instruction_writer,
    )["compose_agent_soul"]
    validation = build_builder_validation_tools()["validate_agent_config"]
    create_stub = StubTool(
        {
            "success": True,
            "agent_id": "00000000-0000-0000-0000-000000000999",
            "name": "Bakmi Sales Assistant",
            "channel_type": "whatsapp",
        }
    )
    pipeline = build_builder_pipeline_tools(
        plan_tool=plan,
        blueprint_tool=blueprint,
        manual_tool=manual,
        instruction_tool=instructions,
        soul_tool=soul,
        validation_tool=validation,
        create_tool=create_stub,
        verify_tool=StubTool({"status": "smoke_verified"}),
        connector_tool=StubTool({"connected": True, "auth_url": ""}),
        get_settings=lambda: SimpleNamespace(
            arthur_builder_min_quality_score=get_settings().arthur_builder_min_quality_score,
        ),
        get_logger=lambda: log,
    )["create_agent_from_brief"]

    started = time.monotonic()
    raw = await pipeline.ainvoke(
        {
            "user_goal": (
                "Buat customer service Warung Bakmi di WhatsApp. Agent menjawab informasi menu dan jam buka, "
                "mencatat reservasi meja dengan mengumpulkan nama, nomor telepon, tanggal, jam, jumlah tamu, "
                "dan catatan, meminta konfirmasi, lalu meneruskan perubahan khusus ke Owner."
            ),
            "agent_name": "Bakmi Sales Assistant",
            "requested_features": "WhatsApp, memory, escalation, hanya teks",
            "persona": "ramah, cepat, teliti, dan tidak mengarang harga atau ketersediaan meja",
            "business_context": (
                "Warung Bakmi menjual Bakmi Ayam dan Bakmi Yamin serta menerima reservasi meja. "
                "Agent tidak boleh mengarang harga, jam buka, atau ketersediaan meja. Jika informasi belum ada, "
                "ada komplain, atau customer meminta perubahan khusus, agent wajib eskalasi ke Owner. "
                "Reservasi selesai ketika seluruh data lengkap dan customer mengonfirmasi ringkasannya."
            ),
            "target_users": "pelanggan Warung Bakmi melalui WhatsApp",
            "operator_name": "Owner Warung Bakmi",
            "known_constraints": "Seluruh interaksi berbentuk teks dan keputusan di luar SOP harus melalui Owner.",
        }
    )
    result = json.loads(raw)
    create_payload = create_stub.calls[0] if create_stub.calls else {}
    quality_score = int((result.get("quality_gate") or {}).get("quality_score") or 0)
    instruction_chars = len(str(create_payload.get("instructions") or ""))
    soul_chars = len(str(create_payload.get("soul") or ""))
    manual_workflows = len(
        ((create_payload.get("operating_manual") or {}).get("workflows") or [])
    )
    print(
        json.dumps(
            {
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "success": result.get("success"),
                "fallback_to_legacy": result.get("fallback_to_legacy"),
                "quality_score": quality_score,
                "validation_errors": (result.get("quality_gate") or {}).get("errors") or [],
                "instruction_chars": instruction_chars,
                "soul_chars": soul_chars,
                "manual_workflows": manual_workflows,
                "database_write_attempted": False,
            },
            ensure_ascii=False,
        )
    )
    if not (
        result.get("success") is True
        and quality_score >= get_settings().arthur_builder_min_quality_score
        and instruction_chars >= 1000
        and soul_chars >= 500
        and manual_workflows >= 2
        and len(create_stub.calls) == 1
    ):
        raise SystemExit("optimized builder smoke quality gate failed")


if __name__ == "__main__":
    asyncio.run(main())
