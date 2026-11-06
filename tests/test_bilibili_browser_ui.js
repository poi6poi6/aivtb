const assert = require('node:assert/strict');
const BrowserSearch = require('../brain/static/bilibili-browser.js');
const BV = 'BV1B7411m7LV';
const response = data => ({ok:true,json:async () => data});
const defer = () => { let resolve; const promise = new Promise(done => {resolve=done;}); return {promise,resolve}; };
const flush = async () => {for(let i=0;i<30;i++) await Promise.resolve();};
function fixture({onPost,fetcher,connectTimeoutMs=25,requestTimeoutMs=200,searchTimeoutMs=200}={}) {
  const listeners = new Map(), posts=[], requests=[];
  const windowRef = {location:{origin:'http://localhost:8080'},
    addEventListener(type,callback){listeners.set(type,callback);},
    postMessage(message,origin){
      posts.push({message,origin});
      if(onPost) return onPost(message,reply);
      if(message.action==='connect') reply(message.id,{connected:true,version:1,extension_version:'1.0.1'});
      if(message.action==='search') reply(message.id,{page_url:message.url,results:[{bvid:BV,title:'Actual page title',play_text:'42万'}]});
    },
  };
  const emit = (message,extra={}) => listeners.get('message')({data:message,source:windowRef,origin:windowRef.location.origin,...extra});
  const reply = (id,data) => emit({source:'ai-vtuber-extension',type:'bilibili-browser-response',id,ok:true,data});
  let serial=0;
  const provider = new BrowserSearch({windowRef,connectTimeoutMs,requestTimeoutMs,searchTimeoutMs,fetcher:async (url,options) => {
    const body=JSON.parse(options.body); requests.push({url,options,body});
    if(fetcher) return fetcher(url,options,body);
    if(url.endsWith('/request')) return response({request_id:'request-'+(++serial),query:body.query,
      kind:'search',url:'https://search.bilibili.com/all?keyword='+encodeURIComponent(body.query)});
    if(url.endsWith('/result')) return response({query:"it's happening",result_id:'result-'+serial,
      results:body.results,auto_bvid:BV,selection_reason:'highest_play'});
    return response({cancelled:true});
  }});
  return {provider,windowRef,posts,requests,reply,emit,pagehide:()=>listeners.get('pagehide')()};
}
async function main(){
  const f=fixture(), controller=new AbortController();
  const result=await f.provider.search("it's happening",{signal:controller.signal});
  assert.equal(result.result_id,'result-1');
  assert.equal(f.provider.extensionVersion,'1.0.1');
  assert.equal(result.results[0].title,'Actual page title');
  assert.deepEqual(f.requests.map(r=>r.url),['/bilibili-browser/request','/bilibili-browser/result']);
  assert.equal(f.requests[0].body.query,"it's happening");
  assert.equal(f.requests[1].body.request_id,'request-1');
  assert.equal(f.posts.every(p=>p.origin==='http://localhost:8080'),true);
  assert.equal(f.requests.every(r=>r.options.method==='POST'),true);
  controller.abort();await flush();
  assert(f.requests.some(r=>r.url.endsWith('/cancel')&&r.body.request_id==='request-1'&&r.body.result_id==='result-1'));
  assert.equal(f.provider.pending.size,0);
  console.log('PASS: real helper handshake reads browser results, registers a snapshot and revokes both capabilities on stop');

  const statsDiagnostic={version:1,cards:[{index:0,candidates:0,stats:[]}]};
  for (const diagnostics of [statsDiagnostic,{version:2,cards:[]},{version:1,cards:[],extra:'x'.repeat(6200)},
    {version:1,cards:[],extra:'量'.repeat(2200)}]) {
    const observed=fixture({onPost:(message,reply)=>{
      if(message.action==='connect')reply(message.id,{connected:true,version:1,extension_version:'1.0.4'});
      if(message.action==='search')reply(message.id,{page_url:message.url,
        results:[{bvid:BV,title:'Actual title',play_text:''}],stats_diagnostic:diagnostics});
    }});
    const searchResult=await observed.provider.search("it's happening");
    assert.deepEqual(searchResult.stats_diagnostic,diagnostics===statsDiagnostic?statsDiagnostic:undefined);
    assert(observed.requests.every(request=>!Object.hasOwn(request.body,'stats_diagnostic')),
      'diagnostic data is never posted to the local server');
    observed.provider.cancel();
  }
  console.log('PASS: bounded public statistics diagnostics stay in this browser response and never enter server snapshots');

  const partURL='https://www.bilibili.com/video/'+BV+'?p=3';
  const part=fixture({fetcher:async(url,options,body)=>{
    if(url.endsWith('/request')){
      assert.equal(body.query,partURL);
      return response({request_id:'part-request',query:body.query,kind:'video',url:partURL});
    }
    if(url.endsWith('/result')){
      assert.equal(body.page_url,partURL,'the exact requested video part is read before snapshot registration');
      return response({result_id:'part-result',query:partURL,results:body.results,auto_bvid:BV,selection_reason:'explicit_video'});
    }
    return response({cancelled:true});
  }});
  const partResult=await part.provider.search(partURL);
  assert.equal(partResult.query,partURL);
  assert.equal(part.posts.find(p=>p.message.action==='search').message.kind,'video');
  part.provider.cancel();
  console.log('PASS: explicit multipart links bind request, browser page and returned snapshot to the same part');

  let handshake;
  const spoof=fixture({onPost:message=>{if(message.action==='connect')handshake=message;}});
  let resolved=false;const connecting=spoof.provider.connect().then(()=>{resolved=true;});
  const packet={source:'ai-vtuber-extension',type:'bilibili-browser-response',id:handshake.id,ok:true,data:{connected:true,version:1}};
  spoof.emit(packet,{origin:'https://evil.test'});spoof.emit(packet,{source:{}});await flush();
  assert.equal(resolved,false,'foreign origins and frames cannot complete the handshake');
  spoof.reply(handshake.id,packet.data);await connecting;
  assert.equal(spoof.provider.extensionVersion,null,'legacy handshakes cannot masquerade as the diagnostic extension');
  const absent=fixture({onPost:()=>{},connectTimeoutMs:2});
  await assert.rejects(absent.provider.search('anything'),error=>error.code==='browser_required');
  assert.equal(absent.requests.length,0,'missing helper cannot silently fall back to an anonymous API');
  assert.equal(absent.provider.pending.size,0);
  console.log('PASS: strict source/origin handshake rejects spoofing and missing installation does not query the server');

  for(const code of ['verification_required','login_required','page_unavailable','not_connected','extension_unavailable']){
    let blocked;
    blocked=fixture({onPost:(message,reply)=>{
      if(message.action==='connect')reply(message.id,{connected:true,version:1});
      if(message.action==='search')blocked.emit({source:'ai-vtuber-extension',type:'bilibili-browser-response',
        id:message.id,ok:false,error:{code,message:'页面需要处理'}});
    }});
    await assert.rejects(blocked.provider.search('query'),error=>error.code===
      (['not_connected','extension_unavailable'].includes(code)?'browser_disconnected':code));
    assert.equal(blocked.requests.some(r=>r.url.endsWith('/result')),false);
    assert(blocked.requests.some(r=>r.url.endsWith('/cancel')));
  }
  console.log('PASS: verification/login/disconnection are explicit outcomes and never become fabricated search results');

  for(const [stage,reason] of [['tab_create','chrome_api_error'],['tab_save','storage_error'],['job_prepare','window_unavailable'],['read_page','deadline'],['request_dispatch','unexpected_error'],['random_id','secure_random_unavailable'],['timer_setup','timer_unavailable'],['https://private.test/?secret=x','private page body']]){
    let diagnostic;
    diagnostic=fixture({onPost:(message,reply)=>{
      if(message.action==='connect')reply(message.id,{connected:true,version:1,extension_version:'1.0.1'});
      if(message.action==='search')diagnostic.emit({source:'ai-vtuber-extension',type:'bilibili-browser-response',
        id:message.id,ok:false,error:{code:'page_unavailable',message:'页面暂时不可用',stage,reason}});
    }});
    await assert.rejects(diagnostic.provider.search('query'),error=>{
      assert.equal(error.stage,stage.startsWith('https:')?null:stage);
      assert.equal(error.diagnostic_reason,stage.startsWith('https:')?null:reason);
      assert.equal(error.message,'页面暂时不可用');return true;
    });
  }
  console.log('PASS: extension versions are factual and only fixed diagnostic stage/reason enums pass through the helper');

  for(const stage of ['request','result']){
    const delayed=defer();
    const late=fixture({fetcher:async (url,options,body)=>{
      if(url.endsWith('/'+stage))return delayed.promise;
      if(url.endsWith('/request'))return response({request_id:'old-request',query:'old',kind:'search',url:'https://search.bilibili.com/all?keyword=old'});
      return response({cancelled:true});
    }});
    const signal=new AbortController();
    const searching=late.provider.search('old',{signal:signal.signal});await flush();
    signal.abort();await assert.rejects(searching,error=>error.name==='AbortError');
    delayed.resolve(response(stage==='request'?{request_id:'late-request',query:'old',kind:'search',url:'https://search.bilibili.com/all?keyword=old'}
      :{query:'old',result_id:'late-result',results:[],auto_bvid:null,selection_reason:'empty'}));await flush();
    assert(late.requests.some(r=>r.url.endsWith('/cancel')&&r.body[stage==='request'?'request_id':'result_id']==='late-'+stage));
    assert.equal(late.provider.flow,null);
    assert.equal(late.provider.pending.size,0);
  }
  console.log('PASS: cancellation returns promptly and request/result tokens minted by late HTTP responses are still revoked');

  const never=defer();let postSignal;
  const timeout=fixture({requestTimeoutMs:3,fetcher:async(url,options)=>{
    if(url.endsWith('/request')){postSignal=options.signal;return never.promise;}
    return response({cancelled:true});
  }});
  await assert.rejects(timeout.provider.search('late'),error=>error.code==='timeout');
  assert.equal(postSignal.aborted,true,'a stuck HTTP request has a bounded real abort');
  never.resolve(response({request_id:'after-timeout',query:'late',kind:'search',url:'https://search.bilibili.com/all?keyword=late'}));await flush();
  assert(timeout.requests.some(r=>r.url.endsWith('/cancel')&&r.body.request_id==='after-timeout'));
  assert.equal(timeout.posts.some(p=>p.message.action==='search'),false);
  const stalled=fixture({searchTimeoutMs:3,onPost:(message,reply)=>{
    if(message.action==='connect')reply(message.id,{connected:true,version:1});
  }});
  await assert.rejects(stalled.provider.search('stalled'),error=>error.code==='timeout');
  assert.equal(stalled.provider.pending.size,0);
  assert(stalled.requests.some(r=>r.url.endsWith('/cancel')));
  console.log('PASS: both HTTP and extension deadlines release waiters; late capabilities cannot survive timeout');

  const oldResult=defer();let currentQuery, count=0;
  const switching=fixture({fetcher:async(url,options,body)=>{
    if(url.endsWith('/request')){currentQuery=body.query;return response({request_id:'request-'+body.query,query:body.query,kind:'search',url:'https://search.bilibili.com/all?keyword='+body.query});}
    if(url.endsWith('/result'))return ++count===1?oldResult.promise:response({query:currentQuery,result_id:'new-result',results:[{bvid:BV}],auto_bvid:BV,selection_reason:'highest_play'});
    return response({cancelled:true});
  }});
  const old=switching.provider.search('old');await flush();
  const newest=switching.provider.search('new');await assert.rejects(old,error=>error.name==='AbortError');
  assert.equal((await newest).result_id,'new-result');
  oldResult.resolve(response({query:'old',result_id:'old-result',results:[]}));await flush();
  assert.equal(switching.provider.flow.resultId,'new-result');
  assert(switching.requests.some(r=>r.url.endsWith('/cancel')&&r.body.result_id==='old-result'));
  assert(!switching.requests.some(r=>r.url.endsWith('/cancel')&&r.body.result_id==='new-result'));
  switching.pagehide();await flush();
  assert(switching.requests.some(r=>r.url.endsWith('/cancel')&&r.body.result_id==='new-result'));
  console.log('PASS: a newer browser search owns its result while late old snapshots and page-unload tokens are revoked');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
