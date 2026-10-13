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
  workingBotIds: new Set(),
  workspaceWorkingBotIds: new Set(), pendingBotCounts: new Map(),
  actionableMessageIds: new Set(), actionMessages: new Map(), pendingActions: new Set(),
  attachments: [],
  drafts: new Map(),
};
let pendingRefreshTimer = null;
let activeRefresh = null;
let sendRefreshTimer = null;
let nextSendId = 0;
let mentionRange = null;
let mentionIndex = 0;
let followingLatest = true;
let computerPreviewTimer = null;
let computerPreviewToken = 0;
let computerViewerUrl = '';
let computerDialogTimer = null;
let computerDialogVersion = 0;
let privateSession = null;
let privateExpiryTimer = null;
let privateTarget = null;
let privateBusy = false;
const color = id => ['#12a99c','#ea3153','#2583ed','#a47744','#c489db','#d0a842'][[...String(id)].reduce((n,c)=>n+c.charCodeAt(0),0)%6];
const agent = id => state.roster.find(a => a.id === id);
const avatar = (item, group=false, historical=false, working=false) => group
  ? '<span class="avatar group" aria-hidden="true"><svg viewBox="0 0 24 24"><circle cx="9" cy="8" r="3"/><path d="M3 20v-2a6 6 0 0 1 12 0v2M16 5a3 3 0 0 1 0 6m2 3a5 5 0 0 1 3 4v2"/></svg></span>'
  : BotAvatar.render(item, {historical, working:historical ? working : state.workingBotIds.has(String(item.id))});

function syncAvatarActivity(items) {
  state.roomWorkingBotIds=new Set(items.filter(m=>m.status==='working'&&m.sender_agent_id).map(m=>String(m.sender_agent_id)));
  applyAvatarActivity();
}
function applyAvatarActivity() {
  state.workingBotIds=new Set([...state.workspaceWorkingBotIds,...(state.roomWorkingBotIds||[]),...state.pendingBotCounts.keys()]);
  document.querySelectorAll('[data-bot-avatar]:not([data-avatar-historical])').forEach(node=>{
    const working=state.workingBotIds.has(node.dataset.botAvatar);
    node.classList.toggle('is-working',working);
    node.title=`${label(node.dataset.botAvatar)}${working?' · Sedang bekerja':''}`;
    node.closest('.room')?.classList.toggle('is-working',working);
  });
}

function setBotsWorking(agentIds, working) {
  for (const id of agentIds) {
    const key=String(id);
    const count=(state.pendingBotCounts.get(key)||0)+(working?1:-1);
    if(count>0)state.pendingBotCounts.set(key,count);else state.pendingBotCounts.delete(key);
  }
  applyAvatarActivity();
}
function acceptRoster(items) {
  state.roster=items;
  state.workspaceWorkingBotIds=new Set(items.filter(item=>item.is_working).map(item=>String(item.id)));
  applyAvatarActivity();
}
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

function teamUrl(path) {
  const url = new URL(`${state.base.replace(/\/$/,'')}/v1/team-chat${path}`, location.href);
  url.searchParams.set('workspace_id', qaWorkspace);
  url.searchParams.set('owner_view', 'true');
  return url;
}

async function uploadAttachment(roomId, file) {
  const url = teamUrl(`/rooms/${roomId}/attachments`);
  const body = new FormData(); body.append('file', file);
  const response = await fetch(url, {method:'POST', headers:{'X-API-Key':state.adminKey}, body});
  const data = await response.json().catch(()=>({detail:`HTTP ${response.status}`}));
  if(!response.ok)throw new Error(typeof data.detail==='string'?data.detail:'Upload lampiran gagal');
  return data;
}

function formatBytes(bytes) {
  if(bytes < 1024)return `${bytes} B`;
  if(bytes < 1024*1024)return `${Math.round(bytes/1024)} KB`;
  return `${(bytes/(1024*1024)).toFixed(1)} MB`;
}

function renderAttachmentDraft() {
  const target=$('attachment-draft');
  target.innerHTML=state.attachments.map(item=>`<div class="attachment-chip ${item.error?'has-error':''}"><span class="attachment-type" aria-hidden="true">${item.is_image?'Gambar':'File'}</span><span class="attachment-name">${esc(item.filename)}</span><span class="attachment-size">${item.uploading?'Mengunggah…':item.error?esc(item.error):formatBytes(item.size_bytes||0)}</span><button type="button" data-remove-attachment="${esc(item.localId)}" aria-label="Hapus lampiran ${esc(item.filename)}">×</button></div>`).join('');
  updateComposer();
}

function rememberDraft() {
  if(state.room)state.drafts.set(state.room.id,{html:$('draft').innerHTML,attachments:state.attachments});
}
function updateComposer() {
  const hasContent=!!$('draft').innerText.trim()||state.attachments.some(item=>item.id);
  $('send').disabled=!state.room||!hasContent||state.attachments.some(item=>item.uploading||item.error);
  rememberDraft();
}
function closeMentions() {
  $('mention-menu').hidden=true;
  $('draft').removeAttribute('aria-activedescendant');
}
function setRail(open, returnFocus=false) {
  $('rail').classList.toggle('open',open);
  $('rail-backdrop').hidden=!open;
  $('mobile-rooms').setAttribute('aria-expanded',String(open));
  if(open)$('rail-close').focus();
  else if(returnFocus)$('mobile-rooms').focus();
}
function setCreateMenu(open) {
  $('create-menu').hidden=!open;
  $('new-open').setAttribute('aria-expanded',String(open));
  if(open)$('create-direct').focus();
}
function setDetails(open, returnFocus=false) {
  if(open&&!state.room)return;
  $('details').hidden=!open;
  $('details-open').setAttribute('aria-expanded',String(open));
  $('chat-title').setAttribute('aria-expanded',String(open));
  if(open){renderDetails();$('details-close').focus()}
  else {rememberConfigDraft();clearTimeout(computerPreviewTimer);computerPreviewToken++;if(returnFocus)$('details-open').focus()}
}
function updateLatestButton() {
  const timeline=$('timeline');
  followingLatest=timeline.scrollHeight-timeline.scrollTop-timeline.clientHeight<120;
  $('latest-message').hidden=!state.room||followingLatest;
}

function attachmentHtml(item) {
  const download=esc(item.download_url || '');
  const label=item.is_image?'Gambar':'File';
  return `<div class="message-attachments"><button type="button" class="message-attachment ${item.is_image?'is-image':''}" data-attachment-download="${download}" data-attachment-name="${esc(item.filename)}" data-attachment-type="${esc(item.content_type)}"><span class="message-attachment-icon" aria-hidden="true">${item.is_image?'Gambar':'File'}</span><span class="message-attachment-copy"><strong>${esc(item.filename)}</strong><small>${label} · ${formatBytes(item.size_bytes||0)}</small></span><span class="message-attachment-action">Buka</span></button></div>`;
}

async function downloadAttachment(path, filename, contentType) {
  const response=await fetch(teamUrl(path),{headers:{'X-API-Key':state.adminKey}});
  if(!response.ok)throw new Error('Lampiran tidak bisa dibuka');
  const blob=await response.blob();
  const href=URL.createObjectURL(new Blob([blob],{type:contentType||blob.type}));
  const link=document.createElement('a');link.href=href;link.download=filename;link.click();
  setTimeout(()=>URL.revokeObjectURL(href),1000);
}

async function computerRequest(method, path, body) {
  const url = new URL(`${state.base.replace(/\/$/,'')}/v1/computer${path}`, location.href);
  const response = await fetch(url, {method, headers:{'X-API-Key':state.adminKey,...(body?{'Content-Type':'application/json'}:{})}, body:body?JSON.stringify(body):undefined, cache:'no-store'});
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
    acceptRoster(roster.items || []); state.rooms = rooms.items || [];
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
  const search = $('search').value.trim().toLowerCase();
  const mainBot = state.roster.find(a=>a.is_arthur);
  const mainRoom = mainBot && state.rooms.find(r=>r.kind==='direct' && r.manager_agent_id===mainBot.id);
  const rooms = state.rooms.filter(r=>!(mainBot && r.kind==='direct' && r.manager_agent_id===mainBot.id) && r.title.toLowerCase().includes(search));
  const directIds = new Set(state.rooms.filter(r=>r.kind==='direct').map(r=>r.manager_agent_id));
  const direct = state.roster.filter(a=>!a.is_arthur && !directIds.has(a.id) && a.name.toLowerCase().includes(search));
  const botCount=rooms.filter(r=>r.kind==='direct').length+direct.length;
  const groupCount=rooms.filter(r=>r.kind==='group').length;
  const mainHtml = mainBot ? `<div class="main-bot-pin">${mainRoom ? roomHtml(mainRoom) : `<button class="room room-main" data-agent="${esc(mainBot.id)}">${avatar(mainBot)}<span class="room-copy"><span class="main-bot-label">Bot utama</span><span class="room-name">${esc(mainBot.name)}</span><span class="room-preview">${esc(mainBot.description||'Koordinator tim kamu')}</span></span></button>`}</div>` : '';
  $('rooms').innerHTML = mainHtml+(botCount?`<div class="room-heading">Bot <span>${botCount}</span></div>`:'')+
    rooms.filter(r=>r.kind==='direct').map(roomHtml).join('')+
    direct.map(a=>`<button class="room" data-agent="${esc(a.id)}">${avatar(a)}<span class="room-copy"><span class="room-name">${esc(a.name)}</span><span class="room-preview">${esc(a.description||'Chat personal')}</span></span></button>`).join('')+
    (groupCount?`<div class="room-heading">Grup <span>${groupCount}</span></div>`:'')+rooms.filter(r=>r.kind==='group').map(roomHtml).join('')+
    (!botCount&&!groupCount&&(search||!mainBot)?`<p class="empty-rooms" role="status">${search?'Tidak ada percakapan lain yang cocok. Coba nama lain.':'Belum ada percakapan. Pilih + untuk mulai.'}</p>`:'')+
    (!search&&!groupCount&&botCount?'<div class="room-heading">Grup</div><p class="empty-rooms">Buat grup lewat + untuk bekerja bersama beberapa bot.</p>':'');
  $('rooms').onclick=event=>{
    const button=event.target.closest('[data-room],[data-agent]');
    if(!button)return;
    if(button.dataset.room)openRoom(button.dataset.room);
    else if(button.dataset.agent)openDirect(button.dataset.agent);
  };
  applyAvatarActivity();
}

async function refreshSidebar() {
  const [roster, rooms] = await Promise.all([request('GET','/roster'), request('GET','/rooms')]);
  acceptRoster(roster.items || []);
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
function roomHtml(r) {
  const a=agent(r.manager_agent_id)||{id:r.manager_agent_id,name:r.title};
  const active=state.room?.id===r.id;
  const main=r.kind==='direct' && a.is_arthur;
  const preview=r.kind==='group'?`${r.member_agent_ids.length} bot · ${label(r.manager_agent_id)}`:(a.description||'Chat personal');
  return `<button class="room ${main?'room-main ':''}${active?'active':''}" data-room="${esc(r.id)}" ${active?'aria-current="page"':''}>${avatar(r.kind==='group'?r:a,r.kind==='group')}<span class="room-copy">${main?'<span class="main-bot-label">Bot utama</span>':''}<span class="room-name">${esc(main?a.name:r.title)}</span><span class="room-preview">${esc(preview)}</span></span></button>`;
}

async function openDirect(id) {
  const a=agent(id); if(!a)return;
  try { const room=await request('POST','/rooms',{kind:'direct',title:a.name,manager_agent_id:id,member_agent_ids:[]});
    if(!state.rooms.some(r=>r.id===room.id))state.rooms.unshift(room);
    await openRoom(room.id); $('new-dialog').close();
  } catch(e) { $('new-error').textContent=e.message; if (!$('new-dialog').open) $('new-dialog').showModal(); }
}
async function openRoom(id) {
  const room=state.rooms.find(r=>r.id===id);if(!room)return;
  clearTimeout(pendingRefreshTimer);
  syncAvatarActivity([]);
  if(state.room?.id!==id){
    rememberDraft();rememberConfigDraft();state.configAgentId=null;state.configData=null;
    state.room=room;
    const saved=state.drafts.get(id);
    $('draft').innerHTML=saved?.html||'';state.attachments=saved?.attachments||[];renderAttachmentDraft();
  }else state.room=room;
  if(state.renderedRoomId!==id){
    followingLatest=true;
    state.renderedRoomId=null;
    state.renderedIds=[];
    state.renderedVersions.clear();
    $('timeline').innerHTML='<div class="day-label">Memuat percakapan…</div>';
  }
  sessionStorage.setItem('teamSelectedRoomId', id);
  $('draft').contentEditable='true';$('attachment-open').disabled=false;$('draft').dataset.placeholder=`Pesan untuk ${state.room.title}…`;
  $('chat-title').innerHTML=`${avatar(state.room.kind==='group'?state.room:agent(state.room.manager_agent_id)||state.room,state.room.kind==='group')}<span class="title-name">${esc(state.room.title)}</span><span class="title-chevron" aria-hidden="true">⌄</span>`;
  $('chat-title').disabled=false;$('details-open').disabled=false;
  $('computer-launch').disabled=false;
  $('composer-context').textContent=state.room.kind==='group'?`@ pilih bot · ${label(state.room.manager_agent_id)} menerima pesan biasa`:'Chat personal';
  renderRooms();$('mention-open').hidden=state.room.kind!=='group';
  closeMentions();setDetails(false);setRail(false);setCreateMenu(false);updateComposer();
  if(matchMedia('(min-width: 761px)').matches)$('draft').focus();
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
      const lastOwner=items.findLastIndex(message=>message.sender_type==='owner');
      state.actionableMessageIds=new Set(items.slice(lastOwner+1).filter(message=>message.sender_type==='agent'&&message.status!=='working'&&message.status!=='failed').map(message=>String(message.id)));
      state.actionMessages=new Map(items.map(message=>[String(message.id),message]));
      const ids=items.map(message=>String(message.id));
      const sameRoom=state.renderedRoomId===roomId;
      const appendOnly=sameRoom&&state.renderedIds.every((id,index)=>ids[index]===id);
      const atBottom=!sameRoom||timeline.scrollHeight-timeline.scrollTop-timeline.clientHeight<120;
      const anchor=!atBottom?[...timeline.querySelectorAll('.message')].find(node=>node.getBoundingClientRect().bottom>timeline.getBoundingClientRect().top):null;
      const anchorTop=anchor?.getBoundingClientRect().top;
      const anchorId=anchor?.dataset.messageId;
      if(!appendOnly){
        timeline.innerHTML=items.length?`<div class="day-label">Percakapan ${esc(state.room.title)}</div>`+items.map(message=>messageHtml(message)).join(''):`<div class="welcome"><span class="welcome-eyebrow">${state.room.kind==='group'?'Percakapan grup':'Chat personal'}</span><h1>${esc(state.room.title)}</h1><p>${state.room.kind==='group'?'Sampaikan tugas ke tim. Gunakan @ untuk memilih bot yang dituju.':'Apa yang ingin kamu kerjakan bersama?'}</p></div>`;
        state.renderedVersions.clear();
      }else{
        if(items.length)timeline.querySelector('.welcome')?.remove();
        for(const message of items.slice(0,state.renderedIds.length)){
          const version=messageVersion(message);
          if(state.renderedVersions.get(String(message.id))!==version){
            const node=[...timeline.querySelectorAll('.message')].find(item=>item.dataset.messageId===String(message.id));
            if(node)node.outerHTML=messageHtml(message,true);
          }
        }
        for(const message of items.slice(state.renderedIds.length))timeline.insertAdjacentHTML('beforeend',messageHtml(message,true));
      }
      state.renderedRoomId=roomId;
      state.renderedIds=ids;
      state.renderedVersions=new Map(items.map(message=>[String(message.id),messageVersion(message)]));
      syncAvatarActivity(items);
      $('timeline').onclick=event=>{
        const choice=event.target.closest('[data-chat-choice]');
        if(choice){sendChatChoice(choice);return}
        if(event.target.closest('[data-private-input]')){openComputerDialog(true);return}
        const button=event.target.closest('[data-attachment-download]');
        if(!button)return;
        downloadAttachment(button.dataset.attachmentDownload,button.dataset.attachmentName,button.dataset.attachmentType)
          .catch(error=>{$('connection-status').textContent=error.message;$('connection-status').hidden=false});
      };
      if(atBottom)timeline.scrollTop=timeline.scrollHeight;
      else if(anchorId){
        const updated=[...timeline.querySelectorAll('.message')].find(node=>node.dataset.messageId===anchorId);
        if(updated)timeline.scrollTop+=updated.getBoundingClientRect().top-anchorTop;
      }
      updateLatestButton();
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
function isInternalFailureNotice(message) {
  const content=String(message.content||'').trim();
  return message.status==='failed' && (
    /^Pekerjaan ini tertahan sebelum runtime mencatat kode penyebab\./.test(content) ||
    /^Pekerjaan ini tertahan \(kode:/.test(content) ||
    content==='Proses bot berhenti sebelum hasil tersedia. Arthur dapat menindaklanjuti catatan runtime-nya.' ||
    content==='Belum ada hasil yang dapat ditampilkan untuk bagian ini.'
  );
}
function messageHtml(m,animate=false) {
  if(isInternalFailureNotice(m))m={...m,content:'Proses bot terhenti sebelum hasil selesai. Tidak ada pekerjaan yang masih berjalan dari pesan ini.'};
  const own=m.sender_type==='owner'; const a=agent(m.sender_agent_id)||{id:m.sender_agent_id,name:'Bot'};
  const time=m.created_at?new Date(m.created_at).toLocaleTimeString('id-ID',{hour:'2-digit',minute:'2-digit'}):'';
  const presentation=chatPresentation(m.content);
  const visible=presentation.text;
  const body=m.status==='working'
    ?`${visible?renderMessage(visible):''}<div class="typing-indicator" role="status" aria-label="${esc(a.name)} sedang mengetik"><span aria-hidden="true"></span><span aria-hidden="true"></span><span aria-hidden="true"></span></div>`
    :renderMessage(visible);
  const attachments=(m.attachments||[]).map(attachmentHtml).join('');
  const actions=!own&&state.actionableMessageIds.has(String(m.id))?chatActionHtml(m,presentation.actions):'';
  return `<article class="message ${own?'owner':''} ${esc(m.status)} ${m.status==='working'&&visible?'has-progress':''} ${animate?'entering':''}" data-message-id="${esc(m.id)}">${own?'':`<div class="sender">${avatar(a,false,true,m.status==='working')} ${esc(a.name)}${m.status==='working'?'<span class="bot-work-label">Sedang bekerja</span>':''}</div>`}<div class="bubble">${body}${attachments}</div>${actions}<div class="time">${esc(time)}</div></article>`;
}
function messageVersion(message) {
  return `${message.status}\0${message.content}\0${state.actionableMessageIds.has(String(message.id))}`;
}
function chatPresentation(content) {
  const text=String(content||'');
  // Models commonly add the conventional `JSON` info string after the
  // application-specific fence name. Accept it so a valid action card is
  // never downgraded to the fragile quoted-text fallback.
  const block=text.match(/```chat-actions(?:\s+json)?\s*\n([\s\S]*?)\n```/i);
  if(block&&block[1].length<=8000){
    try{
      const raw=JSON.parse(block[1]);
      if(raw&&typeof raw==='object'){
        const options=Array.isArray(raw.options)?raw.options.filter(option=>option&&typeof option.label==='string'&&typeof option.value==='string'&&option.label.trim()&&option.value.trim()).slice(0,4).map(option=>({label:option.label.slice(0,60),value:option.value.slice(0,1000)})):[];
        if(raw.kind==='private_input'||options.length)return {text:text.replace(block[0],'').trim(),actions:{kind:raw.kind==='private_input'?'private_input':'reply',prompt:typeof raw.prompt==='string'?raw.prompt.slice(0,180):'Pilih jawaban',options,detailLabel:typeof raw.detail_label==='string'?raw.detail_label.slice(0,120):''}};
      }
    }catch{}
  }
  const offered=text.match(/(?:balas|jawab|klik|pilih)\s*(?:dengan\s*)?[*_]*["“]([^"”\n]{1,60})["”]/i);
  const sensitive=/\b(password|kata sandi|otp|kode verifikasi|api key|access token)\b/i.test(text)&&/\b(login|masuk|isi|mengisi|memasukkan|takeover|olehmu|sendiri|diperlukan)\b/i.test(text);
  if(offered)return {text,actions:{kind:'reply',prompt:'Kirim jawaban ke bot',options:[{label:offered[1],value:offered[1]},{label:'Ubah dulu',value:'Saya ingin mengubah usulnya dulu.'}],detailLabel:'Detail tambahan (opsional)',privateInput:sensitive}};
  if(sensitive)return {text,actions:{kind:'private_input',prompt:'Lanjutkan login di komputer'}};
  return {text,actions:null};
}
function chatActionHtml(message,actions) {
  if(!actions)return '';
  const id=String(message.id),pending=state.pendingActions.has(id);
  const privateButton='<button type="button" data-private-input>Isi data pribadi di komputer</button>';
  return `<section class="chat-action-card" data-action-message="${esc(id)}" aria-label="Pilihan respons"><p class="chat-action-prompt">${esc(actions.prompt)}</p>${actions.kind==='private_input'?`<p class="muted">Password dan kode diisi lewat formulir terpisah, tanpa dikirim sebagai pesan.</p><div class="chat-action-options">${privateButton}</div>`:`<div class="chat-action-options">${actions.options.map((option,index)=>`<button type="button" data-chat-choice="${index}" ${pending?'disabled':''}>${esc(option.label)}</button>`).join('')}${actions.privateInput?privateButton:''}</div>${actions.detailLabel?`<label for="action-detail-${esc(id)}">${esc(actions.detailLabel)}</label><textarea id="action-detail-${esc(id)}" class="chat-action-detail" maxlength="2000" rows="2" ${pending?'disabled':''}></textarea>`:''}`}<p class="chat-action-feedback" role="status">${pending?'Mengirim jawaban…':''}</p></section>`;
}
async function sendChatChoice(button) {
  const card=button.closest('[data-action-message]');const id=card?.dataset.actionMessage;
  const message=state.actionMessages.get(id);
  if(!message||!state.room||state.pendingActions.has(id)||!state.actionableMessageIds.has(id))return;
  const choice=chatPresentation(message.content).actions?.options?.[Number(button.dataset.chatChoice)];
  if(!choice)return;
  const detail=card.querySelector('textarea')?.value.trim()||'';
  const question=chatPresentation(message.content).actions?.prompt||'';
  const content=`Menjawab pertanyaan bot (pesan ${id}): ${question}\nPilihan: ${choice.value}`+(detail?`\nDetail tambahan: ${detail}`:'');const roomId=state.room.id;
  const targets=state.room.kind==='group'&&message.sender_agent_id?[message.sender_agent_id]:[];
  const sendId=++nextSendId;
  state.pendingActions.add(id);state.pendingSends.add(sendId);setBotsWorking(targets,true);keepPendingSendsVisible();
  card.querySelectorAll('button,textarea').forEach(control=>control.disabled=true);
  card.querySelector('[role=status]').textContent='Mengirim jawaban…';
  try{
    await request('POST',`/rooms/${roomId}/messages`,{content,target_agent_ids:targets,client_message_id:crypto.randomUUID()});
    if(state.room?.id===roomId)await refreshMessages();
  }catch(error){
    card.querySelectorAll('button,textarea').forEach(control=>control.disabled=false);
    card.querySelector('[role=status]').textContent=`Belum terkirim: ${error.message}`;
  }finally{
    state.pendingActions.delete(id);state.pendingSends.delete(sendId);setBotsWorking(targets,false);
    if(!state.pendingSends.size){clearInterval(sendRefreshTimer);sendRefreshTimer=null}
  }
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
  clearTimeout(computerPreviewTimer);
  computerPreviewToken++;
  computerViewerUrl='';
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
      <div class="computer-panel-head"><div><div class="detail-label">Komputer bersama</div><strong id="computer-current-user">Komputer tim</strong></div><span id="computer-state" class="computer-state pending">Memeriksa</span></div>
      <p id="computer-copy" class="computer-copy">Memeriksa akses komputer bot ini…</p>
      <div id="computer-live" class="computer-live" hidden><button type="button" id="computer-expand" class="computer-preview-button" aria-label="Perbesar tampilan komputer"><img id="computer-preview" alt="Tampilan terkini komputer bersama ${esc(chosen.name)}"><span class="computer-preview-caption">Lihat komputer<span aria-hidden="true">⤢</span></span></button><div class="computer-actions"><button type="button" id="computer-open" class="quiet-action">Kendali langsung ↗</button><button type="button" id="computer-retry" class="quiet-action">Perbarui</button><button type="button" id="computer-sensitive" class="quiet-action computer-private-shortcut">Isi data pribadi</button></div></div>
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
      <label class="config-check"><input id="config-computer-enabled" type="checkbox">Bot ini boleh memakai komputer bersama</label>
      <p class="field-help computer-setting-help">Berlaku di chat personal dan grup. Satu komputer dipakai bergantian; mengaktifkan bot ini tidak mengaktifkan bot lain.</p>
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
  if($('computer-dialog').open)return;
  const panel=$('computer-panel');if(!panel||data.id!==state.configAgentId)return;
  const live=$('computer-live'),preview=$('computer-preview');
  const token=++computerPreviewToken;
  clearTimeout(computerPreviewTimer);
  computerViewerUrl='';
  live.hidden=true;preview.removeAttribute('src');
  if(!data.computer_enabled){
    setComputerPanel('Akses nonaktif',`${data.name} belum diberi akses komputer. Pengaturan ini berlaku untuk bot ini di chat personal maupun grup.`,'idle');
    return;
  }
  setComputerPanel('Menghubungkan','Meminta akses viewer komputer untuk bot ini…');
  try{
    const takeover=await computerRequest('POST','/takeover');
    if(token!==computerPreviewToken||data.id!==state.configAgentId||$('details').hidden)return;
    computerViewerUrl=takeover.viewer_url;
    const refresh=async()=>{
      if($('computer-dialog').open)return;
      try{
        const shared=await computerRequest('GET','/status');
        const screen=await computerRequest('GET','/snapshot');
        if(token!==computerPreviewToken||$('details').hidden||$('computer-dialog').open)return;
        if(!screen.image_base64||!String(screen.mime_type||'').startsWith('image/'))throw new Error('Gambar komputer tidak tersedia.');
        preview.src=`data:${screen.mime_type};base64,${screen.image_base64}`;
        live.hidden=false;
        const holder=shared.turn?.agent_name;
        $('computer-current-user').textContent=holder||'Tersedia untuk tim';
        const queue=shared.turn?.waiting_count||0;
        setComputerPanel(holder?'Sedang dipakai':'Online',holder?`${holder} sedang memakai komputer.${queue?` ${queue} bot menunggu giliran.`:''} ${data.name} tetap memiliki akses dari grup maupun chat personal.`:'Satu komputer untuk tim, dipakai bergantian. Klik preview untuk memperbesar.','ready');
        computerPreviewTimer=setTimeout(refresh,3000);
      }catch(error){
        if(token!==computerPreviewToken||$('details').hidden)return;
        setComputerPanel('Tidak tersedia',error.message,'error');
      }
    };
    await refresh();
  }catch(error){
    if(data.id!==state.configAgentId||$('details').hidden)return;
    setComputerPanel('Tidak tersedia',error.message,'error');
  }
}
function renderConfigSkills(skills){
  $('config-skills').innerHTML=skills.length?skills.map(skill=>`<div class="skill-card"><div class="skill-card-head"><strong>${esc(skill.name)}</strong>${skill.editable?`<button type="button" class="quiet-action" data-skill-edit="${esc(skill.name)}">Edit</button>`:'<span class="built-in">Bawaan</span>'}</div><p>${esc(skill.description)}</p></div>`).join(''):'<p class="empty-skills">Belum ada skill untuk bot ini.</p>';
}

async function refreshComputerDialog() {
  const version=computerDialogVersion;
  clearTimeout(computerDialogTimer);
  if(!$('computer-dialog').open||privateBusy)return;
  $('computer-dialog-status').textContent='Memperbarui tampilan…';
  privateTarget=null;$('computer-target').hidden=true;updatePrivateSubmit();
  $('private-target-label').textContent='1. Klik kolom tujuan pada gambar komputer.';
  try{
    const shared=await computerRequest('GET','/status');
    const screen=await computerRequest('GET','/snapshot');
    if(version!==computerDialogVersion||!$('computer-dialog').open)return;
    if(screen.mime_type!=='image/png'||!screen.image_base64)throw new Error('Tampilan komputer belum tersedia.');
    if(!screen.image_base64||!String(screen.mime_type||'').startsWith('image/'))throw new Error('Gambar komputer tidak tersedia.');
    $('computer-large-preview').src=`data:${screen.mime_type};base64,${screen.image_base64}`;
    $('computer-dialog-status').textContent=privateSession?'Akses bot dijeda. Pilih kolom pada gambar untuk mengisinya.':shared.turn?.agent_name?`Dipakai ${shared.turn.agent_name}${shared.turn.waiting_count?` · ${shared.turn.waiting_count} bot menunggu`:''}.`:'Komputer tersedia untuk giliran berikutnya.';
  }catch(error){
    if(version!==computerDialogVersion||!$('computer-dialog').open)return;
    $('computer-dialog-status').textContent=error.message;
    $('computer-large-preview').removeAttribute('src');
  }finally{
    if(version===computerDialogVersion&&$('computer-dialog').open&&!privateSession)computerDialogTimer=setTimeout(refreshComputerDialog,3000);
  }
}
async function openComputerDialog(withPrivateInput=false) {
  if($('computer-dialog').open)return;
  clearTimeout(computerPreviewTimer);computerPreviewToken++;
  computerDialogVersion++;
  $('computer-dialog').showModal();
  $('computer-dialog-status').textContent='Menghubungkan komputer…';
  $('computer-dialog-control').disabled=true;
  const version=computerDialogVersion;
  try{
    const takeover=await computerRequest('POST','/takeover');
    if(version!==computerDialogVersion||!$('computer-dialog').open)return;
    computerViewerUrl=takeover.viewer_url;
    $('computer-dialog-control').disabled=!computerViewerUrl;
  }catch(error){$('computer-dialog-status').textContent=error.message}
  if(version!==computerDialogVersion||!$('computer-dialog').open)return;
  if(withPrivateInput)await beginPrivateInput();else await refreshComputerDialog();
}
function updatePrivateSubmit() {
  $('computer-private-submit').disabled=privateBusy||!privateSession||!privateTarget||!$('computer-private-value').value;
}
function resetPrivateForm() {
  clearTimeout(privateExpiryTimer);privateSession=null;privateTarget=null;
  $('computer-private-value').value='';$('computer-private-form').hidden=true;
  $('computer-private-open').hidden=false;$('computer-target').hidden=true;
  $('computer-dialog').classList.remove('has-private-input');
  $('private-target-label').textContent='1. Klik kolom tujuan pada gambar komputer.';
  $('computer-target-help').textContent='Buka kendali langsung untuk mengoperasikan komputer.';
  updatePrivateSubmit();
}
function privateControlsBusy(busy) {
  privateBusy=busy;
  for(const id of ['computer-dialog-close','computer-dialog-refresh','computer-dialog-control','computer-private-open','computer-private-done','computer-private-value','computer-private-replace'])$(id).disabled=busy;
  if(!busy)$('computer-dialog-control').disabled=!computerViewerUrl;
  updatePrivateSubmit();
}
async function beginPrivateInput() {
  if(privateBusy||privateSession)return;
  if(!window.isSecureContext){$('computer-dialog-status').textContent='Gunakan HTTPS atau localhost untuk mengisi data pribadi.';return}
  clearTimeout(computerDialogTimer);computerDialogVersion++;
  $('computer-dialog-status').textContent='Menyiapkan sesi input pribadi…';
  privateControlsBusy(true);
  try{
    privateSession=await computerRequest('POST','/input-session',{});
    $('computer-private-form').hidden=false;$('computer-private-open').hidden=true;
    $('computer-dialog').classList.add('has-private-input');
    $('computer-private-status').textContent='';
    $('computer-target-help').textContent='Klik kolom tujuan. Dengan keyboard: fokus gambar, gunakan panah, lalu Enter untuk memilih.';
    privateExpiryTimer=setTimeout(()=>{
      if(privateBusy)return;
      resetPrivateForm();$('computer-large-preview').removeAttribute('src');
      $('computer-dialog-status').textContent='Sesi lima menit berakhir. Buka Input pribadi lagi untuk melanjutkan.';
    },Math.max(0,privateSession.expires_at*1000-Date.now()));
  }catch(error){$('computer-dialog-status').textContent=error.message}
  finally{privateControlsBusy(false)}
  if(privateSession)await refreshComputerDialog();
}
function choosePrivateTarget(x,y) {
  const preview=$('computer-large-preview');
  if(!privateSession||privateBusy||!preview.naturalWidth||!preview.hasAttribute('src'))return;
  privateTarget={x:Math.min(preview.naturalWidth-1,Math.max(0,Math.round(x))),y:Math.min(preview.naturalHeight-1,Math.max(0,Math.round(y)))};
  $('computer-target').style.left=`${privateTarget.x/preview.naturalWidth*100}%`;
  $('computer-target').style.top=`${privateTarget.y/preview.naturalHeight*100}%`;
  $('computer-target').hidden=false;
  $('private-target-label').textContent='Kolom ditandai. Pastikan penanda berada di dalam kolom yang benar.';
  updatePrivateSubmit();
}
async function closeComputerDialog() {
  if(privateBusy)return;
  $('computer-private-value').value='';
  if(privateSession&&Date.now()>=privateSession.expires_at*1000)resetPrivateForm();
  if(privateSession){
    privateControlsBusy(true);
    try{await computerRequest('POST','/input-session/end',{token:privateSession.token})}
    catch(error){
      if(Date.now()>=privateSession.expires_at*1000)resetPrivateForm();
      else{$('computer-dialog-status').textContent=`Sesi belum dilepas: ${error.message}`;privateControlsBusy(false);return}
    }
    privateControlsBusy(false);
  }
  computerDialogVersion++;clearTimeout(computerDialogTimer);resetPrivateForm();
  $('computer-large-preview').removeAttribute('src');$('computer-dialog').close();
  if(!$('details').hidden&&state.configData)loadComputerPanel(state.configData);
}
$('computer-large-preview').onclick=event=>{
  const rect=event.currentTarget.getBoundingClientRect();
  choosePrivateTarget((event.clientX-rect.left)/rect.width*event.currentTarget.naturalWidth,(event.clientY-rect.top)/rect.height*event.currentTarget.naturalHeight);
  if(privateTarget)$('computer-private-value').focus();
};
$('computer-large-preview').onkeydown=event=>{
  if(!privateSession)return;
  const preview=event.currentTarget;
  const target=privateTarget||{x:preview.naturalWidth/2,y:preview.naturalHeight/2};
  if(['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(event.key)){
    event.preventDefault();const step=event.shiftKey?1:10;
    choosePrivateTarget(target.x+(event.key==='ArrowRight'?step:event.key==='ArrowLeft'?-step:0),target.y+(event.key==='ArrowDown'?step:event.key==='ArrowUp'?-step:0));
  }else if(event.key==='Enter'){event.preventDefault();choosePrivateTarget(target.x,target.y);$('computer-private-value').focus()}
};
$('computer-private-form').onsubmit=async event=>{
  event.preventDefault();if(privateBusy||!privateSession||!privateTarget)return;
  let value=$('computer-private-value').value;if(!value)return;
  $('computer-private-value').value='';privateControlsBusy(true);
  $('computer-private-status').textContent='Mengisi kolom pilihan…';
  try{
    const result=await computerRequest('POST','/private-input',{token:privateSession.token,...privateTarget,value,replace:$('computer-private-replace').checked});
    $('computer-private-status').textContent=result.message;
    privateTarget=null;$('computer-target').hidden=true;
  }catch(error){$('computer-private-status').textContent=error.message}
  finally{
    value='';privateControlsBusy(false);
    if(privateSession&&Date.now()>=privateSession.expires_at*1000){resetPrivateForm();$('computer-dialog-status').textContent='Sesi input pribadi berakhir. Buka kembali jika diperlukan.'}
  }
};
$('computer-private-value').oninput=updatePrivateSubmit;
$('computer-launch').onclick=()=>openComputerDialog();
$('computer-private-open').onclick=beginPrivateInput;
$('computer-dialog-refresh').onclick=refreshComputerDialog;
$('computer-dialog-close').onclick=$('computer-private-done').onclick=closeComputerDialog;
$('computer-dialog-control').onclick=()=>{if(computerViewerUrl)window.open(computerViewerUrl,'_blank','noopener')};
$('computer-dialog').addEventListener('cancel',event=>{event.preventDefault();closeComputerDialog()});
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
  mentionIndex=0;
  $('mention-menu').innerHTML=shown.map((a,index)=>`<button type="button" role="option" tabindex="-1" id="mention-${index}" aria-selected="${index===0}" data-mention="${esc(a.id)}">${a.id==='everyone'?'＠':avatar(a)} ${esc(a.name)}</button>`).join('');
  $('mention-menu').hidden=!shown.length;
  if(shown.length)$('draft').setAttribute('aria-activedescendant','mention-0');else closeMentions();
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
  mentionRange=null;closeMentions();$('draft').focus();updateComposer();
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
  $('new-direct').innerHTML=state.roster.map(a=>`<button type="button" class="choice" data-new-direct="${a.id}" style="width:100%;border:0;text-align:left;color:inherit">${avatar(a)}<span>${esc(a.name)}<small>${esc(a.description||'Chat personal')}</small></span></button>`).join('')||'<p class="empty-rooms">Belum ada bot. Hubungkan workspace melalui pengaturan koneksi.</p>';
  $('new-direct').querySelectorAll('[data-new-direct]').forEach(btn=>btn.onclick=()=>openDirect(btn.dataset.newDirect));
  const managers=state.roster.filter(a=>a.is_manager);const candidates=managers.length?managers:state.roster;
  $('group-manager').innerHTML=candidates.map(a=>`<option value="${a.id}">${esc(a.name)}</option>`).join('');
  $('group-members').innerHTML=state.roster.map(a=>`<label class="choice"><input type="checkbox" value="${a.id}"><span>${esc(a.name)}</span></label>`).join('');
}

function selectNewKind(kind) {
  const group=kind==='group';
  document.querySelectorAll('.tabs button').forEach(button=>{const active=button.dataset.kind===kind;button.classList.toggle('active',active);button.setAttribute('aria-pressed',String(active))});
  $('new-group').hidden=!group;$('new-direct').hidden=group;$('new-submit').hidden=!group;$('new-error').textContent='';
  $('group-name').required=group;
}
function openNewDialog(kind='direct') {
  setCreateMenu(false);renderNewDialog();selectNewKind(kind);$('new-dialog').showModal();
  if(kind==='group')$('group-name').focus();
}
$('new-open').onclick=()=>setCreateMenu($('create-menu').hidden);
$('welcome-new').onclick=()=>openNewDialog();
$('create-direct').onclick=()=>openNewDialog('direct');
$('create-group').onclick=()=>openNewDialog('group');
$('new-close').onclick=$('new-cancel').onclick=()=>$('new-dialog').close();
document.querySelectorAll('.tabs button').forEach(btn=>btn.onclick=()=>selectNewKind(btn.dataset.kind));
$('new-form').onsubmit=async e=>{
  e.preventDefault();if($('new-group').hidden)return;
  const title=$('group-name').value.trim();const manager=$('group-manager').value;
  if(!title||!manager){$('new-error').textContent='Isi nama grup dan pilih manager.';return}
  const members=[...$('group-members').querySelectorAll('input:checked')].map(input=>input.value);
  $('new-submit').disabled=true;$('new-submit').textContent='Membuat…';
  try{const room=await request('POST','/rooms',{kind:'group',title,manager_agent_id:manager,member_agent_ids:members});state.rooms.unshift(room);$('new-dialog').close();$('group-name').value='';await openRoom(room.id)}
  catch(error){$('new-error').textContent=error.message}
  finally{$('new-submit').disabled=false;$('new-submit').textContent='Buat grup'}
};
$('search-open').onclick=()=>{
  const open=$('search-wrap').hidden;$('search-wrap').hidden=!open;$('search-open').setAttribute('aria-expanded',String(open));
  if(open)$('search').focus();else{$('search').value='';renderRooms()}
};
$('search').oninput=renderRooms;
$('search').onkeydown=event=>{if(event.key==='Escape'){event.stopPropagation();$('search-open').click();$('search-open').focus()}};
$('mention-open').onclick=()=>insertAtCaret('@');
$('attachment-open').onclick=()=>{if(state.room)$('attachment-input').click()};
$('attachment-input').onchange=async event=>{
  const files=[...event.target.files];event.target.value='';
  if(!state.room||!files.length)return;
  const roomId=state.room.id;const roomAttachments=state.attachments;
  const remaining=5-state.attachments.length;
  if(remaining<=0){$('connection-status').textContent='Maksimal lima lampiran dalam satu pesan.';$('connection-status').hidden=false;return}
  for(const file of files.slice(0,remaining)){
    const draft={localId:crypto.randomUUID(),filename:file.name,size_bytes:file.size,is_image:file.type.startsWith('image/'),uploading:true,error:''};
    roomAttachments.push(draft);if(state.room?.id===roomId)renderAttachmentDraft();
    try{Object.assign(draft,await uploadAttachment(roomId,file),{localId:draft.localId,uploading:false,error:''})}
    catch(error){draft.uploading=false;draft.error=error.message}
    if(state.room?.id===roomId)renderAttachmentDraft();
  }
};
$('attachment-draft').onclick=event=>{
  const button=event.target.closest('[data-remove-attachment]');if(!button)return;
  const index=state.attachments.findIndex(item=>item.localId===button.dataset.removeAttachment);
  if(index>=0)state.attachments.splice(index,1);renderAttachmentDraft();
};
$('draft').oninput=()=>{if(!$('draft').innerText.trim()&&!$('draft').querySelector('[data-agent-id]'))$('draft').replaceChildren();const found=currentMention();mentionRange=found?.range||null;if(found&&state.room?.kind==='group')mentionOptions(found.filter);else closeMentions();updateComposer()};
$('draft').onpaste=event=>{event.preventDefault();insertAtCaret(event.clipboardData.getData('text/plain'))};
$('draft').onkeydown=e=>{
  if(e.isComposing)return;
  if(e.key==='Escape'&&!$('mention-menu').hidden){e.stopPropagation();closeMentions();return}
  const options=!$('mention-menu').hidden?[...$('mention-menu').querySelectorAll('[data-mention]')]:[];
  if(options.length&&['ArrowDown','ArrowUp'].includes(e.key)){
    e.preventDefault();mentionIndex=(mentionIndex+(e.key==='ArrowDown'?1:-1)+options.length)%options.length;
    options.forEach((button,index)=>button.setAttribute('aria-selected',String(index===mentionIndex)));
    $('draft').setAttribute('aria-activedescendant',options[mentionIndex].id);options[mentionIndex].scrollIntoView({block:'nearest'});return;
  }
  if(options.length&&(e.key==='Tab'||e.key==='Enter')&&!e.shiftKey){e.preventDefault();selectMention(options[mentionIndex].dataset.mention);return}
  if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();if(!$('send').disabled)$('composer').requestSubmit()}
};
$('composer').onsubmit=async e=>{e.preventDefault();if(!state.room)return;
  const draft=$('draft');const content=draft.innerText.trim();const attachments=state.attachments.filter(item=>item.id&&!item.error&&!item.uploading);
  if(!content&&!attachments.length)return;
  if(state.attachments.some(item=>item.uploading)){ $('connection-status').textContent='Tunggu upload lampiran selesai.';$('connection-status').hidden=false;return; }
  if(state.attachments.some(item=>item.error)){ $('connection-status').textContent='Hapus atau unggah ulang lampiran yang gagal.';$('connection-status').hidden=false;return; }
  const tokens=[...draft.querySelectorAll('[data-agent-id]')].map(node=>node.dataset.agentId);
  const everyone=tokens.includes('everyone');const targets=everyone?[]:[...new Set(tokens)];
  const workingTargets=everyone ? (state.room.member_agent_ids || []) : (targets.length ? targets : [state.room.manager_agent_id]);
  const roomId=state.room.id;const draftHtml=draft.innerHTML;
  const sendId=++nextSendId;
  state.pendingSends.add(sendId);
  draft.innerHTML='';state.attachments=[];renderAttachmentDraft();closeMentions();draft.focus();
  // The accepted request already has a persisted working reply, but make the
  // companion visibly start immediately instead of waiting for the next poll.
  setBotsWorking(workingTargets,true);
  keepPendingSendsVisible();
  try{
    await request('POST',`/rooms/${roomId}/messages`,{content,target_agent_ids:targets,attachment_ids:attachments.map(item=>item.id),everyone,client_message_id:crypto.randomUUID()});
    if(state.room?.id===roomId)await refreshMessages();
    try{
      await refreshSidebar();
    }catch(error){
      if(state.room?.id===roomId){
        $('timeline').insertAdjacentHTML('beforeend',`<div class="muted" role="status">Pesan terkirim, tetapi daftar chat belum tersinkron. Coba refresh halaman.</div>`);
      }
    }
  }catch(error){
    const current=state.room?.id===roomId;
    const saved=current?{html:draft.innerHTML,attachments:state.attachments}:state.drafts.get(roomId);
    const restored={html:draftHtml+(saved?.html?'<br>'+saved.html:''),attachments:[...attachments,...(saved?.attachments||[])]};
    state.drafts.set(roomId,restored);
    if(state.room?.id===roomId){
      draft.innerHTML=restored.html;state.attachments=restored.attachments;renderAttachmentDraft();
    }
    $('connection-status').textContent=`Pesan ke ${state.rooms.find(room=>room.id===roomId)?.title||'percakapan'} belum terkirim: ${error.message}. Draft dipulihkan.`;
    $('connection-status').hidden=false;
  }finally{
    setBotsWorking(workingTargets,false);
    state.pendingSends.delete(sendId);
    if(!state.pendingSends.size){clearInterval(sendRefreshTimer);sendRefreshTimer=null}
  }
};
$('details-open').onclick=$('chat-title').onclick=()=>setDetails($('details').hidden);
$('details-close').onclick=()=>setDetails(false,true);
$('details-body').onclick=event=>{
  const member=event.target.closest('[data-config-agent]');
  if(member){rememberConfigDraft();state.configAgentId=member.dataset.configAgent;renderDetails();return}
  if(event.target.closest('#skill-new')){openSkillEditor(null);return}
  const edit=event.target.closest('[data-skill-edit]');
  if(edit){const skill=state.configData?.skills?.find(item=>item.name===edit.dataset.skillEdit&&item.editable);if(skill)openSkillEditor(skill);return}
  if(event.target.closest('#computer-retry')){if(state.configData)loadComputerPanel(state.configData);return}
  if(event.target.closest('#computer-expand')){openComputerDialog();return}
  if(event.target.closest('#computer-sensitive')){openComputerDialog(true);return}
  if(event.target.closest('#computer-open')){if(computerViewerUrl)window.open(computerViewerUrl,'_blank','noopener');return}
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
$('mobile-rooms').onclick=()=>setRail(!$('rail').classList.contains('open'));
$('rail-close').onclick=$('rail-backdrop').onclick=()=>{setCreateMenu(false);setRail(false,true)};
$('timeline').onscroll=updateLatestButton;
new ResizeObserver(()=>{
  if(followingLatest)$('timeline').scrollTop=$('timeline').scrollHeight;
  updateLatestButton();
}).observe($('timeline'));
$('latest-message').onclick=()=>{$('timeline').scrollTop=$('timeline').scrollHeight;updateLatestButton()};
document.addEventListener('click',event=>{
  if(!$('create-menu').hidden&&!event.target.closest('#create-menu,#new-open'))setCreateMenu(false);
  if(!$('mention-menu').hidden&&!event.target.closest('#mention-menu,#draft,#mention-open'))closeMentions();
});
document.addEventListener('keydown',event=>{
  if(document.querySelector('dialog[open]'))return;
  if(event.key==='Escape'){
    if(!$('create-menu').hidden){setCreateMenu(false);$('new-open').focus()}
    else if(!$('details').hidden)setDetails(false,true);
    else if($('rail').classList.contains('open'))setRail(false,true);
  }
  const surface=!$('create-menu').hidden?$('create-menu'):!$('details').hidden&&matchMedia('(max-width: 1200px)').matches?$('details'):$('rail').classList.contains('open')&&matchMedia('(max-width: 760px)').matches?$('rail'):null;
  if(event.key==='Tab'&&surface){
    const focusable=[...surface.querySelectorAll('button:not(:disabled),a[href],input,select,textarea')].filter(node=>node.getClientRects().length);
    const first=focusable[0],last=focusable.at(-1);
    if(event.shiftKey&&document.activeElement===first){event.preventDefault();last?.focus()}
    else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first?.focus()}
  }
});
matchMedia('(min-width: 761px)').addEventListener('change',event=>{if(event.matches)setRail(false)});
$('settings-open').onclick=()=>{fillSettings();$('settings-error').textContent='';$('settings-dialog').showModal()};
$('settings-close').onclick=$('settings-cancel').onclick=()=>$('settings-dialog').close();
$('settings-form').onsubmit=async e=>{e.preventDefault();state.mode=$('auth-mode').value;state.adminKey=$('admin-key').value.trim();if(!credentialsReady()){$('settings-error').textContent='Isi API key.';return}localStorage.setItem('apiKey',state.adminKey);localStorage.setItem('teamAuthMode',state.mode);sessionStorage.removeItem('teamOwnerKey');state.room=null;$('settings-dialog').close();await load()};
async function refreshBotActivity() {
  if(document.hidden||!credentialsReady())return;
  const key=state.adminKey;
  try{if(key===state.adminKey)await refreshSidebar()}catch{}
}
setInterval(refreshBotActivity,3000);
document.addEventListener('visibilitychange',()=>{document.documentElement.classList.toggle('avatars-paused',document.hidden);if(!document.hidden)refreshBotActivity()});
document.documentElement.classList.toggle('avatars-paused',document.hidden);
load();
