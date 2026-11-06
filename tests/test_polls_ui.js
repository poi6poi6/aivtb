const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs'),path=require('node:path');
const clone=x=>JSON.parse(JSON.stringify(x));
let online=true, calls=[], delayed=null;
let state={server_time:1000,current:null,history:[]};
function pollData(){return {id:'a'.repeat(16),program:'庭院连载',question:'<img src=x onerror=alert(1)>',mode:'test',state:'open',remaining:30,created:1000,total:2,
  options:[{number:1,label:'<b>温室</b>',votes:1,percent:50},{number:2,label:'书房',votes:1,percent:50}],winners:[]};}
class Element{
 constructor(){this.children=[];this.value='';this.hidden=false;this.disabled=false;this.style={};this.classes=new Set();this.classList={add:c=>this.classes.add(c),remove:c=>this.classes.delete(c)};}
 set innerHTML(_){throw Error('untrusted content must be rendered as text');}
 append(...items){this.children.push(...items);}
 replaceChildren(...items){this.children=items;}
}
function text(el){return (el.textContent||'')+el.children.map(text).join(' ');}
function page(clean=false){
 const elements=new Map(),timers=[];
 const element=s=>{if(!elements.has(s))elements.set(s,new Element());return elements.get(s);};
 const root=new Element();
 const ctx=vm.createContext({URLSearchParams,AbortSignal,location:{search:clean?'?clean=1':''},Date,console,
  document:{querySelector:element,createElement:()=>new Element(),documentElement:root},
  setTimeout:fn=>timers.push(fn),
  fetch:async(url,opts={})=>{
   if(!online)throw Error('offline');
   if(url.startsWith('/polls-status')){
    if(delayed){const d=delayed;delayed=null;return d;}
    const data=clone(state);return {ok:true,json:async()=>data};
   }
   assert.equal(url,'/polls-action');assert.equal(opts.method,'POST');
   const data=JSON.parse(opts.body);calls.push(data);
   if(data.action==='create')state.current=pollData();
   if(data.action==='close'){state.current.state='closed';state.current.remaining=0;state.current.winners=[1,2];}
   if(data.action==='cancel'){state.current.state='cancelled';state.current.winners=[];}
   return{ok:true,json:async()=>({message:data.action==='announce'?'已加入语音队列':'已更新'})};
  }});
 vm.runInContext(fs.readFileSync(path.join(__dirname,'../brain/static/polls.js'),'utf8'),ctx);
 return {ctx,element,root,timers};
}
(async()=>{
 const host=page(),obs=page(true);await new Promise(setImmediate);
 assert.equal(host.element('#create').disabled,false);
 assert.equal(host.element('#close').disabled,true);
 assert(obs.root.classes.has('clean'));
 assert.equal(obs.element('#connection').hidden,true);
 assert(text(host.element('#source-status')).includes('尚未确认连接'));
 state.source={state:'connected',room_id:123,last_heartbeat:999};await host.ctx.refresh();
 assert(text(host.element('#source-status')).includes('房间 123'));
 host.element('#preset').value='garden';host.element('#preset').onchange();
 assert.equal(host.element('#options').value,'温室\n书房\n花圃');
 host.element('#duration').value='30';host.element('#mode').value='test';
 await host.element('#create-poll').onsubmit({preventDefault(){}});
 assert.equal(calls[0].program,'庭院连载');assert.equal(calls[0].duration,30);
 assert.deepEqual(calls[0].options,['温室','书房','花圃']);
 assert.equal(host.element('#create').disabled,true);
 assert.equal(host.element('#test-vote').hidden,false);
 assert(text(host.element('#current')).includes('<img src=x onerror=alert(1)>'));
 await obs.ctx.refresh();
 assert.equal(obs.element('#test-vote').hidden,true);
 assert.equal(text(obs.element('#current')),text(host.element('#current')));
 host.element('#test-user').value='same';host.element('#test-choice').value='2';
 await host.element('#test-vote').onsubmit({preventDefault(){}});
 assert.equal(calls.at(-1).action,'test_vote');assert.equal(calls.at(-1).id,'a'.repeat(16));
 // An old GET finishing after an action must not resurrect the open poll.
 let resolveOld;const stale=clone(state);delayed=new Promise(resolve=>{resolveOld=resolve;});
 const oldRequest=host.ctx.refresh();
 await host.element('#close').onclick();
 resolveOld({ok:true,json:async()=>stale});await oldRequest;
 assert.equal(host.element('#close').disabled,true);
 assert(text(host.element('#current')).includes('并列最高票'));
 await host.element('#announce').onclick();
 assert.equal(calls.at(-1).action,'announce');
 assert.equal(host.element('#notice').textContent,'已加入语音队列');
 online=false;await host.ctx.refresh();await obs.ctx.refresh();
 assert.equal(host.element('#create').disabled,true);
 assert.equal(obs.element('#connection').hidden,false);
 assert(text(obs.element('#connection')).includes('最后一次统计'));
 online=true;state.history=[clone(state.current)];state.current={...pollData(),mode:'live',total:0,options:pollData().options.map(o=>({...o,votes:0,percent:0}))};
 await host.ctx.refresh();assert.equal(host.element('#test-vote').hidden,true);
 state.current.state='closed';await host.ctx.refresh();
 assert(text(host.element('#current')).includes('没有收到有效票'));
 assert(text(host.element('#history')).includes('并列最高票'));
 state.current.state='cancelled';await host.ctx.refresh();
 assert(text(host.element('#current')).includes('不选出结果'));
 console.log('PASS: poll forms, templates, literal text, OBS sync, stale response rejection, disconnect, ties and zero votes');
})().catch(error=>{console.error(error);process.exitCode=1;});
