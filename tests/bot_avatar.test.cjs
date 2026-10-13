const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const BotAvatar = require('../UI-DEV/bot-avatar.js');

test('identity is deterministic and independent of display name', () => {
  const a = BotAvatar.render({id:'agent-1',name:'Arthur'});
  const renamed = BotAvatar.render({id:'agent-1',name:'New name'});
  assert.equal(a, BotAvatar.render({id:'agent-1',name:'Arthur'}));
  assert.equal(a.slice(a.indexOf('style=')), renamed.slice(renamed.indexOf('style=')));
  const faces = new Set(Array.from({length:20}, (_,i) => BotAvatar.render({id:`agent-${i}`}).split('<svg')[1]));
  assert.ok(faces.size >= 8, 'Roster has varied silhouettes and accessories');
});

test('untrusted bot names and identifiers cannot inject markup', () => {
  const markup = BotAvatar.render({id:'"><script>alert(1)</script>',name:'"><img src=x onerror=alert(1)>'});
  assert.ok(!markup.includes('<script>'));
  assert.ok(!markup.includes('<img'));
  assert.ok(markup.includes('&lt;img'));
});

test('only working avatars get a working class', () => {
  const bot = {id:'a',name:'Arthur'};
  assert.ok(!BotAvatar.render(bot).includes(' is-working'));
  assert.ok(BotAvatar.render(bot,{working:true}).includes(' is-working'));
  assert.ok(BotAvatar.render(bot,{historical:true}).includes('data-avatar-historical="true"'));
});

function harness() {
  const elements = new Map();
  const nodes = [];
  const element = id => {
    if (!elements.has(id)) elements.set(id, {value:'', hidden:true, dataset:{}, innerHTML:'',
      classList:{toggle(){}}, querySelectorAll:()=>[], showModal(){}, addEventListener(){}});
    return elements.get(id);
  };
  const context = vm.createContext({BotAvatar, console, URL, setTimeout, clearTimeout, setInterval, clearInterval,
    ResizeObserver:class {observe(){}},matchMedia:()=>({matches:true,addEventListener(){}}),
    location:{origin:'http://localhost'},localStorage:{getItem:()=>null,setItem(){}},
    document:{hidden:false,documentElement:element('html'), getElementById:element,
      querySelectorAll:selector=>selector.startsWith('[data-bot-avatar]')?nodes:[],addEventListener(){}}});
  vm.runInContext(fs.readFileSync(require.resolve('../UI-DEV/team.js'),'utf8'), context);
  return {context,nodes};
}

test('status updates synchronize roster avatars without animating historical replies', () => {
  const {context,nodes} = harness();
  const classes = new Set();
  nodes.push({dataset:{botAvatar:'a'},classList:{toggle:(c,on)=>on?classes.add(c):classes.delete(c)}});
  vm.runInContext("state.roster=[{id:'a',name:'Arthur'}]; syncAvatarActivity([{sender_agent_id:'a',status:'working'}]);",context);
  assert.ok(classes.has('is-working'));
  assert.match(nodes[0].title,/Sedang bekerja/);
  const sent = vm.runInContext("messageHtml({id:'m1',sender_type:'agent',sender_agent_id:'a',status:'sent',content:'Done'})",context);
  assert.ok(!sent.includes(' is-working'));
  const working = vm.runInContext("messageHtml({id:'m2',sender_type:'agent',sender_agent_id:'a',status:'working',content:''})",context);
  assert.ok(working.includes(' is-working'));
  assert.ok(working.includes('Sedang bekerja'));
  vm.runInContext('syncAvatarActivity([])',context);
  assert.ok(!classes.has('is-working'));
  assert.equal(nodes[0].title,'Arthur');
});

test('motion has reduced-motion and hidden-tab safeguards', () => {
  const css = fs.readFileSync(require.resolve('../UI-DEV/team.css'),'utf8');
  assert.match(css, /@media\s*\(prefers-reduced-motion:\s*reduce\).*\.bot-avatar/s);
  assert.match(css, /\.avatars-paused \.bot-avatar \*\s*\{\s*animation-play-state:\s*paused/);
  const html = fs.readFileSync(require.resolve('../UI-DEV/team.html'),'utf8');
  assert.ok(html.indexOf('bot-avatar.js') < html.indexOf('src="team.js'));
});
