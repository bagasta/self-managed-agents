from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

import pytest

from app.core.tools.builder_pipeline_tools import build_builder_pipeline_tools


class StubTool:
    def __init__(self, handler: Callable[[dict[str, Any]], Awaitable[Any]] | Any):
        self.handler = handler
        self.calls: list[dict[str, Any]] = []

    async def ainvoke(self, payload: dict[str, Any]) -> Any:
        self.calls.append(payload)
        if callable(self.handler):
            return await self.handler(payload)
        return self.handler


class StubLogger:
    def bind(self, **kwargs):
        return self

    def info(self, *args, **kwargs):
        return None

    def warning(self, *args, **kwargs):
        return None

    def error(self, *args, **kwargs):
        return None


def _dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _ready_plan() -> str:
    return _dump(
        {
            "plan_status": "ready",
            "detected_preset": "cs_whatsapp_basic",
            "agent_name": "Bakmi Sales Assistant",
            "recommended_config": {
                "model": "deepseek/deepseek-v4-flash",
                "temperature": 0.4,
                "max_tokens": 700,
                "channel_type": "whatsapp",
                "tools_config": {
                    "memory": True,
                    "escalation": True,
                    "whatsapp_media": True,
                },
                "escalation_config": {},
            },
        }
    )


@pytest.mark.asyncio
async def test_optimized_pipeline_preserves_writers_and_parallelizes_independent_work() -> None:
    writer_started: set[str] = set()
    all_writers_started = asyncio.Event()

    async def writer(label: str, result: dict[str, Any], _: dict[str, Any]) -> str:
        writer_started.add(label)
        if len(writer_started) == 3:
            all_writers_started.set()
        await asyncio.wait_for(all_writers_started.wait(), timeout=1)
        return _dump(result)

    manual = StubTool(
        lambda payload: writer(
            "manual",
            {"operating_manual": {"workflows": [{"workflow_id": "sales"}]}},
            payload,
        )
    )
    instructions_text = (
        "Kamu adalah Bakmi Sales Assistant. Kumpulkan nama, menu, jumlah, alamat, dan pembayaran. "
        "Gunakan escalate_to_human saat keputusan Owner diperlukan. Contoh percakapan: User memesan bakmi, "
        "agent mengonfirmasi rincian dan menyimpan status transaksi sampai selesai."
    )
    instruction = StubTool(
        lambda payload: writer(
            "instructions",
            {"instructions": instructions_text, "remaining_placeholders": []},
            payload,
        )
    )
    soul = StubTool(
        lambda payload: writer(
            "soul",
            {
                "soul": (
                    "IDENTITAS Bakmi Sales Assistant dibuat oleh Arthur dan membantu Owner mencatat penjualan "
                    "secara teliti, ramah, konsisten, serta selalu melakukan eskalasi ketika keputusan manusia dibutuhkan."
                ),
                "remaining_placeholders": [],
            },
            payload,
        )
    )
    create = StubTool(
        _dump(
            {
                "success": True,
                "agent_id": "00000000-0000-0000-0000-000000000123",
                "name": "Bakmi Sales Assistant",
                "channel_type": "whatsapp",
            }
        )
    )
    connector = StubTool(_dump({"connected": True, "auth_url": ""}))
    tools = build_builder_pipeline_tools(
        plan_tool=StubTool(_ready_plan()),
        blueprint_tool=StubTool(
            _dump({"blueprint": {"workflow_steps": [{"step": 1, "name": "intake"}]}})
        ),
        manual_tool=manual,
        instruction_tool=instruction,
        soul_tool=soul,
        validation_tool=StubTool(_dump({"valid": True, "quality_score": 90})),
        create_tool=create,
        verify_tool=StubTool(_dump({"status": "launch_ready_with_warnings"})),
        connector_tool=connector,
        get_settings=lambda: SimpleNamespace(arthur_builder_min_quality_score=60),
        get_logger=lambda: StubLogger(),
    )

    result = json.loads(
        await tools["create_agent_from_brief"].ainvoke(
            {
                "user_goal": "mencatat penjualan Bakmi dari WhatsApp",
                "agent_name": "Bakmi Sales Assistant",
                "business_context": "Warung Bakmi menerima pesanan dan pembayaran transfer.",
                "target_users": "pelanggan warung",
                "operator_phone": "+628111111111",
            }
        )
    )

    assert result["success"] is True
    assert result["pipeline"] == "optimized"
    assert result["quality_gate"]["quality_score"] == 90
    assert writer_started == {"manual", "instructions", "soul"}
    assert len(create.calls) == 1
    assert create.calls[0]["instructions"] == instructions_text
    assert create.calls[0]["operating_manual"]["workflows"]
    assert create.calls[0]["blueprint"]
    assert create.calls[0]["soul"]
    assert create.calls[0]["model"] == "deepseek/deepseek-v4-flash"
    assert connector.calls == []


@pytest.mark.asyncio
async def test_optimized_pipeline_falls_back_before_create_when_quality_gate_fails() -> None:
    instructions_text = "Kamu adalah agent yang membantu user. " * 10
    create = StubTool(_dump({"success": True, "agent_id": "must-not-be-created"}))
    tools = build_builder_pipeline_tools(
        plan_tool=StubTool(_ready_plan()),
        blueprint_tool=StubTool(
            _dump({"blueprint": {"workflow_steps": [{"step": 1, "name": "intake"}]}})
        ),
        manual_tool=StubTool(
            _dump({"operating_manual": {"workflows": [{"workflow_id": "sales"}]}})
        ),
        instruction_tool=StubTool(
            _dump({"instructions": instructions_text, "remaining_placeholders": []})
        ),
        soul_tool=StubTool(
            _dump(
                {
                    "soul": "IDENTITAS Agent dibuat Arthur dan bekerja konsisten untuk Owner serta meminta keputusan manusia saat dibutuhkan.",
                    "remaining_placeholders": [],
                }
            )
        ),
        validation_tool=StubTool(
            _dump(
                {
                    "valid": False,
                    "quality_score": 20,
                    "errors": ["workflow tidak lengkap"],
                    "warnings": [],
                    "suggestions": [],
                }
            )
        ),
        create_tool=create,
        verify_tool=StubTool(_dump({"status": "unused"})),
        connector_tool=StubTool(_dump({"connected": False})),
        get_settings=lambda: SimpleNamespace(arthur_builder_min_quality_score=60),
        get_logger=lambda: StubLogger(),
    )

    result = json.loads(
        await tools["create_agent_from_brief"].ainvoke(
            {"user_goal": "mencatat penjualan", "business_context": "Warung Bakmi"}
        )
    )

    assert result["success"] is False
    assert result["creation_attempted"] is False
    assert result["fallback_to_legacy"] is True
    assert result["error"] == "optimized_quality_gate_failed"
    assert create.calls == []
