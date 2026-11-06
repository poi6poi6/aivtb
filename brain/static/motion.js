// Coordinated, bounded body motion; no per-frame random jitter.
class CompanionMotion {
  constructor() {
    this.last = null;
    this.time = 0;
    this.intensity = 0;
    this.performance = null;
    this.output = null;
    this.speaking = 0;
    this.speechAccent = 0;
    this.speechEnergy = 0;
    this.headIntensity = 0;
    this.attention = 0;
    this.headX = 0;
    this.headY = 0;
    this.gestures = new Map();
  }
  gesture(name, target, now) {
    const previous = this.gestures.get(name) || {value:0, last:now};
    const dt = Math.min(.05, Math.max(0, (now - previous.last) / 1000));
    previous.value += (target - previous.value) * (1 - Math.exp(-dt / (target ? .24 : .45)));
    previous.last = now;
    this.gestures.set(name, previous);
    return previous.value;
  }
  frame(now, {intensity=1, performance=1, speaking=false, speechAccent=0, speechEnergy=0, headIntensity=1, attention=0, gazeX=0, gazeY=0}={}) {
    const bounded = (value, low, high, fallback=0) => Number.isFinite(value)
      ? Math.max(low, Math.min(high, value)) : fallback;
    const timestamp = Number.isFinite(now) ? now : (this.last ?? 0);
    const elapsed = this.last === null ? 0 : Math.max(0, (timestamp-this.last)/1000);
    // A resumed tab starts where it paused instead of catching up in one pose.
    const dt = elapsed > .25 ? 0 : Math.min(.05, elapsed);
    this.last = this.last === null ? timestamp : Math.max(this.last, timestamp);
    this.time += dt;
    const ease = (value,target,tau) => value+(target-value)*(1-Math.exp(-dt/tau));
    this.intensity = ease(this.intensity,bounded(intensity,0,1.6,1),.65);
    const performanceTarget = bounded(performance,0,3,1);
    this.performance = this.performance===null ? performanceTarget : ease(this.performance,performanceTarget,.45);
    this.speaking = ease(this.speaking,speaking?1:0,.6);
    const accentTarget = speaking ? bounded(speechAccent,0,1) : 0;
    this.speechAccent = ease(this.speechAccent,accentTarget,accentTarget>this.speechAccent ? .12 : .28);
    const energyTarget = speaking ? bounded(speechEnergy,0,1) : 0;
    this.speechEnergy = ease(this.speechEnergy,energyTarget,energyTarget>this.speechEnergy ? .4 : .8);
    this.headIntensity = ease(this.headIntensity,bounded(headIntensity,0,1.6,1),.45);
    this.attention = ease(this.attention,bounded(attention,0,1),.45);
    // Eye-only zone: small fixations should not pull the head or torso around.
    const follow = (value, deadzone) => Math.sign(value)*Math.max(0,Math.abs(value)-deadzone);
    this.headX = ease(this.headX,follow(bounded(gazeX,-1,1),.35)*2.6,.85);
    this.headY = ease(this.headY,follow(bounded(gazeY,-1,1),.18)*1.8,.9);
    const t=this.time, amount=this.intensity;
    const phase=(t/4.8)%1;
    const breath=phase<.4?(1-Math.cos(Math.PI*phase/.4))/2:(1+Math.cos(Math.PI*(phase-.4)/.6))/2;
    const rise=(breath-.5)*(1-.28*this.speaking);
    const sway=Math.sin(t*.49)*.75+Math.sin(t*.23+1)*.25;
    // Several slow arcs keep a speaking pose alive without tying every axis
    // to the same beat. Quiet playback settles to a small baseline movement.
    const speechAmount=this.speaking*(.16+.84*this.speechEnergy)*this.headIntensity;
    const speechTurn=(Math.sin(t*.78)*.74+Math.sin(t*.31+.6)*.26)*3.8*speechAmount;
    const speechTilt=(Math.sin(t*.93+.7)*.68+Math.sin(t*.41-1)*.32)*2.5*speechAmount;
    const speechNod=(Math.sin(t*1.19+.9)*.6+Math.sin(t*.53)*.4)*speechAmount
      -this.speechAccent*.85*this.headIntensity;
    const bodyAmount=amount*this.performance,headAmount=amount*Math.pow(this.performance,1.7);
    // Preserve the baseline exactly, then approach this model's native ±10°
    // body and ±30° head bounds gently at the largest combined settings.
    const nativeBound=(value,limit)=>{
      const magnitude=Math.abs(value),knee=limit*.8,room=limit-knee;
      return magnitude<=knee ? value : Math.sign(value)*(knee+room*Math.tanh((magnitude-knee)/room));
    };
    const output={
      breath:Math.min(1,(.08+breath*.88)*(1-.22*this.speaking)*Math.min(1,amount)*this.performance),
      bodyX:nativeBound(sway*2.4*bodyAmount*(1-.35*this.attention),10),
      bodyY:nativeBound((rise*2.6+this.attention*.7)*bodyAmount,10),
      bodyZ:nativeBound(Math.sin(t*.49-.65)*1.6*bodyAmount*(1-.4*this.attention),10),
      headX:nativeBound((this.headX+speechTurn)*headAmount,30),
      headY:nativeBound((this.headY+rise*1.5+speechNod)*headAmount,30),
      headZ:nativeBound((-sway*.85+this.attention*.6+speechTilt)*headAmount,30),
    };
    if (this.output) {
      if (!dt) return {...this.output};
      // At the maximum combined sliders, an accent must not become a snap.
      // Normal baseline arcs remain below these limits and are unchanged.
      for (const axis of ['headX','headY','headZ','bodyX','bodyY','bodyZ']) {
        const step=(axis.startsWith('head') ? 48 : 18)*dt;
        const delta=output[axis]-this.output[axis];
        if (Math.abs(delta)>step) output[axis]=this.output[axis]+Math.sign(delta)*step;
      }
    }
    this.output={...output};
    return output;
  }
}
if(typeof module!=='undefined') module.exports=CompanionMotion;
else window.CompanionMotion=CompanionMotion;
