// Exercise gesture wiring through the real page, audio events and Cubism hooks.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {fixture} = require('./test_avatar_performance_ui.js');

const channels = ['headX','headY','headZ','bodyX','bodyY','bodyZ','gazeX','gazeY','eyeOpen','smile','brow'];
const semantic = ['认可','疑惑','轻吐槽','被夸'];
const kinds = ['倾听','思考',...semantic];
const events = (page, controller, type) => page.gestureEvents.filter(event => event.controller === controller && event.type === type);
const frames = (page, controller) => page.gestureFrames.filter(frame => frame.controller === controller);
const started = (page, controller) => events(page, controller, 'start').filter(event => event.started);
const liveController = page => page.read('conversationGestures');
function advance(page, count, playing = false) {
  for (let i = 0; i < count; i++) {
    if (playing) page.audio.currentTime += .016;
    page.frame();
  }
}
function quiet(frame) {return channels.every(key => Math.abs(frame[key]) < .001);}

async function main() {
  const gate = await fixture(), gestures = liveController(gate);
  gate.receive({type:'reply_partial',id:1,text:'可以呀',emotion:'开心',actions:['认可']});
  gate.receive({type:'reply',id:1,text:'可以呀',emotion:'开心',actions:['认可'],mode:'llm'});
  gate.queue({actions:['认可']});
  await gate.flush(); advance(gate, 15);
  assert.equal(events(gate, gestures, 'begin').length, 0,
    'partial text, final text, prepared audio and play() resolving cannot begin a speech gesture');
  gate.audio.fire('playing'); advance(gate, 45, true);
  assert.equal(events(gate, gestures, 'begin').length, 1);
  const begin = events(gate, gestures, 'begin')[0].args[0];
  assert.equal(begin.turnId, 1); assert.equal(begin.segmentIndex, 0);
  assert.deepEqual(Array.from(begin.actions), ['认可']);
  assert(frames(gate, gestures).some(frame => frame.result.active && frame.result.name === '认可'),
    'actual playback drives the selected semantic gesture');
  const beforePause = frames(gate, gestures).at(-1).result;
  for (const pauseEvent of ['pause','waiting']) {
    gate.audio.fire(pauseEvent); gate.tick(5000); advance(gate, 20);
    const paused = frames(gate, gestures).at(-1);
    assert.equal(paused.options.paused, true);
    assert.equal(paused.options.speaking, false);
    assert.deepEqual(paused.result, beforePause, `${pauseEvent} freezes the gesture instead of spending wall-clock time`);
    gate.audio.fire('playing'); gate.audio.fire('playing');
    assert.equal(events(gate, gestures, 'begin').length, 1, 'playing on resume is not a second gesture begin');
  }
  assert.equal(events(gate, gestures, 'end').length, 0, 'pause and buffering do not complete the speech gesture');
  const startCount = started(gate, gestures).length;
  gate.audio.fire('ended');
  assert.equal(events(gate, gestures, 'end').at(-1).args[0].completed, true,
    'real ended begins gesture release immediately, before the native expression grace period');
  gate.tick(100);
  gate.queue({turn:1,segment:1,actions:['认可']}); gate.audio.fire('playing'); advance(gate, 50, true);
  assert.equal(events(gate, gestures, 'begin').at(-1).args[0].segmentIndex, 1);
  assert.equal(started(gate, gestures).length, startCount, 'same-turn audio segments cannot replay a consumed gesture');
  gate.queue({turn:2,actions:['疑惑']}); gate.audio.fire('playing'); advance(gate, 45, true);
  assert(started(gate, gestures).some(event => event.name === '疑惑'), 'a new reply can select its own gesture');
  assert.equal(frames(gate, gestures).at(-1).result.name, '疑惑');
  console.log('PASS: speech gestures follow actual playing, pause/buffering, ended and segmented turn identity');

  for (const reason of ['interrupted','clear','disconnect','ended']) {
    const page = await fixture(), controller = liveController(page);
    page.queue({actions:['被夸']}); page.audio.fire('playing'); advance(page, 50, true);
    assert(frames(page, controller).at(-1).result.active);
    if (reason === 'disconnect') await page.disconnect();
    else if (reason === 'ended') page.audio.fire('ended');
    else page.receive({type:'audio_control',id:11,action:reason});
    assert.equal(events(page, controller, 'end').at(-1).args[0].completed, reason === 'ended');
    advance(page, 220);
    assert(quiet(frames(page, liveController(page)).at(-1).result), `${reason} returns all gesture channels to neutral`);
  }
  for (const reason of ['clear','disconnect']) {
    const page = await fixture(), controller = liveController(page);
    page.queue({actions:['认可']}); page.audio.fire('playing'); advance(page, 40, true);
    page.audio.fire('ended'); page.tick(100);
    assert.equal(page.read('currentAudioItem'), null);
    if (reason === 'disconnect') await page.disconnect();
    else page.receive({type:'audio_control',id:null,action:'clear'});
    assert.equal(events(page, controller, 'end').at(-1).args[0].completed, false,
      `${reason} also reaches gestures while the page is between audio segments`);
    advance(page, 220);
    assert(quiet(frames(page, liveController(page)).at(-1).result));
  }
  console.log('PASS: completed/interrupted playback and silent segment-gap cleanup release gesture channels');

  const reconnect = await fixture();
  reconnect.queue({actions:['认可']}); reconnect.audio.fire('playing'); advance(reconnect, 45, true);
  await reconnect.disconnect(); reconnect.tick(5000);
  reconnect.receive({type:'hello',tts:'indextts2',audio_owner:true,
    playback:{state:'idle',queued:0,paused:false}});
  advance(reconnect, 400); // Exclude the normal per-action cooldown from this assertion.
  reconnect.queue({turn:1,actions:['认可']}); reconnect.audio.fire('playing'); advance(reconnect, 45, true);
  assert(frames(reconnect, liveController(reconnect)).at(-1).result.active,
    'a restarted backend may reuse turn IDs without inheriting the disconnected session\'s consumed actions');
  assert.equal(frames(reconnect, liveController(reconnect)).at(-1).result.name, '认可');

  const attention = await fixture(), attentionGestures = liveController(attention);
  attention.read('presence.listen(true)'); advance(attention, 50);
  assert.equal(frames(attention, attentionGestures).at(-1).options.listening, true);
  assert.equal(frames(attention, attentionGestures).at(-1).result.name, '倾听');
  attention.read('presence.listen(false);presence.thinking=true'); advance(attention, 50);
  assert.equal(frames(attention, attentionGestures).at(-1).options.thinking, true);
  assert.equal(frames(attention, attentionGestures).at(-1).result.name, '思考');
  attention.queue({actions:['认可']}); attention.audio.fire('playing'); advance(attention, 50, true);
  assert.equal(frames(attention, attentionGestures).at(-1).options.thinking, false);
  assert.equal(frames(attention, attentionGestures).at(-1).result.name, '认可');
  console.log('PASS: connection sessions can reuse turn IDs and actual attention phases hand off to speech');

  for (const closeControl of ['voice-input','discard-voice']) {
    const microphone = await fixture(), controller = liveController(microphone);
    // Isolate deliberate gesture heads from the separate ambient and listening
    // layers, while exercising the actual microphone button and ASR callbacks.
    microphone.read(`
      bodyMotion.frame=()=>({headX:0,headY:0,headZ:0,bodyX:0,bodyY:0,bodyZ:0,breath:0});
      eyeMotion.frame=()=>({x:0,y:0,headX:0,headY:0,blinkRequest:false});
      presence.frame=()=>({attention:0,nod:0,tilt:0});
    `);
    microphone.element('speech-head-intensity').value = '0';
    microphone.element('speech-head-intensity').oninput();
    microphone.queue({actions:['轻吐槽']}); microphone.audio.fire('playing'); advance(microphone, 45, true);
    assert.equal(frames(microphone, controller).at(-1).result.name, '轻吐槽');
    for (const axis of ['X','Y','Z']) assert.equal(microphone.physicsFrames.at(-1)['ParamAngle'+axis], 0);
    const beginsBeforeMic = events(microphone, controller, 'begin').length;
    const speechStartsBeforeMic = started(microphone, controller).filter(event => event.source === 'speech').length;
    const pausedMediaTime = microphone.audio.currentTime;
    microphone.element('voice-input').onclick();
    assert.equal(microphone.read('voiceActive'), true);
    assert.equal(microphone.read('presence.listening'), true);
    assert.equal(microphone.read('currentAudioItem.id'), 11, 'opening the microphone retains the old audio item for resuming');
    assert.equal(microphone.audio.paused, true);
    assert.equal(microphone.sent.filter(message => message.type === 'playback_pause').at(-1).paused, true);
    microphone.receive({type:'playback',state:'paused',current_id:11,paused:true,queued:0});
    advance(microphone, 50);
    const listening = frames(microphone, controller).at(-1);
    assert.equal(listening.options.paused, true);
    assert.equal(listening.options.listening, true);
    assert.equal(listening.result.name, '倾听', 'live microphone attention takes over the retained paused speech gesture');
    assert(listening.result.active);
    assert(microphone.physicsFrames.at(-1).ParamAngleY < -1,
      'speech-head zero cannot suppress the deliberate listening nod, even while the old speech item remains paused');
    assert.equal(microphone.audio.currentTime, pausedMediaTime, 'the listening nod does not advance the paused speech');
    assert.equal(events(microphone, controller, 'begin').length, beginsBeforeMic,
      'microphone listening enters through attention state rather than a fake speech begin');

    microphone.element(closeControl).onclick();
    assert.equal(microphone.read('presence.listening'), false);
    assert.equal(microphone.read('voiceActive'), false);
    advance(microphone, 180);
    assert(quiet(frames(microphone, controller).at(-1).result), `${closeControl} releases the listening gesture while the old audio stays paused`);
    if (closeControl === 'voice-input') microphone.element('pause-audio').onclick();
    assert.equal(microphone.sent.filter(message => message.type === 'playback_pause').at(-1).paused, false,
      'canceling the draft or explicitly resuming playback sends a resume intent');
    microphone.receive({type:'audio_control',id:11,action:'resume'});
    await microphone.flush();
    microphone.receive({type:'playback',state:'playing',current_id:11,paused:false,queued:0});
    microphone.audio.fire('playing'); advance(microphone, 50, true);
    assert.equal(microphone.read('audioPlaying'), true);
    assert.equal(events(microphone, controller, 'begin').length, beginsBeforeMic,
      'resuming the same audio after microphone close does not begin its gesture again');
    assert.equal(started(microphone, controller).filter(event => event.source === 'speech').length, speechStartsBeforeMic);
    assert(quiet(frames(microphone, controller).at(-1).result), 'the retired speech gesture cannot reappear after microphone attention');
  }
  console.log('PASS: microphone open/close and ASR cancel take over paused speech without replaying gestures');

  const composed = await fixture();
  composed.read(`
    bodyMotion.frame=()=>({headX:0,headY:0,headZ:0,bodyX:0,bodyY:0,bodyZ:0,breath:.2});
    eyeMotion.frame=()=>({x:0,y:0,headX:0,headY:0,blinkRequest:false});
    conversationGestures.frame=()=>({headX:4,headY:-2,headZ:3,bodyX:.5,bodyY:.4,bodyZ:.3,
      gazeX:.3,gazeY:.2,eyeOpen:.05,smile:.05,brow:.05,weight:1,active:true,name:'疑惑'});
  `);
  composed.setPhysics(core => {
    core.setParameterValueById('ParamEyeBallX', -.99);
    core.setParameterValueById('ParamEyeBallY', -.99);
    core.setParameterValueById('Param', -10);
    core.setParameterValueById('Param2', -10);
  });
  composed.frame();
  const pose = composed.physicsFrames.at(-1), rendered = composed.renderedFrames.at(-1);
  for (const id of ['ParamAngleX','ParamAngleZ','ParamBodyAngleX','ParamBodyAngleY','ParamBodyAngleZ'])
    assert(pose[id] > .1, `${id} receives gesture offsets before physics`);
  assert(pose.ParamAngleY < -1);
  assert(pose.ParamBrowLForm > 0 && pose.ParamBrowRForm > 0, 'gesture brow reaches the native facial parameters');
  assert(pose.ParamEyeBallX > 0 && pose.ParamEyeBallY > 0);
  assert(rendered.ParamEyeBallX > 0 && rendered.ParamEyeBallY > 0,
    'the final gaze hook keeps the gesture direction after physics writes its own pupils');
  assert.notEqual(rendered.Param, -10); assert.notEqual(rendered.Param2, -10);
  assert.equal(composed.invalidWrites.length, 0);
  composed.read(`faceMotion.frame=()=>({mouthOpen:0,mouthForm:0,mouthWidth:0,mouthRound:0,mouthShift:0,
    browL:0,browR:0,eyeSmile:0,eyeOpenL:0,eyeOpenR:0,blink:0,accent:0,speechEnergy:0})`);
  composed.frame();
  assert.equal(composed.physicsFrames.at(-1).ParamEyeLOpen, 0);
  assert.equal(composed.physicsFrames.at(-1).ParamEyeROpen, 0,
    'a positive gesture eyelid offset cannot reopen a closed blink');
  const native = await fixture(), nativeGestures = liveController(native);
  native.queue({actions:['弯眼','疑惑']}); native.audio.fire('playing'); await native.flush(); advance(native, 50, true);
  const nativeGesture = frames(native, nativeGestures).at(-1);
  assert.equal(nativeGesture.options.explicitFace, true);
  for (const channel of ['gazeX','gazeY','eyeOpen','smile','brow']) assert.equal(nativeGesture.result[channel], 0);
  const nativePose = native.physicsFrames.at(-1);
  assert(nativePose.ParamEyeLOpen <= .5 && nativePose.ParamEyeROpen <= .45);
  assert(nativePose.ParamBrowLForm > .2 && nativePose.ParamBrowRForm < -.15);
  assert(nativePose.ParamMouthOpenY > .05, 'native expression priority preserves speech lip sync');
  console.log('PASS: gesture composition reaches physics/render, respects blinking and preserves explicit expressions');

  const headControl = await fixture();
  headControl.read(`
    bodyMotion.frame=()=>({headX:0,headY:0,headZ:0,bodyX:0,bodyY:0,bodyZ:0,breath:0});
    eyeMotion.frame=()=>({x:0,y:0,headX:0,headY:0,blinkRequest:false});
  `);
  headControl.element('speech-head-intensity').value = '40'; headControl.element('speech-head-intensity').oninput();
  headControl.queue({actions:['疑惑']}); headControl.audio.fire('playing'); advance(headControl, 45, true);
  const headBeforePause = headControl.physicsFrames.at(-1);
  headControl.audio.fire('pause'); headControl.frame();
  for (const axis of ['X','Y','Z']) assert.equal(headControl.physicsFrames.at(-1)['ParamAngle'+axis],
    headBeforePause['ParamAngle'+axis], 'pausing cannot change the gain applied to a frozen speech gesture');
  headControl.element('speech-head-intensity').value = '0'; headControl.element('speech-head-intensity').oninput();
  headControl.frame();
  for (const axis of ['X','Y','Z']) assert.equal(headControl.physicsFrames.at(-1)['ParamAngle'+axis], 0,
    'speech-head zero also disables held speech gesture head offsets during pause');

  for (const [id, outputChannels] of [
    ['performance-intensity',channels], ['motion-intensity',channels],
    ['eye-intensity',['gazeX','gazeY','eyeOpen']], ['face-intensity',['eyeOpen','smile','brow']],
  ]) {
    const page = await fixture(), controller = liveController(page);
    page.element(id).value = '0'; page.element(id).oninput();
    page.queue({actions:['被夸']}); page.audio.fire('playing'); advance(page, 60, true);
    const frame = frames(page, controller).at(-1);
    for (const channel of outputChannels) assert.equal(frame.result[channel], 0, `${id}=0 suppresses ${channel}`);
    assert(page.physicsFrames.at(-1).ParamMouthOpenY > .05, `${id}=0 preserves audible lip sync`);
  }
  console.log('PASS: performance, body, gaze and face controls reach gesture channels without disabling speech lips');

  const html = fs.readFileSync(path.join(__dirname,'../brain/static/index.html'),'utf8');
  const selector = html.match(/<select\b[^>]*id="avatar-preview-kind"[^>]*>([\s\S]*?)<\/select>/);
  assert(selector, 'gesture previews are available through a real, labeled selection control');
  for (const kind of kinds) assert(selector[1].includes(`value="${kind}"`), `the ${kind} preview is in the rendered markup`);
  for (const kind of kinds) {
    const page = await fixture(), live = liveController(page);
    const before = {sent:JSON.stringify(page.sent),fetches:page.fetches.length,plays:page.audio.playCount,
      timeline:page.element('events').children.length,speech:page.read('JSON.stringify(avatarSpeech)')};
    page.element('avatar-preview-kind').value = kind;
    page.element('avatar-preview').onclick();
    const preview = page.read('avatarPreview');
    assert(preview && preview.gestures !== live, `${kind} owns a separate gesture controller`);
    assert.equal(preview.kind, kind);
    advance(page, 135);
    const previewFrames = frames(page, preview.gestures);
    assert(previewFrames.some(frame => frame.result.name === kind && frame.result.active), `${kind} visibly exercises its gesture`);
    assert(previewFrames.some(frame => kind === '倾听' ? frame.options.listening
      : kind === '思考' ? frame.options.thinking : frame.options.speaking));
    if (!semantic.includes(kind)) {
      assert(previewFrames.every(frame => !frame.options.speaking), 'attention previews do not pretend to speak');
      assert.equal(page.physicsFrames.at(-1).ParamMouthOpenY, 0);
    } else assert(page.physicsFrames.slice(60).some(pose => pose.ParamMouthOpenY > .05));
    assert.equal(frames(page, live).length, 135, 'the real idle controller still receives exactly one frame per render');
    assert(frames(page, live).every(frame => !frame.options.speaking && !frame.options.listening && !frame.options.thinking));
    assert.equal(events(page, live, 'begin').length, 0);
    assert.equal(page.read('JSON.stringify(avatarSpeech)'), before.speech);
    assert.equal(page.read('audioPlaying'), false); assert.equal(page.read('currentAudioItem'), null);
    assert.equal(JSON.stringify(page.sent), before.sent);
    assert.equal(page.fetches.length, before.fetches); assert.equal(page.audio.playCount, before.plays);
    assert.equal(page.element('events').children.length, before.timeline);
    assert.equal(page.analyserSamples, 0);
    page.tick(10000);
    assert.equal(page.read('avatarPreview'), null);
    assert.equal(page.read('avatarPreviewTimer'), null);
  }
  const handoff = await fixture(), live = liveController(handoff);
  handoff.element('avatar-preview-kind').value = '疑惑'; handoff.element('avatar-preview').onclick(); advance(handoff, 120);
  const previewGestures = handoff.read('avatarPreview.gestures');
  assert(frames(handoff, previewGestures).some(frame => frame.result.active));
  handoff.queue({turn:42,actions:['认可']}); handoff.frame();
  assert.equal(handoff.read('avatarPreview'), null, 'real queued audio immediately takes priority over the gesture preview');
  assert.equal(events(handoff, live, 'begin').length, 0, 'preview handoff does not start unplayed real audio gestures');
  assert(quiet(frames(handoff, live).at(-1).result), 'preview offsets cannot contaminate the waiting real reply');
  handoff.audio.fire('playing'); advance(handoff, 50, true);
  assert.equal(events(handoff, live, 'begin').at(-1).args[0].turnId, 42);
  assert.equal(frames(handoff, live).at(-1).result.name, '认可');
  console.log('PASS: all six silent previews isolate controllers, preserve real clocks and yield to actual playback');
}

main().catch(error => {console.error(error); process.exitCode = 1;});
