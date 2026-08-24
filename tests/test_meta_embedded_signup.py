from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from types import SimpleNamespace

import pytest

from app.core.infra import meta_embedded_signup as signup
from app.api import meta_signup


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


def test_webhook_signature_requires_app_secret():
    body = b'{"object":"whatsapp_business_account"}'
    import hashlib
    import hmac

    digest = hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()

    assert signup.verify_webhook_signature(body, f"sha256={digest}")
    assert not signup.verify_webhook_signature(body, "sha256=bad")
    assert not signup.verify_webhook_signature(body, None)
