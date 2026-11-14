import '@fontsource-variable/dm-sans';
import '@fontsource-variable/manrope';
import './style.css';
import { createHotelScene } from './scene.js';
import { createJournal } from './journal.js';
import { createReplayControls } from './replay.js';
import { createDatasetImport } from './import.js';

const icons = {
  grid:'<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  chat:'<path d="M21 11.5a8.5 8.5 0 0 1-8.5 8.5H4l-2 2v-9.5A8.5 8.5 0 0 1 10.5 4h2A8.5 8.5 0 0 1 21 11.5Z"/><path d="M7 10h9M7 14h6"/>',
  cube:'<path d="m12 3 9 5v9l-9 5-9-5V8zM3 8l9 5 9-5M12 13v9M7.5 5.5l9 5"/>',
  bell:'<path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4"/>',
  arrow:'<path d="M5 12h14m-5-5 5 5-5 5"/>',
  send:'<path d="m22 2-7 20-4-9L2 9Z"/><path d="m22 2-11 11"/>',
  check:'<path d="m5 12 4 4L19 6"/>',
  close:'<path d="m6 6 12 12M6 18 18 6"/>',
  play:'<path d="m8 4 12 8-12 8Z"/>',
  layers:'<path d="m12 3 10 5-10 5L2 8zM2 12l10 5 10-5M2 16l10 5 10-5"/>',
  rotate:'<path d="M20 7a9 9 0 1 0 1 8M20 2v5h-5"/>',
  water:'<path d="M12 2S5 10 5 15a7 7 0 0 0 14 0c0-5-7-13-7-13Z"/><path d="M8 15a4 4 0 0 0 4 4"/>',
  key:'<circle cx="8" cy="8" r="5"/><path d="m12 12 9 9m-3-3 3-3m-6 0 3-3"/>',
  sun:'<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5"/>',
  shield:'<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6z"/><path d="m8 12 3 3 5-6"/>',
  people:'<circle cx="9" cy="7" r="3"/><path d="M3 21v-4a6 6 0 0 1 12 0v4M16 4a3 3 0 0 1 0 6m3 11v-4a5 5 0 0 0-3-4"/>',
  expand:'<path d="M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5"/>',
  wifi:'<path d="M2 8a16 16 0 0 1 20 0M5 12a11 11 0 0 1 14 0M8 16a6 6 0 0 1 8 0"/><circle cx="12" cy="20" r="1"/>',
};
const icon = (name, cls='') => `<svg class="icon ${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icons[name]||icons.chat}</svg>`;
const escape = value => String(value ?? '').replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const names = { ready:'Ready for arrival', occupied:'Occupied', attention:'Needs attention', cleaning:'Housekeeping', vacant:'Vacant · release not verified', unknown:'Not confirmed' };
const assetLabel = value => /^R\d+$/.test(value||'') ? 'Room '+value.slice(1) : ({WASTE_MAIN:'Waste collection',VEND1:'Vending',HP1:'Heat pump',BUILDING:'Hotel'}[value]||String(value||'Hotel'));
const displayReason=value=>String(value||'').replaceAll('_',' ');
const intentNames = { water_issue:'Water issue', maintenance_issue:'Maintenance', access_issue:'Room access', early_checkin:'Early arrival', waiting_for_room:'Waiting for a room', arrival_delay:'Late arrival', late_checkout:'Late checkout', wifi_help:'Wi-Fi help', extra_towels:'Extra towels', cleaning_request:'Housekeeping', temperature_hot:'Room too warm', temperature_cold:'Room too cold', air_quality:'Air quality', noise_complaint:'Noise', luggage_storage:'Luggage storage', parking:'Parking', invoice:'Invoice', unknown:'Needs clarification' };
const actionNames={request_inspection:'Inspect the issue',schedule_cleaning:'Schedule housekeeping',create_service_task:'Arrange guest service',notify_guest:'Update the guest',defer_action:'Request clarification',cancel_action:'Cancel a prior action',release_room:'Release the room',dispatch_technician:'Send a technician',adjust_setpoint:'Adjust temperature',adjust_room_setpoint:'Adjust room temperature',optimize_energy:'Improve energy use',flag_anomaly:'Check unusual readings',order_service:'Arrange extra service',request_human_approval:'Review the request',close_valve:'Close the water valve'};
const scenarios = [
  {id:'water',icon:'water',title:'A little drip. A clear next step.',room:'R208',text:'The bathroom tap keeps dripping even when I close it completely. Could someone take a look?',language:'en',tag:'Maintenance'},
  {id:'access',icon:'key',title:'A guest cannot get into their room.',room:'R305',text:'Meine Zimmerkarte funktioniert nicht und ich komme nicht in mein Zimmer.',language:'de',tag:'Guest access'},
  {id:'arrival',icon:'sun',title:'An early arrival, handled thoughtfully.',room:'R102',text:'We are still on the train. Could we check in today at 12:45 instead of 15:00?',language:'en',tag:'Front desk'},
  {id:'towels',icon:'chat',title:'Two requests. Nothing missed.',room:'R403',text:'Could we have two extra towels? The television screen also stays black when we turn it on.',language:'en',tag:'Multiple requests'},
];
let liveState=null, replayControls=null, journalView=null;
let state=null, selectedRoom='R208', selectedProposal=null, busy=false, scene=null, pendingRequest=null, tourRunning=false;

document.querySelector('#app').innerHTML=`
  <div class="shell">
    <header class="topbar"><a class="wordmark" href="#">PLATO<span>40 rooms · Vienna</span></a><div class="topbar-right"><span class="demo-chip">Demo</span><span class="today" id="clock"></span><button class="icon-button" id="presentation" title="Full screen">${icon('expand')}</button></div></header>
    <main id="overview">
      <section class="metrics" aria-label="Hotel status">
        <div class="metric"><span class="metric-icon sage">${icon('cube')}</span><div><span class="metric-label">Rooms</span><strong id="metric-rooms">40</strong></div></div>
        <div class="metric"><span class="metric-icon blue">${icon('people')}</span><div><span class="metric-label">Occupied</span><strong id="metric-occupied">—</strong></div></div>
        <div class="metric"><span class="metric-icon sage">${icon('check')}</span><div><span class="metric-label">Autonomous</span><strong id="metric-autonomous">0</strong></div></div>
        <div class="metric"><span class="metric-icon amber">${icon('bell')}</span><div><span class="metric-label">Needs approval</span><strong id="metric-pending">0</strong></div><span class="mini-label" id="approval-label">All clear</span></div>
      </section>
      <section class="workspace">
        <section class="property-panel panel" aria-label="Hotel overview">
          <div class="property-heading"><div class="property-title"><h2>The hotel</h2><span>40 rooms · one view</span></div><div class="camera-controls" role="group" aria-label="Hotel camera"><button id="reset-view" data-camera="overview" class="selected" aria-pressed="true">${icon('grid')}Overview</button><button data-camera="reception" aria-pressed="false">${icon('key')}Reception</button><button data-camera="housekeeping" aria-pressed="false">${icon('people')}Housekeeping</button></div></div>
          <div class="scene-stage"><div id="hotel-scene" class="hotel-scene"></div><div class="scene-annotation"><span>Drag to rotate · Scroll to zoom · Select a room</span><span class="fixture-label">Illustrative positions</span></div></div>
          <div class="property-footer"><div class="legend"><span><i class="occupied"></i>Occupied</span><span><i class="ready"></i>Ready</span><span><i class="cleaning"></i>Cleaning</span><span><i class="attention"></i>Attention</span><span><i class="unknown"></i>Unknown</span></div></div>
          <div class="room-overview"><div class="room-detail" id="room-detail"></div><div class="room-directory"><span class="room-directory-label">Choose a room</span><div class="room-grid" id="room-grid" aria-label="Select a room"></div></div></div>
        </section>
        <aside class="inbox panel" id="inbox"><div class="panel-heading"><h2>Guest messages</h2><span class="count" id="message-count">0</span></div><div id="messages" class="messages" aria-live="polite"></div>
          <form id="message-form" class="composer"><div class="compose-context"><label for="room-select">Room</label><select id="room-select" aria-label="Guest room"></select><label class="sr-only" for="language">Message language</label><select id="language"><option value="en">EN</option><option value="de">DE</option><option value="it">IT</option><option value="fr">FR</option><option value="es">ES</option><option value="nl">NL</option></select></div><label class="sr-only" for="message-input">Guest message</label><textarea id="message-input" rows="3" maxlength="4000" placeholder="Write a guest message…">${scenarios[0].text}</textarea><div class="compose-bottom"><span id="model-status"></span><button type="submit" id="send-message" aria-label="Send guest message">${icon('send')}</button></div></form>
          <div class="quick-scenarios"><button class="button ghost" id="walkthrough">${icon('chat')}Try a request</button></div>
        </aside>
        <aside class="manager-column panel" id="approvals"><div class="manager-heading"><h2>Manager</h2><span id="manager-mode">Only exceptions</span></div><div class="phone"><div class="phone-screen"><div class="phone-status"><span id="phone-clock">—</span><div class="dynamic-island"></div><span class="phone-signal">▮▮▮ ▰</span></div><div class="phone-appbar"><span class="phone-logo">p.</span><div><strong>PLATO</strong><span>Needs your attention</span></div><span class="live-dot"></span></div><div class="phone-body" id="phone-body"></div><div class="phone-nav"><button class="selected" title="Operations">${icon('grid')}<span>Overview</span></button><button id="phone-requests" title="Guest requests">${icon('chat')}<span>Requests</span></button><button id="phone-team" title="View team">${icon('people')}<span>Team</span></button></div><div class="home-indicator"></div></div></div><div class="approval-switcher" id="approval-switcher"></div><p class="phone-caption">Simulated actions</p></aside>
      </section>
      <details class="activity-section" id="journal-panel"><summary class="activity-heading"><strong>Action log</strong><span>Actions & reasons <span id="journal-count"></span> ↓</span></summary><div id="activity" class="activity-list"></div></details>
      <footer><span>PLATO 40</span><span>Simulated actions · Illustrated residents</span></footer>
    </main>
  </div><div id="toast" class="toast" role="status" aria-live="polite"></div>
  <dialog id="tour-dialog"><button class="dialog-close icon-button" aria-label="Close">${icon('close')}</button><h2>Try a request</h2><div class="tour-options">${scenarios.map(s=>`<button data-run-scenario="${s.id}"><span class="scenario-icon">${icon(s.icon)}</span><span><small>ROOM ${s.room.slice(1)}</small><strong>${s.tag}</strong></span>${icon('arrow')}</button>`).join('')}</div></dialog>`;

const $ = sel => document.querySelector(sel);
function toast(text, error=false) { const el=$('#toast');el.textContent=text;el.className=`toast visible${error?' error':''}`; clearTimeout(toast.timer);toast.timer=setTimeout(()=>el.classList.remove('visible'),6000); }
function formatTime(value) { if(!value)return 'Now';const d=new Date(value);return Number.isNaN(d.valueOf())?'Now':new Intl.DateTimeFormat('en-GB',{hour:'2-digit',minute:'2-digit',timeZone:'Europe/Vienna'}).format(d); }
function updateClock(value=state?.playback?.at_utc || state?.property?.as_of_utc || new Date().toISOString()) {
  const d=new Date(value);if(!Number.isFinite(d.valueOf()))return;
  $('#clock').textContent=new Intl.DateTimeFormat('en-GB',{day:'2-digit',month:'short',year:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit',timeZone:'Europe/Vienna'}).format(d);
  $('#phone-clock').textContent=formatTime(value);
}
updateClock();setInterval(()=>{if(state?.source!=='historical_dataset')updateClock();},1000);

async function api(path, data) {
  const response=await fetch(path,{method:data?'POST':'GET',headers:data?{'Content-Type':'application/json'}:{},body:data?JSON.stringify(data):undefined});
  const payload=await response.json();
  if(!response.ok) {const error=new Error(payload.error?.message || 'The request could not be completed. Please try again.');error.status=response.status;throw error;}
  return payload;
}
function roomById(id) {return state?.rooms.find(r=>r.id===id);}
function selectRoom(id) {
  if(!roomById(id))return;
  selectedRoom=id;if($('#room-select').querySelector(`option[value="${id}"]`))$('#room-select').value=id;
  scene?.select(id);scene?.focusRoom(id);markCamera(null);renderRoom();
  if(state) {const proposal=[...state.proposals].reverse().find(p=>p.room_id===id);if(proposal){selectedProposal=proposal.id;renderPhone();}}
  document.querySelectorAll('.room-cell').forEach(b=>b.classList.toggle('selected',b.dataset.room===id));
}
function renderRoom() {
  const room=roomById(selectedRoom);if(!room)return;
  const displayStatus=room.active_request_count?'attention':room.status;
  $('#room-detail').innerHTML=`<div class="room-number"><span>ROOM</span><strong>${escape(room.number||room.id.slice(1))}</strong></div><div class="room-description"><strong>${escape(room.guest_name || (room.occupancy==='occupied'?'Occupied':room.occupancy==='vacant'?'Vacant':'Occupancy unknown'))}</strong><span><i class="status-dot ${escape(displayStatus)}"></i>${escape(room.active_request_count?`${room.active_request_count} request(s) need attention`:(names[room.status]||room.status))}</span></div><div class="room-temperature">${icon('sun')}<strong>${Number.isFinite(room.temperature_c)?room.temperature_c.toFixed(1)+'°':'—'}</strong><span>${state.source==='historical_dataset'?'Recorded reading':'Demo reading'}</span></div><button class="icon-button" id="message-this-room" title="Write to this room">${icon('chat')}</button>`;
  $('#message-this-room').title=state.source==='historical_dataset'?'View guest messages':'Write a guest message';
  $('#message-this-room').onclick=()=>{if(state.source!=='historical_dataset')$('#message-input').focus();$('#inbox').scrollIntoView({behavior:'smooth',block:'nearest'});};
}
function renderMessages() {
  $('#message-count').textContent=state.stats?.guests??state.messages.length;
  if(!state.messages.length){$('#messages').innerHTML='<div class="empty-inbox"><p>Waiting for the next guest message.</p></div>';return;}
  $('#messages').innerHTML=state.messages.slice(-5).map(m=>{
    const room=roomById(m.room_id), labels=(m.intents||[]).map(i=>typeof i==='string'?i:i.intent);
    return `<article class="guest-message"><div class="message-author"><span class="guest-avatar">${escape((room?.guest_name||'Guest').charAt(0))}</span><div><strong>${escape(room?.guest_name||'Guest')}</strong><span>Room ${escape(m.room_id?.replace(/^R/,'')||'unassigned')} · ${state.source==='historical_dataset'?escape(new Date(m.created_at).toLocaleString('en-GB',{day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit',timeZone:'Europe/Vienna'})):formatTime(m.created_at)}</span></div><span class="message-language">${escape(m.language?.toUpperCase())}</span></div><p>${escape(m.text)}</p><div class="intent-chips">${labels.map(l=>`<span>${escape(intentNames[l]||l)}</span>`).join('')}</div><div class="message-status ${m.status==='degraded'?'warning':''}">${icon(m.status==='degraded'?'bell':'check')} ${m.status==='degraded'?'AI unavailable · review needed':m.needs_review?'Clarification needed':m.status==='handled_autonomously'?'Handled autonomously':m.status==='approval_required'?'Approval needed':m.status==='clarification_needed'?'Clarification needed':m.status==='no_action'?'No new action needed':'Request processed'}</div></article>`;
  }).join('');
  $('#messages').scrollTop=$('#messages').scrollHeight;
}
function renderPhone() {
  if(!state.proposals.length){
    selectedProposal=null;$('#approval-switcher').innerHTML='';
    $('#phone-body').innerHTML=`<div class="phone-welcome"><div class="phone-welcome-icon">${icon('shield')}</div><h3>All clear.</h3><p>No approval needed.</p><div class="phone-ready"><span class="live-dot"></span>${state.stats?.autonomous||0} autonomous decisions</div></div>`;return;
  }
  const proposal=state.proposals.find(p=>p.id===selectedProposal)||state.proposals.at(-1);selectedProposal=proposal.id;
  const requiresReview=Boolean(proposal.review_required);
  const pending=!proposal.historical&&['pending','pending_approval','awaiting_approval','awaiting_consent','needs_review','pending_guest_consent'].includes(proposal.status);
  const approved=proposal.status.startsWith('approved');
  const rejected=['rejected','declined'].includes(proposal.status);
  $('#phone-body').innerHTML=`<div class="phone-date">${proposal.historical?escape(new Date(proposal.created_at).toLocaleDateString('en-GB',{timeZone:'Europe/Vienna'})):'TODAY'} · ${formatTime(proposal.created_at)}</div><article class="approval-card"><div class="approval-card-label"><span class="notification-app">${icon(proposal.intent==='water_issue'?'water':proposal.intent==='access_issue'?'key':'bell')}</span><div><strong>${escape(proposal.team || 'Operations')}</strong><span>${escape(assetLabel(proposal.room_id))}</span></div><span class="approval-dot"></span></div><h3>${escape(proposal.title)}</h3><p class="approval-reason">${escape(displayReason(proposal.reason))}</p><div class="proposal-tag">${icon('shield')} ${pending?(requiresReview?'Clarification is needed':proposal.status==='pending_guest_consent'?'Waiting for guest permission':'Your approval is needed'):approved?'Approved in demo':rejected?'Declined in demo':escape(proposal.status==='pending_human_approval'?'Approval needed':proposal.status.replaceAll('_',' '))}</div>${proposal.requires_consent?`<div class="consent-box">${icon('key')}<p><strong>Guest permission required</strong><span>${proposal.historical?'The room was occupied or unconfirmed.':'The room is occupied or unconfirmed.'} Approval alone does not authorize entry.</span></p></div>`:''}${proposal.historical?'<p class="review-warning">Recorded approval request</p>':pending?`<div class="approval-actions">${requiresReview?'<p class="review-warning">The meaning or quoted details need checking. Send a clarified guest message before approving.</p>':''}${proposal.requires_consent?`<label class="consent-control"><input id="guest-consent" type="checkbox" ${proposal.consent_granted?'checked':''}><span>Simulate the guest’s permission to enter</span></label>`:''}<button class="button primary approve-button" id="approve-action" ${requiresReview?'disabled':''}>${icon('check')}${requiresReview?'Clarify before approval':proposal.status==='pending_guest_consent'?'Record guest permission':'Approve proposal'}</button><button class="decline-button" id="decline-action">Decline for now</button></div>`:`<div class="decision-result ${approved?'approved':'declined'}">${icon(approved?'check':'close')}<strong>${approved?'Proposal approved in demo.':'Proposal declined.'}</strong><span>${approved?'No real dispatch or room entry has occurred.':'No task has been dispatched.'}</span></div>`}</article>`;
  if(pending){$('#approve-action').onclick=()=>decide(proposal,'approve');$('#decline-action').onclick=()=>decide(proposal,'reject');}
  $('#approval-switcher').innerHTML=`<label class="sr-only" for="approval-select">Choose an approval exception</label><select id="approval-select">${state.proposals.map(p=>`<option value="${escape(p.id)}" ${p.id===selectedProposal?'selected':''}>${escape(assetLabel(p.room_id))} · ${escape(p.title)} · ${escape(p.status==='pending_human_approval'?'Approval needed':p.status.replaceAll('_',' '))}</option>`).join('')}</select>`;
  $('#approval-select').onchange=e=>{selectedProposal=e.target.value;renderPhone();};
}
function renderActivity() {
  if(state.source!=='historical_dataset')journalView?.update(null,state.journal?.total||0);
}

function render() {
  const occupied=state.rooms.filter(r=>r.occupancy==='occupied').length;
  const pending=state.stats?.pending ?? state.proposals.filter(p=>['pending_human_approval','pending','pending_approval','awaiting_approval','awaiting_consent','needs_review','pending_guest_consent'].includes(p.status)).length;
  $('#metric-rooms').textContent=state.rooms.length;
  $('#metric-occupied').textContent=occupied;
  $('#metric-autonomous').textContent=(state.stats?.autonomous||0).toLocaleString();
  $('#metric-pending').textContent=pending.toLocaleString();
  $('#manager-mode').textContent=state.source==='historical_dataset'?'Recorded exceptions':'Only exceptions';
  $('.phone-caption').textContent=state.source==='historical_dataset'?'Recorded requests · Simulated actions':'Simulated actions';
  $('#approval-label').textContent=pending?'Review':'All clear';$('#approval-label').classList.toggle('needs-attention',Boolean(pending));
  $('#model-status').textContent=state.model.status==='degraded'?'Needs review':['connected','result_available'].includes(state.model.status)?'AI ready':'Ready';
  if(!$('#room-select').options.length)$('#room-select').innerHTML=state.rooms.map(r=>`<option value="${escape(r.id)}" ${r.id===selectedRoom?'selected':''}>${escape(r.number||r.id.slice(1))}</option>`).join('');
  $('#room-grid').innerHTML=state.rooms.map(r=>`<button data-room="${escape(r.id)}" class="room-cell ${escape(r.active_request_count?'attention':r.status)} ${r.id===selectedRoom?'selected':''}" title="Room ${escape(r.id.slice(1))} — ${escape(names[r.status]||r.status)}">${escape(r.id.slice(1))}</button>`).join('');
  document.querySelectorAll('[data-room]').forEach(b=>b.onclick=()=>selectRoom(b.dataset.room));
  renderRoom();renderMessages();renderPhone();renderActivity();scene?.update(state.rooms,selectedRoom);
  scene?.showPeople(true);
  scene?.setPlayback({timeMs:Date.parse(state.playback?.at_utc||state.property?.as_of_utc),playing:state.source==='historical_dataset'?Boolean(state.playback?.playing):true,speed:state.playback?.speed||1,historical:state.source==='historical_dataset'});
  updateClock();
  $('.fixture-label').textContent='Illustrative positions';
  $('.demo-chip').textContent=state.source==='historical_dataset'?'Replay':'Live demo';
  $('footer>span:last-child').textContent=state.source==='historical_dataset'?'Recorded room states · Illustrated residents · Simulated actions':'Demo property · Illustrated residents · Simulated actions';
  $('.composer').hidden=state.source==='historical_dataset';$('.quick-scenarios').hidden=state.source==='historical_dataset';
  $('#inbox').classList.toggle('history-mode',state.source==='historical_dataset');
  $('#walkthrough').disabled=state.source==='historical_dataset'||busy;

}
function setBusy(value) {busy=value;$('#send-message').disabled=value;$('#send-message').innerHTML=value?'<span class="spinner"></span>':icon('send');$('#walkthrough').disabled=value||state?.source==='historical_dataset';$('#message-input').disabled=value;$('#language').disabled=value;$('#room-select').disabled=value;$('.composer').classList.toggle('processing',value);}
async function sendMessage() {
  if(busy||state?.source==='historical_dataset')return;
  const text=$('#message-input').value.trim();if(!text){$('#message-input').focus();return;}
  const payload={text,room_id:$('#room-select').value,language:$('#language').value};
  const fingerprint=JSON.stringify(payload);
  if(!pendingRequest||pendingRequest.fingerprint!==fingerprint)pendingRequest={fingerprint,id:crypto.randomUUID()};
  payload.request_id=pendingRequest.id;setBusy(true);
  try {
    const result=await api('/api/messages',payload);liveState=result.snapshot;if(replayControls?.isViewing()){pendingRequest=null;return;}state=liveState;
    const proposals=state.proposals.filter(p=>(p.request_ids||[p.request_id]).includes(result.request_id));if(proposals.length)selectedProposal=proposals[0].id;
    selectedRoom=payload.room_id;render();pendingRequest=null;
    const degraded=result.request?.status==='degraded'||state.model.status==='degraded';
    const clarify=result.request?.needs_review||result.request?.status==='clarification_needed';
    toast(degraded?'The model could not verify this message. Please clarify and try again.':clarify?'Please clarify this request. The engine deferred the unsupported action.':result.request?.status==='handled_autonomously'?'Handled autonomously. The engine’s action and reason are in the journal.':result.request?.status==='no_action'?'No new action was created. The journal explains why.':'Engine decision recorded. Only the approval exception needs your attention.',degraded||clarify);
    if(tourRunning){$('#approvals').classList.add('highlighted');setTimeout(()=>$('#approvals').classList.remove('highlighted'),3500);tourRunning=false;scene?.tour(false);}
  }catch(error){toast(error.message,true);}finally{setBusy(false);}
}
async function decide(proposal, decision) {
  const consent=Boolean($('#guest-consent')?.checked);
  const buttons=$('#phone-body').querySelectorAll('button');buttons.forEach(b=>b.disabled=true);
  try {const result=await api(`/api/approvals/${encodeURIComponent(proposal.id)}`,{decision,consent});liveState=result.snapshot;if(!replayControls?.isViewing()){state=liveState;render();}toast(decision==='reject'?'Proposal declined.':result.proposal.status==='pending_guest_consent'?'Manager approval recorded. Waiting for guest permission; no entry is authorized.':'Proposal approved in the demonstration. No real-world action was taken.');}
  catch(error){buttons.forEach(b=>b.disabled=false);toast(error.message,true);}
}
function chooseScenario(id, run=false) {const s=scenarios.find(x=>x.id===id);if(!s||busy||state?.source==='historical_dataset')return;selectRoom(s.room);$('#message-input').value=s.text;$('#language').value=s.language;if(run){$('#tour-dialog').close();tourRunning=true;scene?.tour(true);sendMessage();}else{$('#message-input').focus();toast(`${s.tag} scenario ready. Send it to try the live API.`);}}
$('#message-form').onsubmit=e=>{e.preventDefault();sendMessage();};
$('#message-input').addEventListener('keydown',e=>{if(e.key==='Enter'&&(e.metaKey||e.ctrlKey)){e.preventDefault();sendMessage();}});
$('#room-select').onchange=e=>selectRoom(e.target.value);
document.querySelectorAll('[data-scenario]').forEach(b=>b.onclick=()=>chooseScenario(b.dataset.scenario));
document.querySelectorAll('[data-run-scenario]').forEach(b=>b.onclick=()=>chooseScenario(b.dataset.runScenario,true));
function markCamera(value){
  document.querySelectorAll('[data-camera]').forEach(button=>{const selected=button.dataset.camera===value;button.classList.toggle('selected',selected);button.setAttribute('aria-pressed',String(selected));});
}
document.querySelectorAll('[data-camera]').forEach(button=>button.onclick=()=>{const view=button.dataset.camera;markCamera(view);if(view==='overview')scene?.reset();else scene?.focusZone(view);});
document.querySelectorAll('[data-scroll]').forEach(b=>b.onclick=()=>{$('#'+b.dataset.scroll).scrollIntoView({behavior:'smooth',block:'center'});});
$('#walkthrough').onclick=()=>$('#tour-dialog').showModal();
$('.dialog-close').onclick=()=>$('#tour-dialog').close();
$('#tour-dialog').addEventListener('click',e=>{if(e.target===$('#tour-dialog'))$('#tour-dialog').close();});
$('#presentation').onclick=async()=>{try{if(!document.fullscreenElement)await document.documentElement.requestFullscreen();else await document.exitFullscreen();}catch{document.body.classList.toggle('presentation-mode');toast('Presentation layout enabled. Use your browser’s full-screen mode if available.');}};
$('#phone-requests').onclick=()=>$('#inbox').scrollIntoView({behavior:'smooth',block:'center'});
$('#phone-team').onclick=()=>{if(!state)return;const staff=state.people.filter(p=>!['guest','visitor'].includes(p.role));toast(staff.map(p=>`${p.name||p.id} · ${p.role}`).join('  /  ')||(state.source==='historical_dataset'?'No recorded staff on duty at this time.':'No staff shown.'));};
try{scene=createHotelScene($('#hotel-scene'),selectRoom);}catch{$('#hotel-scene').innerHTML='<div class="scene-unavailable"><h3>Explore the hotel below.</h3><p>3D rendering is unavailable in this browser.<br>Room selection and live requests still work.</p></div>';}
async function loadState(){try{liveState=await api('/api/state');if(!replayControls?.isViewing()){state=liveState;render();}}catch(error){toast('The Hotel server is unavailable. Start the local app and refresh.',true);$('#model-status').textContent='Server unavailable';}}
journalView=createJournal({api,escape,formatTime,toast});
replayControls=createReplayControls({api,escape,toast,
  onDemo(){if(liveState){state=liveState;selectedProposal=null;render();}else loadState();},
  onJournal(id,total,playback){journalView.update(id,total,playback);},
  onClock(playback){if(state?.source!=='historical_dataset')return;state.playback=playback;updateClock(playback.at_utc);scene?.setPlayback({timeMs:Date.parse(playback.at_utc),playing:playback.playing,speed:playback.speed,historical:true,reason:playback.reason});},
  onView(result){
    const snapshot=result.snapshot,job=result.job;
    const proposals=(snapshot.pending_approvals||[]).map(({decision:d,audit})=>({
      id:d.event_id,room_id:d.affected_assets.find(a=>a.startsWith('R'))||d.affected_assets[0]||'Hotel',
      intent:d.issue_type,title:actionNames[d.action]||d.action.replaceAll('_',' '),reason:d.reason,team:'Hotel operations',
      status:audit?.status||'pending_approval',requires_consent:d.requires_guest_consent,
      historical:true,created_at:d.timestamp_utc,decision_record:d,audit
    }));
    if(proposals.at(-1)?.id!==state?.proposals?.at(-1)?.id)selectedProposal=proposals.at(-1)?.id||null;
    state={source:'historical_dataset',playback:result.playback,property:{as_of_utc:snapshot.as_of_utc},
      rooms:snapshot.rooms?.length?snapshot.rooms:(liveState?.rooms||[]).map(r=>({id:r.id,floor:r.floor,status:'unknown',occupancy:'unknown'})),people:snapshot.people||[],messages:snapshot.recent_messages||[],proposals,
      decisions:snapshot.decisions||[],activity:[],model:{caption:job.progress.fallback_count?`Cerebras · ${job.progress.fallback_count} fallback(s) in replay`:null,status:job.progress.fallback_count?'degraded':job.progress.llm_count?'result_available':'not_checked'},
      stats:{guests:job.progress.guest_processed,autonomous:job.progress.autonomous_count,pending:snapshot.pending_approvals_total||0}};
    if(!state.rooms.some(r=>r.id===selectedRoom))selectedRoom=state.rooms[0]?.id;
    render();
  }
});
createDatasetImport({host:replayControls.importHost,captureViewIntent:()=>replayControls.viewIntent(),onImported:(dataset,intent)=>replayControls.addDataset(dataset,{viewIntent:intent}),toast});
loadState();
