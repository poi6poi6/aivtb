const assert=require('node:assert/strict');
const Gaze=require('../brain/static/gaze.js');
function seeded(seed=42) {
  let calls=0;
  const random=()=>{calls++;seed=(Math.imul(seed,1664525)+1013904223)>>>0;return seed/4294967296;};
  return {random,get calls(){return calls;}};
}
function sample(options={},duration=30000,seed=42,step=16) {
  const rng=seeded(seed),gaze=new Gaze({random:rng.random}),frames=[];
  for(let t=0;t<duration;t+=step)frames.push({t,...gaze.frame(t,typeof options==='function'?options(t):options),pose:gaze.pose});
  return {gaze,frames,rng};
}
const extent=(frames,key)=>Math.max(...frames.map(f=>f[key]))-Math.min(...frames.map(f=>f[key]));
const peak=(frames,key)=>Math.max(...frames.map(f=>Math.abs(f[key])));
const idle=sample({},30000);
assert(extent(idle.frames,'x')>.025&&extent(idle.frames,'x')<.09);
assert(extent(idle.frames,'y')>.015&&extent(idle.frames,'y')<.06);
assert(idle.rng.calls<100,'randomness is sampled per fixation rather than every frame');
assert(idle.frames.every(f=>f.headX===0&&f.headY===0&&!f.blinkRequest));
let dwell=0,longestDwell=0;
for(let i=1;i<idle.frames.length;i++) {
  const a=idle.frames[i-1],b=idle.frames[i];
  dwell=Math.hypot(b.x-a.x,b.y-a.y)<.0001?dwell+16:0;
  longestDwell=Math.max(longestDwell,dwell);
}
assert(longestDwell>=600,'fixations include a stable dwell');
for(const random of [seeded(42).random,seeded(7).random,()=>0,()=>1]) {
  const gaze=new Gaze({random}),frames=[];
  for(let t=0;t<10000;t+=16)frames.push({...gaze.frame(t,{speaking:true}),pose:gaze.pose});
  assert(peak(frames,'x')<.03&&peak(frames,'y')<.02,'ordinary speech keeps small camera fixations');
  assert(frames.every(f=>f.pose==='camera'&&f.headX===0&&f.headY===0&&!f.blinkRequest),
    'short speech cannot alternate side looks or schedule a head turn');
}
const speech=sample({speaking:true},45000);
assert(speech.frames.every(f=>f.pose==='camera'&&!f.blinkRequest&&f.headX===0&&f.headY===0),
  'long speech has no periodic large looks either');
assert(speech.rng.calls<130,'ordinary speech does not add a second random scan scheduler');

// A reflect cue moves the eyes first, then follows gently with the head.
const reflecting=sample(t=>({speaking:true,focus:t<1000?'engage':'reflect'}),12000);
const firstAside=reflecting.frames.find(f=>f.pose==='aside');
assert(firstAside&&firstAside.t>=1000&&firstAside.t<1030);
const early=reflecting.frames.filter(f=>f.t>=firstAside.t&&f.t<firstAside.t+192);
assert(early.every(f=>Math.abs(f.headX)<.001),'head waits roughly 200ms for the eyes');
const firstHead=reflecting.frames.find(f=>f.t>=firstAside.t&&Math.abs(f.headX)>.01);
assert(firstHead&&firstHead.t-firstAside.t>=192&&Math.abs(firstHead.x)>.2);
assert(peak(reflecting.frames,'headX')>.35&&peak(reflecting.frames,'headX')<.5,
  'reflect crosses the existing head-follow dead zone with only a small target');
assert(peak(reflecting.frames,'x')>.4&&peak(reflecting.frames,'x')<.62);
let starts=0,prior='camera';
for(const f of reflecting.frames){if(f.pose==='aside'&&prior!=='aside')starts++;prior=f.pose;}
assert.equal(starts,1,'repeating reflect keeps its direction and does not restart side looks');
assert(reflecting.frames.filter(f=>f.t>3200).every(f=>f.pose==='camera'),
  'a stale reflect cue returns to camera within a short bounded interval');
assert(reflecting.frames.filter(f=>f.blinkRequest).length<=2);

// Focus changes within the same speaking mode, without forced side alternation.
const changing=new Gaze({random:()=>0});let now=0;
for(;now<700;now+=16)changing.frame(now,{speaking:true,focus:'engage'});
for(;now<1400;now+=16)changing.frame(now,{speaking:true,focus:'reflect'});
const fromX=changing.x;
assert(fromX<-.4&&changing.pose==='aside');
const atReturn=changing.frame(now,{speaking:true,focus:'engage'});now+=16;
assert.equal(changing.pose,'camera','engage updates the same-mode target immediately');
assert(Math.abs(atReturn.x)<Math.abs(fromX));
for(;now<2600;now+=16)changing.frame(now,{speaking:true,focus:'engage'});
assert(Math.abs(changing.x)<.04&&Math.abs(changing.headX)<.01);
changing.frame(now,{speaking:true,focus:'reflect'});now+=16;
assert.equal(changing.pose,'aside','a later semantic reflect can be performed again');
assert(changing.targetX<0,'directions are not forced to alternate');

// Question eye contact continues into the silent wait for an answer.
const question=sample(t=>({speaking:t<2200,focus:t<1000?'engage':t<1700?'reflect':'question'}),11000);
assert(question.frames.some(f=>f.pose==='aside'));
const waiting=question.frames.filter(f=>f.t>3500);
assert(waiting.every(f=>f.pose==='camera'&&Math.abs(f.x)<.02&&Math.abs(f.y)<.015&&!f.blinkRequest));
assert(peak(waiting,'headX')<.002,'question waiting does not retain the aside head turn');
const listening=sample({speaking:true,listening:true,focus:'reflect'},10000);
assert(listening.frames.every(f=>f.pose==='camera'&&f.headX===0&&f.headY===0&&!f.blinkRequest));
const thinking=sample({thinking:true,focus:'question'},30000);
assert(thinking.frames.some(f=>f.y>.15&&Math.abs(f.x)>.1),'thinking attention overrides semantic focus');
assert(thinking.frames.filter(f=>f.y>.15).length/thinking.frames.length>.15);
assert(thinking.frames.some(f=>f.t>5000&&Math.abs(f.y)<.035),'thinking also returns to camera');

function duringSide(seed=42) {
  const rng=seeded(seed),gaze=new Gaze({random:rng.random});
  for(let t=0;t<3000;t+=16) {
    const f=gaze.frame(t,{speaking:true,focus:'reflect'});
    if(gaze.pose==='aside'&&Math.abs(f.x)>.4)return {gaze,t,frame:f};
  }
  throw new Error('deterministic reflect did not reach its side target');
}
for(const options of [
  {speaking:true,paused:true,focus:'reflect'}, {speaking:false,focus:'engage'},
  {speaking:true,explicitFace:true,focus:'reflect'}, {speaking:true,listening:true,focus:'reflect'},
  {speaking:true,intensity:0,focus:'reflect'}
]) {
  const state=duringSide();let last=state.frame,final;
  for(let t=state.t+16;t<state.t+7000;t+=16) {
    final=state.gaze.frame(t,options);
    assert(Math.abs(final.x-last.x)<.1,'ownership transitions return smoothly');
    if(options.focus==='reflect')assert(!final.blinkRequest);
    last=final;
  }
  assert(Math.abs(final.x)<.05&&Math.abs(final.y)<.04);
  assert(Math.abs(final.headX)<.001&&Math.abs(final.headY)<.001);
  if(options.intensity===0)assert(Math.abs(final.x)<1e-9&&Math.abs(final.y)<1e-9);
}
const explicit=sample({speaking:true,explicitFace:true,focus:'reflect'},30000);
assert(extent(explicit.frames,'x')>.005&&extent(explicit.frames,'x')<.03);
assert(explicit.frames.every(f=>f.pose==='camera'&&!f.blinkRequest&&f.headX===0&&f.headY===0));
const owned=new Gaze({random:seeded(5).random});
for(let t=0;t<2000;t+=16)owned.frame(t,{speaking:true,explicitFace:true,focus:'reflect'});
for(let t=2000;t<5000;t+=16) {
  const f=owned.frame(t,{speaking:true,focus:'reflect'});
  assert(owned.pose==='camera'&&!f.blinkRequest,'releasing an explicit face must not replay stale reflect');
}
const reduced=sample({speaking:true,intensity:.1,focus:'reflect'},10000);
assert(reduced.frames.every(f=>Math.abs(f.x)<.09&&Math.abs(f.y)<.05&&!f.blinkRequest));

const closed=duringSide();let shut;
for(let t=closed.t+16;t<closed.t+5000;t+=16) {
  shut=closed.gaze.frame(t,{speaking:true,focus:'reflect',eyeOpen:0});
  assert.equal(shut.x,closed.frame.x,'closed eyes hold pupils under the lid');
  assert.equal(shut.y,closed.frame.y);assert(!shut.blinkRequest);
}
const opened=closed.gaze.frame(closed.t+5000,{speaking:true,focus:'reflect',eyeOpen:1});
assert(Math.abs(opened.x-shut.x)<.13);
const suspended=duringSide();
const after=suspended.gaze.frame(3600000,{speaking:true,focus:'reflect'});
assert.deepEqual(after,{...suspended.frame,blinkRequest:false},'background resume never fast-forwards');
const backwards=suspended.gaze.frame(1,{speaking:true,focus:'reflect'});
assert.deepEqual(backwards,after,'backwards timestamps do not advance or rewind gaze');
const resumed=suspended.gaze.frame(3600016,{speaking:true,focus:'reflect'});
assert(Math.abs(resumed.x-after.x)<.13);
const bad=new Gaze({random:()=>NaN});
for(let t=0;t<30000;t+=16) {
  const f=bad.frame(t%160===0?Infinity:t,{speaking:true,focus:t%8000<4000?'reflect':'question',intensity:t%64===0?NaN:999,eyeOpen:NaN});
  for(const key of ['x','y','headX','headY'])assert(Number.isFinite(f[key]));
  assert(Math.abs(f.x)<=.88&&Math.abs(f.y)<=.5&&Math.abs(f.headX)<=.65&&Math.abs(f.headY)<=.3);
}
assert.deepEqual(sample({speaking:true,focus:'reflect'},5000,7).frames,
  sample({speaking:true,focus:'reflect'},5000,7).frames);
for(const frameMs of [1000/10,1000/30,1000/60,1000/120]) {
  for(const seed of [1,17,91]) {
    const trial=sample(t=>({speaking:true,focus:t<1000?'engage':t<6000?'reflect':'question'}),10000,seed,frameMs);
    let previous,previousBlink=false,sideStarts=0,lastPose='camera';
    for(const f of trial.frames) {
      if(previous)assert(Math.hypot(f.x-previous.x,f.y-previous.y)<=7.5*frameMs/1000+1e-10);
      assert(!(previousBlink&&f.blinkRequest),'blink requests remain discrete across display rates');
      if(f.pose==='aside'&&lastPose!=='aside')sideStarts++;
      previous=f;previousBlink=f.blinkRequest;lastPose=f.pose;
    }
    assert.equal(sideStarts,1,'low/high display rates cannot retrigger a held semantic focus');
    assert(trial.rng.calls<60,'refresh rate does not drive random sampling');
  }
}
const baseline=sample({speaking:true,performance:1},5000);
assert.deepEqual(baseline.frames,sample({speaking:true},5000).frames);
const bold=sample({speaking:true,performance:1.8},15000);
assert(bold.frames.every(f=>Math.abs(f.x)<.05&&Math.abs(f.y)<.035&&f.pose==='camera'&&f.headX===0&&!f.blinkRequest),
  'bold performance cannot turn camera fixation back into a pendulum');
assert(extent(bold.frames,'x')>extent(sample({speaking:true},15000).frames,'x')*1.5);
for(const options of [{intensity:-1},{performance:0},{performance:-1},{performance:1.8,intensity:0}]) {
  assert(sample({speaking:true,focus:'reflect',...options},10000).frames.every(f=>
    f.x===0&&f.y===0&&f.headX===0&&f.headY===0&&!f.blinkRequest));
}
for(const guard of [{paused:true},{explicitFace:true},{listening:true},{eyeOpen:0}]) {
  assert(sample({speaking:true,focus:'reflect',performance:1.8,...guard},10000).frames.every(f=>
    Math.abs(f.x)<.05&&Math.abs(f.y)<.04&&!f.blinkRequest&&f.headX===0&&f.headY===0));
}
const globalOff=duringSide();let faded;
for(let t=globalOff.t+16;t<globalOff.t+5000;t+=16) {
  faded=globalOff.gaze.frame(t,{speaking:true,focus:'reflect',performance:0});
  assert(!faded.blinkRequest&&faded.headX===0&&faded.headY===0);
}
assert(Math.abs(faded.x)<1e-8&&Math.abs(faded.y)<1e-8);
assert.deepEqual(sample({speaking:true,performance:NaN},5000).frames,baseline.frames);
const maximum=sample({speaking:true,focus:'reflect',intensity:1.6,performance:3},16000);
assert.deepEqual(maximum.frames,sample({speaking:true,focus:'reflect',intensity:1.6,performance:999},16000).frames);
assert(maximum.frames.every(f=>Math.abs(f.x)<=.98&&Math.abs(f.y)<=.8&&Math.abs(f.headX)<=.65&&Math.abs(f.headY)<=.3));
console.log('MEASURE: ordinary 45s gaze X range',extent(speech.frames,'x').toFixed(3),'reflection peak',peak(reflecting.frames,'x').toFixed(3),'one deliberate side look');
console.log('PASS: camera attention, semantic reflect/engage/question, attention ownership, finite focus lifetime, delayed head follow, control and time guards');
