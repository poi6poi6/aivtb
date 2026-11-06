const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs'),path=require('node:path');
class Element{constructor(){this.value='';this.textContent='';this.disabled=false;this.hidden=false;this.children=[];}replaceChildren(...items){this.children=items;}set innerHTML(_){throw Error('Use literal text');}}
const elements=new Map(),timers=[],commands=[],drafts=new Map();
const element=s=>{if(!elements.has(s))elements.set(s,new Element());return elements.get(s);};
let online=true,delay=null,editorRefreshes=0;
let snapshot={show_id:'s',version:3,program:{live_mode:true,card_id:'c',stage:'intro',poll_id:null},context:{live_mode:false,stage_label:'介绍委托',status:'介绍当前主题'},cards:[{id:'c',title:'<b>温室</b>'}],available_poll:{id:'p',question:'下什么天气？',state:'open',mode:'test'}};
const clone=x=>JSON.parse(JSON.stringify(x));
const context=vm.createContext({AbortSignal,drafts,refresh:async()=>{editorRefreshes++;},setTimeout:fn=>timers.push(fn),document:{querySelector:element,createElement:()=>new Element()},
 fetch:async(url,options={})=>{
  if(!online)throw Error('offline');
  if(url==='/show-program-status'){if(delay){const old=delay;delay=null;return old;}return{ok:true,json:async()=>clone(snapshot)};}
  assert.equal(url,'/shows-action');assert.equal(options.method,'POST');const data=JSON.parse(options.body);commands.push(data);assert.equal(data.action,'program');
  if(data.command==='enable'){snapshot.context.live_mode=true;snapshot.program.live_mode=true;}
  if(data.command==='disable'){snapshot.context.live_mode=false;snapshot.program.live_mode=false;}
  if(data.command==='next_stage'){snapshot.program.stage='vote';snapshot.context.stage_label='观众投票';}
  if(data.command==='finish_card')snapshot.program.stage='finished';
  if(data.command==='attach_poll')snapshot.program.poll_id=data.poll_id;
  snapshot.version++;return{ok:true,json:async()=>({version:snapshot.version})};
 }});
vm.runInContext(fs.readFileSync(path.join(__dirname,'../brain/static/shows-program.js'),'utf8'),context);
(async()=>{
 await new Promise(setImmediate);
 assert.equal(commands.length,0,'Reading a saved live preference must not enable it');
 assert(element('#program-status').textContent.includes('重启后请重新开启'));
 assert.equal(element('#program-card').children[0].textContent,'<b>温室</b>');
 await element('#program-enable').onclick();assert.equal(commands.at(-1).command,'enable');assert.equal(element('#program-enable').disabled,true);
 drafts.set('c',{});const count=commands.length;await element('#program-next').onclick();assert.equal(commands.length,count);assert(element('#notice').textContent.includes('请先保存'));drafts.clear();
 let release;const old=clone(snapshot);delay=new Promise(resolve=>release=resolve);const slow=timers.shift()();
 await element('#program-next').onclick();release({ok:true,json:async()=>old});await slow;
 assert(element('#program-status').textContent.includes('观众投票'));
 await element('#program-attach').onclick();assert.equal(commands.at(-1).poll_id,'p');
 await element('#program-finish').onclick();assert.equal(element('#program-next').disabled,true);
 assert(editorRefreshes>=4);
 online=false;await timers.shift()();assert.equal(element('#program-enable').disabled,true);assert(element('#program-status').textContent.includes('连接中断'));
 console.log('PASS: explicit live mode, restart rearm, draft protection, host-only progression, poll association, stale response and offline controls');
})().catch(e=>{console.error(e);process.exitCode=1;});
