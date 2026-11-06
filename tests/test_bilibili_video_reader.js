const assert=require('node:assert/strict');
const test=require('node:test');
const {Reader}=require('../tools/bilibili-browser-extension/activity-reader.js');
const {BrowserQueries}=require('../tools/bilibili-browser-extension/background.js');
const BrowserSearch=require('../brain/static/bilibili-browser.js');
const BV='BV1B7411m7LV',URL2=`https://www.bilibili.com/video/${BV}?p=2`;
const SUB='https://aisubtitle.hdslb.com/bfs/ai_subtitle/example.json';
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return{resolve,promise};};
const flush=async()=>{for(let i=0;i<35;i++)await Promise.resolve();};
const info={bvid:BV,cid:101,title:'正确标题',desc:'简介',pages:[{page:1,cid:101,part:'第一P'},{page:2,cid:202,part:'第二P'}]};
function readerFixture({view=info,tracks=[{lan:'zh-CN',subtitle_url:SUB}],body=[{from:0,to:2,content:'P2字幕'}],failure}={}) {
  const calls=[];
  const reader=new Reader({fetcher:async(url,options)=>{
    calls.push({url,options});let result;
    if(failure)return new Response(JSON.stringify({code:failure}),{headers:{'content-type':'application/json'}});
    if(url.includes('/view?'))result={code:0,data:view};
    else if(url.includes('/nav'))result={code:0,data:{wbi_img:{img_url:'https://i0.hdslb.com/bfs/wbi/0123456789abcdef0123456789abcdef.png',sub_url:'https://i0.hdslb.com/bfs/wbi/fedcba9876543210fedcba9876543210.png'}}};
    else if(url.includes('/wbi/v2?'))result={code:0,data:{subtitle:{subtitles:tracks}}};
    else if(url===SUB)result={body};
    else throw Error('Unexpected request');
    return new Response(JSON.stringify(result),{headers:{'content-type':'application/json'}});
  }});
  return{reader,calls};
}
test('pointed P2 selects P2 cid and only subtitle APIs, never P1 or comments',async()=>{
  const f=readerFixture(),result=await f.reader.readVideo(URL2);
  assert.equal(result.video_part,2);assert.equal(result.video_cid,202);assert.equal(result.part_title,'第二P');
  assert.deepEqual(result.subtitles,['P2字幕']);assert.equal(result.evidence_kind,'full_subtitles');
  assert(f.calls.some(c=>c.url.includes('cid=202')));assert(!f.calls.some(c=>c.url.includes('cid=101')||c.url.includes('/reply')));
  assert(f.calls.every(c=>c.options.method==='GET'&&c.options.redirect==='error'));
  assert.equal(f.calls.at(-1).options.credentials,'omit');
});
test('missing part, wrong video, absent and unsafe subtitles cannot fall back to P1 or DOM cues',async()=>{
  const missing=readerFixture({view:{...info,pages:[info.pages[0]]}});
  assert.equal((await missing.reader.readVideo(URL2)).subtitle_status,'part_unavailable');assert.equal(missing.calls.length,1);
  const wrong=readerFixture({view:{...info,bvid:'BV9B7411m7LV'}});
  assert.equal((await wrong.reader.readVideo(URL2)).title,'');assert.equal(wrong.calls.length,1);
  for(const tracks of [[],[{lan:'zh-CN',subtitle_url:'https://evil.invalid/a.json'}]]) {
    const f=readerFixture({tracks}),result=await f.reader.readVideo(URL2);
    assert.deepEqual(result.subtitles,[]);assert.equal(result.evidence_kind,'page_metadata');
    assert.equal(result.subtitle_status,tracks.length?'unavailable':'none');
  }
});
test('login, risk control and transcript truncation are explicit statuses',async()=>{
  for(const [failure,status] of [[-101,'login_required'],[-412,'blocked']]){
    const f=readerFixture({failure});assert.equal((await f.reader.readVideo(URL2)).subtitle_status,status);assert.equal(f.calls.length,1);
  }
  const result=await readerFixture({body:[{from:0,to:1,content:'甲'.repeat(12001)}]}).reader.readVideo(URL2);
  assert.equal(result.evidence_kind,'subtitle_excerpt');assert.equal(result.subtitles.join('\n').length,12000);
});
test('read timeout and explicit abort return promptly even if transport ignores abort',async()=>{
  const late=deferred(),reader=new Reader({timeout:5,fetcher:()=>late.promise});
  assert.equal((await reader.readVideo(URL2)).subtitle_status,'unavailable');
  late.resolve(new Response(JSON.stringify({code:0,data:info}),{headers:{'content-type':'application/json'}}));
  const controller=new AbortController(),never=new Reader({fetcher:()=>new Promise(()=>{})});
  const pending=never.readVideo(URL2,{signal:controller.signal});controller.abort();
  await assert.rejects(pending,error=>error==='cancelled');
});
test('extension read ownership is document-bound and cancellation is independent of search',async()=>{
  const pending=deferred(),calls=[],saved={},listeners={};
  const api={storage:{session:{get:async()=>saved,set:async value=>Object.assign(saved,value)}},
    runtime:{getManifest:()=>({version:'1.3.0'}),onMessage:{addListener:fn=>listeners.message=fn}},
    tabs:{onRemoved:{addListener:fn=>listeners.removed=fn}}};
  const worker=new BrowserQueries(api,{reader:{readVideo:async(url,{signal})=>{calls.push({url,signal});return pending.promise;}}});
  const owner={tab:{id:7},frameId:0,documentId:'first',url:'http://127.0.0.1:8080/'};
  assert.equal((await worker.handle({id:'r',action:'read_video',url:URL2},owner)).error.code,'not_connected');
  const hello=await worker.handle({id:'c',action:'connect'},owner);assert(hello.data.capabilities.includes('video_subtitles_v1'));
  const search={id:'search',owner:7};worker.active=search;
  const read=worker.handle({id:'r',action:'read_video',url:URL2},owner);await flush();
  await worker.handle({id:'r',action:'cancel'},owner);assert(calls[0].signal.aborted);assert.equal(worker.active,search);
  pending.resolve({subtitles:['late']});assert.equal((await read).error.code,'cancelled');
  assert.equal((await worker.handle({id:'r2',action:'read_video',url:URL2},{...owner,documentId:'new'})).error.code,'not_connected');
});
function browserFixture({capabilities=['video_subtitles_v1'],resultGate}={}) {
  const events={},requests=[],posts=[];
  const win={location:{origin:'http://localhost:8080'},addEventListener:(name,fn)=>events[name]=fn,
    postMessage(message){posts.push(message);const reply=data=>events.message({source:win,origin:win.location.origin,data:{source:'ai-vtuber-extension',type:'bilibili-browser-response',id:message.id,ok:true,data}});
      if(message.action==='connect')reply({connected:true,version:1,extension_version:'1.3.0',capabilities});
      if(message.action==='read_video')reply({url:message.url,subtitles:['真实字幕']});
    }};
  const receipt={evidence_id:'evidence',query:URL2,url:URL2,bvid:BV,part:2,evidence_kind:'full_subtitles',subtitle_status:'ready'};
  const provider=new BrowserSearch({windowRef:win,fetcher:async(path,options)=>{
    const body=JSON.parse(options.body);requests.push({path,body});
    if(path==='/bilibili-video/request')return{ok:true,json:async()=>({request_id:'request',query:URL2,url:URL2})};
    if(path==='/bilibili-video/result')return resultGate?resultGate.promise:{ok:true,json:async()=>receipt};
    return{ok:true,json:async()=>({ok:true})};
  }});
  return{provider,requests,posts,receipt,events};
}
test('browser readVideo gets receipt while keeping search state and revokes on stop',async()=>{
  const f=browserFixture(),search={id:'search'};f.provider.flow=search;
  const receipt=await f.provider.readVideo(URL2);
  assert.equal(receipt.evidence_id,'evidence');assert.equal(f.provider.flow,search);
  f.provider.cancelRead();await flush();assert.equal(f.provider.flow,search);
  assert(f.requests.some(r=>r.path==='/bilibili-video/cancel'&&r.body.evidence_id==='evidence'));
});
test('old extensions fail before registration; late saved evidence is revoked after abort',async()=>{
  const old=browserFixture({capabilities:[]});
  await assert.rejects(old.provider.readVideo(URL2),error=>error.code==='extension_upgrade_required');
  assert.equal(old.requests.length,0);
  const gate=deferred(),f=browserFixture({resultGate:gate}),controller=new AbortController();
  const read=f.provider.readVideo(URL2,{signal:controller.signal});await flush();controller.abort();
  await assert.rejects(read,error=>error.name==='AbortError');
  gate.resolve({ok:true,json:async()=>f.receipt});await flush();
  assert(f.requests.some(r=>r.path==='/bilibili-video/cancel'&&r.body.evidence_id==='evidence'));
});
