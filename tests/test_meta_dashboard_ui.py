from pathlib import Path


def test_dashboard_exposes_scoped_meta_embedded_signup_action():
    script = Path("UI-DEV/app.js").read_text()
    index = Path("UI-DEV/index.html").read_text()

    assert "window.location.origin" in script
    assert "isStaleLocalDefault" in script
    assert "isStaleProductionBaseUrl" in script
    assert "localStorage.setItem('baseUrl', window.location.origin)" in script
    assert "Nomor baru · Cloud API" in script
    assert "Nomor existing · Coexistence" in script
    assert "connectMetaEmbeddedSignup" in script
    assert "mode = 'cloud_api_new'" in script
    assert "mode === 'coexistence'" in script
    assert "/v1/meta/signup/links/${agentId}" in script
    assert "arthurConnectMeta" in script
    assert "wa-service legacy di port 8080 tidak aktif" in script
    assert "dashboardWasUnavailable" in script
    assert "cache: 'no-store'" in script
    assert "Isi API Key di bagian atas" in script
    assert "arthurConnectMeta('cloud_api_new')" in index
    assert "arthurConnectMeta('coexistence')" in index
    assert "Webhook AI Agent n8n" in script
    assert "AI Agent n8n" in script
    assert "/whatsapp/routing" in script
    assert "nav('agent-n8n')" in index
    assert 'id="sec-agent-n8n"' in index
    assert "Buat Agent n8n" in index
    assert "connectN8nCoexistence" in script
    assert "loadN8nWebhookDraft" in script
    assert "saveN8nWebhookDraft" in script
    assert "Webhook tersimpan sebagai draft" in script
    assert "business.facebook.com/business-support-home" in script
    assert "created_by_type: 'dashboard_n8n'" in script
    assert 'app.js?v=n8n-agent-draft-webhook-1' in index

    whatsapp_loader = script.split("async function loadWAAgent()", 1)[1].split(
        "async function connectMetaEmbeddedSignup", 1
    )[0]
    assert "n8n-routing-panel" not in whatsapp_loader


def test_dashboard_loads_all_agent_pages_before_finding_arthur():
    script = Path("UI-DEV/app.js").read_text()

    assert "async function listAllAgents()" in script
    assert "offset=${offset}" in script
    assert "const r = await listAllAgents();" in script
    assert "a.tools_config?.system_plugin === 'arthur_v2' && !a.is_deleted" in script
    assert "✅ Aktif" in script
    assert "/whatsapp/status" in script
