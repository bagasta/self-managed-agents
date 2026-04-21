/* ═══════════════════════════════════════════════════════════════════
   Managed Agents — Dev UI  |  app.js
   ═══════════════════════════════════════════════════════════════════ */

// ── State ──────────────────────────────────────────────────────────
const S = {
  baseUrl: localStorage.getItem('baseUrl') || 'http://localhost:8000',
  apiKey: localStorage.getItem('apiKey') || '',
  waServiceUrl: localStorage.getItem('waServiceUrl') || 'http://localhost:8080',
  agents: [],
  logCollapsed: false,
  sseConnection: null,
  sseSessionId: null,
  // WhatsApp state
  waCurrentAgentId: null,
  waCurrentDeviceId: null,
  waStatusPoller: null, // setInterval for status polling
  waQRPoller: null, // setInterval for QR refresh
  waModalDeviceId: null,
  waModalAgentId: null,
};

// ── Init ───────────────────────────────────────────────────────────
window.addEventListener('DOMContentLoaded', () => {
  document.getElementById('cfg-url').value = S.baseUrl;
  document.getElementById('cfg-key').value = S.apiKey;
  setAgentFormDefaults();
  loadAgents().then(() => pingWAService());
  nav('agents');
});

// ── Config ─────────────────────────────────────────────────────────
function saveConfig() {
  S.baseUrl = document.getElementById('cfg-url').value.replace(/\/$/, '');
  S.apiKey = document.getElementById('cfg-key').value;
  localStorage.setItem('baseUrl', S.baseUrl);
  localStorage.setItem('apiKey', S.apiKey);
  pingServer();
  pingWAService();
}

async function pingServer() {
  const dot = document.getElementById('status-dot');
  dot.className = '';
  try {
    const r = await fetch(`${S.baseUrl}/health`);
    dot.className = r.ok ? 'ok' : 'err';
  } catch { dot.className = 'err'; }
}

async function pingWAService() {
  const dot = document.getElementById('wa-service-dot');
  if (!dot) return;
  dot.style.background = 'var(--text-muted)';
  try {
    const r = await fetch(`${S.waServiceUrl}/health`, { signal: AbortSignal.timeout(3000) });
    dot.style.background = r.ok ? 'var(--success)' : 'var(--error)';
  } catch { dot.style.background = 'var(--error)'; }
}

// ── Navigation ─────────────────────────────────────────────────────
function nav(id) {
  document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.getElementById(`sec-${id}`).classList.add('active');
  document.querySelectorAll('.nav-item').forEach(item => {
    if (item.getAttribute('onclick') === `nav('${id}')`) item.classList.add('active');
  });
  if (['sessions', 'chat', 'documents', 'memory', 'skills', 'tools', 'escalation'].includes(id)) {
    populateAgentSelects();
  }
  if (id === 'whatsapp') { populateWAAgentSelect(); }
  if (id !== 'chat') disconnectSSE();
  // Stop WA polling when leaving whatsapp section
  if (id !== 'whatsapp') stopWAPolling();
}

// ── SSE ────────────────────────────────────────────────────────────
function connectSSE(sessionId) {
  if (S.sseConnection && S.sseSessionId === sessionId) return;
  disconnectSSE();
  if (!sessionId || !S.apiKey) return;
  const url = `${S.baseUrl}/v1/sessions/${sessionId}/stream?api_key=${encodeURIComponent(S.apiKey)}`;
  const es = new EventSource(url);
  S.sseConnection = es;
  S.sseSessionId = sessionId;
  _setSseIndicator('connecting');
  es.addEventListener('open', () => _setSseIndicator('connected'));
  es.addEventListener('message', (e) => {
    try {
      const data = JSON.parse(e.data);
      if (data.type === 'scheduled_message') appendScheduledBubble(data.reply, data.label);
    } catch { }
  });
  es.addEventListener('ping', () => { });
  es.addEventListener('error', () => _setSseIndicator('disconnected'));
}

function disconnectSSE() {
  if (S.sseConnection) { S.sseConnection.close(); S.sseConnection = null; S.sseSessionId = null; }
  _setSseIndicator('off');
}

function _setSseIndicator(state) {
  const el = document.getElementById('sse-indicator');
  if (!el) return;
  const map = {
    off: { dot: '⚪', text: 'Stream off', cls: '' },
    connecting: { dot: '🟡', text: 'Connecting...', cls: 'sse-connecting' },
    connected: { dot: '🟢', text: 'Live', cls: 'sse-connected' },
    disconnected: { dot: '🔴', text: 'Disconnected', cls: 'sse-error' },
  };
  const m = map[state] || map.off;
  el.innerHTML = `<span class="${m.cls}">${m.dot} ${m.text}</span>`;
}

function appendScheduledBubble(content, label) {
  const chatEl = document.getElementById('chat-messages');
  if (!chatEl) return;
  const div = document.createElement('div');
  div.className = 'chat-bubble bubble-agent bubble-scheduled';
  div.innerHTML = `<div class="bubble-label">⏰ Reminder${label ? ` · ${escHtml(label)}` : ''}</div>${escHtml(content || '')}`;
  chatEl.appendChild(div);
  chatEl.scrollTop = chatEl.scrollHeight;
}

// ── API Helpers ────────────────────────────────────────────────────
async function api(method, path, body = null, isFormData = false) {
  const url = `${S.baseUrl}${path}`;
  const headers = { 'X-API-Key': S.apiKey };
  if (body && !isFormData) headers['Content-Type'] = 'application/json';
  const opts = { method, headers };
  if (body) opts.body = isFormData ? body : JSON.stringify(body);
  logRequest(method, url, body);
  let data, status;
  try {
    const res = await fetch(url, opts);
    status = res.status;
    const text = await res.text();
    try { data = JSON.parse(text); } catch { data = text; }
    logResponse(status, data, method, url);
    return { ok: res.ok, status, data };
  } catch (err) {
    logResponse(0, { error: err.message }, method, url);
    return { ok: false, status: 0, data: { error: err.message } };
  }
}

// For message endpoints that need X-Agent-Key instead of X-API-Key
async function apiMsg(agentId, path, body) {
  const agent = S.agents.find(a => a.id === agentId);
  const agentKey = agent?.api_key || '';
  const url = `${S.baseUrl}${path}`;
  const headers = { 'X-Agent-Key': agentKey, 'Content-Type': 'application/json' };
  logRequest('POST', url, body);
  let data, status;
  try {
    const res = await fetch(url, { method: 'POST', headers, body: JSON.stringify(body) });
    status = res.status;
    const text = await res.text();
    try { data = JSON.parse(text); } catch { data = text; }
    logResponse(status, data, 'POST', url);
    return { ok: res.ok, status, data };
  } catch (err) {
    logResponse(0, { error: err.message }, 'POST', url);
    return { ok: false, status: 0, data: { error: err.message } };
  }
}

// Direct call to wa-service (Go) - no auth needed
async function waApi(method, path, body = null) {
  const url = `${S.waServiceUrl}${path}`;
  const headers = {};
  if (body) headers['Content-Type'] = 'application/json';
  const opts = { method, headers };
  if (body) opts.body = JSON.stringify(body);
  try {
    const res = await fetch(url, opts);
    const text = await res.text();
    let data;
    try { data = JSON.parse(text); } catch { data = text; }
    return { ok: res.ok, status: res.status, data };
  } catch (err) {
    return { ok: false, status: 0, data: { error: err.message } };
  }
}

function parseJson(str, fallback = {}) {
  try { return JSON.parse(str || '{}'); } catch { return fallback; }
}

// ── Log Panel ──────────────────────────────────────────────────────
function logRequest(method, url, body) {
  document.getElementById('log-method').textContent = method;
  document.getElementById('log-method').className = `badge ${methodColor(method)}`;
  document.getElementById('log-url').textContent = url;
  document.getElementById('log-status-badge').textContent = '...';
  document.getElementById('log-body').innerHTML =
    (body ? '<span style="color:var(--text-muted)">→ REQUEST:\n</span>' + syntaxHighlight(body) + '\n\n' : '') +
    '<span class="spinner"></span> Waiting...';
  if (S.logCollapsed) { S.logCollapsed = false; toggleLog(); }
}

function logResponse(status, data, method, url) {
  const badge = document.getElementById('log-status-badge');
  badge.textContent = status ? `${status}` : 'ERR';
  badge.style.color = status >= 200 && status < 300 ? 'var(--success)' :
    status >= 400 ? 'var(--error)' : 'var(--warning)';
  document.getElementById('log-body').innerHTML =
    '<span style="color:var(--text-muted)">← RESPONSE ' + status + ':\n</span>' +
    syntaxHighlight(data);
}

function syntaxHighlight(obj) {
  if (typeof obj === 'string') return escHtml(obj);
  const json = JSON.stringify(obj, null, 2);
  return json.replace(/("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false|null)\b|-?\d+\.?\d*)/g, (m) => {
    let cls = 'log-num';
    if (/^"/.test(m)) cls = /:$/.test(m) ? 'log-key' : 'log-str';
    else if (/true|false/.test(m)) cls = 'log-bool';
    else if (/null/.test(m)) cls = 'log-null';
    return `<span class="${cls}">${escHtml(m)}</span>`;
  });
}

function escHtml(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function methodColor(m) {
  return { GET: 'badge-blue', POST: 'badge-green', PATCH: 'badge-yellow', DELETE: 'badge-red' }[m] || 'badge-blue';
}

function toggleLog() {
  S.logCollapsed = !S.logCollapsed;
  document.getElementById('log-body').style.display = S.logCollapsed ? 'none' : '';
  document.getElementById('log-toggle-icon').textContent = S.logCollapsed ? '▲' : '▼';
}

// ═══════════════════════════════════════════════════════════════════
//  AGENTS
// ═══════════════════════════════════════════════════════════════════
function setAgentFormDefaults() {
  document.getElementById('a-tools').value = JSON.stringify({
    memory: true, tool_creator: true, rag: true,
    sandbox: true, skills: true, scheduler: true, escalation: true,
    http: false, mcp: false
  }, null, 2);
  document.getElementById('a-escalation').value = JSON.stringify({
    channel_type: "whatsapp",
    operator_phone: "+62811000000"
  }, null, 2);
}

function onAgentChannelTypeChange() {
  const type = document.getElementById('a-channel-type').value;
  // No extra UI changes needed — just stored in payload
}

async function loadAgents() {
  const r = await api('GET', '/v1/agents?limit=100');
  if (!r.ok) { renderAgentsTable([]); return; }
  S.agents = r.data.items || [];
  renderAgentsTable(S.agents);
  populateAgentSelects();
}

function renderAgentsTable(agents) {
  const el = document.getElementById('agents-table');
  if (!agents.length) {
    el.innerHTML = '<div class="empty-state"><div class="icon">🤖</div>Belum ada agent</div>';
    return;
  }
  el.innerHTML = `
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th>Name</th><th>Model</th><th>WhatsApp</th><th>Quota</th><th>Created</th><th>Actions</th>
        </tr></thead>
        <tbody>
          ${agents.map(a => `
            <tr>
              <td>
                <strong>${escHtml(a.name)}</strong>
                ${a.description ? `<br><span class="text-muted">${escHtml(a.description)}</span>` : ''}
              </td>
              <td class="td-mono">${escHtml(a.model)}</td>
              <td>
                ${a.channel_type === 'whatsapp'
      ? `<button class="btn btn-ghost btn-sm wa-btn" onclick="goToWAAgent('${a.id}')">📱 Kelola WA</button>`
      : '<span class="text-muted">—</span>'}
              </td>
              <td class="td-mono" style="font-size:10px">
                ${(a.tokens_used || 0).toLocaleString()} / ${(a.token_quota || 0).toLocaleString()}<br>
                <span class="text-muted">exp: ${a.active_until?.slice(0, 10) || '—'}</span>
              </td>
              <td class="td-mono">${a.created_at?.slice(0, 10)}</td>
              <td>
                <div class="td-actions">
                  <button class="btn btn-ghost btn-sm" onclick='editAgent(${JSON.stringify(a)})'>✏️ Edit</button>
                  <button class="btn btn-danger btn-sm" onclick="deleteAgent('${a.id}')">🗑</button>
                </div>
              </td>
            </tr>`).join('')}
        </tbody>
      </table>
    </div>`;
}

function goToWAAgent(agentId) {
  nav('whatsapp');
  // Pre-select this agent after DOM updates
  setTimeout(() => {
    const sel = document.getElementById('wa-agent-sel');
    if (sel) { sel.value = agentId; loadWAAgent(); }
  }, 100);
}

function editAgent(a) {
  document.getElementById('agent-form-title').textContent = '✏️ Edit Agent';
  document.getElementById('agent-edit-id').value = a.id;
  document.getElementById('a-name').value = a.name;
  document.getElementById('a-desc').value = a.description || '';
  document.getElementById('a-instructions').value = a.instructions || '';
  document.getElementById('a-model').value = a.model;
  document.getElementById('a-temp').value = a.temperature;
  document.getElementById('a-tools').value = JSON.stringify(a.tools_config, null, 2);
  document.getElementById('a-escalation').value = JSON.stringify(a.escalation_config || {}, null, 2);
  document.getElementById('a-safety').value = JSON.stringify(a.safety_policy || {}, null, 2);
  document.getElementById('a-channel-type').value = a.channel_type || '';
  document.getElementById('a-name').scrollIntoView({ behavior: 'smooth' });
}

function resetAgentForm() {
  document.getElementById('agent-form-title').textContent = '➕ Create Agent';
  document.getElementById('agent-edit-id').value = '';
  ['a-name', 'a-desc', 'a-instructions'].forEach(id => document.getElementById(id).value = '');
  document.getElementById('a-model').value = 'anthropic/claude-sonnet-4-6';
  document.getElementById('a-temp').value = '0.7';
  document.getElementById('a-channel-type').value = '';
  setAgentFormDefaults();
}

async function submitAgent() {
  const editId = document.getElementById('agent-edit-id').value;
  const channelType = document.getElementById('a-channel-type').value || null;
  const payload = {
    name: document.getElementById('a-name').value.trim(),
    description: document.getElementById('a-desc').value.trim() || null,
    instructions: document.getElementById('a-instructions').value.trim(),
    model: document.getElementById('a-model').value.trim(),
    temperature: parseFloat(document.getElementById('a-temp').value) || 0.7,
    tools_config: parseJson(document.getElementById('a-tools').value),
    escalation_config: parseJson(document.getElementById('a-escalation').value),
    safety_policy: parseJson(document.getElementById('a-safety').value),
  };
  if (!editId && channelType) payload.channel_type = channelType;
  if (!payload.name) return alert('Name wajib diisi');

  // Check wa-service health before creating WA agent
  if (!editId && channelType === 'whatsapp') {
    const waHealth = await waApi('GET', '/health');
    if (!waHealth.ok) {
      const proceed = confirm(
        '⚠️ wa-service tidak aktif (port 8080).\n\n' +
        'Agent tetap bisa dibuat, tapi QR code tidak akan muncul sekarang.\n' +
        'Kamu bisa scan QR nanti setelah wa-service dijalankan.\n\n' +
        'Lanjutkan membuat agent?'
      );
      if (!proceed) return;
    }
  }

  const saveBtn = document.querySelector('#sec-agents .btn-success');
  const origText = saveBtn.textContent;
  saveBtn.disabled = true;
  saveBtn.innerHTML = '<span class="spinner"></span> Menyimpan...';

  const r = editId
    ? await api('PATCH', `/v1/agents/${editId}`, payload)
    : await api('POST', '/v1/agents', payload);

  saveBtn.disabled = false;
  saveBtn.textContent = origText;

  if (r.ok) {
    resetAgentForm();
    await loadAgents();
    // Show QR modal if new WA agent with QR image
    if (!editId && channelType === 'whatsapp') {
      if (r.data.qr_image) {
        openQRModal(r.data.id, r.data.wa_device_id, r.data.qr_image);
      } else if (r.data.wa_device_id) {
        // Agent created but QR not available yet — navigate to WA section
        alert('✅ Agent dibuat! Buka tab WhatsApp untuk scan QR code.');
        setTimeout(() => {
          nav('whatsapp');
          populateWAAgentSelect();
          const sel = document.getElementById('wa-agent-sel');
          sel.value = r.data.id;
          loadWAAgent();
        }, 200);
      }
    }
  } else {
    alert(`❌ Gagal menyimpan agent: ${r.data?.detail || JSON.stringify(r.data)}`);
  }
}

async function deleteAgent(id) {
  if (!confirm('Hapus agent ini?')) return;
  const r = await api('DELETE', `/v1/agents/${id}`);
  if (r.ok || r.status === 204) loadAgents();
}

// ═══════════════════════════════════════════════════════════════════
//  SESSIONS
// ═══════════════════════════════════════════════════════════════════
function populateAgentSelects() {
  const selects = ['sess-agent-sel', 'chat-agent-sel', 'doc-agent-sel',
    'mem-agent-sel', 'skill-agent-sel', 'ct-agent-sel', 'esc-agent-sel'];
  selects.forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    const val = el.value;
    el.innerHTML = '<option value="">— pilih agent —</option>' +
      S.agents.map(a => `<option value="${a.id}">${escHtml(a.name)}</option>`).join('');
    el.value = val;
  });
}

async function loadSessions() {
  document.getElementById('sessions-table').innerHTML =
    '<div class="text-muted" style="padding:8px">Buat session baru di bawah. Session ID akan muncul di log.</div>';
}

function updateChannelConfigExample() {
  const type = document.getElementById('s-channel-type').value;
  const examples = {
    whatsapp: { user_phone: '+62812xxx', device_id: 'DEVICE_UUID_FROM_AGENT' },
    telegram: { chat_id: '123456789', bot_token: 'BOT_TOKEN' },
    slack: { webhook_url: 'https://hooks.slack.com/services/xxx' },
    webhook: { url: 'https://your-server.com/webhook', headers: {} },
    'in-app': {},
  };
  document.getElementById('s-channel-config').value = JSON.stringify(examples[type] || {}, null, 2);
}

async function createSession() {
  const agentId = document.getElementById('sess-agent-sel').value;
  if (!agentId) return alert('Pilih agent dulu');
  const channelType = document.getElementById('s-channel-type').value;
  const payload = {
    external_user_id: document.getElementById('s-userid').value.trim() || null,
    metadata: parseJson(document.getElementById('s-meta').value),
    channel_type: channelType || null,
    channel_config: channelType ? parseJson(document.getElementById('s-channel-config').value) : {},
  };
  const r = await api('POST', `/v1/agents/${agentId}/sessions`, payload);
  if (r.ok) {
    const sid = r.data.id;
    alert(`Session created!\nID: ${sid}`);
    const sel = document.getElementById('chat-session-sel');
    const opt = document.createElement('option');
    opt.value = sid; opt.textContent = sid.slice(0, 8) + '...';
    sel.appendChild(opt); sel.value = sid;
    loadChatHistory();
  }
}

// ═══════════════════════════════════════════════════════════════════
//  CHAT
// ═══════════════════════════════════════════════════════════════════
async function loadSessionsForChat() {
  const agentId = document.getElementById('chat-agent-sel').value;
  const sel = document.getElementById('chat-session-sel');
  sel.innerHTML = '<option value="">Pilih session...</option><option value="__manual__">— ketik manual —</option>';
  const badge = document.getElementById('chat-agent-key-badge');
  if (agentId) {
    const agent = S.agents.find(a => a.id === agentId);
    if (agent) {
      badge.innerHTML = `🔑 Agent Key: <code>${escHtml(agent.api_key?.slice(0, 12))}...</code>`;
    }
  } else { badge.innerHTML = ''; }
}

async function loadChatHistory() {
  const sessionId = document.getElementById('chat-session-sel').value;
  if (!sessionId || sessionId === '__manual__') { showChatManualInput(); return; }
  const agentId = document.getElementById('chat-agent-sel').value;
  if (!agentId || !sessionId) return;
  const r = await api('GET', `/v1/sessions/${sessionId}/history?limit=50`);
  if (!r.ok) return;
  const msgs = r.data.messages || [];
  const chatEl = document.getElementById('chat-messages');
  chatEl.innerHTML = '';
  if (!msgs.length) {
    chatEl.innerHTML = '<div class="bubble-system">Session baru — belum ada pesan</div>';
  } else {
    msgs.forEach(m => {
      if (m.role === 'user' && (m.content || '').startsWith('[SCHEDULED]')) return;
      appendChatBubble(m.role, m.content, m.tool_name);
    });
  }
  chatEl.scrollTop = chatEl.scrollHeight;
  connectSSE(sessionId);
}

function showChatManualInput() {
  const sel = document.getElementById('chat-session-sel');
  const input = prompt('Masukkan Session ID:');
  if (input) {
    const opt = document.createElement('option');
    opt.value = input; opt.textContent = input.slice(0, 8) + '...';
    sel.appendChild(opt); sel.value = input;
    loadChatHistory();
  }
}

function clearChatUI() {
  document.getElementById('chat-messages').innerHTML = '<div class="bubble-system">Chat cleared</div>';
}

function appendChatBubble(role, content, toolName = null) {
  const chatEl = document.getElementById('chat-messages');
  const div = document.createElement('div');
  if (role === 'user') {
    div.className = 'chat-bubble bubble-user';
    div.innerHTML = `<div class="bubble-label">You</div>${escHtml(content || '')}`;
  } else if (role === 'agent') {
    div.className = 'chat-bubble bubble-agent';
    div.innerHTML = `<div class="bubble-label">🤖 Agent</div>${escHtml(content || '')}`;
  } else if (role === 'tool') {
    div.className = 'chat-bubble bubble-agent';
    div.innerHTML = `<div class="bubble-tools">🔧 Tool: ${escHtml(toolName || 'unknown')}</div>`;
  } else {
    div.className = 'chat-bubble bubble-system';
    div.textContent = content || '';
  }
  chatEl.appendChild(div);
  chatEl.scrollTop = chatEl.scrollHeight;
  return div;
}

function chatKeydown(e) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChatMessage(); }
}

async function sendChatMessage() {
  const agentId = document.getElementById('chat-agent-sel').value;
  let sessionId = document.getElementById('chat-session-sel').value;
  if (sessionId === '__manual__' || !sessionId) {
    sessionId = prompt('Session ID:');
    if (!sessionId) return;
  }
  if (!agentId) return alert('Pilih agent dulu');

  const input = document.getElementById('chat-input');
  const msg = input.value.trim();
  if (!msg) return;

  input.value = '';
  appendChatBubble('user', msg);

  const btn = document.getElementById('chat-send-btn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>';

  const thinkBubble = appendChatBubble('agent', '...');
  thinkBubble.style.opacity = '0.5';

  // Use X-Agent-Key (each agent has its own key)
  const r = await apiMsg(agentId, `/v1/agents/${agentId}/sessions/${sessionId}/messages`, { message: msg });

  thinkBubble.remove();
  btn.disabled = false;
  btn.textContent = 'Send ↑';

  if (r.ok) {
    appendChatBubble('agent', r.data.reply || '');
    const steps = r.data.steps || [];
    if (steps.length) {
      const toolDiv = document.createElement('div');
      toolDiv.className = 'chat-bubble bubble-agent';
      toolDiv.innerHTML = `<div class="bubble-tools">🔧 Tools used: ${steps.map(s => escHtml(s.tool)).join(', ')}</div>`;
      document.getElementById('chat-messages').appendChild(toolDiv);
    }
    if (r.data.run_id) document.getElementById('run-id-input').value = r.data.run_id;
  } else {
    appendChatBubble('system', `❌ Error ${r.status}: ${r.data?.detail || JSON.stringify(r.data)}`);
  }
}

// ═══════════════════════════════════════════════════════════════════
//  HISTORY
// ═══════════════════════════════════════════════════════════════════
async function loadHistory() {
  const sid = document.getElementById('hist-session-id').value.trim();
  const limit = document.getElementById('hist-limit').value || 50;
  if (!sid) return alert('Masukkan Session ID');
  const r = await api('GET', `/v1/sessions/${sid}/history?limit=${limit}`);
  const el = document.getElementById('history-result');
  if (!r.ok) { el.innerHTML = `<div class="card"><span class="badge badge-red">Error ${r.status}</span></div>`; return; }
  const msgs = r.data.messages || [];
  if (!msgs.length) { el.innerHTML = '<div class="card empty-state">Belum ada pesan</div>'; return; }
  el.innerHTML = `<div class="card">
    <div class="card-title">📜 ${msgs.length} messages</div>
    <div class="table-wrap"><table>
      <thead><tr><th>#</th><th>Role</th><th>Content / Tool</th><th>Time</th></tr></thead>
      <tbody>
        ${msgs.map((m, i) => `<tr>
          <td class="td-mono">${i + 1}</td>
          <td><span class="badge ${m.role === 'user' ? 'badge-blue' : m.role === 'agent' ? 'badge-green' : 'badge-yellow'}">${m.role}</span></td>
          <td style="max-width:500px; word-break:break-word">${m.tool_name
      ? `<span class="text-muted">🔧 ${escHtml(m.tool_name)}</span>`
      : escHtml((m.content || '').slice(0, 200)) + ((m.content || '').length > 200 ? '…' : '')
    }</td>
          <td class="td-mono">${m.timestamp?.slice(11, 19) || ''}</td>
        </tr>`).join('')}
      </tbody>
    </table></div>
  </div>`;
}

// ═══════════════════════════════════════════════════════════════════
//  DOCUMENTS
// ═══════════════════════════════════════════════════════════════════
async function loadDocuments() {
  const agentId = document.getElementById('doc-agent-sel').value;
  if (!agentId) return;
  const r = await api('GET', `/v1/agents/${agentId}/documents?limit=50`);
  const el = document.getElementById('docs-table');
  if (!r.ok || !r.data.items?.length) { el.innerHTML = '<div class="empty-state text-muted">Belum ada dokumen</div>'; return; }
  el.innerHTML = `<div class="table-wrap"><table>
    <thead><tr><th>Title</th><th>Source</th><th>Created</th><th>Actions</th></tr></thead>
    <tbody>
      ${r.data.items.map(d => `<tr>
        <td><strong>${escHtml(d.title)}</strong></td>
        <td class="td-mono">${escHtml(d.source || '—')}</td>
        <td class="td-mono">${d.created_at?.slice(0, 10)}</td>
        <td><button class="btn btn-danger btn-sm" onclick="deleteDoc('${d.id}')">🗑</button></td>
      </tr>`).join('')}
    </tbody>
  </table></div>`;
}

async function uploadDoc() {
  const agentId = document.getElementById('doc-agent-sel').value;
  if (!agentId) return alert('Pilih agent dulu');
  const payload = {
    title: document.getElementById('doc-title').value.trim(),
    content: document.getElementById('doc-content').value.trim(),
    source: document.getElementById('doc-source').value.trim() || null,
  };
  if (!payload.title || !payload.content) return alert('Title dan content wajib diisi');
  const r = await api('POST', `/v1/agents/${agentId}/documents`, payload);
  if (r.ok) { loadDocuments();['doc-title', 'doc-content'].forEach(id => document.getElementById(id).value = ''); }
}

async function uploadFile() {
  const agentId = document.getElementById('doc-agent-sel').value;
  if (!agentId) return alert('Pilih agent dulu');
  const fileInput = document.getElementById('doc-file');
  if (!fileInput.files.length) return alert('Pilih file dulu');
  const formData = new FormData();
  formData.append('file', fileInput.files[0]);
  const title = document.getElementById('doc-file-title').value.trim();
  if (title) formData.append('title', title);
  const r = await api('POST', `/v1/agents/${agentId}/documents/upload`, formData, true);
  if (r.ok) { loadDocuments(); fileInput.value = ''; }
}

async function searchDocs() {
  const agentId = document.getElementById('doc-agent-sel').value;
  if (!agentId) return alert('Pilih agent dulu');
  const q = document.getElementById('doc-search-q').value.trim();
  if (!q) return alert('Masukkan query');
  await api('POST', `/v1/agents/${agentId}/documents/search`, { query: q, limit: 5 });
}

async function deleteDoc(id) {
  if (!confirm('Hapus dokumen ini?')) return;
  const agentId = document.getElementById('doc-agent-sel').value;
  await api('DELETE', `/v1/agents/${agentId}/documents/${id}`);
  loadDocuments();
}

// ═══════════════════════════════════════════════════════════════════
//  MEMORY
// ═══════════════════════════════════════════════════════════════════
async function loadMemory() {
  const agentId = document.getElementById('mem-agent-sel').value;
  if (!agentId) return;
  const r = await api('GET', `/v1/agents/${agentId}/memory`);
  const el = document.getElementById('memory-list');
  const mems = r.data || [];
  if (!mems.length) { el.innerHTML = '<div class="empty-state text-muted">Belum ada memory</div>'; return; }
  el.innerHTML = mems.map(m => `
    <div class="flex" style="margin-bottom:8px; padding:8px; background:var(--surface-2); border-radius:6px">
      <div style="flex:1">
        <div style="font-weight:700; font-size:12px">${escHtml(m.key)}</div>
        <div class="text-muted">${escHtml(m.value_data || '')}</div>
      </div>
      <button class="btn btn-danger btn-sm" onclick="deleteMemory('${agentId}','${escHtml(m.key)}')">🗑</button>
    </div>`).join('');
}

async function addMemory() {
  const agentId = document.getElementById('mem-agent-sel').value;
  if (!agentId) return alert('Pilih agent dulu');
  const key = document.getElementById('mem-key').value.trim();
  const val = document.getElementById('mem-val').value.trim();
  if (!key || !val) return alert('Key dan value wajib diisi');
  const r = await api('POST', `/v1/agents/${agentId}/memory`, { key, value: val });
  if (r.ok) { loadMemory();['mem-key', 'mem-val'].forEach(id => document.getElementById(id).value = ''); }
}

async function deleteMemory(agentId, key) {
  await api('DELETE', `/v1/agents/${agentId}/memory/${encodeURIComponent(key)}`);
  loadMemory();
}

// ═══════════════════════════════════════════════════════════════════
//  SKILLS
// ═══════════════════════════════════════════════════════════════════
async function loadSkills() {
  const agentId = document.getElementById('skill-agent-sel').value;
  if (!agentId) return;
  const r = await api('GET', `/v1/agents/${agentId}/skills`);
  const el = document.getElementById('skills-list');
  const skills = r.data || [];
  if (!skills.length) { el.innerHTML = '<div class="empty-state text-muted">Belum ada skill</div>'; return; }
  el.innerHTML = skills.map(s => `
    <div style="margin-bottom:10px; padding:10px; background:var(--surface-2); border-radius:6px">
      <div class="flex">
        <strong style="flex:1">${escHtml(s.name)}</strong>
        <button class="btn btn-danger btn-sm" onclick="deleteSkill('${agentId}','${escHtml(s.name)}')">🗑</button>
      </div>
      <div class="text-muted mt-8">${escHtml(s.description || '')}</div>
    </div>`).join('');
}

async function createSkill() {
  const agentId = document.getElementById('skill-agent-sel').value;
  if (!agentId) return alert('Pilih agent dulu');
  const payload = {
    name: document.getElementById('sk-name').value.trim(),
    description: document.getElementById('sk-desc').value.trim(),
    content_md: document.getElementById('sk-content').value.trim(),
  };
  if (!payload.name) return alert('Name wajib diisi');
  const r = await api('POST', `/v1/agents/${agentId}/skills`, payload);
  if (r.ok) { loadSkills();['sk-name', 'sk-desc', 'sk-content'].forEach(id => document.getElementById(id).value = ''); }
}

async function deleteSkill(agentId, name) {
  await api('DELETE', `/v1/agents/${agentId}/skills/${name}`);
  loadSkills();
}

// ═══════════════════════════════════════════════════════════════════
//  CUSTOM TOOLS
// ═══════════════════════════════════════════════════════════════════
async function loadCustomTools() {
  const agentId = document.getElementById('ct-agent-sel').value;
  if (!agentId) return;
  const r = await api('GET', `/v1/agents/${agentId}/custom-tools`);
  const el = document.getElementById('custom-tools-list');
  const tools = r.data || [];
  if (!tools.length) { el.innerHTML = '<div class="empty-state text-muted">Belum ada custom tool</div>'; return; }
  el.innerHTML = tools.map(t => `
    <div style="margin-bottom:10px; padding:10px; background:var(--surface-2); border-radius:6px">
      <div class="flex">
        <strong style="flex:1">${escHtml(t.name)}</strong>
        <button class="btn btn-danger btn-sm" onclick="deleteCustomTool('${agentId}','${escHtml(t.name)}')">🗑</button>
      </div>
      <div class="text-muted mt-8">${escHtml(t.description || '')}</div>
    </div>`).join('');
}

async function createCustomTool() {
  const agentId = document.getElementById('ct-agent-sel').value;
  if (!agentId) return alert('Pilih agent dulu');
  const payload = {
    name: document.getElementById('ct-name').value.trim(),
    description: document.getElementById('ct-desc').value.trim(),
    code: document.getElementById('ct-code').value.trim(),
  };
  if (!payload.name || !payload.code) return alert('Name dan code wajib diisi');
  const r = await api('POST', `/v1/agents/${agentId}/custom-tools`, payload);
  if (r.ok) { loadCustomTools();['ct-name', 'ct-desc', 'ct-code'].forEach(id => document.getElementById(id).value = ''); }
}

async function deleteCustomTool(agentId, name) {
  await api('DELETE', `/v1/agents/${agentId}/custom-tools/${name}`);
  loadCustomTools();
}

// ═══════════════════════════════════════════════════════════════════
//  WHATSAPP SECTION
// ═══════════════════════════════════════════════════════════════════
function populateWAAgentSelect() {
  const sel = document.getElementById('wa-agent-sel');
  if (!sel) return;
  const val = sel.value;
  // Show all agents but highlight WA ones
  sel.innerHTML = '<option value="">— pilih agent —</option>';
  S.agents.forEach(a => {
    const opt = document.createElement('option');
    opt.value = a.id;
    opt.textContent = (a.channel_type === 'whatsapp' ? '📱 ' : '') + a.name;
    if (a.channel_type !== 'whatsapp') opt.style.color = 'var(--text-muted)';
    sel.appendChild(opt);
  });
  sel.value = val;
}

async function loadWAAgent() {
  const agentId = document.getElementById('wa-agent-sel').value;
  stopWAPolling();

  if (!agentId) {
    document.getElementById('wa-connect-panel').style.display = 'none';
    document.getElementById('wa-agent-info').innerHTML = '';
    return;
  }

  const agent = S.agents.find(a => a.id === agentId);
  if (!agent) return;

  // Show agent info
  const infoEl = document.getElementById('wa-agent-info');
  infoEl.innerHTML =
    `<span class="badge ${agent.channel_type === 'whatsapp' ? 'badge-green' : 'badge-red'}">
      ${agent.channel_type === 'whatsapp' ? '📱 channel: whatsapp' : '⚠ channel_type bukan whatsapp'}
    </span>
    ${agent.wa_device_id
      ? `<span class="text-muted" style="margin-left:8px; font-size:11px">device_id: ${escHtml(agent.wa_device_id)}</span>`
      : '<span class="text-muted" style="margin-left:8px">belum ada device</span>'}`;

  if (!agent.wa_device_id) {
    // Show connect button instead of hiding panel entirely
    document.getElementById('wa-connect-panel').style.display = 'none';
    infoEl.innerHTML += `
      <div style="margin-top:12px">
        <button class="btn btn-success" onclick="connectWADevice('${agentId}')">
          📱 Connect WhatsApp (Init Device + QR)
        </button>
        <div class="text-muted" style="margin-top:6px; font-size:11px">
          Pastikan wa-service sudah aktif (port 8080) sebelum connect.
        </div>
      </div>`;
    return;
  }

  S.waCurrentAgentId = agentId;
  S.waCurrentDeviceId = agent.wa_device_id;

  document.getElementById('wa-connect-panel').style.display = 'block';
  renderWAInfo(agent);
  await refreshWAStatus();
  await refreshWAQR();
}

async function connectWADevice(agentId) {
  const infoEl = document.getElementById('wa-agent-info');
  infoEl.innerHTML = '<span class="spinner"></span> Menghubungkan ke WhatsApp... (max 35 detik)';

  const r = await api('POST', `/v1/agents/${agentId}/whatsapp/connect`);
  if (!r.ok) {
    infoEl.innerHTML = `
      <span class="badge badge-red">❌ Gagal connect: ${escHtml(r.data?.detail || JSON.stringify(r.data))}</span>
      <div class="text-muted" style="margin-top:6px; font-size:11px">
        Pastikan wa-service aktif: <code>make wa</code>
      </div>`;
    return;
  }

  // Reload agents to get updated wa_device_id
  await loadAgents();
  populateWAAgentSelect();
  document.getElementById('wa-agent-sel').value = agentId;

  // Show QR in modal if available
  if (r.data.qr_image) {
    openQRModal(agentId, r.data.device_id, r.data.qr_image);
  }

  // Load WA panel
  await loadWAAgent();
}

async function refreshWAStatus() {
  if (!S.waCurrentAgentId) return;
  const r = await api('GET', `/v1/agents/${S.waCurrentAgentId}/whatsapp/status`);
  if (!r.ok) {
    setWAStatusBadge('error', '⚠ wa-service tidak dapat dijangkau');
    return;
  }
  const { status, phone_number } = r.data;
  renderWAStatusBadge(status, phone_number);
  if (status === 'connected') {
    stopWAPolling();
    setWAPollIndicator('');
  } else if (status === 'waiting_qr') {
    startWAPolling();
  }
}

async function refreshWAQR() {
  if (!S.waCurrentAgentId) return;
  const wrap = document.getElementById('wa-qr-wrap');
  if (!wrap) return;

  const r = await api('GET', `/v1/agents/${S.waCurrentAgentId}/whatsapp/qr`);
  if (!r.ok) {
    wrap.innerHTML = '<div class="empty-state text-muted">Gagal load QR — cek wa-service</div>';
    return;
  }
  const { qr_image, status } = r.data;
  if (status === 'connected') {
    wrap.innerHTML = `
      <div style="text-align:center; padding:24px">
        <div style="font-size:48px">✅</div>
        <div style="font-size:16px; font-weight:700; color:var(--success); margin-top:8px">WhatsApp Terhubung</div>
      </div>`;
    renderWAStatusBadge('connected', '');
    stopWAPolling();
  } else if (qr_image) {
    wrap.innerHTML = `
      <div style="text-align:center">
        <img src="data:image/png;base64,${qr_image}" class="qr-img" alt="WhatsApp QR">
        <div class="text-muted" style="margin-top:8px; font-size:11px">Auto-refresh tiap 20 detik</div>
      </div>`;
  } else {
    wrap.innerHTML = '<div class="empty-state text-muted">QR tidak tersedia — coba Refresh QR</div>';
  }
}

function renderWAInfo(agent) {
  document.getElementById('wa-info-rows').innerHTML = `
    <div class="info-row"><span class="info-label">Agent</span><span class="info-val">${escHtml(agent.name)}</span></div>
    <div class="info-row"><span class="info-label">Device ID</span><span class="info-val td-mono" style="font-size:10px">${escHtml(agent.wa_device_id || '—')}</span></div>
    <div class="info-row"><span class="info-label">Agent Key</span><span class="info-val td-mono" style="font-size:10px">${escHtml(agent.api_key?.slice(0, 16))}...</span></div>`;
}

function renderWAStatusBadge(status, phone) {
  const el = document.getElementById('wa-qr-status-badge');
  if (!el) return;
  const map = {
    waiting_qr: { cls: 'badge-yellow', label: '⏳ Waiting QR Scan' },
    connected: { cls: 'badge-green', label: `✅ Connected${phone ? ' · ' + phone : ''}` },
    disconnected: { cls: 'badge-red', label: '🔴 Disconnected' },
  };
  const m = map[status] || { cls: 'badge-red', label: status };
  el.innerHTML = `<span class="badge ${m.cls}" style="font-size:12px; padding:4px 12px">${escHtml(m.label)}</span>`;
}

function setWAStatusBadge(type, text) {
  const el = document.getElementById('wa-qr-status-badge');
  if (el) el.innerHTML = `<span class="badge badge-${type === 'error' ? 'red' : 'yellow'}">${escHtml(text)}</span>`;
}

function setWAPollIndicator(text) {
  const el = document.getElementById('wa-poll-indicator');
  if (el) el.textContent = text;
}

function startWAPolling() {
  if (S.waStatusPoller) return; // already polling
  setWAPollIndicator('🔄 Polling...');
  S.waStatusPoller = setInterval(async () => {
    if (!S.waCurrentAgentId) { stopWAPolling(); return; }
    const r = await api('GET', `/v1/agents/${S.waCurrentAgentId}/whatsapp/status`);
    if (r.ok) {
      const { status, phone_number } = r.data;
      renderWAStatusBadge(status, phone_number);
      if (status === 'connected') {
        stopWAPolling();
        await refreshWAQR();
        renderWAInfo(S.agents.find(a => a.id === S.waCurrentAgentId) || {});
      }
    }
  }, 3000);

  // Also refresh QR image every 20s (whatsmeow rotates QR)
  S.waQRPoller = setInterval(() => {
    if (S.waCurrentAgentId) refreshWAQR();
  }, 20000);
}

function stopWAPolling() {
  if (S.waStatusPoller) { clearInterval(S.waStatusPoller); S.waStatusPoller = null; }
  if (S.waQRPoller) { clearInterval(S.waQRPoller); S.waQRPoller = null; }
  setWAPollIndicator('');
}

async function disconnectWA() {
  if (!S.waCurrentAgentId) return;
  if (!confirm('Logout dari WhatsApp? Agent akan disconnect dari nomor yang terhubung.')) return;
  const r = await api('DELETE', `/v1/agents/${S.waCurrentAgentId}/whatsapp`);
  if (r.ok || r.status === 204) {
    stopWAPolling();
    document.getElementById('wa-connect-panel').style.display = 'none';
    await loadAgents();
    populateWAAgentSelect();
    document.getElementById('wa-agent-sel').value = S.waCurrentAgentId;
    await loadWAAgent();
  }
}

async function simulateWAIncoming() {
  if (!S.waCurrentDeviceId) return alert('Pilih agent dengan device WA dulu');
  const from = document.getElementById('wa-sim-from').value.trim();
  const msg = document.getElementById('wa-sim-msg').value.trim();
  if (!from || !msg) return alert('From dan message wajib diisi');

  const resultEl = document.getElementById('wa-sim-result');
  resultEl.innerHTML = '<span class="spinner"></span> Sending...';

  const r = await api('POST', '/v1/channels/wa/incoming', {
    device_id: S.waCurrentDeviceId,
    from,
    message: msg,
    timestamp: Math.floor(Date.now() / 1000),
  });

  if (r.ok) {
    resultEl.innerHTML = `
      <div style="background:var(--chat-agent); border:1px solid #2a4a2a; border-radius:8px; padding:12px; margin-top:8px">
        <div class="badge badge-green" style="margin-bottom:6px">Agent Reply</div>
        <div style="white-space:pre-wrap; font-size:13px">${escHtml(r.data.reply || '(kosong)')}</div>
        ${r.data.run_id ? `<div class="text-muted" style="margin-top:6px; font-size:10px">run_id: ${r.data.run_id}</div>` : ''}
      </div>`;
  } else {
    resultEl.innerHTML = `<div class="badge badge-red">Error ${r.status}: ${escHtml(JSON.stringify(r.data))}</div>`;
  }
}

async function createWAAgent() {
  const name = document.getElementById('wa-new-name').value.trim();
  const model = document.getElementById('wa-new-model').value.trim();
  const inst = document.getElementById('wa-new-inst').value.trim();
  if (!name) return alert('Name wajib diisi');

  const resultEl = document.getElementById('wa-create-result');

  // Check wa-service health first
  const waHealth = await waApi('GET', '/health');
  if (!waHealth.ok) {
    resultEl.innerHTML = `
      <div class="badge badge-red" style="margin-bottom:8px">⚠️ wa-service tidak aktif!</div>
      <div class="text-muted" style="font-size:12px; line-height:1.6">
        Jalankan wa-service dulu di terminal terpisah:<br>
        <code style="background:var(--surface-2);padding:4px 8px;border-radius:4px">cd wa-service && ./wa-service</code><br>
        Atau: <code style="background:var(--surface-2);padding:4px 8px;border-radius:4px">make wa</code>
      </div>`;
    const proceed = confirm('wa-service belum aktif. Tetap buat agent? (QR bisa di-scan nanti)');
    if (!proceed) return;
  }

  resultEl.innerHTML = '<span class="spinner"></span> Membuat agent & menunggu QR (max 30s)...';

  const r = await api('POST', '/v1/agents', {
    name,
    model: model || 'anthropic/claude-sonnet-4-6',
    instructions: inst || 'Kamu adalah customer service yang membalas pesan WhatsApp dengan ramah.',
    channel_type: 'whatsapp',
    tools_config: {
      memory: true, tool_creator: true, rag: true, sandbox: false,
      skills: true, scheduler: true, escalation: true, http: false, mcp: false
    },
    escalation_config: {},
    safety_policy: {},
  });

  if (!r.ok) {
    resultEl.innerHTML = `<div class="badge badge-red">Error ${r.status}: ${escHtml(JSON.stringify(r.data))}</div>`;
    return;
  }

  await loadAgents();
  const agent = r.data;
  resultEl.innerHTML = `<span class="badge badge-green">✅ Agent dibuat: ${escHtml(agent.name)}</span>`;

  // Clear the form
  document.getElementById('wa-new-name').value = '';
  document.getElementById('wa-new-inst').value = '';

  if (agent.qr_image) {
    // Show QR in modal
    openQRModal(agent.id, agent.wa_device_id, agent.qr_image);
  } else if (agent.wa_device_id) {
    // No QR yet — select agent in WA panel to load QR
    resultEl.innerHTML += '<div class="text-muted" style="margin-top:4px">Pilih agent di atas untuk melihat QR code.</div>';
  }

  // Pre-select new agent in WA panel
  populateWAAgentSelect();
  const sel = document.getElementById('wa-agent-sel');
  sel.value = agent.id;
  loadWAAgent();
}

// ── QR Modal ──────────────────────────────────────────────────────
function openQRModal(agentId, deviceId, qrImage) {
  S.waModalAgentId = agentId;
  S.waModalDeviceId = deviceId;
  const imgEl = document.getElementById('modal-qr-img');
  imgEl.innerHTML = qrImage
    ? `<img src="data:image/png;base64,${qrImage}" class="qr-img" alt="WhatsApp QR">`
    : '<div class="empty-state text-muted">QR tidak tersedia</div>';
  document.getElementById('modal-qr-status').innerHTML = `<span class="badge badge-yellow">⏳ Waiting QR Scan</span>`;
  document.getElementById('modal-done-btn').style.display = 'none';
  document.getElementById('qr-modal').style.display = 'flex';
  // Start polling for this modal
  startModalPolling();
}

function closeQRModal(e) {
  if (e && e.target !== document.getElementById('qr-modal')) return;
  stopModalPolling();
  document.getElementById('qr-modal').style.display = 'none';
  // Refresh agents + WA section
  loadAgents().then(() => {
    if (S.waCurrentAgentId === S.waModalAgentId) loadWAAgent();
  });
}

let _modalPoller = null;
function startModalPolling() {
  if (_modalPoller) clearInterval(_modalPoller);
  _modalPoller = setInterval(async () => {
    if (!S.waModalAgentId) { stopModalPolling(); return; }
    const r = await api('GET', `/v1/agents/${S.waModalAgentId}/whatsapp/status`);
    if (!r.ok) return;
    const { status, phone_number } = r.data;
    const statusEl = document.getElementById('modal-qr-status');
    if (status === 'connected') {
      statusEl.innerHTML = `<span class="badge badge-green">✅ Connected${phone_number ? ' · ' + escHtml(phone_number) : ''}</span>`;
      document.getElementById('modal-qr-img').innerHTML =
        `<div style="text-align:center;padding:24px"><div style="font-size:64px">✅</div><div style="font-size:18px;font-weight:700;color:var(--success);margin-top:8px">WhatsApp Terhubung!</div></div>`;
      document.getElementById('modal-done-btn').style.display = 'inline-flex';
      document.getElementById('modal-qr-hint').textContent = '';
      stopModalPolling();
    }
  }, 3000);

  // Refresh QR image every 20s
  setTimeout(async function tick() {
    if (!S.waModalAgentId || document.getElementById('qr-modal').style.display === 'none') return;
    const r = await api('GET', `/v1/agents/${S.waModalAgentId}/whatsapp/qr`);
    if (r.ok && r.data.qr_image && r.data.status !== 'connected') {
      document.getElementById('modal-qr-img').innerHTML =
        `<img src="data:image/png;base64,${r.data.qr_image}" class="qr-img" alt="WhatsApp QR">`;
    }
    if (r.data?.status !== 'connected') setTimeout(tick, 20000);
  }, 20000);
}

function stopModalPolling() {
  if (_modalPoller) { clearInterval(_modalPoller); _modalPoller = null; }
}

async function refreshModalQR() {
  if (!S.waModalAgentId) return;
  const r = await api('GET', `/v1/agents/${S.waModalAgentId}/whatsapp/qr`);
  if (r.ok && r.data.qr_image) {
    document.getElementById('modal-qr-img').innerHTML =
      `<img src="data:image/png;base64,${r.data.qr_image}" class="qr-img" alt="WhatsApp QR">`;
  }
}

// ═══════════════════════════════════════════════════════════════════
//  ESCALATION SIMULATOR
// ═══════════════════════════════════════════════════════════════════
function loadEscalationAgent() {
  const agentId = document.getElementById('esc-agent-sel').value;
  if (!agentId) return;
  const agent = S.agents.find(a => a.id === agentId);
  if (!agent) return;
  const opPhone = agent.escalation_config?.operator_phone || '';
  document.getElementById('esc-operator-phone').value = opPhone;
  document.getElementById('esc-status-bar').innerHTML = opPhone
    ? `<span class="badge badge-blue">Operator phone: ${escHtml(opPhone)}</span>`
    : '<span class="badge badge-red">⚠ escalation_config.operator_phone belum diset</span>';
}

async function forceEscalation(active) {
  const agentId = document.getElementById('esc-agent-sel').value;
  const sessionId = document.getElementById('esc-session-id').value.trim();
  if (!agentId || !sessionId) return alert('Pilih agent dan isi session ID dulu');
  const r = await api('PATCH', `/v1/agents/${agentId}/sessions/${sessionId}`, { escalation_active: active });
  if (r.ok) {
    checkEscalationStatus();
    escLog(active ? 'op' : 'user', 'system', active
      ? '🚨 Eskalasi diaktifkan secara manual'
      : '✅ Eskalasi dinonaktifkan — agent kembali normal');
  }
}

async function checkEscalationStatus() {
  const agentId = document.getElementById('esc-agent-sel').value;
  const sessionId = document.getElementById('esc-session-id').value.trim();
  if (!agentId || !sessionId) return alert('Pilih agent dan isi session ID');
  const r = await api('GET', `/v1/agents/${agentId}/sessions/${sessionId}`);
  if (!r.ok) return;
  const active = r.data.escalation_active;
  document.getElementById('esc-status-bar').innerHTML =
    `<span class="badge ${active ? 'badge-red' : 'badge-green'}">
      ${active ? '🚨 Eskalasi AKTIF' : '✅ Normal'}
    </span>
    <span class="text-muted" style="margin-left:8px">channel: ${escHtml(r.data.channel_type || 'none')}</span>`;
}

function escLog(side, role, text) {
  const el = document.getElementById(side === 'user' ? 'esc-user-log' : 'esc-op-log');
  const div = document.createElement('div');
  div.className = `chat-bubble ${role === 'agent' ? 'bubble-agent' : role === 'system' ? 'bubble-system' : 'bubble-user'}`;
  div.innerHTML = `<div class="bubble-label">${escHtml(
    role === 'agent' ? '🤖 Agent' : role === 'system' ? 'System' : role === 'operator' ? '👨‍💼 Operator' : '👤 User'
  )}</div>${escHtml(text)}`;
  el.appendChild(div);
  el.scrollTop = el.scrollHeight;
}

async function escalationSendUser() {
  const sessionId = document.getElementById('esc-session-id').value.trim();
  const msg = document.getElementById('esc-user-msg').value.trim();
  if (!sessionId) return alert('Isi Session ID dulu');
  if (!msg) return;
  document.getElementById('esc-user-msg').value = '';
  escLog('user', 'user', msg);
  const r = await api('POST', `/v1/channels/incoming/${sessionId}`, { message: msg });
  if (r.ok) {
    if (r.data.reply) escLog('user', 'agent', r.data.reply);
  } else {
    escLog('user', 'system', `❌ Error ${r.status}: ${r.data?.detail || ''}`);
  }
}

async function escalationSendOperator() {
  const sessionId = document.getElementById('esc-session-id').value.trim();
  const opPhone = document.getElementById('esc-operator-phone').value.trim();
  const msg = document.getElementById('esc-op-msg').value.trim();
  if (!sessionId) return alert('Isi Session ID dulu');
  if (!opPhone) return alert('Operator phone belum diset.');
  if (!msg) return;
  document.getElementById('esc-op-msg').value = '';
  escLog('op', 'operator', msg);
  const r = await api('POST', `/v1/channels/incoming/${sessionId}`, { message: msg, from_phone: opPhone });
  if (r.ok) {
    if (r.data.reply) escLog('op', 'agent', r.data.reply);
    (r.data.messages_to_user || []).forEach(m => {
      if (m.type === 'reply_to_user') {
        escLog('user', 'agent', m.message);
        escLog('user', 'system', '📨 Dikirim agent atas perintah operator');
      } else if (m.type === 'send_to_number') {
        escLog('user', 'agent', m.message);
        escLog('user', 'system', `📨 Dikirim ke ${m.target}`);
      }
    });
    if (msg.toLowerCase().includes('selesai') || msg.toLowerCase().includes('tangani sendiri')) {
      setTimeout(checkEscalationStatus, 500);
    }
  } else {
    escLog('op', 'system', `❌ Error ${r.status}: ${r.data?.detail || ''}`);
  }
}

// ═══════════════════════════════════════════════════════════════════
//  CHANNELS — Incoming
// ═══════════════════════════════════════════════════════════════════
async function sendIncoming() {
  const sessionId = document.getElementById('ch-session-id').value.trim();
  const fromPhone = document.getElementById('ch-from').value.trim();
  const message = document.getElementById('ch-message').value.trim();
  if (!sessionId || !message) return alert('Session ID dan message wajib diisi');
  const payload = { message };
  if (fromPhone) payload.from_phone = fromPhone;
  const r = await api('POST', `/v1/channels/incoming/${sessionId}`, payload);
  const el = document.getElementById('ch-result');
  if (r.ok) {
    el.innerHTML = `
      <div style="background:var(--chat-agent); border:1px solid #2a4a2a; border-radius:8px; padding:12px">
        <div class="badge badge-green" style="margin-bottom:8px">Agent Reply</div>
        <div style="white-space:pre-wrap; font-size:13px">${escHtml(r.data.reply || '')}</div>
        ${r.data.run_id ? `<div class="text-muted" style="margin-top:8px; font-size:10px">run_id: ${r.data.run_id}</div>` : ''}
      </div>`;
    if (r.data.run_id) document.getElementById('run-id-input').value = r.data.run_id;
  } else {
    el.innerHTML = `<div class="badge badge-red">Error ${r.status}: ${escHtml(JSON.stringify(r.data))}</div>`;
  }
}

// ═══════════════════════════════════════════════════════════════════
//  RUNS
// ═══════════════════════════════════════════════════════════════════
async function getRun() {
  const runId = document.getElementById('run-id-input').value.trim();
  if (!runId) return alert('Masukkan Run ID');
  const r = await api('GET', `/v1/runs/${runId}`);
  const el = document.getElementById('run-result');
  if (!r.ok) { el.innerHTML = `<div class="card"><span class="badge badge-red">Error ${r.status}</span></div>`; return; }
  const d = r.data;
  el.innerHTML = `
    <div class="card">
      <div class="card-title">Run Detail</div>
      <div class="card-grid cols-3">
        <div class="form-group"><label>Run ID</label><input readonly value="${d.run_id || ''}"></div>
        <div class="form-group"><label>Session ID</label><input readonly value="${d.session_id || ''}"></div>
        <div class="form-group"><label>Steps</label><input readonly value="${d.steps?.length || 0}"></div>
      </div>
      ${d.steps?.length ? `
        <div class="divider"></div>
        <div class="card-title">Tool Steps</div>
        <div class="table-wrap"><table>
          <thead><tr><th>#</th><th>Tool</th><th>Args</th><th>Result</th></tr></thead>
          <tbody>
            ${d.steps.map(s => `<tr>
              <td class="td-mono">${s.step}</td>
              <td><span class="badge badge-yellow">${escHtml(s.tool)}</span></td>
              <td class="td-mono" style="max-width:200px;word-break:break-all">${escHtml(JSON.stringify(s.args)).slice(0, 100)}</td>
              <td style="max-width:250px;word-break:break-word">${escHtml((s.result || '').slice(0, 150))}</td>
            </tr>`).join('')}
          </tbody>
        </table></div>` : ''}
    </div>`;
}
