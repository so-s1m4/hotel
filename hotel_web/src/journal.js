/** The visible journal is a prefix of the selected point in the replay. */
const titles={order_service:'Arrange extra service',optimize_energy:'Improve energy use',flag_anomaly:'Check unusual readings',adjust_room_setpoint:'Adjust room temperature',adjust_heatpump:'Adjust heating',schedule_cleaning:'Schedule housekeeping',request_inspection:'Inspect an issue',create_service_task:'Arrange guest service',notify_guest:'Update the guest',defer_action:'Request clarification',cancel_action:'Cancel a previous action',release_room:'Release a room',guest_extraction:'Understand guest request',replay_started:'Replay prepared',replay_finished:'Replay completed',schedule_robot:'Schedule a robot',schedule_waste_collection:'Arrange waste collection',schedule_vending_restock:'Restock supplies'};
const statusLabel=value=>({llm:'Interpreted',fallback:'Needs review',source_label:'Source label',pending_human_approval:'Approval needed',simulated_message_proposed:'Update prepared',simulated:'Simulated',queued:'Queued',completed:'Complete'}[value]||String(value||'Recorded').replaceAll('_',' '));
export function createJournal({ api, escape, toast }) {
  let replayId=null,total=0,after=0,next=0,latest=true,request=0,entries=[],timer=null,playback=null;
  const host=document.querySelector('#activity'),panel=document.querySelector('#journal-panel');
  const controls=document.createElement('div');controls.className='journal-controls';
  controls.innerHTML='<button id="journal-prev">← Earlier</button><span id="journal-range">No actions yet</span><button id="journal-next">Later →</button><button id="journal-latest">Latest</button><button id="journal-export">Download log</button>';
  host.after(controls);
  host.addEventListener('toggle',event=>{if(event.target.matches('.journal-entry')&&event.target.open)latest=false;},true);
  panel.addEventListener('toggle',()=>{if(panel.open)load(latest?Math.max(0,total-100):after);});
  function render(){
    const opened=new Set([...host.querySelectorAll('.journal-entry[open]')].map(e=>e.dataset.entry)),scroll=host.scrollTop;
    const visible=entries.filter(a=>a.id<=total);
    host.innerHTML=visible.length?visible.map(a=>`<details class="journal-entry" data-entry="${a.id}" ${opened.has(String(a.id))?'open':''}><summary><time datetime="${escape(a.timestamp)}">${escape(new Date(a.timestamp).toLocaleString('en-GB',{day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit',timeZone:'Europe/Vienna'}))}</time><strong>${escape(titles[a.action]||titles[a.kind]||(a.title||'Action recorded').replaceAll('_',' '))}</strong>${a.room_id?`<span>${escape(a.room_id.replace(/^R/,'Room '))}</span>`:''}<span class="journal-status">${escape(statusLabel(a.status))}</span></summary><div class="journal-body"><p><strong>Why</strong> ${escape(String(a.reason||'').replaceAll('_',' '))}</p>${(a.details?.decision_record||a.details?.decision)?.verification_plan?`<p><strong>Verify</strong> ${escape(String((a.details.decision_record||a.details.decision).verification_plan).replaceAll('_',' '))}</p>`:''}${a.evidence?.length?`<details><summary>Supporting records</summary><p class="journal-evidence">${escape(a.evidence.join(' · '))}</p></details>`:''}</div></details>`).join(''):'<p class="muted">No actions at this point.</p>';
    host.scrollTop=scroll;
    document.querySelector('#journal-range').textContent=visible.length?`${visible[0].id}–${visible.at(-1).id} of ${total}`:`${total} actions`;
    document.querySelector('#journal-count').textContent=total?`· ${total.toLocaleString()}`:'';
    document.querySelector('#journal-prev').disabled=after===0;
    document.querySelector('#journal-next').disabled=next>=total;
    document.querySelector('#journal-export').title=playback?'Download actions up to the selected time':'Download the full session log';
  }
  async function load(cursor){
    if(!panel.open)return;
    const version=++request,source=replayId,boundary=total;
    if(!boundary){entries=[];after=next=0;render();return;}
    cursor=Math.min(cursor,Math.max(0,boundary-1));
    try{
      const limit=Math.min(100,boundary-cursor);
      const page=await api(source?`/api/replays/${encodeURIComponent(source)}?after=${cursor}&limit=${limit}`:`/api/journal?after=${cursor}&limit=${limit}`);
      if(version!==request||source!==replayId)return;
      entries=(page.entries||page.timeline||[]).filter(row=>row.id<=Math.min(boundary,total));
      after=cursor;next=Math.min(page.next_cursor,boundary,total);render();
    }catch(e){if(version===request)toast(e.message,true);}
  }
  document.querySelector('#journal-prev').onclick=()=>{latest=false;load(Math.max(0,after-100));};
  document.querySelector('#journal-next').onclick=()=>{latest=false;load(next);};
  document.querySelector('#journal-latest').onclick=()=>{latest=true;load(Math.max(0,total-100));};
  document.querySelector('#journal-export').onclick=async()=>{
    const button=document.querySelector('#journal-export');button.disabled=true;
    const source=replayId,targetTotal=total;
    try{
      const all=[];let cursor=0;
      while(cursor<targetTotal){
        const limit=Math.min(200,targetTotal-cursor);
        const page=await api(source?`/api/replays/${encodeURIComponent(source)}?after=${cursor}&limit=${limit}`:`/api/journal?after=${cursor}&limit=${limit}`);
        all.push(...(page.entries||page.timeline||[]).filter(row=>row.id<=targetTotal));
        if(page.next_cursor<=cursor)throw new Error('The journal download stopped early. Please retry.');
        cursor=page.next_cursor;
      }
      if(all.length!==targetTotal)throw new Error('The journal is incomplete. Please retry.');
      const blob=new Blob([all.map(row=>JSON.stringify(row)).join('\n')+(all.length?'\n':'')],{type:'application/x-ndjson'});
      const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=`hotel-${source||'session'}-journal.jsonl`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
    }catch(e){toast(e.message,true);}finally{button.disabled=false;}
  };
  return {
    update(source=null,count=0,frame=null){
      const changed=source!==replayId,rewound=count<total;playback=frame;
      if(changed||rewound){request++;clearTimeout(timer);timer=null;entries=[];after=next=0;latest=true;}
      const old=total;replayId=source;total=count;
      document.querySelector('#journal-count').textContent=total?`· ${total.toLocaleString()}`:'';
      if(changed||rewound||!count)render();
      if(panel.open&&(changed||rewound||old!==count)&&!timer)timer=setTimeout(()=>{timer=null;load(latest?Math.max(0,total-100):after);},350);
    },
    refresh(){load(latest?Math.max(0,total-100):after);}
  };
}
