// Execute the complete page script with small DOM/audio test doubles.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname,'../brain/static/index.html'),'utf8');
const markup = html.replace(/<script\b[^>]*>[\s\S]*?<\/script>/g, '');
const markupIds = [...markup.matchAll(/\bid="([^"]+)"/g)].map(match => match[1]);
const actualIds = new Set(markupIds);
assert.equal(actualIds.size, markupIds.length, 'each control ID must appear once after regrouping the console');
for (const match of markup.matchAll(/<a\b[^>]*data-console-target="([^"]+)"[^>]*href="#([^"]+)"/g)) {
  assert.equal(match[1], match[2], 'console navigation must open the section it links to');
  assert(actualIds.has(match[1]), `console target ${match[1]} exists in the rendered markup`);
}
const elements = new Map(), created = [], sent = [];
class Element {
  constructor() { this.style={};this.value='';this.textContent='';this.innerHTML='';this.children=[];this.listeners={};
    this.classList={add(){},remove(){},toggle(){},contains(){return false;}}; }
  addEventListener(name,fn) {const previous=this.listeners[name];this.listeners[name]=(...args)=>{previous?.(...args);fn(...args);};}
  appendChild(child) {this.children.push(child);}
  setAttribute() {}
  removeAttribute() {}
  focus() {}
  scrollIntoView() {}
}
function element(id) {if(!elements.has(id)) elements.set(id,new Element());return elements.get(id);}
let audio, socket;
class Audio extends Element {
  constructor() {super();audio=this;this.currentTime=0;this.duration=5;this.playCount=0;this.paused=true;}
  play() {this.paused=false;this.playCount++;this.listeners.playing?.();return Promise.resolve();}
  pause() {this.paused=true;}
}
class WebSocket {
  static OPEN=1;
  constructor() {socket=this;this.readyState=1;}
  send(raw) {sent.push(JSON.parse(raw));}
}
const context=vm.createContext({
  ChatInteraction:require('../brain/static/interaction.js'),
  CompanionPresence:require('../brain/static/presence.js'),
  CompanionMotion:require('../brain/static/motion.js'),
  CompanionFace:require('../brain/static/face.js'),
  CompanionGaze:require('../brain/static/gaze.js'),
  CompanionGestures:require('../brain/static/gestures.js'),
  CompanionSpeechPerformance:require('../brain/static/speech-performance.js'),
  CompanionSpeechCues:require('../brain/static/speech-cues.js'),
  document:{documentElement:new Element(),body:new Element(),getElementById:id=>actualIds.has(id)?element(id):null,createElement:()=>{const el=new Element();created.push(el);return el;},
    querySelectorAll:selector=>selector==='.edit-transcript'?created.filter(el=>el.className==='edit-transcript'):[]},
  Audio,WebSocket,URLSearchParams,AbortController,window:{},location:{host:'localhost',protocol:'http:',search:''},localStorage:{getItem:()=>null,setItem(){}},
  SongDeck:class {stop(){} command(){return false;}},fetch:async()=>({ok:false}),
  performance:{now:()=>1000},setInterval(){},setTimeout(){},clearTimeout(){},console,
});
element('autoplay').checked=true;
element('test-scenario').value='chat';
vm.runInContext(fs.readFileSync(path.join(__dirname,'../brain/static/console.js'),'utf8'),context);
context.window.document=context.document;context.window.fetch=context.fetch;
vm.runInContext(fs.readFileSync(path.join(__dirname,'../brain/static/companion-chat.js'),'utf8'),context);
const source=[...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)].map(m=>m[1]).join('\n');
vm.runInContext(source,context);
function receive(message) {socket.onmessage({data:JSON.stringify(message)});}
receive({type:'hello',tts:'indextts2',playback:{state:'idle',paused:false}});
assert.equal(element('chat-image').hidden,false,'local hello enables the separately loaded image controls');
receive({type:'companion_meme',turn_id:'meme-startup',id:'meme-hello',url:'/api/companion/memes/meme-hello',caption:'<b>收到</b>'});
assert.equal(element('events').children.at(-1).className,'row reply companion-meme');
assert.equal(element('events').children.at(-1).children[1].textContent,'<b>收到</b>');
element('text').value='你好';element('send').onclick();
assert.equal(sent.find(m=>m.type==='inject').source,'companion');
receive({type:'event',id:1,source:'voice',kind:'chat',user:'你',text:'明天上午跑步'});
receive({type:'thinking',id:1});
assert.equal(element('interaction-status').textContent,'正在想');
receive({type:'speech_stage',turn_id:1,stage:'synthesizing'});
assert.equal(element('interaction-status').textContent,'正在准备语音');
receive({type:'broadcast_notice',turn_id:1,text:'声音还在准备 <script>literal</script>',duration_sec:12});
assert.equal(element('broadcast-notice').hidden,false);
assert.equal(element('broadcast-notice').textContent,'声音还在准备 <script>literal</script>');
receive({type:'audio',id:10,turn_id:1,segment_index:0,url:'/audio/a.wav',text:'回答',emotion:'平静'});
assert.equal(element('broadcast-notice').hidden,true,'ready audio clears an obsolete waiting notice');
receive({type:'playback',state:'playing',current_id:10,paused:false,queued:0});
audio.currentTime=1.25;
element('pause-audio').onclick();
assert.equal(sent.at(-1).type,'playback_pause');
receive({type:'audio_control',action:'pause',id:10});
receive({type:'playback',state:'paused',current_id:10,paused:true,queued:0});
assert.equal(audio.paused,true);
assert.equal(audio.currentTime,1.25);
element('pause-audio').onclick();
assert.equal(sent.at(-1).paused,false);
receive({type:'audio_control',action:'resume',id:10});
receive({type:'playback',state:'playing',current_id:10,paused:false,queued:0});
assert.equal(audio.currentTime,1.25);
assert.equal(audio.playCount,2);
assert.equal(sent.filter(m=>m.type==='audio_started').length,1,'resume must not duplicate delivery receipts');
created.find(el=>el.className==='edit-transcript').onclick();
assert.equal(element('text').value,'明天上午跑步');
element('text').value='明天下午跑步';element('confirm-voice').onclick();
const correction=sent.filter(m=>m.type==='inject').at(-1);
assert.equal(correction.corrects,1);
assert.equal(correction.text,'明天下午跑步');
console.log('PASS: complete page boot, stage display, pause/resume position and submitted-transcript correction');
receive({type:'event',id:2,source:'voice',kind:'chat',user:'你',text:'明天下午跑步',corrects:1});
created.filter(el=>el.textContent==='撤销这条语音').at(-1).onclick();
assert.equal(sent.at(-1).retract,true);
assert.equal(sent.at(-1).corrects,2);
receive({type:'voice_retracted',id:2});
assert.equal(element('interaction-status').textContent,'可以和我说话');
console.log('PASS: voice undo request and acknowledgement');
element('refresh-memories').onclick();
assert.equal(sent.at(-1).type,'memory_manage');
receive({type:'memory_list',ok:true,memories:[{id:7,summary:'喜欢咖啡',subject:'观众你',
  status:'active',scope:'temporary',source:{user:'你',event_ts:1800000000},expires_at:1800086400}]});
const memoryRow=element('memory-list').children.at(-1);
assert.match(memoryRow.children[0].textContent,/临时状态/);
assert.match(memoryRow.children[0].textContent,/最后确认：未记录/,'legacy timestamps must not be presented as confirmed facts');
assert.match(memoryRow.children[0].textContent,/召回 0 次/);
memoryRow.children[1].value='今天喜欢茶';
memoryRow.children.find(el=>el.textContent==='保存修改').onclick();
assert.equal(sent.at(-1).text,'今天喜欢茶');
memoryRow.children.find(el=>el.textContent==='忘记').onclick();
assert.equal(sent.at(-1).action,'forget');
receive({type:'event',id:3,source:'voice',kind:'chat',user:'你',text:'我喜欢茶'});
created.filter(el=>el.textContent==='确认保存记忆').at(-1).onclick();
assert.equal(sent.at(-1).confirm_memory,true);
assert.equal(sent.at(-1).corrects,3);
console.log('PASS: memory list, editing, forgetting and explicit voice confirmation');
receive({type:'reply',id:3,text:'我记住了。',emotion:'平静',mode:'llm',actions:[]});
assert(element('events').children.some(row=>row.className==='row reply'), 'assistant replies use the same timeline');
assert(created.some(el=>el.className==='message-actions'), 'voice controls are grouped in expandable details');
element('focus-mode').onclick();
assert.equal(element('events').hidden,true);
assert.equal(element('focus-mode').textContent,'返回完整界面');
element('focus-mode').onclick();
assert.equal(element('events').hidden,false);
receive({type:'memory_list',ok:true,memories:[
  {id:10,summary:'今天喝茶',subject:'你',status:'active',scope:'temporary'},
  {id:11,summary:'旧咖啡偏好',subject:'你',status:'forgotten'},
  {id:12,summary:'喜欢苹果',subject:'你',status:'active',scope:'lasting'}]});
assert(element('memory-feedback').textContent.includes('2 / 3'));
element('memory-filter').value='history';element('memory-filter').onchange();
assert(element('memory-feedback').textContent.includes('1 / 3'));
element('memory-search').value='苹果';element('memory-search').oninput();
assert(element('memory-feedback').textContent.includes('0 / 3'));
element('memory-filter').value='active';element('memory-filter').onchange();
assert(element('memory-feedback').textContent.includes('1 / 3'));
element('memory-search').value='';
receive({type:'memory_list',ok:true,memories:[
  {id:20,summary:'准备周末去爬山',subject:'你',status:'active',scope:'lasting',retention_type:'plan',
    last_confirmed_at:1800000000,updated_at:1800086400,access_count:3,visibility:'private'},
  {id:21,summary:'今天想喝茶',subject:'你',status:'active',scope:'lasting',retention_type:'temporary_state',
    last_confirmed_at:null,access_count:0}]});
const planRow=element('memory-list').children.at(-2);
assert.match(planRow.children[0].textContent,/近期计划.*私有记忆/);
assert(planRow.children[0].textContent.includes(`最后确认：${new Date(1800000000*1000).toLocaleString()}`));
assert.match(planRow.children[0].textContent,/召回 3 次/);
assert.equal(planRow.children[1].value,'准备周末去爬山');
element('memory-filter').value='temporary';element('memory-filter').onchange();
assert(element('memory-feedback').textContent.includes('1 / 2'),'temporary filtering accepts the new lifecycle category');
assert.equal(element('memory-list').children.at(-1).children[1].value,'今天想喝茶');
vm.runInContext("voiceActive=true;voiceRecognition={abort(){this.aborted=true}};",context);
element('continuous-voice').checked=true;
vm.runInContext('renderInteraction()',context);
assert(element('mic-state').textContent.includes('正在收音'));
element('stop-mic').onclick();
assert.equal(element('continuous-voice').checked,false);
assert.equal(vm.runInContext('voiceRecognition.aborted',context),true);
vm.runInContext('voiceActive=false;voiceStarting=false;voiceDraft=true;renderInteraction()',context);
assert(element('mic-state').textContent.includes('等待确认'));
console.log('PASS: unified timeline, focus mode, memory filters and microphone stop state');
const pupils={};
context.pupilCore={_parameterIds:['Param','Param2'],getParameterIndex:id=>id,getParameterMinimumValue:()=>-10,
  getParameterMaximumValue:()=>10,getParameterDefaultValue:()=>0,
  getParameterValueById:()=>0,setParameterValueById:(id,value)=>pupils[id]=value};
vm.runInContext('applyPupilGaze(pupilCore,{x:.6,y:-.3},1)',context);
assert(pupils.Param>2 && pupils.Param2< -1,'listening retains visible native pupil displacement');
assert(pupils.ParamEyeBallX>0 && pupils.ParamEyeBallY<0);
vm.runInContext('applyPupilGaze(pupilCore,{x:5,y:-5},0)',context);
assert(pupils.Param<=10 && pupils.Param2>=-10,'native pupil output respects model range');
console.log('PASS: native pupil displacement, attention weighting and parameter bounds');

// Received text, display names and metadata must stay inert in both text and
// HTML attribute contexts, including prototype-looking event IDs.
receive({type:'event',id:'__proto__',source:'<img src=x onerror=1>',
  user:'<svg onload=1>',kind:'chat" onclick="bad',text:'<b>literal</b>'});
const unsafeEvent=element('events').children.at(-1).innerHTML;
assert(unsafeEvent.includes('class="tag ambient"'));
assert(unsafeEvent.includes('&lt;svg onload=1&gt;'));
assert(unsafeEvent.includes('chat&quot; onclick=&quot;bad'));
assert(!unsafeEvent.includes('<svg'));
receive({type:'reply',id:'<img src=x>',emotion:'<svg onload=1>',text:'<b>reply</b>',
  mode:'fallback',error:'" onmouseover="bad',actions:[]});
const unsafeReply=element('events').children.at(-1).innerHTML;
assert(unsafeReply.includes('&lt;svg onload=1&gt;'));
assert(unsafeReply.includes('title="&quot; onmouseover=&quot;bad"'));
assert(unsafeReply.includes('#&lt;img src=x&gt;'));
receive({type:'reply',id:4,emotion:'平静',text:'离线场景',mode:'local_test',actions:[]});
assert(element('events').children.at(-1).innerHTML.includes('离线脚本'));
assert(!element('events').children.at(-1).innerHTML.includes('后备回复'));
console.log('PASS: safe chat metadata, attribute quoting and offline script labels');

const timeline=element('events');
timeline.scrollHeight=1200;timeline.clientHeight=400;timeline.scrollTop=100;
receive({type:'event',id:5,source:'danmaku',kind:'chat',user:'观众',text:'新的弹幕'});
assert.equal(timeline.scrollTop,100,'incoming chat must not interrupt reading history');
assert.equal(element('chat-latest').hidden,false);
assert(element('chat-latest').textContent.includes('1 条新消息'));
receive({type:'reply_partial',id:5,text:'正在回复',emotion:'平静'});
receive({type:'reply_partial',id:5,text:'正在回复新的弹幕',emotion:'平静'});
receive({type:'reply',id:5,text:'回复完成',emotion:'平静',mode:'llm',actions:[]});
assert.equal(timeline.scrollTop,100);
assert(element('chat-latest').textContent.includes('2 条新消息'),'stream updates count as one reply');
element('chat-latest').onclick();
assert.equal(timeline.scrollTop,1200);
assert.equal(element('chat-latest').hidden,true);
timeline.scrollTop=780;
receive({type:'event',id:6,source:'danmaku',kind:'chat',user:'观众',text:'跟随新弹幕'});
assert.equal(timeline.scrollTop,1200,'near-bottom readers continue following the timeline');
console.log('PASS: history reading position, unread replies and return-to-latest');

vm.runInContext('voiceDraft=false;voiceActive=false;voiceStarting=false;correctingId=null;',context);
element('text').value='中文输入确认';
const beforeIME=sent.filter(m=>m.type==='inject').length;
element('text').onkeydown({key:'Enter',isComposing:true,preventDefault(){throw new Error('IME was intercepted');}});
element('text').onkeydown({key:'Enter',keyCode:229,preventDefault(){throw new Error('IME was intercepted');}});
element('text').onkeydown({key:'Enter',shiftKey:true,preventDefault(){throw new Error('newline was intercepted');}});
assert.equal(sent.filter(m=>m.type==='inject').length,beforeIME);
let prevented=false;
element('text').onkeydown({key:'Enter',preventDefault(){prevented=true;}});
assert.equal(prevented,true);
assert.equal(sent.filter(m=>m.type==='inject').length,beforeIME+1);
socket.readyState=3;
element('text').value='断线时保留这句话';
element('send').onclick();
assert.equal(element('text').value,'断线时保留这句话');
assert(element('interaction-status').textContent.includes('消息已保留'));
element('advanced-text').value='断线测试弹幕';
element('advanced-send').onclick();
assert.equal(element('advanced-text').value,'断线测试弹幕');
assert(element('interaction-status').textContent.includes('测试消息已保留'));
socket.readyState=1;
const normalSend=socket.send;
socket.send=()=>{throw new Error('closed while sending');};
element('send').onclick();
assert.equal(element('text').value,'断线时保留这句话');
assert(element('interaction-status').textContent.includes('消息已保留'));
socket.send=normalSend;
console.log('PASS: IME Enter, deliberate send and drafts retained on connection failures');

(async()=>{
  receive({type:'audio',id:30,turn_id:8,segment_index:0,url:'/audio/b.wav',text:'旧回答',emotion:'平静'});
  receive({type:'thinking',id:8});
  receive({type:'speech_stage',turn_id:8,stage:'synthesizing'});
  const receiptsBeforeClose=sent.filter(m=>m.type==='audio_finished').length;
  socket.readyState=3;
  await socket.onclose();
  assert.equal(audio.paused,true,'disconnection must stop local orphan audio');
  assert.equal(vm.runInContext('currentAudioItem',context),null);
  assert.equal(vm.runInContext('generationActive',context),false);
  assert.equal(vm.runInContext('synthesisTurns.size',context),0);
  assert.equal(sent.filter(m=>m.type==='audio_finished').length,receiptsBeforeClose,
    'disconnect cleanup must not fabricate delivery receipts');
  vm.runInContext('connectSocket()',context);
  receive({type:'hello',tts:'indextts2',playback:{state:'playing',current_id:31,current_turn_id:9,queued:0},
    conversation_state:{generating:true,generating_turn_ids:[10],synthesizing_turn_ids:[9]}});
  assert.equal(element('interaction-status').textContent,'正在说','playing state takes precedence over generation');
  assert.equal(vm.runInContext('generationActive',context),true);
  assert.equal(vm.runInContext('synthesisTurns.has(9)',context),true);
  assert.equal(vm.runInContext('interaction.turn',context),10);
  receive({type:'hello',tts:'indextts2',playback:{state:'idle',current_id:null,queued:0},
    conversation_state:{generating:true,generating_turn_ids:[2],synthesizing_turn_ids:[]}});
  assert.equal(element('interaction-status').textContent,'正在想');
  assert.equal(vm.runInContext('interaction.turn',context),2,'server restart may reset turn identifiers');
  receive({type:'hello',tts:'indextts2',playback:{state:'idle',current_id:null,queued:0},conversation_state:{}});
  assert.equal(element('interaction-status').textContent,'可以和我说话');
  receive({type:'thinking',id:1});
  assert.equal(element('interaction-status').textContent,'正在想','new low turn IDs remain usable after restart');
  console.log('PASS: disconnect cleanup and generation/playback recovery without stale turn IDs');

  // A manual reconnect can beat the previous close handler's health request.
  let finishOldProbe;
  context.fetch=()=>new Promise(resolve=>{finishOldProbe=resolve;});
  const oldSocket=socket;
  const lateClose=oldSocket.onclose();
  vm.runInContext('connectSocket()',context);
  receive({type:'hello',tts:'indextts2',audio_owner:true,playback:{state:'idle',queued:0}});
  const connectedStatus=element('status').textContent;
  oldSocket.onmessage({data:JSON.stringify({type:'thinking',id:999})});
  assert.equal(vm.runInContext('generationActive',context),false);
  finishOldProbe({status:403});
  await lateClose;
  assert.equal(element('status').textContent,connectedStatus,'stale close must not revoke a newer connection');
  console.log('PASS: old WebSocket events and late reconnect probes cannot replace current state');
})().catch(error=>{console.error(error);process.exitCode=1;});

