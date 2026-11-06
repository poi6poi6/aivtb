const assert = require('node:assert/strict');
const Presence = require('../brain/static/presence.js');
const Interaction = require('../brain/static/interaction.js');
const p = new Presence();
assert.deepEqual(p.frame(0, false), { attention: 0, nod: 0, tilt: 0, readiness:0, curiosity:0 });
p.listen(true);
const hear = (target, text, now, final=true, alternatives=[]) => target.hear(text, now,
  {final, decision:Interaction.voiceDecision(text, alternatives)});
hear(p, '今天发生了一件让我很开心的事', 100, false);
let state;
for (let time = 100; time <= 450; time += 16) state = p.frame(time, false);
assert(state.attention > 0.7 && state.attention < 1);
assert.equal(state.nod,0,'interim character growth must never be interpreted as agreement');
assert.equal(state.readiness,0,'interim recognition does not announce readiness');
hear(p, '今天发生了一件让我很开心的事。', 500);
state=p.frame(750,false);
assert(state.nod>0 && state.nod<=1.2,'a stable final statement gets one small acknowledgment');
const firstNod = p.nodAt;
hear(p, '今天发生了一件让我很开心的事。', 3000);
assert.equal(p.nodAt, firstNod, 'duplicate final events must not nod again after cooldown');
hear(p, '今天发生了一件让我很开心的事', 3100);
assert.equal(p.nodAt,firstNod,'punctuation corrections do not restart a nod');
hear(p, '今天发生了一件让我很开心的事，我终于做完了任务', 3500, false);
assert.equal(p.nodAt, firstNod, 'continuous partial transcripts must not cause repeated nods');
hear(p, '今天发生了一件让我很难过的事。', 4000);
assert.equal(p.nodAt,firstNod,'replacing a finalized transcript does not nod again');
hear(p, '今天发生了一件让我很难过的事。我已经处理好了。', 4300);
assert.equal(p.nodAt,4300,'a later finalized sentence may acknowledge after cooldown');
hear(p, '今天发生了一件让我很难过的事。我已经处理好了。现在我在休息。', 4400);
assert.equal(p.nodAt,4300,'separate completed sentences still respect the nod cooldown');
p.listen(false);
for (let time = 4500; time < 7600; time += 16) state = p.frame(time, false);
assert(state.attention < 0.001);
assert.equal(state.nod, 0);
assert(state.readiness<.001 && state.curiosity<.001);
p.thinking = true;
for (let time = 7600; time < 8100; time += 16) state = p.frame(time, false);
assert(state.tilt > 0 && state.tilt <= 0.9);
assert.equal(p.frame(8116, true).tilt, 0, 'thinking pose must not override speech');

for(const text of ['让我想想','我想想','还有','但是','我觉得']) {
  const waiting=new Presence();waiting.listen(true);waiting.frame(0,false);
  hear(waiting,text,100);
  for(let time=100;time<850;time+=16) {
    const values=waiting.frame(time,false);
    assert.equal(values.nod,0,`no nod for unfinished or hesitant statement: ${text}`);
    assert.equal(values.readiness,0);
    assert.equal(values.curiosity,0);
  }
  assert.equal(waiting.listeningCue,'wait');
}
const unsure=new Presence();unsure.listen(true);
hear(unsure,'明天去',100,true,[{transcript:'明天去',confidence:.4}]);
assert.equal(unsure.nodAt,-Infinity,'review-required final speech cannot be acknowledged as understood');
const q=new Presence();q.listen(true);q.frame(0,false);
hear(q,'你觉得这件事对吗？',100);
q.listen(false);
for(let time=100;time<=750;time+=16)state=q.frame(time,false);
assert.equal(state.nod,0,'a question gets curiosity, never an agreement nod');
assert(state.curiosity>.8 && state.curiosity<=1);
assert(state.readiness>.5 && state.readiness<=.65);
assert(state.attention<.001,'final reaction survives normal recognition stop without keeping microphone attention');
assert.deepEqual([q.frame(766,true).readiness,q.frame(782,true).curiosity],[0,0],
  'listening expressions immediately yield to speaking');
assert.equal(q.frame(798,false).readiness,0,'completed speech cannot restore an old final cue');
q.listen(true);
hear(q,'你觉得这件事对吗？',800);
for(let time=800;time<1100;time+=16)state=q.frame(time,false);
assert(state.curiosity>.5);
hear(q,'你觉得这件事对吗？因为我还想说',1100,false);
for(let time=1100;time<2200;time+=16)state=q.frame(time,false);
assert(state.curiosity<.02 && state.readiness<.01,'continued speech withdraws pending readiness');
q.listen(false);q.listen(true);
hear(q,'我说完了',2300);
for(let time=2300;time<3100;time+=16)state=q.frame(time,false);
assert(state.readiness>.95 && state.readiness<=1,'an explicit ending prepares to take the turn');
for(let time=3100;time<5800;time+=16)state=q.frame(time,false);
assert(state.readiness<.001 && state.curiosity<.001,'stale final cues decay without new events');
const resolved=new Presence();resolved.listen(true);
hear(resolved,'让我想想',100);
hear(resolved,'让我想想，现在可以回答了',500);
assert.equal(resolved.nodAt,500,'resolving an unfinished thought can earn its first acknowledgment');
resolved.resetSession();
assert.deepEqual(resolved.frame(600,false),{attention:0,nod:0,tilt:0,readiness:0,curiosity:0},
  'disconnect reset clears pending attention and reactions');
const correction=new Presence();correction.listen(true);correction.frame(0,false);
hear(correction,'你会这样做。',100);
hear(correction,'你会这样做吗？',200);
assert.equal(correction.frame(350,false).nod,0,'a question correction cancels a pending statement nod');
const receipt=new Presence();receipt.frame(0,false);receipt.acknowledge(100);
assert(receipt.frame(350,false).nod>0,'explicit backend backchannel still acknowledges');
assert.equal(receipt.frame(366,true).nod,0,'receipt nod does not interfere with speech');
const replayed=new Presence();replayed.listen(true);replayed.frame(0,false);
hear(replayed,'你觉得这件事对吗？',100);
for(let time=100;time<=4600;time+=20) {
  if(time>100 && (time-100)%300===0) {
    const punctuation=(time-100)%600===0 ? '？' : '?';
    hear(replayed,`你觉得这件事对吗${punctuation}`,time);
    assert.equal(replayed.cueAt,100,'duplicate or punctuation-only final cannot extend a reaction');
  }
  state=replayed.frame(time,false);
}
assert(state.curiosity<.001 && state.readiness<.001,
  'frequent repeated final questions cannot hold a raised eyebrow indefinitely');
const trusted=new Presence();trusted.listen(true);trusted.frame(0,false);
hear(trusted,'你同意吗？',100,true,[{transcript:'你同意吗？',confidence:.4}]);
assert.equal(trusted.listeningCue,'wait');
hear(trusted,'你同意吗？',900,true,[{transcript:'你同意吗？',confidence:.9}]);
assert.equal(trusted.cueAt,900,'same wording gaining reliable interpretation can trigger a first reaction');
assert.equal(trusted.listeningCue,'question');
for(let time=900;time<1500;time+=16)state=trusted.frame(time,false);
assert(state.curiosity>.8 && state.readiness>.5);
hear(trusted,'你同意吗？因为',1500,false);
hear(trusted,'你同意吗？',1600);
assert.equal(trusted.cueAt,-Infinity,'replaying an earlier final cannot restore a reaction withdrawn by new speech');
const continued=new Presence();continued.listen(true);continued.frame(0,false);
hear(continued,'这件事结束了。',100);
assert(continued.frame(200,false).nod>0);
hear(continued,'这件事结束了。不过还有',250,false);
assert.equal(continued.frame(350,false).nod,0,'new interim speech stops the remaining acknowledgment nod');
console.log('PASS: semantic final listening, review guards, correction deduplication, bounded readiness and curiosity');
