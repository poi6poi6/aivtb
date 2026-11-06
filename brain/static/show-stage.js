'use strict';
(()=>{
 const $=s=>document.querySelector(s);
 if(new URLSearchParams(location.search).get('clean')==='1')document.documentElement.classList.add('clean');
 function stale(message){$('#stage-card').classList.add('stale');$('#connection').textContent=message;}
 function render(data){
  $('#theme').textContent=data.theme;
  $('#title').textContent=data.active?data.title||'等待主播选择栏目':'栏目休息中';
  $('#stage').textContent=data.stage_label;$('#status').textContent=data.status;
  $('#next').textContent=data.active&&data.next_label?'下一环节 · '+data.next_label:'';
  $('#pending').textContent=!data.active?'':Number.isInteger(data.pending_count)?`待处理委托 ${data.pending_count} 项`:'委托排队状态待确认';
  if(data.fresh){$('#stage-card').classList.remove('stale');$('#connection').textContent='栏目状态已同步';}
  else stale('状态过期 · 当前进度暂未确认');
 }
 async function tick(){
  try{const r=await fetch('/show-stage-status',{cache:'no-store',signal:AbortSignal.timeout(3500)});const data=await r.json();if(!r.ok)throw Error('unavailable');render(data);}
  catch(e){stale('连接中断 · 下方为最后一次状态，进度可能已过期');}
  setTimeout(tick,1500);
 }
 tick();
})();
