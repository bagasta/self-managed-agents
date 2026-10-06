const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const qaWorkspace = 'clevio-arthur-ui-enterprise-test';
if (localStorage.getItem('teamModeVersion') !== '4') {
  localStorage.setItem('teamAuthMode', 'owner');
  localStorage.setItem('teamModeVersion', '4');
}
const state = {
  base: location.origin,
  adminKey: localStorage.getItem('apiKey') || '',
  mode: localStorage.getItem('teamAuthMode') || 'owner',
  roster: [], rooms: [], room: null, pendingSends: new Set(),
  renderedRoomId: null, renderedIds: [], renderedVersions: new Map(),
  configAgentId: null, configData: null, configDrafts: new Map(),
};
let pendingRefreshTimer = null;
let activeRefresh = null;
let sendRefreshTimer = null;
let nextSendId = 0;
let mentionRange = null;
const color = id => ['#12a99c','#ea3153','#2583ed','#a47744','#c489db','#d0a842'][[...String(id)].reduce((n,c)=>n+c.charCodeAt(0),0)%6];
const agent = id => state.roster.find(a => a.id === id);
const avatar = (item, group=false) => `<span class="avatar ${group?'group':''}" style="--avatar:${color(item.id)}">${group?'◈':esc((item.name||'?').slice(0,1).toUpperCase())}</span>`;
const label = id => agent(id)?.name || 'Bot';

async function request(method, path, body) {
  const url = new URL(`${state.base.replace(/\/$/,'')}/v1/team-chat${path}`, location.href);
  url.searchParams.set('workspace_id', qaWorkspace);
  url.searchParams.set('owner_view', 'true');
  const headers = {'X-API-Key':state.adminKey};
  if (body) headers['Content-Type'] = 'application/json';
  const response = await fetch(url, {method, headers, body:body?JSON.stringify(body):undefined, cache:'no-store'});
  const data = await response.json().catch(()=>({detail:`HTTP ${response.status}`}));
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail || data));
  return data;
}

async function computerRequest(method, path) {
  const url = new URL(`${state.base.replace(/\/$/,'')}/v1/computer${path}`, location.href);
  const response = await fetch(url, {method, headers:{'X-API-Key':state.adminKey}, cache:'no-store'});
  const data = await response.json().catch(()=>({detail:`HTTP ${response.status}`}));
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail || data));
  return data;
}

function credentialsReady() { return !!state.adminKey; }
function fillSettings() {
  $('admin-key').value = state.adminKey;
  $('auth-mode').value = state.mode;
}
async function load() {
  if (!credentialsReady()) { fillSettings(); $('settings-dialog').showModal(); return; }
  try {
    const roster = await request('GET','/roster');
    const rooms = await request('GET','/rooms');
    state.roster = roster.items || []; state.rooms = rooms.items || [];
    renderRooms(); renderNewDialog();
    if (state.room) {
      state.room = state.rooms.find(r=>r.id===state.room.id) || null;
      if (state.room) await openRoom(state.room.id);
    } else {
      const selected = state.rooms.find(r=>r.id===sessionStorage.getItem('teamSelectedRoomId'));
      if (selected) { await openRoom(selected.id); return; }
      const arthur = state.roster.find(a=>a.is_arthur);
      if (arthur) {
        const existing = state.rooms.find(r=>r.kind==='direct' && r.manager_agent_id===arthur.id);
        if (existing) await openRoom(existing.id);
        else await openDirect(arthur.id);
      }
    }
  } catch (error) { fillSettings(); $('settings-error').textContent = error.message; if (!$('settings-dialog').open) $('settings-dialog').showModal(); }
}

function renderRooms() {
  const search = $('search').value.toLowerCase();
  const rooms = state.rooms.filter(r=>r.title.toLowerCase().includes(search));
  const directIds = new Set(rooms.filter(r=>r.kind==='direct').map(r=>r.manager_agent_id));
  const direct = state.roster.filter(a=>!directIds.has(a.id) && a.name.toLowerCase().includes(search));
  $('rooms').innerHTML = `<div class="room-heading">Bot</div>`+
    rooms.filter(r=>r.kind==='direct').map(roomHtml).join('')+
    direct.map(a=>`<button class="room" data-agent="${a.id}">${avatar(a)}<span class="room-copy"><span class="room-name">${esc(a.name)}${a.is_manager?' <span class="badge">Manager</span>':''}</span><span class="room-preview">Chat personal</span></span></button>`).join('')+
    `<div class="room-heading">Grup</div>`+rooms.filter(r=>r.kind==='group').map(roomHtml).join('')+
    (!state.roster.length?'<p class="muted" style="padding:10px">Belum ada bot di workspace ini.</p>':'');
  $('rooms').onclick=event=>{
    const button=event.target.closest('[data-room],[data-agent]');
    if(!button)return;
    if(button.dataset.room)openRoom(button.dataset.room);
    else if(button.dataset.agent)openDirect(button.dataset.agent);
  };
}

async function refreshSidebar() {
  const [roster, rooms] = await Promise.all([request('GET','/roster'), request('GET','/rooms')]);
  state.roster = roster.items || [];
  state.rooms = rooms.items || [];
  const previousRoom = state.room;
  if (previousRoom) state.room = state.rooms.find(room => room.id === previousRoom.id) || null;
  renderRooms();
  renderNewDialog();
  if (previousRoom && !state.room) {
    sessionStorage.removeItem('teamSelectedRoomId');
    const arthur = state.roster.find(item => item.is_arthur);
    if (arthur) {
      const direct = state.rooms.find(room => room.kind === 'direct' && room.manager_agent_id === arthur.id);
      if (direct) await openRoom(direct.id);
      else await openDirect(arthur.id);
    }
  }
}

function keepPendingSendsVisible() {
  if (sendRefreshTimer) return;
  sendRefreshTimer = setInterval(() => {
    if (state.room) refreshMessages();
  }, 1200);
}
function roomHtml(r) { const a=agent(r.manager_agent_id)||{id:r.manager_agent_id,name:r.title}; return `<button class="room ${state.room?.id===r.id?'active':''}" data-room="${r.id}">${avatar(r.kind==='group'?r:a,r.kind==='group')}<span class="room-copy"><span class="room-name">${esc(r.title)}${r.kind==='direct'&&a.is_manager?' <span class="badge">Manager</span>':''}</span><span class="room-preview">${r.kind==='group'?`${r.member_agent_ids.length} bot · pesan biasa ke ${esc(label(r.manager_agent_id))}`:'Chat personal'}</span></span></button>`; }

async function openDirect(id) {
  const a=agent(id); if(!a)return;
  try { const room=await request('POST','/rooms',{kind:'direct',title:a.name,manager_agent_id:id,member_agent_ids:[]});
    if(!state.rooms.some(r=>r.id===room.id))state.rooms.unshift(room);
    await openRoom(room.id); $('new-dialog').close();
  } catch(e) { $('new-error').textContent=e.message; if (!$('new-dialog').open) $('new-dialog').showModal(); }
}
async function openRoom(id) {
  clearTimeout(pendingRefreshTimer);
  if(state.room?.id!==id){rememberConfigDraft();state.configAgentId=null;state.configData=null}
  state.room=state.rooms.find(r=>r.id===id); if(!state.room)return;
  if(state.renderedRoomId!==id){
    state.renderedRoomId=null;
    state.renderedIds=[];
    state.renderedVersions.clear();
    $('timeline').innerHTML='<div class="day-label">Memuat percakapan…</div>';
  }
  sessionStorage.setItem('teamSelectedRoomId', id);
  $('draft').contentEditable='true';$('send').disabled=false;$('draft').dataset.placeholder=`Message ${state.room.title}`;
  $('chat-title').innerHTML=`${avatar(state.room.kind==='group'?state.room:agent(state.room.manager_agent_id)||state.room,state.room.kind==='group')} ${esc(state.room.title)}`;
  renderRooms(); renderDetails(); $('mention-open').hidden=state.room.kind!=='group';
  $('composer-hint').hidden=state.room.kind!=='group'; $('mention-menu').hidden=true;
  $('details').hidden=true;document.querySelector('.rail').classList.remove('open');
  await refreshMessages();
}
async function refreshMessages() {
  if(!state.room)return;
  const roomId=state.room.id;
  if(activeRefresh?.roomId===roomId)return activeRefresh.promise;
  const promise=(async()=>{
    try {const data=await request('GET',`/rooms/${roomId}/messages`);if(state.room?.id!==roomId)return;
      const timeline=$('timeline');
      const items=data.items||[];
      const ids=items.map(message=>String(message.id));
      const sameRoom=state.renderedRoomId===roomId;
      const appendOnly=sameRoom&&state.renderedIds.every((id,index)=>ids[index]===id);
      const atBottom=!sameRoom||timeline.scrollHeight-timeline.scrollTop-timeline.clientHeight<120;
      const anchor=!atBottom?[...timeline.querySelectorAll('.message')].find(node=>node.getBoundingClientRect().bottom>timeline.getBoundingClientRect().top):null;
      const anchorTop=anchor?.getBoundingClientRect().top;
      const anchorId=anchor?.dataset.messageId;
      if(!appendOnly){
        timeline.innerHTML=`<div class="day-label">Percakapan ${esc(state.room.title)}</div>`+items.map(message=>messageHtml(message)).join('');
        state.renderedVersions.clear();
      }else{
        for(const message of items.slice(0,state.renderedIds.length)){
          const version=`${message.status}\0${message.content}`;
          if(state.renderedVersions.get(String(message.id))!==version){
            const node=[...timeline.querySelectorAll('.message')].find(item=>item.dataset.messageId===String(message.id));
            if(node)node.outerHTML=messageHtml(message,true);
          }
        }
        for(const message of items.slice(state.renderedIds.length))timeline.insertAdjacentHTML('beforeend',messageHtml(message,true));
      }
      state.renderedRoomId=roomId;
      state.renderedIds=ids;
      state.renderedVersions=new Map(items.map(message=>[String(message.id),`${message.status}\0${message.content}`]));
      if(atBottom)timeline.scrollTop=timeline.scrollHeight;
      else if(anchorId){
        const updated=[...timeline.querySelectorAll('.message')].find(node=>node.dataset.messageId===anchorId);
        if(updated)timeline.scrollTop+=updated.getBoundingClientRect().top-anchorTop;
      }
      $('connection-status').hidden=true;
      clearTimeout(pendingRefreshTimer);
      if(items.some(message=>message.status==='working'))pendingRefreshTimer=setTimeout(refreshMessages,1600);
    }catch(e){
      if(state.room?.id!==roomId)return;
      $('connection-status').textContent=`Koneksi terputus: ${e.message}`;
      $('connection-status').hidden=false;
      clearTimeout(pendingRefreshTimer);
      pendingRefreshTimer=setTimeout(refreshMessages,2500);
    }
  })();
  activeRefresh={roomId,promise};
  try{return await promise}finally{if(activeRefresh?.promise===promise)activeRefresh=null}
}
function messageHtml(m,animate=false) {
  const own=m.sender_type==='owner'; const a=agent(m.sender_agent_id)||{id:m.sender_agent_id,name:'Bot'};
  const time=m.created_at?new Date(m.created_at).toLocaleTimeString('id-ID',{hour:'2-digit',minute:'2-digit'}):'';
  const visible=String(m.content||'');
  const body=m.status==='working'
    ?`${visible?renderMessage(visible):''}<div class="typing-indicator" role="status" aria-label="${esc(a.name)} sedang mengetik"><span aria-hidden="true"></span><span aria-hidden="true"></span><span aria-hidden="true"></span></div>`
    :renderMessage(visible);
  return `<article class="message ${own?'owner':''} ${esc(m.status)} ${m.status==='working'&&visible?'has-progress':''} ${animate?'entering':''}" data-message-id="${esc(m.id)}">${own?'':`<div class="sender">${avatar(a)} ${esc(a.name)}</div>`}<div class="bubble">${body}</div><div class="time">${esc(time)}</div></article>`;
}
function renderInlineText(value) {
  let html=esc(value).replace(/`([^`\n]+)`/g,'<code>$1</code>').replace(/\*\*([^*\n]+)\*\*/g,'<strong>$1</strong>').replace(/(^|\W)\*([^*\n]+)\*(?=\W|$)/g,'$1<em>$2</em>');
  const names=[...state.roster.map(a=>a.name),'everyone'].sort((a,b)=>b.length-a.length);
  if(names.length){
    const pattern=new RegExp(`(^|\\s)@(${names.map(name=>name.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')).join('|')})(?=$|[\\s.,!?;:])`,'gi');
    html=html.replace(pattern,(_,before,name)=>`${before}<span class="chat-mention">@${name}</span>`);
  }
  return html;
}
function safeLink(url) {
  try {const parsed=new URL(url);return ['http:','https:'].includes(parsed.protocol)?parsed.href:null}
  catch {return null}
}
function renderInline(value) {
  const source=String(value||'');
  const links=/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)|(https?:\/\/[^\s<>]+)/g;
  let html='';let offset=0;let match;
  while((match=links.exec(source))){
    html+=renderInlineText(source.slice(offset,match.index));
    const raw=match[2]||match[3];
    const trailing=match[3]?(raw.match(/[.,!?;:]+$/)||[''])[0]:'';
    const url=raw.slice(0,raw.length-trailing.length);
    const href=safeLink(url);
    html+=href?`<a href="${esc(href)}" target="_blank" rel="noopener noreferrer">${renderInlineText(match[1]||url)}</a>${esc(trailing)}`:renderInlineText(match[0]);
    offset=links.lastIndex;
  }
  return html+renderInlineText(source.slice(offset));
}
function renderMessage(value) {
  const lines=String(value||'').replace(/\r\n/g,'\n').split('\n');
  const result=[];let list='';
  const close=()=>{if(list){result.push(`</${list}>`);list=''}};
  for(let index=0;index<lines.length;index++){
    const line=lines[index];
    const text=line.trim();
    if(!text){close();result.push('<div class="text-gap"></div>');continue}
    if(text.startsWith('|')&&text.endsWith('|')&&/^\|[\s:|-]+\|$/.test((lines[index+1]||'').trim())){
      close();const cells=row=>row.trim().slice(1,-1).split('|').map(cell=>cell.trim());
      const headers=cells(text).map(cell=>`<th>${renderInline(cell)}</th>`).join('');
      const rows=[];index+=2;
      while(index<lines.length&&lines[index].trim().startsWith('|')&&lines[index].trim().endsWith('|')){
        rows.push(`<tr>${cells(lines[index]).map(cell=>`<td>${renderInline(cell)}</td>`).join('')}</tr>`);index++;
      }
      result.push(`<div class="chat-table-wrap"><table><thead><tr>${headers}</tr></thead><tbody>${rows.join('')}</tbody></table></div>`);
      index--;continue;
    }
    const heading=text.match(/^#{1,3}\s+(.+)$/);
    const bullet=text.match(/^[-*]\s+(.+)$/);
    const numbered=text.match(/^\d+[.)]\s+(.+)$/);
    if(/^---+$/.test(text)){close();result.push('<hr>');continue}
    if(text.startsWith('> ')){close();result.push(`<blockquote>${renderInline(text.slice(2))}</blockquote>`);continue}
    if(heading){close();result.push(`<p class="chat-heading">${renderInline(heading[1])}</p>`);continue}
    if(bullet||numbered){const next=bullet?'ul':'ol';if(list!==next){close();result.push(`<${next}>`);list=next}result.push(`<li>${renderInline((bullet||numbered)[1])}</li>`);continue}
    close();result.push(`<p>${renderInline(text)}</p>`);
  }
  close();return result.join('');
}
function rememberConfigDraft(){
  const form=$('config-form');
  if(!form||form.dataset.dirty!=='true'||!state.configAgentId)return;
  state.configDrafts.set(state.configAgentId,{
    name:$('config-name').value,description:$('config-description').value,
    identity:$('config-identity').value,soul:$('config-soul').value,
    instructions:$('config-instructions').value,model:$('config-model').value,
    temperature:$('config-temperature').value,skills_enabled:$('config-skills-enabled').checked,
    computer_enabled:$('config-computer-enabled').checked,
  });
}
function renderDetails(){
  const room=state.room;if(!room)return;
  const group=room.kind==='group';
  const selected=state.configAgentId&&room.member_agent_ids.includes(state.configAgentId)
    ?state.configAgentId:room.manager_agent_id;
  state.configAgentId=selected;
  const chosen=agent(selected)||{id:selected,name:'Bot'};
  const members=group?`<div class="detail-label">Bot dalam grup</div><div class="detail-members">${room.member_agent_ids.map(id=>{
    const member=agent(id)||{id,name:'Bot'};
    return `<button type="button" class="detail-member ${id===selected?'selected':''}" data-config-agent="${esc(id)}">${avatar(member)}<span>${esc(member.name)}${id===room.manager_agent_id?'<small>Manager</small>':''}</span><span class="member-chevron">›</span></button>`;
  }).join('')}</div>`:'';
  $('details-body').innerHTML=`<div class="detail-hero">${avatar(group?room:chosen,group)}<h3>${esc(group?room.title:chosen.name)}</h3><p>${group?`${room.member_agent_ids.length} bot dalam grup`:'Chat personal'}</p></div>${members}
    <section id="computer-panel" class="computer-panel" aria-live="polite">
      <div class="computer-panel-head"><div><div class="detail-label">Komputer bersama</div><strong>${esc(chosen.name)}</strong></div><span id="computer-state" class="computer-state pending">Memeriksa</span></div>
      <p id="computer-copy" class="computer-copy">Memeriksa akses komputer bot ini…</p>
      <div id="computer-live" class="computer-live" hidden><iframe id="computer-viewer" title="Komputer realtime ${esc(chosen.name)}" referrerpolicy="no-referrer"></iframe><div class="computer-actions"><button type="button" id="computer-open" class="quiet-action">Buka penuh</button><button type="button" id="computer-retry" class="quiet-action">Muat ulang</button></div></div>
    </section>
    <div class="detail-section-head"><div><div class="detail-label">Konfigurasi bot</div><h3>${esc(chosen.name)}</h3></div></div>
    <div id="config-feedback" class="config-feedback" role="status"></div>
    <form id="config-form" class="config-form" data-dirty="false">
      <label>Nama<input id="config-name" maxlength="255" required></label>
      <label>Description<textarea id="config-description" rows="3" maxlength="20000"></textarea></label>
      <label>Identity<span class="field-help">Siapa bot ini dan perannya dalam tim.</span><textarea id="config-identity" rows="4" maxlength="30000"></textarea></label>
      <label>Soul<span class="field-help">Kepribadian dan cara bot berbicara.</span><textarea id="config-soul" rows="4" maxlength="30000"></textarea></label>
      <label>Job / instruksi kerja<span class="field-help">Tugas, batasan, dan cara bekerja bot.</span><textarea id="config-instructions" rows="7" maxlength="200000"></textarea></label>
      <div class="config-pair"><label>Model<input id="config-model" maxlength="255" required></label><label>Temperature<input id="config-temperature" type="number" min="0" max="2" step="0.1" required></label></div>
      <label class="config-check"><input id="config-skills-enabled" type="checkbox">Bot dapat memakai skill</label>
      <label class="config-check"><input id="config-computer-enabled" type="checkbox">Bot dapat memakai komputer bersama</label>
      <p class="field-help computer-setting-help">Saat aktif, bot mendapat tool browser/VNC. Login, OTP, password, dan aksi sensitif tetap harus diambil alih oleh owner atau admin.</p>
      <button id="config-save" class="primary-action" type="submit">Simpan konfigurasi</button>
    </form>
    <div class="detail-section-head skill-heading"><div><div class="detail-label">Skill</div><p>Petunjuk kerja yang bisa dibaca bot saat diperlukan.</p></div><button type="button" id="skill-new" class="quiet-action">Tambah</button></div>
    <div id="config-skills" class="config-skills"></div>
    <form id="skill-editor" class="skill-editor" hidden>
      <label>Nama skill<input id="skill-name" maxlength="255" required></label>
      <label>Deskripsi<input id="skill-description" maxlength="20000" required></label>
      <label>Isi skill<textarea id="skill-content" rows="8" maxlength="100000" required></textarea></label>
      <div class="skill-actions"><button type="button" id="skill-delete" class="danger-action" hidden>Hapus</button><button type="button" id="skill-cancel" class="quiet-action">Batal</button><button id="skill-save" class="primary-action" type="submit">Simpan skill</button></div>
    </form>`;
  if(!$('details').hidden)loadBotConfig(selected);
}
function fillBotConfig(data){
  const draft=state.configDrafts.get(data.id)||data;
  for(const [field,id] of Object.entries({name:'config-name',description:'config-description',identity:'config-identity',soul:'config-soul',instructions:'config-instructions',model:'config-model',temperature:'config-temperature'}))$(id).value=draft[field]??'';
  $('config-skills-enabled').checked=!!draft.skills_enabled;
  $('config-computer-enabled').checked=!!draft.computer_enabled;
  $('config-form').dataset.dirty=state.configDrafts.has(data.id)?'true':'false';
  $('config-feedback').textContent=state.configDrafts.has(data.id)?'Perubahan belum disimpan.':'';
  renderConfigSkills(data.skills||[]);
  loadComputerPanel(data);
}
function setComputerPanel(status, copy, tone='pending'){
  const stateEl=$('computer-state'),copyEl=$('computer-copy');
  if(!stateEl||!copyEl)return;
  stateEl.textContent=status;stateEl.className=`computer-state ${tone}`;copyEl.textContent=copy;
}
async function loadComputerPanel(data){
  const panel=$('computer-panel');if(!panel||data.id!==state.configAgentId)return;
  const live=$('computer-live'),viewer=$('computer-viewer');
  live.hidden=true;viewer.removeAttribute('src');
  if(!data.computer_enabled){
    setComputerPanel('Tidak aktif','Aktifkan akses komputer pada konfigurasi bot, lalu simpan. Preview live akan muncul di sini.','idle');
    return;
  }
  setComputerPanel('Menghubungkan','Meminta akses viewer komputer untuk bot ini…');
  try{
    const takeover=await computerRequest('POST','/takeover');
    if(data.id!==state.configAgentId||$('details').hidden)return;
    viewer.src=takeover.viewer_url;
    live.hidden=false;
    setComputerPanel('Online','Tampilan live dari komputer bersama. Klik di dalam preview untuk mengambil alih saat diperlukan.','ready');
  }catch(error){
    if(data.id!==state.configAgentId||$('details').hidden)return;
    setComputerPanel('Tidak tersedia',error.message,'error');
  }
}
function renderConfigSkills(skills){
  $('config-skills').innerHTML=skills.length?skills.map(skill=>`<div class="skill-card"><div class="skill-card-head"><strong>${esc(skill.name)}</strong>${skill.editable?`<button type="button" class="quiet-action" data-skill-edit="${esc(skill.name)}">Edit</button>`:'<span class="built-in">Bawaan</span>'}</div><p>${esc(skill.description)}</p></div>`).join(''):'<p class="empty-skills">Belum ada skill untuk bot ini.</p>';
}
async function loadBotConfig(id){
  $('config-feedback').textContent='Memuat konfigurasi…';
  try{
    const data=await request('GET',`/agents/${id}/config`);
    if(state.configAgentId!==id||$('details').hidden)return;
    state.configData=data;fillBotConfig(data);
  }catch(error){if(state.configAgentId===id)$('config-feedback').textContent=`Gagal memuat: ${error.message}`}
}
async function saveBotConfig(){
  const id=state.configAgentId;if(!id)return;
  const form=$('config-form');const button=$('config-save');
  const payload={
    name:$('config-name').value.trim(),description:$('config-description').value,
    identity:$('config-identity').value,soul:$('config-soul').value,
    instructions:$('config-instructions').value,model:$('config-model').value.trim(),
    temperature:Number($('config-temperature').value),skills_enabled:$('config-skills-enabled').checked,
    computer_enabled:$('config-computer-enabled').checked,
  };
  button.disabled=true;button.textContent='Menyimpan…';$('config-feedback').textContent='';
  try{
    const oldName=agent(id)?.name;
    const data=await request('PATCH',`/agents/${id}/config`,payload);
    state.configData=data;state.configDrafts.delete(id);form.dataset.dirty='false';
    loadComputerPanel(data);
    const rosterAgent=agent(id);if(rosterAgent){rosterAgent.name=data.name;rosterAgent.description=data.description}
    if(oldName!==data.name){for(const room of state.rooms)if(room.kind==='direct'&&room.manager_agent_id===id&&room.title===oldName)room.title=data.name}
    if(state.room?.kind==='direct'&&state.room.manager_agent_id===id){$('chat-title').innerHTML=`${avatar(rosterAgent||data)} ${esc(data.name)}`;$('draft').dataset.placeholder=`Message ${data.name}`}
    renderRooms();
    const heading=document.querySelector('.detail-section-head h3');if(heading)heading.textContent=data.name;
    if(state.room?.kind==='direct'){const hero=document.querySelector('.detail-hero h3');if(hero)hero.textContent=data.name}
    $('config-feedback').textContent='Konfigurasi tersimpan. Bot akan memakainya pada pesan berikutnya.';
  }catch(error){$('config-feedback').textContent=`Gagal menyimpan: ${error.message}`}
  finally{button.disabled=false;button.textContent='Simpan konfigurasi'}
}
function openSkillEditor(skill){
  $('skill-editor').hidden=false;
  $('skill-name').value=skill?.name||'';$('skill-name').readOnly=!!skill;
  $('skill-description').value=skill?.description||'';
  $('skill-content').value=skill?.content_md||'';
  $('skill-delete').hidden=!skill;
  $('skill-name').focus();
}
async function saveBotSkill(){
  const id=state.configAgentId;const button=$('skill-save');
  button.disabled=true;button.textContent='Menyimpan…';
  try{
    const data=await request('POST',`/agents/${id}/skills`,{
      name:$('skill-name').value.trim(),description:$('skill-description').value.trim(),content_md:$('skill-content').value.trim(),
    });
    state.configData=data;renderConfigSkills(data.skills||[]);$('skill-editor').hidden=true;
    $('config-feedback').textContent='Skill tersimpan.';
  }catch(error){$('config-feedback').textContent=`Gagal menyimpan skill: ${error.message}`}
  finally{button.disabled=false;button.textContent='Simpan skill'}
}
async function deleteBotSkill(){
  const id=state.configAgentId;const name=$('skill-name').value;
  if(!confirm(`Hapus skill “${name}” dari bot ini?`))return;
  const button=$('skill-delete');button.disabled=true;
  try{
    const data=await request('DELETE',`/agents/${id}/skills/${encodeURIComponent(name)}`);
    state.configData=data;renderConfigSkills(data.skills||[]);$('skill-editor').hidden=true;
    $('config-feedback').textContent='Skill dihapus.';
  }catch(error){$('config-feedback').textContent=`Gagal menghapus skill: ${error.message}`}
  finally{button.disabled=false}
}

function mentionOptions(filter='') {
  if(!state.room||state.room.kind!=='group')return;
  const options=[{id:'everyone',name:'everyone'},...state.room.member_agent_ids.map(id=>agent(id)).filter(Boolean)];
  const shown=options.filter(a=>a.name.toLowerCase().includes(filter.toLowerCase()));
  $('mention-menu').innerHTML=shown.map(a=>`<button type="button" data-mention="${a.id}">${a.id==='everyone'?'＠':avatar(a)} ${esc(a.name)}</button>`).join('');
  $('mention-menu').hidden=!shown.length;
  $('mention-menu').querySelectorAll('[data-mention]').forEach(btn=>{btn.onmousedown=event=>event.preventDefault();btn.onclick=()=>selectMention(btn.dataset.mention)});
}
function selectMention(id) {
  const selected=id==='everyone'?{id,name:'everyone'}:agent(id);
  if(!selected||!mentionRange)return;
  const range=mentionRange.cloneRange();range.deleteContents();
  const token=document.createElement('span');token.className='inline-mention';token.contentEditable='false';token.dataset.agentId=id;token.textContent=`@${selected.name}`;
  const space=document.createTextNode(' ');range.insertNode(space);range.insertNode(token);
  const cursor=document.createRange();cursor.setStart(space,1);cursor.collapse(true);
  const selection=window.getSelection();selection.removeAllRanges();selection.addRange(cursor);
  mentionRange=null;$('mention-menu').hidden=true;$('draft').focus();
}
function currentMention() {
  const selection=window.getSelection();const draft=$('draft');
  if(!selection.rangeCount||!draft.contains(selection.anchorNode)||selection.anchorNode.nodeType!==Node.TEXT_NODE)return null;
  const node=selection.anchorNode;const before=node.textContent.slice(0,selection.anchorOffset);
  const match=before.match(/(?:^|\s)@([^\s@]*)$/);
  if(!match)return null;
  const range=document.createRange();range.setStart(node,before.lastIndexOf('@'));range.setEnd(node,selection.anchorOffset);
  return {range,filter:match[1]};
}
function insertAtCaret(text) {
  const draft=$('draft');draft.focus();const selection=window.getSelection();
  const range=selection.rangeCount&&draft.contains(selection.anchorNode)?selection.getRangeAt(0):document.createRange();
  if(!draft.contains(range.startContainer)){range.selectNodeContents(draft);range.collapse(false)}
  range.deleteContents();const node=document.createTextNode(text);range.insertNode(node);range.setStart(node,node.length);range.collapse(true);
  selection.removeAllRanges();selection.addRange(range);draft.dispatchEvent(new Event('input'));
}

function renderNewDialog() {
  $('new-direct').innerHTML=state.roster.map(a=>`<button type="button" class="choice" data-new-direct="${a.id}" style="width:100%;background:none;border:0;text-align:left;color:inherit">${avatar(a)}<span>${esc(a.name)}<small>${esc(a.description||'Chat personal')}</small></span></button>`).join('');
  $('new-direct').querySelectorAll('[data-new-direct]').forEach(btn=>btn.onclick=()=>openDirect(btn.dataset.newDirect));
  const managers=state.roster.filter(a=>a.is_manager);const candidates=managers.length?managers:state.roster;
  $('group-manager').innerHTML=candidates.map(a=>`<option value="${a.id}">${esc(a.name)}</option>`).join('');
  $('group-members').innerHTML=state.roster.map(a=>`<label class="choice"><input type="checkbox" value="${a.id}"><span>${esc(a.name)}</span></label>`).join('');
}

$('new-open').onclick=$('welcome-new').onclick=()=>{renderNewDialog();$('new-dialog').showModal()};
$('new-close').onclick=$('new-cancel').onclick=()=>$('new-dialog').close();
document.querySelectorAll('.tabs button').forEach(btn=>btn.onclick=()=>{document.querySelectorAll('.tabs button').forEach(b=>b.classList.toggle('active',b===btn));const group=btn.dataset.kind==='group';$('new-group').hidden=!group;$('new-direct').hidden=group;$('new-submit').hidden=!group;$('new-error').textContent='';});
$('new-form').onsubmit=async e=>{e.preventDefault();const manager=$('group-manager').value;const members=[...$('group-members').querySelectorAll('input:checked')].map(input=>input.value);try{const room=await request('POST','/rooms',{kind:'group',title:$('group-name').value.trim(),manager_agent_id:manager,member_agent_ids:members});state.rooms.unshift(room);$('new-dialog').close();$('group-name').value='';await openRoom(room.id)}catch(error){$('new-error').textContent=error.message}};
$('search-open').onclick=()=>{$('search-wrap').hidden=!$('search-wrap').hidden;if(!$('search-wrap').hidden)$('search').focus()};$('search').oninput=renderRooms;
$('mention-open').onclick=()=>insertAtCaret('@');
$('draft').oninput=()=>{const found=currentMention();mentionRange=found?.range||null;if(found&&state.room?.kind==='group')mentionOptions(found.filter);else $('mention-menu').hidden=true};
$('draft').onpaste=event=>{event.preventDefault();insertAtCaret(event.clipboardData.getData('text/plain'))};
$('draft').onkeydown=e=>{if(e.key==='Escape')$('mention-menu').hidden=true;if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();const firstMention=!$('mention-menu').hidden&&$('mention-menu').querySelector('[data-mention]');if(firstMention)selectMention(firstMention.dataset.mention);else $('composer').requestSubmit()}};
$('composer').onsubmit=async e=>{e.preventDefault();if(!state.room)return;
  const draft=$('draft');const content=draft.innerText.trim();if(!content)return;
  const tokens=[...draft.querySelectorAll('[data-agent-id]')].map(node=>node.dataset.agentId);
  const everyone=tokens.includes('everyone');const targets=everyone?[]:[...new Set(tokens)];
  const roomId=state.room.id;const draftHtml=draft.innerHTML;
  const sendId=++nextSendId;
  state.pendingSends.add(sendId);
  draft.innerHTML='';$('mention-menu').hidden=true;draft.focus();
  keepPendingSendsVisible();
  try{
    await request('POST',`/rooms/${roomId}/messages`,{content,target_agent_ids:targets,everyone});
    if(state.room?.id===roomId)await refreshMessages();
    try{
      await refreshSidebar();
    }catch(error){
      if(state.room?.id===roomId){
        $('timeline').insertAdjacentHTML('beforeend',`<div class="muted" role="status">Pesan terkirim, tetapi daftar chat belum tersinkron. Coba refresh halaman.</div>`);
      }
    }
  }catch(error){
    if(state.room?.id===roomId){
      if(!draft.innerText.trim())draft.innerHTML=draftHtml;
      $('timeline').insertAdjacentHTML('beforeend',`<div class="error">Pesan belum terkirim: ${esc(error.message)}</div>`);
    }
  }finally{
    state.pendingSends.delete(sendId);
    if(!state.pendingSends.size){clearInterval(sendRefreshTimer);sendRefreshTimer=null}
  }
};
$('details-open').onclick=()=>{if(!state.room)return;const opening=$('details').hidden;$('details').hidden=!opening;if(opening)renderDetails()};
$('details-close').onclick=()=>{rememberConfigDraft();$('details').hidden=true};
$('details-body').onclick=event=>{
  const member=event.target.closest('[data-config-agent]');
  if(member){rememberConfigDraft();state.configAgentId=member.dataset.configAgent;renderDetails();return}
  if(event.target.closest('#skill-new')){openSkillEditor(null);return}
  const edit=event.target.closest('[data-skill-edit]');
  if(edit){const skill=state.configData?.skills?.find(item=>item.name===edit.dataset.skillEdit&&item.editable);if(skill)openSkillEditor(skill);return}
  if(event.target.closest('#computer-retry')){if(state.configData)loadComputerPanel(state.configData);return}
  if(event.target.closest('#computer-open')){const viewer=$('computer-viewer')?.src;if(viewer)window.open(viewer,'_blank','noopener');return}
  if(event.target.closest('#skill-cancel')){$('skill-editor').hidden=true;return}
  if(event.target.closest('#skill-delete')){deleteBotSkill()}
};
$('details-body').oninput=event=>{
  const form=event.target.closest('#config-form');
  if(form){form.dataset.dirty='true';$('config-feedback').textContent='Perubahan belum disimpan.';rememberConfigDraft()}
};
$('details-body').onsubmit=event=>{
  if(event.target.id==='config-form'){event.preventDefault();saveBotConfig()}
  if(event.target.id==='skill-editor'){event.preventDefault();saveBotSkill()}
};
$('mobile-rooms').onclick=()=>document.querySelector('.rail').classList.toggle('open');
$('settings-open').onclick=()=>{fillSettings();$('settings-error').textContent='';$('settings-dialog').showModal()};
$('settings-close').onclick=$('settings-cancel').onclick=()=>$('settings-dialog').close();
$('settings-form').onsubmit=async e=>{e.preventDefault();state.mode=$('auth-mode').value;state.adminKey=$('admin-key').value.trim();if(!credentialsReady()){$('settings-error').textContent='Isi API key.';return}localStorage.setItem('apiKey',state.adminKey);localStorage.setItem('teamAuthMode',state.mode);sessionStorage.removeItem('teamOwnerKey');state.room=null;$('settings-dialog').close();await load()};
load();
