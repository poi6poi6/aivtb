'use strict';
// Offline only: semantic DOM fixtures and simulated Chrome APIs; no account,
// network, cookies, or real publish button is accessed by these tests.
const assert=require('node:assert/strict');
const path=require('node:path');
const fs=require('node:fs');
const base=path.join(__dirname,'../tools/bilibili-browser-extension');
const a=require(path.join(base,'activity.js'));
const {ActivityWorker}=require(path.join(base,'activity-background.js'));
const bridge=require(path.join(base,'activity-bridge.js'));
const content=require(path.join(base,'activity-content.js'));
const cases=[],test=(name,fn)=>cases.push({name,fn});
const uuid=n=>`00000000-0000-4000-8000-${String(n).padStart(12,'0')}`;
const draft={expected_uid:'12345',text:'今天听到一段安静的旋律。',draft_id:uuid(5)};
const attemptState=()=>({activityPublishLedger:{[draft.draft_id]:{request_id:uuid(1),
  fingerprint:`hash:${draft.expected_uid}:${draft.text.length}:${draft.text.charCodeAt(0)}`,status:'attempted',attempted_at:1000}}});
const command=(action,payload={},id=uuid(1))=>({channel:a.CHANNEL,kind:'request',request_id:id,action,payload});
const local={tab:{id:7},frameId:0,documentId:'local-page',url:'http://127.0.0.1:8080/bilibili-activity'};
const flush=async()=>{for(let i=0;i<100;i++)await Promise.resolve();};
const deferred=()=>{let resolve;const promise=new Promise(done=>{resolve=done;});return {resolve,promise};};
function clock(){let now=1000,next=0;const timers=new Map();return {now:()=>now,timers,
  setTimer(fn,ms){const id=++next;timers.set(id,{fn,at:now+ms});return id;},clearTimer:id=>timers.delete(id),
  tick(ms){const end=now+ms;for(let i=0;i<1000;i++){
    const entry=[...timers].filter(([,t])=>t.at<=end).sort((a,b)=>a[1].at-b[1].at)[0];
    if(!entry)break;now=entry[1].at;timers.delete(entry[0]);entry[1].fn();
  }now=end;}};}
class Element {
  constructor(tag='div',attrs={},text='',children=[]){this.tagName=tag.toUpperCase();this.nodeType=1;this.attrs=attrs;this.value=text;this.children=[];this.hidden=false;this.clicks=0;this.listeners={};this.append(...children);}
  append(...children){for(const child of children){child.parentNode=this;this.children.push(child);}}
  get childNodes(){return this.value?[{nodeType:3,nodeValue:this.value},...this.children]:this.children;}
  get className(){return this.attrs.class||'';}
  get classList(){return {contains:name=>this.className.split(/\s+/).includes(name)};}
  get innerText(){return this.value+this.children.map(n=>n.innerText).join('');}
  get textContent(){return this.innerText;}
  set textContent(value){this.value=value;this.children=[];}
  get href(){return this.attrs.href?new URL(this.attrs.href,'https://t.bilibili.com/').href:'';}
  getAttribute(name){return this.attrs[name]??null;}
  getClientRects(){return this.hidden||(this.parentNode&&!this.parentNode.getClientRects().length)?[]:[{}];}
  matches(selector){return selector.split(',').some(part=>{
    const pieces=part.trim().split(/\s+(?![^\[]*\])/);let node=this;
    if(!simple(node,pieces.pop()))return false;
    while(pieces.length){const clause=pieces.pop();node=node.parentNode;while(node&&!simple(node,clause))node=node.parentNode;if(!node)return false;}return true;
  });}
  querySelectorAll(selector){const out=[];const walk=node=>{for(const child of node.children){if(child.matches(selector))out.push(child);walk(child);}};walk(this);return out;}
  querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
  addEventListener(name,fn){(this.listeners[name]??=[]).push(fn);}
  dispatchEvent(event){for(const fn of this.listeners[event.type]||[])fn(event);return true;}
  focus(){}click(){this.clicks++;this.onClick?.();}
}
function simple(node,selector){
  const attributes=[...selector.matchAll(/\[([^\]=]+)(=)?(?:"([^"]*)")?\]/g)];selector=selector.replace(/\[[^\]]+\]/g,'');
  const tag=selector.match(/^[A-Za-z0-9-]+/)?.[0];if(tag&&node.tagName!==tag.toUpperCase())return false;
  for(const match of selector.matchAll(/\.([\w-]+)/g))if(!node.className.split(/\s+/).includes(match[1]))return false;
  return attributes.every(([,key,op,value])=>node.getAttribute(key)!==null&&(!op||node.getAttribute(key)===value));
}
const el=(cls,text='',children=[],attrs={})=>new Element('div',{class:cls,...attrs},text,children);
const doc=(children=[])=>new Element('document',{},'',children);
function member(uid='12345') {return el('bili-dyn-my-info','',[
  el('bili-dyn-my-info__header','',[el('info__name','助手')]),
  el('bili-dyn-my-info__stats','',[new Element('a',{href:`https://space.bilibili.com/${uid}/dynamic`},'动态')])]);}
function card(text=draft.text){return el('bili-dyn-list__item','',[
  el('bili-dyn-content__orig__desc',text),el('bili-dyn-time','刚刚')]);}
function editorDOM({uid='12345',text='',extra=[]}={}){
  const editor=new Element('div',{contenteditable:'true'},text),button=el('bili-dyn-publishing__action launcher','发布');
  const document=doc([member(uid),el('bili-dyn-publishing','',[el('bili-dyn-publishing__input','',[editor]),button,...extra])]);
  return {document,editor,button};
}
function fixture({state={},responses={},reader=null}={}) {
  const time=clock(),calls=[],listeners={},tabs=new Map();let next=20,token=100,worker;
  const on=name=>({addListener(fn){(listeners[name]??=[]).push(fn);}});
  const emit=(name,...args)=>{for(const fn of listeners[name]||[])fn(...args);};
  const api={runtime:{onMessage:on('message')},storage:{local:{
    async get(){return structuredClone(state);},async set(value){calls.push({op:'persist',value:structuredClone(value)});Object.assign(state,structuredClone(value));}
  }},tabs:{onRemoved:on('removed'),onCreated:on('created'),onUpdated:on('updated'),
    async get(id){if(!tabs.has(id))throw Error('gone');return {...tabs.get(id)};},
    async create(options){const tab={id:next++,...options};tabs.set(tab.id,tab);calls.push({op:'create',...tab});return {...tab};},
    async update(id,options){const tab=tabs.get(id);Object.assign(tab,options);calls.push({op:'update',id,...options});
      if(options.url)queueMicrotask(()=>worker.handle({channel:a.CHANNEL,kind:'hello',token:worker.active?.token},
        {tab:{id},frameId:0,documentId:`doc-${id}-${calls.length}`,url:tab.url}));return {...tab};},
    async remove(id){calls.push({op:'remove',id});tabs.delete(id);emit('removed',id);},
    async sendMessage(id,message,options){calls.push({op:'message',id,message,options});
      if(message.kind!=='run')return;
      const operation=message.operation;
      if(responses[operation])return responses[operation]({id,message,worker,calls,emit,tabs});
      if(operation==='account')return {ok:true,result:{uid:'12345',name:'助手'}};
      if(operation==='search')return {ok:true,result:{items:[]}};
      if(operation==='read')return {ok:true,result:{url:message.payload.url,title:'音乐',description:'简介',subtitles:[],evidence_kind:'page_metadata'}};
      if(operation==='candidate')return {ok:true,result:{unique:true}};
      if(operation==='preflight')return {ok:true,result:{scanned:true,existing:false}};
      if(operation==='open-candidate'){
        const child={id:next++,openerTabId:id,url:'https://www.bilibili.com/opus/999999'};
        tabs.set(child.id,child);emit('created',child);return {ok:true,result:{clicked:true}};
      }
      if(operation==='detail')return {ok:true,result:{matches:true,author_uid:'12345'}};
      return {ok:true,result:{prepared:true,submitted:true}};
    }
  }};
  worker=new ActivityWorker(api,{...time,reader,randomId:()=>uuid(++token),hash:async p=>`hash:${p.expected_uid}:${p.text.length}:${p.text.charCodeAt(0)}`});
  return {worker,api,time,calls,state,tabs,emit};
}

test('only fixed local routes, official BV and dynamic URLs are accepted',()=>{
  assert(a.local(local.url));assert(a.local('http://localhost:9000/'));assert(!a.local('https://localhost/bilibili-activity'));
  for(const value of ['https://evil.invalid/','http://127.0.0.1/other','http://127.0.0.1.evil.invalid/bilibili-activity'])assert(!a.local(value));
  assert.equal(a.videoURL('https://www.bilibili.com/video/BV1xx411c7mD?spm_id_from=1'),'https://www.bilibili.com/video/BV1xx411c7mD');
  assert.equal(a.videoURL('https://www.bilibili.com/opus/123'),'');assert.equal(a.videoURL('https://www.bilibili.com:9000/video/BV1xx411c7mD'),'');
  assert.deepEqual(a.dynamicURL('https://t.bilibili.com/123'),{dynamic_id:'123',url:'https://www.bilibili.com/opus/123'});
  assert.equal(a.dynamicURL('https://t.bilibili.com/'),null);
});
test('request schemas drop unknown fields and require identity, draft UUID and bounded text',()=>{
  const clean=a.validate(command('publish',{...draft,token:'private',cookie:'private'}));assert.deepEqual(clean.payload,draft);
  for(const payload of [{...draft,expected_uid:12345},{...draft,draft_id:'draft'},{...draft,text:' '},{...draft,text:'x'.repeat(2001)}])assert.equal(a.validate(command('publish',payload)),null);
  assert.equal(a.validate(command('read',{url:'https://evil.invalid/'})),null);
  assert.equal(a.validate(command('search',{query:'x'.repeat(101)})),null);
  assert.equal(a.validate({...command('account'),request_id:[uuid(1)]}),null);
  assert(a.validate(command('verify',draft)));assert.equal(a.validate(command('verify',{...draft,url:'https://evil.invalid/123'})),null);
});
test('account is derived only from the member widget or login header, never uploader links',()=>{
  const document=doc([new Element('a',{href:'https://space.bilibili.com/999'},'UP主')]);assert.equal(a.account(document),null);
  document.append(member());assert.deepEqual(a.account(document),{uid:'12345',name:'助手'});
  document.append(el('header-avatar-wrap','',[new Element('a',{class:'header-entry-mini',href:'https://space.bilibili.com/999'})]));
  assert.equal(a.account(document),null);
});
test('public search is bounded and read distinguishes metadata from currently visible captions',()=>{
  const document=doc([el('bili-video-card','',[
    new Element('a',{href:'https://www.bilibili.com/video/BV1xx411c7mD'}),el('bili-video-card__info--tit','安静的歌'),el('bili-video-card__info--author','UP')])]);
  assert.deepEqual(a.search(document),[{url:'https://www.bilibili.com/video/BV1xx411c7mD',title:'安静的歌',author:'UP'}]);
  const video=new Element('video');video.textTracks=[{mode:'hidden',activeCues:[{text:'隐藏字幕'}]},{mode:'showing',activeCues:[{text:'现在可见'}]}];
  const readDoc=doc([new Element('h1',{class:'video-title'},'视频'),new Element('meta',{name:'description',content:'页面简介'}),video]);
  const value=a.read(readDoc,'https://www.bilibili.com/video/BV1xx411c7mD');
  assert.equal(value.description,'页面简介');assert.deepEqual(value.subtitles,['现在可见']);assert.equal(value.evidence_kind,'visible_subtitles');
  video.textTracks=[];assert.equal(a.read(readDoc,value.url).evidence_kind,'page_metadata');
});
test('composer refuses wrong identity, existing drafts, attachments, or disabled launchers',()=>{
  const good=editorDOM();assert.equal(a.composer(good.document,draft).editor,good.editor);
  assert.throws(()=>a.composer(editorDOM({uid:'999'}).document,draft),value=>value==='account_mismatch');
  assert.throws(()=>a.composer(editorDOM({text:'用户原稿'}).document,draft),value=>value==='unsafe_composer');
  assert.throws(()=>a.composer(editorDOM({extra:[el('bili-dyn-publishing__attachment','图片')]}).document,draft),value=>value==='unsafe_composer');
  good.editor.textContent=draft.text;good.button.attrs.class+=' disabled';
  assert.throws(()=>a.composer(good.document,draft,{filled:true}),value=>value==='unsafe_composer');
});
test('verification requires a unique exact original in this account scope, not a repost',()=>{
  const document=doc([member(),card()]),location='https://space.bilibili.com/12345/dynamic';
  assert(a.candidate(document,location,draft));assert.equal(a.candidate(document,'https://space.bilibili.com/999/dynamic',draft),null);
  assert.equal(a.candidate(document,location,{...draft,text:'今天'}),null);
  document.append(card());assert.equal(a.candidate(document,location,draft),null);
  const repost=card();repost.append(el('bili-dyn-content__forw__desc','转发'));assert.equal(a.candidate(doc([member(),repost]),location,draft),null);
});
test('visible link labels reconstruct official source URLs; hidden text cannot satisfy evidence',()=>{
  const source='https://www.bilibili.com/video/BV1xx411c7mD';
  const text=el('opus-module-content','推荐 ',[new Element('a',{href:source},'网页链接')]);
  assert(a.detailMatches(doc([text]),`推荐 ${source}`));text.hidden=true;assert(!a.detailMatches(doc([text]),`推荐 ${source}`));
});
test('preflight waits for a loaded account list, notices existing text and supports a verified empty list',()=>{
  const location='https://space.bilibili.com/12345/dynamic';
  const list=el('bili-dyn-list','',[card()]),document=doc([member(),list]);
  assert(a.timeline(document,location,draft).existing);
  list.children=[];assert.equal(a.timeline(document,location,draft),null);
  list.append(el('bili-dyn-list-empty'));assert.deepEqual(a.timeline(document,location,draft),{scanned:true,existing:false,signature:'[]'});
  list.append(el('bili-dyn-list-loading'));assert.equal(a.timeline(document,location,draft),null);
});
test('old or unrecognized timestamps cannot establish a fresh publication',()=>{
  assert(a.recentTime(el('', '刚刚'),1000,2000));assert(a.recentTime(el('', '1分钟前'),1000,50000));
  assert(!a.recentTime(el('', '2小时前'),1000,50000));assert(!a.recentTime(el('', '2026-10-20'),1000,50000));
  const document=doc([member(),card()]);document.querySelector('.bili-dyn-time').value='昨天';
  assert.equal(a.candidate(document,'https://space.bilibili.com/12345/dynamic',{...draft,_since:1000,_now:2000}),null);
});
test('author evidence comes from the detail author region, not generic profile links',()=>{
  assert.equal(a.author(doc([new Element('a',{href:'https://space.bilibili.com/12345'},'任意链接')])).uid,'');
  assert.equal(a.author(doc([el('opus-module-author','',[new Element('a',{href:'https://space.bilibili.com/12345'},'作者')])])).uid,'12345');
  assert(a.author(doc([el('opus-module-author__name','作者')])).node);
});
test('published receipts need a real matching dynamic URL plus a matching detail body',()=>{
  assert.equal(a.proof(draft,'https://t.bilibili.com/',true).status,'unknown');
  assert.equal(a.proof(draft,'https://t.bilibili.com/999',false).status,'unknown');
  assert.equal(a.proof({...draft,url:'https://www.bilibili.com/opus/111'},'https://t.bilibili.com/999',true).status,'unknown');
  assert.deepEqual(a.proof(draft,'https://t.bilibili.com/999',true),{status:'published',uid:'12345',text:draft.text,dynamic_id:'999',url:'https://www.bilibili.com/opus/999'});
});
test('bridge requires same window/origin and drops pending replies after pagehide',async()=>{
  const events={},posted=[],sent=[],pending=deferred();
  const win={location:{href:local.url,origin:'http://127.0.0.1:8080'},addEventListener:(name,fn)=>{events[name]=fn;},postMessage:(...args)=>posted.push(args)};win.top=win;
  bridge.install(win,{sendMessage:async message=>{sent.push(message);return pending.promise;}},a);
  const data={source:a.SOURCE,type:'request',...command('publish',{...draft,token:'secret'})};
  events.message({source:{},origin:win.location.origin,data});events.message({source:win,origin:'https://evil.invalid',data});await flush();assert.equal(sent.length,0);
  events.message({source:win,origin:win.location.origin,data});await flush();assert.equal(sent.length,1);assert(!('token' in sent[0].payload));
  events.message({source:win,origin:win.location.origin,data});await flush();assert.equal(sent.length,1);
  events.pagehide();pending.resolve({ok:true,result:{status:'published'}});await flush();assert.equal(posted.length,0);assert.equal(sent[1].kind,'cancel');
});
test('a disconnected publish bridge reports unknown, never a retryable publication failure',async()=>{
  const events={},posted=[];const win={location:{href:local.url,origin:'http://127.0.0.1:8080'},addEventListener:(name,fn)=>events[name]=fn,postMessage:data=>posted.push(data)};win.top=win;
  bridge.install(win,{sendMessage(){throw Error('closed');}},a);
  events.message({source:win,origin:win.location.origin,data:{source:a.SOURCE,type:'request',...command('publish',draft)}});await flush();
  assert.equal(posted[0].result.status,'unknown');assert.equal(posted[0].ok,true);
});
test('host cancellation is forwarded only for same-origin pending request IDs',async()=>{
  const events={},sent=[],pending=deferred();const win={location:{href:local.url,origin:'http://127.0.0.1:8080'},addEventListener:(name,fn)=>events[name]=fn,postMessage(){}};win.top=win;
  bridge.install(win,{sendMessage:message=>{sent.push(message);return pending.promise;}},a);
  const emit=(data,origin=win.location.origin)=>events.message({source:win,origin,data:{source:a.SOURCE,...data}});
  emit({type:'cancel',request_id:uuid(1)});await flush();assert.equal(sent.length,0);
  emit({type:'request',...command('publish',draft)});await flush();
  emit({type:'cancel',request_id:uuid(1)},'https://evil.invalid');emit({type:'cancel',request_id:uuid(9)});await flush();assert.equal(sent.length,1);
  emit({type:'cancel',request_id:uuid(1)});await flush();assert.equal(sent[1].kind,'cancel');pending.resolve(a.error('cancelled'));await flush();
});
test('worker rejects remote pages, iframes and missing document identity before navigation',async()=>{
  const f=fixture();
  for(const sender of [{...local,url:'https://evil.invalid/'},{...local,frameId:1},{...local,documentId:undefined}])
    assert.equal((await f.worker.handle(command('account'),sender)).error.code,'invalid_request');
  assert.equal(f.calls.length,0);
});
test('worker persists before the one commit, then verifies body and real navigation URL',async()=>{
  const f=fixture();const result=await f.worker.handle(command('publish',draft),local);
  assert.equal(result.result.status,'published');assert.equal(result.result.uid,draft.expected_uid);assert.equal(result.result.text,draft.text);
  const commit=f.calls.findIndex(c=>c.message?.operation==='commit'),persist=f.calls.findIndex(c=>c.op==='persist');assert(persist>=0&&persist<commit);
  assert.equal(f.calls.filter(c=>c.message?.operation==='commit').length,1);
  assert(!JSON.stringify(f.state).includes(draft.text));assert.equal(f.state.activityPublishLedger[draft.draft_id].status,'published');
  assert.equal(f.calls.filter(c=>c.op==='create').length,1);assert(f.calls.some(c=>c.op==='remove'));
});
test('same draft replays a receipt without opening or publishing and conflicting bodies are refused',async()=>{
  const f=fixture();await f.worker.handle(command('publish',draft),local);const count=f.calls.length;
  const replay=await f.worker.handle(command('publish',draft,uuid(2)),local);assert.equal(replay.result.status,'published');assert.equal(f.calls.length,count);
  assert.equal((await f.worker.handle(command('publish',{...draft,text:'另一篇文章'},uuid(3)),local)).error.code,'draft_conflict');
});
test('timeout after commit is unknown and a restarted worker cannot submit the draft again',async()=>{
  const never=deferred(),f=fixture({responses:{commit:()=>never.promise}});
  const pending=f.worker.handle(command('publish',draft),local);await flush();assert(f.worker.active.attempted);
  f.time.tick(50000);await flush();assert.equal((await pending).result.status,'unknown');
  const restarted=fixture({state:f.state});const duplicate=await restarted.worker.handle(command('publish',draft,uuid(2)),local);
  assert.equal(duplicate.result.status,'unknown');assert.equal(restarted.calls.length,0);
  never.resolve({ok:true,result:{submitted:true}});await flush();assert.equal(f.calls.filter(c=>c.message?.operation==='commit').length,1);
});
test('late preparation cannot cross the persisted submit barrier after cancellation',async()=>{
  const delayed=deferred(),f=fixture({responses:{prepare:()=>delayed.promise}}),pending=f.worker.handle(command('publish',draft),local);await flush();
  await f.worker.handle({channel:a.CHANNEL,kind:'cancel',request_id:uuid(1)},local);delayed.resolve({ok:true,result:{prepared:true}});await flush();
  assert.equal((await pending).error.code,'cancelled');assert.equal(f.calls.filter(c=>c.op==='persist'||c.message?.operation==='commit').length,0);
});
test('failed persistence prevents commit and raw errors never leave the extension',async()=>{
  const f=fixture();f.api.storage.local.set=async()=>{throw Error('SECRET_PRIVATE_TOKEN');};
  const result=await f.worker.handle(command('publish',draft),local);assert.equal(result.ok,false);assert(!JSON.stringify(result).includes('SECRET'));
  assert.equal(f.calls.filter(c=>c.message?.operation==='commit').length,0);
});
test('verify without a URL uses a unique account candidate and never prepares or commits',async()=>{
  const f=fixture({state:attemptState()});const result=await f.worker.handle(command('verify',draft),local);
  assert.equal(result.result.status,'published');assert(!f.calls.some(c=>['prepare','commit'].includes(c.message?.operation)));
  assert(!f.calls.some(c=>c.op==='persist'));
});
test('ambiguous, wrong-link and wrong-body verification remain unknown',async()=>{
  for(const responses of [{candidate:async()=>({ok:true,result:{unique:false}})},{detail:async()=>({ok:true,result:{matches:false}})}]) {
    const f=fixture({state:attemptState(),responses});assert.equal((await f.worker.handle(command('verify',draft),local)).result.status,'unknown');
  }
  const f=fixture();assert.equal((await f.worker.handle(command('verify',{...draft,url:'https://www.bilibili.com/opus/111'}),local)).result.status,'unknown');
});
test('wrong account or unrecognized composer blocks before persistent submission or click',async()=>{
  const f=fixture({responses:{prepare:async()=>a.error('account_mismatch')}});
  assert.equal((await f.worker.handle(command('publish',draft),local)).error.code,'account_mismatch');
  assert(!f.calls.some(c=>c.op==='persist'||c.message?.operation==='commit'));
});
test('a preexisting identical post blocks publication instead of becoming false success evidence',async()=>{
  const f=fixture({responses:{preflight:async()=>({ok:true,result:{scanned:true,existing:true}})}});
  assert.equal((await f.worker.handle(command('publish',draft),local)).error.code,'existing_text');
  assert(!f.calls.some(c=>c.op==='persist'||['prepare','commit'].includes(c.message?.operation)));
});
test('matching detail text from a different or unidentifiable author is never published',async()=>{
  for(const author_uid of ['999','']) {
    const f=fixture({state:attemptState(),responses:{detail:async()=>({ok:true,result:{matches:true,author_uid,can_open_author:false}})}});
    assert.equal((await f.worker.handle(command('verify',draft),local)).result.status,'unknown');
  }
});
test('a DIV author name is resolved through its actual profile navigation before accepting UID',async()=>{
  for(const uid of ['12345','999']) {
    const f=fixture({state:attemptState(),responses:{detail:async()=>({ok:true,result:{matches:true,author_uid:'',can_open_author:true}}),
      'open-author':async({id,emit,tabs})=>{
        const child={id:88,openerTabId:id,url:`https://space.bilibili.com/${uid}`};tabs.set(child.id,child);emit('created',child);
        return {ok:true,result:{clicked:true}};
      }}});
    assert.equal((await f.worker.handle(command('verify',draft),local)).result.status,uid==='12345'?'published':'unknown');
  }
});
test('collector ownership requires current live tab, token and document before granting a permit',async()=>{
  const delayed=deferred(),f=fixture({responses:{prepare:()=>delayed.promise}}),pending=f.worker.handle(command('publish',draft),local);await flush();
  const job=f.worker.active,sender={tab:{id:job.tab},frameId:0,documentId:job.document,url:job.target};
  assert.deepEqual(await f.worker.handle({channel:a.CHANNEL,kind:'permit',token:job.token},sender),{allowed:false});
  assert.deepEqual(await f.worker.handle({channel:a.CHANNEL,kind:'hello',token:job.token},{...sender,tab:{id:999}}),{owned:false});
  assert.deepEqual(await f.worker.handle({channel:a.CHANNEL,kind:'hello',token:uuid(999)},sender),{owned:false});
  f.time.tick(50000);await pending;assert.deepEqual(await f.worker.handle({channel:a.CHANNEL,kind:'permit',token:job.token},sender),{owned:false});
  delayed.resolve({ok:true,result:{prepared:true}});await flush();
});
test('successive reads reuse only owned tabs; a user-navigated tab is never commandeered',async()=>{
  const f=fixture();await f.worker.handle(command('account'),local);await f.worker.handle(command('account',{},uuid(2)),local);
  assert.equal(f.calls.filter(c=>c.op==='create').length,1);f.tabs.get(f.worker.owned.id).url='https://example.org/';
  await f.worker.handle(command('account',{},uuid(3)),local);assert.equal(f.calls.filter(c=>c.op==='create').length,2);
  const navigations=f.calls.filter(c=>c.op==='update'&&c.url);
  for(let i=0;i<navigations.length;i+=2){assert.equal(navigations[i].url,'about:blank');assert(navigations[i+1].url.includes('#ai-vtuber-activity='));}
});
test('lost local publish history cannot verify an arbitrary old same-text post without a known URL',async()=>{
  const f=fixture();assert.equal((await f.worker.handle(command('verify',draft),local)).result.status,'unknown');assert.equal(f.calls.length,0);
  const known=await f.worker.handle(command('verify',{...draft,url:'https://www.bilibili.com/opus/999999'},uuid(2)),local);
  assert.equal(known.result.status,'published');
});
test('timer setup failure cannot leave a busy worker or start navigation',async()=>{
  const f=fixture();f.worker.setTimer=()=>{throw Error('timer');};
  assert.equal((await f.worker.handle(command('account'),local)).error.code,'unavailable');assert.equal(f.worker.active,null);assert.equal(f.calls.length,0);
});
test('content script sets its click barrier and checks identity again after preparing',async()=>{
  const time=clock(),savedNow=Date.now;Date.now=time.now;
  try {
    const {document,button}=editorDOM(),listeners={},events={},token=uuid(88);
    const win={location:{href:`https://t.bilibili.com/#ai-vtuber-activity=${token}`},setTimeout:time.setTimer,
      MutationObserver:class{observe(){}disconnect(){}},InputEvent:class{constructor(type,options){this.type=type;Object.assign(this,options);}},
      addEventListener:(name,fn)=>events[name]=fn};win.top=win;
    const runtime={onMessage:{addListener:fn=>listeners.message=fn},sendMessage:async m=>m.kind==='hello'?{owned:true}:{allowed:true}};
    content.install(document,win,runtime,a);await flush();
    const run=operation=>new Promise(resolve=>listeners.message({channel:a.CHANNEL,kind:'run',token,operation,payload:draft,deadline:51000},{},resolve));
    const prepare=run('prepare');for(let i=0;i<8;i++){time.tick(250);await flush();}assert.equal((await prepare).ok,true);
    const commit=run('commit'),duplicate=run('commit');await flush();assert.equal(button.clicks,1);time.tick(1500);await flush();assert.equal((await commit).ok,true);
    assert.equal((await duplicate).ok,false);
    assert.equal((await run('commit')).ok,false);assert.equal(button.clicks,1);
  } finally {Date.now=savedNow;}
});
test('account changes, host stop and expired permits between preparation and submission prevent clicks',async()=>{
  const savedNow=Date.now;
  try {
    for(const scenario of ['account_changed','host_stopped','permit_revoked','deadline']) {
      const time=clock();Date.now=time.now;
      const {document,button}=editorDOM(),listeners={},token=uuid(88);let allowed=true;
      const win={location:{href:`https://t.bilibili.com/#ai-vtuber-activity=${token}`},setTimeout:time.setTimer,
        MutationObserver:class{observe(){}disconnect(){}},InputEvent:class{constructor(type){this.type=type;}},addEventListener(){}};win.top=win;
      const runtime={onMessage:{addListener:fn=>listeners.message=fn},sendMessage:async message=>message.kind==='hello'?{owned:true}:{allowed}};
      content.install(document,win,runtime,a);await flush();
      const run=operation=>new Promise(resolve=>listeners.message({channel:a.CHANNEL,kind:'run',token,operation,payload:draft,deadline:51000},{},resolve));
      const prepare=run('prepare');for(let i=0;i<8;i++){time.tick(250);await flush();}assert.equal((await prepare).ok,true);
      if(scenario==='account_changed')document.querySelector('.bili-dyn-my-info__stats a').attrs.href='https://space.bilibili.com/999/dynamic';
      if(scenario==='host_stopped')listeners.message({channel:a.CHANNEL,kind:'stop',token},{},()=>{});
      if(scenario==='permit_revoked')allowed=false;
      if(scenario==='deadline')time.tick(50000);
      assert.equal((await run('commit')).ok,false,scenario);assert.equal(button.clicks,0,scenario);
    }
  } finally {Date.now=savedNow;}
});
test('multiline published text matches rendered block paragraphs without losing word boundaries',()=>{
  const document=doc([el('opus-module-content','',[new Element('p',{},'第一段'),new Element('p',{},'第二段')])]);
  assert(a.detailMatches(document,'第一段\n\n第二段'));assert(!a.detailMatches(document,'第一段第二段'));
});
test('owned read commands enrich page evidence through the read-only adapter',async()=>{
  const url='https://www.bilibili.com/video/BV1xx411c7mD';let received;
  const baseline={url,title:'视频',description:'简介',subtitles:[],evidence_kind:'page_metadata'};
  const f=fixture({responses:{read:async()=>({ok:true,result:baseline})},reader:{async enrich(value,{signal}){
    received=value;assert(!signal.aborted);return {...value,subtitles:['完整字幕'],evidence_kind:'full_subtitles'};
  }}});
  const result=await f.worker.handle(command('read',{url}),local);assert.deepEqual(received,baseline);
  assert.equal(result.result.evidence_kind,'full_subtitles');assert(!f.calls.some(c=>c.op==='persist'));
});
test('host cancellation aborts ongoing API reads and cannot return late evidence',async()=>{
  const url='https://www.bilibili.com/video/BV1xx411c7mD';let signal;
  const f=fixture({responses:{read:async()=>({ok:true,result:{url}})},reader:{enrich(_value,options){
    signal=options.signal;return new Promise((_resolve,reject)=>signal.addEventListener('abort',()=>reject('cancelled')));
  }}});
  const pending=f.worker.handle(command('read',{url}),local);await flush();assert(signal);
  await f.worker.handle({channel:a.CHANNEL,kind:'cancel',request_id:uuid(1)},local);
  assert.equal((await pending).error.code,'cancelled');assert(signal.aborted);await flush();assert.equal(f.worker.active,null);
});
test('page actors use no cookie, API fetch, framework state or remote execution APIs',()=>{
  const files=['activity.js','activity-bridge.js','activity-background.js','activity-content.js'];
  const source=files.map(file=>fs.readFileSync(path.join(base,file),'utf8')).join('\n');
  assert(!/document\s*\.\s*cookie|chrome\s*\.\s*cookies|\bfetch\s*\(|XMLHttpRequest|__vue|__INITIAL_STATE__|\beval\s*\(/.test(source));
  const reader=fs.readFileSync(path.join(base,'activity-reader.js'),'utf8');
  assert(!/document\s*\.\s*cookie|chrome\s*\.\s*cookies|__vue|__INITIAL_STATE__|\beval\s*\(/.test(reader));
  const manifest=JSON.parse(fs.readFileSync(path.join(base,'manifest.json'),'utf8'));
  assert.deepEqual(manifest.permissions,['storage']);assert(!JSON.stringify(manifest).includes('<all_urls>'));
});

(async()=>{let failed=0;for(const {name,fn} of cases){try{await fn();console.log(`PASS: ${name}`);}catch(error){failed++;console.error(`FAIL: ${name}\n${error.stack||error}`);}}
  if(failed)process.exitCode=1;else console.log(`PASS: ${cases.length} activity extension checks`);
})();
