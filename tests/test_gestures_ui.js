const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const Gestures=require('../brain/static/gestures.js');
const keys=Object.keys(Gestures.limits);
const head=['headX','headY','headZ'];
const eyes=['gazeX','gazeY','eyeOpen'];
const face=['eyeOpen','smile','brow'];
function seeded(seed=42) {
  let calls=0;
  const random=()=>{calls++;seed=(Math.imul(seed,1664525)+1013904223)>>>0;return seed/4294967296;};
  return {random,get calls(){return calls;}};
}
function session(name='认可',options={}) {
  const rng=seeded(),g=new Gestures({random:rng.random});
  g.begin({turnId:'reply',actions:[name]});
  let now=0,media=0;
  let value=g.frame(now,{speaking:true,mediaTime:media,...options});
  return {g,rng,get now(){return now;},get media(){return media;},get value(){return value;},
    step(extra={},ms=16,advance=ms/1000) {
      now+=ms;media+=advance;
      value=g.frame(now,{speaking:true,mediaTime:media,...options,...extra});
      return value;
    },
    run(count,extra={}) {const result=[];for(let i=0;i<count;i++)result.push(this.step(extra));return result;},
  };
}
const magnitude=f=>Math.max(...keys.map(key=>Math.abs(f[key])));
function bounded(frame) {
  for(const key of keys) {
    assert(Number.isFinite(frame[key]),`${key} must always be finite`);
    assert(Math.abs(frame[key])<=Gestures.limits[key],`${key} respects its documented hard bound`);
  }
  assert.equal(typeof frame.active,'boolean');
  assert.equal(typeof frame.name,'string');
}

// Idle and unknown semantics do not manufacture movement. Exports work both
// with CommonJS and as an ordinary browser script.
const browser={window:{}};
vm.runInNewContext(fs.readFileSync(require.resolve('../brain/static/gestures.js'),'utf8'),browser);
assert.equal(typeof browser.window.CompanionGestures,'function');
const idle=new Gestures();
for(let t=0;t<10000;t+=16)assert.equal(magnitude(idle.frame(t)),0);
const unknown=session('未知动作');
assert(unknown.run(300).every(f=>!f.active&&magnitude(f)===0));
const fallback=new Gestures();
fallback.begin({turnId:1,emotion:'吐槽'});
fallback.frame(0,{speaking:true});
assert.equal(fallback.frame(16,{speaking:true,mediaTime:.016}).name,'轻吐槽');

// Every action has a visible but restrained start, dwell and complete return.
// A short eye cue arrives before any head movement; no frame samples randomness.
let measured=[];
for(const name of Object.keys(Gestures.poses)) {
  const s=session(name),frames=s.run(250);
  const peak=Math.max(...frames.flatMap(f=>head.map(key=>Math.abs(f[key]))));
  assert(peak>=3.8&&peak<9,`${name} has visible, restrained head travel`);
  assert(frames.slice(0,10).every(f=>head.every(key=>f[key]===0)),`${name}: eyes lead the head`);
  assert(frames.slice(0,10).some(f=>Math.abs(f.gazeX)+Math.abs(f.gazeY)>.01));
  assert(frames.filter(f=>Math.max(...head.map(key=>Math.abs(f[key])))>peak*.9).length>=20,
    `${name} includes a held pose, not a momentary spike`);
  assert(magnitude(frames.at(-1))<.001,`${name} returns to rest during a long segment`);
  assert.equal(s.rng.calls,4,'randomness occurs only once when a gesture starts');
  frames.forEach(bounded);
  measured.push(`${name} ${peak.toFixed(2)}°`);
}

// Listening nods once then holds a small lean. Thinking glances once then
// returns; changing phases must blend instead of teleporting to another pose.
const attentionRng=seeded(),attention=new Gestures({random:attentionRng.random});
let last,listenFrames=[],thinkFrames=[];
for(let t=0;t<32000;t+=16) {
  const f=attention.frame(t,{listening:t<16000,thinking:t>=16000});
  if(last)for(const key of head)assert(Math.abs(f[key]-last[key])<=32*.016+1e-10);
  (t<16000?listenFrames:thinkFrames).push(f);
  last=f;
}
assert(Math.min(...listenFrames.map(f=>f.headY))<-4,'listening includes one clear nod');
assert(listenFrames.slice(400).every(f=>Math.abs(f.headY+.45)<.0001&&Math.abs(f.bodyY-.65)<.0001),
  'long listening remains a static light lean with no repeated nod');
assert(thinkFrames.some(f=>Math.abs(f.headX)>3.5&&f.gazeY>.1),'thinking looks upward and aside');
assert(thinkFrames.slice(400).every(f=>magnitude(f)===0),'long thinking never schedules repeated glances');
assert.equal(attentionRng.calls,8,'each phase begins only once even after a long hold');
assert.equal(magnitude(attention.frame(32016)),0,'finished thinking is already at rest');
const phasePause=new Gestures({random:()=>.5});
for(let t=0;t<1000;t+=16)assert.equal(magnitude(phasePause.frame(t,{thinking:true,paused:true})),0);
let phaseHeld;
for(let t=1008;t<1800;t+=16)phaseHeld=phasePause.frame(t,{thinking:true});
assert(Math.abs(phaseHeld.headX)>3,'a phase first observed while paused starts on resume');
for(let t=1808;t<2800;t+=16)assert.deepEqual(phasePause.frame(t,{paused:true}),phaseHeld,
  'paused phase changes keep the exact current pose');
const phaseResumed=phasePause.frame(2800,{listening:true});
assert(Math.abs(phaseResumed.headX-phaseHeld.headX)<.52,'resuming into a new phase blends the old pose');

// Microphone attention must take over an audio element retained in paused
// state. Its old semantic clip is consumed permanently; only normal audio
// pause without microphone input freezes the speech pose (tested below).
const microphone=session('轻吐槽');
microphone.run(45);
const beforeMic=microphone.value;
assert(Math.abs(beforeMic.headX)>4,'reproduce microphone input during a visible speech gesture');
const firstMic=microphone.step({listening:true,speaking:false,paused:true},16,0);
assert.equal(firstMic.name,'倾听');
assert.equal(microphone.g.segmentActive,false,'microphone retires stale audio gesture ownership');
assert(microphone.g.turns.has('reply'),'retired audio keeps its consumed turn record');
for(const key of head)assert(Math.abs(firstMic[key]-beforeMic[key])<.52,'microphone takeover blends the existing pose');
const micFrames=[];
for(let i=0;i<440;i++)micFrames.push(microphone.step({listening:true,speaking:false,paused:true},16,0));
assert(micFrames.some(f=>f.headY<-3.8),'listening nod advances despite the stale audio pause flag');
assert(micFrames.slice(300).every(f=>Math.abs(f.headY+.45)<.0001&&Math.abs(f.bodyY-.65)<.0001),
  'paused audio cannot repeat or stall the one listening nod');
assert.equal(microphone.rng.calls,8,'only the original speech gesture and one listening gesture were sampled');
const stoppedMic=microphone.step({listening:false,speaking:false,paused:true},16,0);
assert(stoppedMic.bodyY>0&&stoppedMic.bodyY<.65,'closing microphone releases its lean even while old audio is paused');
const micRecovery=microphone.run(180,{listening:false,speaking:false,paused:true});
assert.equal(magnitude(micRecovery.at(-1)),0);
assert(microphone.run(80).every(f=>magnitude(f)===0),'resuming the original audio cannot restore its retired gesture');
microphone.g.begin({turnId:'reply',segmentIndex:1,actions:['轻吐槽']});
assert(microphone.run(80).every(f=>magnitude(f)===0),'late begin callbacks cannot replay the retired speech action');
assert.equal(microphone.rng.calls,8);

// A speech action takes ownership smoothly from the listening hold. An empty
// speech action releases that hold without inventing an unrelated main action.
for(const actions of [[],['疑惑']]) {
  const g=new Gestures({random:()=>.5});
  let before;
  for(let t=0;t<=4000;t+=16)before=g.frame(t,{listening:true});
  g.begin({turnId:1,actions});
  let next=g.frame(4016,{speaking:true});
  for(const key of head)assert(Math.abs(next[key]-before[key])<.1);
  for(let t=4032;t<8000;t+=16)next=g.frame(t,{speaking:true,mediaTime:(t-4016)/1000});
  assert(magnitude(next)<.001,'speech finishes its only action and listening does not resume under it');
}

// Pause, decoder stalls, forward/backward seeks and hidden-tab gaps freeze the
// exact gesture pose. Resuming legal progress continues without catch-up.
const transport=session('轻吐槽');
transport.run(35);
const held=transport.value;
assert(Math.abs(held.headX)>2);
for(let i=0;i<80;i++)assert.deepEqual(transport.step({paused:true}),held,'pause freezes the entire action');
for(let i=0;i<80;i++)assert.deepEqual(transport.step({},16,0),held,'stalled media cannot advance the action');
assert.deepEqual(transport.step({},16,8),held,'forward seek cannot advance the action');
assert.deepEqual(transport.step({},16,-4),held,'backward seek cannot rewind the action');
assert.deepEqual(transport.step({},3600000,3600),held,'background resume cannot catch up');
const resumed=transport.step();
assert(Math.abs(resumed.headX-held.headX)<=32*.016);
assert.notDeepEqual(resumed,held,'normal progress resumes the same action');
assert.deepEqual(transport.g.frame(1,{speaking:true,mediaTime:transport.media+1}),resumed,'backwards wall clock freezes');

// Same-turn segments do not re-run a semantic action, including after cooldown
// has expired and after interruption. No delayed cue may survive cancellation.
for(const completed of [true,false]) {
  const s=session();s.run(40);
  const before=s.value;
  s.g.end({completed});
  const release=s.step({speaking:false});
  for(const key of head)assert(Math.abs(release[key]-before[key])<.52,'cancellation starts a smooth recovery');
  assert(magnitude(release)>1,'cancellation must not snap to rest');
  const recovery=s.run(500,{speaking:false});
  assert.equal(magnitude(recovery.at(-1)),0,'canceled or completed actions fully release');
  s.g.begin({turnId:'reply',segmentIndex:1,actions:['疑惑','认可']});
  assert(s.run(200).every(f=>magnitude(f)===0),'later segments cannot replay or change the consumed main action');
  assert.equal(s.rng.calls,4);
}
const multiple=new Gestures({random:()=>.5});
multiple.begin({turnId:1,actions:['不支持','疑惑','被夸']});
multiple.frame(0,{speaking:true});
assert.equal(multiple.frame(16,{speaking:true,mediaTime:.016}).name,'疑惑','only the first supported action is selected');
const emptyCanceled=session('未知动作');
emptyCanceled.run(40);emptyCanceled.g.end();
emptyCanceled.g.begin({turnId:'reply',segmentIndex:1,actions:['被夸']});
assert(emptyCanceled.run(100).every(f=>magnitude(f)===0),'late segments of a canceled reply cannot introduce a first action');

// A reconnect may restart the backend's turn IDs. Forget old deduplication and
// cooldowns without snapping the visible gesture to zero or replaying its tail.
const reconnect=new Gestures({random:()=>.5});
reconnect.begin({turnId:1,actions:['认可']});
let reconnectBefore;
for(let t=0;t<=640;t+=16)reconnectBefore=reconnect.frame(t,{speaking:true,mediaTime:t/1000});
assert(reconnectBefore.headY<-4);
const oldClock=reconnect.clock;
reconnect.end({completed:false});
reconnect.resetSession();
assert.equal(reconnect.clock,oldClock,'reconnection keeps the animation clock');
assert.equal(reconnect.turnId,null);
assert.equal(reconnect.segmentActive,false);
assert.equal(reconnect.lastMedia,null);
assert.equal(reconnect.clip,null);
assert.equal(reconnect.turns.size,0);
assert.equal(reconnect.cooldowns.size,0);
assert.deepEqual(reconnect.frame(640),reconnectBefore,'reset itself preserves the visible pose');
const reconnectRelease=reconnect.frame(656);
assert(Math.abs(reconnectRelease.headY-reconnectBefore.headY)<.52&&Math.abs(reconnectRelease.headY)>3,
  'the old session releases smoothly on the next frame');
let reconnectRest;
for(let t=672;t<=2560;t+=16)reconnectRest=reconnect.frame(t);
assert(magnitude(reconnectRest)<.0001&&!reconnectRest.active,'old session has no remaining scheduled action');
reconnect.begin({turnId:1,actions:['认可']});
const reconnectReplay=[];
for(let t=2576;t<=3536;t+=16)reconnectReplay.push(reconnect.frame(t,{speaking:true,mediaTime:(t-2576)/1000}));
assert(reconnectReplay.some(f=>f.headY<-4),'new session can reuse turn one and the same action before the old cooldown expires');
const reconnectPhase=new Gestures({random:()=>.5});
for(let t=0;t<=640;t+=16)reconnectPhase.frame(t,{listening:true});
reconnectPhase.resetSession();
assert.equal(reconnectPhase.phase,'','phase ownership belongs to the new session');
reconnectPhase.frame(656,{listening:true});
assert.equal(reconnectPhase.clip.name,'倾听','new session can re-enter the same attention phase');

// Repeating the same request in a new turn is suppressed for six seconds; a
// different semantic action is still allowed. Consumed suppressed turns stay so.
const cooldown=session();cooldown.run(45);cooldown.g.end();cooldown.run(140,{speaking:false});
cooldown.g.begin({turnId:'too soon',actions:['认可']});
assert(cooldown.run(100).every(f=>magnitude(f)===0),'same-action cooldown applies across replies');
cooldown.g.end();cooldown.run(400,{speaking:false});
cooldown.g.begin({turnId:'too soon',segmentIndex:1,actions:['认可']});
assert(cooldown.run(50).every(f=>magnitude(f)===0),'a suppressed reply never plays its action belatedly');
cooldown.g.end();
cooldown.g.begin({turnId:'fresh',actions:['认可']});
assert(cooldown.run(60).some(f=>Math.abs(f.headY)>4),'a fresh reply can use the action after cooldown');
cooldown.g.end();
cooldown.g.begin({turnId:'different',actions:['疑惑']});
assert(cooldown.run(60).some(f=>Math.abs(f.headZ)>4),'cooldowns do not block unrelated gestures');

// Each action exposes three stable visual variants, and a biased random source
// cannot repeat the exact same variant on consecutive eligible replies.
for(const name of Object.keys(Gestures.poses)) {
  const contours=[];
  for(const random of [()=>.05,()=>.4,()=>.9]) {
    const g=new Gestures({random});g.begin({turnId:1,actions:[name]});
    let f;
    for(let t=0;t<=800;t+=16)f=g.frame(t,{speaking:true,mediaTime:t/1000});
    contours.push(head.map(key=>f[key]).join(','));
  }
  assert.equal(new Set(contours).size,3,`${name} supplies three different contours`);
}
const variants=new Gestures({random:()=>.5});
variants.begin({turnId:1,actions:['认可']});
const firstVariant=variants.clip.pose;
variants.end();
for(let t=0;t<=7000;t+=16)variants.frame(t);
variants.begin({turnId:2,actions:['认可']});
assert.notEqual(variants.clip.pose,firstVariant,'a biased random source still changes consecutive variants');

// Independent controls and explicit native assets must never compete for the
// eyes/face. A native face retains a reduced head/body cue.
for(const options of [{intensity:0},{performance:0},{intensity:-2},{performance:-1}]) {
  const s=session('被夸',options);
  assert(s.run(200).every(f=>magnitude(f)===0&&!f.active));
}
for(const options of [{eyeIntensity:0},{faceIntensity:0},{explicitFace:true}]) {
  const s=session('被夸',options),frames=s.run(100);
  const suppressed=options.explicitFace ? [...eyes,...face] : options.eyeIntensity===0 ? eyes : face;
  assert(frames.every(f=>suppressed.every(key=>f[key]===0)),'disabled channels stay exactly zero');
  assert(frames.some(f=>Math.abs(f.headZ)>2),'independent facial controls retain head/body movement');
  if(options.faceIntensity===0)assert(frames.some(f=>Math.abs(f.gazeX)>.1),'face switch does not disable the independent gaze');
}
const override=session('被夸');override.run(50);
assert(override.value.smile>.05);
const native=override.step({explicitFace:true});
assert([...eyes,...face].every(key=>native[key]===0),'native face suppresses already visible competing layers immediately');
assert(head.some(key=>Math.abs(native[key])>2));
const off=override.step({intensity:0});
assert.equal(magnitude(off),0,'master zero also suppresses an existing gesture immediately');

// Continuous low-FPS rendering must follow the clip's actual time, rather than
// stretching every valid frame to a fixed 50ms animation step. Check the full
// gesture contour at matching media timestamps and the observed finish time.
function atRate(fps,name,until=4000,phase='',options={}) {
  const rng=seeded(),g=new Gestures({random:rng.random});
  if(!phase)g.begin({turnId:1,actions:[name]});
  let frame,started=false,finished=null,lastTime=0;
  const tick=t=>{
    frame=g.frame(t,{speaking:!phase,mediaTime:t/1000,listening:phase==='倾听',thinking:phase==='思考',...options});
    if(frame.active)started=true;
    else if(started&&finished===null)finished=t;
    lastTime=t;
  };
  for(let i=0;i*1000/fps<=until+1e-8;i++)tick(i*1000/fps);
  if(until-lastTime>1e-8)tick(until);
  return {frame,finished,clock:g.clock};
}
let lowRateHeadError=0;
for(const fps of [5,8,10,15]) {
  for(const name of Object.keys(Gestures.poses)) {
    for(const t of [300,600,1000,1400,1800,2500,3500]) {
      const expected=atRate(60,name,t).frame,actual=atRate(fps,name,t);
      assert(Math.abs(actual.clock-t/1000)<1e-8,'continuous valid frames keep real animation time');
      for(const key of keys) {
        const error=Math.abs(actual.frame[key]-expected[key]);
        if(head.includes(key))lowRateHeadError=Math.max(lowRateHeadError,error);
        assert(error<(key.startsWith('head')||key.startsWith('body')?.08:.006),
          `${fps}fps ${name} ${t}ms ${key} must follow the 60fps pose`);
      }
    }
    const expectedFinish=atRate(60,name).finished,actualFinish=atRate(fps,name).finished;
    assert(expectedFinish!==null&&actualFinish!==null,'actions finish within their natural duration at every rendering rate');
    assert(Math.abs(actualFinish-expectedFinish)<=1000/fps+1000/60,
      'observed completion differs by at most one rendered frame');
  }
  for(const phase of ['倾听','思考']) {
    for(const t of [600,1000,1800,3500]) {
      const expected=atRate(60,phase,t,phase).frame,actual=atRate(fps,phase,t,phase).frame;
      for(const key of keys)assert(Math.abs(actual[key]-expected[key])<
        (key.startsWith('head')||key.startsWith('body')?.08:.006),'attention phases use actual animation time at low FPS');
    }
  }
}
const gapGuard=session('疑惑');gapGuard.run(40);
const beforeGap=gapGuard.value;
assert.deepEqual(gapGuard.step({},251,.251),beforeGap,'a gap just above 250ms remains frozen');
assert.deepEqual(gapGuard.step({paused:true},125,.125),beforeGap,'valid low-FPS intervals do not defeat explicit pause');
const progressed=gapGuard.step({},125,.125);
assert.notDeepEqual(progressed,beforeGap,'legal low-FPS progress resumes after re-anchoring');
console.log(`MEASURE: 5/8/10/15fps versus 60fps, maximum matched-time head difference ${lowRateHeadError.toFixed(4)}°`);

// Extreme and invalid inputs cannot poison state; maximum sliders retain both
// parameter and velocity bounds across refresh rates. Invalid RNG is contained.
for(const frameMs of [1000/5,1000/8,1000/10,1000/15,1000/30,1000/60,1000/120]) {
  for(const name of Object.keys(Gestures.poses)) {
    const g=new Gestures({random:()=>Infinity});g.begin({turnId:1,actions:[name]});
    let previous;
    for(let t=0;t<4000;t+=frameMs) {
      const f=g.frame(t,{speaking:true,mediaTime:t/1000,intensity:999,performance:999,eyeIntensity:999,faceIntensity:999});
      bounded(f);
      if(previous)for(const key of head)assert(Math.abs(f[key]-previous[key])<=32*frameMs/1000+1e-9);
      previous=f;
    }
  }
}
for(const random of [()=>NaN,()=>{throw new Error('bad RNG');}]) {
  const g=new Gestures({random});g.begin({turnId:1,actions:['被夸']});
  for(let t=0;t<5000;t+=16)bounded(g.frame(t%160===0?NaN:t,{speaking:true,
    mediaTime:t%240===0?NaN:t/1000,intensity:NaN,performance:Infinity,eyeIntensity:NaN,faceIntensity:Infinity}));
}
console.log(`MEASURE: natural 1x gesture head peaks: ${measured.join(', ')}`);
console.log('PASS: six stable gestures, eye-first phases, media-clock guards, one action per reply, cooldown, cancellation, channel ownership and bounded output');
