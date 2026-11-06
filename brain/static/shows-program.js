'use strict';
(()=>{
 const $=s=>document.querySelector(s);
 let snapshot=null,busy=false,online=false,signature='',sequence=0;
 const buttons=['enable','disable','start','next','finish','attach','detach'];
 function controls(){
  for(const name of buttons)$('#program-'+name).disabled=busy||!online||!snapshot?.show_id;
  if(!snapshot?.show_id)return;
  $('#program-enable').disabled||=Boolean(snapshot.context.live_mode);
  $('#program-disable').disabled||=!snapshot.program.live_mode;
  $('#program-start').disabled||=!$('#program-card').value;
  for(const name of ['next','finish'])$('#program-'+name).disabled||=!snapshot.program.card_id||snapshot.program.stage==='finished';
  $('#program-attach').disabled||=!snapshot.program.card_id||!snapshot.available_poll;
  $('#program-detach').disabled||=!snapshot.program.poll_id;
 }
 function render(data){
  snapshot=data;online=true;$('#program-panel').hidden=!data.show_id;
  const p=data.program,c=data.context;
  $('#program-status').textContent=(c.live_mode?'直播节奏已开启':p.live_mode?'直播节奏待恢复 · 重启后请重新开启':'直播节奏未开启')+' · '+(c.stage_label||'等待栏目')+' · '+(c.status||'');
  const next=JSON.stringify(data.cards);
  if(next!==signature){const selected=$('#program-card').value;signature=next;$('#program-card').replaceChildren(...data.cards.map(card=>{const o=document.createElement('option');o.value=card.id;o.textContent=card.title;return o;}));$('#program-card').value=data.cards.some(c=>c.id===selected)?selected:p.card_id||data.cards[0]?.id||'';}
  const poll=data.available_poll;
  $('#program-poll-label').textContent=poll?`可关联的${poll.mode==='test'?'测试':'正式'}投票：${poll.question}（${poll.state==='open'?'进行中':poll.state==='closed'?'已结束':'已取消'}）`:'当前没有可关联的投票；可去投票页手动发起。';
  controls();
 }
 async function refreshProgram(){
  const ticket=++sequence;
  try{const r=await fetch('/show-program-status',{cache:'no-store',signal:AbortSignal.timeout(5000)});const data=await r.json();if(!r.ok)throw Error(data.error||'节目单不可用');if(ticket===sequence)render(data);}
  catch(e){if(ticket!==sequence)return;online=false;$('#program-status').textContent='节目单连接中断 · 状态尚未更新';controls();}
 }
 async function command(name,extra={}){
  if(busy||!online||!snapshot?.show_id)return;
  // Preserve the other card editor's optimistic version until its draft is saved.
  if(typeof drafts!=='undefined'&&drafts.size){$('#notice').textContent='请先保存正在编辑的卡片，再推进节目单。';return;}
  busy=true;++sequence;controls();
  try{
   const r=await fetch('/shows-action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'program',id:snapshot.show_id,version:snapshot.version,command:name,...extra}),signal:AbortSignal.timeout(8000)});
   const result=await r.json();if(!r.ok)throw Error(result.error||'节目单操作未完成');
   $('#notice').textContent='节目单已更新';
   if(typeof refresh==='function')await refresh(true);
  }catch(e){$('#notice').textContent=e.name==='TimeoutError'?'操作尚未确认，请核对节目单后再操作。':e.message;}
  finally{busy=false;await refreshProgram();controls();}
 }
 $('#program-enable').onclick=()=>command('enable');
 $('#program-disable').onclick=()=>command('disable');
 $('#program-start').onclick=()=>command('start_card',{card_id:$('#program-card').value});
 $('#program-next').onclick=()=>command('next_stage');
 $('#program-finish').onclick=()=>command('finish_card');
 $('#program-attach').onclick=()=>command('attach_poll',{poll_id:snapshot?.available_poll?.id});
 $('#program-detach').onclick=()=>command('attach_poll',{poll_id:null});
 $('#program-card').onchange=controls;
 async function tick(){if(!busy)await refreshProgram();setTimeout(tick,2000);}tick();
})();
