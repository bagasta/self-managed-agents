from pathlib import Path


def test_dashboard_exposes_scoped_meta_embedded_signup_action():
    script = Path("UI-DEV/app.js").read_text()

    assert "window.location.origin" in script
    assert "isStaleLocalDefault" in script
    assert "Connect via Meta Embedded Signup" in script
    assert "connectMetaEmbeddedSignup" in script
    assert "/v1/meta/signup/links/${agentId}" in script
    assert "arthurConnectMeta" in script
    assert "wa-service legacy di port 8080 tidak aktif" in script
    assert "Isi API Key di bagian atas" in script
