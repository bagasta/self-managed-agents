"""Tests for the WA agent manager tool (send_agent_wa_qr).

Regression for the production incident where Arthur sent the connect-QR to a
chat-typed number (6289477477238) instead of the verified session owner
(62895619356936), so the owner never received it.
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


class TestSendAgentWaQr:
    def test_qr_goes_to_verified_owner_not_chat_typed_phone(self):
        from app.core.engine.tool_builder import build_wa_agent_manager_tools

        agent_id = uuid.uuid4()
        agent_row = SimpleNamespace(id=agent_id, wa_device_id="agent-device", channel_type="whatsapp")
        db = _make_mock_db(agent_row)

        session = _session_with(
            owner_phone="62895619356936",          # verified sender owner
            user_phone="151414827434073@lid",       # LID (must never be the target)
        )

        sent: dict[str, str] = {}

        async def _fake_get_wa_qr(_dev):
            return {"status": "", "qr_image": "data:image/png;base64,QUJD"}

        async def _fake_send_wa_image(device_id, to, b64, caption, mime):
            sent["device_id"] = device_id
            sent["to"] = to
            return {"status": "ok"}

        tools = build_wa_agent_manager_tools(session, db_factory=db)
        tool = next(t for t in tools if t.name == "send_agent_wa_qr")

        with patch("app.core.infra.wa_client.get_wa_qr", _fake_get_wa_qr), patch(
            "app.core.infra.wa_client.send_wa_image", _fake_send_wa_image
        ):
            result = _run(tool.ainvoke({
                "agent_id": str(agent_id),
                "phone": "6289477477238",  # chat-typed wrong number — must be ignored
            }))

        # Sent from Arthur's own device, to the VERIFIED owner — not the chat number, not the LID.
        assert sent.get("device_id") == "arthur-device"
        assert sent.get("to") == "62895619356936"
        assert "[QR_SENT]" in result
        assert "62895619356936" in result

    def test_qr_never_targets_lid_when_no_verified_owner(self):
        from app.core.engine.tool_builder import build_wa_agent_manager_tools

        agent_id = uuid.uuid4()
        agent_row = SimpleNamespace(id=agent_id, wa_device_id="agent-device", channel_type="whatsapp")
        db = _make_mock_db(agent_row)

        # No verified phone_number; only a LID user_phone available.
        session = _session_with(owner_phone=None, user_phone="151414827434073@lid")

        async def _fake_get_wa_qr(_dev):
            return {"status": "", "qr_image": "QUJD"}

        send_called = {"hit": False}

        async def _fake_send_wa_image(*_a, **_k):
            send_called["hit"] = True
            return {"status": "ok"}

        tools = build_wa_agent_manager_tools(session, db_factory=db)
        tool = next(t for t in tools if t.name == "send_agent_wa_qr")

        with patch("app.core.infra.wa_client.get_wa_qr", _fake_get_wa_qr), patch(
            "app.core.infra.wa_client.send_wa_image", _fake_send_wa_image
        ):
            result = _run(tool.ainvoke({"agent_id": str(agent_id)}))

        # A LID is not a real WhatsApp number — refuse rather than mis-send.
        assert send_called["hit"] is False
        assert "[error]" in result
