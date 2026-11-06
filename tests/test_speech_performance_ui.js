const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const SpeechPerformance=require('../brain/static/speech-performance.js');
const keys=Object.keys(SpeechPerformance.limits);
const motionKeys=keys.filter(k=>k.startsWith('head')||k.startsWith('body'));
const eyeKeys=['gazeX','gazeY','eyeOpen'];
const faceKeys=['smile','browL','browR'];
const magnitude=f=>Math.max(...keys.map(k=>Math.abs(f[k])));
function seeded(seed=73) {
  let calls=0;
  const random=()=>{calls++;seed=(Math.imul(seed,1664525)+1013904223)>>>0;return seed/4294967296;};
  return {random,get calls(){return calls;}};
}
function session(emotion='平静',options={},rng=seeded()) {
  const p=new SpeechPerformance({random:rng.random});
  p.begin({turnId:1,emotion});
  let now=0,media=0,value=p.frame(0,{speaking:true,...options});
  return {p,rng,get value(){return value;},get now(){return now;},get media(){return media;},
    step(extra={},ms=10,advance=ms/1000) {
      now+=ms;media+=advance;
      value=p.frame(now,{speaking:true,mediaTime:media,energy:.6,...options,...extra});
      return value;
    },
    run(count,extra={},ms=10) {return Array.from({length:count},()=>this.step(extra,ms));},
    begin(args={}) {media=0;p.begin({turnId:1,segmentIndex:1,emotion,...args});this.step({},0,0);},
  };
}
function bounded(f) {
  for(const k of keys) {
    assert(Number.isFinite(f[k]),`${k} is finite`);
    assert(Math.abs(f[k])<=SpeechPerformance.limits[k],`${k} respects its hard limit`);
  }
  assert(f.weight>=0&&f.weight<=1);
  assert.equal(typeof f.active,'boolean');
  assert.equal(typeof f.cue,'string');
}

// Browser and CommonJS consumers share the same API. Unowned or silent audio
// cannot create a pose, even when an unrelated accent value is present.
const browser={window:{}};
vm.runInNewContext(fs.readFileSync(require.resolve('../brain/static/speech-performance.js'),'utf8'),browser);
assert.equal(typeof browser.window.CompanionSpeechPerformance,'function');
const unowned=new SpeechPerformance();
for(let t=0;t<3000;t+=10)assert.equal(magnitude(unowned.frame(t,{speaking:true,energy:1,accent:1,mediaTime:t/1000})),0);
const silent=session();
silent.run(700,{energy:0,accent:1}).forEach(f=>assert.equal(magnitude(f),0));
assert.equal(silent.rng.calls,0);
const waiting=session();
waiting.run(300,{speaking:false,energy:1}).forEach(f=>assert.equal(magnitude(f),0));
assert.equal(waiting.rng.calls,0);
const moving=waiting.run(50);
assert(moving.some(f=>Math.abs(f.headY)+Math.abs(f.headX)+Math.abs(f.headZ)>2));

// Every emotion gets a clear, bounded coordinated cue. Eyes/brows start about
// 100ms ahead of the head, and each cue fully returns before another begins.
const peaks=[];
for(const emotion of Object.keys(SpeechPerformance.profiles)) {
  const s=session(emotion,{eyeIntensity:1,faceIntensity:1});
  const frames=s.run(200);
  const firstEye=frames.findIndex(f=>eyeKeys.concat(faceKeys).some(k=>Math.abs(f[k])>.0001));
  const firstHead=frames.findIndex(f=>Math.max(...['headX','headY','headZ'].map(k=>Math.abs(f[k])))>.0001);
  assert(firstEye>=0 && firstHead-firstEye>=8,`${emotion}: face precedes head by at least 80ms`);
  const peak=Math.max(...frames.flatMap(f=>['headX','headY','headZ'].map(k=>Math.abs(f[k]))));
  assert(peak>=3.1&&peak<=5,`${emotion}: 1x head travel should be clearly 3–5 degrees, got ${peak}`);
  assert(magnitude(frames.at(-1))<.001,`${emotion}: no permanent speech sway`);
  frames.forEach(bounded);
  if(['委屈','生气'].includes(emotion))assert(frames.every(f=>f.smile===0),`${emotion} never smiles`);
  if(['得意','吐槽'].includes(emotion))assert(frames.some(f=>Math.abs(f.browL-f.browR)>.03&&Math.abs(f.gazeX)>.05));
  if(emotion==='惊讶')assert(frames.some(f=>f.eyeOpen>.05&&f.browL>.05&&f.headY>2&&f.bodyY<0));
  if(emotion==='开心')assert(frames.some(f=>f.smile>.05&&f.eyeOpen<-.035));
  peaks.push(`${emotion} ${peak.toFixed(2)}°`);
}

// With no model action tags and no per-syllable accents, sustained voice still
// gets sparse, varied phrasing. Every event consumes exactly five RNG samples.
const steady=session(),events=[];
let calls=0;
for(let i=0;i<2400;i++) {
  steady.step();
  if(steady.rng.calls!==calls) {
    assert.equal(steady.rng.calls-calls,5);
    events.push({at:steady.p.clock,variant:steady.p.clip.variant,cue:steady.value.cue});
    calls=steady.rng.calls;
  }
}
assert(events.length>=6&&events.length<=10,'roughly one event every 2.5–4 seconds');
assert.equal(new Set(events.slice(0,3).map(e=>e.variant)).size,3,'all three contours rotate');
const intervals=events.slice(1).map((e,i)=>e.at-events[i].at);
assert(intervals.every(gap=>gap>=2.49&&gap<=4.02));
assert(Math.max(...intervals)-Math.min(...intervals)>.3,'phrasing is not a fixed metronome');

// Accent level must cross an edge and observe a cooldown. A level held high
// must not produce a new nod on every syllable/frame.
const emphasis=session();
emphasis.run(165);
const firstCalls=emphasis.rng.calls;
emphasis.step({accent:.8});
assert.equal(emphasis.value.cue,'accent');
assert.equal(emphasis.rng.calls,firstCalls+5);
emphasis.run(170,{accent:.8});
assert.equal(emphasis.rng.calls,firstCalls+5,'held accent has no second rising edge or premature phrase beat');
emphasis.step({accent:0});
emphasis.step({accent:.8});
assert.equal(emphasis.rng.calls,firstCalls+10);

// Brief syllabic gaps do not count as fresh phrases. A longer pause releases
// eyes before head/body, then silence never schedules new movement.
const syllables=session();
for(let i=0;i<220;i++)syllables.step({energy:i%20<8 ? 0 : .7});
assert.equal(syllables.rng.calls,5,'80ms gaps cannot trigger serial onset nods');
const pause=session();
pause.run(40);
const beforePause=pause.rng.calls;
const settling=pause.run(150,{energy:0});
assert(settling.some(f=>f.cue==='return'));
assert(settling.slice(50).some(f=>Math.abs(f.browL)<.001&&Math.abs(f.headY)>.05),'brows settle before the head');
assert(magnitude(settling.at(-1))<.001);
pause.run(700,{energy:0,accent:.9});
assert.equal(pause.rng.calls,beforePause,'silent playback never generates accents');
pause.run(40);
assert(pause.rng.calls>beforePause,'audible restart after a real pause can acknowledge a new phrase');

// Pause, buffering, no media progress, seek, invalid time and suspended tabs
// freeze the active contour. Resuming advances only the current media frame.
for(const situation of ['paused','buffering','stalled','seek','backwards','nan','tab']) {
  const s=session();s.run(40);
  const before={...s.value},clock=s.p.clock,rngCalls=s.rng.calls;
  if(situation==='paused')s.run(70,{paused:true});
  if(situation==='buffering')s.run(70,{speaking:false});
  if(situation==='stalled')for(let i=0;i<70;i++)s.step({},10,0);
  if(situation==='seek')s.step({},10,20);
  if(situation==='backwards')s.step({},10,-.1);
  if(situation==='nan')s.step({mediaTime:NaN},10,0);
  if(situation==='tab')s.step({},2000,2);
  assert.deepEqual(s.value,before,`${situation}: pose stays frozen`);
  assert.equal(s.p.clock,clock);
  assert.equal(s.rng.calls,rngCalls);
  if(situation==='nan')s.step(); // First finite sample establishes a new anchor.
  s.step();
  assert(s.p.clock-clock<=.021,`${situation}: resume cannot catch up`);
}
const invalidTime=session();invalidTime.run(40);
const beforeInvalid={...invalidTime.value},beforeClock=invalidTime.p.clock;
assert.deepEqual(invalidTime.p.frame(NaN,{speaking:true,energy:1,mediaTime:invalidTime.media+.01}),beforeInvalid);
assert.deepEqual(invalidTime.p.frame(invalidTime.now-10,{speaking:true,energy:1,mediaTime:invalidTime.media+.02}),beforeInvalid);
assert.equal(invalidTime.p.clock,beforeClock);

// Completion and interruption release without the audio clock. A completed
// segment may continue, but its short follow-up cannot re-trigger the opener.
const complete=session();complete.run(40);
const completedCount=complete.rng.calls;
complete.p.end({completed:true});
assert(complete.step({speaking:false},10,0).active,'completion begins a smooth release');
complete.run(12,{speaking:false});
complete.begin();
complete.run(60);
assert.equal(complete.rng.calls,completedCount,'segment boundary retains onset cooldown');
complete.run(300);
assert(complete.rng.calls>completedCount,'same-turn continuation still receives later phrasing');
complete.p.end({completed:true});
const released=complete.run(180,{speaking:false,paused:true,energy:1,accent:1});
assert(magnitude(released.at(-1))<.0001);
const cancel=session();cancel.run(40);
cancel.p.end();
assert.equal(cancel.p.begin({turnId:1,segmentIndex:2}),false,'cancelled turn rejects delayed playback');
cancel.run(250,{speaking:true,energy:1});
assert.equal(cancel.rng.calls,5);
assert.equal(magnitude(cancel.value),0);
cancel.p.resetSession();
cancel.begin({segmentIndex:0});
cancel.run(50);
assert.equal(cancel.rng.calls,10,'reconnection permits a reused turn identifier');

// Repeated playing callbacks do not reset a moving segment's media anchor.
const duplicate=session();duplicate.run(40);
const duplicateMedia=duplicate.p.lastMedia;
assert.equal(duplicate.p.begin({turnId:1,segmentIndex:0}),false);
assert.equal(duplicate.p.lastMedia,duplicateMedia);

// Opening the microphone takes ownership from a paused speech item, even if
// that old item remains in the audio queue. Its pose releases and stays retired.
const microphone=session();microphone.run(40);
const microphoneCalls=microphone.rng.calls;
assert(microphone.step({listening:true,speaking:false,paused:true},10,0).active,
  'microphone takeover releases smoothly rather than snapping to neutral');
microphone.run(240,{listening:true,speaking:false,paused:true});
assert.equal(magnitude(microphone.value),0);
assert.equal(microphone.p.segmentActive,false);
assert.equal(microphone.p.begin({turnId:1,segmentIndex:1}),false,'mic takeover retires the old turn');
microphone.run(100,{listening:false});
assert.equal(microphone.rng.calls,microphoneCalls);
assert.equal(magnitude(microphone.value),0,'closing the mic cannot resurrect old speech gestures');
microphone.begin({turnId:2,segmentIndex:0});microphone.run(50);
assert(microphone.value.active,'a genuinely new reply still works after microphone takeover');

// Independent controls immediately suppress only the selected channel, and an
// expression asset owns the entire face/gaze even in the middle of a cue.
const controls=session('开心',{eyeIntensity:1,faceIntensity:1});controls.run(40);
const visible={...controls.value};
const noMotion=controls.step({intensity:0},0,0);
motionKeys.forEach(k=>assert.equal(noMotion[k],0));
eyeKeys.concat(faceKeys).forEach(k=>assert.equal(noMotion[k],visible[k]));
const noEyes=controls.step({eyeIntensity:0},0,0);
eyeKeys.forEach(k=>assert.equal(noEyes[k],0));
faceKeys.concat(motionKeys).forEach(k=>assert.equal(noEyes[k],visible[k]));
const noFace=controls.step({faceIntensity:0},0,0);
faceKeys.concat('eyeOpen').forEach(k=>assert.equal(noFace[k],0));
['gazeX','gazeY'].concat(motionKeys).forEach(k=>assert.equal(noFace[k],visible[k]));
const explicit=controls.step({explicitFace:true},0,0);
eyeKeys.concat(faceKeys).forEach(k=>assert.equal(explicit[k],0));
assert(motionKeys.some(k=>Math.abs(explicit[k])>.1));
motionKeys.forEach(k=>assert(Math.abs(explicit[k])<=Math.abs(visible[k])*.45000001));
assert.equal(magnitude(controls.step({performance:0},0,0)),0);
assert.equal(controls.value.active,false);
const off=controls.step({intensity:0,eyeIntensity:0,faceIntensity:0},0,0);
assert.equal(magnitude(off),0);
assert.equal(off.active,false);
assert.equal(off.cue,'');

// Native bounds remain strict under large settings and malformed input. No
// configuration or random source is allowed to introduce NaN into Cubism.
for(const emotion of Object.keys(SpeechPerformance.profiles)) {
  const s=session(emotion,{performance:999,intensity:999,eyeIntensity:999,faceIntensity:999});
  s.run(1500).forEach(bounded);
}
for(const badRandom of [()=>NaN,()=>Infinity,()=>-1,()=>{throw Error('rng');}]) {
  const p=new SpeechPerformance({random:badRandom});p.begin({turnId:1});
  for(let i=0;i<300;i++)bounded(p.frame(i*10,{speaking:true,mediaTime:i/100,energy:.5}));
  bounded(p.frame(3010,{speaking:true,mediaTime:3.01,energy:NaN,accent:Infinity,
    intensity:NaN,faceIntensity:NaN,eyeIntensity:NaN,performance:NaN}));
}

// A legal 5fps frame is integrated in short substeps: it should follow the
// same contour and event count as 100fps, without silently losing audio time.
const fast=session('惊讶',{},seeded(91)),slow=session('惊讶',{},seeded(91));
let worstHead=0,worstFace=0;
for(let i=0;i<90;i++) {
  fast.run(20);slow.step({},200);
  for(const k of ['headX','headY','headZ'])worstHead=Math.max(worstHead,Math.abs(fast.value[k]-slow.value[k]));
  for(const k of eyeKeys.concat(faceKeys))worstFace=Math.max(worstFace,Math.abs(fast.value[k]-slow.value[k]));
}
assert.equal(fast.rng.calls,slow.rng.calls,'low fps does not lose phrase events');
assert(Math.abs(fast.p.clock-slow.p.clock)<1e-8);
assert(worstHead<.15,`5fps and 100fps head contours agree: ${worstHead}`);
assert(worstFace<.008,`5fps and 100fps face contours agree: ${worstFace}`);

// Semantic mode waits for the planner even with sustained energy, repeated
// voice onsets, accent peaks and enough time for several old generic phrases.
const semantic=session('平静',{semanticMode:true,eyeIntensity:1,faceIntensity:1});
for(let i=0;i<1000;i++) {
  const out=semantic.step({energy:i%70<25 ? 0 : .7,accent:i%12<4 ? .8 : 0});
  assert.equal(magnitude(out),0,'unannotated loud speech cannot invent semantic gestures');
}
assert.equal(semantic.rng.calls,0);
semantic.step({semanticCue:{id:'important-1',kind:'emphasis'},energy:.7});
const semanticFrames=semantic.run(170,{energy:.7});
assert(semanticFrames.some(f=>f.headY < -3), 'the marked emphasis produces a clear, bounded nod');
assert.equal(semantic.rng.calls,5);
semantic.run(500,{energy:.7,accent:.9});
assert.equal(semantic.rng.calls,5,'no onset, accent or timer fallback after a semantic cue');
assert.equal(magnitude(semantic.value),0);

// Same audio and RNG, different meanings: reflection and questions tilt while
// emphasis nods. Questions do not accidentally signal agreement with a nod.
const kinds={};
for(const kind of ['reflect','emphasis','question']) {
  const s=session('平静',{semanticMode:true,eyeIntensity:1,faceIntensity:1},seeded(19));
  s.step({semanticCue:{id:kind,kind}});
  kinds[kind]=s.run(170);
  kinds[kind].forEach(f=>{
    bounded(f);
    assert.equal(f.gazeX,0,'semantic performance yields horizontal gaze ownership');
    assert.equal(f.gazeY,0,'semantic performance yields vertical gaze ownership');
    assert.equal(f.smile,0,'meaning alone must not invent a positive emotion');
  });
}
const maximum=(samples,key)=>Math.max(...samples.map(f=>Math.abs(f[key])));
assert(maximum(kinds.reflect,'headZ')>2.5 && maximum(kinds.reflect,'headY')<.4);
assert(maximum(kinds.emphasis,'headY')>3 && maximum(kinds.emphasis,'headZ')<.7);
assert(maximum(kinds.question,'headZ')>3 && kinds.question.every(f=>f.headY>=0));
assert(maximum(kinds.question,'browL')>maximum(kinds.reflect,'browL')+.02);
assert(maximum(kinds.question,'browR')>maximum(kinds.reflect,'browR')+.02);
for(const emotion of ['委屈','生气'])for(const kind of ['reflect','emphasis','question']) {
  const s=session(emotion,{semanticMode:true,faceIntensity:1,eyeIntensity:1});
  s.step({semanticCue:{id:0,kind}});
  s.run(170).forEach(f=>assert.equal(f.smile,0,`${emotion}/${kind} never adds a smile`));
}

// An ID is a one-shot event, including when it arrives during a pause, a clock
// discontinuity, or silence. It is never queued for a later resume or voice.
const repeated=session('平静',{semanticMode:true});
repeated.step({semanticCue:{id:'same',kind:'question'}});
repeated.run(700,{semanticCue:{id:'same',kind:'question'}});
assert.equal(repeated.rng.calls,5,'an attached cue triggers only once across hundreds of frames');
assert.equal(magnitude(repeated.value),0);
for(const situation of ['paused','buffering','stalled','seek','backwards','nan','tab','silence','listening']) {
  const s=session('平静',{semanticMode:true});
  const cue={id:situation,kind:'question'};
  s.run(20);
  if(situation==='paused')s.step({semanticCue:cue,paused:true});
  if(situation==='buffering')s.step({semanticCue:cue,speaking:false});
  if(situation==='stalled')s.step({semanticCue:cue},10,0);
  if(situation==='seek')s.step({semanticCue:cue},10,20);
  if(situation==='backwards')s.step({semanticCue:cue},10,-.1);
  if(situation==='nan')s.step({semanticCue:cue,mediaTime:NaN});
  if(situation==='tab')s.step({semanticCue:cue},2000,2);
  if(situation==='silence')s.step({semanticCue:cue,energy:0});
  if(situation==='listening')s.step({semanticCue:cue,listening:true});
  s.run(300,{semanticCue:cue});
  assert.equal(s.rng.calls,0,`${situation}: discarded cue cannot replay later`);
  assert.equal(magnitude(s.value),0);
  s.step({semanticCue:{id:`new-${situation}`,kind:'reflect'}});
  assert.equal(s.rng.calls,5,`${situation}: a genuinely new, audible event can still play`);
}

// Dense planner events are consumed, not postponed. The first semantic cue can
// replace a generic opener, so short marked sentences retain their meaning.
const dense=session('平静',{semanticMode:true});
dense.step({semanticCue:{id:'first',kind:'reflect'}});
for(let i=0;i<100;i++)dense.step({semanticCue:{id:`dense-${i}`,kind:'question'}});
assert.equal(dense.rng.calls,5);
dense.run(200);
dense.step({semanticCue:{id:'dense-50',kind:'question'}});
assert.equal(dense.rng.calls,5,'rejected dense events are not replayed after cooldown');
dense.step({semanticCue:{id:'later',kind:'emphasis'}});
assert.equal(dense.rng.calls,10);
const genericReplacement=session();
genericReplacement.run(25);
assert.equal(genericReplacement.value.cue,'onset');
genericReplacement.step({semanticMode:true,semanticCue:{id:'short-question',kind:'question'}});
assert.equal(genericReplacement.value.cue,'question');
assert.equal(genericReplacement.p.clip.semanticKind,'question');
assert.equal(genericReplacement.rng.calls,10);
assert.equal(genericReplacement.value.gazeX,0,'focus owns gaze immediately, including an outgoing generic clip');

// Semantic channels obey native expression ownership and independent sliders.
// A cancelled turn, pending completion or session reset follows the old audio
// lifecycle and cannot resurrect a queued semantic pose.
const semanticControls=session('开心',{semanticMode:true,eyeIntensity:1,faceIntensity:1});
semanticControls.step({semanticCue:{id:'controls',kind:'question'}});
semanticControls.run(40);
const semanticVisible={...semanticControls.value};
const semanticNative=semanticControls.step({explicitFace:true},0,0);
eyeKeys.concat(faceKeys).forEach(k=>assert.equal(semanticNative[k],0));
motionKeys.forEach(k=>assert(Math.abs(semanticNative[k])<=Math.abs(semanticVisible[k])*.45000001));
const semanticMotionOff=semanticControls.step({intensity:0},0,0);
motionKeys.forEach(k=>assert.equal(semanticMotionOff[k],0));
const semanticEyeOff=semanticControls.step({eyeIntensity:0},0,0);
eyeKeys.forEach(k=>assert.equal(semanticEyeOff[k],0));
const semanticFaceOff=semanticControls.step({faceIntensity:0},0,0);
faceKeys.concat('eyeOpen').forEach(k=>assert.equal(semanticFaceOff[k],0));
assert.equal(magnitude(semanticControls.step({performance:0},0,0)),0);
semanticControls.p.end();
assert.equal(semanticControls.p.semanticIds.size,0);
assert.equal(semanticControls.p.begin({turnId:1,segmentIndex:1}),false);
semanticControls.run(200,{semanticCue:{id:'late',kind:'reflect'}});
assert.equal(semanticControls.rng.calls,5);
assert.equal(magnitude(semanticControls.value),0);
semanticControls.p.resetSession();
assert.equal(semanticControls.p.semanticIds.size,0);
semanticControls.begin({segmentIndex:0});
semanticControls.step({semanticCue:{id:'controls',kind:'question'}});
assert.equal(semanticControls.rng.calls,10,'a new session permits reused event and turn IDs');
const semanticContinuation=session('平静',{semanticMode:true});
semanticContinuation.step({semanticCue:{id:'part-one',kind:'reflect'}});
semanticContinuation.run(40);
semanticContinuation.p.end({completed:true});
assert.equal(semanticContinuation.p.semanticIds.size,0);
semanticContinuation.begin();
semanticContinuation.step({semanticCue:{id:'part-two',kind:'question'}});
assert.equal(semanticContinuation.rng.calls,5,'segment boundaries preserve the sparse semantic cooldown');
semanticContinuation.run(170);
semanticContinuation.step({semanticCue:{id:'part-three',kind:'question'}});
assert.equal(semanticContinuation.rng.calls,10);

// Event-aligned substeps preserve the same semantic contour at legal low FPS.
const semanticFast=session('平静',{semanticMode:true},seeded(29));
const semanticSlow=session('平静',{semanticMode:true},seeded(29));
let semanticError=0;
for(let i=0;i<15;i++) {
  const event=i===0 ? {semanticCue:{id:'fps',kind:'emphasis'}} : {};
  semanticFast.step(event);semanticFast.run(19);
  semanticSlow.step(event,200);
  for(const k of motionKeys)semanticError=Math.max(semanticError,Math.abs(semanticFast.value[k]-semanticSlow.value[k]));
}
assert.equal(semanticFast.rng.calls,5);
assert.equal(semanticSlow.rng.calls,5);
assert(semanticError<.08,`semantic low-FPS contour preserves the cue: ${semanticError}`);
const malformed=session('平静',{semanticMode:true});
for(const semanticCue of [null,{},'question',{id:'bad',kind:'__proto__'},
  {id:'bad',kind:'nod'},{id:'',kind:'question'},{id:NaN,kind:'question'}])
  assert.equal(magnitude(malformed.step({semanticCue})),0);
assert.equal(malformed.rng.calls,0);
console.log('semantic performance: explicit cue timing, meaning-specific contours, focus ownership, one-shot lifecycle and controls passed');
console.log(`speech performance: all checks passed; ${peaks.join(', ')}; 5fps error ${worstHead.toFixed(3)}°`);
