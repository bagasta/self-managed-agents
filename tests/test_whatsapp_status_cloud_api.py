from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api import agents


@pytest.mark.asyncio
async def test_legacy_agent_without_device_remains_not_found(monkeypatch):
    agent = SimpleNamespace(
        wa_connection_type=None,
        wa_device_id=None,
        wa_phone_number_id=None,
        wa_waba_id=None,
        wa_access_token_encrypted=None,
    )
    monkeypatch.setattr(agents, "_get_active_agent", AsyncMock(return_value=agent))

    with pytest.raises(HTTPException) as exc_info:
        await agents.get_whatsapp_status(uuid4(), db=object(), _="test")

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Agent does not have a WhatsApp channel configured"


@pytest.mark.asyncio
async def test_cloud_api_agent_without_device_is_connected_without_wa_service(monkeypatch):
    cloud_agent = SimpleNamespace(
        wa_connection_type="cloud_api",
        wa_device_id=None,
        wa_phone_number_id="phone-number-id",
        wa_waba_id="waba-id",
        wa_access_token_encrypted="enc:encrypted-token",
        wa_display_phone="+62 812-0000-0000",
        wa_business_name="Arthur Business",
    )
    monkeypatch.setattr(agents, "_get_active_agent", AsyncMock(return_value=cloud_agent))

    result = await agents.get_whatsapp_status(uuid4(), db=object(), _="test")

    assert result.status == "connected"
    assert result.connection_type == "cloud_api"
    assert result.device_id is None
    assert result.phone_number == "+62 812-0000-0000"
    assert result.business_name == "Arthur Business"


def test_dashboard_does_not_start_qr_flow_for_cloud_api_agents():
    from pathlib import Path

    script = Path("UI-DEV/app.js").read_text()
    cloud_branch = script.split(
        "const isCloudAPI = agent.wa_connection_type === 'cloud_api';", 1
    )[1].split("return;", 1)[0]

    assert "Terhubung via Meta Cloud API" in cloud_branch
    assert "wa-connect-panel').style.display = 'none'" in cloud_branch
    assert "refreshWAStatus" not in cloud_branch
    assert "refreshWAQR" not in cloud_branch
