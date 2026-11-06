const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const SpeechCues=require('../brain/static/speech-cues.js');

function session(text,duration=8) {
  const p=new SpeechCues();
  assert.equal(p.begin({text,turnId:1}),true);
  let now=0,media=0,value=p.frame(0,{speaking:true,mediaTime:0,duration});
  const events=[];
  return {p,events,get now(){return now;},get media(){return media;},get value(){return value;},
    step(extra={},ms=10,advance=ms/1000) {
      now+=ms;media+=advance;
      value=p.frame(now,{speaking:true,mediaTime:media,duration,...extra});
      if(value.cue)events.push({...value.cue,at:media,focus:value.focus});
      return value;
    },
    run(seconds,extra={},fps=100) {
      const frames=[];
      for(let i=0;i<Math.round(seconds*fps);i++)frames.push(this.step(extra,1000/fps));
      return frames;
    },
    begin(args={}) {
      media=0;
      const accepted=p.begin({text,turnId:1,segmentIndex:1,...args});
      value=p.frame(now,{speaking:true,mediaTime:0,duration});
      return accepted;
    },
  };
}

const browser={window:{}};
vm.runInNewContext(fs.readFileSync(require.resolve('../brain/static/speech-cues.js'),'utf8'),browser);
assert.equal(typeof browser.window.CompanionSpeechCues,'function');
const unowned=new SpeechCues();
for(let t=0;t<5000;t+=20)assert.deepEqual(unowned.frame(t,{speaking:true,mediaTime:t/1000,duration:5}),{cue:null,focus:''});

// Identical audio duration/clock (no volume input) with a different focal
// location schedules different beats. This is a text-duration estimate only.
const early=session('重点是后半段。前面只是轻轻地铺开，后来才逐渐安静下来。');
const late=session('前面只是轻轻地铺开，后来才逐渐安静下来。重点是后半段。');
early.run(8);late.run(8);
assert.equal(early.events.length,1);
assert.equal(late.events.length,1);
assert.equal(early.events[0].kind,'emphasis');
assert(late.events[0].at-early.events[0].at>4,'late focal words must not get a generic opening nod');
assert.equal(early.value.cue,null,'an emitted cue is an event, never a sticky command');

// No explicit semantics just keeps direct engagement. Do not add affirmative
// emphasis for negations, uncertain preferences, or quoted instructions.
for(const text of ['这段旋律听起来很安静。', '我不太喜欢这一版。', '不是真的。', '没有特别的感觉。',
  '我并不是更喜欢这个版本。', '这不是重点是在哪里的问题。', '他说“重点是节奏”，我只是转述。',
  '标题是「你也喜欢吗？」。', '他说“你也喜欢吗？”', 'Read https://example.org/what?',
  '他说“你也喜欢吗？', '标题是「怎么说呢？', 'He asked "Do you like it?',
  "I don't think the key point is the ending."]) {
  const s=session(text);s.run(8);
  assert.equal(s.events.length,0,text);
  assert.equal(s.value.focus,'engage',text);
}
const contrast=session('不是前半段，而是更喜欢后半段。');contrast.run(8);
assert.equal(contrast.events[0].kind,'emphasis','a real positive contrast is still usable');

// Reflection is short and returns to eye contact. A real terminal question
// owns a final waiting gaze; an earlier question does not do so.
const reflect=session('我想想。它更像是一段雨停后的小路。');
reflect.run(.4);
assert.equal(reflect.events[0].kind,'reflect');
assert.equal(reflect.value.focus,'reflect');
reflect.run(1.2);
assert.equal(reflect.value.focus,'engage');
const rhetorical=session('你也这么想？我只是觉得这一段很安静。');rhetorical.run(8);
assert.equal(rhetorical.events.length,0);
const question=session('这一段很安静，你也喜欢这样的感觉吗？');question.run(8);
assert.equal(question.events.at(-1).kind,'question');
question.p.end({completed:true},question.now);
assert.equal(question.step({speaking:false},10,0).focus,'question');
question.run(1.5,{speaking:false});
assert.equal(question.value.focus,'question');
question.run(.4,{speaking:false});
assert.equal(question.value.focus,'');

// A short sentence gets one clear cue, a longer one at most three, separated
// enough for the host's performance cooldown rather than a nod per keyword.
const dense='我想想。重点是先理解这里。这里我更喜欢慢一点。关键是后半段。你觉得呢？';
for(const duration of [1.3,3,8,20]) {
  const s=session(dense,duration);s.run(duration);
  assert(s.events.length>=1 && s.events.length<=3);
  if(duration<1.6)assert.equal(s.events.length,1);
  for(let i=1;i<s.events.length;i++)assert(s.events[i].at-s.events[i-1].at>=1.59);
  assert.equal(new Set(s.events.map(e=>e.id)).size,s.events.length);
}

// Time needs a real, advancing clock. Unknown duration never guesses. When
// metadata finally arrives, already-passed beats are discarded, not caught up.
const unknown=session('我想想。前面很安静，重点是后面的节奏。你觉得呢？',NaN);
unknown.run(4);
assert.equal(unknown.events.length,0);
assert.equal(unknown.value.focus,'engage');
unknown.step({duration:8});
unknown.run(4,{duration:8});
assert(unknown.events.every(e=>e.kind!=='reflect' && e.at>4));
assert(unknown.events.some(e=>e.kind==='question'));
const tooLate=session('重点是后半段。',NaN);tooLate.run(7);
tooLate.run(1,{duration:8});
assert.equal(tooLate.events.length,0);
const unknownQuestion=session('你觉得呢？',NaN);unknownQuestion.run(8);
unknownQuestion.p.end({completed:true},unknownQuestion.now);
assert.equal(unknownQuestion.step({speaking:false}).focus,'','unknown timing cannot invent a final question hold');
for(const duration of [NaN,Infinity,-1,0]) {
  const s=session('重点是后半段。',duration);s.run(8);
  assert.equal(s.events.length,0);
}
const revised=session(dense,12);
revised.run(2.5);
revised.run(1,{duration:8});
revised.run(1,{duration:5});
revised.run(8,{duration:12});
assert(revised.events.length<=3,'duration revisions cannot replenish the cue budget');
assert.equal(new Set(revised.events.map(e=>e.id)).size,revised.events.length);
for(let i=1;i<revised.events.length;i++)assert(revised.events[i].at-revised.events[i-1].at>=1.59,
  'duration revisions cannot schedule an extra movement inside the host cooldown');

// Pausing/stalling freezes a reflective gaze. Forward jumps/seek/backwards
// never replay a consumed or skipped cue. Resumption only covers new progress.
for(const kind of ['pause','buffer','stall']) {
  const s=session('我想想。这一段应该慢慢听。');s.run(.3);
  assert.equal(s.value.focus,'reflect');
  for(let i=0;i<200;i++)s.step(kind==='pause'?{paused:true}:kind==='buffer'?{speaking:false}:{},10,0);
  assert.equal(s.value.focus,'reflect',kind);
  s.run(1.1);
  assert.equal(s.value.focus,'engage');
  assert.equal(s.events.length,1);
}
const seek=session('前面这段旋律慢慢地展开，重点是后面的节奏。你觉得呢？',10);
seek.run(.5);
seek.step({},10,8);
assert.equal(seek.events.length,0,'forward seek skips the focal cue');
seek.run(1.5);
assert(seek.events.every(e=>e.at>8.5));
const already=seek.events.length;
seek.step({},10,-9);
seek.run(9);
assert.equal(seek.events.length,already,'backward seek cannot replay previous beats');
const background=session('我想想。前面这段慢慢地铺开，重点是后面的节奏。',8);
background.step({},6000,6);
background.run(2);
assert.equal(background.events.length,0,'background jump discards all past cues');
const noMedia=session('重点是后半段。');
noMedia.run(2,{mediaTime:NaN});
noMedia.run(6);
assert.equal(noMedia.events.length,0,'missing media time does not create an opening cue on recovery');

// Repeated events/late earlier turns cannot restart semantics. Segment changes
// and listening remove the completed-question hold, cancellation retires a turn.
const lifecycle=session('你也觉得这样好吗？',3);lifecycle.run(3);
const count=lifecycle.events.length,lastMedia=lifecycle.p.lastMedia;
assert.equal(lifecycle.p.begin({text:'你也觉得这样好吗？',turnId:1,segmentIndex:0}),false);
assert.equal(lifecycle.p.lastMedia,lastMedia);
lifecycle.p.end({completed:true},lifecycle.now);
assert.equal(lifecycle.p.begin({turnId:1,segmentIndex:0,text:'旧段。'}),false);
assert.equal(lifecycle.begin({text:'下一句很普通。'}),true);
assert.notEqual(lifecycle.value.focus,'question');
lifecycle.run(.1);
lifecycle.p.end({completed:false},lifecycle.now);
assert.equal(lifecycle.p.begin({turnId:1,segmentIndex:2,text:'重点是旧段。'}),false);
assert.equal(lifecycle.step({speaking:false}).focus,'');
assert.equal(lifecycle.begin({turnId:2,segmentIndex:0,text:'新的内容。'}),true);
assert.equal(lifecycle.p.begin({turnId:1,segmentIndex:99,text:'重点是旧段。'}),false);
assert.equal(lifecycle.events.length,count);
lifecycle.p.resetSession();
assert.equal(lifecycle.begin({turnId:1,segmentIndex:0,text:'重点是新的内容。'}),true);
lifecycle.run(.3);
assert.equal(lifecycle.events.at(-1).kind,'emphasis');
for(const interruption of ['listening','clear','cancel']) {
  const s=session('你也觉得这样好吗？',3);s.run(3);
  s.p.end({completed:true},s.now);
  if(interruption==='clear')s.p.clearFocus();
  if(interruption==='cancel')s.p.end({completed:false},s.now);
  const f=s.step({speaking:false,listening:interruption==='listening'},10,0);
  assert.equal(f.focus,'',interruption);
}
const edit=session('你也觉得这样好吗？',3);edit.run(2.5);
edit.p.clearFocus();edit.run(.5);edit.p.end({completed:true},edit.now);
assert.equal(edit.step({speaking:false}).focus,'','new input forbids restoring the same question hold');
assert.equal(edit.begin({text:'下一句内容。'}),true,'clearing focus does not cancel the whole turn');
const earlyEdit=session('你也觉得这样好吗？',3);earlyEdit.run(.2);earlyEdit.p.clearFocus();
assert(earlyEdit.run(2.8).every(f=>f.focus!=='question'),'new input also prevents a later cue restoring old question gaze');
const mic=session('我想想。还有后面的内容。');mic.run(.3);
assert.equal(mic.step({listening:true,speaking:false}).focus,'');
assert.equal(mic.p.begin({turnId:1,segmentIndex:1,text:'迟到内容。'}),false);

// Completed hold freezes on pause; duplicate end cannot extend it forever.
const held=session('你觉得呢？',3);held.run(3);held.p.end({completed:true},held.now);
held.run(1,{speaking:false});held.p.end({completed:true},held.now);
held.run(2,{speaking:false,paused:true});assert.equal(held.value.focus,'question');
held.run(.9,{speaking:false});assert.equal(held.value.focus,'');
const hiddenHold=session('你觉得呢？',3);hiddenHold.run(3);hiddenHold.p.end({completed:true},hiddenHold.now);
assert.equal(hiddenHold.step({speaking:false},60000,0).focus,'','completed waiting gaze expires in a background tab');

// Unicode, long numeric IDs, English words and punctuation stay bounded.
// A long Latin word must not outweigh every Chinese syllable merely by bytes.
for(const text of ['Let me think. The key point is the ending. What do you think?',
  '我想想。2026101912345678901234567890只是编号，重点是后半段。你觉得呢？',
  'I prefer the quiet ending. Do you like it?', '我想想，🎶重点是后半段。', '我想想。'+'9'.repeat(5000)+'重点是后半段。']) {
  const s=session(text,12);s.run(12);
  assert(s.events.length<=3);
  for(const event of s.events)assert(Number.isFinite(event.at)&&event.at>=0&&event.at<=12.01);
}
const times=[];
for(const fps of [10,30,60,120]) {
  const s=session('前面这段旋律慢慢展开，重点是后半段的节奏。',8);s.run(8,{},fps);
  assert.equal(s.events.length,1);
  times.push(s.events[0].at);
}
assert(Math.max(...times)-Math.min(...times)<.11,'frame rate does not materially relocate a semantic cue');
console.log('speech cues: semantic estimates, clock guards, focus ownership and lifecycle passed');
