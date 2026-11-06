'use strict';
const $ = s => document.querySelector(s);
const clean = new URLSearchParams(location.search).get('clean') === '1';
if (clean) document.documentElement.classList.add('clean');
const presets = {
  commission:['观众委托所','画里的窗外，选雪还是雨？',['雪','雨']],
  garden:['庭院连载','下一次为庭院添哪一处？',['温室','书房','花圃']],
  tea:['旧物茶会','今晚聊哪一种旧物？',['童年的玩具','一直留着的信','旅行纪念品']],
  challenge:['互动小挑战','下一轮 Minecraft 小挑战做什么？',['收集木头','制作工作台']]
};
let current = null, online = false, busy = false, sequence = 0, signature = '';
function node(tag, text, cls){const el=document.createElement(tag);if(text!==undefined)el.textContent=text;if(cls)el.className=cls;return el;}
function label(p){return p.state==='open'?'投票进行中':p.state==='cancelled'?'本轮已取消':'投票已结束';}
function resultText(p){
  if(p.state==='cancelled')return '本轮已取消，不选出结果。';
  if(p.state==='open')return '弹幕发送「投票 1」或对应编号 · 每人一票，可改票';
  if(!p.total)return '没有收到有效票，暂不选出结果。';
  const winners=p.options.filter(o=>p.winners.includes(o.number)).map(o=>o.label).join('、');
  return p.winners.length>1?`并列最高票：${winners} · 等待主播决定`:`最高票：${winners}`;
}
function card(p, history=false){
  const box=node('div');
  const badges=node('div');badges.append(node('span',p.program,'badge'),node('span',p.mode==='test'?'测试投票':'正式投票','badge'+(p.mode==='test'?' test':'')));
  box.append(badges,node(history?'h3':'h2',p.question));
  const meta=node('div',undefined,'poll-meta');
  meta.append(node('span',label(p)+(p.state==='open'?` · 剩余 ${p.remaining} 秒`:'')),node('span',`${p.total} 人参与`));box.append(meta);
  for(const o of p.options){
    const row=node('div',undefined,'option'+(p.winners.includes(o.number)?' winner':''));
    const title=node('div',undefined,'option-title');title.append(node('strong',`${o.number}. ${o.label}`),node('span',`${o.votes} 票 · ${o.percent}%`));
    const track=node('div',undefined,'track'),fill=node('div',undefined,'fill');fill.style.width=Math.max(0,Math.min(100,o.percent))+'%';track.append(fill);row.append(title,track);box.append(row);
  }
  box.append(node('p',resultText(p),'result'));
  if(!clean&&p.state==='closed'){
    const link=node('a','按结果生成绘画委托');link.href='/shows?poll='+encodeURIComponent(p.id);box.append(link);
  }
  if(history)box.append(node('small',new Date(p.created*1000).toLocaleString('zh-CN',{hour12:false})));
  return box;
}
function controls(){
  const open=current?.state==='open';
  $('#create').disabled=!online||busy||open;
  for(const id of ['#close','#cancel'])$(id).disabled=!online||busy||!open;
  $('#announce').disabled=!online||busy||!current;
  $('#test-vote').hidden=clean||!open||current?.mode!=='test';
  $('#test-submit').disabled=!online||busy;
}
function render(state){
  online=true;current=state.current;
  const source=state.source||{}, recent=source.last_heartbeat&&state.server_time-source.last_heartbeat<90;
  $('#source-status').textContent=source.state==='connected'&&recent?`B站弹幕已连接 · 房间 ${source.room_id}`:'B站弹幕尚未确认连接 · 正式投票需要主控连上直播间';
  $('#connection').textContent='已连接 · 每秒更新';$('#connection').hidden=clean;
  $('#current').classList.remove('stale');
  $('#current').replaceChildren(current?card(current):node('p','还没有投票，准备好后发起第一轮吧。','empty'));
  const next=JSON.stringify(state.history);
  if(!clean&&next!==signature){signature=next;$('#history').replaceChildren(...(state.history.length?state.history.map(p=>{
    const item=node('article',undefined,'history-card');item.append(card(p,true));return item;
  }):[node('p','暂无历史记录','hint')]));}
  controls();
}
async function refresh(){
  const request=++sequence;
  try{
    const response=await fetch('/polls-status'+(clean?'?clean=1':''),{cache:'no-store',signal:AbortSignal.timeout(5000)});
    const state=await response.json();if(!response.ok)throw Error(state.error||'统计暂不可用');
    if(request===sequence)render(state);
  }catch(error){
    if(request!==sequence)return;
    online=false;$('#connection').hidden=false;
    $('#connection').textContent='连接中断 · 下方是最后一次统计，倒计时已暂停显示';
    $('#current').classList.add('stale');controls();
  }
}
async function action(data){
  if(busy)return;
  busy=true;++sequence;controls();
  try{
    const response=await fetch('/polls-action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data),signal:AbortSignal.timeout(8000)});
    const result=await response.json();if(!response.ok)throw Error(result.error||'操作未完成');
    $('#notice').textContent=result.message||'已更新';
  }catch(error){$('#notice').textContent=error.name==='TimeoutError'?'请求超时，请检查最新统计后再操作。':error.message;}
  finally{busy=false;await refresh();controls();}
}
$('#preset').onchange=()=>{const preset=presets[$('#preset').value];if(preset){$('#program').value=preset[0];$('#question').value=preset[1];$('#options').value=preset[2].join('\n');}};
$('#create-poll').onsubmit=e=>{e.preventDefault();return action({action:'create',program:$('#program').value,question:$('#question').value,options:$('#options').value.split(/\r?\n/).map(s=>s.trim()).filter(Boolean),duration:Number($('#duration').value),mode:$('#mode').value});};
$('#close').onclick=()=>current&&action({action:'close',id:current.id});
$('#cancel').onclick=()=>current&&action({action:'cancel',id:current.id});
$('#announce').onclick=()=>current&&action({action:'announce',id:current.id});
$('#test-vote').onsubmit=e=>{e.preventDefault();return current&&action({action:'test_vote',id:current.id,user:$('#test-user').value,choice:$('#test-choice').value});};
async function poll(){if(!busy)await refresh();setTimeout(poll,1000);}poll();
