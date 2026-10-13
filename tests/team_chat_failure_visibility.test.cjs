const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '..', 'UI-DEV', 'team.js'), 'utf8');

test('a persisted failed reply remains visible instead of disappearing', () => {
  const functions = ['isInternalFailureNotice', 'messageHtml'].map(name => {
    const start = source.indexOf('function ' + name + '(');
    const end = source.indexOf('\n}', start) + 2;
    return source.slice(start, end);
  }).join('\n');
  const context = vm.createContext({
    state: { actionableMessageIds: new Set() },
    agent: () => ({ id: 'bot', name: 'Admin LMS' }),
    chatPresentation: text => ({ text }),
    renderMessage: text => text,
    avatar: () => '',
    attachmentHtml: () => '',
    esc: text => text,
  });
  vm.runInContext(functions, context);
  const html = vm.runInContext(
    "messageHtml({id:'m1',sender_type:'agent',sender_agent_id:'bot',status:'failed',content:'Belum ada hasil yang dapat ditampilkan untuk bagian ini.'})",
    context,
  );
  assert.match(html, /Proses bot terhenti/);
  assert.match(html, /Tidak ada pekerjaan yang masih berjalan/);
  assert.match(html, /article/);
});
