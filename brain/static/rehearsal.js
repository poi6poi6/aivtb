(function(root){
  'use strict';
  const labels={danmaku:'直播弹幕',llm:'真实模型短句',tts:'语音合成',playback:'主播放页回执',microphone:'人工 · 麦克风与听感',obs_audio:'人工 · OBS 声音'};
  const states={pending:'未确认',running:'进行中',passed:'通过',failed:'未通过',skipped:'本轮未启用'};
  function resultLabel(data){
    if(data.running)return data.state==='cancelling'?'正在停止本轮演练…':'正在演练，请等待当前步骤完成';
    if(data.full_passed===true&&data.automatic_passed===true&&['microphone','obs_audio'].every(k=>data.steps?.[k]?.state==='passed'))return '本轮已启用链路演练通过（含主播人工确认）';
    if(data.automatic_passed)return data.state==='needs_attention'?'人工检查发现问题，尚未通过':'自动检查已完成，仍需人工确认';
    return data.message || (data.available?'尚未开始演练':'开播演练尚未就绪');
  }
  function canConfirm(data){return !!(data?.run_id&&data.automatic_passed&&!data.running);}
  if(typeof module!=='undefined'&&module.exports)module.exports={resultLabel,canConfirm};
  if(typeof document==='undefined')return;
  const el=id=>document.getElementById(id), node=(tag,text,cls)=>{const n=document.createElement(tag);n.textContent=text;if(cls)n.className=cls;return n;};
  let state=null,online=false,busy=false,sequence=0;
  function controls(){el('start').disabled=busy||!online||!state?.available||state.running;el('cancel').disabled=busy||!online||!state?.running||state.state==='cancelling';document.querySelectorAll('[data-field]').forEach(b=>{b.disabled=busy||!online||!canConfirm(state);});}
  function render(data){state=data;el('result').textContent=resultLabel(data);el('result').className=data.full_passed?'passed':data.state==='failed'||data.state==='needs_attention'?'failed':'';el('run-time').textContent=data.started_at?`开始于 ${new Date(data.started_at*1000).toLocaleTimeString()}`:'';el('steps').replaceChildren(...Object.entries(labels).map(([key,label])=>{const item=data.steps?.[key]||{state:'pending',detail:''},li=node('li',''),detail=node('div','');detail.append(node('span',states[item.state]||'未确认','badge '+item.state),node('small',item.detail||''));li.append(node('strong',label),detail);return li;}));controls();}
  async function request(url,options){const response=await fetch(url,{cache:'no-store',signal:AbortSignal.timeout(8000),...options});const data=await response.json();if(!response.ok)throw new Error(data.error||(response.status===403?'此页面仅供本机主播使用':'演练服务暂不可用'));return data;}
  async function refresh(){const serial=++sequence;try{const data=await request('/rehearsal-status');if(serial!==sequence)return;online=true;render(data);}catch(error){if(serial!==sequence)return;online=false;el('result').textContent='主控连接中断，不能确认演练通过';el('result').className='failed';controls();}}
  async function action(data){if(busy)return;busy=true;++sequence;controls();try{const result=await request('/rehearsal-action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});online=true;render(result);el('notice').textContent='';}catch(error){el('notice').textContent=error.message;}finally{busy=false;await refresh();controls();}}
  el('start').onclick=()=>action({action:'start'});
  el('cancel').onclick=()=>state?.run_id&&action({action:'cancel',run_id:state.run_id});
  document.querySelectorAll('[data-field]').forEach(button=>{button.onclick=()=>state?.run_id&&action({action:'confirm',run_id:state.run_id,field:button.dataset.field,passed:button.dataset.passed==='true'});});
  async function poll(){if(!busy)await refresh();root.setTimeout(poll,1000);}poll();
})(typeof window!=='undefined'?window:globalThis);
