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


@pytest.mark.asyncio
async def test_cloud_api_agent_without_device_can_disconnect_for_a_new_number(monkeypatch):
    cloud_agent = SimpleNamespace(
        wa_connection_type="cloud_api",
        wa_device_id=None,
        wa_phone_number_id="phone-number-id",
        wa_waba_id="waba-id",
        wa_access_token_encrypted="enc:encrypted-token",
        wa_display_phone="+62 812-0000-0000",
        wa_business_name="Arthur Business",
        channel_type="whatsapp",
        version=7,
    )
    db = SimpleNamespace(flush=AsyncMock())
    monkeypatch.setattr(agents, "_get_active_agent", AsyncMock(return_value=cloud_agent))

    await agents.disconnect_whatsapp(uuid4(), db=db, _="test")

    assert cloud_agent.wa_connection_type is None
    assert cloud_agent.wa_phone_number_id is None
    assert cloud_agent.wa_waba_id is None
    assert cloud_agent.wa_access_token_encrypted is None
    assert cloud_agent.wa_display_phone is None
    assert cloud_agent.wa_business_name is None
    assert cloud_agent.channel_type is None
    assert cloud_agent.version == 8
    db.flush.assert_awaited_once()


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


def test_arthur_dashboard_uses_cloud_api_status_without_a_legacy_device():
    from pathlib import Path

    script = Path("UI-DEV/app.js").read_text()
    arthur_branch = script.split("const isCloudAPI = Arthur.connectionType === 'cloud_api';", 1)[1].split("panel.innerHTML =", 1)[0]

    assert "(isCloudAPI || arthur.wa_device_id)" in arthur_branch
    assert "Terhubung via Meta Cloud API" in arthur_branch
    assert "arthurStopQRPoller()" in arthur_branch
    assert "arthur-legacy-qr-controls" in arthur_branch


def test_arthur_dashboard_explains_cloud_api_disconnect_before_number_change():
    from pathlib import Path

    script = Path("UI-DEV/app.js").read_text()

    assert "Nomor tidak dihapus dari Meta" in script
    assert "siap hubungkan nomor Meta lain" in script
