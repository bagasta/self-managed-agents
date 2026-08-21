from __future__ import annotations

from types import SimpleNamespace

import pytest

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

    assert str(signup.verify_signup_state(state)) == "2b149a50-9f54-4c26-b9d6-e82d5d699859"
    with pytest.raises(ValueError, match="Invalid or expired"):
        signup.verify_signup_state(state + "x")


def test_webhook_signature_requires_app_secret():
    body = b'{"object":"whatsapp_business_account"}'
    import hashlib
    import hmac

    digest = hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()

    assert signup.verify_webhook_signature(body, f"sha256={digest}")
    assert not signup.verify_webhook_signature(body, "sha256=bad")
    assert not signup.verify_webhook_signature(body, None)
