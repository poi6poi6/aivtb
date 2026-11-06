'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const base=path.join(__dirname,'../tools/bilibili-browser-extension');
const common=require(path.join(base,'common.js'));
const {BrowserQueries}=require(path.join(base,'background.js'));
const bridge=require(path.join(base,'bridge.js'));
const collector=require(path.join(base,'collector.js'));
const cases=[];
const test=(name,run)=>cases.push({name,run});
const flush=async()=>{for(let i=0;i<35;i++)await Promise.resolve();};
const deferred=()=>{let resolve;const promise=new Promise(done=>resolve=done);return {promise,resolve};};
const local={tab:{id:7},frameId:0,documentId:'console-1',url:'http://127.0.0.1:8080/'};
const query=(id='request-1',value="it's happening")=>({channel:common.channel,action:'search',id,query:value});
const send=(action,id='request-1',extra={})=>({channel:common.channel,action,id,...extra});
function clock(){
  let now=0,next=0;const timers=new Map();
  return {now:()=>now,setTimer(fn,delay){const id=++next;timers.set(id,{fn,at:now+delay});return id;},
    clearTimer:id=>timers.delete(id),tick(ms){const end=now+ms;for(let limit=0;limit<1000;limit++){
      const entry=[...timers].filter(([,t])=>t.at<=end).sort((a,b)=>a[1].at-b[1].at)[0];
      if(!entry)break;now=entry[1].at;timers.delete(entry[0]);entry[1].fn();
    }now=end;},timers};
}
function chromeFixture(){
  const calls=[],tabs=new Map([[90,{id:90,url:'https://www.bilibili.com/video/BV1xx411c7mD'}]]);
  const events={},state={};let next=20,uuid=0;
  const api={storage:{session:{async get(){return structuredClone(state);},async set(value){Object.assign(state,structuredClone(value));}}},
    runtime:{getManifest:()=>JSON.parse(fs.readFileSync(path.join(base,'manifest.json'),'utf8')),onMessage:{addListener(fn){events.message=fn;}}},tabs:{
      onRemoved:{addListener(fn){events.removed=fn;}},
      async get(id){if(!tabs.has(id))throw Error('gone');return {...tabs.get(id)};},
      async create(options){calls.push({op:'create',options});const tab={id:next++,...options};tabs.set(tab.id,tab);return {...tab};},
      async update(id,options){calls.push({op:'update',id,options});Object.assign(tabs.get(id),options);return {...tabs.get(id)};},
      async sendMessage(id,message){calls.push({op:'message',id,message});},
    }};
  const time=clock(),worker=new BrowserQueries(api,{...time,randomId:()=>`00000000-0000-4000-8000-${String(++uuid).padStart(12,'0')}`});
  return {api,worker,calls,tabs,time,events,state};
}
async function connect(f){return f.worker.handle(send('connect','connect-1'),local);}
async function begin(f,id='request-1',value="it's happening"){
  const promise=f.worker.handle(query(id,value),local);await flush();await f.worker.openQueue;return {promise,job:f.worker.active};
}
async function result(f,job,items=[{bvid:'BV1xx411c7mD',title:'歌曲',play_text:'23.4万'}],overrides={}){
  const tab=f.tabs.get(f.worker.owned.id);
  return f.worker.handle({channel:common.channel,action:'collector-result',jobId:job.jobId,
    result:{ok:true,data:{page_url:tab.url,results:items}},...overrides},
    {tab:{id:tab.id},frameId:0,url:tab.url});
}

// Small semantic DOM fixture: selectors are evaluated against actual tag,
// attribute, class and ancestor data, including SVG xlink:href markers.
class Element{
  constructor(tag='div',attrs={},value='',children=[]){this.tagName=tag.toUpperCase();this.attrs=attrs;this.value=value;this.children=[];this.hidden=false;this.style={display:'block',visibility:'visible'};this.listeners={};this.className=attrs.class||'';this.append(...children);}
  append(...nodes){for(const node of nodes){node.parentNode=this;this.children.push(node);}}
  get textContent(){return this.value+this.children.map(n=>n.textContent).join('');}
  get innerText(){return this.textContent;}
  getAttribute(name){return this.attrs[name] ?? null;}
  getClientRects(){return this.hidden||this.style.display==='none'||(this.parentNode&&!this.parentNode.getClientRects().length)?[]:[{}];}
  matches(selector){return selector.split(',').some(part=>{
    const pieces=part.trim().split(/\s+(?![^\[]*\])/);let node=this;
    if(!simple(node,pieces.pop()))return false;
    while(pieces.length){const clause=pieces.pop();node=node.parentNode;while(node&&!simple(node,clause))node=node.parentNode;if(!node)return false;}return true;
  });}
  querySelectorAll(selector){const result=[];const visit=node=>{for(const child of node.children){if(child.matches(selector))result.push(child);visit(child);}};visit(this);return result;}
  querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
  addEventListener(name,fn){(this.listeners[name]??=[]).push(fn);}
  fire(name,target=this){for(const fn of this.listeners[name]||[])fn({target});}
}
function simple(node,selector){
  const attributes=[...selector.matchAll(/\[([^\]=*]+)(\*=|=)?(?:"([^"]*)")?\]/g)];
  selector=selector.replace(/\[[^\]]+\]/g,'');
  const tag=selector.match(/^[A-Za-z0-9-]+/)?.[0];if(tag&&node.tagName!==tag.toUpperCase())return false;
  for(const match of selector.matchAll(/\.([\w-]+)/g))if(!node.className.split(/\s+/).includes(match[1]))return false;
  return attributes.every(([,key,op,value])=>{const raw=node.getAttribute(key.replace(/\\:/g,':'));return raw!==null&&(!op||(op==='='?raw===value:raw.includes(value)));});
}
const el=(tag,cls,value='',children=[],attrs={})=>new Element(tag,{class:cls,...attrs},value,children);
function dom(children=[],url='https://search.bilibili.com/all?keyword=test&order=click'){
  const doc=new Element('document');doc.body=el('body','', '',children);doc.append(doc.body);
  const observers=[];const win={location:{href:url},getComputedStyle:n=>n.style,
    MutationObserver:class{constructor(fn){this.fn=fn;observers.push(this);}observe(){}disconnect(){this.disconnected=true;}}};
  win.top=win;win.addEventListener=(name,fn)=>{win[name]=fn;};return {doc,win,observers};
}
function card({bvid='BV1xx411c7mD',title="it's happening",play='23.4万',marker='href',extra=[]}={}){
  const stats=[el('span','bili-video-card__stats--item','9999',[new Element('svg',{},'',[
    new Element('use',{href:'#widget-danmaku'})])]),
    el('span','bili-video-card__stats--item','',[new Element('svg',{},'',[new Element('use',{[marker]:'#widget-play-count'})]),el('span','bili-video-card__stats--text',play)])];
  return el('div','bili-video-card','',[
    new Element('a',{href:`https://www.bilibili.com/video/${bvid}`}),el('h3','bili-video-card__info--tit',title),
    el('span','bili-video-card__info--author','UP主'),el('span','bili-video-card__stats__duration','03:21'),
    new Element('img',{src:'https://i0.hdslb.com/public.jpg'}),...stats,...extra]);
}

test('manifest has storage only, constrained hosts and document_start guards',()=>{
  const manifest=JSON.parse(fs.readFileSync(path.join(base,'manifest.json'),'utf8'));
  assert.deepEqual(manifest.permissions,['storage']);assert.equal(manifest.manifest_version,3);
  assert.deepEqual(manifest.host_permissions,['http://localhost/*','http://127.0.0.1/*',
    'https://search.bilibili.com/*','https://www.bilibili.com/video/*',
    'https://t.bilibili.com/*','https://space.bilibili.com/*','https://www.bilibili.com/opus/*',
    'https://api.bilibili.com/*','https://aisubtitle.hdslb.com/*','https://subtitle.hdslb.com/*',
    'https://i0.hdslb.com/*','https://i1.hdslb.com/*','https://i2.hdslb.com/*']);
  assert(manifest.content_scripts.every(s=>s.run_at==='document_start'&&!s.all_frames));
  assert.throws(()=>common.target({url:'https://t.bilibili.com/123456'}));
  assert.throws(()=>common.target({url:'https://www.bilibili.com/opus/123456'}));
  assert(!JSON.stringify(manifest).includes('<all_urls>'));
});
test('official targets preserve apostrophes and exact BV parts; local routes are constrained',()=>{
  assert.equal(new URL(common.target({query:"it's happening"}).url).searchParams.get('keyword'),"it's happening");
  assert.equal(new URL(common.target({query:'海色 现场版'}).url).searchParams.get('keyword'),'海色 现场版');
  assert.equal(common.target({url:'https://www.bilibili.com/video/BV1xx411c7mD?p=10000'}).kind,'video');
  for(const url of ['https://evil.test/video/BV1xx411c7mD','https://www.bilibili.com/video/BV1xx411c7mD?p=10001','https://www.bilibili.com/video/BV1xx411c7mD?p=1&p=2','https://search.bilibili.com/all?keyword=x&keyword=y&order=click'])assert.throws(()=>common.target({url}));
  assert(common.local('http://localhost:8080/bilibili?mode=obs'));assert(common.local(local.url));
  for(const url of ['http://localhost:8080/dev','https://localhost/','http://127.0.0.1.evil.test/','http://user:pass@localhost/'])assert(!common.local(url));
});
test('bridge validates source/origin, filters fields and drops cancelled replies',async()=>{
  const handlers={},posted=[],sent=[],pending=deferred();
  const win={location:{href:local.url,origin:'http://127.0.0.1:8080'},addEventListener:(name,fn)=>handlers[name]=fn,postMessage:(data,origin)=>posted.push({data,origin})};win.top=win;
  bridge.install(win,{sendMessage:message=>{sent.push(message);return message.action==='search'?pending.promise:Promise.resolve({ok:true,data:{connected:true,version:1}});}},common);
  const message={source:'ai-vtuber',type:'bilibili-browser-request',id:'x',action:'connect',token:'never-forward'};
  handlers.message({source:{},origin:win.location.origin,data:message});handlers.message({source:win,origin:'http://evil.test',data:message});assert.equal(sent.length,0);
  handlers.message({source:win,origin:win.location.origin,data:message});await flush();assert.equal(posted.length,1);assert(!('token'in sent[0]));
  handlers.message({source:win,origin:win.location.origin,data:{...message,id:'search',action:'search',query:'test'}});
  handlers.message({source:win,origin:win.location.origin,data:{...message,id:'search',action:'cancel'}});
  pending.resolve({ok:true,data:{results:[]}});await flush();assert.equal(posted.length,1);assert.equal(sent.at(-1).action,'cancel');
});
test('bridge converts synchronous invalidated-context and async messaging errors to safe responses',async()=>{
  for(const sync of [true,false]){
    const handlers={},posted=[];
    const win={location:{href:local.url,origin:'http://127.0.0.1:8080'},
      addEventListener:(name,fn)=>handlers[name]=fn,postMessage:data=>posted.push(data)};win.top=win;
    bridge.install(win,{sendMessage(){
      const error=Error('Extension context invalidated: https://private.test/?token=secret');
      if(sync)throw error;
      return Promise.reject(error);
    }},common);
    const data={source:'ai-vtuber',type:'bilibili-browser-request',id:'reloaded-extension',action:'connect'};
    assert.doesNotThrow(()=>handlers.message({source:win,origin:win.location.origin,data}));
    await flush();assert.equal(posted.length,1);assert.equal(posted[0].error.code,'extension_unavailable');
    assert.equal(posted[0].id,data.id);assert(!JSON.stringify(posted).includes('secret'));
    // Fire-and-forget cancellation must absorb the same synchronous failure.
    assert.doesNotThrow(()=>handlers.message({source:win,origin:win.location.origin,data:{...data,action:'cancel'}}));
    await flush();assert.equal(posted.length,1);
  }
});
test('worker requires connected top-level local document, rejecting other pages',async()=>{
  const f=chromeFixture();assert.equal((await f.worker.handle(query(),local)).error.code,'not_connected');
  assert.equal((await f.worker.handle(send('connect'),{...local,url:'http://localhost/dev'})).error.code,'invalid_request');
  assert.equal((await f.worker.handle(send('connect'),{...local,frameId:2})).error.code,'invalid_request');
  const connected=await connect(f);assert.equal(connected.data.version,1);
  assert.equal(connected.data.extension_version,JSON.parse(fs.readFileSync(path.join(base,'manifest.json'),'utf8')).version);
  assert.equal((await f.worker.handle(query(),{...local,documentId:'reloaded'})).error.code,'not_connected');
  assert.equal(f.calls.length,0);
});
test('create/mute/reset/navigate failures identify their stage without disclosing raw API errors',async()=>{
  for(const stage of ['tab_create','tab_mute','tab_reset','tab_navigate','tab_save']){
    const f=chromeFixture();await connect(f);
    const fail=()=>{throw Error('Tabs cannot be edited right now. https://example.test/?token=private-secret');};
    if(stage==='tab_create')f.api.tabs.create=fail;
    else if(stage==='tab_save')f.api.storage.session.set=()=>{throw Error('storage failure private-secret');};
    else {
      const update=f.api.tabs.update;
      f.api.tabs.update=(id,options)=>{
        if((stage==='tab_mute'&&options.muted)||(stage==='tab_reset'&&options.url==='about:blank')
            ||(stage==='tab_navigate'&&options.url?.startsWith('https:')))return fail();
        return update(id,options);
      };
    }
    const response=await f.worker.handle(query(),local);
    assert.equal(response.error.code,'page_unavailable');assert.equal(response.error.stage,stage);
    assert.equal(response.error.reason,stage==='tab_save'?'storage_error':'tab_busy');
    assert(!JSON.stringify(response).includes('private-secret'));assert.equal(f.worker.active,null);
    if(stage==='tab_mute'||stage==='tab_reset'||stage==='tab_navigate')
      assert.equal(f.tabs.get(f.worker.owned.id).url,'about:blank','fixture reproduces the reported blank query tab');
  }
});
test('message channel reports preparation and session failures with safe diagnostics',async()=>{
  const delivered=(f,message)=>new Promise(resolve=>{
    assert.equal(f.events.message(message,local,resolve),true,'Chrome async response channel must stay open');
  });
  const prepare=chromeFixture();await connect(prepare);
  prepare.worker.randomId=()=>{throw TypeError('crypto API is unavailable; private-secret');};
  const preparation=await delivered(prepare,query());assert.equal(preparation.error.stage,'random_id');
  assert.equal(preparation.error.reason,'secure_random_unavailable');assert.equal(prepare.calls.length,0);
  const storage=chromeFixture();storage.api.storage.session.set=()=>{throw Error('quota private-secret');};
  const save=await delivered(storage,send('connect','new-connect'));
  assert.equal(save.error.stage,'connect_save');assert.equal(save.error.reason,'storage_error');
  const session=chromeFixture();session.api.storage.session.get=()=>Promise.reject(Error('private-secret'));
  new BrowserQueries(session.api);
  const load=await delivered(session,send('connect','new-connect'));
  assert.equal(load.error.stage,'session_load');assert.equal(load.error.reason,'storage_error');
  assert(!JSON.stringify([preparation,save,load]).includes('private-secret'));
});
test('diagnostic fields accept only fixed stages/reasons, with bounded API classifications',()=>{
  const unsafe=common.error('page_unavailable',{stage:'https://secret.test/?token=x',reason:'page text'});
  assert(!('stage'in unsafe.error));assert(!('reason'in unsafe.error));
  for(const [message,reason] of [['No tab with id: 17','tab_closed'],['Permission denied for secret URL','permission_denied'],
    ['No current window','window_unavailable'],['unrecognized private message','chrome_api_error']])
    assert.equal(common.errorReason(Error(message)),reason);
});
test('worker mutes only its new tab before navigation and returns actual public URL',async()=>{
  const f=chromeFixture();await connect(f);const {promise,job}=await begin(f);
  const nav=f.calls.findIndex(c=>c.options?.url?.startsWith('https://search'));
  assert(f.calls.findIndex(c=>c.options?.muted===true)<nav);assert.equal(f.calls[0].options.url,'about:blank');
  assert(f.calls.every(c=>c.id!==90));assert.equal(new URL(f.tabs.get(f.worker.owned.id).url).searchParams.get('keyword'),"it's happening");
  await result(f,job);const response=await promise;assert(response.ok);assert(!response.data.page_url.includes('ai-vtuber-job'));
  assert.equal(response.data.results[0].play_text,'23.4万');
});
test('default browser timers retain their required global receiver',async()=>{
  const f=chromeFixture();
  const context=vm.createContext({module:{exports:{}},require:()=>common,URL});
  vm.runInContext(`
    globalThis.crypto={randomUUID:()=> '00000000-0000-4000-8000-000000000001'};
    globalThis.setTimeout=function(){if(this!==globalThis)throw TypeError('Illegal invocation');return 1;};
    globalThis.clearTimeout=function(){if(this!==globalThis)throw TypeError('Illegal invocation');};
  `,context);
  vm.runInContext(fs.readFileSync(path.join(base,'background.js'),'utf8'),context);
  f.worker=new context.module.exports.BrowserQueries(f.api);
  await connect(f);
  const reply=new Promise(resolve=>f.events.message(query(),local,resolve));
  await flush();await f.worker.openQueue;
  assert(f.calls.some(call=>call.op==='create'),'unbound host timer must not prevent opening the query tab');
  await result(f,f.worker.active);assert((await reply).ok);
});
test('secure IDs fall back to getRandomValues and fail closed without either secure API',()=>{
  const context=vm.createContext({module:{exports:{}},require:()=>common,URL});
  vm.runInContext(`
    let calls=0;
    globalThis.crypto={getRandomValues(bytes){
      if(this!==globalThis.crypto)throw TypeError('Illegal invocation');
      bytes.fill(0xab);bytes[15]=++calls;return bytes;
    }};
    Math.random=()=>{throw Error('weak randomness is forbidden');};
  `,context);
  vm.runInContext(fs.readFileSync(path.join(base,'background.js'),'utf8'),context);
  const generate=context.module.exports.secureRandomId;
  const first=generate(),second=generate();assert.notEqual(first,second);
  assert.match(first,/^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/);
  assert.equal(common.jobId('https://search.bilibili.com/all#ai-vtuber-job='+first),first);
  vm.runInContext(`crypto.randomUUID=()=>{throw Error('missing native UUID capability');};`,context);
  assert.match(generate(),/^[a-f0-9-]{36}$/,'a broken UUID API still uses secure random bytes');
  vm.runInContext('delete crypto.getRandomValues;',context);
  assert.throws(generate,error=>error.stage==='random_id'&&error.reason==='secure_random_unavailable');
});
test('timer setup failure leaves no stuck active job and unknown failures are not mislabeled preparation',async()=>{
  const f=chromeFixture();await connect(f);f.worker.setTimer=()=>{throw TypeError('Illegal invocation private-secret');};
  const response=await f.worker.handle(query(),local);
  assert.equal(response.error.stage,'timer_setup');assert.equal(response.error.reason,'timer_unavailable');
  assert.equal(f.worker.active,null);assert.equal(f.calls.filter(call=>call.op==='create').length,0);
  assert.equal(f.worker.failure(Error('private-secret')).error.stage,'request_dispatch');
  assert(!JSON.stringify(response).includes('private-secret'));
});
test('collector cleanup failures never erase a completed result or block its successor',async()=>{
  for(const failure of ['sync_send','non_promise_send','clear_timer']){
    const f=chromeFixture();await connect(f);const old=await begin(f);
    if(failure==='sync_send')f.api.tabs.sendMessage=()=>{throw Error('Extension context invalidated: private-secret');};
    if(failure==='non_promise_send')f.api.tabs.sendMessage=()=>undefined;
    if(failure==='clear_timer')f.worker.clearTimer=()=>{throw TypeError('Illegal invocation');};
    await assert.doesNotReject(()=>result(f,old.job));assert((await old.promise).ok);
    const next=await begin(f,'successor');await result(f,next.job);assert((await next.promise).ok);
    const pending=await begin(f,'pending');const replacement=await begin(f,'replacement');
    assert.equal((await pending.promise).error.code,'cancelled','old cleanup cannot prevent new request ownership');
    await result(f,replacement.job);assert((await replacement.promise).ok);
    f.time.tick(30000);assert.equal(f.worker.active,null,'uncleared old deadlines cannot revive completed work');
  }
});
test('cancel discards old results; repeated keyword reuses own tab with a fresh document',async()=>{
  const f=chromeFixture();await connect(f);const old=await begin(f);
  await f.worker.handle(send('cancel'),local);assert.equal((await old.promise).error.code,'cancelled');
  await result(f,old.job);assert.equal(f.worker.active,null);
  const current=await begin(f,'new');assert.equal(f.calls.filter(c=>c.op==='create').length,1);
  assert.equal(f.calls.filter(c=>c.op==='update'&&c.options.url==='about:blank').length,2);
  await result(f,current.job);assert((await current.promise).ok);
});
test('a newer query cancels the old one and never commandeers a user-repurposed tab',async()=>{
  const f=chromeFixture();await connect(f);const old=await begin(f);const firstId=f.worker.owned.id;
  f.tabs.get(firstId).url='https://example.com/user-page';const next=await begin(f,'new','new query');
  assert.equal((await old.promise).error.code,'cancelled');assert.notEqual(f.worker.owned.id,firstId);
  assert.equal(f.tabs.get(firstId).url,'https://example.com/user-page');await result(f,next.job);assert((await next.promise).ok);
});
test('source, live tab and DOM page URL must identify the requested query',async()=>{
  for(const mismatch of ['sender','live','dom']){
    const f=chromeFixture();await connect(f);const run=await begin(f);const tab=f.tabs.get(f.worker.owned.id),correct=tab.url;
    const wrong=correct.replace('happening','different');if(mismatch==='live')tab.url=wrong;
    const reply=await f.worker.handle({channel:common.channel,action:'collector-result',jobId:run.job.jobId,
      result:{ok:true,data:{page_url:mismatch==='dom'?wrong:correct,results:[{bvid:'BV1xx411c7mD',title:'wrong'}]}}},
      {tab:{id:tab.id},frameId:0,url:mismatch==='sender'?wrong:correct});
    if(mismatch!=='dom'){assert.equal(reply.owned,false);f.time.tick(25000);}
    const failure=await run.promise;assert.equal(failure.ok,false);assert.equal(failure.error.stage,'page_check');
    assert.equal(failure.error.reason,'page_changed');
  }
});
test('safe tracking is allowed while duplicate query and changed page/part are rejected',()=>{
  const id='00000000-0000-4000-8000-000000000001',target=common.target({query:'a'}).url;
  assert(common.samePage(target+'&spm_id_from=333.337#ai-vtuber-job='+id,target,id));
  assert(common.samePage(target.replace('/all?','/video?')+'#ai-vtuber-job='+id,target,id));
  assert(!common.samePage(target.replace('/all?','/live?')+'#ai-vtuber-job='+id,target,id));
  assert(!common.samePage(target+'&keyword=b#ai-vtuber-job='+id,target,id));
  assert(!common.samePage(target+'&page=2#ai-vtuber-job='+id,target,id));
  assert(!common.samePage('https://www.bilibili.com/video/BV1xx411c7mD?p=2#ai-vtuber-job='+id,'https://www.bilibili.com/video/BV1xx411c7mD?p=1',id));
});
test('25-second timeout stops collector without retrying navigation',async()=>{
  const f=chromeFixture();await connect(f);const run=await begin(f);const count=f.calls.filter(c=>c.op==='update').length;
  f.time.tick(25000);const failure=await run.promise;assert.equal(failure.error.code,'timeout');
  assert.equal(failure.error.stage,'read_page');assert.equal(failure.error.reason,'deadline');
  assert.equal(f.calls.filter(c=>c.op==='update').length,count);assert.equal(f.calls.at(-1).message.action,'cancel');
});
test('cancel while tab creation is pending cannot leak a second owned query tab',async()=>{
  const f=chromeFixture(),gate=deferred(),create=f.api.tabs.create;f.api.tabs.create=async options=>{await gate.promise;return create(options);};
  await connect(f);const one=f.worker.handle(query(),local);await flush();
  const two=f.worker.handle(query('new','second'),local);await flush();gate.resolve();await flush();await f.worker.openQueue;
  assert.equal((await one).error.code,'cancelled');assert.equal(f.calls.filter(c=>c.op==='create').length,1);
  await result(f,f.worker.active);assert((await two).ok);
});
test('DOM extracts modern href/xlink play icons without reading danmaku count',()=>{
  for(const marker of ['href','xlink:href']){const {doc,win}=dom([card({marker})]);const result=collector.readPage(doc,win,'search');
    assert(result.ok);assert.equal(result.data.results.length,1);assert.equal(result.data.results[0].title,"it's happening");
    assert.equal(result.data.results[0].play_text,'23.4万');assert.equal(result.data.results[0].author,'UP主');}
});
test('semantic inline icons/data labels and nested tooltips identify complete play fields',()=>{
  for(const semantic of [{ 'aria-label':'播放量' },{'data-icon':'video-play'},{'data-stat':'view_count'},
    {'data-type':'play'},{title:'觀看次數'},{class:'icon-play-count'}]){
    const count=el('span','bili-video-card__stats--text','２３．４萬',[],{title:'總播放數：２３４，５６７次'});
    const icon=new Element('svg',semantic,'',[new Element('path',{d:'M1 1L5 3L1 5Z'})]);
    const stat=el('span','bili-video-card__stats--item','',[icon,count]);const {doc,win}=dom([stat]);
    assert.equal(collector.playText(doc,win),'234,567');
  }
  const titledIcon=new Element('svg',{},'',[new Element('title',{},'播放'),new Element('path',{d:'M1 1L5 3L1 5Z'})]);
  const sample=dom([el('span','bili-video-card__stats--item','',[titledIcon,el('span','bili-video-card__stats--text','１．２億')])]);
  assert.equal(collector.playText(sample.doc,sample.win),'1.2亿');
  for(const raw of ['觀看次數：１２．３萬','總播放數：１，２３４次','１２３次觀看','123 views'])
    assert(collector.playNumber(raw),'normalize full-width/traditional complete counts');
  for(const marker of ['彈幕','評論','點讚','時長','收藏','video-danmaku','like']){
    const stat=el('span','play-count','900000',[],{'aria-label':marker});const {doc,win}=dom([stat]);
    assert.equal(collector.playText(doc,win),'','negative semantics take priority over a conflicting positive class');
  }
});
test('known inline triangle requires the full glyph, exact stats structure and a separate numeric field',()=>{
  const triangle='M14.7138 10.96875C15.50765 11.4271 15.50765 12.573 14.71375 13.0313L11.5362 14.8659C10.74235 15.3242 9.75 14.7513 9.75001 13.8346L9.75001 10.1655C9.75001 9.24881 10.74235 8.67587 11.5362 9.13422L14.7138 10.96875z';
  const frame='M2 4L22 4L22 20L2 20Z',danmaku='M7 10L17 10M7 14L17 14';
  const stat=(paths,attrs={},value='９８．３萬',iconClass='bili-video-card__stats--icon')=>el('span','bili-video-card__stats--item','',[
    new Element('svg',{class:iconClass,viewBox:'0 0 24 24',...attrs},'',paths.map(d=>new Element('path',{d}))),
    el('span','bili-video-card__stats--text',value)]);
  const sample=dom([stat([frame,danmaku],{},'99999999'),stat([frame,triangle])]);
  assert.equal(collector.playText(sample.doc,sample.win),'98.3万','do not use the first high danmaku number');
  for(const rejected of [stat([frame]),stat([frame,danmaku]),stat([triangle],{viewBox:'0 0 1024 1024'}),
    stat([triangle],{},'03:21'),stat([triangle],{},'123 456'),stat([triangle],{},'123','unrelated-icon'),
    stat([triangle],{'aria-label':'彈幕'},'123'),stat([triangle.replace('14.7138','14.7139')])]){
    const {doc,win}=dom([rejected]);assert.equal(collector.playText(doc,win),'','unknown/modified glyphs and durations are not counts');
  }
  const hidden=stat([triangle]);hidden.querySelector('svg').hidden=true;
  const hiddenDoc=dom([hidden]);assert.equal(collector.playText(hiddenDoc.doc,hiddenDoc.win),'');
  const hiddenPath=stat([triangle]);hiddenPath.querySelector('path').hidden=true;
  const hiddenPathDoc=dom([hiddenPath]);assert.equal(collector.playText(hiddenPathDoc.doc,hiddenPathDoc.win),'');
});
test('legacy play labels and precise tooltip values are parsed as entire fields only',()=>{
  const stats=el('span','so-icon watch-num','190.9万播放',[],{title:'总播放数：1,909,123次'}),{doc,win}=dom([stats]);
  assert.equal(collector.playText(doc,win),'1,909,123');delete stats.attrs.title;assert.equal(collector.playText(doc,win),'190.9万');
  for(const value of ['123播放 456弹幕','播放量未知','2026-09-30','123人正在看'])assert.equal(collector.playNumber(value),'');
});
test('unknown stats stay unknown, hidden cards are ignored and result titles are not challenges',()=>{
  const unknown=card({title:'安全验证之歌'});unknown.querySelector('use[href*="play"]').attrs.href='#widget-danmaku';
  const hidden=card({bvid:'BV1B7411m7LV'});hidden.hidden=true;const {doc,win}=dom([unknown,hidden]);
  const response=collector.readPage(doc,win,'search');assert(response.ok);assert.equal(response.data.results.length,1);assert.equal(response.data.results[0].play_text,'');
});

test('missing-count diagnostics sample two missing cards and only bounded public statistic fields',()=>{
  const missing=Array.from({length:3},(_,i)=>card({bvid:'BV'+String(i).padStart(10,'0'),title:'PRIVATE-TITLE-'+i,play:'12.3万 45'}));
  for(const item of missing){
    const play=item.querySelector('use[href*="play"]');play.attrs.href='https://secret.test/?query=PRIVATE-QUERY';
    item.querySelector('.bili-video-card__info--author').value='PRIVATE-AUTHOR';
    const stat=item.querySelectorAll('.bili-video-card__stats--item')[1];
    stat.attrs.title='PRIVATE-LABEL';stat.attrs.class+=' private-secret icon-public-stat';
    const svg=stat.querySelector('svg');svg.attrs.viewBox='0 0 24 24';
    svg.append(new Element('path',{d:'M1 1L2 2Z'}));
    item.append(...Array.from({length:4},()=>el('span','play-count','unknown PRIVATE-TEXT')));
  }
  const page=dom([card(),...missing]);page.doc.visibilityState='hidden';
  const response=collector.readPage(page.doc,page.win,'search');
  const diagnostic=response.data.stats_diagnostic,serialized=JSON.stringify(diagnostic);
  assert.equal(diagnostic.version,1);assert.equal(diagnostic.page_visibility,'hidden');
  assert.deepEqual(diagnostic.cards.map(item=>item.index),[1,2]);
  assert(diagnostic.cards.every(item=>item.stats.length===3));assert(Buffer.byteLength(serialized)<=6144);
  for(const secret of ['PRIVATE-','private-secret','https://','page_url','"title":','"author":','query=','M1 1L2'])
    assert(!serialized.includes(secret),secret+' must not be exported by statistics diagnostics');
  const stat=diagnostic.cards[0].stats[1];
  assert.equal(stat.positive,false);assert.equal(stat.rejected,false);
  assert(stat.node.classes.includes('icon-public-stat'));assert.equal(stat.node.other_classes,1);
  assert.equal(stat.values.find(value=>value.source==='stat_title').invalid,true);
  assert.equal(stat.values.find(value=>value.source==='count_text').text,'12.3万 45');
  assert.equal(stat.values.find(value=>value.source==='count_text').parsed,false);
  assert.deepEqual(stat.icons[0].use,['unknown']);assert.match(stat.icons[0].paths[0].hash,/^[a-f0-9]{8}$/);
  assert.equal(response.data.results[1].play_text,'','diagnosis never relaxes playback count parsing');
  const good=dom([card()]);assert(!('stats_diagnostic' in collector.readPage(good.doc,good.win,'search').data));
});

test('selector misses include only a small named stats-region outline',()=>{
  const item=card({title:'PRIVATE TITLE'});
  item.children=item.children.filter(node=>!node.matches('.bili-video-card__stats--item'));
  const region=el('div','bili-video-card__stats','',[
    el('span','bili-video-card__stats--new-count','９８．３萬'),
    el('span','icon-new-symbol','PRIVATE CONTENT'),
    el('span','unknown-query-secret','123弹幕'),el('span','icon-extra','777')]);
  item.append(region);const {doc,win}=dom([item]),response=collector.readPage(doc,win,'search');
  const diagnostic=response.data.stats_diagnostic.cards[0];
  assert.equal(diagnostic.candidates,0);assert.equal(diagnostic.stats.length,0);
  assert.equal(diagnostic.regions.length,1);assert.equal(diagnostic.regions[0].children.length,3);
  assert.equal(diagnostic.regions[0].children[0].text,'98.3萬');
  assert.deepEqual(diagnostic.regions[0].children[0].node.classes,['bili-video-card__stats--new-count']);
  assert.equal(diagnostic.regions[0].children[1].text,'');assert.equal(diagnostic.regions[0].children[2].node.other_classes,1);
  assert(!JSON.stringify(diagnostic).includes('PRIVATE'));assert(!JSON.stringify(diagnostic).includes('query-secret'));
});

test('worker re-cleans diagnostics, enforces byte/card/field limits and excludes known-count payloads',async()=>{
  const rawStat={node:{tag:'a',classes:['bili-video-card__stats--item','PRIVATE-CLASS','icon-new'],
    display:'url(https://PRIVATE)',visibility:'visible',rects:999,hidden:'PRIVATE',cookie:'PRIVATE'},
    positive:true,rejected:false,count:null,values:Array.from({length:9},(_,i)=>({source:i%2?'count_text':'stat_aria',
      text:i%2?'123.4万播放 '.repeat(6):'PRIVATE-TITLE',parsed:true,extra:'PRIVATE'})),
    icons:[{node:{tag:'svg'},view_box:[0,0,24,Infinity],path_count:999,use:['https://PRIVATE','#widget-play-count'],
      paths:Array.from({length:5},()=>({length:9999,hash:'abcd1234',d:'PRIVATE-PATH',known:true,rects:999}))}],
    html:'PRIVATE-FULL-HTML'};
  const forged={version:1,page_visibility:'PRIVATE-VISIBILITY',query:'PRIVATE-QUERY',cards:Array.from({length:10},()=>({index:999,candidates:999,
    title:'PRIVATE-TITLE',stats:Array.from({length:10},()=>rawStat)}))};
  const f=chromeFixture();await connect(f);const run=await begin(f),tab=f.tabs.get(f.worker.owned.id);
  await result(f,run.job,undefined,{result:{ok:true,data:{page_url:tab.url,
    results:[{bvid:'BV1xx411c7mD',title:'normal',play_text:''}],stats_diagnostic:forged}}});
  const received=await run.promise,diagnostic=received.data.stats_diagnostic,serialized=JSON.stringify(diagnostic);
  assert.equal(diagnostic.cards.length,2);assert(diagnostic.cards.every(item=>item.stats.length<=3));
  assert.equal(diagnostic.page_visibility,'unknown');
  assert(Buffer.byteLength(serialized)<=6144);assert(!serialized.includes('PRIVATE'));assert(!serialized.includes('Infinity'));
  assert.equal(diagnostic.cards[0].stats[0].node.rects,4);
  assert.deepEqual(diagnostic.cards[0].stats[0].icons[0].view_box,[]);
  const known=await begin(f,'known'),knownTab=f.tabs.get(f.worker.owned.id);
  await result(f,known.job,undefined,{result:{ok:true,data:{page_url:knownTab.url,
    results:[{bvid:'BV1xx411c7mD',title:'normal',play_text:'22万'}],stats_diagnostic:forged}}});
  assert(!('stats_diagnostic'in(await known.promise).data),'known counts cannot attach a diagnostic');
});

test('cancelled missing-count collection cannot publish diagnostic data or late worker results',async()=>{
  const page=dom([card({play:''})]),time=clock(),received=[];
  const stop=collector.collect(page.doc,page.win,'search',data=>received.push(data),time);
  time.tick(3999);stop();time.tick(26000);assert.equal(received.length,0);
  const f=chromeFixture();await connect(f);const run=await begin(f),tab=f.tabs.get(f.worker.owned.id);
  const data=collector.readPage(page.doc,page.win,'search').data;assert(data.stats_diagnostic);
  await f.worker.handle(send('cancel'),local);assert.equal((await run.promise).error.code,'cancelled');
  await result(f,run.job,undefined,{result:{ok:true,data:{...data,page_url:tab.url}}});
  assert.equal(f.worker.active,null);assert.equal((await run.promise).error.code,'cancelled');
});
test('verification, required login and refused pages return clear errors',()=>{
  for(const [value,code] of [['请完成验证','verification_required'],['请先登录后查看','login_required'],['访问被拒绝','page_unavailable']]){
    const {doc,win}=dom([el('div','',value)]);assert.equal(collector.readPage(doc,win,'search').error.code,code);
  }
  const {doc,win}=dom([el('div','geetest_panel','')]);assert.equal(collector.readPage(doc,win,'search').error.code,'verification_required');
});
test('video page uses named view field, keeps exact part URL, and length limits match backend',()=>{
  const {doc,win}=dom([el('h1','video-title','歌'.repeat(270)),el('span','up-name','作'.repeat(110)),
    el('div','video-info-detail-list','',[el('span','view-text','20.0万次')])],
    'https://www.bilibili.com/video/BV1xx411c7mD?p=2');
  const response=collector.readPage(doc,win,'video');assert.equal(response.data.results[0].play_text,'20.0万');
  assert.equal(response.data.results[0].title.length,250);assert.equal(response.data.results[0].author.length,100);assert(response.data.page_url.endsWith('p=2'));
});
test('normal BV title/description do not masquerade as a login or verification page',()=>{
  const {doc,win}=dom([el('h1','video-title','安全验证之歌'),el('div','video-desc','登录后才能：这是视频简介的文字')],
    'https://www.bilibili.com/video/BV1xx411c7mD');
  assert(collector.readPage(doc,win,'video').ok);
  doc.body.append(el('div','login-panel','请先登录'));
  assert.equal(collector.readPage(doc,win,'video').error.code,'login_required');
});
test('collector caps 30 unique visible results',()=>{
  const cards=Array.from({length:35},(_,i)=>card({bvid:'BV'+String(i).padStart(10,'0')}));cards.unshift(cards[0]);
  const {doc,win}=dom(cards);assert.equal(collector.readPage(doc,win,'search').data.results.length,30);
});
test('collector waits for stable cards and cancellation stops polling/results',()=>{
  const {doc,win}=dom(),time=clock(),answers=[];const stop=collector.collect(doc,win,'search',value=>answers.push(value),time);
  time.tick(1000);assert.equal(answers.length,0);doc.body.append(card());time.tick(750);assert.equal(answers.length,0);time.tick(750);assert.equal(answers.length,1);
  const second=dom(),later=[];const cancel=collector.collect(second.doc,second.win,'search',value=>later.push(value),time);
  second.doc.body.append(card());cancel();time.tick(30000);assert.equal(later.length,0);stop();
});
test('late statistics get a short grace period while permanently unknown counts remain unknown',()=>{
  const late=card({play:''}),{doc,win}=dom([late]),time=clock(),answers=[];
  collector.collect(doc,win,'search',value=>answers.push(value),time);
  time.tick(1500);assert.equal(answers.length,0,'title-only stability must not submit empty counts after one second');
  late.querySelector('.bili-video-card__stats--text').value='32.1万';
  time.tick(1500);assert.equal(answers.length,1);assert.equal(answers[0].data.results[0].play_text,'32.1万');

  const unknown=dom([card({play:''})]),unknownTime=clock(),emptyCounts=[];
  collector.collect(unknown.doc,unknown.win,'search',value=>emptyCounts.push(value),unknownTime);
  unknownTime.tick(3999);assert.equal(emptyCounts.length,0);unknownTime.tick(1);
  assert.equal(emptyCounts.length,1);assert(emptyCounts[0].ok);assert.equal(emptyCounts[0].data.results[0].play_text,'');
  assert.equal(unknownTime.timers.size,0,'all-unknown statistics do not poll until the 24-second deadline');

  const cancelled=dom([card({play:''})]),cancelTime=clock(),cancelledResults=[];
  const stop=collector.collect(cancelled.doc,cancelled.win,'search',value=>cancelledResults.push(value),cancelTime);
  cancelTime.tick(1500);stop();cancelled.doc.querySelector('.bili-video-card__stats--text').value='99万';
  cancelTime.tick(25000);assert.equal(cancelledResults.length,0);
});
test('statistics grace and total timeout never bypass the one-second full-row stability check',()=>{
  const late=card({play:''}),page=dom([late]),time=clock(),answers=[];
  collector.collect(page.doc,page.win,'search',value=>answers.push(value),time);
  time.tick(3900);late.querySelector('.bili-video-card__stats--text').value='99.1万';
  time.tick(100);assert.equal(answers.length,0,'new statistics at the grace boundary must settle');
  time.tick(999);assert.equal(answers.length,0);time.tick(1);assert.equal(answers[0].data.results[0].play_text,'99.1万');
  const delayed=dom(),deadline=clock(),timed=[];
  collector.collect(delayed.doc,delayed.win,'search',value=>timed.push(value),deadline);
  deadline.tick(23750);delayed.doc.body.append(card());deadline.tick(250);
  assert.equal(timed.length,1);assert.equal(timed[0].error.code,'timeout','do not return newly appearing unstable rows at 24 seconds');
});
test('document-start media guard changes only confirmed owned media, including new nodes/play',()=>{
  const video=new Element('video');video.paused=false;video.muted=false;video.pause=function(){this.paused=true;this.pauses=(this.pauses||0)+1;};
  const {doc,win,observers}=dom([video]);const guard=collector.mediaGuard(doc,win);
  doc.fire('play',video);assert.equal(video.muted,false);assert.equal(video.paused,false);
  guard.enable();assert.equal(video.muted,true);assert.equal(video.paused,true);
  video.paused=false;video.muted=false;doc.fire('play',video);assert.equal(video.paused,true);assert.equal(video.muted,true);
  const audio=new Element('audio');audio.paused=false;audio.pause=function(){this.paused=true;};doc.body.append(audio);observers[0].fn();assert(audio.muted&&audio.paused);
  guard.stop();assert(observers[0].disconnected);
});
test('copied job hash in an unrelated user tab does not authorize media changes',async()=>{
  const {doc,win}=dom([], 'https://www.bilibili.com/video/BV1xx411c7mD#ai-vtuber-job=00000000-0000-4000-8000-000000000001');
  const video=new Element('video');video.paused=false;video.muted=false;video.pause=()=>{throw Error('unowned media touched');};doc.body.append(video);
  collector.install(doc,win,{onMessage:{addListener(){}},sendMessage:async()=>({owned:false})});await flush();doc.fire('play',video);assert.equal(video.muted,false);
});

(async()=>{for(const item of cases){await item.run();console.log('PASS: '+item.name);}console.log(`PASS: ${cases.length} browser extension checks`);})()
  .catch(error=>{console.error(error);process.exitCode=1;});
