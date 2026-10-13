const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'UI-DEV', 'team.js'), 'utf8');
const start = source.indexOf('function chatPresentation(');
const end = source.indexOf('\nfunction chatActionHtml(', start);
const functionSource = source.slice(start, end);

function presentation(content) {
  const context = vm.createContext({});
  vm.runInContext(`${functionSource}; globalThis.result = chatPresentation;`, context);
  return context.result(content);
}

const actionJson = JSON.stringify({
  prompt: 'Pilih hasil prototype',
  options: [{label: 'Website berjalan', value: 'Prototype website'}],
});

for (const header of ['chat-actions', 'chat-actions JSON']) {
  test(`parses a ${header} action fence without exposing its JSON`, () => {
    const result = presentation(`Pilih salah satu.\n\n\`\`\`${header}\n${actionJson}\n\`\`\``);
    assert.equal(result.text, 'Pilih salah satu.');
    assert.equal(result.actions.prompt, 'Pilih hasil prototype');
    assert.deepEqual(JSON.parse(JSON.stringify(result.actions.options)), [
      {label: 'Website berjalan', value: 'Prototype website'},
    ]);
  });
}
