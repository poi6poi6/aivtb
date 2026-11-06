const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs'),path=require('node:path');
const clone=x=>JSON.parse(JSON.stringify(x));
class Element{
 constructor(tag='div'){this.tag=tag;this.children=[];this.value='';this.hidden=false;this.disabled=false;}
 set innerHTML(_){throw Error('Show content must be rendered as text');}
 append(...children){this.children.push(...children);}
 replaceChildren(...children){this.children=children;}
 reportValidity(){return true;}
}
const text=e=>(e.textContent||'')+e.children.map(text).join(' ');
const walk=e=>[e,...e.children.flatMap(walk)];
const elements=new Map(),made=[],calls=[];
const element=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
let online=true,lost=false,stale=null,state={current:null,history:[]};
const poll={id:'a'.repeat(16),state:'closed',program:'庭院',question:'<img src=x onerror=alert(1)>',mode:'test',total:0,winners:[],options:[{number:1,label:'温室',votes:0},{number:2,label:'书房',votes:0}]};
const ctx=vm.createContext({URLSearchParams,AbortSignal,Date,console,location:{search:'?poll='+poll.id},history:{replaceState(){}},setTimeout(){},
 document:{querySelector:element,querySelectorAll:()=>[...made,...elements.values()].filter(e=>e.tag==='button'),createElement:tag=>{const e=new Element(tag);made.push(e);return e;}},
 fetch:async(url,opts={})=>{
  if(!online)throw Error('offline');
  if(url.startsWith('/shows-poll/'))return{ok:true,json:async()=>({poll:clone(poll),choice:null,prompt:''})};
  if(url==='/shows-status'){if(stale){const old=stale;stale=null;return old;}const result=clone(state);return{ok:true,json:async()=>result};}
  assert.equal(url,'/shows-action');const data=JSON.parse(opts.body);calls.push(data);
  const s=state.current;
  if(data.action!=='start'&&data.version!==s.version)return{ok:false,json:async()=>({error:'版本已变化'})};
  if(data.action==='start')state.current={id:'show',version:1,state:'open',created:1,theme:data.theme,cards:[]};
  if(data.action==='add'){s.cards.push({id:'card'+s.cards.length,title:data.title,prompt:data.prompt,name:'',note:'',next_todo:'',drawing_root:null,submission:null,...(data.poll_id?{poll:clone(poll),choice:data.choice,decision:'主播选择（零票）'}:{})});s.version++;}
  if(data.action==='edit'){const c=s.cards.find(c=>c.id===data.card_id);for(const key of ['title','prompt','name','note','next_todo'])if(key in data)c[key]=data[key];s.version++;}
  if(data.action==='submit'){const c=s.cards.find(c=>c.id===data.card_id);c.submission={state:'pending'};s.version++;if(lost){lost=false;return{ok:false,json:async()=>({error:'回执丢失，请核对提交'})};}c.drawing_root='drawing-root';c.submission.state='submitted';}
  if(data.action==='close'){s.state='closed';s.recap='本场事实回顾';state.history.push(clone(s));state.current=null;}
  return{ok:true,json:async()=>clone(state.current||s)};
 }});
vm.runInContext(fs.readFileSync(path.join(__dirname,'../brain/static/shows.js'),'utf8'),ctx);
const event={preventDefault(){}};
const cards=()=>element('#cards').children;
const form=card=>card.children.find(e=>e.tag==='form');
const input=(card,label)=>form(card).children.find(e=>e.textContent===label).children[0];
const button=(card,label)=>walk(card).find(e=>e.tag==='button'&&e.textContent===label);
(async()=>{
 await new Promise(setImmediate);
 assert.equal(element('#add-card').disabled,true);
 assert(text(element('#poll-summary')).includes('<img src=x onerror=alert(1)>'));
 element('#theme').value='庭院连载';await element('#start-form').onsubmit(event);
 assert.equal(element('#add-card').disabled,true,'Zero votes must not silently choose');
 element('#poll-choice').value='2';element('#poll-choice').onchange();
 assert.equal(element('#add-card').disabled,false);
 assert(element('#card-prompt').value.includes('书房'));
 await element('#add-form').onsubmit(event);
 assert.equal(calls.at(-1).choice,2);assert(text(cards()[0]).includes('主播选择（零票）'));
 element('#card-title').value='第二张';element('#card-prompt').value='画树';await element('#add-form').onsubmit(event);
 input(cards()[0],'作品命名').value='雪夜';input(cards()[0],'作品命名').oninput();
 input(cards()[1],'下次待办').value='下次加鸟';input(cards()[1],'下次待办').oninput();
 await ctx.refresh();assert.equal(input(cards()[0],'作品命名').value,'雪夜');
 await form(cards()[0]).onsubmit(event);
 assert.equal(input(cards()[1],'下次待办').value,'下次加鸟','Saving one card must not erase another draft');
 await form(cards()[1]).onsubmit(event);
 assert.equal(state.current.cards[1].next_todo,'下次加鸟');
 lost=true;await button(cards()[0],'保存并开始画草稿').onclick();
 assert(text(element('#notice')).includes('回执丢失'));
 assert(button(cards()[0],'核对提交'));
 await button(cards()[0],'核对提交').onclick();
 assert(walk(cards()[0]).some(e=>e.tag==='a'&&e.href==='/drawing?job=drawing-root'));
 online=false;await ctx.refresh();assert.equal(element('#add-card').disabled,true);
 online=true;await ctx.refresh();
 state.current.cards[0].drawing={drawing:{job_id:'old-version',version:'fixed',stage:'sketch',title:'旧作'},latest:{state:'succeeded'}};
 // A slow earlier GET cannot restore an ended session.
 let release;const old=clone(state);stale=new Promise(resolve=>release=resolve);const request=ctx.refresh();
 await element('#end').onclick();release({ok:true,json:async()=>old});await request;
 assert.equal(element('#active-panel').hidden,true);
 assert(text(element('#history')).includes('本场事实回顾'));
 assert(text(element('#history')).includes('接续待办'));
 assert(walk(element('#history')).some(e=>e.tag==='a'&&e.href==='/shows-drawing?show=show&card=card0'));
 await element('#start-form').onsubmit(event);
 element('#card-title').value='无法确认的绘画';element('#card-prompt').value='画猫';await element('#add-form').onsubmit(event);
 lost=true;await button(cards()[0],'保存并开始画草稿').onclick();
 assert.equal(element('#end-unconfirmed').hidden,false);
 await element('#end-unconfirmed').onclick();
 assert.equal(calls.at(-1).allow_unconfirmed,true);
 assert(text(element('#history')).includes('收播不会取消后台任务'));
 console.log('PASS: show forms, explicit poll choice, literal text, independent drafts, lost submission receipt, canvas link, offline controls, stale GET');
})().catch(e=>{console.error(e);process.exitCode=1;});
