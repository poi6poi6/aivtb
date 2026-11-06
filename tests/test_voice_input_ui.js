const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const Presence = require('../brain/static/presence.js');
const Face = require('../brain/static/face.js');
const ChatInteraction = require('../brain/static/interaction.js');
const html = fs.readFileSync(path.join(__dirname, '../brain/static/index.html'), 'utf8');
const start = html.indexOf('function suspendAutoListening()');
const end = html.indexOf('// 后端拥有唯一语音队列', start);
let recognition, timerId = 0;
const timers = new Map(), messages = [], elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {value: id === 'companion-user' ? '你' : '', checked:false,
    listeners:{}, addEventListener(name,fn) {this.listeners[name]=fn;},
    classList:{contains:()=>false,add(){},remove(){}}});
  return elements.get(id);
}
class Recognition {
  constructor() { recognition = this; }
  stop() {}
  abort() {}
  start() { this.onstart(); }
}
const interaction = new ChatInteraction(() => 1000);
const context = vm.createContext({
  ChatInteraction,
  window:{SpeechRecognition:Recognition}, document:{getElementById:element},
  voiceInputEl:element('voice-input'), continuousEl:element('continuous-voice'),
  textInputEl:element('text'), reviewEl:element('voice-review'), reviewHintEl:element('voice-review-hint'),
  WebSocket:{OPEN:1}, ws:{readyState:1,send:raw=>messages.push(JSON.parse(raw))},
  presence:new Presence(), faceMotion:new Face(), speechCues:new (require('../brain/static/speech-cues.js'))(), performance:{now:()=>1000}, interaction,
  setInteraction:(phase,turn,error)=>interaction.set(phase,turn,error),
  reportPlaybackProgress:()=>messages.push({type:'progress'}),
  voiceRecognition:null,voiceActive:false,voiceStarting:false,voiceRestartTimer:null,
  voiceDraft:false,correctingId:null,lastSubmitted:null,autoSendTimer:null,pausedForMic:false,
  generationActive:false,synthesisTurns:new Set(),renderInteraction:()=>{},currentAudioItem:null,playbackState:{paused:false,queued:0},audioPlaying:false,
  audioPlayer:{pause(){}},
  setTimeout:(fn,ms)=>{timers.set(++timerId,{fn,ms});return timerId;},
  clearTimeout:id=>timers.delete(id),
});
vm.runInContext(html.slice(html.indexOf('function stopAllMedia('),html.indexOf('function prepareMediaInteraction(')),context);
vm.runInContext(html.slice(html.indexOf('function sendCompanion('),html.indexOf('function injectAdvanced(')),context);
vm.runInContext(html.slice(start,end),context);
function result(text,final) {
  const item=[{transcript:text}]; item.isFinal=final;
  recognition.onresult({results:[item]});
}
function runTimers(ms) {
  for (const [id,timer] of [...timers]) if(timer.ms===ms) {timers.delete(id);timer.fn();}
}
recognition.onstart();
assert.equal(messages.length,0);
result('嗯嗯',false);
assert.equal(messages.length,0,'acknowledgement must not interrupt');
result('嗯嗯',true);
assert.equal(context.voiceDraft,true);
assert.equal(messages.length,0,'click-to-talk requires transcript confirmation');
element('confirm-voice').onclick();
assert.equal(messages.filter(m=>m.type==='inject')[0].source,'voice');
recognition.onend();
messages.length=0;
recognition.onstart();
result('等一下',false);
result('等一下，我说的是明天',false);
assert.equal(messages.filter(m=>m.type==='conversation_interrupt').length,1);
result('等一下，我说的是明天',true);
element('text').value='等一下，我说的是后天';
element('confirm-voice').onclick();
assert.equal(messages.find(m=>m.type==='inject').text,'等一下，我说的是后天');
recognition.onend();
messages.length=0;
context.continuousEl.checked=true;
recognition.onstart();
result('明天下午去跑步',true);
element('text').value='后天下午去跑步';
element('text').listeners.input();
runTimers(2000);
assert(!messages.some(m=>m.type==='inject'),'editing cancels auto-send');
context.cancelVoiceDraft();
recognition.onend();
recognition.onstart();
result('现在听清楚了',true);
runTimers(2000);
assert.equal(messages.filter(m=>m.type==='inject').length,1);
recognition.onend();
recognition.onerror({error:'not-allowed'});
assert.equal(context.continuousEl.checked,false);
assert.equal(interaction.phase,'error');
recognition.onend();
assert.equal([...timers.values()].filter(t=>t.ms===500).length,0,'permission denial must not restart microphone');
context.voiceDraft=true;context.correctingId=9;context.textInputEl.value='修正后的内容';
context.sendCompanion();
assert.equal(messages.filter(m=>m.type==='inject').at(-1).corrects,9);
console.log('PASS: transcript review, edit, correction, auto-send cancellation, continuous mode and permission failures');

context.continuousEl.checked=true;context.voiceDraft=false;context.textInputEl.value='';context.voiceActive=false;context.voiceStarting=false;
interaction.set('idle');context.generationActive=true;context.scheduleAutoListening();
assert.equal([...timers.values()].filter(t=>t.ms===500).length,0);
context.generationActive=false;context.synthesisTurns.add(20);context.scheduleAutoListening();
assert.equal([...timers.values()].filter(t=>t.ms===500).length,0,'must wait for remaining synthesis');
context.synthesisTurns.clear();context.scheduleAutoListening();
assert.equal([...timers.values()].filter(t=>t.ms===500).length,1);
console.log('PASS: continuous recording waits for generation and synthesis drain');
messages.length=0;
recognition.onstart();
result('你准备好了吗？',true);
recognition.onspeechstart();
runTimers(1100);
assert(!messages.some(m=>m.type==='inject'),'speech onset cancels the previous endpoint timer');
result('你准备好了吗？还有',true);
assert([...timers.values()].some(t=>t.ms===4500));
element('text').listeners.input();
const edited=element('text').value='手动纠正';
result('迟到的识别结果',true);
assert.equal(element('text').value,edited,'late ASR must not overwrite manual edits');
console.log('PASS: adaptive endpoint, resumed speech and manual edit race');
messages.length=0;
recognition.onstart();
result('等等，我不是这个意思',false);
assert.equal(messages.filter(m=>m.type==='conversation_interrupt').length,1);
result('等等，我不是这个意思',true);
assert.equal(messages.filter(m=>m.type==='conversation_interrupt').length,1);
context.cancelVoiceDraft();
recognition.onend();
context.continuousEl.checked=true;
recognition.onstart();
element('voice-names').value='测试助手';
result('测试助首你在吗',true);
assert(element('voice-review-hint').textContent.includes('测试助手'));
runTimers(1100);runTimers(2000);
assert(!messages.some(m=>m.type==='inject'),'uncertain character name requires review');
console.log('PASS: explicit interruption is deduplicated and likely name errors require review');

context.cancelVoiceDraft();
recognition.onend();
messages.length=0;
context.continuousEl.checked=true;
recognition.onstart();
const uncertainTime = [{transcript:'我明天十点出门，',confidence:0.4}];
uncertainTime.isFinal=true;
const confidentTail = [{transcript:'请帮我记住。',confidence:0.95}];
confidentTail.isFinal=true;
recognition.onresult({results:[uncertainTime]});
assert(element('voice-review-hint').textContent.includes('把握较低'));
recognition.onresult({resultIndex:1,results:[uncertainTime,confidentTail]});
assert(element('voice-review-hint').textContent.includes('把握较低'),'earlier uncertainty remains visible');
runTimers(2000);
assert(!messages.some(m=>m.type==='inject'),'a confident tail must not send an uncertain earlier segment');
element('confirm-voice').onclick();
assert.equal(messages.filter(m=>m.type==='inject').length,1);
assert.equal(messages.find(m=>m.type==='inject').text,'我明天十点出门，请帮我记住。');
recognition.onend();
messages.length=0;
recognition.onstart();
const ambiguousNumber = [{transcript:'买三个苹果，',confidence:0.95},{transcript:'买四个苹果，',confidence:0.9}];
ambiguousNumber.isFinal=true;
recognition.onresult({resultIndex:1,results:[ambiguousNumber,confidentTail]});
assert(element('voice-review-hint').textContent.includes('买四个苹果'));
runTimers(2000);
assert(!messages.some(m=>m.type==='inject'),'earlier numeric ambiguity requires review despite confident tail');
element('text').value='买五个苹果，请帮我记住。';
element('text').listeners.input();
recognition.onresult({results:[confidentTail]});
assert.equal(element('text').value,'买五个苹果，请帮我记住。');
element('confirm-voice').onclick();
assert.equal(messages.find(m=>m.type==='inject').text,'买五个苹果，请帮我记住。');
recognition.onend();
messages.length=0;
recognition.onstart();
const clearOpening = [{transcript:'今天去散步，',confidence:0.95}];
clearOpening.isFinal=true;
recognition.onresult({results:[clearOpening,confidentTail]});
runTimers(2000);
assert.equal(messages.filter(m=>m.type==='inject').length,1,'a reviewed previous draft does not block a fresh clear utterance');
console.log('PASS: every accumulated ASR segment retains confidence and ambiguity review; manual confirmation/edit and new utterances work');
