// The real page couples wording to actual playback without requiring action tags.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {fixture:baseFixture} = require('./test_avatar_performance_ui.js');
async function fixture(options) {
  const page=await baseFixture(options), queue=page.queue;
  page.queue=(input={})=>queue({text:'重点是把这件事情说清楚。',duration:4,...input});
  return page;
}

const channels = ['headX','headY','headZ','bodyX','bodyY','bodyZ','gazeX','gazeY','eyeOpen','smile','browL','browR'];
const zero = Object.fromEntries([...channels,'weight'].map(key => [key,0]));
const live = page => page.read('speechPerformance');
const frames = (page, controller = live(page)) => page.speechPerformanceFrames.filter(frame => frame.controller === controller);
const events = (page, type, controller = live(page)) => page.speechPerformanceEvents.filter(event => event.controller === controller && event.type === type);
const quiet = result => channels.every(key => Math.abs(result[key]) < .001);
function advance(page, count, playing = false) {
  for (let i = 0; i < count; i++) {
    if (playing) page.audio.currentTime += .016;
    page.frame();
  }
}

async function main() {
  const gate = await fixture();
  gate.receive({type:'reply_partial',id:1,text:'今天可以慢慢聊。',emotion:'平静',actions:[]});
  gate.queue({emotion:'平静'});
  await gate.flush(); advance(gate, 90);
  assert.equal(events(gate,'begin').length, 0, 'reply text, queued audio and play() resolving cannot start speech performance');
  assert(frames(gate).every(frame => quiet(frame.result)), 'silent prepared audio has no speaking gestures');
  gate.audio.fire('playing'); advance(gate, 150, true);
  assert.equal(events(gate,'begin').length, 1);
  assert.equal(events(gate,'begin')[0].args[0].emotion, '平静');
  assert.equal(events(gate,'begin')[0].args[0].turnId, 1);
  const spoken = frames(gate).filter(frame => frame.options.speaking);
  for (const group of [['headX','headY','headZ'],['bodyX','bodyY','bodyZ'],['browL','browR']]) {
    assert(spoken.some(frame => group.some(key => Math.abs(frame.result[key]) > .005)),
      `ordinary speech without action tags visibly reaches ${group.join('/')}`);
  }
  assert(spoken.every(frame => frame.result.gazeX===0 && frame.result.gazeY===0),
    'word-driven speech leaves deliberate eye contact to the gaze controller');
  assert(gate.gazeFrames.some(frame => frame.options.focus==='engage'));
  assert(spoken.some(frame => frame.options.energy > .1), 'the controller receives the already-sampled real speech energy');
  assert.equal(gate.invalidWrites.length, 0);
  gate.audio.fire('playing');
  assert.equal(events(gate,'begin').length, 1, 'repeated playing on an existing audio element cannot replay the opening');
  console.log('PASS: spoken emphasis couples face, head and body on playback while preserving deliberate eye contact');

  for (const pauseEvent of ['pause','waiting']) {
    const page = await fixture();
    page.queue({emotion:'开心'}); page.audio.fire('playing'); advance(page, 45, true);
    const held = frames(page).at(-1).result;
    assert(!quiet(held), 'the transport test pauses an existing visible speech performance');
    page.audio.fire(pauseEvent); advance(page, 150);
    assert.deepEqual(frames(page).at(-1).result, held, `${pauseEvent} freezes the speech contour`);
    assert.equal(frames(page).at(-1).options.paused, true);
    assert.equal(events(page,'end').length, 0, 'buffering is not completion');
    page.audio.fire('playing'); page.frame();
    assert.deepEqual(frames(page).at(-1).result, held, 'resume without new media progress cannot catch up or restart a gesture');
    assert.equal(events(page,'begin').length, 1);
    advance(page, 30, true);
    const beforeSeek = frames(page).at(-1).result;
    page.audio.currentTime += 20; page.frame();
    assert.deepEqual(frames(page).at(-1).result, beforeSeek, 'seeking does not invent a gesture or consume its contour');
    const beforeBackground = frames(page).at(-1).result;
    page.audio.currentTime += 20; page.frame(30000);
    assert.deepEqual(frames(page).at(-1).result, beforeBackground, 'background return does not replay unseen gestures');
  }
  console.log('PASS: pause, buffering, resume, seek and background gaps preserve the spoken contour');

  for (const completion of ['ended','interrupted','clear','disconnect']) {
    const page = await fixture();
    page.queue({emotion:'开心'}); page.audio.fire('playing'); advance(page, 45, true);
    if (completion === 'disconnect') await page.disconnect();
    else if (completion === 'ended') page.audio.fire('ended');
    else page.receive({type:'audio_control',id:11,action:completion});
    assert(events(page,'end').some(event => event.args[0].completed === (completion === 'ended')),
      `${completion} reaches speech-performance cleanup with the correct completion state`);
    advance(page, 220);
    assert(quiet(frames(page).at(-1).result), `${completion} releases all speaking channels`);
    if (completion === 'disconnect') assert(events(page,'resetSession').length > 0);
  }
  console.log('PASS: completion, interruption and connection cleanup release speaking animation');

  const segments = await fixture();
  segments.queue({emotion:'开心'});segments.audio.fire('playing');advance(segments,45,true);
  assert.equal(events(segments,'cue').length,1);
  segments.audio.fire('ended');segments.tick(80);
  segments.queue({turn:1,segment:1,emotion:'开心'});segments.audio.fire('playing');advance(segments,45,true);
  assert.equal(events(segments,'begin').at(-1).args[0].segmentIndex,1);
  assert.equal(events(segments,'cue').length,1,'the next streamed segment preserves the shared cooldown instead of replaying the opening');
  segments.audio.fire('ended');segments.tick(80);
  segments.queue({turn:2,emotion:'开心'});segments.audio.fire('playing');advance(segments,45,true);
  assert.equal(events(segments,'cue').length,2,'a genuinely new reply can open with its own coordinated cue');
  segments.read('sampleSpeechLevel=()=>0');advance(segments,220,true);
  assert(quiet(frames(segments).at(-1).result),'a silent region releases the current phrase contour');
  const cuesBeforeSilence=events(segments,'cue').length;
  advance(segments,220,true);
  assert.equal(events(segments,'cue').length,cuesBeforeSilence,'a playing but silent clip cannot invent additional emphasis');
  console.log('PASS: streamed segments preserve the speech cooldown and actual silence releases its contour');

  const microphone = await fixture();
  microphone.queue({emotion:'开心'});microphone.audio.fire('playing');advance(microphone,45,true);
  assert(!quiet(frames(microphone).at(-1).result));
  const micBegins=events(microphone,'begin').length,micCues=events(microphone,'cue').length;
  microphone.element('voice-input').onclick();
  microphone.receive({type:'playback',state:'paused',current_id:11,paused:true,queued:0});
  advance(microphone,180);
  assert.equal(frames(microphone).at(-1).options.listening,true);
  assert.equal(frames(microphone).at(-1).options.paused,true);
  assert(quiet(frames(microphone).at(-1).result),'opening the microphone retires the retained paused speech contour');
  microphone.element('discard-voice').onclick();
  microphone.receive({type:'audio_control',id:11,action:'resume'});await microphone.flush();
  microphone.audio.fire('playing');advance(microphone,120,true);
  assert.equal(events(microphone,'begin').length,micBegins);
  assert.equal(events(microphone,'cue').length,micCues);
  assert(quiet(frames(microphone).at(-1).result),'late playing after microphone close cannot resurrect the retired contour');
  console.log('PASS: live microphone attention retires paused speech performance without stale replay');

  const neutral = await fixture(), happy = await fixture(), sad = await fixture(), angry = await fixture();
  const emotionalPages = [[neutral,'平静'],[happy,'开心'],[sad,'委屈'],[angry,'生气']];
  for (const [page, emotion] of emotionalPages) {
    page.queue({emotion}); page.audio.fire('playing'); advance(page, 150, true);
  }
  const contour = page => frames(page).map(frame => channels.map(key => Number(frame.result[key].toFixed(5))));
  assert.notDeepEqual(contour(happy), contour(neutral), 'happy speech has a different coordinated contour from ordinary explanation');
  assert.notDeepEqual(contour(sad), contour(happy), 'downcast speech cannot inherit happy movement');
  assert.notDeepEqual(contour(angry), contour(sad), 'anger and sadness remain distinguishable');
  for (const page of [sad, angry]) {
    assert(frames(page).every(frame => frame.result.smile <= .0001), 'negative speech never receives a cheerful smile overlay');
    assert(page.physicsFrames.slice(80).every(pose => pose.ParamMouthForm <= .001),
      'after the resting-smile transition, negative speech stays non-smiling in the rendered model');
  }
  console.log('PASS: shared audio produces emotion-specific contours without cheerful overlays on negative emotion');

  const composed = await fixture();
  composed.read(`
    bodyMotion.frame=()=>({headX:0,headY:0,headZ:0,bodyX:0,bodyY:0,bodyZ:0,breath:0});
    eyeMotion.frame=()=>({x:0,y:0,headX:0,headY:0,blinkRequest:false});
    presence.frame=()=>({attention:0,nod:0,tilt:0});
    faceMotion.frame=()=>({mouthOpen:.4,mouthForm:0,mouthWidth:0,mouthRound:0,mouthShift:0,
      browL:0,browR:0,eyeSmile:0,eyeOpenL:1,eyeOpenR:1,blink:1,accent:0,speechEnergy:.5});
    conversationGestures.frame=()=>(${JSON.stringify({...zero,brow:0,active:false})});
    speechPerformance.frame=()=>({headX:4,headY:-2,headZ:3,bodyX:.4,bodyY:.5,bodyZ:.6,
      gazeX:.3,gazeY:.2,eyeOpen:-.05,smile:.05,browL:.08,browR:-.03,weight:.8,active:true,cue:'speech'});
  `);
  composed.setPhysics(core => {core.setParameterValueById('ParamEyeBallX',-.9);core.setParameterValueById('ParamEyeBallY',-.9);});
  composed.frame();
  const pose = composed.physicsFrames.at(-1), rendered = composed.renderedFrames.at(-1);
  assert(pose.ParamAngleX > 1 && pose.ParamAngleY < -1 && pose.ParamBodyAngleY > .1);
  assert(pose.ParamBrowLForm > .01 && pose.ParamBrowRForm < -.01, 'left and right speech brows remain independent');
  assert(pose.ParamEyeLOpen < 1 && pose.ParamMouthForm > .01);
  assert.equal(pose.ParamMouthOpenY,.4, 'speech expression layering preserves the real audio-driven jaw');
  assert(rendered.ParamEyeBallX > 0 && rendered.ParamEyeBallY > 0, 'post-physics correction preserves the same coordinated gaze');
  composed.read(`conversationGestures.frame=()=>(${JSON.stringify({...zero,brow:0,weight:1,active:true})})`);
  composed.frame();
  const owned = composed.physicsFrames.at(-1);
  for (const id of ['ParamAngleX','ParamAngleY','ParamAngleZ','ParamBodyAngleX','ParamBodyAngleY','ParamBodyAngleZ',
    'ParamEyeBallX','ParamEyeBallY','ParamMouthForm','ParamBrowLForm','ParamBrowRForm'])
    assert.equal(owned[id],0, `a full semantic gesture suppresses the secondary speaking layer at ${id}`);
  assert.equal(owned.ParamMouthOpenY,.4);
  composed.read(`conversationGestures.frame=()=>(${JSON.stringify({...zero,brow:0,active:false})});
    faceMotion.frame=()=>({mouthOpen:.4,mouthForm:0,mouthWidth:0,mouthRound:0,mouthShift:0,
      browL:0,browR:0,eyeSmile:0,eyeOpenL:0,eyeOpenR:0,blink:0,accent:0,speechEnergy:.5});`);
  composed.frame();
  assert.equal(composed.physicsFrames.at(-1).ParamEyeLOpen,0);
  assert.equal(composed.physicsFrames.at(-1).ParamEyeROpen,0,'speech eye styling cannot reopen a blink');
  console.log('PASS: coordinated speech reaches native physics/render, yields to semantic gestures and preserves jaw/blink');

  const native = await fixture();
  native.queue({emotion:'开心',actions:['弯眼']}); native.audio.fire('playing'); await native.flush(); advance(native,90,true);
  assert(frames(native).some(frame => frame.options.explicitFace));
  for (const frame of frames(native).filter(frame => frame.options.explicitFace)) {
    for (const key of ['gazeX','gazeY','eyeOpen','smile','browL','browR']) assert.equal(Math.abs(frame.result[key]),0,`native expression owns ${key}`);
  }
  assert.equal(native.physicsFrames.at(-1).ParamMouthForm,.25);
  assert(native.physicsFrames.at(-1).ParamEyeLOpen <= .4);
  assert(native.physicsFrames.at(-1).ParamMouthOpenY > .05);

  for (const [id, disabled] of [
    ['performance-intensity',channels],['motion-intensity',['headX','headY','headZ','bodyX','bodyY','bodyZ']],
    ['eye-intensity',['gazeX','gazeY','eyeOpen']],['face-intensity',['eyeOpen','smile','browL','browR']],
  ]) {
    const page = await fixture(); page.element(id).value='0'; page.element(id).oninput();
    page.queue({emotion:'开心'}); page.audio.fire('playing'); advance(page,120,true);
    for (const frame of frames(page)) for (const key of disabled) assert.equal(Math.abs(frame.result[key]),0,`${id}=0 owns ${key}`);
    assert(page.physicsFrames.at(-1).ParamMouthOpenY > .05, 'disabling style keeps the real speaking jaw');
  }
  console.log('PASS: explicit expression priority and independent controls preserve speech lip sync');

  const headOff = await fixture();
  headOff.read(`
    bodyMotion.frame=()=>({headX:0,headY:2,headZ:1,bodyX:0,bodyY:0,bodyZ:0,breath:0});
    eyeMotion.frame=()=>({x:0,y:0,headX:0,headY:0,blinkRequest:false});
    presence.frame=()=>({attention:0,nod:0,tilt:0});
  `);
  headOff.element('speech-head-intensity').value='0';headOff.element('speech-head-intensity').oninput();
  headOff.queue({emotion:'开心'});headOff.audio.fire('playing');advance(headOff,120,true);
  for (const pose of headOff.physicsFrames) {
    assert.equal(pose.ParamAngleX,0);assert.equal(pose.ParamAngleY,2);assert.equal(pose.ParamAngleZ,1,
      'speech-head zero must not attenuate or amplify the existing idle head pose');
  }
  assert(headOff.physicsFrames.some(pose=>Math.abs(pose.ParamBodyAngleY)>.01),'head-off preserves speaking body motion');
  assert(frames(headOff).some(frame=>Math.abs(frame.result.browL)>.01),'head-off preserves speaking facial expression');

  const maximum = await fixture();
  for (const [id,value] of [['performance-intensity','300'],['motion-intensity','160'],['speech-head-intensity','160'],['eye-intensity','100'],['face-intensity','100']]) {
    maximum.element(id).value=value;maximum.element(id).oninput();
  }
  maximum.queue({emotion:'惊讶',actions:['疑惑']});maximum.audio.fire('playing');advance(maximum,500,true);
  assert.equal(maximum.invalidWrites.length,0,'even combined high-intensity gestures cannot create unknown native parameters');
  for (const pose of maximum.renderedFrames) for (const [id,value] of Object.entries(pose)) {
    const index=maximum.core.getParameterIndex(id);
    assert(Number.isFinite(value),`${id} remains finite`);
    assert(value>=maximum.core.getParameterMinimumValue(index)&&value<=maximum.core.getParameterMaximumValue(index),`${id} stays in its native bounds`);
  }
  console.log('PASS: head-off preserves baseline pose and combined maximum performance stays inside native bounds');

  const html = fs.readFileSync(path.join(__dirname,'../brain/static/index.html'),'utf8');
  for (const emotion of ['开心','惊讶','委屈']) {
    const value = 'speech:'+emotion;
    assert(html.includes(`value="${value}"`), `${emotion} speaking preview is selectable`);
    const page = await fixture(), controller = live(page);
    const before = {sent:JSON.stringify(page.sent),fetches:page.fetches.length,plays:page.audio.playCount,
      timeline:page.element('events').children.length,speech:page.read('JSON.stringify(avatarSpeech)')};
    page.element('avatar-preview-kind').value=value; page.element('avatar-preview').onclick();
    const preview=page.read('avatarPreview');
    assert(preview.speechPerformance !== controller,'silent preview owns an independent speech-performance controller');
    advance(page,190);
    assert(events(page,'begin',preview.speechPerformance).some(event=>event.args[0].emotion===emotion));
    assert(frames(page,preview.speechPerformance).some(frame=>!quiet(frame.result)));
    assert(frames(page,controller).every(frame=>!frame.options.speaking && quiet(frame.result)));
    assert.equal(events(page,'begin',controller).length,0);
    assert.equal(page.read('JSON.stringify(avatarSpeech)'),before.speech);
    assert.equal(JSON.stringify(page.sent),before.sent);assert.equal(page.fetches.length,before.fetches);
    assert.equal(page.audio.playCount,before.plays);assert.equal(page.analyserSamples,0);
    assert.equal(page.element('events').children.length,before.timeline);
    page.tick(10000);assert.equal(page.read('avatarPreview'),null);
  }
  console.log('PASS: emotional speaking previews are isolated from real playback, chat, memory and audio sampling');
}

main().catch(error=>{console.error(error);process.exitCode=1;});
