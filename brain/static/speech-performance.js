// Sparse, audio-clocked performance accents. This layer never drives the jaw:
// one shared cue coordinates the eyes, eyebrows, head and upper body.
class CompanionSpeechPerformance {
  constructor({random=Math.random}={}) {
    this.random=typeof random==='function' ? random : Math.random;
    this.last=null;
    this.lastMedia=null;
    this.clock=0;
    this.turnId=null;
    this.segmentIndex=null;
    this.anonymousTurn=0;
    this.cancelled=new Set();
    this.segmentActive=false;
    this.emotion='平静';
    this.clip=null;
    this.lastVariant=-1;
    this.lastEvent=-Infinity;
    this.nextBeat=Infinity;
    this.voiceAge=0;
    this.quietAge=0;
    this.voiced=false;
    this.accentArmed=true;
    this.cue='';
    this.semanticIds=new Set();
    this.lastSemanticEvent=-Infinity;
    this.values=CompanionSpeechPerformance.zero();
  }
  static bound(value,low,high,fallback=0) {
    return Number.isFinite(value) ? Math.max(low,Math.min(high,value)) : fallback;
  }
  static zero() {
    return Object.fromEntries(Object.keys(CompanionSpeechPerformance.limits).map(key=>[key,0]));
  }
  static smooth(value) {
    const x=CompanionSpeechPerformance.bound(value,0,1);
    return x*x*(3-2*x);
  }
  sample() {
    try { return CompanionSpeechPerformance.bound(this.random(),0,1,.5); }
    catch (_) { return .5; }
  }
  begin({turnId,segmentIndex=0,emotion='平静'}={}) {
    const index=CompanionSpeechPerformance.bound(segmentIndex,0,Number.MAX_SAFE_INTEGER);
    const key=turnId ?? (index===0 || this.turnId===null
      ? `anonymous:${++this.anonymousTurn}` : this.turnId);
    // A delayed playing callback must not resurrect an interrupted response.
    if(this.cancelled.has(key))return false;
    if(key===this.turnId && index===this.segmentIndex && this.segmentActive)return false;
    const sameTurn=key===this.turnId;
    this.turnId=key;
    this.segmentIndex=index;
    this.segmentActive=true;
    this.semanticIds.clear();
    this.lastMedia=null;
    this.clip=null;
    this.voiceAge=0;
    this.quietAge=0;
    this.voiced=false;
    this.accentArmed=true;
    const mapped=CompanionSpeechPerformance.aliases[emotion] || emotion;
    this.emotion=Object.hasOwn(CompanionSpeechPerformance.profiles,mapped) ? mapped : '平静';
    if(!sameTurn) {
      this.lastEvent=-Infinity;
      this.nextBeat=Infinity;
      this.lastSemanticEvent=-Infinity;
    }
    return true;
  }
  end({completed=false}={}) {
    this.segmentActive=false;
    this.lastMedia=null;
    this.clip=null;
    this.voiced=false;
    this.voiceAge=0;
    this.quietAge=0;
    this.semanticIds.clear();
    if(!completed && this.turnId!==null) {
      this.cancelled.add(this.turnId);
      if(this.cancelled.size>128)this.cancelled.delete(this.cancelled.values().next().value);
    }
    // Keep the visible values: frame() releases them on the wall clock.
  }
  resetSession() {
    this.end({completed:true});
    this.cancelled.clear();
    this.turnId=null;
    this.segmentIndex=null;
    this.anonymousTurn=0;
    this.lastEvent=-Infinity;
    this.nextBeat=Infinity;
    this.lastSemanticEvent=-Infinity;
  }
  start(cue,semanticKind=null) {
    // Randomness belongs to an event, never a rendered frame or a syllable.
    const initial=Math.min(2,Math.floor(this.sample()*3));
    const variant=this.lastVariant<0 ? initial : (this.lastVariant+1)%3;
    this.lastVariant=variant;
    const side=this.sample()<.5 ? -1 : 1;
    const amplitude=.94+this.sample()*.12;
    const duration=.94+this.sample()*.12;
    this.nextBeat=this.clock+2.5+this.sample()*1.5;
    this.lastEvent=this.clock;
    const profile=CompanionSpeechPerformance.profiles[this.emotion];
    const pose=semanticKind ? this.semanticPose(semanticKind,profile,variant)
      : {...profile.face,...profile.poses[variant]};
    for(const key of ['headX','headZ','bodyX','bodyZ','gazeX'])pose[key]=(pose[key] || 0)*side;
    // A side glance and its asymmetric eyebrow must face the same direction.
    if(side<0)[pose.browL,pose.browR]=[pose.browR,pose.browL];
    this.clip={age:0,pose,variant,amplitude,duration,semanticKind,returnAge:null,returnFrom:null};
    if(semanticKind)this.lastSemanticEvent=this.clock;
    this.cue=cue;
  }
  semanticPose(kind,profile,variant) {
    // Meaning picks the axis, not a random generic speaking pose. Small event
    // variants keep recurring phrases from using an identical frozen shape.
    const face=profile.face;
    const warm=['开心','得意'].includes(this.emotion) ? face.smile*.25 : 0;
    if(kind==='reflect')return {
      headX:1.1+variant*.25,headY:.25,headZ:2.8+variant*.25,
      bodyX:.1,bodyZ:.25,eyeOpen:face.eyeOpen*.4-.02,smile:warm,
      browL:face.browL*.35+.025,browR:face.browR*.35-.01,
    };
    if(kind==='emphasis')return {
      headX:.2,headY:-3.5-variant*.2,headZ:.2+variant*.15,
      bodyY:.45,eyeOpen:face.eyeOpen*.35,smile:warm,
      browL:face.browL*.35+.035,browR:face.browR*.35+.03,
    };
    // A question raises one eyebrow and tilts; it never borrows the downward
    // headY contour that would look like agreeing with the answer in advance.
    return {
      headX:.7,headY:.45,headZ:3.2+variant*.3,bodyY:-.1,bodyZ:.3,
      eyeOpen:.025+face.eyeOpen*.2,smile:0,
      browL:.075+face.browL*.2,browR:.055+face.browR*.2,
    };
  }
  consumeSemanticCue(cue) {
    if(!cue || !Object.hasOwn(CompanionSpeechPerformance.semanticKinds,cue.kind))return null;
    const id=cue.id;
    if(!((typeof id==='string' && id.length>0) || (typeof id==='number' && Number.isFinite(id))))return null;
    const key=`${typeof id}:${id}`;
    if(this.semanticIds.has(key))return null;
    this.semanticIds.add(key);
    return cue.kind;
  }
  poseAt(clip) {
    const out=CompanionSpeechPerformance.zero();
    const smooth=CompanionSpeechPerformance.smooth;
    if(clip.returnAge!==null) {
      for(const key of Object.keys(out)) {
        const length=key.startsWith('head') ? .48 : key.startsWith('body') ? .62 : .28;
        out[key]=clip.returnFrom[key]*(1-smooth(clip.returnAge/length));
      }
      return out;
    }
    const envelope=(delay,attack,hold,release)=>{
      const t=clip.age/clip.duration-delay;
      if(t<=0)return 0;
      if(t<attack)return smooth(t/attack);
      if(t<attack+hold)return 1;
      return 1-smooth((t-attack-hold)/release);
    };
    const eye=envelope(0,.12,.2,.68);
    const head=envelope(.1,.2,.2,.72);
    const body=envelope(.19,.25,.2,.76);
    for(const key of Object.keys(out)) {
      const weight=key.startsWith('head') ? head : key.startsWith('body') ? body : eye;
      out[key]=(clip.pose[key] || 0)*weight*clip.amplitude;
    }
    out.weight=Math.max(eye,head,body);
    return out;
  }
  advance(dt,energy,accent,semanticMode=false,semanticKind=null) {
    this.clock+=dt;
    // Hysteresis and a short attack keep noise and single-frame peaks from
    // looking like a new phrase. Brief syllabic gaps do not re-arm the onset.
    const audible=energy>(this.voiced ? .035 : .065);
    let onset=false;
    if(audible) {
      this.voiceAge+=dt;
      this.quietAge=0;
      if(!this.voiced && this.voiceAge>=.075) {
        this.voiced=true;
        onset=true;
      }
    } else {
      this.quietAge+=dt;
      if(this.quietAge>=.18) {
        this.voiceAge=0;
        this.voiced=false;
      }
    }
    const edge=accent>=.26 && this.accentArmed;
    if(accent>=.26)this.accentArmed=false;
    else if(accent<=.12)this.accentArmed=true;
    const ready=!this.clip && this.clock-this.lastEvent>=1.5;
    if(semanticMode && semanticKind && audible && this.clock-this.lastSemanticEvent>=1.5
        && (!this.clip || !this.clip.semanticKind))this.start(semanticKind,semanticKind);
    if(!semanticMode && audible && this.voiced && ready) {
      if(onset)this.start('onset');
      else if(edge)this.start('accent');
      else if(this.clock>=this.nextBeat && this.voiceAge>=.4)this.start('phrase');
    }
    if(this.clip) {
      if(this.quietAge>=.18 && this.clip.returnAge===null) {
        this.clip.returnFrom=this.poseAt(this.clip);
        this.clip.returnAge=0;
        this.cue='return';
      }
      if(this.clip.returnAge!==null)this.clip.returnAge+=dt;
      else this.clip.age+=dt;
      if((this.clip.returnAge!==null && this.clip.returnAge>=.62)
        || this.clip.age>=1.4*this.clip.duration)this.clip=null;
    }
    return this.clip ? this.poseAt(this.clip) : CompanionSpeechPerformance.zero();
  }
  frame(now,{speaking=false,paused=false,listening=false,mediaTime=0,energy=0,accent=0,
    intensity=1,eyeIntensity=.75,faceIntensity=.8,performance=1,explicitFace=false,
    semanticMode=false,semanticCue=null}={}) {
    // Microphone ownership supersedes a paused audio item. Retire its cue and
    // reject a late resumed segment rather than freezing an old talking pose.
    if(listening && !speaking && this.segmentActive)this.end({completed:false});
    const bound=CompanionSpeechPerformance.bound;
    const validTime=Number.isFinite(now) && (this.last===null || now>=this.last);
    const timestamp=validTime ? now : (this.last ?? 0);
    const elapsed=this.last===null ? 0 : (timestamp-this.last)/1000;
    const wallDt=validTime && elapsed>=0 && elapsed<=.25 ? elapsed : 0;
    this.last=timestamp;
    const media=Number.isFinite(mediaTime) && mediaTime>=0 ? mediaTime : null;
    const delta=media!==null && this.lastMedia!==null ? media-this.lastMedia : 0;
    // Always re-anchor, even on pause/seek. Resuming cannot catch up through
    // buffered time, a tab suspension, a backwards seek or invalid telemetry.
    const mediaDt=this.segmentActive && speaking && !paused && !listening && wallDt>0
      && delta>0 && delta<=.25 && delta<=wallDt+.035 ? delta : 0;
    this.lastMedia=media;
    const dt=this.segmentActive ? mediaDt : wallDt;
    const steps=Math.max(1,Math.ceil(dt/(1/120)));
    const slice=dt/steps;
    const loudness=bound(energy,0,1), emphasis=bound(accent,0,1);
    // Consume on receipt even when a pause, seek or silence prevents execution.
    // The caller cannot replay an old event by leaving it attached on resume.
    const semanticKind=semanticMode ? this.consumeSemanticCue(semanticCue) : null;
    for(let i=0;i<steps && slice>0;i++) {
      const target=this.segmentActive ? this.advance(slice,loudness,emphasis,semanticMode,i===0 ? semanticKind : null)
        : CompanionSpeechPerformance.zero();
      for(const key of Object.keys(this.values)) {
        const tau=key.startsWith('head') ? .055 : key.startsWith('body') ? .085 : .028;
        const releaseTau=key.startsWith('head') ? .14 : key.startsWith('body') ? .19 : .085;
        this.values[key]+=(target[key]-this.values[key])*(1-Math.exp(-slice/(this.segmentActive ? tau : releaseTau)));
        if(target[key]===0 && Math.abs(this.values[key])<.00001)this.values[key]=0;
      }
    }
    const master=bound(performance,0,3), motion=bound(intensity,0,2);
    const eyes=bound(eyeIntensity,0,1), face=bound(faceIntensity,0,1);
    const out=CompanionSpeechPerformance.zero();
    for(const key of Object.keys(out)) {
      const eye=key.startsWith('gaze') || key==='eyeOpen';
      const facial=eye || key==='smile' || key.startsWith('brow');
      let gain=key==='weight' ? master : master*(eye ? eyes : facial ? face : motion);
      if(key==='eyeOpen')gain*=face;
      // Purposeful gaze is owned by CompanionGaze's focus signal in this mode.
      if(semanticMode && key.startsWith('gaze'))gain=0;
      if(explicitFace && key!=='weight')gain*=facial ? 0 : .45;
      const limit=CompanionSpeechPerformance.limits[key];
      out[key]=gain===0 ? 0 : bound(this.values[key]*gain,-limit,limit);
    }
    const active=Object.keys(out).some(key=>key!=='weight' && Math.abs(out[key])>.0001);
    if(!active)out.weight=0;
    if(!this.clip && !active)this.cue='';
    return {...out,active,cue:active ? this.cue : ''};
  }
}

CompanionSpeechPerformance.limits={headX:10,headY:10,headZ:10,bodyX:2,bodyY:2,bodyZ:2,
  gazeX:.25,gazeY:.18,eyeOpen:.10,smile:.10,browL:.12,browR:.12,weight:1};
CompanionSpeechPerformance.aliases={'伤心':'委屈','难过':'委屈','愤怒':'生气','高兴':'开心','惊喜':'惊讶'};
CompanionSpeechPerformance.semanticKinds={reflect:true,emphasis:true,question:true};
CompanionSpeechPerformance.profiles={
  '平静':{
    face:{eyeOpen:.025,smile:.018,browL:.065,browR:.055},
    poses:[
      {headY:-4,headZ:.7,bodyY:.55,gazeY:-.065},
      {headZ:3.7,headY:-1.3,bodyZ:.55,gazeX:.12,gazeY:-.025},
      {headX:3.8,headY:-1.1,bodyX:.45,gazeX:.16,gazeY:.015},
    ],
  },
  '开心':{
    face:{eyeOpen:-.065,smile:.085,browL:.045,browR:.035},
    poses:[
      {headZ:4.3,headY:-1.1,bodyZ:.7,bodyY:.2,gazeX:.12},
      {headY:-4.1,headX:1.3,bodyY:.7,gazeY:-.07},
      {headX:3.8,headZ:2.7,bodyX:.55,bodyZ:.35,gazeX:.17,gazeY:.025},
    ],
  },
  '得意':{
    face:{eyeOpen:-.035,smile:.045,browL:.095,browR:-.025},
    poses:[
      {headX:4,headY:1.7,bodyX:.5,gazeX:.19,gazeY:.025},
      {headZ:-3.7,headY:1.3,bodyZ:-.5,gazeX:.14,gazeY:.045},
      {headX:2.2,headY:3.5,bodyY:-.4,gazeX:.15,gazeY:.08},
    ],
  },
  '吐槽':{
    face:{eyeOpen:-.055,smile:.012,browL:.075,browR:-.065},
    poses:[
      {headX:4.3,headY:.7,bodyX:.55,gazeX:.21,gazeY:.01},
      {headZ:-3.8,headX:1.8,bodyZ:-.55,gazeX:.15,gazeY:.035},
      {headX:3.5,headZ:2.3,bodyX:.4,bodyZ:.25,gazeX:.18,gazeY:-.025},
    ],
  },
  '惊讶':{
    face:{eyeOpen:.085,smile:0,browL:.115,browR:.105},
    poses:[
      {headY:4,headZ:.8,bodyY:-.7,gazeY:.085},
      {headZ:3.8,headY:2.1,bodyY:-.55,bodyZ:.35,gazeX:.1,gazeY:.07},
      {headX:3.6,headY:2.4,bodyX:.35,bodyY:-.6,gazeX:.14,gazeY:.08},
    ],
  },
  '委屈':{
    face:{eyeOpen:-.045,smile:0,browL:-.045,browR:-.025},
    poses:[
      {headY:-3.8,headZ:1.3,bodyY:.35,gazeY:-.12},
      {headZ:3.6,headY:-2,bodyZ:.35,gazeX:.1,gazeY:-.1},
      {headX:3.5,headY:-2.2,bodyX:.35,gazeX:.16,gazeY:-.11},
    ],
  },
  '生气':{
    face:{eyeOpen:-.035,smile:0,browL:-.105,browR:-.09},
    poses:[
      {headY:-4.1,headZ:-.7,bodyY:.65,gazeY:-.045},
      {headZ:-3.8,headY:-1.8,bodyZ:-.45,bodyY:.4,gazeX:.1,gazeY:-.025},
      {headX:3.7,headY:-2,bodyX:.45,bodyY:.35,gazeX:.16,gazeY:-.04},
    ],
  },
};
if(typeof module!=='undefined')module.exports=CompanionSpeechPerformance;
if(typeof window!=='undefined')window.CompanionSpeechPerformance=CompanionSpeechPerformance;
