const assert = require('node:assert/strict');
const ChatInteraction = require('../brain/static/interaction.js');
let now = 0;
const state = new ChatInteraction(() => now);
state.set('thinking', 2);
now = 9000;
assert(state.label().includes('已等待 9 秒'));
state.set('idle', 1);
assert.equal(state.phase, 'thinking', 'old cancellation must not erase new turn state');
state.set('synthesizing', 2);
assert.equal(state.label(), '正在准备语音');
state.set('paused');
assert(state.label().includes('原位置'));
state.set('error', 2, '麦克风权限未开启');
assert.equal(state.label(), '麦克风权限未开启');
console.log('PASS: stage labels, elapsed wait, stale event guard and actionable errors');
const Decision = require('../brain/static/interaction.js');
assert.equal(Decision.voiceDecision('你准备好了吗？').delay, 1100);
assert.equal(Decision.voiceDecision('还有').delay, 4500);
assert.equal(Decision.voiceDecision('让我想想').delay, null);
assert.equal(Decision.voiceDecision('我想想').delay, null);
assert.equal(Decision.voiceDecision('买三个', [{transcript:'买三个',confidence:0.9}, {transcript:'买四个',confidence:0.8}]).delay, null);
assert.equal(Decision.voiceDecision('明天去', [{transcript:'明天去',confidence:0.4}]).delay, null);
assert.equal(Decision.voiceDecision('明天去', [{transcript:'明天去',confidence:0}]).delay, 2000);
assert.equal(Decision.voiceDecision('我觉得').delay,4500);
assert.equal(Decision.voiceDecision('就这些，我说完了').delay,650);
assert.equal(Decision.voiceDecision('明天去',[],{pace:'patient'}).delay,3000);
assert.equal(Decision.voiceDecision('明天去',[],{pace:'quick'}).delay,1500);
assert.equal(Decision.voiceDecision('还有',[],{pace:'quick'}).delay,4500);
assert.equal(Decision.isInterruption('嗯嗯，你继续'),false);
assert.equal(Decision.isInterruption('我不是这个意思，先等一下'),true);
assert.equal(Decision.isInterruption('等等，我说的是明天'),true);
assert.equal(Decision.voiceDecision('测试助首',[],{names:['测试助手']}).delay,null);
assert.equal(Decision.voiceDecision('测试助手',[],{names:['测试助手']}).delay,2000);
assert.equal(Decision.voiceResultsDecision('让我想想，现在说完了',[
  [{transcript:'让我想想，',confidence:.9}],[{transcript:'现在说完了',confidence:.9}]
]).delay,2000,'a completed later clause resolves an earlier thinking pause');
assert.equal(Decision.voiceResultsDecision('测试助首，你是测试助手吗？',[
  [{transcript:'测试助首，',confidence:.9}],[{transcript:'你是测试助手吗？',confidence:.9}]
],{names:['测试助手']}).delay,null,'a correct name later cannot hide an uncertain earlier name');
for(const [text,cue,delay] of [
  ['你准备好了吗？','question',1100],['还有','wait',4500],['让我想想','wait',null],
  ['我说完了','complete',650],['明天去','neutral',2000]
]) {
  const decision=Decision.voiceDecision(text);
  assert.equal(decision.listeningCue,cue);
  assert.equal(decision.delay,delay,'listening intent must not change the existing send policy');
}
for(const decision of [
  Decision.voiceDecision('明天去',[{transcript:'明天去',confidence:.4}]),
  Decision.voiceDecision('买三个',[{transcript:'买三个',confidence:.9},{transcript:'买四个',confidence:.8}]),
  Decision.voiceDecision('测试助首',[],{names:['测试助手']})
]) {
  assert.equal(decision.listeningCue,'wait','uncertain wording must not look like confirmed understanding');
  assert.equal(decision.reviewRequired,true);
  assert.equal(decision.delay,null);
}
assert.equal(Decision.voiceResultsDecision('测试助首，你是测试助手吗？',[
  [{transcript:'测试助首，',confidence:.9}],[{transcript:'你是测试助手吗？',confidence:.9}]
],{names:['测试助手']}).listeningCue,'wait','review guards outrank a question ending');
assert.equal(Decision.voiceResultsDecision('让我想想，现在可以回答了',[
  [{transcript:'让我想想，',confidence:.9}],[{transcript:'现在可以回答了',confidence:.9}]
]).listeningCue,'complete','the latest completed thought can resolve an earlier hesitation');
console.log('PASS: shared listening intent preserves voice timing and review priority');
