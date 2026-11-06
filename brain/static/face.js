// Speech drives the jaw; these small offsets add expression, not phoneme guesses.
class CompanionFace {
  constructor({random = Math.random} = {}) {
    this.random = typeof random === 'function' ? random : Math.random;
    this.lastFrame = null;
    this.clock = 0;
    this.speechClock = 0;
    this.turnId = null;
    this.active = false;
    this.explicitFace = false;
    this.emotion = '平静';
    this.afterglow = null;
    this.afterglowAllowed = true;
    this.referenceLevel = .10;
    this.gateOpen = false;
    this.mouthOpen = 0;
    this.shapeEnergy = 0;
    this.phraseEnergy = 0;
    this.articulation = {width:0, round:0, shift:0};
    this.articulationPhase = 0;
    this.articulationOnsets = 0;
    this.articulationWasVoiced = null;
    this.articulationSilence = 0;
    this.articulationBrightness = .5;
    this.pose = {...CompanionFace.poses['平静']};
    this.lastMedia = null;
    this.wasAdvancing = false;
    this.guard = .16;
    this.previousEnergy = 0;
    this.voicedFor = 0;
    this.silentFor = 0;
    this.heardVoice = false;
    this.pauseBlinkUsed = false;
    this.lastAccentAt = -Infinity;
    this.accentAge = null;
    this.accentStrength = 0;
    this.blinkAge = null;
    this.lastBlinkAt = -Infinity;
    this.nextBlinkAt = 2 + this.pick() * 1.6;
    this.blinkTiming = {close:.065, hold:.03, open:.13};
    this.blinkFollowup = false;
    this.pairAfterBlink = false;
    this.doubleBlinkAt = null;
    this.lastDoublePairAt = -Infinity;
    this.previousGazeBlink = false;
    this.eyelidRelax = 0;
    this.eyelidAsymmetry = 0;
    this.eyelidTarget = 0;
    this.asymmetryTarget = 0;
    this.nextEyelidShiftAt = .65 + this.pick() * .65;
  }

  static finite(value, fallback = 0) {
    return Number.isFinite(value) ? value : fallback;
  }
  static clamp(value, low, high) {
    return Math.max(low, Math.min(high, CompanionFace.finite(value, low)));
  }
  static perform(value, multiplier, low, high) {
    // Preserve the existing performance exactly at 1. Above it, spend the
    // remaining native parameter range smoothly instead of flattening multiple
    // distinct mouth shapes against a shared hard limit.
    if (value === 0 || multiplier === 0) return 0;
    if (multiplier <= 1) return value * multiplier;
    const magnitude = Math.abs(value);
    const room = (value < 0 ? -low : high) - magnitude;
    if (room <= 0) return value;
    const extra = magnitude * (multiplier - 1);
    return Math.sign(value) * (magnitude + extra / Math.cbrt(1 + Math.pow(extra / room, 3)));
  }
  pick() {
    return CompanionFace.clamp(this.random(), 0, 1);
  }

  begin({turnId, segmentIndex = 0, emotion = '平静', text = '', explicitFace = false} = {}, now) {
    const sameTurn = turnId != null && turnId === this.turnId;
    this.turnId = turnId;
    this.segmentIndex = CompanionFace.finite(segmentIndex);
    const mappedEmotion = Object.hasOwn(CompanionFace.emotionAliases, emotion)
      ? CompanionFace.emotionAliases[emotion] : emotion;
    this.emotion = Object.hasOwn(CompanionFace.poses, mappedEmotion) ? mappedEmotion : '平静';
    this.explicitFace = Boolean(explicitFace);
    this.active = true;
    this.afterglow = null;
    this.afterglowAllowed = true;
    // A new audio element starts at zero. Keep the visual pose and same-turn gain.
    if (!sameTurn) {
      this.referenceLevel = .10;
      this.speechClock = 0;
      this.lastAccentAt = -Infinity;
      this.articulationPhase = 0;
      this.articulationOnsets = 0;
      this.articulationBrightness = .5;
    }
    this.articulationWasVoiced = null;
    this.articulationSilence = 0;
    this.lastMedia = null;
    this.wasAdvancing = false;
    this.guard = .16;
    this.previousEnergy = 0;
    this.voicedFor = 0;
    this.silentFor = 0;
    this.heardVoice = false;
    this.pauseBlinkUsed = false;
    this.accentAge = null;
    if (this.lastFrame === null && Number.isFinite(now)) this.lastFrame = now;
    // Text is deliberately not turned into random or guessed syllable animation.
    void text;
  }

  end({completed = false} = {}, now) {
    const releasedExplicitFace = this.explicitFace;
    const wasActive = this.active;
    this.explicitFace = false;
    this.active = false;
    // The afterglow has a wall-clock deadline: hiding the tab cannot preserve a
    // smile indefinitely. Repeated completion notifications never restart it.
    if (!completed || releasedExplicitFace || !this.afterglowAllowed) this.clearAfterglow();
    else if (wasActive) {
      const contour = CompanionFace.afterglows[this.emotion];
      const startedAt = Math.max(CompanionFace.finite(now, this.lastFrame || 0), this.lastFrame || 0);
      this.afterglow = contour ? {emotion:this.emotion, startedAt, ...contour} : null;
    }
    // An explicit asset already owned the visible face: do not briefly reveal
    // the hidden emotional pose after its owner releases it.
    if (releasedExplicitFace) {
      for (const key of Object.keys(this.pose)) this.pose[key] = 0;
    }
    this.accentAge = null;
    this.wasAdvancing = false;
    this.guard = .16;
    if (this.lastFrame === null && Number.isFinite(now)) this.lastFrame = now;
  }

  clearAfterglow() {
    this.afterglow = null;
    // A user may respond during the host's delayed release. Remember that
    // hand-off so the later completed end cannot resurrect the old emotion.
    this.afterglowAllowed = false;
  }

  startBlink({followup = false, allowDouble = false, eyeAmount = 0} = {}) {
    if (this.blinkAge !== null) return false;
    if (followup) {
      if (this.doubleBlinkAt === null || this.clock < this.doubleBlinkAt) return false;
    } else if (this.clock - this.lastBlinkAt < 1.65) return false;
    this.blinkAge = 0;
    this.lastBlinkAt = this.clock;
    this.blinkFollowup = followup;
    this.doubleBlinkAt = null;
    const speed = followup ? .9 : 1;
    this.blinkTiming = {
      close:(.05 + this.pick() * .025) * speed,
      hold:.02 + this.pick() * .02,
      open:(.105 + this.pick() * .055) * speed,
    };
    this.pairAfterBlink = false;
    if (!followup) {
      this.nextBlinkAt = this.clock + 2.3 + this.pick() * 3.2;
      if (allowDouble && this.clock - this.lastDoublePairAt >= 12
          && this.pick() < .12 * eyeAmount) {
        this.pairAfterBlink = true;
        this.lastDoublePairAt = this.clock;
      }
    }
    return true;
  }

  updateArticulation({brightness, dt, advancing, rhythmDt, discontinuity, speaking, amount}) {
    const clamp = CompanionFace.clamp;
    const ease = (value, target, tau) => value + (target - value) * (1 - Math.exp(-dt / tau));
    // Only real clip progress and real voice onsets change the fallback contour.
    // Pauses/seeks/stalls cannot invent a new syllable or catch up a lost queue.
    if (discontinuity || !advancing) {
      this.articulationWasVoiced = this.gateOpen;
      this.articulationSilence = 0;
    } else {
      if (this.gateOpen) {
        if (this.articulationWasVoiced === false && this.articulationSilence >= .055)
          this.articulationOnsets = (this.articulationOnsets + 1) % 1024;
        this.articulationSilence = 0;
        this.articulationPhase = (this.articulationPhase + rhythmDt * (4.2 + this.shapeEnergy)) % (Math.PI * 256);
      } else this.articulationSilence += rhythmDt;
      this.articulationWasVoiced = this.gateOpen;
    }
    if (!speaking || this.explicitFace || amount === 0) {
      for (const key of Object.keys(this.articulation)) this.articulation[key] = 0;
      return;
    }
    const targets = {width:0, round:0, shift:0};
    if (this.gateOpen) {
      if (!advancing) return;
      const phase = this.articulationPhase, onset = this.articulationOnsets;
      // Spectral brightness distinguishes narrow/rounded and wider shapes at
      // the same jaw opening. Without it this is bounded speech animation, not
      // recognition of vowels or phonemes. No per-frame random shape changes.
      const fallback = clamp(.5 + .35 * Math.sin(phase + onset * .9)
        + .13 * Math.sin(phase * .5 + onset * .31), .04, .96);
      const targetBrightness = Number.isFinite(brightness) ? clamp(brightness, 0, 1) : fallback;
      this.articulationBrightness = ease(this.articulationBrightness, targetBrightness, .085);
      const color = this.articulationBrightness;
      const voiceWeight = clamp(Math.sqrt(this.shapeEnergy) * 1.05, 0, 1);
      targets.width = (-.42 + color * 1.02) * voiceWeight;
      targets.round = Math.pow(1 - color, 1.1) * .52 * voiceWeight;
      targets.shift = Math.sin(phase * .62 + onset * .91) * .23 * voiceWeight;
    }
    for (const key of Object.keys(this.articulation)) {
      const tau = !this.gateOpen ? .05 : key === 'shift' ? .095 : .065;
      this.articulation[key] = ease(this.articulation[key], targets[key], tau);
      if (targets[key] === 0 && Math.abs(this.articulation[key]) < .001) this.articulation[key] = 0;
    }
  }

  frame(now, {level = 0, playing = false, paused = false, mediaTime = 0, intensity = 1,
    eyeIntensity = .75, gazeBlink = false, brightness = null, performance = 1, listening = false} = {}) {
    const finite = CompanionFace.finite, clamp = CompanionFace.clamp;
    const validClock = Number.isFinite(now) && (this.lastFrame === null || now >= this.lastFrame);
    now = finite(now, this.lastFrame === null ? 0 : this.lastFrame);
    if (this.lastFrame !== null) now = Math.max(this.lastFrame, now);
    const elapsed = this.lastFrame === null ? 0 : Math.max(0, (now - this.lastFrame) / 1000);
    const dt = Math.min(.05, elapsed);
    this.lastFrame = now;
    this.clock += dt;
    const ease = (value, target, tau) => value + (target - value) * (1 - Math.exp(-dt / tau));
    const speaking = Boolean(playing && !paused);
    if (listening) this.clearAfterglow();
    const rms = speaking ? clamp(level, 0, 1) : 0;
    const amount = clamp(intensity, 0, 1.6);
    const eyeAmount = clamp(eyeIntensity, 0, 1);
    const performanceAmount = clamp(finite(performance, 1), 0, 3);
    const perform = (value, low, high) => CompanionFace.perform(value, performanceAmount, low, high);
    // Face intensity remains the master facial-style switch; pupil gaze has its
    // own controller and is not disabled here.
    const eyeStyle = eyeAmount > 0 && amount > 0 && performanceAmount > 0 && !this.explicitFace && !paused;
    const gazeCue = Boolean(gazeBlink) && !this.previousGazeBlink;
    this.previousGazeBlink = Boolean(gazeBlink);

    // A conservative gate and bounded, slow gain adaptation preserve quiet voices
    // without amplifying silence. Gain is shared by all segments of one reply.
    this.gateOpen = speaking && rms >= (this.gateOpen ? .0065 : .009);
    if (this.gateOpen) this.referenceLevel = ease(this.referenceLevel, clamp(rms, .035, .22), 1.1);
    const gain = clamp(.12 / this.referenceLevel, .65, 2.4);
    const energy = clamp((rms - .0065) * gain / .14, 0, 1);
    const jawTarget = this.gateOpen ? Math.pow(energy, .78) * .9 : 0;
    this.mouthOpen = ease(this.mouthOpen, jawTarget, jawTarget > this.mouthOpen ? .026 : .045);
    if (jawTarget === 0 && this.mouthOpen < .001) this.mouthOpen = 0;
    // A restrained lip-shape contour follows real syllable energy. It adds no
    // guessed vowels and returns to the emotional baseline between syllables.
    const shapeTarget = this.gateOpen ? energy : 0;
    this.shapeEnergy = speaking
      ? ease(this.shapeEnergy, shapeTarget, shapeTarget > this.shapeEnergy ? .04 : .06) : 0;
    if (shapeTarget === 0 && this.shapeEnergy < .001) this.shapeEnergy = 0;
    // Brows and eyelids follow the broad phrase contour, not every syllable.
    // Silence releases this layer; neither neutral rest nor paused audio holds it.
    this.phraseEnergy = speaking ? ease(this.phraseEnergy, shapeTarget,
      shapeTarget === 0 ? .18 : shapeTarget > this.phraseEnergy ? .32 : .42) : 0;
    if (shapeTarget === 0 && this.phraseEnergy < .001) this.phraseEnergy = 0;

    // Rhythm advances only with the clip. A seek, stalled clip or background gap
    // primes the detector again, instead of replaying missed emphasis events.
    const media = Number.isFinite(mediaTime) ? mediaTime : null;
    const mediaDelta = media !== null && this.lastMedia !== null ? media - this.lastMedia : 0;
    const discontinuity = !validClock || elapsed > .25 || mediaDelta < -.02 || mediaDelta > .35;
    const extraEyeCues = eyeStyle && !discontinuity;
    if (!extraEyeCues) {
      this.doubleBlinkAt = null;
      this.pairAfterBlink = false;
    }
    const advancing = speaking && !discontinuity && mediaDelta > 0;
    const rhythmDt = advancing ? Math.min(.05, mediaDelta) : 0;
    this.lastMedia = media;
    this.updateArticulation({brightness, dt, advancing, rhythmDt, discontinuity, speaking,
      amount:performanceAmount > 0 ? amount : 0});
    if (!advancing || !this.wasAdvancing) {
      this.guard = .16;
      this.previousEnergy = energy;
      this.accentAge = null;
      this.silentFor = 0;
      this.voicedFor = 0;
      if (discontinuity) { this.heardVoice = false; this.pauseBlinkUsed = false; }
    }
    if (advancing) {
      this.speechClock += rhythmDt;
      this.guard = Math.max(0, this.guard - rhythmDt);
      if (this.gateOpen) {
        this.voicedFor += rhythmDt;
        this.silentFor = 0;
        this.pauseBlinkUsed = false;
        if (this.voicedFor >= .08) this.heardVoice = true;
      } else {
        this.silentFor += rhythmDt;
        this.voicedFor = 0;
      }
      if (this.guard === 0 && this.heardVoice && !this.pauseBlinkUsed
          && this.silentFor >= .18 && extraEyeCues) {
        this.pauseBlinkUsed = this.startBlink({allowDouble:true, eyeAmount});
      }
      const rise = energy - this.previousEnergy;
      if (this.guard === 0 && this.gateOpen && energy > .32 && rise > .16
          && this.speechClock - this.lastAccentAt >= 1.25 && amount > 0 && performanceAmount > 0 && !this.explicitFace) {
        this.lastAccentAt = this.speechClock;
        this.accentAge = 0;
        this.accentStrength = Math.min(.78, .35 + rise * .7);
      }
      this.previousEnergy += (energy - this.previousEnergy) * (1 - Math.exp(-rhythmDt / .055));
      if (this.accentAge !== null) {
        this.accentAge += rhythmDt;
        if (this.accentAge >= .36) this.accentAge = null;
      }
    }
    this.wasAdvancing = advancing;
    const accent = this.accentAge === null || !speaking || amount === 0 || performanceAmount === 0 || this.explicitFace ? 0
      : clamp(Math.sin(Math.PI * this.accentAge / .36) * this.accentStrength * Math.min(1, amount), 0, 1);

    const alive = this.active && !listening && (this.afterglowAllowed || speaking);
    // Rest has a restrained smile of its own. It replaces a finished emotion;
    // it is never added on top of a sad, angry or explicitly owned expression.
    const rest = CompanionFace.poses['平静'];
    let profile = CompanionFace.poses[alive ? this.emotion : '平静'];
    if (!alive && this.afterglow) {
      const glow = this.afterglow;
      const progress = clamp((now - glow.startedAt) / (glow.duration * 1000), 0, 1);
      if (progress >= 1) this.afterglow = null;
      else {
        const source = CompanionFace.poses[glow.emotion];
        const residue = glow.strength * Math.pow(1 - progress, glow.falloff || 1);
        // Sadness/anger first settle into a quiet face. Only the closing portion
        // returns to the usual slight smile; no cheerful layer is added early.
        const recovery = glow.quiet ? clamp((progress - .65) / .35, 0, 1) : 1;
        const settled = recovery * recovery * (3 - 2 * recovery);
        profile = {};
        for (const key of Object.keys(rest)) {
          const base = glow.quiet && ['mouthForm', 'eyeSmile'].includes(key) ? 0 : rest[key];
          profile[key] = base + (source[key] - base) * residue + (rest[key] - base) * settled;
        }
      }
    }
    const weight = !this.explicitFace ? Math.min(1.25, amount) : 0;
    // Expression decay catches up with elapsed wall time after backgrounding,
    // without advancing any jaw, blink, syllable or emphasis clocks.
    const poseDt = !alive ? Math.min(10, elapsed) : dt;
    for (const key of Object.keys(this.pose)) {
      const target = profile[key] * weight;
      const tau = !weight ? .36 : alive ? .24 : this.afterglow ? .38 : .55;
      this.pose[key] += (target - this.pose[key]) * (1 - Math.exp(-poseDt / tau));
    }
    // Explicit asset expressions own the face. The jaw and base blink stay live.
    const visible = this.explicitFace || amount === 0 || performanceAmount === 0 ? 0 : 1;
    if (gazeCue && extraEyeCues) this.startBlink({allowDouble:true, eyeAmount});
    if (this.doubleBlinkAt !== null && this.clock >= this.doubleBlinkAt)
      this.startBlink({followup:true});
    if (this.clock >= this.nextBlinkAt) this.startBlink({allowDouble:extraEyeCues, eyeAmount});
    let blink = 1;
    if (this.blinkAge !== null) {
      this.blinkAge += dt;
      const t = this.blinkAge;
      const {close, hold, open} = this.blinkTiming;
      const smooth = value => { const x = clamp(value, 0, 1); return x * x * (3 - 2 * x); };
      blink = t < close ? 1 - smooth(t / close) : t < close + hold ? 0 : smooth((t - close - hold) / open);
      if (t >= close + hold + open) {
        this.blinkAge = null;
        blink = 1;
        if (this.pairAfterBlink && !this.blinkFollowup && extraEyeCues)
          this.doubleBlinkAt = this.clock + .09 + this.pick() * .09;
        this.pairAfterBlink = false;
      }
    }
    // Slow, mostly downward eyelid relaxation stays visible below a model's
    // fully-open limit. Small asymmetry never becomes a wink or frame jitter.
    if (eyeStyle && this.clock >= this.nextEyelidShiftAt) {
      this.eyelidTarget = this.eyelidTarget > .006 ? this.pick() * .003 : .012 + this.pick() * .028;
      this.asymmetryTarget = (this.pick() * 2 - 1) * .006;
      this.nextEyelidShiftAt = this.clock + 1.1 + this.pick() * 1.6;
    }
    this.eyelidRelax = ease(this.eyelidRelax, eyeStyle ? this.eyelidTarget : 0, .55);
    this.eyelidAsymmetry = ease(this.eyelidAsymmetry, eyeStyle ? this.asymmetryTarget : 0, .7);
    const eyeWeight = eyeStyle ? eyeAmount : 0;
    const relax = perform(this.eyelidRelax * eyeWeight, 0, .12);
    const asymmetry = perform(this.eyelidAsymmetry * eyeWeight, -.04, .04);
    const phrase = this.phraseEnergy * Math.min(1.25, amount);
    const response = CompanionFace.phraseResponse[this.emotion];
    const baseSmile = clamp(this.pose.eyeSmile * visible, 0, .2);
    const eyeSmile = perform(baseSmile, 0, .5);
    // On this rig EyeSmile barely moves the mesh with fully open eyes. Couple
    // additional positive smile to a little lid closure; never apply that smile
    // cue to a negative emotion, a native expression or disabled eye styling.
    const smileClosure = eyeStyle && profile.eyeSmile > 0
      ? clamp((eyeSmile - baseSmile) * 2.2 * eyeAmount, 0, .3) : 0;
    const eyeBase = .98 + perform((this.pose.eyeDelta + phrase * .028 * response.eye) * visible, -.28, .2)
      - smileClosure;
    const lift = perform(accent * .008, 0, .04);
    const lipContour = this.shapeEnergy * Math.min(1.25, amount);
    const articulationScale = Math.min(1.25, amount);
    return {
      mouthOpen: clamp(this.mouthOpen, 0, 1),
      mouthForm: perform(clamp((this.pose.mouthForm + lipContour * .065) * visible, -1, 1), -1, 1),
      mouthWidth: perform(clamp((this.pose.mouthWidth + this.articulation.width * articulationScale) * visible, -.65, .85), -1, 1),
      mouthRound: perform(clamp(this.articulation.round * articulationScale * visible, 0, .6), 0, .95),
      mouthShift: perform(clamp(this.articulation.shift * articulationScale * visible, -.24, .24), -.7, .7),
      browL: perform(clamp((this.pose.browL + phrase * .112 * response.left + accent * .024) * visible, -1, 1), -1, 1),
      browR: perform(clamp((this.pose.browR + phrase * .105 * response.right + accent * .02) * visible, -1, 1), -1, 1),
      eyeSmile,
      eyeOpenL: clamp((eyeBase + lift - relax + asymmetry) * blink, 0, 1.15),
      eyeOpenR: clamp((eyeBase + lift * .8 - relax - asymmetry) * blink, 0, 1.15),
      blink: clamp(blink, 0, 1),
      accent,
      // Body speech motion must remain available when face styling is disabled.
      speechEnergy: clamp(this.shapeEnergy, 0, 1),
    };
  }
}

// Values are offsets from native defaults, not full-range expression switches.
CompanionFace.poses = {
  '平静': {mouthForm:.13, mouthWidth:.02, browL:0, browR:0, eyeSmile:.04, eyeDelta:0},
  '开心': {mouthForm:.19, mouthWidth:.075, browL:.045, browR:.045, eyeSmile:.095, eyeDelta:-.035},
  '得意': {mouthForm:.13, mouthWidth:.05, browL:-.035, browR:.045, eyeSmile:.045, eyeDelta:-.03},
  '吐槽': {mouthForm:.015, mouthWidth:-.025, browL:.065, browR:-.07, eyeSmile:.008, eyeDelta:-.015},
  '惊讶': {mouthForm:-.03, mouthWidth:-.045, browL:.11, browR:.11, eyeSmile:0, eyeDelta:.06},
  '委屈': {mouthForm:-.13, mouthWidth:-.035, browL:.08, browR:.08, eyeSmile:0, eyeDelta:-.06},
  '生气': {mouthForm:-.075, mouthWidth:-.025, browL:-.055, browR:-.055, eyeSmile:0, eyeDelta:-.035},
};
CompanionFace.emotionAliases = {'伤心':'委屈', '难过':'委屈', '愤怒':'生气'};
// A short, quiet continuation of the reply, not another full expression clip.
CompanionFace.afterglows = {
  '开心': {duration:4, strength:.55},
  '得意': {duration:3.5, strength:.45},
  '吐槽': {duration:3, strength:.4},
  '惊讶': {duration:2, strength:.4, falloff:2},
  '委屈': {duration:5, strength:.45, quiet:true},
  '生气': {duration:4.5, strength:.35, quiet:true},
};
// Preserve asymmetric or downcast emotional poses instead of lifting every
// emotional face into the same neutral talking pose.
CompanionFace.phraseResponse = {
  '平静': {left:1, right:1, eye:1},
  '开心': {left:.55, right:.55, eye:.35},
  '得意': {left:.25, right:.65, eye:.35},
  '吐槽': {left:.65, right:.12, eye:.5},
  '惊讶': {left:.45, right:.45, eye:.6},
  '委屈': {left:.28, right:.28, eye:.2},
  '生气': {left:.18, right:.18, eye:.2},
};

if (typeof module !== 'undefined') module.exports = CompanionFace;
else window.CompanionFace = CompanionFace;
