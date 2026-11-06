const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs'),path=require('node:path');
class Element{
  constructor(tag='div'){this.tag=tag;this.children=[];this.style={};this.dataset={};this.value='';this.textContent='';this.disabled=false;}
  set innerHTML(_){throw Error('Question content must render as text');}
  append(...children){this.children.push(...children);}
  replaceChildren(...children){this.children=children;}
  setAttribute(){}
  querySelector(selector){return this.children.find(e=>selector==='.'+e.className)||null;}
}
const walk=e=>[e,...e.children.flatMap(walk)],text=e=>walk(e).map(n=>n.textContent).join(' ');
const clone=data=>JSON.parse(JSON.stringify(data));
async function hooks(){
  let display=false,guest=false,fail=false,releasePin;const calls=[];const document={documentElement:{classList:{contains:()=>display}},querySelector:()=>guest?{}:null,createElement:tag=>new Element(tag)};
  const context={window:{},document,AbortSignal,fetch:async(url,options)=>{calls.push(JSON.parse(options.body));if(fail)throw Error('offline');await new Promise(resolve=>{releasePin=resolve;});return{ok:true,json:async()=>({state:'waiting'})};}};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../brain/static/question-pin.js'),'utf8'),context);
  const row=new Element(),event={kind:'chat',question_pin_token:'trusted-token',text:'spoof',viewer_key:'never-send'};
  context.window.QuestionPin.attach(row,event);context.window.QuestionPin.attach(row,event);assert.equal(row.children.length,2);
  fail=true;await row.children[0].onclick();assert.equal(row.children[0].disabled,false);assert.equal(row.questionPinPending,false);
  fail=false;const pinning=row.children[0].onclick();assert.equal(row.questionPinPending,true);
  releasePin();await pinning;assert.equal(row.children[0].textContent,'已固定');assert.equal(row.questionPinPending,false);
  assert.deepEqual(calls,[{action:'pin',token:'trusted-token'},{action:'pin',token:'trusted-token'}]);
  assert.equal(row.children[1].children[0].href,'/questions');
  display=true;const obs=new Element();context.window.QuestionPin.attach(obs,event);assert.equal(obs.children.length,0);
  display=false;guest=true;const publicRow=new Element();context.window.QuestionPin.attach(publicRow,event);assert.equal(publicRow.children.length,0);
}
async function page(){
  const elements=new Map(),made=[],calls=[];let online=true,loseManual=true,serial=0,oldGet=null;
  const node=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};node('#filter').value='active';
  const state={cards:[],can_ask:true,can_cancel:true};
  const context=vm.createContext({AbortSignal,Date,console,setTimeout(){},crypto:{randomUUID:()=>String(++serial).padStart(32,'0')},
    document:{querySelector:node,querySelectorAll:()=>[...made,...elements.values()].filter(e=>e.tag==='button'),createElement:tag=>{const e=new Element(tag);made.push(e);return e;}},
    fetch:async(url,options={})=>{if(!online)throw Error('offline');if(url==='/questions-status'){if(oldGet){const promise=oldGet;oldGet=null;return promise;}return{ok:true,json:async()=>clone(state)};}
      const data=JSON.parse(options.body);calls.push(data);
      if(data.action==='manual'){if(!state.cards.length)state.cards.push({id:'question',text:data.text,origin:{kind:'manual'},state:'waiting',version:1,created:1});if(loseManual){loseManual=false;const error=Error('lost');error.name='TimeoutError';throw error;}}
      else{const card=state.cards[0];if(data.version!==card.version)return{ok:false,json:async()=>({error:'版本变化'})};if(data.action==='ask')card.state='answering';if(data.action==='move')card.state=data.state;card.version++;}
      return{ok:true,json:async()=>clone(state.cards[0])};
    }});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../brain/static/questions.js'),'utf8'),context);
  await new Promise(setImmediate);
  node('#manual-text').value='<img src=x onerror=alert(1)> 要问的问题';const submit=()=>node('#manual-form').onsubmit({preventDefault(){}});
  await submit();assert.match(node('#notice').textContent,/可能已提交/);assert(text(node('#cards')).includes('<img src=x onerror=alert(1)>'));
  await submit();assert.equal(calls[0].request_key,calls[1].request_key,'Lost manual receipt retries same key');assert.equal(serial,1);assert.equal(node('#manual-text').value,'');
  const button=label=>walk(node('#cards')).find(e=>e.tag==='button'&&e.textContent===label);
  await button('现在回答').onclick();assert.equal(state.cards[0].state,'answering');assert(!button('已答'));
  await button('停止回答，留待答').onclick();assert.equal(state.cards[0].state,'waiting');assert.equal(calls.at(-1).state,'waiting');
  let release;const previous=clone(state);oldGet=new Promise(resolve=>release=resolve);const stale=context.refresh();
  await button('暂放').onclick();release({ok:true,json:async()=>previous});await stale;assert(text(node('#cards')).includes('暂放'));assert.equal(state.cards[0].state,'parked');
  online=false;await context.refresh();assert.match(node('#connection').textContent,/连接中断/);assert(button('现在回答').disabled);
}
(async()=>{await hooks();await page();console.log('PASS: question pin privacy, OBS/guest exclusion, literal rendering, lost-response dedup, truthful answering, cancellation, stale GET and offline controls');})().catch(error=>{console.error(error);process.exitCode=1;});
