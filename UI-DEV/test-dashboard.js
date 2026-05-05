const STORAGE = {
  baseUrl: 'managedAgents.test.baseUrl',
  apiKey: 'managedAgents.test.apiKey',
  waUrl: 'managedAgents.test.waUrl',
};

const ENDPOINTS = [
  'GET /health',
  'GET WA /health',
  'GET /v1/agents?limit=100',
  'GET /v1/agents/{agentId}',
  'GET /v1/sessions/{sessionId}/history?limit=50',
  'POST /v1/agents/{agentId}/sessions/{sessionId}/messages',
  'POST /v1/channels/incoming/{sessionId}',
  'GET /v1/agents/{agentId}/documents?limit=50',
  'GET /v1/agents/{agentId}/memory',
  'GET /v1/agents/{agentId}/skills',
  'GET /v1/agents/{agentId}/custom-tools',
  'GET /v1/agents/{agentId}/whatsapp/status',
  'GET /v1/agents/{agentId}/whatsapp/qr',
  'POST /v1/channels/wa/incoming',
  'GET /v1/runs/{runId}',
];

function $(id) { return document.getElementById(id); }

function getCfg() {
  return {
    baseUrl: $('base-url').value.replace(/\/$/, ''),
    apiKey: $('api-key').value.trim(),
    waUrl: $('wa-url').value.replace(/\/$/, ''),
  };
}

function saveCfg() {
  const cfg = getCfg();
  localStorage.setItem(STORAGE.baseUrl, cfg.baseUrl);
  localStorage.setItem(STORAGE.apiKey, cfg.apiKey);
  localStorage.setItem(STORAGE.waUrl, cfg.waUrl);
  setMeta('Config saved ke localStorage.');
}

function loadCfg() {
  $('base-url').value = localStorage.getItem(STORAGE.baseUrl) || 'http://localhost:8000';
  $('api-key').value = localStorage.getItem(STORAGE.apiKey) || '';
  $('wa-url').value = localStorage.getItem(STORAGE.waUrl) || 'http://localhost:8080';
}

function readIds() {
  return {
    agentId: $('agent-id').value.trim(),
    sessionId: $('session-id').value.trim(),
    runId: $('run-id').value.trim(),
    deviceId: $('device-id').value.trim(),
    message: $('message-text').value || 'Halo dari test dashboard',
    fromPhone: $('from-phone').value.trim() || '+620000000000',
  };
}

function setStatus(kind, text) {
  const el = $('last-status');
  el.className = `status ${kind}`;
  el.textContent = text;
}

function setMeta(text) {
  $('result-meta').textContent = text;
}

function setResult(obj) {
  $('result-output').textContent = typeof obj === 'string' ? obj : JSON.stringify(obj, null, 2);
}

async function http({ base = 'main', method = 'GET', path = '/', body = null, headers = {} }) {
  const cfg = getCfg();
  const root = base === 'wa' ? cfg.waUrl : cfg.baseUrl;
  const url = `${root}${path}`;
  const finalHeaders = { ...headers };
  if (base === 'main' && cfg.apiKey) finalHeaders['X-API-Key'] = cfg.apiKey;
  if (body && !finalHeaders['Content-Type']) finalHeaders['Content-Type'] = 'application/json';

  setMeta(`${method} ${url}`);
  setStatus('warn', 'loading');

  try {
    const res = await fetch(url, {
      method,
      headers: finalHeaders,
      body: body ? JSON.stringify(body) : undefined,
    });
    const text = await res.text();
    let data;
    try { data = JSON.parse(text); } catch { data = text; }
    setStatus(res.ok ? 'ok' : 'err', `${res.status} ${res.ok ? 'ok' : 'error'}`);
    setResult(data);
    return { ok: res.ok, status: res.status, data, url };
  } catch (err) {
    setStatus('err', 'network error');
    setResult({ error: err.message });
    return { ok: false, status: 0, data: { error: err.message }, url };
  }
}

function requireField(value, name) {
  if (!value) throw new Error(`${name} wajib diisi dulu.`);
}

async function runAction(action) {
  const ids = readIds();
  switch (action) {
    case 'health-main':
      return http({ path: '/health' });
    case 'health-wa':
      return http({ base: 'wa', path: '/health' });
    case 'agents-list':
      return http({ path: '/v1/agents?limit=100' });
    case 'run-detail':
      requireField(ids.runId, 'Run ID');
      return http({ path: `/v1/runs/${encodeURIComponent(ids.runId)}` });
    case 'agent-detail':
      requireField(ids.agentId, 'Agent ID');
      return http({ path: `/v1/agents/${encodeURIComponent(ids.agentId)}` });
    case 'session-history':
      requireField(ids.sessionId, 'Session ID');
      return http({ path: `/v1/sessions/${encodeURIComponent(ids.sessionId)}/history?limit=50` });
    case 'incoming-session':
      requireField(ids.sessionId, 'Session ID');
      return http({ method: 'POST', path: `/v1/channels/incoming/${encodeURIComponent(ids.sessionId)}`, body: { message: ids.message } });
    case 'agent-message':
      requireField(ids.agentId, 'Agent ID');
      requireField(ids.sessionId, 'Session ID');
      return http({ method: 'POST', path: `/v1/agents/${encodeURIComponent(ids.agentId)}/sessions/${encodeURIComponent(ids.sessionId)}/messages`, body: { message: ids.message } });
    case 'agent-docs':
      requireField(ids.agentId, 'Agent ID');
      return http({ path: `/v1/agents/${encodeURIComponent(ids.agentId)}/documents?limit=50` });
    case 'agent-memory':
      requireField(ids.agentId, 'Agent ID');
      return http({ path: `/v1/agents/${encodeURIComponent(ids.agentId)}/memory` });
    case 'agent-skills':
      requireField(ids.agentId, 'Agent ID');
      return http({ path: `/v1/agents/${encodeURIComponent(ids.agentId)}/skills` });
    case 'agent-custom-tools':
      requireField(ids.agentId, 'Agent ID');
      return http({ path: `/v1/agents/${encodeURIComponent(ids.agentId)}/custom-tools` });
    case 'wa-status':
      requireField(ids.agentId, 'Agent ID');
      return http({ path: `/v1/agents/${encodeURIComponent(ids.agentId)}/whatsapp/status` });
    case 'wa-qr':
      requireField(ids.agentId, 'Agent ID');
      return http({ path: `/v1/agents/${encodeURIComponent(ids.agentId)}/whatsapp/qr` });
    case 'wa-incoming':
      requireField(ids.deviceId, 'Device ID');
      return http({
        method: 'POST',
        path: '/v1/channels/wa/incoming',
        body: {
          device_id: ids.deviceId,
          from: ids.fromPhone,
          message: ids.message,
          timestamp: Math.floor(Date.now() / 1000),
        },
      });
    default:
      throw new Error(`Unknown action: ${action}`);
  }
}

async function runSmokeTest() {
  const checks = [
    { label: 'main /health', fn: () => http({ path: '/health' }) },
    { label: 'wa /health', fn: () => http({ base: 'wa', path: '/health' }) },
    { label: 'agents list', fn: () => http({ path: '/v1/agents?limit=100' }) },
  ];
  const results = [];
  for (const check of checks) {
    const out = await check.fn();
    results.push({ label: check.label, ok: out.ok, status: out.status, url: out.url });
  }
  setMeta('Smoke test selesai.');
  setStatus(results.every(r => r.ok) ? 'ok' : 'warn', 'smoke done');
  setResult(results);
}

async function sendCustom() {
  const method = $('custom-method').value;
  const path = $('custom-path').value.trim();
  if (!path) throw new Error('Custom path wajib diisi.');
  const rawBody = $('custom-body').value.trim();
  let body = null;
  if (rawBody) body = JSON.parse(rawBody);
  return http({ method, path, body });
}

function initCatalog() {
  $('endpoint-catalog').innerHTML = ENDPOINTS.map(item => `<div class="catalog-item"><code>${item}</code></div>`).join('');
}

function bind() {
  $('save-config-btn').addEventListener('click', saveCfg);
  $('run-smoke-btn').addEventListener('click', () => runSmokeTest().catch(handleError));
  $('custom-send-btn').addEventListener('click', () => sendCustom().catch(handleError));
  document.querySelectorAll('[data-action]').forEach(btn => {
    btn.addEventListener('click', async () => {
      try {
        await runAction(btn.dataset.action);
      } catch (err) {
        handleError(err);
      }
    });
  });
}

function handleError(err) {
  setStatus('err', 'input error');
  setMeta('Request dibatalkan karena input belum lengkap / invalid.');
  setResult({ error: err.message });
}

window.addEventListener('DOMContentLoaded', () => {
  loadCfg();
  initCatalog();
  bind();
});
