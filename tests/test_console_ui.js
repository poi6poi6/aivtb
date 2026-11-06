const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
assert.equal(require('../brain/static/console.js').voiceLabel({tts:'fish_speech'}), 'Fish Speech');
const source = fs.readFileSync(path.join(__dirname, '../brain/static/console.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '../brain/static/index.html'), 'utf8');
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
}
class Element {
  constructor() {
    this.value='';this.textContent='';this.hidden=false;this.disabled=false;this.listeners={};
    this.dataset={};this.attributes={};this.tagName='DIV';
    this.classes=new Set();this.classList={toggle:(key,on)=>on?this.classes.add(key):this.classes.delete(key)};
  }
  addEventListener(name,callback) {this.listeners[name]=callback;}
  setAttribute(key,value) {this.attributes[key]=value;}
  removeAttribute(key) {delete this.attributes[key];}
  scrollIntoView() {this.scrolled=true;}
}
function fixture(search='') {
  const nodes=new Map([...html.matchAll(/<[a-z][^>]*\bid="([^"]+)"[^>]*>/gi)].map(match=>{
    const element=new Element();element.hidden=/\shidden(?:[=>\s])/.test(match[0]);
    return [match[1],element];
  }));
  const node=id=>{assert(nodes.has(id),`real DOM element missing: ${id}`);return nodes.get(id);};
  node('test-scenario').value='chat';
  const liveLink=new Element(), navLink=new Element();navLink.dataset.consoleTarget='local-test-panel';
  node('local-test-panel').tagName='DETAILS';
  const timers=new Map(),requests=[],sent=[],drafts=[];
  let serial=0,sendOK=true,draftOK=true,reconnects=0,audioCalls=0,audioError=false,audioHook=null;
  const context={window:{},document:{getElementById:node,documentElement:new Element(),
    querySelectorAll:selector=>selector==='[data-console-target]'?[navLink]:[liveLink]},
    location:{search},URLSearchParams,AbortSignal:{timeout:()=>undefined},
    setTimeout:fn=>{timers.set(++serial,fn);return serial;},clearTimeout:id=>timers.delete(id),
    navigator:{clipboard:{writeText:async()=>{throw new Error('denied');}}},
    fetch:(url,options)=>{const request=deferred();requests.push({url,options,...request});return request.promise;}};
  vm.runInNewContext(source,context);
  const ui=context.window.ConsoleUI;
  ui.init({send:payload=>{sent.push(payload);return sendOK;},
    draft:payload=>{drafts.push(payload);return draftOK;},reconnect:()=>reconnects++,
    enableAudio:async()=>{audioCalls++;if(audioHook)audioHook();if(audioError)throw Error('blocked');}});
  const hello=(extra={})=>ui.message({type:'hello',llm:'configured-model',tts:'configured-voice',
    audio_owner:true,audio_target:'control',playback:{current_id:null,queued:0},...extra});
  const select=name=>{node('test-scenario').value=name;node('test-scenario').onchange();};
  const run=()=>node('test-run').onclick();
  const event=(id,extra={})=>ui.message({type:'event',id,source:'companion',kind:'chat',
    user:'本地测试员',text:node('test-preview').value,local_test:true,...extra});
  return {ui,node,hello,select,run,event,requests,sent,drafts,timers,liveLink,navLink,
    sendOK:value=>sendOK=value,draftOK:value=>draftOK=value,audioError:value=>audioError=value,
    audioHook:value=>audioHook=value,
    reconnects:()=>reconnects,audioCalls:()=>audioCalls};
}
const accepted=id=>({ok:true,json:async()=>({accepted:true,turn_id:id,local_test:true})});
(async()=>{
  let f=fixture();
  assert.equal(f.node('test-run').disabled,true);
  f.hello();
  assert.equal(f.node('console-update-notice').hidden,false,'old Python service cannot be presented as fully updated');
  assert.match(f.node('console-update-notice').textContent,/旧版主服务/);
  f.ui.message({type:'audio_owner',audio_owner:true});
  assert.equal(f.node('console-update-notice').hidden,false,'audio ownership notices must not hide the stale-service warning');
  f.hello({music_protocol:2});
  assert.equal(f.node('console-update-notice').hidden,false,'top-play sorting requires the newer narration service');
  f.hello({music_protocol:3});
  assert.equal(f.node('console-update-notice').hidden,false,'minimum play count requires the newer narration service');
  f.hello({music_protocol:4});
  assert.equal(f.node('console-update-notice').hidden,false,'reliable search and error narration require the newer service');
  f.hello({music_protocol:5});
  assert.equal(f.node('console-update-notice').hidden,false,'browser-result narration requires protocol 6');
  f.hello({music_protocol:6});
  assert.equal(f.node('console-update-notice').hidden,true,'a new service handshake clears the restart notice');
  assert.equal(f.node('console-enable-audio').hidden,false,'connected host can see the initially hidden audio action');
  assert.equal(f.node('console-reconnect').hidden,true);
  assert.equal(f.node('console-notice').hidden,false,'status feedback must be visible');
  assert.equal(f.node('console-local-banner').hidden,true);
  assert.equal(f.node('console-voice').textContent,'configured-voice');
  f.node('test-preview').value='主持人修改的测试草稿';
  await f.run();
  assert.equal(f.drafts[0].text,'主持人修改的测试草稿');
  assert.equal(f.sent.length,0);assert.equal(f.requests.length,0);
  assert.match(f.node('test-result').textContent,/尚未发送/);
  f.draftOK(false);await f.run();
  assert.match(f.node('test-result').textContent,/未发送的输入/);
  assert.equal(f.node('test-preview').value,'主持人修改的测试草稿');
  f.node('test-preview').value='  ';await f.run();
  assert.equal(f.drafts.length,2,'empty drafts never replace input');
  f.select('reply_error');await f.run();
  assert.equal(f.node('test-run').disabled,true);
  assert.equal(f.requests.length,0,'formal service must never run offline POST scenarios');
  f.navLink.listeners.click();assert.equal(f.node('local-test-panel').open,true);
  await f.node('test-copy-command').onclick();
  assert.match(f.node('test-result').textContent,/启动\.cmd -Action local-test/);
  console.log('PASS: formal console only fills explicit drafts, preserves user input and never invokes offline scenarios');

  for (const [search,extra] of [['?display=1',{}],['',{public_test:true}]]) {
    f=fixture(search);f.hello(extra);
    for(const id of ['console-summary','console-nav','local-test-panel'])assert.equal(f.node(id).hidden,true);
    assert.equal(f.node('test-run').disabled,true);
    assert.equal(f.node('console-enable-audio').disabled,true);
    assert.equal(f.node('console-enable-audio').hidden,true);
    assert.equal(f.node('console-reconnect').hidden,true);
    assert.equal(f.node('console-notice').hidden,true);
    assert.equal(f.node('console-update-notice').hidden,true);
    await f.run();assert.equal(f.drafts.length+f.sent.length+f.requests.length,0);
  }
  console.log('PASS: OBS and public-test surfaces have no console management actions');

  f=fixture();f.hello({local_test:true,tts:'local-tone'});
  assert.equal(f.node('console-update-notice').hidden,true,'isolated local tests do not claim to need the formal music service');
  assert.match(f.node('console-voice').textContent,/非角色语音/);
  assert.match(f.node('test-mode-note').textContent,/正式服务/);
  assert.equal(f.node('console-local-banner').hidden,false);
  assert.equal(f.liveLink.attributes['aria-disabled'],'true');
  let prevented=false;f.liveLink.listeners.click({preventDefault(){prevented=true;}});assert(prevented);
  await f.run();assert.equal(f.sent.length,1);assert.equal(f.drafts.length,0);
  assert.equal(f.node('test-run').disabled,true);
  f.event(3,{source:'danmaku'});f.ui.message({type:'reply_error',id:3});
  assert.equal(f.node('test-run').disabled,true,'other sources cannot claim a pending scenario');
  f.event(4);f.ui.message({type:'reply',id:4});
  assert.match(f.node('test-result').textContent,/等待短提示音/);
  const audio={type:'audio',id:40,turn_id:4};f.ui.message(audio);
  assert.equal(f.node('test-run').disabled,true,'dispatch is not playback completion');
  f.ui.playing(audio);assert.match(f.node('test-result').textContent,/等待完整播放结束/);
  f.ui.finished({id:41,turn_id:5},'ended');assert.equal(f.node('test-run').disabled,true);
  f.ui.finished(audio,'ended');assert.match(f.node('test-result').textContent,/已回报结束/);
  assert.equal(f.node('test-run').disabled,false);
  await f.run();f.event(5);f.ui.message({type:'audio',id:50,turn_id:5});
  f.ui.finished({id:50,turn_id:5},'ended');
  assert.match(f.node('test-result').textContent,/未收到开始回执/,'ended without a playing callback is not success');
  console.log('PASS: isolated scripted runs require matching event plus actual start/end playback receipts');

  f=fixture();f.hello({local_test:true});f.select('reply_error');
  const fast=f.run();
  f.ui.message({type:'event',id:11,source:'system',local_test:true});
  f.ui.message({type:'reply_error',id:11});
  f.ui.message({type:'event',id:12,source:'companion',local_test:true});
  f.requests[0].resolve(accepted(11));await fast;
  assert.match(f.node('test-result').textContent,/回复失败反馈/);
  assert.equal(f.node('test-run').disabled,false,'fast failure before POST response is retained');
  f.select('slow');const fastAudio=f.run();
  f.ui.message({type:'audio',id:130,turn_id:13});
  f.ui.playing({id:130,turn_id:13});f.ui.finished({id:130,turn_id:13},'ended');
  f.requests[1].resolve(accepted(13));await fastAudio;
  assert.match(f.node('test-result').textContent,/已回报结束/,'actual receipts before POST acknowledgement remain valid');
  console.log('PASS: HTTP/WebSocket race retains fast failures and actual audio receipts without binding unrelated events');

  f=fixture();f.hello({local_test:true});f.select('slow');
  const old=f.run();f.select('interrupt');await f.run();f.select('slow');const current=f.run();
  f.requests[0].resolve(accepted(20));await old;
  f.ui.message({type:'reply_error',id:20});
  assert.equal(f.node('test-run').disabled,true,'late old same-scenario response cannot bind the new run');
  f.ui.message({type:'tts_error',turn_id:21});f.requests[1].resolve(accepted(21));await current;
  assert.match(f.node('test-result').textContent,/语音失败反馈/);
  const failingOld=f.run();f.select('interrupt');await f.run();f.select('slow');const next=f.run();
  f.requests[2].reject(new Error('old request failed'));await failingOld;
  assert.equal(f.node('test-run').disabled,true,'late old POST failure cannot clear a new run');
  assert.doesNotMatch(f.node('test-result').textContent,/old request failed/);
  f.ui.disconnected();const afterDisconnect=f.node('test-result').textContent;
  f.requests[3].resolve(accepted(22));await next;
  assert.equal(f.node('test-result').textContent,afterDisconnect);
  assert.match(afterDisconnect,/不会自动重发/);
  const count=f.sent.length;f.hello({local_test:true});assert.equal(f.sent.length,count);
  f.ui.disconnected();f.node('console-reconnect').onclick();assert.equal(f.reconnects(),1);
  assert.equal(f.node('console-reconnect').hidden,false);
  assert.equal(f.node('console-enable-audio').hidden,true);
  assert.equal(f.sent.length,count);
  console.log('PASS: cancelled or disconnected runs ignore stale HTTP completions and never resend automatically');

  f=fixture();f.hello({local_test:true});f.select('slow');const failed=f.run();
  f.requests[0].resolve({ok:false,status:503});await failed;
  assert.match(f.node('test-result').textContent,/未确认/);
  assert.equal(f.node('test-run').disabled,false);
  const invalid=f.run();f.requests[1].resolve({ok:true,json:async()=>({accepted:true})});await invalid;
  assert.match(f.node('test-result').textContent,/缺少事件编号/);
  f.select('chat');f.sendOK(false);await f.run();assert.match(f.node('test-result').textContent,/测试未发送/);
  f.sendOK(true);await f.run();const timeout=[...f.timers.values()][0];timeout();
  assert.match(f.node('test-result').textContent,/尚未确认完成/);
  assert.equal(f.sent.length,2,'timeouts do not submit again');
  console.log('PASS: rejected, malformed, disconnected and timed-out requests expose recoverable feedback');

  f=fixture();f.hello({local_test:true,audio_owner:false,audio_target:'obs'});
  assert.equal(f.node('console-enable-audio').disabled,true);
  assert.equal(f.node('console-player').textContent,'OBS 直播页播放');
  f.ui.message({type:'audio_owner',audio_owner:true,audio_target:'control'});
  assert.equal(f.node('console-enable-audio').disabled,false);
  f.audioError(true);await f.node('console-enable-audio').onclick();
  assert.match(f.node('console-notice').textContent,/尚未允许播放/);
  f.audioError(false);await f.node('console-enable-audio').onclick();
  assert.equal(f.audioCalls(),2);
  f.audioHook(()=>f.ui.audioBlocked());await f.node('console-enable-audio').onclick();
  assert.match(f.node('console-notice').textContent,/浏览器拦截/,'autoplay refusal during unlock is not overwritten by apparent callback success');
  assert.match(f.node('console-enable-audio').textContent,/恢复本页声音/);
  f.ui.audioBlocked();assert.match(f.node('console-enable-audio').textContent,/恢复本页声音/);
  await f.run();f.ui.message({type:'audio_owner',audio_owner:false,audio_target:'display'});
  assert.equal(f.node('console-enable-audio').disabled,true);
  assert.match(f.node('test-result').textContent,/本页不会代报完成/);
  assert.equal(f.node('console-enable-audio').textContent,'启用本页声音');
  console.log('PASS: audio ownership and autoplay permission changes are explicit without claiming remote playback success');
})().catch(error=>{console.error(error);process.exitCode=1;});
