const assert = require('node:assert/strict');
const Face = require('../brain/static/face.js');

function runner(emotion = '平静', extra = {}, random = () => .5) {
  const face = new Face({random});
  face.begin({turnId: 1, segmentIndex: 0, emotion, ...extra}, 0);
  let now = 0, media = 0, output;
  return {face, get now() { return now; }, get media() { return media; },
    step(level = .06, options = {}) {
      now += options.jump || 16;
      if (!options.paused && options.playing !== false) media += options.seek || .016;
      output = face.frame(now, {playing:true, mediaTime:media, level, ...options});
      for (const value of Object.values(output)) assert(Number.isFinite(value), 'every parameter remains finite');
      assert(output.mouthOpen >= 0 && output.mouthOpen <= 1);
      assert(output.mouthForm >= -1 && output.mouthForm <= 1);
      assert(output.mouthWidth >= -1 && output.mouthWidth <= 1);
      assert(output.mouthRound >= 0 && output.mouthRound <= .95);
      assert(output.mouthShift >= -.7 && output.mouthShift <= .7);
      assert(output.browL >= -1 && output.browL <= 1);
      assert(output.browR >= -1 && output.browR <= 1);
      assert(output.eyeSmile >= 0 && output.eyeSmile <= .5);
      if (options.performance === undefined || options.performance <= 1) {
        assert(output.mouthWidth >= -.65 && output.mouthWidth <= .85);
        assert(output.mouthRound <= .6 && Math.abs(output.mouthShift) <= .24);
      }
      assert(output.eyeOpenL >= 0 && output.eyeOpenL <= 1.15);
      assert(output.eyeOpenR >= 0 && output.eyeOpenR <= 1.15);
      assert(output.accent >= 0 && output.accent <= 1);
      assert(output.speechEnergy >= 0 && output.speechEnergy <= 1);
      assert(output.blink >= 0 && output.blink <= 1);
      return output;
    },
    run(frames, level = .06, options = {}) {
      for (let i = 0; i < frames; i++) this.step(level, options);
      return output;
    },
  };
}

// Silence/noise never fabricates jaw motion; both soft and loud real sound work.
const quiet = runner();
assert.equal(quiet.run(180, 0).mouthOpen, 0);
assert.equal(quiet.run(180, .005).mouthOpen, 0);
assert.equal(quiet.run(180, 0).accent, 0);
const soft = runner(), loud = runner();
const softMouth = soft.run(100, .02).mouthOpen;
const loudMouth = loud.run(100, .3).mouthOpen;
assert(softMouth > .12, 'quiet speech remains visible with bounded gain');
assert(loudMouth > softMouth && loudMouth <= .91, 'gain must not flatten every voice to full-open');
const syllable = runner();
assert(syllable.run(4, .13).mouthOpen > .5, 'short syllable opens promptly');
assert(syllable.run(12, 0).mouthOpen < .015, 'pause closes the mouth within 200 ms');
console.log('PASS: real speech amplitude, noise gate, adaptive quiet voice and fast syllable closure');

// With the same emotion, actual syllable energy changes the lip shape as well
// as the jaw. Silence and pause restore the unchanged emotional baseline.
const contour = runner('平静');
const baseline = contour.run(100, 0);
const softShape = contour.run(25, .025, {brightness:.8});
const loudShape = contour.run(8, .16, {brightness:.8});
assert(softShape.mouthForm > baseline.mouthForm + .003);
assert(softShape.mouthWidth > baseline.mouthWidth + .005);
assert(loudShape.mouthForm > softShape.mouthForm + .015, 'same emotion responds to real energy changes');
assert(loudShape.mouthWidth > softShape.mouthWidth + .02, 'lip width follows the syllable contour');
const silentShape = contour.run(25, 0);
assert(Math.abs(silentShape.mouthForm - baseline.mouthForm) < .001);
assert(Math.abs(silentShape.mouthWidth - baseline.mouthWidth) < .001);
contour.run(8, .16);
const pausedShape = contour.step(.16, {paused:true});
assert(Math.abs(pausedShape.mouthForm - baseline.mouthForm) < .001, 'pause immediately releases the speech-shape layer');
assert(Math.abs(pausedShape.mouthWidth - baseline.mouthWidth) < .001);
console.log('PASS: energy-driven lip shape and width, silence return and pause ownership');

// The same jaw opening must support distinct transverse/lip shapes. Brightness
// is a measured spectrum feature, not a phoneme label or another volume gain.
const darkSpectrum = runner(), brightSpectrum = runner();
let darkArticulation, brightArticulation;
const articulationSamples = [];
for (let i = 0; i < 360; i++) {
  darkArticulation = darkSpectrum.step(.1, {brightness:.05, intensity:.8});
  brightArticulation = brightSpectrum.step(.1, {brightness:.95, intensity:.8});
  assert.equal(darkArticulation.mouthOpen, brightArticulation.mouthOpen, 'spectrum must not alter jaw gain');
  assert.equal(darkArticulation.speechEnergy, brightArticulation.speechEnergy, 'body energy is still purely amplitude-driven');
  articulationSamples.push(brightArticulation);
}
const widthSpan = brightArticulation.mouthWidth - darkArticulation.mouthWidth;
assert(widthSpan > .55, 'default-strength speech has a visible narrow-to-wide span at identical jaw opening');
assert(darkArticulation.mouthWidth < -.2 && brightArticulation.mouthWidth > .35);
assert(darkArticulation.mouthRound > brightArticulation.mouthRound + .3);
assert(Math.min(...articulationSamples.map(s => s.mouthShift)) < -.15);
assert(Math.max(...articulationSamples.map(s => s.mouthShift)) > .15, 'mouth offset uses both sides, with no permanent slant');
let lastShape = darkArticulation;
for (let i = 0; i < 60; i++) {
  const next = darkSpectrum.step(.1, {brightness:.95, intensity:.8});
  assert(Math.abs(next.mouthWidth - lastShape.mouthWidth) < .09, 'spectral transitions are smooth at normal frame rates');
  assert(Math.abs(next.mouthRound - lastShape.mouthRound) < .06);
  assert(Math.abs(next.mouthShift - lastShape.mouthShift) < .02);
  lastShape = next;
}
assert(lastShape.mouthWidth > .35);
console.log(`PASS: same-jaw spectral articulation, width span ${widthSpan.toFixed(3)}, two-sided lip offset and smooth transitions`);

// With no spectrum, actual voiced episodes and clip progress still change
// shape. This bounded fallback never manufactures jaw movement during silence.
const fallbackShape = runner();
const episodeShapes = [];
for (let episode = 0; episode < 6; episode++) {
  episodeShapes.push(fallbackShape.run(30, .1, {brightness:null, intensity:.8}));
  const gap = fallbackShape.run(30, 0, {brightness:null, intensity:.8});
  assert.equal(gap.mouthOpen, 0);
  assert.equal(gap.mouthRound, 0);
  assert.equal(gap.mouthShift, 0);
  assert(Math.abs(gap.mouthWidth - .016) < .001, 'silence returns to the scaled static profile');
}
const episodeRange = key => Math.max(...episodeShapes.map(s => s[key])) - Math.min(...episodeShapes.map(s => s[key]));
assert(episodeRange('mouthOpen') < .001, 'equal voiced episode levels retain the same jaw opening');
assert(episodeRange('mouthWidth') > .25 && episodeRange('mouthRound') > .12,
  'separate real voiced episodes do not repeat a single vertical jaw shape');
for (const badBrightness of [NaN, Infinity, -Infinity, undefined, -9, 8])
  fallbackShape.step(.1, {brightness:badBrightness, intensity:1.6});
console.log('PASS: bounded missing-spectrum articulation, distinct voiced episodes and silent reset');

// Pause/ownership release new channels immediately. Stalls and clock jumps do
// not advance the contour or replay a backlog of absent speech movements.
const articulationGuard = runner();
articulationGuard.run(250, .1, {brightness:.05});
const pausedArticulation = articulationGuard.step(.1, {brightness:.95, paused:true});
assert.equal(pausedArticulation.mouthRound, 0);
assert.equal(pausedArticulation.mouthShift, 0);
assert(Math.abs(pausedArticulation.mouthWidth - .02) < .001);
for (let i = 0; i < 10; i++) assert.equal(articulationGuard.step(.1, {brightness:.05}).accent, 0);
const stableArticulation = articulationGuard.run(100, .1, {brightness:.05});
const frozenMedia = articulationGuard.media;
for (let i = 0; i < 10; i++) {
  const held = articulationGuard.step(.1, {brightness:.95, mediaTime:frozenMedia});
  assert.equal(held.mouthRound, stableArticulation.mouthRound, 'buffering freezes spectral shape');
  assert.equal(held.mouthShift, stableArticulation.mouthShift, 'buffering freezes fallback phase');
}
const seekShape = articulationGuard.step(.1, {brightness:.95, seek:4});
assert.equal(seekShape.mouthRound, stableArticulation.mouthRound);
assert.equal(seekShape.mouthShift, stableArticulation.mouthShift);
const backgroundShape = articulationGuard.step(.1, {brightness:.95, jump:120000});
assert.equal(backgroundShape.mouthRound, seekShape.mouthRound);
assert.equal(backgroundShape.mouthShift, seekShape.mouthShift);
for (const badNow of [NaN, -1, Infinity]) {
  const held = articulationGuard.face.frame(badNow,
    {playing:true, level:.1, mediaTime:articulationGuard.media, brightness:.95});
  assert.equal(held.mouthRound, backgroundShape.mouthRound);
  assert.equal(held.mouthShift, backgroundShape.mouthShift);
}
const resumeShape = articulationGuard.step(.1, {brightness:.95});
assert(Math.abs(resumeShape.mouthRound - backgroundShape.mouthRound) < .06);
assert(Math.abs(resumeShape.mouthShift - backgroundShape.mouthShift) < .02);
const disabledArticulation = articulationGuard.step(.1, {brightness:.05, intensity:0});
for (const key of ['mouthWidth','mouthRound','mouthShift']) assert.equal(disabledArticulation[key], 0);
assert(disabledArticulation.mouthOpen > .3 && disabledArticulation.speechEnergy > .3);
articulationGuard.run(30, .1, {brightness:.05});
articulationGuard.face.begin({turnId:2, explicitFace:true}, articulationGuard.now);
const ownedArticulation = articulationGuard.step(.1, {brightness:.05});
for (const key of ['mouthWidth','mouthRound','mouthShift']) assert.equal(ownedArticulation[key], 0);
assert(ownedArticulation.mouthOpen > .3 && ownedArticulation.speechEnergy > .3);
console.log('PASS: articulation pause, explicit/off priority, stalled media and invalid/seek/background clock guards');

// Ordinary neutral speech is expressive even without a detected accent. The
// broad phrase contour moves the brows slowly, while the lips track syllables.
const neutralPhrase = runner('平静');
const ordinary = neutralPhrase.run(160, .06, {eyeIntensity:0, brightness:.75});
assert.equal(ordinary.accent, 0);
assert(ordinary.browL >= .075 && ordinary.browL <= .12, 'neutral talking brow has a visible but bounded lift');
assert(ordinary.browR >= .07 && ordinary.browR <= .12);
assert(ordinary.mouthForm >= .04 && ordinary.mouthWidth >= .055);
assert(ordinary.eyeOpenL > .995 && ordinary.eyeOpenL < 1.02, 'speech gently engages the eyelids');
const subtleSyllables = [];
for (let i = 0; i < 120; i++) {
  const sample = neutralPhrase.step(i % 6 < 3 ? .055 : .065, {brightness:.75});
  assert.equal(sample.accent, 0, 'small normal syllables are not forced emphasis');
  subtleSyllables.push(sample);
}
const range = (samples, key) => Math.max(...samples.map(s => s[key])) - Math.min(...samples.map(s => s[key]));
assert(range(subtleSyllables.slice(60), 'browL') < .0045, 'brows smooth over fast syllables');
assert(range(subtleSyllables.slice(60), 'mouthWidth') > .005, 'the lips remain responsive while brows stay calm');
for (let i = 0; i < 60; i++) neutralPhrase.step(.06 - .0006 * i);
const lowerPhrase = neutralPhrase.run(50, .024);
assert(lowerPhrase.browL < ordinary.browL - .025, 'the slower phrase contour still responds to a quieter sentence');
const neutralRest = neutralPhrase.run(80, 0);
assert.equal(neutralRest.browL, 0);
assert.equal(neutralRest.browR, 0);
assert(Math.abs(neutralRest.mouthForm - .13) < .001);
assert(Math.abs(neutralRest.eyeSmile - .04) < .001, 'neutral silence keeps only the default smile');
neutralPhrase.run(60, .09);
const phrasePaused = neutralPhrase.step(.09, {paused:true});
assert.equal(phrasePaused.browL, 0);
assert.equal(phrasePaused.browR, 0);
assert.equal(phrasePaused.speechEnergy, 0);
console.log('PASS: visible neutral speech without accents, slow phrase brows, responsive lips and gently smiling rest');

// Emphasis responds to energy changes, never to a free-running sine or timer.
const rhythm = runner();
let pulses = 0, inPulse = false, pulseTimes = [];
for (let i = 0; i < 240; i++) {
  const frame = rhythm.step(i % 20 < 4 ? .18 : .025);
  if (frame.accent > 0 && !inPulse) { pulses++; pulseTimes.push(rhythm.media); }
  inPulse = frame.accent > 0;
}
assert(pulses >= 2 && pulses <= 4, 'lively speech has restrained emphasis');
for (let i = 1; i < pulseTimes.length; i++) assert(pulseTimes[i] - pulseTimes[i - 1] >= 1.24);
const steady = runner();
for (let i = 0; i < 240; i++) assert.equal(steady.step(.1).accent, 0, 'steady tone has no invented emphasis');
rhythm.run(20, .12, {paused:true});
assert(rhythm.step(.2, {paused:true}).mouthOpen < .002, 'pause closes mouth despite stale loud level');
for (let i = 0; i < 10; i++) assert.equal(rhythm.step(.2).accent, 0, 'resume must not manufacture an onset');
assert.equal(rhythm.step(.22, {seek:8}).accent, 0, 'seek does not replay accents');
assert.equal(rhythm.step(.22, {jump:120000}).accent, 0, 'background restore does not replay accents');
console.log('PASS: emphasis cooldown, steady audio, pause/resume and discontinuity guards');

// Natural blinks remain without speech. A pause blink cannot turn into flapping.
const idle = runner();
let blinkCount = 0, wasClosed = false;
for (let i = 0; i < 700; i++) {
  const out = idle.step(0, {playing:false, intensity:0});
  const closed = out.eyeOpenL < .1;
  if (closed && !wasClosed) blinkCount++;
  wasClosed = closed;
  assert.equal(out.accent, 0);
}
assert(blinkCount >= 2 && blinkCount <= 4, 'base blink continues during silence and zero intensity');
const pauses = runner();
let lastBlink = -Infinity;
wasClosed = false;
for (let i = 0; i < 400; i++) {
  const out = pauses.step(i % 40 < 15 ? .08 : 0);
  const closed = out.eyeOpenL < .1;
  if (closed && !wasClosed) {
    assert(pauses.now - lastBlink >= 1600, 'short repeated gaps do not spam blinks');
    lastBlink = pauses.now;
  }
  wasClosed = closed;
}
console.log('PASS: ordinary idle blinking and bounded speech-pause blink cadence');

// Emotional tones are distinct yet restrained; long replies do not lose their face.
const signatures = new Set();
for (const emotion of Object.keys(Face.poses)) {
  const r = runner(emotion);
  const out = r.run(500, .06);
  assert(Math.abs(out.mouthForm) <= .26 && Math.abs(out.browL) <= .16 && Math.abs(out.browR) <= .16);
  signatures.add([out.mouthForm, out.browL, out.browR, out.eyeSmile].map(v => v.toFixed(3)).join(','));
}
assert.equal(signatures.size, Object.keys(Face.poses).length);
const segments = runner('开心');
const before = segments.run(250, .025);
const gainBefore = segments.face.referenceLevel;
segments.face.end({completed:true}, segments.now);
segments.run(4, 0, {playing:false});
segments.face.begin({turnId:1, segmentIndex:1, emotion:'开心'}, segments.now);
assert.equal(segments.face.referenceLevel, gainBefore, 'same reply preserves learned loudness');
const after = segments.step(.025, {mediaTime:0});
assert(after.mouthForm > .15 && Math.abs(after.mouthForm - before.mouthForm) < .02,
  'segment boundary preserves emotional pose while the lips reopen for actual audio');
segments.face.end({completed:false}, segments.now);
const rest = segments.run(150, 0, {playing:false});
assert(Math.abs(rest.mouthForm - .13) < .003 && Math.abs(rest.eyeSmile - .04) < .002
  && rest.mouthOpen === 0, 'interruption returns smoothly to the default smile');
console.log('PASS: distinct emotion profiles, long replies, same-turn continuity and smiling ending fade');

const explicit = runner('开心', {explicitFace:true});
const explicitOut = explicit.run(70, .1);
assert(explicitOut.mouthOpen > .3, 'manual expression does not silence lipsync');
for (const key of ['mouthForm','mouthWidth','mouthRound','mouthShift','browL','browR','eyeSmile','accent']) assert.equal(explicitOut[key], 0, key);
const off = runner('惊讶');
const offOut = off.run(100, .1, {intensity:0});
assert(offOut.mouthOpen > .3);
for (const key of ['mouthForm','mouthWidth','mouthRound','mouthShift','browL','browR','eyeSmile','accent']) assert.equal(offOut[key], 0, key);
for (const level of [NaN, Infinity, -1, undefined]) off.step(level, {intensity:NaN, mediaTime:NaN});
const badClock = off.face.frame(NaN, {level:Infinity, mediaTime:Infinity, intensity:Infinity});
for (const value of Object.values(badClock)) assert(Number.isFinite(value));
console.log('PASS: explicit-expression ownership, zero-intensity lipsync and invalid-input bounds');

// Speech energy is an audio signal, independent of the face styling controls.
const bodyNormal = runner('平静'), bodyOff = runner('平静'), bodyExplicit = runner('平静', {explicitFace:true});
for (let i = 0; i < 80; i++) {
  const level = i % 20 < 12 ? .08 : .035;
  const normal = bodyNormal.step(level);
  const off = bodyOff.step(level, {intensity:0});
  const explicit = bodyExplicit.step(level);
  assert.equal(off.speechEnergy, normal.speechEnergy);
  assert.equal(explicit.speechEnergy, normal.speechEnergy);
}
assert(bodyOff.step(.1, {intensity:0}).speechEnergy > .3);
assert(bodyExplicit.step(.1).speechEnergy > .3);
assert.equal(bodyOff.step(.1, {intensity:0, paused:true}).speechEnergy, 0);
assert.equal(bodyExplicit.step(.1, {paused:true}).speechEnergy, 0);
bodyNormal.run(50, 0);
assert.equal(bodyNormal.step(0).speechEnergy, 0);
assert.equal(bodyNormal.step(.1, {playing:false}).speechEnergy, 0);
console.log('PASS: independent bounded speech energy for motion, including face-off, explicit faces and silence');

// Eyelids slowly relax below the open limit instead of remaining pinned to it.
const eyelids = runner('平静', {}, () => .8);
const eyeSamples = [];
let previousEye;
for (let i = 0; i < 750; i++) {
  const out = eyelids.step(0, {playing:false});
  if (out.blink === 1) {
    eyeSamples.push(out);
    assert(out.eyeOpenL >= .942 && out.eyeOpenR >= .942);
    assert(out.eyeOpenL <= .985 && out.eyeOpenR <= .985);
    assert(Math.abs(out.eyeOpenL - out.eyeOpenR) <= .0091, 'asymmetry remains too small to become a wink');
    if (previousEye?.blink === 1)
      assert(Math.abs(out.eyeOpenL - previousEye.eyeOpenL) < .0015, 'relaxation must not jitter each frame');
  }
  previousEye = out;
}
assert(range(eyeSamples, 'eyeOpenL') > .015, 'eyes subtly change between ordinary blinks');
assert(eyeSamples.some(out => Math.abs(out.eyeOpenL - out.eyeOpenR) > .003));
console.log('PASS: slow downward eyelid relaxation, small asymmetry and bounded transitions');

function blinkTrack(random, options = () => ({}), frames = 1200, extra = {}) {
  const r = runner('平静', extra, random);
  const starts = [], durations = [];
  let wasBlinking = false, start = 0;
  for (let i = 0; i < frames; i++) {
    const out = r.step(0, {playing:false, ...options(i)});
    const blinking = out.blink < .99;
    if (blinking && !wasBlinking) { start = r.now; starts.push(start); }
    if (!blinking && wasBlinking) durations.push(r.now - start);
    wasBlinking = blinking;
  }
  return {starts, durations};
}

// Force the rare random branch to audit the hard pair limit and cooldown.
const paired = blinkTrack(() => 0, () => ({}), 2200);
let pairs = 0, lastPair = -Infinity, previousWasFollowup = false;
for (let i = 1; i < paired.starts.length; i++) {
  const gap = paired.starts[i] - paired.starts[i - 1];
  const followup = gap < 750;
  if (followup) {
    assert(gap >= 220 && gap <= 550, 'second blink follows a short, visible reopening');
    assert(!previousWasFollowup, 'one pair cannot turn into a triple blink');
    assert(paired.starts[i - 1] - lastPair >= 12000, 'double-blink pairs have a long cooldown');
    lastPair = paired.starts[i - 1];
    pairs++;
  } else assert(gap >= 1600, 'separate main blinks retain their minimum interval');
  previousWasFollowup = followup;
}
assert(pairs >= 2 && pairs <= 3, 'forced probability still permits only a few pairs');
let randomIndex = 0;
const varied = blinkTrack(() => [0,.2,.85,.45,1,.6,.3][randomIndex++ % 7], () => ({eyeIntensity:0}), 1800);
assert(new Set(varied.durations).size >= 2, 'ordinary blink timing is not identical every time');
assert(varied.durations.every(duration => duration >= 144 && duration <= 288));
console.log('PASS: occasional controlled double blinks, no triples, pair cooldown and varied blink timing');

// Gaze is a consumed rising-edge cue, sharing the gate with speech pauses.
const heldGaze = blinkTrack(() => .5, i => ({gazeBlink:i >= 2}), 200);
assert.equal(heldGaze.starts.length, 1, 'holding the gaze request cannot retrigger each eligible frame');
const cueUnion = runner();
let cueBlinks = 0, wasCueBlinking = false;
for (let i = 0; i < 80; i++) {
  const out = cueUnion.step(i < 20 ? .08 : 0, {gazeBlink:i === 32});
  if (out.blink < .99 && !wasCueBlinking) cueBlinks++;
  wasCueBlinking = out.blink < .99;
}
assert.equal(cueBlinks, 1, 'pause and gaze requests must not create overlapping blinks');
for (const options of [{paused:true}, {eyeIntensity:0}, {intensity:0}]) {
  const blockedCue = blinkTrack(() => 0, () => ({gazeBlink:true, ...options}), 90);
  assert.equal(blockedCue.starts.length, 0, 'disabled styling does not trigger gaze blinks');
  const basic = blinkTrack(() => 0, () => ({gazeBlink:true, ...options}), 800);
  assert(basic.starts.length >= 3, 'base blinks continue with cues disabled');
  for (let i = 1; i < basic.starts.length; i++) assert(basic.starts[i] - basic.starts[i - 1] >= 1600);
}
const explicitBlinks = blinkTrack(() => 0, () => ({gazeBlink:true}), 800, {explicitFace:true});
assert(explicitBlinks.starts[0] >= 1900, 'explicit expression suppresses the early gaze cue');
for (let i = 1; i < explicitBlinks.starts.length; i++)
  assert(explicitBlinks.starts[i] - explicitBlinks.starts[i - 1] >= 1600, 'explicit expression permits no double blink');
console.log('PASS: consumed gaze requests, shared pause gate and base blinking with styling disabled');

// Zero eye styling keeps the prior emotional eye shape and every audio signal.
const plainEyes = runner('委屈'), styledEyes = runner('委屈');
for (let i = 0; i < 100; i++) {
  const plain = plainEyes.step(.07, {eyeIntensity:0});
  const styled = styledEyes.step(.07);
  for (const key of ['mouthOpen','mouthForm','mouthWidth','mouthRound','mouthShift','browL','browR','speechEnergy'])
    assert.equal(plain[key], styled[key], 'eye intensity must not change speech or face signals');
  assert(plain.eyeOpenL < .98, 'disabling micro-motions keeps the existing downcast eye shape');
}
const explicitEyes = runner('开心', {explicitFace:true}, () => .8);
const pausedEyes = runner('平静', {}, () => .8);
for (let i = 0; i < 160; i++) {
  const explicitEye = explicitEyes.step(.08);
  const pausedEye = pausedEyes.step(.08, {paused:true});
  if (explicitEye.blink === 1) assert.equal(explicitEye.eyeOpenL, .98);
  if (pausedEye.blink === 1) assert.equal(pausedEye.eyeOpenL, .98);
}
const cancelledPair = blinkTrack(() => 0, i => ({paused:i >= 136}), 210);
assert.equal(cancelledPair.starts.length, 1, 'pausing between the two blinks cancels the queued followup');
const backgroundPair = blinkTrack(() => 0, i => i === 136 ? {jump:60000} : {}, 210);
assert.equal(backgroundPair.starts.length, 1, 'background restoration drops the pending pair rather than replaying it');
const brokenClock = runner('平静', {}, () => .8);
const beforeClock = brokenClock.run(150, 0, {playing:false});
for (const badNow of [NaN, -100, Infinity]) {
  const out = brokenClock.face.frame(badNow, {playing:false, gazeBlink:true});
  for (const value of Object.values(out)) assert(Number.isFinite(value));
  assert.equal(out.blink, beforeClock.blink, 'invalid time cannot progress a blink');
  assert.equal(out.eyeOpenL, beforeClock.eyeOpenL, 'invalid time cannot jump an already-relaxed eyelid');
  assert.equal(out.eyeOpenR, beforeClock.eyeOpenR);
}
for (const badIntensity of [NaN, Infinity, -1, 5]) {
  const out = brokenClock.step(0, {playing:false, eyeIntensity:badIntensity});
  assert(out.eyeOpenL >= 0 && out.eyeOpenL <= 1.15);
}
console.log('PASS: eye styling isolation, explicit priority, cancelled double blinks and invalid/background clocks');

// Releasing a native expression must restore ambient eyes without exposing the
// previous hidden emotion for one grace period or waiting for the next reply.
const releasedFace = runner('开心', {}, () => .8);
releasedFace.run(80, .08);
releasedFace.face.begin({turnId:2, emotion:'开心', explicitFace:true}, releasedFace.now);
releasedFace.run(2, .08);
releasedFace.face.end({completed:true}, releasedFace.now);
let resumedIdleEyes = false;
for (let i = 0; i < 100; i++) {
  const out = releasedFace.step(0, {playing:false});
  for (const key of ['browL','browR']) assert.equal(out[key], 0, 'explicit completion cannot restore stale emotion');
  assert(out.mouthForm >= 0 && out.mouthForm <= .13, 'explicit completion fades only to the default smile');
  assert(out.mouthWidth >= 0 && out.mouthWidth <= .02);
  assert(out.eyeSmile >= 0 && out.eyeSmile <= .04);
  if (i === 0) assert(out.mouthForm < .004, 'native expression release must not jump to a smiling pose');
  if (out.blink === 1 && out.eyeOpenL < .97) resumedIdleEyes = true;
}
assert(resumedIdleEyes, 'idle eyelid styling resumes after explicit end without another begin');
console.log('PASS: explicit-expression release restores idle eyes without a held-emotion flash');

// Loading the page requires no first utterance to obtain its resting expression.
const firstIdle = new Face({random:() => .5});
const firstIdleFrame = firstIdle.frame(0, {playing:false});
assert(firstIdleFrame.mouthForm >= .12 && firstIdleFrame.mouthForm <= .14);
assert(firstIdleFrame.eyeSmile >= .035 && firstIdleFrame.eyeSmile <= .045);
assert(firstIdleFrame.mouthWidth <= .025 && firstIdleFrame.mouthOpen === 0);
assert.equal(firstIdleFrame.speechEnergy, 0);
const disabledSmile = firstIdle.frame(16, {playing:false, intensity:0});
for (const key of ['mouthForm','mouthWidth','mouthRound','mouthShift','eyeSmile']) assert.equal(disabledSmile[key], 0);
console.log('PASS: true first-load idle smile, no fabricated audio and intensity-off priority');

// Negative speech replaces the resting smile instead of receiving it as an
// additive layer. Native negative expressions still own every styled parameter.
for (const emotion of ['委屈','伤心','难过','生气','愤怒']) {
  const negative = runner(emotion);
  const spoken = negative.run(200, .06);
  assert(spoken.mouthForm < -.015, `${emotion} speech must not carry a default smile`);
  assert(spoken.eyeSmile < .0001, `${emotion} must not inherit smiling eyes`);
  const silent = negative.run(60, 0);
  assert(silent.mouthForm < -.07 && silent.eyeSmile < .0001);
  negative.face.end({completed:true}, negative.now);
  let previous = silent.mouthForm;
  for (let i = 0; i < 450; i++) {
    const out = negative.step(0, {playing:false});
    assert(Math.abs(out.mouthForm - previous) < .01, 'negative emotion returns to rest gradually');
    if (i < 125) {
      assert(out.mouthForm <= 0, `${emotion} settles quietly before the default smile returns`);
      assert(out.eyeSmile < .0001, `${emotion} has no smiling eye layer during its quiet afterglow`);
    }
    previous = out.mouthForm;
  }
  assert(Math.abs(previous - .13) < .002, 'finished negative emotion releases to the same resting smile');
}
const nativeAngry = runner('生气', {explicitFace:true});
const nativeAngryFrame = nativeAngry.run(100, .1);
assert.equal(nativeAngryFrame.mouthForm, 0);
assert.equal(nativeAngryFrame.eyeSmile, 0);
assert(nativeAngryFrame.mouthOpen > .3 && nativeAngryFrame.speechEnergy > .3);
console.log('PASS: sad/angry expression priority, smooth return to mild smile and explicit negative ownership');

// Completed speech carries a softer, bounded expression for a few seconds.
// Surprise recovers quickly; positive warmth and negative quiet have distinct
// contours. None of these silent states generate speech or move the jaw.
const happyGlow = runner('开心'), surpriseGlow = runner('惊讶');
const happySpoken = happyGlow.run(200, 0);
surpriseGlow.run(200, 0);
happyGlow.face.end({completed:true}, happyGlow.now);
surpriseGlow.face.end({completed:true}, surpriseGlow.now);
const happyOneSecond = happyGlow.run(63, 0, {playing:false});
const surpriseOneSecond = surpriseGlow.run(63, 0, {playing:false});
assert(happyOneSecond.mouthForm > .15 && happyOneSecond.mouthForm < happySpoken.mouthForm - .015,
  'happy completion retains a shallower smile, not the full talking expression');
assert(happyOneSecond.eyeSmile > .055 && happyOneSecond.browL > .014);
assert(surpriseOneSecond.browL > .01 && surpriseOneSecond.browL < .04,
  'surprised eyebrows soften quickly instead of remaining wide-eyed');
const happyTwoSeconds = happyGlow.run(62, 0, {playing:false});
const surpriseTwoSeconds = surpriseGlow.run(62, 0, {playing:false});
assert(happyTwoSeconds.mouthForm < happyOneSecond.mouthForm && happyTwoSeconds.mouthForm > .145);
assert(surpriseTwoSeconds.browL < happyTwoSeconds.browL * .4,
  'surprise resolves before the happy afterglow');
for (const out of [happyOneSecond, surpriseOneSecond, happyTwoSeconds, surpriseTwoSeconds]) {
  assert.equal(out.mouthOpen, 0);
  assert.equal(out.speechEnergy, 0);
  assert.equal(out.accent, 0);
}
const happyRest = happyGlow.run(220, 0, {playing:false});
assert(Math.abs(happyRest.mouthForm - .13) < .002 && Math.abs(happyRest.eyeSmile - .04) < .001);
console.log('PASS: softer positive afterglow, fast surprise recovery and silent return to rest');

// New input may arrive before the host's delayed end callback. It must cancel
// both an existing glow and that future glow without affecting actual lipsync.
const delayedRelease = runner('开心'), jawControl = runner('开心');
delayedRelease.run(120, .09);
jawControl.run(120, .09);
delayedRelease.face.clearAfterglow();
for (let i = 0; i < 20; i++) {
  const actual = delayedRelease.step(.09), expected = jawControl.step(.09);
  assert.equal(actual.mouthOpen, expected.mouthOpen, 'user input cannot interfere with an already playing jaw');
  assert.equal(actual.speechEnergy, expected.speechEnergy);
  assert.equal(actual.mouthForm, expected.mouthForm, 'a still-playing reply keeps its speaking emotion');
}
delayedRelease.run(55, 0, {playing:false});
delayedRelease.face.end({completed:true}, delayedRelease.now);
const cancelledGlow = delayedRelease.run(90, 0, {playing:false});
assert.equal(delayedRelease.face.afterglow, null, 'a delayed release must not resurrect the old expression');
assert(Math.abs(cancelledGlow.mouthForm - .13) < .002 && cancelledGlow.browL < .001);
delayedRelease.face.begin({turnId:2, emotion:'开心'}, delayedRelease.now);
delayedRelease.run(100, .06);
delayedRelease.face.end({completed:true}, delayedRelease.now);
assert(delayedRelease.run(65, 0, {playing:false}).mouthForm > .15, 'a new reply regains normal afterglow eligibility');
delayedRelease.face.clearAfterglow();
assert.equal(delayedRelease.face.afterglow, null);
assert(Math.abs(delayedRelease.run(170, 0, {playing:false}).mouthForm - .13) < .001);
console.log('PASS: new-input cancellation survives delayed completion and preserves real speech channels');

// Listening interrupts the old pose even while the host still considers the
// old segment active. A new reply overrides the lingering mood immediately.
const listeningGlow = runner('开心');
listeningGlow.run(180, .06);
const listeningRest = listeningGlow.run(170, 0, {playing:false, listening:true});
assert(Math.abs(listeningRest.mouthForm - .13) < .001 && listeningRest.browL < .001);
listeningGlow.face.end({completed:true}, listeningGlow.now);
assert.equal(listeningGlow.face.afterglow, null);
const replacedGlow = runner('委屈');
replacedGlow.run(180, 0);
replacedGlow.face.end({completed:true}, replacedGlow.now);
replacedGlow.run(50, 0, {playing:false});
replacedGlow.face.begin({turnId:2, emotion:'开心'}, replacedGlow.now);
assert.equal(replacedGlow.face.afterglow, null);
assert(replacedGlow.run(150, 0).mouthForm > .185, 'the new reply takes over a former negative mood');
replacedGlow.face.end({completed:false}, replacedGlow.now);
assert.equal(replacedGlow.face.afterglow, null);
assert(Math.abs(replacedGlow.run(170, 0, {playing:false}).mouthForm - .13) < .001);
console.log('PASS: listening hand-off, new-emotion takeover and interruption cleanup');

// Deadlines expire in wall time at low FPS, when styling is disabled, and when
// a background tab resumes. Duplicate completion cannot extend a finished mood.
for (const jump of [16, 50, 100, 200]) {
  const timed = runner('开心');
  timed.run(180, 0);
  timed.face.end({completed:true}, timed.now);
  const deadline = timed.face.afterglow.startedAt;
  timed.run(Math.ceil(1000 / jump), 0, {playing:false, jump});
  timed.face.end({completed:true}, timed.now);
  assert.equal(timed.face.afterglow.startedAt, deadline);
  const settled = timed.run(Math.ceil(6000 / jump), 0, {playing:false, jump});
  assert.equal(timed.face.afterglow, null);
  assert(Math.abs(settled.mouthForm - .13) < .002, 'deadline is not stretched by a slow rendering clock');
}
const hiddenGlow = runner('委屈');
hiddenGlow.run(180, 0);
hiddenGlow.face.end({completed:true}, hiddenGlow.now);
hiddenGlow.run(30, 0, {playing:false});
const resumedGlow = hiddenGlow.step(0, {playing:false, jump:120000});
assert.equal(hiddenGlow.face.afterglow, null);
assert(Math.abs(resumedGlow.mouthForm - .13) < .0001, 'expired mood cannot flash when a hidden tab returns');
for (const disabled of [{intensity:0}, {performance:0}]) {
  const controlled = runner('开心');
  controlled.run(150, .06);
  controlled.face.end({completed:true}, controlled.now);
  for (let i = 0; i < 430; i++) {
    const out = controlled.step(0, {playing:false, ...disabled});
    for (const key of ['mouthForm','mouthWidth','mouthRound','mouthShift','browL','browR','eyeSmile'])
      assert.equal(out[key], 0, `${key} afterglow obeys the existing style-off switch`);
  }
  assert.equal(controlled.face.afterglow, null, 'disabled styling cannot pause the emotion deadline');
  const enabled = controlled.run(180, 0, {playing:false});
  assert(Math.abs(enabled.mouthForm - .13) < .001 && enabled.browL < .001);
}
console.log('PASS: wall-clock expiry, duplicate-end guard, hidden-tab decay and style-off priority');

// Performance is a separate, backward-compatible gain, not a way to push the
// existing face slider into its cap. It must leave all actual audio signals alone.
const defaultPerformance = runner(), unitPerformance = runner(), boldPerformance = runner();
const performanceSamples = {base:[], bold:[]};
for (let i = 0; i < 360; i++) {
  const input = {brightness:.05, intensity:.8};
  const normal = defaultPerformance.step(.1, input);
  const unit = unitPerformance.step(.1, {...input, performance:1});
  const bold = boldPerformance.step(.1, {...input, performance:1.8});
  assert.deepEqual(unit, normal, 'omitted performance is exactly the old unit performance');
  assert.equal(bold.mouthOpen, normal.mouthOpen, 'performance cannot invent a louder voice');
  assert.equal(bold.speechEnergy, normal.speechEnergy, 'body energy remains independent of facial performance');
  assert.equal(bold.blink, normal.blink, 'performance does not speed up basic blink rhythm');
  performanceSamples.base.push(normal);
  performanceSamples.bold.push(bold);
}
const unitFrame = performanceSamples.base.at(-1), boldFrame = performanceSamples.bold.at(-1);
for (const key of ['mouthForm','mouthWidth','mouthRound','browL','browR','eyeSmile']) {
  const gain = Math.abs(boldFrame[key] / unitFrame[key]);
  assert(gain >= 1.6 && gain <= 1.81, `${key} should visibly increase near the requested 1.8 performance`);
}
const shiftGain = range(performanceSamples.bold, 'mouthShift') / range(performanceSamples.base, 'mouthShift');
assert(shiftGain > 1.7 && shiftGain <= 1.81);
const neutralEyePairs = performanceSamples.base.map((base, index) => ({base, bold:performanceSamples.bold[index]}))
  .filter(({base}) => base.blink === 1);
assert(neutralEyePairs.some(({base, bold}) => bold.eyeOpenL < base.eyeOpenL - .025),
  'smiling eyes combine smile and lid closure instead of adding a geometrically ineffective EyeSmile alone');
console.log(`PASS: independent performance gain, mouth width ${unitFrame.mouthWidth.toFixed(3)} to ${boldFrame.mouthWidth.toFixed(3)}, round ${unitFrame.mouthRound.toFixed(3)} to ${boldFrame.mouthRound.toFixed(3)}, shift gain ${shiftGain.toFixed(3)}`);

// The upper setting stays inside native ranges without hard-clipping all bright
// and dark shapes to a single maximum. The same RMS must still give distinct shapes.
const maximumShapes = [];
for (const brightness of [0, .25, .5, .75, 1]) {
  const maximum = runner('开心');
  maximumShapes.push(maximum.run(300, .3, {brightness, intensity:1.6, performance:3}));
}
for (let i = 1; i < maximumShapes.length; i++) {
  assert.equal(maximumShapes[i].mouthOpen, maximumShapes[0].mouthOpen);
  assert(maximumShapes[i].mouthWidth > maximumShapes[i - 1].mouthWidth + .01,
    'maximum performance preserves ordering and distinction between spectral shapes');
  assert(maximumShapes[i].mouthRound < maximumShapes[i - 1].mouthRound - .005);
}
assert(maximumShapes.every(out => out.mouthWidth > -1 && out.mouthWidth < 1));
assert(maximumShapes.every(out => out.mouthRound < .95 && Math.abs(out.mouthShift) < .7));
const boundaryPerformance = runner(), referencePerformance = runner();
for (const performance of [NaN, Infinity, -Infinity, undefined]) {
  const actual = boundaryPerformance.step(.1, {brightness:.4, performance});
  const expected = referencePerformance.step(.1, {brightness:.4, performance:1});
  assert.deepEqual(actual, expected, 'invalid performance safely retains the unit default');
}
const highPerformance = runner(), cappedPerformance = runner();
assert.deepEqual(highPerformance.run(200, .1, {performance:100}), cappedPerformance.run(200, .1, {performance:3}));
console.log('PASS: maximum performance native bounds, distinct spectrum shapes and invalid-setting fallback');

// Zero disables the whole added facial performance, but preserves the real jaw
// and basic blink. Explicit asset ownership and the existing face switch win too.
const performanceZero = runner('生气', {}, () => 0), styleZero = runner('生气', {}, () => 0);
let zeroBlinkCount = 0, zeroWasClosed = false;
for (let i = 0; i < 650; i++) {
  const out = performanceZero.step(.1, {performance:0, brightness:.05, gazeBlink:true});
  const plain = styleZero.step(.1, {intensity:0, gazeBlink:true});
  for (const key of ['mouthForm','mouthWidth','mouthRound','mouthShift','browL','browR','eyeSmile','accent'])
    assert.equal(out[key], 0, `zero performance suppresses ${key}`);
  assert.equal(out.mouthOpen, plain.mouthOpen);
  assert.equal(out.speechEnergy, plain.speechEnergy);
  assert.equal(out.blink, plain.blink);
  const closed = out.blink < .1;
  if (closed && !zeroWasClosed) zeroBlinkCount++;
  zeroWasClosed = closed;
}
assert(zeroBlinkCount >= 3 && zeroBlinkCount <= 5);
const takeover = runner('委屈');
takeover.run(150, .1, {performance:3, brightness:.05});
const switchedOff = takeover.step(.1, {performance:3, intensity:0});
for (const key of ['mouthForm','mouthWidth','mouthRound','mouthShift','browL','browR','eyeSmile'])
  assert.equal(switchedOff[key], 0);
takeover.face.begin({turnId:2, emotion:'生气', explicitFace:true}, takeover.now);
const nativeTakeover = takeover.step(.1, {performance:3});
for (const key of ['mouthForm','mouthWidth','mouthRound','mouthShift','browL','browR','eyeSmile','accent'])
  assert.equal(nativeTakeover[key], 0);
assert(nativeTakeover.mouthOpen > .3 && nativeTakeover.speechEnergy > .3);
console.log('PASS: performance-zero real lipsync/basic blinking, explicit priority and immediate face-off release');

// Strong styling still stops its speech layer at a real pause, remains frozen
// during buffering/clock jumps and cannot turn a negative emotion into a smile.
const boldGuard = runner();
const beforeBoldStall = boldGuard.run(300, .1, {brightness:.05, performance:1.8});
const heldBoldMedia = boldGuard.media;
for (let i = 0; i < 20; i++) {
  const held = boldGuard.step(.1, {brightness:.95, performance:1.8, mediaTime:heldBoldMedia});
  assert.equal(held.mouthRound, beforeBoldStall.mouthRound);
  assert.equal(held.mouthShift, beforeBoldStall.mouthShift);
}
const boldJump = boldGuard.step(.1, {brightness:.95, performance:1.8, seek:8, jump:60000});
assert.equal(boldJump.mouthRound, beforeBoldStall.mouthRound);
assert.equal(boldJump.mouthShift, beforeBoldStall.mouthShift);
assert.equal(boldJump.accent, 0);
const boldPaused = boldGuard.step(.1, {performance:1.8, paused:true});
assert.equal(boldPaused.mouthRound, 0);
assert.equal(boldPaused.mouthShift, 0);
assert.equal(boldPaused.browL, 0);
assert.equal(boldPaused.speechEnergy, 0);
const boldNoise = boldGuard.run(100, .005, {performance:3});
assert.equal(boldNoise.mouthOpen, 0);
assert.equal(boldNoise.mouthRound, 0);
assert.equal(boldNoise.mouthShift, 0);
assert.equal(boldNoise.browL, 0);
for (const emotion of ['委屈','生气']) {
  const negativeBold = runner(emotion).run(250, .1, {performance:3});
  assert(negativeBold.mouthForm < -.01 && negativeBold.eyeSmile < .0001,
    'large performance scales the selected negative pose instead of injecting a positive smile');
}
console.log('PASS: strong-performance pause/noise/stall/background guards and negative-emotion priority');
