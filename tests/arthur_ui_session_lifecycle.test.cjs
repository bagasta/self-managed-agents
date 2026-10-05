const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const appJs = fs.readFileSync(path.join(__dirname, '..', 'UI-DEV', 'app.js'), 'utf8');
const idA = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const idB = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

function makeHarness(fetchImpl) {
  const elements = new Map();
  const getElementById = (id) => {
    if (!elements.has(id)) {
      elements.set(id, {
        id, value: '', textContent: '', innerHTML: '', className: '', disabled: false,
        style: {}, children: [], appendChild(child) { this.children.push(child); },
        focus() {}, scrollHeight: 0, scrollTop: 0,
      });
    }
    return elements.get(id);
  };
  const context = vm.createContext({
    console, URLSearchParams, AbortSignal, setTimeout, clearTimeout, setInterval, clearInterval,
    localStorage: { getItem: () => null, setItem() {} },
    window: { location: { hostname: 'example.test', origin: 'https://example.test' }, addEventListener() {} },
    document: { getElementById, createElement: () => ({ className: '', innerHTML: '', textContent: '' }) },
    fetch: fetchImpl,
  });
  vm.runInContext(appJs, context, { filename: 'UI-DEV/app.js' });
  vm.runInContext('logRequest = () => {}; logResponse = () => {};', context);
  vm.runInContext("Arthur.id = 'agent-1'; Arthur.apiKey = 'agent-key';", context);
  return { context, getElementById };
}

test('New Session makes one POST, clears prior state, and gates send until valid success', async () => {
  const create = deferred();
  const requests = [];
  const h = makeHarness(async (url, options) => {
    requests.push({ url, options });
    return create.promise;
  });
  vm.runInContext("Arthur.sessionId = 'old-session';", h.context);
  h.getElementById('arthur-session-badge').style.display = 'inline';
  h.getElementById('arthur-chat-messages').innerHTML = 'old transcript';

  const first = vm.runInContext('arthurNewSession()', h.context);
  const second = vm.runInContext('arthurNewSession()', h.context);
  assert.equal(requests.length, 1);
  assert.equal(requests[0].options.method, 'POST');
  const sessionPayload = JSON.parse(requests[0].options.body);
  assert.equal(sessionPayload.channel_type, 'api');
  assert.equal(sessionPayload.external_user_id, 'clevio-arthur-ui-enterprise-test');
  assert.equal(sessionPayload.metadata.memory_mode, 'isolated');
  assert.equal(sessionPayload.metadata.test_plan, 'enterprise');
  assert.equal(vm.runInContext('Arthur.sessionId', h.context), null);
  assert.equal(h.getElementById('arthur-chat-messages').innerHTML, '');
  assert.equal(h.getElementById('arthur-send-btn').disabled, true);
  assert.equal(h.getElementById('arthur-new-session-btn').disabled, true);

  create.resolve({ ok: true, status: 201, text: async () => JSON.stringify({ id: idA }) });
  await Promise.all([first, second]);
  assert.equal(vm.runInContext('Arthur.sessionId', h.context), idA);
  assert.equal(h.getElementById('arthur-send-btn').disabled, false);
  assert.equal(h.getElementById('arthur-new-session-btn').disabled, false);
});

test('malformed create leaves no sendable session and reports visible failure', async () => {
  let calls = 0;
  const h = makeHarness(async () => {
    calls += 1;
    return { ok: true, status: 201, text: async () => JSON.stringify({ id: 'not-a-uuid' }) };
  });
  vm.runInContext("Arthur.sessionId = 'old-session';", h.context);
  await vm.runInContext('arthurNewSession()', h.context);
  assert.equal(vm.runInContext('Arthur.sessionId', h.context), null);
  assert.equal(h.getElementById('arthur-send-btn').disabled, true);
  assert.match(h.getElementById('arthur-chat-messages').children.at(-1).textContent, /Gagal membuat session baru/);
  h.getElementById('arthur-chat-input').value = 'hello';
  await vm.runInContext('arthurSendMessage()', h.context);
  assert.equal(calls, 1, 'send must not issue a message request after session creation failed');
});

test('session state is invalidated and creation is locked while Arthur itself is loading', async () => {
  const load = deferred();
  let createCalls = 0;
  const h = makeHarness(async () => {
    createCalls += 1;
    return { ok: true, status: 201, text: async () => JSON.stringify({ id: idA }) };
  });
  vm.runInContext("Arthur.id = null; Arthur.sessionId = 'old-session';", h.context);
  h.context.loadPromise = load.promise;
  h.context.loadCalls = 0;
  vm.runInContext("arthurLoad = async () => { loadCalls += 1; await loadPromise; Arthur.id = 'agent-1'; };", h.context);

  const first = vm.runInContext('arthurNewSession()', h.context);
  const second = vm.runInContext('arthurNewSession()', h.context);
  assert.equal(vm.runInContext('Arthur.sessionId', h.context), null);
  assert.equal(h.getElementById('arthur-send-btn').disabled, true);
  assert.equal(h.getElementById('arthur-new-session-btn').disabled, true);
  assert.equal(h.context.loadCalls, 1);
  load.resolve();
  await Promise.all([first, second]);
  assert.equal(createCalls, 1);
  assert.equal(vm.runInContext('Arthur.sessionId', h.context), idA);
});

test('late old-session reply cannot overwrite the new transcript or send target', async () => {
  const oldReply = deferred();
  const requests = [];
  const h = makeHarness(async (url, options) => {
    requests.push({ url, options });
    if (url.endsWith('/messages')) return oldReply.promise;
    return { ok: true, status: 201, text: async () => JSON.stringify({ id: idB }) };
  });
  vm.runInContext(`Arthur.sessionId = '${idA}'; Arthur.sessionGeneration = 1;`, h.context);
  h.getElementById('arthur-chat-input').value = 'first';
  const sendOld = vm.runInContext('arthurSendMessage()', h.context);
  await Promise.resolve();
  assert.match(requests[0].url, new RegExp(idA));

  await vm.runInContext('arthurNewSession()', h.context);
  assert.equal(vm.runInContext('Arthur.sessionId', h.context), idB);
  oldReply.resolve({ ok: true, status: 200, text: async () => JSON.stringify({ reply: 'stale reply' }) });
  await sendOld;
  assert.equal(h.getElementById('arthur-chat-messages').children.some((node) => /stale reply/.test(node.innerHTML || node.textContent)), false);

  h.getElementById('arthur-chat-input').value = 'second';
  const newReply = deferred();
  h.context.fetch = async (url, options) => {
    requests.push({ url, options });
    return newReply.promise;
  };
  const sendNew = vm.runInContext('arthurSendMessage()', h.context);
  assert.match(requests.at(-1).url, new RegExp(idB));
  newReply.resolve({ ok: true, status: 200, text: async () => JSON.stringify({ reply: 'current reply' }) });
  await sendNew;
});

test('Chat/Messages creates the selected Arthur V2 session with isolated Enterprise test markers', async () => {
  const requests = [];
  const h = makeHarness(async (url, options) => {
    requests.push({ url, options });
    return {
      ok: true,
      status: options.method === 'POST' ? 201 : 200,
      text: async () => JSON.stringify(
        options.method === 'POST' ? { id: idA } : { items: [] },
      ),
    };
  });
  vm.runInContext(
    "S.agents = [{ id: 'agent-1', tools_config: { system_plugin: 'arthur_v2' } }]; " +
    "document.getElementById('chat-agent-sel').value = 'agent-1'; " +
    "loadSessionsForChat = async () => {}; loadChatHistory = async () => {};",
    h.context,
  );
  let promptCalls = 0;
  h.context.prompt = () => { promptCalls += 1; return 'must-not-prompt'; };

  await vm.runInContext(
    "createChatSession()",
    h.context,
  );

  assert.equal(promptCalls, 0);
  assert.equal(requests[0].options.method, 'POST');
  const payload = JSON.parse(requests[0].options.body);
  assert.equal(payload.external_user_id, 'clevio-arthur-ui-enterprise-test');
  assert.equal(payload.channel_type, 'api');
  assert.deepEqual(payload.metadata, {
    source: 'arthur-ui', memory_mode: 'isolated', test_plan: 'enterprise',
  });
});

test('Chat/Messages keeps the external-user prompt and ordinary payload for non-Arthur agents', async () => {
  const requests = [];
  const h = makeHarness(async (url, options) => {
    requests.push({ url, options });
    return {
      ok: true,
      status: options.method === 'POST' ? 201 : 200,
      text: async () => JSON.stringify(
        options.method === 'POST' ? { id: idB } : { items: [] },
      ),
    };
  });
  vm.runInContext(
    "S.agents = [{ id: 'agent-2', tools_config: { system_plugin: 'other_plugin' } }]; " +
    "document.getElementById('chat-agent-sel').value = 'agent-2'; " +
    "loadSessionsForChat = async () => {}; loadChatHistory = async () => {};",
    h.context,
  );
  let promptCalls = 0;
  h.context.prompt = (message) => {
    promptCalls += 1;
    assert.equal(message, 'External User ID (kosongkan untuk anonim):');
    return 'operator-entered-user';
  };

  await vm.runInContext(
    "createChatSession()",
    h.context,
  );

  assert.equal(promptCalls, 1);
  assert.equal(requests[0].options.method, 'POST');
  const payload = JSON.parse(requests[0].options.body);
  assert.equal(payload.external_user_id, 'operator-entered-user');
  assert.deepEqual(payload.metadata, {});
  assert.equal('channel_type' in payload, false);
});
