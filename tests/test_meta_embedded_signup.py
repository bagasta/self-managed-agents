from __future__ import annotations

import base64
import hashlib
import hmac
import inspect
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api import meta_signup
from app.core.infra import meta_embedded_signup as signup


@pytest.fixture(autouse=True)
def meta_settings(monkeypatch):
    monkeypatch.setattr(
        signup,
        "_settings",
        lambda: SimpleNamespace(
            meta_app_id="test-app",
            meta_app_secret="test-secret",
            meta_embedded_signup_config_id="test-config",
            meta_signup_state_ttl_seconds=60,
            meta_graph_api_version="v26.0",
        ),
    )


def test_signup_state_is_signed_and_bound_to_agent():
    state = signup.build_signup_state("2b149a50-9f54-4c26-b9d6-e82d5d699859")

    assert len(state) == 45
    assert str(signup.verify_signup_state(state)) == "2b149a50-9f54-4c26-b9d6-e82d5d699859"
    with pytest.raises(ValueError, match="Invalid or expired"):
        signup.verify_signup_state(state + "x")


def test_legacy_signup_state_remains_valid_until_expiry():
    payload = {"agent_id": "2b149a50-9f54-4c26-b9d6-e82d5d699859", "exp": int(time.time()) + 60, "nonce": "legacy"}
    encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    signature = hmac.new(b"test-secret", encoded.encode(), hashlib.sha256).hexdigest()

    assert str(signup.verify_signup_state(f"{encoded}.{signature}")) == payload["agent_id"]


def test_short_launch_path_is_registered_for_whatsapp_friendly_links():
    paths = {route.path for route in meta_signup.router.routes}

    assert "/v1/meta/signup/l/{state}" in paths
    assert "/v1/meta/signup/activate" in paths
    assert "/v1/meta/signup/callback" in paths
    assert "/v1/meta/signup/handoff/status" in paths
    assert "/v1/meta/signup/handoff/complete" in paths
    assert "/v1/meta/signup/telemetry" in paths
    assert "/v1/meta/signup/identity/start" in paths
    assert "/v1/meta/signup/identity/callback" in paths
    assert "/v1/meta/signup/identity/select" in paths


def test_signup_launch_page_has_a_branded_onboarding_layout():
    page_template = inspect.getsource(meta_signup._render_standard_signup_page)

    assert "META EMBEDDED SIGNUP" in page_template
    assert "Hubungkan WhatsApp Business" in page_template
    assert "Browser bawaan aplikasi" in page_template


def test_webhook_signature_requires_app_secret():
    body = b'{"object":"whatsapp_business_account"}'
    import hashlib
    import hmac

    digest = hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()

    assert signup.verify_webhook_signature(body, f"sha256={digest}")
    assert not signup.verify_webhook_signature(body, "sha256=bad")
    assert not signup.verify_webhook_signature(body, None)


def test_signup_launch_persists_activation_state_and_resumes_server_handoff():
    page_template = inspect.getsource(meta_signup._render_standard_signup_page)

    assert "sessionStorage.setItem(key" in page_template
    assert "localStorage" not in page_template
    assert "expires_at=Date.now()+ttlMs" in page_template
    assert "function finish()" in page_template


def test_signup_launch_resumes_after_mobile_return_without_false_cancel():
    page_template = inspect.getsource(meta_signup._render_standard_signup_page)

    assert "window.addEventListener('pageshow',()=>finish())" in page_template
    assert "document.addEventListener('visibilitychange'" in page_template
    assert "Login Meta dibatalkan atau belum selesai." not in page_template
    assert "FB.login" in page_template


def test_signup_launch_requires_explicit_pin_activation_after_completion():
    page_template = inspect.getsource(meta_signup._render_standard_signup_page)

    assert "activation_required" in page_template
    assert "cloud_api_new" in page_template
    assert "PIN enam digit" in page_template
    assert "FB.login" in page_template


def test_signup_launch_uses_hosted_flow_and_safe_lifecycle_telemetry():
    page_template = inspect.getsource(meta_signup._render_standard_signup_page)

    assert "featureType:'whatsapp_business_app_onboarding'" in page_template
    assert "sessionInfoVersion:'3'" in page_template
    assert "/v1/meta/signup/telemetry" in page_template
    assert "telemetry('page_loaded')" in page_template
    assert '"meta_signup_state"' in inspect.getsource(meta_signup.launch)
    launch_source = inspect.getsource(meta_signup.launch)
    assert "httponly=True" in launch_source
    assert 'samesite="lax"' in launch_source
    assert 'response.headers["Cache-Control"] = "no-store, max-age=0"' in launch_source
    assert "FB.login(response" in page_template


def test_signup_launch_offers_official_coexistence_and_new_number_paths_on_mobile():
    page_template = inspect.getsource(meta_signup._render_standard_signup_page)

    assert "Pakai WhatsApp Business yang sudah ada" in page_template
    assert "Gunakan nomor baru khusus Cloud API" in page_template
    assert "featureType:'whatsapp_business_app_onboarding'" in page_template
    assert "Buka di Chrome / Safari" in page_template
    assert "launchUrl=`/v1/meta/signup/l/" in page_template
    assert "sdk_load_failed" in page_template


def test_signup_launch_completes_coexistence_with_multiple_phone_numbers():
    page_template = inspect.getsource(meta_signup._render_standard_signup_page)

    assert "if(d.selection_required)" in page_template
    assert "function chooseNumber(candidates)" in page_template
    assert "/v1/meta/signup/handoff/complete" in page_template
    assert "connection_mode:fragments.connection_mode||'cloud_api_new'" in page_template


def test_signup_launch_accepts_all_official_meta_login_origins():
    page_template = inspect.getsource(meta_signup._render_standard_signup_page)

    assert "https://www.facebook.com" in page_template
    assert "https://web.facebook.com" in page_template
    assert "https://business.facebook.com" in page_template


def test_hosted_signup_uses_server_bound_business_identity_not_browser_asset_ids():
    source = inspect.getsource(meta_signup.record_hosted_signup_handoff)
    callback_source = inspect.getsource(meta_signup.identity_callback)

    assert "get_business_token(business_id)" in source
    assert "_hosted_business_key(business_id)" in source
    assert "get_user_businesses(user_token)" in callback_source
    assert "business_id" not in callback_source.split("return HTMLResponse", 1)[1]


def test_oauth_callback_resumes_the_original_signed_launch_in_popup_or_mobile_tab():
    source = inspect.getsource(meta_signup.oauth_callback)

    assert "window.opener.location.replace(next)" in source
    assert "window.location.replace(next)" in source


def test_callback_state_prefers_the_http_only_browser_binding(monkeypatch):
    verified = []
    monkeypatch.setattr(meta_signup, "_agent_for_state", lambda value: verified.append(value))

    assert meta_signup._callback_state(None, "signed-cookie") == "signed-cookie"
    assert verified == ["signed-cookie"]
    assert meta_signup._callback_state("sdk-managed-state", "signed-cookie") == "signed-cookie"
    with pytest.raises(meta_signup.HTTPException, match="missing or expired"):
        meta_signup._callback_state(None, None)


@pytest.mark.asyncio
async def test_signup_telemetry_logs_only_a_non_sensitive_client_event(monkeypatch):
    logged = MagicMock()
    monkeypatch.setattr(meta_signup, "_agent_for_state", lambda _state: __import__("uuid").uuid4())
    monkeypatch.setattr(meta_signup, "logger", SimpleNamespace(info=logged))

    response = await meta_signup.signup_telemetry(
        meta_signup.EmbeddedSignupTelemetryRequest(state="x" * 32, event="sdk_launch_requested", mobile=True)
    )

    assert response.status_code == 204
    logged.assert_called_once_with("meta_signup.client_event", client_event="sdk_launch_requested", mobile=True)


def test_signup_state_default_allows_mobile_registration_to_finish():
    from app.config import Settings

    assert Settings().meta_signup_state_ttl_seconds == 3600


@pytest.mark.asyncio
async def test_signup_completion_keeps_cloud_api_credential_persistence(monkeypatch):
    agent_id = __import__("uuid").uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        wa_phone_number_id=None,
        wa_waba_id=None,
        wa_access_token_encrypted=None,
        wa_display_phone=None,
        wa_business_name=None,
        wa_connection_type=None,
        channel_type=None,
        version=1,
    )
    selected = {"id": "phone-id", "display_phone_number": "+62 812", "verified_name": "Arthur"}
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[
        MagicMock(scalar_one_or_none=lambda: agent),
        MagicMock(scalar_one_or_none=lambda: None),
    ])
    db.commit = AsyncMock()
    monkeypatch.setattr(meta_signup, "_agent_for_state", lambda _state: agent_id)
    monkeypatch.setattr(meta_signup, "exchange_code_for_token", AsyncMock(return_value="short-lived-code-exchange"))
    monkeypatch.setattr(meta_signup, "get_waba_phone_numbers", AsyncMock(return_value=[selected]))
    monkeypatch.setattr(meta_signup, "subscribe_waba_to_webhooks", AsyncMock())
    monkeypatch.setattr(meta_signup, "encrypt_value", lambda _value: "enc:stored-credential")

    response = await meta_signup.complete(
        meta_signup.EmbeddedSignupCompleteRequest(
            state="x" * 32, code="code", waba_id="waba-id", phone_number_id="phone-id"
        ),
        db,
    )

    assert response["ok"] is True
    assert response["activation_required"] is True
    assert agent.wa_connection_type == "cloud_api"
    assert agent.channel_type == "whatsapp"
    assert agent.wa_phone_number_id == "phone-id"
    assert agent.wa_waba_id == "waba-id"
    assert agent.wa_access_token_encrypted == "enc:stored-credential"
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_signup_activation_uses_signed_state_and_never_persists_pin(monkeypatch):
    agent_id = __import__("uuid").uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        wa_connection_type="cloud_api",
        wa_phone_number_id="phone-id",
        wa_waba_id="waba-id",
        wa_access_token_encrypted="enc:stored-credential",
    )
    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=lambda: agent))
    register = AsyncMock()
    monkeypatch.setattr(meta_signup, "_agent_for_state", lambda _state: agent_id)
    monkeypatch.setattr(meta_signup, "decrypt_value", lambda _value: "decrypted-token")
    monkeypatch.setattr("app.core.infra.wa_cloud_client.register_phone_number", register)

    response = await meta_signup.activate_phone_number(
        meta_signup.EmbeddedSignupActivationRequest(state="x" * 32, pin="123456"), db
    )

    assert response == {"ok": True, "registration": "requested"}
    register.assert_awaited_once_with("phone-id", "decrypted-token", "123456")
    assert not hasattr(agent, "pin")


@pytest.mark.asyncio
async def test_coexistence_completion_skips_pin_registration_and_persists_cloud_credentials(monkeypatch):
    agent_id = __import__("uuid").uuid4()
    agent = SimpleNamespace(
        id=agent_id, wa_phone_number_id=None, wa_waba_id=None,
        wa_access_token_encrypted=None, wa_display_phone=None,
        wa_business_name=None, wa_connection_type=None, channel_type=None, version=1,
    )
    selected = {
        "id": "phone-id", "display_phone_number": "+62 812", "verified_name": "Arthur",
        "is_on_biz_app": True, "platform_type": "CLOUD_API",
    }
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[MagicMock(scalar_one_or_none=lambda: agent), MagicMock(scalar_one_or_none=lambda: None)])
    db.commit = AsyncMock()
    monkeypatch.setattr(meta_signup, "_agent_for_state", lambda _state: agent_id)
    monkeypatch.setattr(meta_signup, "exchange_code_for_token", AsyncMock(return_value="provider-token"))
    monkeypatch.setattr(meta_signup, "get_waba_phone_numbers", AsyncMock(return_value=[selected]))
    monkeypatch.setattr(meta_signup, "subscribe_waba_to_webhooks", AsyncMock())
    monkeypatch.setattr(meta_signup, "encrypt_value", lambda _value: "enc:stored-credential")

    response = await meta_signup.complete(
        meta_signup.EmbeddedSignupCompleteRequest(
            state="x" * 32, code="code", waba_id="waba-id", connection_mode="coexistence"
        ), db,
    )

    assert response["activation_required"] is False
    assert agent.wa_connection_type == "cloud_api"
    assert agent.wa_cloud_api_mode == "coexistence"
    assert agent.wa_phone_number_id == "phone-id"


@pytest.mark.asyncio
async def test_coexistence_activation_is_rejected_without_calling_meta(monkeypatch):
    agent_id = __import__("uuid").uuid4()
    agent = SimpleNamespace(
        id=agent_id, wa_connection_type="cloud_api", wa_cloud_api_mode="coexistence",
        wa_phone_number_id="phone-id", wa_waba_id="waba-id", wa_access_token_encrypted="enc:credential",
    )
    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=lambda: agent))
    monkeypatch.setattr(meta_signup, "_agent_for_state", lambda _state: agent_id)
    with pytest.raises(meta_signup.HTTPException, match="no PIN activation"):
        await meta_signup.activate_phone_number(
            meta_signup.EmbeddedSignupActivationRequest(state="x" * 32, pin="123456"), db
        )
