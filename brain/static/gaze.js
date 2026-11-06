// One fixation scheduler for pupil movement and delayed, low-amplitude head follow.
class CompanionGaze {
  constructor({random=Math.random}={}) {
    this.random = typeof random === 'function' ? random : Math.random;
    this.last = null;
    this.clock = 0;
    this.mode = '';
    this.pose = 'camera';
    this.x = 0;
    this.y = 0;
    this.headX = 0;
    this.headY = 0;
    this.targetX = 0;
    this.targetY = 0;
    this.microX = 0;
    this.microY = 0;
    this.driftX = 0;
    this.driftY = 0;
    this.fixationAt = 0;
    this.nextMicro = 0;
    this.nextLook = Infinity;
    this.focus = '';
    this.focusReturnAt = Infinity;
    this.headAt = Infinity;
    this.pendingHeadX = 0;
    this.pendingHeadY = 0;
  }
  static bounded(value, low, high, fallback=0) {
    return Number.isFinite(value) ? Math.max(low, Math.min(high, value)) : fallback;
  }
  sample(low=0, high=1) {
    let value;
    try { value = this.random(); } catch (_) { value = .5; }
    return low+(high-low)*CompanionGaze.bounded(value,0,1,.5);
  }
  setPose(pose, x, y, delayed=true) {
    this.pose = pose;
    this.targetX = x;
    this.targetY = y;
    // Micro-fixations never enter these targets. The motion controller adds
    // its existing dead zones and slow head response after this eye-first delay.
    this.pendingHeadX = pose === 'aside' ? x*.82 : 0;
    this.pendingHeadY = pose === 'thinking' ? y*.78 : 0;
    this.headAt = this.clock+(delayed ? .2 : 0);
  }
  frame(now, {speaking=false, paused=false, listening=false, thinking=false,
    intensity=.75, performance=1, explicitFace=false, eyeOpen=1, focus=''}={}) {
    const bounded = CompanionGaze.bounded;
    const timestamp = Number.isFinite(now) ? now : (this.last ?? 0);
    const elapsed = this.last === null ? 0 : Math.max(0,(timestamp-this.last)/1000);
    const dt = elapsed>.25 ? 0 : Math.min(.05,elapsed);
    this.last = this.last === null ? timestamp : Math.max(this.last,timestamp);
    // Timers use animation time, so a background tab never catches up through
    // several unseen fixations or issues a burst of blink requests on resume.
    if (!dt) return {x:this.x,y:this.y,headX:this.headX,headY:this.headY,blinkRequest:false};
    this.clock += dt;
    const baseAmount = bounded(intensity,0,1.6,.75);
    const performanceAmount = bounded(performance,0,3,1);
    const amount = baseAmount*performanceAmount;
    const open = bounded(eyeOpen,0,1,1);
    const closed = open<=.12;
    const mode = paused ? 'paused' : explicitFace ? 'explicit' : listening ? 'listening'
      : thinking ? 'thinking' : speaking ? 'speaking' : 'idle';
    const desiredFocus = ['reflect','engage','question'].includes(focus) ? focus : '';
    const focusChanged = desiredFocus !== this.focus;
    this.focus = desiredFocus;
    const ease = (value,target,tau) => value+(target-value)*(1-Math.exp(-dt/tau));
    let blinkRequest = false;
    if (mode!==this.mode) {
      this.mode = mode;
      this.setPose('camera',0,0,false);
      this.microX = this.microY = this.driftX = this.driftY = 0;
      this.focusReturnAt = Infinity;
      this.fixationAt = this.clock;
      this.nextMicro = this.clock+this.sample(.65,1.1);
      this.nextLook = mode==='thinking' ? this.clock+this.sample(.7,1.4) : Infinity;
    }
    if (amount===0 || mode==='paused') {
      this.setPose('camera',0,0,false);
      this.microX = this.microY = this.driftX = this.driftY = 0;
      this.focusReturnAt = Infinity;
      // Keep disabled time from immediately triggering a look on re-enabling.
      this.nextLook = mode==='thinking' ? this.clock+.7 : Infinity;
      this.nextMicro = this.clock+.65;
    } else if (!closed) {
      // Semantic focus is edge-triggered, not a periodic scan. Repeating one
      // reflect cue never picks another direction, even after it has settled.
      // Attention, explicit expressions and pauses consume an incoming focus
      // without replaying that stale cue when their ownership later ends.
      const semantic = mode==='speaking' || mode==='idle';
      if (semantic && (focusChanged || this.clock>=this.focusReturnAt)) {
        const oldX = this.targetX, oldY = this.targetY;
        if (focusChanged && desiredFocus==='reflect') {
          const side = this.sample()<.5 ? -1 : 1;
          this.setPose('aside',side*this.sample(.62,.78),this.sample(.04,.12));
          this.focusReturnAt = this.clock+this.sample(1.2,1.7);
        } else {
          this.setPose('camera',0,0);
          this.focusReturnAt = Infinity;
        }
        blinkRequest = Math.hypot(this.targetX-oldX,this.targetY-oldY)*baseAmount>.3 && open>.35;
      }
      if (this.clock>=this.nextLook) {
        const oldX = this.targetX, oldY = this.targetY;
        if (this.pose!=='camera') {
          this.setPose('camera',0,0);
          this.nextLook = this.clock+this.sample(1.8,3);
        } else if (mode==='thinking') {
          const side = this.sample()<.5 ? -1 : 1;
          this.setPose('thinking',side*this.sample(.2,.34),this.sample(.21,.33));
          this.nextLook = this.clock+this.sample(1.4,2.4);
        }
        blinkRequest = Math.hypot(this.targetX-oldX,this.targetY-oldY)*baseAmount>.3 && open>.35;
      }
      if (this.clock>=this.nextMicro) {
        const scale = mode==='explicit' ? .3 : mode==='listening' ? .65
          : semantic && desiredFocus==='question' ? .35 : mode==='speaking' ? .55 : 1;
        this.microX = this.sample(-.045,.045)*scale;
        this.microY = this.sample(-.027,.027)*scale;
        // One tiny drift vector per fixation, not an oscillation or frame noise.
        this.driftX = this.sample(-.006,.006)*scale;
        this.driftY = this.sample(-.004,.004)*scale;
        this.fixationAt = this.clock;
        this.nextMicro = this.clock+this.sample(1.6,2.8);
      }
    }
    const drift = Math.min(1,(this.clock-this.fixationAt)/2);
    const microWeight = Math.max(0,(open-.12)/.88);
    const boosted = Math.min(1,Math.max(0,performanceAmount-1)/.8);
    const limitX = .88+.1*boosted,limitY = .5+.3*boosted;
    const targetX = bounded((this.targetX+(this.microX+this.driftX*drift)*microWeight)*amount,-limitX,limitX);
    const targetY = bounded((this.targetY+(this.microY+this.driftY*drift)*microWeight)*amount,-limitY,limitY);
    const returning = amount===0 || mode==='paused';
    if (!closed || returning) {
      const tau = returning ? .22 : Math.hypot(targetX-this.x,targetY-this.y)>.12 ? .095 : .15;
      const dx = ease(this.x,targetX,tau)-this.x;
      const dy = ease(this.y,targetY,tau)-this.y;
      // A new semantic cue can follow an unfinished return. Keep that wider
      // transition smooth without changing its target or timer.
      const distance = Math.hypot(dx,dy);
      const stepScale = distance ? Math.min(1,7.5*dt/distance) : 1;
      this.x += dx*stepScale;
      this.y += dy*stepScale;
    }
    if (amount===0 || mode==='paused' || mode==='explicit' || mode==='listening') {
      // These are gaze targets for an already-smoothed head controller.
      this.headX = this.headY = 0;
    } else if (this.clock>=this.headAt) {
      this.headX = ease(this.headX,bounded(this.pendingHeadX*amount,-.65,.65),.22);
      this.headY = ease(this.headY,bounded(this.pendingHeadY*amount,-.3,.3),.22);
    }
    return {x:this.x,y:this.y,headX:this.headX,headY:this.headY,blinkRequest};
  }
}
if (typeof module!=='undefined') module.exports=CompanionGaze;
else window.CompanionGaze=CompanionGaze;
