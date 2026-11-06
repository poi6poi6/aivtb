/* Simulated DOM and HTTP only: no browser, model, Bilibili account or live draw. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const script = fs.readFileSync(path.join(__dirname, '../brain/static/tarot.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '../brain/static/tarot.html'), 'utf8');
const css = fs.readFileSync(path.join(__dirname, '../brain/static/tarot.css'), 'utf8');
const clone = value => JSON.parse(JSON.stringify(value));
const settle = () => new Promise(setImmediate);
const submit = {preventDefault() {}};

class Element {
  constructor(tag='div') {
    this.tagName=tag;this.children=[];this.textContent='';this.value='';this.hidden=false;
    this.disabled=false;this.style={};this.attributes={};this.className='';this.classes=new Set();
    this.classList={add:name=>this.classes.add(name),remove:name=>this.classes.delete(name)};
  }
  set innerHTML(_) { throw Error('Viewer text must never become HTML'); }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children=items; }
  setAttribute(name,value) { this.attributes[name]=String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
}
function content(element) { return (element.textContent||'')+element.children.map(content).join(' '); }
function descendants(element) { return [element,...element.children.flatMap(descendants)]; }
function hasClass(element,name) { return element.classes.has(name)||element.className.split(' ').includes(name); }
function card(position=7) {
  return {id:'star',name:'星星 <img src=x>',orientation:'reversed',position,label:'今晚的一点提醒',
    keywords:['微光','<b>整理</b>'],meaning:'把一个可以做到的小动作留给明天。'};
}
function page(clean=false) {
  const elements=new Map(),root=new Element('html'),timers=[],requests=[],calls=[];
  const element=selector=>{if(!elements.has(selector))elements.set(selector,new Element());return elements.get(selector);};
  for(const id of html.matchAll(/\bid="([^"]+)"/g))element('#'+id[1]);
  element('#mode').value='test';element('#spread').value='single';element('#hosting').value='guided';
  element('#test-user').value='夜归的旅人';element('#test-choose-user').value='夜归的旅人';
  const backend={online:true,state:{server_time:1000,current:null,entries:[],reading:null,history:[]},
    nextStatus:null,nextAction:null,error:null,entrySerial:0,readingSerial:0};
  function mutate(data) {
    const state=backend.state;
    if(data.action==='create')state.current={id:'session-1',mode:data.mode,spread:data.spread,hosting:data.hosting,state:'open',queue_count:0,created:1000};
    if(data.action==='test_join')state.entries.push({id:'entry-'+(++backend.entrySerial),user:data.user,question:data.question,state:'waiting'});
    if(data.action==='select') {
      const entry=state.entries.find(item=>item.id===data.entry_id);entry.state='selected';
      state.reading={id:'reading-'+(++backend.readingSerial),session_id:state.current.id,user:entry.user,question:entry.question,
        state:state.current.hosting==='guided'?'welcoming':'selecting',hosting:state.current.hosting,stage:state.current.hosting==='guided'?'welcome':'reading',revealed_count:0,
        spread:state.current.spread,card_count:state.current.spread==='three'?3:1,cards:[],positions:[],attempt_id:'private-attempt',error:''};
    }
    if(data.action==='draw'||data.action==='test_choose') {
      state.reading.state='revealed';state.reading.positions=data.positions;
      state.reading.cards=data.positions.map((number,index)=>({...card(number),label:state.reading.card_count===3?['眼下','阻碍','可尝试的方向'][index]:'今晚的一点提醒'}));
    }
    if(data.action==='interpret')state.reading.state='queued';
    if(data.action==='pause'){state.reading.state='paused';state.reading.paused=true;}
    if(data.action==='resume'){state.reading.state='awaiting_reply';state.reading.paused=false;state.reading.wait_deadline=1060;}
    if(data.action==='takeover'){state.reading.hosting='manual';state.reading.state=state.reading.cards.length?'revealed':'selecting';state.reading.paused=false;}
    if(data.action==='test_reply')state.reading.state=state.reading.state==='awaiting_confirmation'?'selecting':'closing';
    if(data.action==='confirm_delivered')state.reading.state='awaiting_reply';
    if(data.action==='finish') {
      state.reading.state='finished';
      const entry=state.entries.find(item=>item.state==='selected');if(entry)entry.state='done';
      state.history.push(clone(state.reading));
    }
    if(data.action==='close')state.current.state='closed';
    if(state.current)state.current.queue_count=state.entries.filter(item=>item.state==='waiting').length;
  }
  const context=vm.createContext({URLSearchParams,AbortSignal,location:{search:clean?'?clean=1':''},console,
    document:{querySelector:element,createElement:tag=>new Element(tag),createElementNS:(_,tag)=>new Element(tag),documentElement:root,
      querySelectorAll:selector=>selector==='.select-entry'?[...elements.values()].flatMap(descendants).filter(item=>hasClass(item,'select-entry')):[]},
    setTimeout:fn=>{timers.push(fn);return timers.length;},
    fetch:async(url,options={})=>{
      requests.push({url,options});
      if(!backend.online)throw new TypeError('network unavailable');
      if(url.startsWith('/tarot-status')) {
        if(backend.nextStatus){const promise=backend.nextStatus;backend.nextStatus=null;return promise;}
        return {ok:true,json:async()=>clone(backend.state)};
      }
      assert.equal(url,'/tarot-action');assert.equal(options.method,'POST');
      const data=JSON.parse(options.body);calls.push(data);
      if(backend.nextAction){const promise=backend.nextAction;backend.nextAction=null;return promise;}
      if(backend.error){const error=backend.error;backend.error=null;return{ok:false,json:async()=>({error})};}
      mutate(data);return{ok:true,json:async()=>({ok:true})};
    }});
  vm.runInContext(script,context);
  return {context,element,root,elements,timers,requests,calls,backend,mutate,
    selects:()=>descendants(element('#participants')).filter(item=>hasClass(item,'select-entry'))};
}
async function prepare(host,spread='single',hosting='manual') {
  host.element('#spread').value=spread;
  host.element('#hosting').value=hosting;
  await host.element('#create-form').onsubmit(submit);
  host.element('#test-question').value='明天怎样轻松一点？';
  await host.element('#test-form').onsubmit(submit);
  await host.selects()[0].onclick();
}

(async()=>{
  // All selectors in the implementation refer to actual page elements.
  const knownIds=new Set([...html.matchAll(/\bid="([^"]+)"/g)].map(match=>match[1]));
  for(const match of script.matchAll(/\$\(['"]#([^'"]+)['"]\)/g))assert(knownIds.has(match[1]),'missing HTML #'+match[1]);
  assert(css.includes('.clean .host-only{display:none!important}'));
  assert(css.includes('.clean,.clean body{background:transparent!important}'));
  assert(html.includes('问题会公开上屏'));
  assert(html.includes('最多 30 位排队'));
  assert(!script.includes('Math.random'),'the UI must never draw its own deck');
  assert(!/https?:\/\/[^'" ]+\.(?:png|jpg|svg)/.test(html+script),'art should stay repo-native');

  const host=page(),obs=page(true);await settle();
  assert.equal(host.element('#create').disabled,false);
  assert.equal(host.element('#draw').disabled,true);
  assert.equal(host.calls.length,0,'reading the page cannot create or announce a round');
  assert(obs.root.classes.has('clean'));
  assert.equal(obs.requests[0].url,'/tarot-status?clean=1');
  assert.equal(obs.element('#create-form').onsubmit,undefined);
  await obs.context.action({action:'create',mode:'live'});
  assert.equal(obs.calls.length,0,'overlay cannot POST even if action is called directly');

  // Static inputs survive background state updates, including keyboard focus.
  host.element('#test-question').value='还没写完 <b>自己的问题</b>';
  host.element('#positions').value='12';
  host.element('#mode').value='live';
  await host.context.refresh();
  assert.equal(host.element('#test-question').value,'还没写完 <b>自己的问题</b>');
  assert.equal(host.element('#positions').value,'12');
  assert.equal(host.element('#mode').value,'live');
  host.element('#mode').value='test';
  await prepare(host);
  assert.deepEqual(host.calls[0],{action:'create',mode:'test',spread:'single',hosting:'manual'});
  assert.equal(host.calls[1].action,'test_join');
  assert.equal(host.calls[2].entry_id,'entry-1');
  assert.equal(host.element('#create').disabled,true);
  assert.equal(host.element('#test-form').hidden,false);
  assert.equal(host.element('#draw').disabled,false);
  assert.equal(host.element('#interpret').disabled,true);
  assert(content(host.element('#cards')).includes('待揭晓'));
  assert(!content(host.element('#cards')).includes('星星'));

  // Unrevealed card data is not disclosed even if an invalid response includes it.
  host.backend.state.reading.cards=[card()];
  await host.context.refresh();
  assert(!content(host.element('#cards')).includes('星星'));
  host.backend.state.reading.cards=[];
  const beforeInvalid=host.calls.length;
  for(const value of ['0','23','1 2','1.5','<svg>']) {
    host.element('#positions').value=value;
    await host.element('#draw-form').onsubmit(submit);
    assert.equal(host.calls.length,beforeInvalid);
  }
  host.element('#positions').value='7';
  await host.element('#draw-form').onsubmit(submit);
  assert.deepEqual(host.calls.at(-1),{action:'draw',session_id:'session-1',reading_id:'reading-1',positions:[7]});
  assert.equal(host.element('#draw').disabled,true);
  assert.equal(host.element('#interpret').disabled,false);
  assert(content(host.element('#cards')).includes('星星 <img src=x>'));
  assert(content(host.element('#cards')).includes('<b>整理</b>'));
  assert(content(host.element('#cards')).includes('逆位'));
  assert(descendants(host.element('#cards')).some(item=>item.tagName==='svg'&&hasClass(item,'reversed')));
  assert(!descendants(host.element('#cards')).some(item=>item.tagName==='img'));
  assert.equal(host.calls.filter(item=>item.action==='interpret').length,0,'revealing does not automatically speak');

  obs.backend.state=clone(host.backend.state);
  obs.backend.state.entries=[{user:'PRIVATE WAITING VIEWER',question:'PRIVATE WAITING QUESTION'}];
  obs.backend.state.history=[{user:'PRIVATE HISTORY'}];
  obs.backend.state.reading.attempt_id='SECRET ATTEMPT';
  obs.backend.state.reading.error='SECRET ERROR';
  await obs.context.refresh();
  assert.equal(content(obs.element('#cards')),content(host.element('#cards')),'OBS shows the same actual faces');
  assert.equal(obs.element('#connection').hidden,true);
  assert.equal(obs.element('#test-form').hidden,true);
  const rendered=[...obs.elements.values()].map(content).join(' ');
  for(const secret of ['PRIVATE WAITING','PRIVATE HISTORY','SECRET ATTEMPT','SECRET ERROR'])assert(!rendered.includes(secret));
  assert(obs.requests.every(request=>request.options.method===undefined));

  // A stale GET that started before a mutation cannot replace the new stage.
  const stale=clone(host.backend.state);
  let releaseStatus;
  host.backend.nextStatus=new Promise(resolve=>releaseStatus=resolve);
  const oldGet=host.context.refresh();
  await host.element('#interpret').onclick();
  releaseStatus({ok:true,json:async()=>stale});await oldGet;
  assert.equal(host.element('#interpret').disabled,true);
  assert.equal(host.element('#finish').disabled,false,'host can end a queued interpretation');
  assert(content(host.element('#stage-note')).includes('尚未确认已经播出'));
  host.backend.state.reading.state='generated';await host.context.refresh();
  assert.equal(host.element('#finish').disabled,false);
  assert(content(host.element('#stage-note')).includes('解读已生成'));
  assert(content(host.element('#stage-note')).includes('确认'));
  assert(!content(host.element('#stage-note')).includes('已送达'));
  assert(!content(host.element('#stage-note')).includes('已播完'));

  // Failed/interrupted attempts preserve the same cards and IDs for explicit retry.
  host.backend.state.reading.state='interrupted';await host.context.refresh();
  assert.equal(host.element('#interpret').disabled,false);
  assert.equal(host.element('#interpret').textContent,'用同一组牌重试解读');
  const cardsBeforeRetry=content(host.element('#cards'));
  host.backend.error='现在不能解读 <b>请稍后</b>';
  await host.element('#interpret').onclick();
  assert.equal(host.element('#notice').textContent,'现在不能解读 <b>请稍后</b>');
  assert.equal(content(host.element('#cards')),cardsBeforeRetry);
  assert.equal(host.calls.at(-1).reading_id,'reading-1');

  // Busy guard prevents duplicate POST and protects the captured session/reading.
  let releaseAction;
  host.backend.nextAction=new Promise(resolve=>releaseAction=resolve);
  const inFlight=host.element('#interpret').onclick();await settle();
  const actionCount=host.calls.length;
  assert.equal(host.element('#interpret').disabled,true);
  await host.element('#interpret').onclick();
  assert.equal(host.calls.length,actionCount);
  const requestCount=host.requests.length;
  await host.context.refresh();
  assert.equal(host.requests.length,requestCount,'polling pauses while an action owns the IDs');
  releaseAction({ok:true,json:async()=>({ok:true})});await inFlight;
  assert.equal(host.calls.at(-1).reading_id,'reading-1');

  // Timeout has an uncertain-result notice and does not retry the action.
  host.backend.nextAction=Promise.reject(Object.assign(new Error('timeout'),{name:'TimeoutError'}));
  const timeoutCount=host.calls.length;
  await host.element('#interpret').onclick();
  assert.equal(host.calls.length,timeoutCount+1);
  assert(host.element('#notice').textContent.includes('尚未确认'));
  assert(host.element('#notice').textContent.includes('不会自动重发'));

  host.backend.online=false;obs.backend.online=false;
  await host.context.refresh();await obs.context.refresh();
  assert.equal(host.element('#interpret').disabled,true);
  assert.equal(host.element('#test-join').disabled,true);
  assert.equal(obs.element('#connection').hidden,false);
  assert(obs.element('#table').classes.has('stale'));
  assert(content(obs.element('#cards')).includes('星星'));
  assert(obs.element('#connection').textContent.includes('最后确认'));
  const offlineCount=host.calls.length;await host.element('#interpret').onclick();assert.equal(host.calls.length,offlineCount);

  host.backend.online=true;await host.context.refresh();
  await host.element('#finish').onclick();
  assert.equal(host.element('#finish').disabled,true);
  assert(content(host.element('#history')).includes('星星 <img src=x>'));
  await host.element('#close').onclick();
  assert.equal(host.element('#create').disabled,false);
  assert.equal(host.element('#test-form').hidden,true);

  // Three positions are distinct integers; test choose uses the test identity.
  const three=page();await settle();await prepare(three,'three');
  const threeCount=three.calls.length;
  three.element('#positions').value='3 3 18';await three.element('#draw-form').onsubmit(submit);
  assert.equal(three.calls.length,threeCount);
  three.element('#test-positions').value='3，12、18';
  await three.element('#test-choose-form').onsubmit(submit);
  assert.deepEqual(three.calls.at(-1),{action:'test_choose',session_id:'session-1',user:'夜归的旅人',positions:[3,12,18]});
  assert.equal(three.element('#cards').children.length,3);
  assert(content(three.element('#cards')).includes('可尝试的方向'));
  three.backend.state.current.mode='live';three.backend.state.reading.state='selecting';
  await three.context.refresh();
  assert.equal(three.element('#test-form').hidden,true);
  assert.equal(three.element('#test-choose-form').hidden,true);
  const liveCount=three.calls.length;await three.element('#test-form').onsubmit(submit);assert.equal(three.calls.length,liveCount);
  await three.element('#test-choose-form').onsubmit(submit);assert.equal(three.calls.length,liveCount);
  // Guided hosting is explicit; every waiting transition comes from the server.
  assert(html.includes('<option value="guided">'));
  assert(html.includes('测试场也会真实调用'));
  const guided=page();await settle();await prepare(guided,'three','guided');
  assert.equal(guided.calls[0].hosting,'guided');
  assert.equal(guided.element('#card-choices').children.length,22);
  assert(guided.element('#card-choices').children.every(choice=>choice.disabled));
  assert.equal(guided.element('#interpret').hidden,true);
  assert.equal(guided.element('#test-reply').disabled,true);
  guided.backend.state.reading.state='awaiting_confirmation';
  guided.backend.state.reading.wait_deadline=1060;
  await guided.context.refresh();
  assert(guided.element('#audience-instruction').textContent.includes('确认'));
  assert(guided.element('#wait-clock').textContent.includes('60 秒'));
  guided.element('#test-reply-user').value='夜归的旅人';guided.element('#test-reply-text').value='对，就是这个问题';
  await guided.element('#test-reply-form').onsubmit(submit);
  assert.deepEqual(guided.calls.at(-1),{action:'test_reply',session_id:'session-1',user:'夜归的旅人',text:'对，就是这个问题'});
  const choiceButtons=guided.element('#card-choices').children;
  for(const index of [2,11,17])choiceButtons[index].onclick();
  assert.equal(guided.element('#positions').value,'3 12 18');
  assert.equal(choiceButtons.filter(choice=>hasClass(choice,'selected')).length,3);
  const priorDraws=guided.calls.length;
  choiceButtons[3].onclick();assert.equal(guided.calls.length,priorDraws,'selecting a card is a draft, not an automatic draw');
  assert.equal(guided.element('#positions').value,'3 12 18');
  await guided.element('#draw-form').onsubmit(submit);
  assert.equal(guided.element('#card-choices').hidden,true);
  assert(!content(guided.element('#cards')).includes('星星'),'a chosen face waits for actual playback');
  assert(content(guided.element('#cards')).includes('NO. 03'));
  assert(guided.element('#audience-instruction').textContent.includes('3、12、18'));
  guided.backend.state.reading.stage='reading';guided.backend.state.reading.state='generated';
  guided.backend.state.reading.revealed_count=1;
  await guided.context.refresh();
  const firstReveal=guided.element('#cards').children;
  assert.equal(firstReveal.filter(item=>hasClass(item,'newly-revealed')).length,1);
  assert.equal(firstReveal.filter(item=>content(item).includes('星星')).length,1);
  await guided.context.refresh();
  assert.equal(guided.element('#cards').children,firstReveal,'polling must not restart a reveal');
  guided.backend.state.reading.state='awaiting_reply';guided.backend.state.reading.revealed_count=3;
  await guided.context.refresh();
  assert.equal(guided.element('#cards').children.filter(item=>hasClass(item,'newly-revealed')).length,2);
  assert(guided.element('#audience-instruction').textContent.includes('回应'));
  await guided.element('#pause').onclick();
  assert.equal(guided.element('#test-reply').disabled,true);
  assert(guided.element('#wait-clock').textContent.includes('暂停'));
  await guided.element('#resume').onclick();
  assert.equal(guided.element('#test-reply').disabled,false);
  guided.backend.state.reading.state='awaiting_host';guided.backend.state.reading.display_text='这是最终文字 <b>不会成为 HTML</b>';
  await guided.context.refresh();
  assert.equal(guided.element('#text-delivery').hidden,false);
  assert(content(guided.element('#display-text')).includes('最终文字'));
  const publicGuided=page(true);await settle();publicGuided.backend.state=clone(guided.backend.state);
  await publicGuided.context.refresh();
  assert.equal(publicGuided.element('#text-delivery').hidden,true);
  assert(![...publicGuided.elements.values()].map(content).join(' ').includes('最终文字'));
  assert(!content(publicGuided.element('#round-state')).includes('生成'));
  assert(publicGuided.element('#card-choices').children.every(choice=>choice.onclick===undefined));
  await guided.element('#confirm-delivered').onclick();
  assert.equal(guided.calls.at(-1).action,'confirm_delivered');
  await guided.element('#takeover').onclick();
  assert.equal(guided.element('#pause').hidden,true);
  assert.equal(guided.element('#interpret').hidden,false);
  assert.equal(guided.element('#interpret').disabled,false);
  assert(css.includes('@media(prefers-reduced-motion:reduce)'));
  assert(css.includes('.newly-revealed .card-face{animation:none}'));
  console.log('PASS: tarot host flow, server-owned faces, readonly OBS, public text isolation, draft preservation, ID guards, reveal and playback truthfulness, retry, errors and reconnect');
})().catch(error=>{console.error(error);process.exitCode=1;});
