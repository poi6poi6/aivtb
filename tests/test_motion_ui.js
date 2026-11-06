const assert=require('node:assert/strict');
const Motion=require('../brain/static/motion.js');
const motion=new Motion();
let previous, breathMin=1, breathMax=0;
for(let t=0;t<20000;t+=16){
  const f=motion.frame(t,{intensity:1.15,speaking:t>10000,attention:t>15000?1:0,gazeX:.7});
  for(const value of Object.values(f)) assert(Number.isFinite(value));
  assert(Math.abs(f.bodyX)<=3.84 && Math.abs(f.bodyZ)<=2.56);
  assert(f.breath>=0 && f.breath<=1);
  if(previous) assert(Math.abs(f.bodyY-previous.bodyY)<.1,'no jump when speaking/listening changes');
  if(t>2000 && t<10000){breathMin=Math.min(breathMin,f.breath);breathMax=Math.max(breathMax,f.breath);}
  previous=f;
}
assert(breathMax-breathMin>.7,'breathing should span most of the native parameter range');
const resumed=motion.frame(3600000,{intensity:1.15,speaking:true,attention:1,gazeX:.7});
assert(Math.abs(resumed.bodyY-previous.bodyY)<.15,'background-tab resume must not jump');
let gesture=0;
for(let t=0;t<1000;t+=16) gesture=motion.gesture('wave',1,t);
assert(gesture>.9 && gesture<1);
for(let t=1000;t<5000;t+=16) gesture=motion.gesture('wave',0,t);
assert(gesture<.001,'hand must return to rest');
for(let t=3600016;t<3610000;t+=16) previous=motion.frame(t,{intensity:0});
assert(Math.abs(previous.bodyY)<.001 && previous.breath<.001,'intensity zero fades new motion out');
console.log('PASS: visible breath range, bounded body motion, smooth state transitions and gesture recovery');
const still=new Motion(), glancing=new Motion();
for(let t=0;t<5000;t+=16){
  const a=still.frame(t,{gazeX:0,gazeY:0});
  const b=glancing.frame(t,{gazeX:Math.sin(t*.02)*.3,gazeY:Math.cos(t*.03)*.15});
  assert.equal(a.headX,b.headX,'small eye movements must not drive the head');
  assert.equal(a.headY,b.headY);
  assert.equal(a.bodyX,b.bodyX,'torso motion must be independent of gaze');
}
let last=0;
for(let t=5000;t<7500;t+=16){
  const f=glancing.frame(t,{gazeX:.8});
  assert(Math.abs(f.headX-last)<.04,'large gaze changes follow gradually');
  last=f.headX;
}
assert(last>.7 && last<1.2);
console.log('PASS: eye-only gaze zone and slow head follow without torso coupling');

// Measure ten settled seconds at the default global intensity, without accents.
// The baseline has identical breathing and gaze but disables speech head motion.
const axes=['headX','headY','headZ'];
const range=()=>Object.fromEntries(axes.map(axis=>[axis,{min:Infinity,max:-Infinity}]));
const record=(stats,frame)=>{
  for(const axis of axes){
    stats[axis].min=Math.min(stats[axis].min,frame[axis]);
    stats[axis].max=Math.max(stats[axis].max,frame[axis]);
  }
};
const span=(stats,axis)=>stats[axis].max-stats[axis].min;
const voiced=new Motion(), speechDisabled=new Motion(), quietSpeech=new Motion();
const visible=range(), speechOnly=range(), quietOnly=range();
let lastVoiced, maxSpeed=0;
for(let t=0;t<14000;t+=16){
  const args={intensity:.8,speaking:true,speechEnergy:.8};
  const f=voiced.frame(t,args);
  const b=speechDisabled.frame(t,{...args,headIntensity:0});
  const quiet=quietSpeech.frame(t,{intensity:.8,speaking:true});
  if(lastVoiced)for(const axis of axes){
    maxSpeed=Math.max(maxSpeed,Math.abs(f[axis]-lastVoiced[axis])/.016);
  }
  for(const axis of ['bodyX','bodyY','bodyZ','breath']){
    assert.equal(f[axis],b[axis],'speech head settings must not drive breathing or the torso');
    assert.equal(f[axis],quiet[axis],'energy must only change the additional head movement');
  }
  if(t>=4000){
    record(visible,f);
    record(speechOnly,Object.fromEntries(axes.map(axis=>[axis,f[axis]-b[axis]])));
    record(quietOnly,Object.fromEntries(axes.map(axis=>[axis,quiet[axis]-b[axis]])));
  }
  lastVoiced=f;
}
assert(span(visible,'headX')>=3,'steady speech must visibly turn the head without accent pulses');
assert(span(visible,'headZ')>=2,'steady speech must visibly tilt the head without accent pulses');
assert(span(speechOnly,'headY')>.6,'speech must include gentle vertical movement');
assert(maxSpeed<5,'steady speech head arcs must remain below five degrees per second');
assert(span(quietOnly,'headX')>.5&&span(quietOnly,'headX')<1.2,'quiet playback keeps only light baseline movement');
assert(span(quietOnly,'headX')<span(speechOnly,'headX')*.25,'quiet playback is substantially calmer than voiced playback');
console.log('MEASURE: intensity=0.8, headIntensity=1, speechEnergy=0.8, ten seconds, peak-to-peak '+
  axes.map(axis=>`${axis.slice(4)}=${span(visible,axis).toFixed(3)}deg`).join(', ')+
  `; max speed=${maxSpeed.toFixed(3)}deg/s`);

// Silence inside a playing clip converges to the small fallback movement.
let quietDistance;
for(let t=14000;t<23000;t+=16){
  const f=voiced.frame(t,{intensity:.8,speaking:true,speechEnergy:0});
  const quiet=quietSpeech.frame(t,{intensity:.8,speaking:true,speechEnergy:0});
  quietDistance=Math.max(...axes.map(axis=>Math.abs(f[axis]-quiet[axis])));
}
assert(quietDistance<.001,'long silence settles to quiet playback rather than continuing energetic motion');

// Stale audio inputs after playback stops must not keep the head animated.
const idleReference=new Motion();
let stoppedDistance;
for(let t=0;t<33000;t+=16){
  const b=idleReference.frame(t,{intensity:.8});
  if(t>=23000){
    const f=voiced.frame(t,{intensity:.8,speaking:false,speechEnergy:1,speechAccent:1});
    stoppedDistance=Math.max(...axes.map(axis=>Math.abs(f[axis]-b[axis])));
  }
}
assert(stoppedDistance<.001,'stopping speech smoothly returns every head axis to the idle baseline');

const baseline=new Motion(), accented=new Motion();
let nod=0, peakNod=0, lastNod=0, lastHead, pausedNod=0;
for(let t=0;t<7000;t+=16){
  const speaking=t<3400;
  // An accent still supplied after pause is intentionally ignored.
  const speechAccent=t>=3000?1:0;
  const a=baseline.frame(t,{speaking});
  const b=accented.frame(t,{speaking,speechAccent});
  nod=b.headY-a.headY;
  if(t>=3000&&t<3400)peakNod=Math.max(peakNod,-nod);
  if(t>=3400&&t<3416)pausedNod=nod;
  assert(Math.abs(nod-lastNod)<.12,'an accent must enter and return smoothly');
  if(lastHead!==undefined)assert(Math.abs(b.headY-lastHead)<.14,'continuous frames must not snap the head');
  assert(Math.abs(nod)<=.85,'accent is a bounded, light nod');
  assert.equal(a.bodyX,b.bodyX,'accents must not drive the torso');
  assert.equal(a.headX,b.headX,'accents must not disturb horizontal gaze');
  lastNod=nod;lastHead=b.headY;
}
assert(peakNod>.65,'a sustained accent has a visible but light response');
assert(pausedNod<-.5,'the accent returns gradually after speech stops');
assert(Math.abs(nod)<.001,'paused speech returns to the baseline despite stale accents');

const briefBase=new Motion(), briefAccent=new Motion();
let pulsePeak=0;
for(let t=0;t<6000;t+=16){
  const a=briefBase.frame(t,{speaking:true});
  const b=briefAccent.frame(t,{speaking:true,speechAccent:t>=3000&&t<3120?1:0});
  pulsePeak=Math.max(pulsePeak,a.headY-b.headY);
  nod=b.headY-a.headY;
}
assert(pulsePeak>.45&&pulsePeak<.7,'a short emphasis produces a noticeable smooth nod');
assert(Math.abs(nod)<.001,'the head returns while speech continues without another accent');

const idle=new Motion(), stale=new Motion();
for(let t=0;t<4000;t+=16){
  assert.deepEqual(stale.frame(t,{speaking:false,speechAccent:1,speechEnergy:1}),idle.frame(t),
    'idle motion must ignore incoming speech accents');
}
const off=new Motion();
for(let t=0;t<2000;t+=16)off.frame(t,{speaking:true,speechAccent:1,speechEnergy:1});
let faded;
for(let t=2000;t<14000;t+=16)faded=off.frame(t,{intensity:0,speaking:true,speechAccent:1,speechEnergy:1});
assert(Object.values(faded).every(value=>Math.abs(value)<.001),'zero intensity fades accents and existing motion out');

const headOff=new Motion(), headOffReference=new Motion();
let headOffDistance, lastHeadOff;
for(let t=0;t<16000;t+=16){
  const args={intensity:.8,speaking:true,speechEnergy:.8,speechAccent:t%1500<100?1:0};
  const f=headOff.frame(t,{...args,headIntensity:t<4000?1:0});
  const b=headOffReference.frame(t,{...args,headIntensity:0});
  headOffDistance=Math.max(...axes.map(axis=>Math.abs(f[axis]-b[axis])));
  if(lastHeadOff)for(const axis of axes){
    assert(Math.abs(f[axis]-lastHeadOff[axis])<.13,'disabling head motion must fade smoothly');
  }
  lastHeadOff=f;
}
assert(headOffDistance<.001,'headIntensity zero disables every added speech head action');

const varying=new Motion();
let lastVarying;
for(let t=0;t<16000;t+=16){
  const playing=t<11000;
  const f=varying.frame(t,{intensity:.8,speaking:playing,
    speechEnergy:t%1100<650?1:0,speechAccent:t%1700<80?1:0});
  if(lastVarying)for(const axis of axes){
    assert(Math.abs(f[axis]-lastVarying[axis])/.016<8,
      'changing volume, short accents and stopping must stay below eight degrees per second');
  }
  lastVarying=f;
}

const resumedAccent=new Motion();
let beforeResume;
for(let t=0;t<=3000;t+=16)beforeResume=resumedAccent.frame(t,{speaking:true});
const afterResume=resumedAccent.frame(3600000,{speaking:true,speechAccent:1,speechEnergy:1});
assert.deepEqual(afterResume,beforeResume,'background resume must not jump to an incoming accent');
const nextResume=resumedAccent.frame(3600016,{speaking:true,speechAccent:1,speechEnergy:1});
assert(Math.abs(nextResume.headY-afterResume.headY)<.12,'motion resumes smoothly on the next normal frame');
assert.deepEqual(resumedAccent.frame(1,{speaking:true,speechAccent:1,speechEnergy:1}),nextResume,
  'a backwards clock must not rewind or advance the pose');

const invalid=new Motion(), capped=new Motion(), excessive=new Motion();
for(let t=0;t<4000;t+=16){
  const f=invalid.frame(t%64===0?NaN:t,{
    intensity:NaN,speaking:true,speechAccent:Infinity,speechEnergy:NaN,headIntensity:Infinity,
    attention:Infinity,gazeX:NaN,gazeY:-Infinity,
  });
  assert(Object.values(f).every(Number.isFinite),'invalid numbers must never poison motion state');
  const e=excessive.frame(t,{intensity:999,speaking:true,speechAccent:999,speechEnergy:999,headIntensity:999,gazeX:999,gazeY:999,attention:999});
  assert.deepEqual(e,capped.frame(t,{intensity:1.6,speaking:true,speechAccent:1,speechEnergy:1,headIntensity:1.6,gazeX:1,gazeY:1,attention:1}),
    'speech inputs and independent head intensity are clamped to their public ranges');
  assert(Math.abs(e.headX)<13&&Math.abs(e.headY)<9&&Math.abs(e.headZ)<9,'even maximum settings keep head angles bounded');
}
const recovered=invalid.frame(4016,{speaking:true,speechAccent:.8,gazeX:.7});
assert(Object.values(recovered).every(Number.isFinite),'valid input still works after invalid frames');
const negative=new Motion(), zero=new Motion();
for(let t=0;t<4000;t+=16){
  assert.deepEqual(negative.frame(t,{speaking:true,speechEnergy:-1,speechAccent:-1,headIntensity:-1}),
    zero.frame(t,{speaking:true,speechEnergy:0,speechAccent:0,headIntensity:0}),
    'negative energy, accents and head intensity must clamp to zero');
}
console.log('PASS: visible energy-driven head motion, quiet fallback, independent intensity, accents, smooth recovery and background resume');

const performanceBaseline=new Motion(), explicitBaseline=new Motion(), boldMotion=new Motion();
const boldSpan=range(), baseSpan=range();
let bodyScaleError=0,previousBold,boldSpeed=0;
for(let t=0;t<14000;t+=16){
  const args={intensity:.8,speaking:true,speechEnergy:.8};
  const base=performanceBaseline.frame(t,args);
  assert.deepEqual(base,explicitBaseline.frame(t,{...args,performance:1}),'performance one preserves baseline motion exactly');
  const bold=boldMotion.frame(t,{...args,performance:1.8});
  if(t>=4000){record(baseSpan,base);record(boldSpan,bold);}
  bodyScaleError=Math.max(bodyScaleError,Math.abs(bold.bodyX-base.bodyX*1.8),Math.abs(bold.bodyY-base.bodyY*1.8));
  if(previousBold)for(const axis of axes)boldSpeed=Math.max(boldSpeed,Math.abs(bold[axis]-previousBold[axis])/.016);
  previousBold=bold;
}
for(const axis of axes){
  const ratio=span(boldSpan,axis)/span(baseSpan,axis);
  assert(ratio>2.5&&ratio<3,'bold speech head travel grows about 2.5 to 3 times across each axis');
}
assert(bodyScaleError<1e-10,'bold body movement scales independently by 1.8');
assert(boldSpeed<15,'bold speech remains a smooth head turn rather than a shake');
const bornOff=new Motion();
for(let t=0;t<3000;t+=16)assert(Object.values(bornOff.frame(t,{performance:0,speaking:true,speechEnergy:1,speechAccent:1})).every(v=>v===0),
  'performance zero starts with all added motion disabled');
let released;
for(let t=14000;t<24000;t+=16)released=boldMotion.frame(t,{intensity:.8,performance:0,speaking:true,speechEnergy:1});
assert(Object.values(released).every(v=>Math.abs(v)<.001),'global zero smoothly fades all current movement out');
const invalidPerformance=new Motion(), normalPerformance=new Motion(), cappedPerformance=new Motion(), excessivePerformance=new Motion();
let previousMaximum;
for(let t=0;t<20000;t+=16){
  const args={intensity:1.6,speaking:true,speechEnergy:t%1000<600?1:0,speechAccent:t%1500<100?1:0,
    gazeX:1,gazeY:-1,attention:1,headIntensity:1.6};
  assert.deepEqual(invalidPerformance.frame(t,{...args,performance:Infinity}),normalPerformance.frame(t,args),
    'invalid performance falls back to baseline');
  const max=cappedPerformance.frame(t,{...args,performance:3});
  assert.deepEqual(max,excessivePerformance.frame(t,{...args,performance:999}),'global performance clamps at three');
  assert(axes.every(axis=>Math.abs(max[axis])<30)&&['bodyX','bodyY','bodyZ'].every(axis=>Math.abs(max[axis])<10),
    'maximum settings stay inside the actual native head and body angle ranges');
  assert(max.breath>=0&&max.breath<=1,'boosted breathing stays inside its native parameter range');
  if(previousMaximum)for(const axis of axes)assert(Math.abs(max[axis]-previousMaximum[axis])<=48*.016+1e-10,
    'even the largest combined settings keep accents below the head velocity limit');
  previousMaximum=max;
}
const pausedBold=cappedPerformance.frame(3600000,{performance:3});
const unchangedBold=cappedPerformance.frame(3600000,{performance:0});
assert.deepEqual(pausedBold,unchangedBold,'background or same-time performance changes never jump the pose');
console.log('MEASURE: performance=1.8, head peak-to-peak '+axes.map(axis=>`${axis.slice(4)}=${span(boldSpan,axis).toFixed(3)}deg`).join(', ')+
  `; max speed=${boldSpeed.toFixed(3)}deg/s; body gain=1.8`);
console.log('PASS: bold motion gain, baseline compatibility, smooth disable and native parameter bounds');
