from pathlib import Path


def test_dashboard_exposes_scoped_meta_embedded_signup_action():
    script = Path("UI-DEV/app.js").read_text()

    assert "window.location.origin" in script
    assert "Connect via Meta Embedded Signup" in script
    assert "connectMetaEmbeddedSignup" in script
    assert "/v1/meta/signup/links/${agentId}" in script
