"""Tests for the WA agent manager tool (send_agent_wa_pairing_code).

Regression for pairing-code ownership: the code must be generated only for the
verified session owner, never a chat-typed number or LID.
"""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_mock_db(agent_row):
    db = MagicMock()
    db.return_value.__aenter__.return_value = db
    db.commit = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = agent_row
    db.execute = AsyncMock(return_value=result)
    return db


def _session_with(owner_phone: str | None, user_phone: str, device_id: str = "arthur-device"):
    cfg = {"device_id": device_id, "user_phone": user_phone}
    if owner_phone is not None:
        cfg["phone_number"] = owner_phone
    return SimpleNamespace(channel_config=cfg)


class TestSendAgentWaPairingCode:
    def test_pairing_code_uses_verified_owner_not_chat_typed_phone(self):
        from app.core.engine.tool_builder import build_wa_agent_manager_tools

        agent_id = uuid.uuid4()
        agent_row = SimpleNamespace(id=agent_id, wa_device_id="agent-device", channel_type="whatsapp")
        db = _make_mock_db(agent_row)

        session = _session_with(
            owner_phone="62895619356936",          # verified sender owner
            user_phone="151414827434073@lid",       # LID (must never be the target)
        )

        requested: dict[str, str] = {}

        async def _fake_create_pairing_code(device_id, phone):
            requested["device_id"] = device_id
            requested["phone"] = phone
            return {"pairing_code": "ABCD-EFGH", "expires_in_seconds": 160}

        tools = build_wa_agent_manager_tools(session, db_factory=db)
        tool = next(t for t in tools if t.name == "send_agent_wa_pairing_code")

        with patch("app.core.infra.wa_client.create_wa_pairing_code", _fake_create_pairing_code):
            result = _run(tool.ainvoke({"agent_id": str(agent_id)}))

        assert requested == {"device_id": "agent-device", "phone": "62895619356936"}
        assert "[PAIRING_CODE]" in result
        assert "62895619356936" in result

    def test_pairing_code_never_targets_lid_when_no_verified_owner(self):
        from app.core.engine.tool_builder import build_wa_agent_manager_tools

        agent_id = uuid.uuid4()
        agent_row = SimpleNamespace(id=agent_id, wa_device_id="agent-device", channel_type="whatsapp")
        db = _make_mock_db(agent_row)

        # No verified phone_number; only a LID user_phone available.
        session = _session_with(owner_phone=None, user_phone="151414827434073@lid")

        called = {"hit": False}

        async def _fake_create_pairing_code(*_a, **_k):
            called["hit"] = True
            return {"pairing_code": "ABCD-EFGH"}

        tools = build_wa_agent_manager_tools(session, db_factory=db)
        tool = next(t for t in tools if t.name == "send_agent_wa_pairing_code")

        with patch("app.core.infra.wa_client.create_wa_pairing_code", _fake_create_pairing_code):
            result = _run(tool.ainvoke({"agent_id": str(agent_id)}))

        # A LID is not a real WhatsApp number — refuse rather than mis-send.
        assert called["hit"] is False
        assert "[error]" in result
