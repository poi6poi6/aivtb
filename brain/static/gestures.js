// One deliberate gesture per reply or attention phase. All values are additive:
// degrees for head/body, normalized offsets for gaze, lids, smile and brows.
class CompanionGestures {
  constructor({random=Math.random}={}) {
    this.random = typeof random === 'function' ? random : Math.random;
    this.last = null;
    this.clock = 0;
    this.lastMedia = null;
    this.segmentActive = false;
    this.turnId = null;
    this.anonymousTurn = 0;
    this.turns = new Map();
    this.cooldowns = new Map();
    this.variants = new Map();
    this.phase = '';
    this.clip = null;
    this.name = '';
    this.values = CompanionGestures.zero();
  }
  static bounded(value, low, high, fallback=0) {
    return Number.isFinite(value) ? Math.max(low, Math.min(high, value)) : fallback;
  }
  static zero() {
    return Object.fromEntries(Object.keys(CompanionGestures.limits).map(key=>[key,0]));
  }
  static smooth(value) {
    const x = CompanionGestures.bounded(value,0,1);
    return x*x*(3-2*x);
  }
  sample() {
    try { return CompanionGestures.bounded(this.random(),0,1,.5); }
    catch (_) { return .5; }
  }
  start(name, source) {
    // Cooldown uses visible animation time, not time spent in a background tab.
    if (this.clock-(this.cooldowns.get(name) ?? -Infinity)<6) return false;
    const poses = CompanionGestures.poses[name];
    let variant = Math.min(2,Math.floor(this.sample()*3));
    if (variant===this.variants.get(name)) variant=(variant+1)%3;
    this.variants.set(name,variant);
    this.cooldowns.set(name,this.clock);
    this.clip = {name,source,age:0,pose:poses[variant],
      side:this.sample()<.5 ? -1 : 1, amplitude:.92+this.sample()*.16,
      hold:.58+this.sample()*.35};
    this.name = name;
    return true;
  }
  begin({turnId,segmentIndex=0,actions=[],emotion='平静'}={}) {
    // Call only after an audio element has actually started. Segment zero is a
    // new anonymous reply when callers cannot supply a stable turn identifier.
    const index = CompanionGestures.bounded(segmentIndex,0,Number.MAX_SAFE_INTEGER);
    const key = turnId ?? (index===0 || this.turnId===null
      ? `anonymous:${++this.anonymousTurn}` : this.turnId);
    const sameTurn = key===this.turnId;
    this.turnId = key;
    this.segmentActive = true;
    this.lastMedia = null;
    this.phase = '';
    if (!sameTurn || this.clip?.source==='phase') this.clip=null;
    if (this.turns.has(key)) return;
    const name = Array.isArray(actions)
      ? actions.find(action=>Object.hasOwn(CompanionGestures.poses,action)) : undefined;
    const selected = name || (emotion==='吐槽' ? '轻吐槽' : '');
    if (!selected) return;
    // A suppressed gesture is consumed too: later segments must not replay it.
    this.turns.set(key,true);
    if (this.turns.size>128) this.turns.delete(this.turns.keys().next().value);
    this.start(selected,'speech');
  }
  end({completed=false}={}) {
    this.segmentActive = false;
    this.lastMedia = null;
    // Keep the visible pose for the release interpolation, never a queued clip.
    // Retain consumed turn IDs on completion AND interruption to reject late
    // segment callbacks from an interrupted reply.
    this.clip=null;
    if (!completed && this.turnId!==null) {
      this.turns.set(this.turnId,true);
      if (this.turns.size>128) this.turns.delete(this.turns.keys().next().value);
    }
  }
  resetSession() {
    // Socket reconnection may restart server turn IDs at one. Drop the old
    // session's deduplication, timers and queued clip, but keep the displayed
    // pose and animation clock so the next frame can release it smoothly.
    this.turns.clear();
    this.cooldowns.clear();
    this.turnId=null;
    this.anonymousTurn=0;
    this.phase='';
    this.segmentActive=false;
    this.lastMedia=null;
    this.clip=null;
  }
  poseAt(clip) {
    const smooth=CompanionGestures.smooth;
    const {age,pose,side,amplitude,hold,name,source}=clip;
    const out=CompanionGestures.zero();
    const envelope=(delay)=>{
      const t=age-delay;
      if(t<=0)return 0;
      if(t<.4)return smooth(t/.4);
      if(t<.4+hold)return 1;
      return 1-smooth((t-.4-hold)/.72);
    };
    const eye=envelope(0),head=envelope(.18),body=envelope(.28);
    for(const key of Object.keys(out)) {
      const weight=key.startsWith('head') ? head : key.startsWith('body') ? body : eye;
      const directional=['headX','headZ','bodyX','bodyZ','gazeX'].includes(key) ? side : 1;
      out[key]=(pose[key] || 0)*weight*amplitude*directional;
    }
    // A single nod has a distinct descent, short stop and return. There is no
    // repeating oscillator; sustained listening keeps only a slight lean.
    if(name==='倾听' && source==='phase') {
      const lean=smooth((age-.28)/.65);
      out.bodyY+=.65*lean;
      out.headY-=.45*lean;
    }
    out.weight=Math.max(eye,head,body,name==='倾听' && source==='phase' ? .15*smooth(age/.6) : 0);
    return out;
  }
  frame(now,{listening=false,thinking=false,speaking=false,paused=false,mediaTime=0,
    intensity=1,eyeIntensity=.75,faceIntensity=1,performance=1,explicitFace=false}={}) {
    const bound=CompanionGestures.bounded;
    const validTime=Number.isFinite(now) && (this.last===null || now>=this.last);
    const timestamp=validTime ? now : (this.last ?? 0);
    const elapsed=this.last===null ? 0 : Math.max(0,(timestamp-this.last)/1000);
    const dt=validTime && elapsed<=.25 ? elapsed : 0;
    this.last=timestamp;
    this.clock+=dt;
    // Opening the microphone can leave the old audio element paused in place.
    // Listening owns attention now: retire its speech action (and consume the
    // turn), rather than allowing stale playback ownership to block the nod.
    const microphoneListening=Boolean(listening && !speaking);
    if(microphoneListening && (this.segmentActive || this.clip?.source==='speech'))this.end();
    const media=Number.isFinite(mediaTime) && mediaTime>=0 ? mediaTime : null;
    const delta=media!==null && this.lastMedia!==null ? media-this.lastMedia : 0;
    // Re-anchor every frame, including pause, seek and invalid time. A seek is
    // not movement: legal clip progress is bounded by the current frame gap.
    const mediaDt=this.segmentActive && speaking && !paused && dt>0 && delta>0
      && delta<=Math.min(.25,elapsed*2+.025) ? delta : 0;
    this.lastMedia=media;
    const desiredPhase=microphoneListening ? '倾听' : this.segmentActive || speaking ? '' : thinking ? '思考' : '';
    // The audio pause flag cannot freeze live microphone attention. When the
    // mic closes, release its pose even if the old audio remains paused.
    const phase=paused && !microphoneListening ? (this.phase==='倾听' ? '' : this.phase) : desiredPhase;
    if(phase!==this.phase) {
      this.phase=phase;
      if(this.clip?.source==='phase')this.clip=null;
      if(phase)this.start(phase,'phase');
    }
    let step=dt;
    if(this.clip) {
      step=this.clip.source==='speech' ? mediaDt : paused && !microphoneListening ? 0 : dt;
    }
    const master=bound(intensity,0,1.6,1)*bound(performance,0,3,1);
    const eyes=bound(eyeIntensity,0,1,.75),face=bound(faceIntensity,0,1.6,1);
    // Low rendering rates still consume the full valid time interval. Integrate
    // in at most 1/60s slices so the envelope and speed limit follow the same
    // contour as 60fps; never replay a >250ms gap discarded above. The epsilon
    // prevents floating-point noise from splitting an ordinary 60fps frame.
    const slices=Math.max(1,Math.ceil(step*60-1e-9)),slice=step/slices;
    for(let i=0;i<slices;i++) {
      if(this.clip)this.clip.age+=slice;
      const target=this.clip ? this.poseAt(this.clip) : CompanionGestures.zero();
      for(const key of Object.keys(this.values)) {
        const facial=['gazeX','gazeY','eyeOpen','smile','brow'].includes(key);
        const gain=key==='weight' ? Math.min(1,master)
          : key.startsWith('gaze') ? master*eyes
          : key==='eyeOpen' ? master*eyes*face
          : key==='smile' || key==='brow' ? master*face
          : master*(explicitFace ? .7 : 1);
        const suppressed=master===0 || (facial && explicitFace)
          || (key.startsWith('gaze') && eyes===0)
          || (key==='eyeOpen' && (eyes===0 || face===0))
          || ((key==='smile' || key==='brow') && face===0);
        const limit=CompanionGestures.limits[key];
        const desired=bound(target[key]*gain,-limit,limit);
        const tau=key.startsWith('gaze') ? .065 : key.startsWith('body') ? .18 : .12;
        let change=(desired-this.values[key])*(1-Math.exp(-slice/tau));
        const speed=key.startsWith('head') ? 32 : key.startsWith('body') ? 10 : key==='weight' ? 8 : 3;
        change=bound(change,-speed*slice,speed*slice);
        this.values[key]=suppressed ? 0 : bound(this.values[key]+change,-limit,limit);
        if(desired===0 && Math.abs(this.values[key])<.00001)this.values[key]=0;
      }
    }
    const active=Object.values(this.values).some(value=>Math.abs(value)>.0001);
    if(!active && !this.clip)this.name='';
    return {...this.values,active,name:active ? this.name : ''};
  }
}

// Bounds apply after all gain controls. The host still clamps the final sum to
// each model's native range. At 1x these gestures peak around 4–7 head degrees.
CompanionGestures.limits={headX:12,headY:10,headZ:11,bodyX:3,bodyY:3,bodyZ:3,
  gazeX:.55,gazeY:.35,eyeOpen:.12,smile:.16,brow:.14,weight:1};
// Three stable contours per action; direction, scale and dwell are sampled
// once on entry. Each contour has an eye lead, a held pose and a slow return.
CompanionGestures.poses={
  '倾听':[
    {headY:-4.4,headZ:1.2,bodyY:.7,gazeY:-.1,eyeOpen:-.015,smile:.025},
    {headY:-4.8,headX:1.2,bodyY:.9,gazeY:-.12,eyeOpen:-.025,brow:.025},
    {headY:-4.1,headZ:2,bodyY:.8,gazeY:-.08,smile:.035},
  ],
  '思考':[
    {headX:4.4,headY:1.5,headZ:2,bodyZ:.35,gazeX:.31,gazeY:.2,eyeOpen:-.035,brow:.04},
    {headX:5.1,headY:1,headZ:-1.2,bodyX:.45,gazeX:.35,gazeY:.17,eyeOpen:-.025,brow:.035},
    {headX:3.8,headY:2.3,headZ:2.4,bodyZ:.4,gazeX:.28,gazeY:.23,brow:.05},
  ],
  '认可':[
    {headY:-5.6,headZ:.8,bodyY:.7,gazeY:-.13,eyeOpen:-.035,smile:.07,brow:.02},
    {headY:-6.1,headX:1.3,bodyY:.85,gazeY:-.15,eyeOpen:-.025,smile:.055},
    {headY:-5.2,headZ:1.8,bodyY:.6,gazeY:-.1,eyeOpen:-.045,smile:.08},
  ],
  '疑惑':[
    {headX:1.8,headZ:6.2,bodyZ:.75,gazeX:.18,gazeY:.04,eyeOpen:.035,brow:.09},
    {headX:2.5,headZ:5.6,headY:.6,bodyZ:.6,gazeX:.22,gazeY:.07,brow:.1},
    {headX:1.2,headZ:6.7,headY:-.5,bodyZ:.8,gazeX:.15,eyeOpen:.045,brow:.07},
  ],
  '轻吐槽':[
    {headX:6.2,headY:1,headZ:-1.8,bodyX:.55,gazeX:.37,gazeY:.04,eyeOpen:-.07,brow:-.035},
    {headX:5.7,headY:.6,headZ:2.5,bodyZ:.5,gazeX:.32,gazeY:.08,eyeOpen:-.06,smile:.025},
    {headX:6.7,headZ:-1,bodyX:.65,gazeX:.4,gazeY:.02,eyeOpen:-.08,brow:-.025},
  ],
  '被夸':[
    {headX:3.5,headY:-2.4,headZ:4.9,bodyZ:.55,gazeX:.25,gazeY:-.16,eyeOpen:-.055,smile:.11},
    {headX:4.2,headY:-2.8,headZ:3.8,bodyX:.4,gazeX:.29,gazeY:-.18,eyeOpen:-.045,smile:.1,brow:.025},
    {headX:2.8,headY:-2,headZ:5.4,bodyZ:.65,gazeX:.22,gazeY:-.14,eyeOpen:-.065,smile:.12},
  ],
};
if(typeof module!=='undefined')module.exports=CompanionGestures;
if(typeof window!=='undefined')window.CompanionGestures=CompanionGestures;
