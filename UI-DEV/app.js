/* ═══════════════════════════════════════════════════════════════════
   Managed Agents — Dev UI  |  app.js
   ═══════════════════════════════════════════════════════════════════ */

// ── State ──────────────────────────────────────────────────────────
const S = {
  baseUrl: localStorage.getItem('baseUrl') || 'http://localhost:8000',
  apiKey:  localStorage.getItem('apiKey')  || '',
  agents:  [],
  logCollapsed: false,
  sseConnection: null,   // active EventSource
  sseSessionId:  null,   // session ID yang sedang di-listen
};

// ── Init ───────────────────────────────────────────────────────────
window.addEventListener('DOMContentLoaded', () => {
  document.getElementById('cfg-url').value = S.baseUrl;
  document.getElementById('cfg-key').value = S.apiKey;
  setAgentFormDefaults();
  loadAgents();
  nav('agents');
});

// ── Config ─────────────────────────────────────────────────────────
function saveConfig() {
  S.baseUrl = document.getElementById('cfg-url').value.replace(/\/$/, '');
  S.apiKey  = document.getElementById('cfg-key').value;
  localStorage.setItem('baseUrl', S.baseUrl);
  localStorage.setItem('apiKey',  S.apiKey);
  pingServer();
}

async function pingServer() {
  const dot = document.getElementById('status-dot');
  dot.className = '';
  try {
    const r = await fetch(`${S.baseUrl}/health`);
    dot.className = r.ok ? 'ok' : 'err';
  } catch {
    dot.className = 'err';
  }
}

// ── Navigation ─────────────────────────────────────────────────────
function nav(id) {
  document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.getElementById(`sec-${id}`).classList.add('active');
  const items = document.querySelectorAll('.nav-item');
  items.forEach(item => {
    if (item.getAttribute('onclick') === `nav('${id}')`) item.classList.add('active');
  });
  // populate selects on section entry
  if (['sessions','chat','documents','memory','skills','tools','escalation'].includes(id)) {
    populateAgentSelects();
  }
  // disconnect SSE when leaving chat
  if (id !== 'chat') disconnectSSE();
}

// ── SSE (Scheduled / Proactive Messages) ───────────────────────────
function connectSSE(sessionId) {
  if (S.sseConnection && S.sseSessionId === sessionId) return; // already connected
  disconnectSSE();

  if (!sessionId || !S.apiKey) return;

  const url = `${S.baseUrl}/v1/sessions/${sessionId}/stream?api_key=${encodeURIComponent(S.apiKey)}`;
  const es = new EventSource(url);
  S.sseConnection = es;
  S.sseSessionId  = sessionId;
  _setSseIndicator('connecting');

  es.addEventListener('open', () => _setSseIndicator('connected'));

  es.addEventListener('message', (e) => {
    try {
      const data = JSON.parse(e.data);
      if (data.type === 'scheduled_message') {
        appendScheduledBubble(data.reply, data.label);
      }
    } catch {}
  });

  es.addEventListener('ping', () => {}); // keep-alive, no-op

  es.addEventListener('error', () => {
    _setSseIndicator('disconnected');
    // EventSource auto-reconnects — tidak perlu manual reconnect
  });
}

function disconnectSSE() {
  if (S.sseConnection) {
    S.sseConnection.close();
    S.sseConnection = null;
    S.sseSessionId  = null;
  }
  _setSseIndicator('off');
}

function _setSseIndicator(state) {
  const el = document.getElementById('sse-indicator');
  if (!el) return;
  const map = {
    off:          { dot: '⚪', text: 'Stream off',      cls: '' },
    connecting:   { dot: '🟡', text: 'Connecting...',   cls: 'sse-connecting' },
    connected:    { dot: '🟢', text: 'Live',            cls: 'sse-connected' },
    disconnected: { dot: '🔴', text: 'Disconnected',    cls: 'sse-error' },
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

// ── API Helper ─────────────────────────────────────────────────────
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
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function methodColor(m) {
  return { GET:'badge-blue', POST:'badge-green', PATCH:'badge-yellow', DELETE:'badge-red' }[m] || 'badge-blue';
}

function toggleLog() {
  S.logCollapsed = !S.logCollapsed;
  const body = document.getElementById('log-body');
  const icon = document.getElementById('log-toggle-icon');
  body.style.display = S.logCollapsed ? 'none' : '';
  icon.textContent = S.logCollapsed ? '▲' : '▼';
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
          <th>Name</th><th>Model</th><th>Escalation</th><th>Created</th><th>Actions</th>
        </tr></thead>
        <tbody>
          ${agents.map(a => `
            <tr>
              <td>
                <strong>${escHtml(a.name)}</strong>
                ${a.description ? `<br><span class="text-muted">${escHtml(a.description)}</span>` : ''}
              </td>
              <td class="td-mono">${escHtml(a.model)}</td>
              <td>${a.escalation_config?.operator_phone
                  ? `<span class="badge badge-green">${escHtml(a.escalation_config.operator_phone)}</span>`
                  : '<span class="badge badge-red">not set</span>'}</td>
              <td class="td-mono">${a.created_at?.slice(0,10)}</td>
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
  document.getElementById('a-name').scrollIntoView({ behavior: 'smooth' });
}

function resetAgentForm() {
  document.getElementById('agent-form-title').textContent = '➕ Create Agent';
  document.getElementById('agent-edit-id').value = '';
  ['a-name','a-desc','a-instructions'].forEach(id => document.getElementById(id).value = '');
  document.getElementById('a-model').value = 'anthropic/claude-sonnet-4-6';
  document.getElementById('a-temp').value = '0.7';
  setAgentFormDefaults();
}

async function submitAgent() {
  const editId = document.getElementById('agent-edit-id').value;
  const payload = {
    name:             document.getElementById('a-name').value.trim(),
    description:      document.getElementById('a-desc').value.trim() || null,
    instructions:     document.getElementById('a-instructions').value.trim(),
    model:            document.getElementById('a-model').value.trim(),
    temperature:      parseFloat(document.getElementById('a-temp').value) || 0.7,
    tools_config:     parseJson(document.getElementById('a-tools').value),
    escalation_config: parseJson(document.getElementById('a-escalation').value),
    safety_policy:    parseJson(document.getElementById('a-safety').value),
  };
  if (!payload.name) return alert('Name wajib diisi');

  const r = editId
    ? await api('PATCH', `/v1/agents/${editId}`, payload)
    : await api('POST', '/v1/agents', payload);

  if (r.ok) { resetAgentForm(); loadAgents(); }
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
  const selects = ['sess-agent-sel','chat-agent-sel','doc-agent-sel',
                   'mem-agent-sel','skill-agent-sel','ct-agent-sel','esc-agent-sel'];
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
  const agentId = document.getElementById('sess-agent-sel').value;
  if (!agentId) return;
  // Sessions don't have a direct list endpoint, show create only
  document.getElementById('sessions-table').innerHTML =
    '<div class="text-muted" style="padding:8px">Buat session baru di bawah. Session ID akan muncul di log.</div>';
}

function updateChannelConfigExample() {
  const type = document.getElementById('s-channel-type').value;
  const examples = {
    whatsapp: { user_phone: '+62812xxx', api_key: 'WABA_TOKEN', phone_number_id: '12345678' },
    telegram: { chat_id: '123456789', bot_token: 'BOT_TOKEN' },
    slack:    { webhook_url: 'https://hooks.slack.com/services/xxx' },
    webhook:  { url: 'https://your-server.com/webhook', headers: {} },
    'in-app': {},
  };
  document.getElementById('s-channel-config').value =
    JSON.stringify(examples[type] || {}, null, 2);
}

async function createSession() {
  const agentId = document.getElementById('sess-agent-sel').value;
  if (!agentId) return alert('Pilih agent dulu');
  const channelType = document.getElementById('s-channel-type').value;
  const payload = {
    external_user_id: document.getElementById('s-userid').value.trim() || null,
    metadata:    parseJson(document.getElementById('s-meta').value),
    channel_type: channelType || null,
    channel_config: channelType ? parseJson(document.getElementById('s-channel-config').value) : {},
  };
  const r = await api('POST', `/v1/agents/${agentId}/sessions`, payload);
  if (r.ok) {
    const sid = r.data.id;
    alert(`Session created!\nID: ${sid}\n\nID ini sudah tercopy di log. Gunakan di Chat section.`);
    // Auto-populate chat section + load history + connect SSE
    const sel = document.getElementById('chat-session-sel');
    const opt = document.createElement('option');
    opt.value = sid;
    opt.textContent = sid.slice(0, 8) + '...';
    sel.appendChild(opt);
    sel.value = sid;
    loadChatHistory();
  }
}

// ═══════════════════════════════════════════════════════════════════
//  CHAT
// ═══════════════════════════════════════════════════════════════════
const chatSessions = {}; // cache: agentId → sessions[]

async function loadSessionsForChat() {
  const agentId = document.getElementById('chat-agent-sel').value;
  if (!agentId) return;
  const sel = document.getElementById('chat-session-sel');
  sel.innerHTML = '<option value="">Ketik Session ID langsung atau...</option>';
  // Add manual input option
  sel.innerHTML += '<option value="__manual__">— ketik manual —</option>';
}

async function loadChatHistory() {
  const sessionId = document.getElementById('chat-session-sel').value;
  if (!sessionId || sessionId === '__manual__') {
    showChatManualInput();
    return;
  }
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
      // Sembunyikan [SCHEDULED] user message — itu trigger internal, bukan dari user
      if (m.role === 'user' && (m.content || '').startsWith('[SCHEDULED]')) return;
      appendChatBubble(m.role, m.content, m.tool_name);
    });
  }
  chatEl.scrollTop = chatEl.scrollHeight;

  // Connect SSE untuk terima scheduled/proactive messages secara real-time
  connectSSE(sessionId);
}

function showChatManualInput() {
  const sel = document.getElementById('chat-session-sel');
  const input = prompt('Masukkan Session ID:');
  if (input) {
    const opt = document.createElement('option');
    opt.value = input;
    opt.textContent = input.slice(0, 8) + '...';
    sel.appendChild(opt);
    sel.value = input;
    loadChatHistory(); // trigger history load + connectSSE
  }
}

function clearChatUI() {
  document.getElementById('chat-messages').innerHTML =
    '<div class="bubble-system">Chat cleared</div>';
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
  const agentId   = document.getElementById('chat-agent-sel').value;
  let sessionId = document.getElementById('chat-session-sel').value;

  // Support manual session ID entry in the select
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

  // Add thinking bubble
  const thinkBubble = appendChatBubble('agent', '...');
  thinkBubble.style.opacity = '0.5';

  const r = await api('POST', `/v1/agents/${agentId}/sessions/${sessionId}/messages`, { message: msg });

  thinkBubble.remove();
  btn.disabled = false;
  btn.textContent = 'Send ↑';

  if (r.ok) {
    const reply = r.data.reply || '';
    const steps = r.data.steps || [];
    appendChatBubble('agent', reply);
    if (steps.length) {
      const toolDiv = document.createElement('div');
      toolDiv.className = 'chat-bubble bubble-agent';
      toolDiv.innerHTML = `<div class="bubble-tools">🔧 Tools used: ${steps.map(s => escHtml(s.tool)).join(', ')}</div>`;
      document.getElementById('chat-messages').appendChild(toolDiv);
    }
    // Auto-fill run ID
    if (r.data.run_id) document.getElementById('run-id-input').value = r.data.run_id;
  } else {
    appendChatBubble('system', `❌ Error: ${r.data?.detail || r.status}`);
  }
}

// ═══════════════════════════════════════════════════════════════════
//  HISTORY
// ═══════════════════════════════════════════════════════════════════
async function loadHistory() {
  const sid   = document.getElementById('hist-session-id').value.trim();
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
          <td style="max-width:500px; word-break:break-word">${
            m.tool_name
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
  if (!r.ok || !r.data.items?.length) {
    el.innerHTML = '<div class="empty-state text-muted">Belum ada dokumen</div>';
    return;
  }
  el.innerHTML = `<div class="table-wrap"><table>
    <thead><tr><th>Title</th><th>Source</th><th>Created</th><th>Actions</th></tr></thead>
    <tbody>
      ${r.data.items.map(d => `<tr>
        <td><strong>${escHtml(d.title)}</strong></td>
        <td class="td-mono">${escHtml(d.source || '—')}</td>
        <td class="td-mono">${d.created_at?.slice(0,10)}</td>
        <td><button class="btn btn-danger btn-sm" onclick="deleteDoc('${d.id}')">🗑</button></td>
      </tr>`).join('')}
    </tbody>
  </table></div>`;
}

async function uploadDoc() {
  const agentId = document.getElementById('doc-agent-sel').value;
  if (!agentId) return alert('Pilih agent dulu');
  const payload = {
    title:   document.getElementById('doc-title').value.trim(),
    content: document.getElementById('doc-content').value.trim(),
    source:  document.getElementById('doc-source').value.trim() || null,
  };
  if (!payload.title || !payload.content) return alert('Title dan content wajib diisi');
  const r = await api('POST', `/v1/agents/${agentId}/documents`, payload);
  if (r.ok) { loadDocuments(); document.getElementById('doc-title').value = ''; document.getElementById('doc-content').value = ''; }
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
  const r = await api('POST', `/v1/agents/${agentId}/documents/search`, { query: q, limit: 5 });
  // result shown in log panel
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
  if (r.ok) { loadMemory(); document.getElementById('mem-key').value = ''; document.getElementById('mem-val').value = ''; }
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
    name:        document.getElementById('sk-name').value.trim(),
    description: document.getElementById('sk-desc').value.trim(),
    content_md:  document.getElementById('sk-content').value.trim(),
  };
  if (!payload.name) return alert('Name wajib diisi');
  const r = await api('POST', `/v1/agents/${agentId}/skills`, payload);
  if (r.ok) { loadSkills(); ['sk-name','sk-desc','sk-content'].forEach(id => document.getElementById(id).value = ''); }
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
    name:        document.getElementById('ct-name').value.trim(),
    description: document.getElementById('ct-desc').value.trim(),
    code:        document.getElementById('ct-code').value.trim(),
  };
  if (!payload.name || !payload.code) return alert('Name dan code wajib diisi');
  const r = await api('POST', `/v1/agents/${agentId}/custom-tools`, payload);
  if (r.ok) { loadCustomTools(); ['ct-name','ct-desc','ct-code'].forEach(id => document.getElementById(id).value = ''); }
}

async function deleteCustomTool(agentId, name) {
  await api('DELETE', `/v1/agents/${agentId}/custom-tools/${name}`);
  loadCustomTools();
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
  if (!opPhone) {
    document.getElementById('esc-status-bar').innerHTML =
      '<span class="badge badge-red">⚠ escalation_config.operator_phone belum diset di agent ini</span>';
  } else {
    document.getElementById('esc-status-bar').innerHTML =
      `<span class="badge badge-blue">Operator phone: ${escHtml(opPhone)}</span>`;
  }
}

async function forceEscalation(active) {
  const agentId   = document.getElementById('esc-agent-sel').value;
  const sessionId = document.getElementById('esc-session-id').value.trim();
  if (!agentId || !sessionId) return alert('Pilih agent dan isi session ID dulu');
  const r = await api('PATCH', `/v1/agents/${agentId}/sessions/${sessionId}`, { escalation_active: active });
  if (r.ok) {
    checkEscalationStatus();
    const side = active ? 'op' : 'user';
    escLog(side, 'system', active
      ? '🚨 Eskalasi diaktifkan secara manual (dev mode)'
      : '✅ Eskalasi dinonaktifkan — agent kembali normal');
  }
}

async function checkEscalationStatus() {
  const agentId   = document.getElementById('esc-agent-sel').value;
  const sessionId = document.getElementById('esc-session-id').value.trim();
  if (!agentId || !sessionId) return alert('Pilih agent dan isi session ID');
  const r = await api('GET', `/v1/agents/${agentId}/sessions/${sessionId}`);
  if (!r.ok) return;
  const active = r.data.escalation_active;
  document.getElementById('esc-status-bar').innerHTML =
    `<span class="badge ${active ? 'badge-red' : 'badge-green'}">
      ${active ? '🚨 Eskalasi AKTIF' : '✅ Normal (eskalasi tidak aktif)'}
    </span>
    <span class="text-muted" style="margin-left:8px">channel: ${escHtml(r.data.channel_type || 'none')}</span>`;
}

function escLog(side, role, text) {
  const el = document.getElementById(side === 'user' ? 'esc-user-log' : 'esc-op-log');
  const div = document.createElement('div');
  div.className = `chat-bubble ${role === 'agent' ? 'bubble-agent' : role === 'system' ? 'bubble-system' : 'bubble-user'}`;
  div.innerHTML = `<div class="bubble-label">${escHtml(role === 'agent' ? '🤖 Agent' : role === 'system' ? 'System' : role === 'operator' ? '👨‍💼 Operator' : '👤 User')}</div>${escHtml(text)}`;
  el.appendChild(div);
  el.scrollTop = el.scrollHeight;
}

async function escalationSendUser() {
  const sessionId = document.getElementById('esc-session-id').value.trim();
  const msg       = document.getElementById('esc-user-msg').value.trim();
  if (!sessionId) return alert('Isi Session ID dulu');
  if (!msg) return;

  document.getElementById('esc-user-msg').value = '';
  escLog('user', 'user', msg);

  const r = await api('POST', `/v1/channels/incoming/${sessionId}`, { message: msg });
  if (r.ok) {
    const reply = r.data.reply || '';
    if (reply) {
      escLog('user', 'agent', reply);
      // Jika agent mengeskalasi, tampilkan notif di sisi operator juga
      if (reply.toLowerCase().includes('eskalasi') || reply.toLowerCase().includes('operator')) {
        checkEscalationStatus();
        loadEscalationNotif(sessionId);
      }
    }
  } else {
    escLog('user', 'system', `❌ Error ${r.status}: ${r.data?.detail || ''}`);
  }
}

async function escalationSendOperator() {
  const sessionId  = document.getElementById('esc-session-id').value.trim();
  const opPhone    = document.getElementById('esc-operator-phone').value.trim();
  const msg        = document.getElementById('esc-op-msg').value.trim();
  if (!sessionId) return alert('Isi Session ID dulu');
  if (!opPhone)   return alert('Operator phone belum diset. Set escalation_config di agent.');
  if (!msg) return;

  document.getElementById('esc-op-msg').value = '';
  escLog('op', 'operator', msg);

  // Kirim sebagai operator (from_phone = operator_phone → agent detect sebagai perintah)
  const r = await api('POST', `/v1/channels/incoming/${sessionId}`, {
    message: msg,
    from_phone: opPhone,
  });

  if (r.ok) {
    const reply = r.data.reply || '';
    const messagesToUser = r.data.messages_to_user || [];

    // Tampilkan balasan agent ke operator
    if (reply) escLog('op', 'agent', reply);

    // Tampilkan pesan yang dikirim ke customer di sisi USER
    messagesToUser.forEach(m => {
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

async function loadEscalationNotif(sessionId) {
  // Load history dan tampilkan pesan role=escalation di sisi operator
  const r = await api('GET', `/v1/sessions/${sessionId}/history?limit=50`);
  if (!r.ok) return;
  const escalationMsgs = (r.data.messages || []).filter(m => m.role === 'escalation');
  escalationMsgs.forEach(m => {
    escLog('op', 'system', `📨 Notifikasi agent:\n${m.content}`);
  });
}

// ═══════════════════════════════════════════════════════════════════
//  CHANNELS — Incoming
// ═══════════════════════════════════════════════════════════════════
async function sendIncoming() {
  const sessionId = document.getElementById('ch-session-id').value.trim();
  const fromPhone = document.getElementById('ch-from').value.trim();
  const message   = document.getElementById('ch-message').value.trim();
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
              <td class="td-mono" style="max-width:200px;word-break:break-all">${escHtml(JSON.stringify(s.args)).slice(0,100)}</td>
              <td style="max-width:250px;word-break:break-word">${escHtml((s.result||'').slice(0,150))}</td>
            </tr>`).join('')}
          </tbody>
        </table></div>` : ''}
    </div>`;
}
