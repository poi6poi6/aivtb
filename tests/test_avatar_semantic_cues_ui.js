// Real host wiring: wording, audio clock, intention, native parameters and cleanup.
const assert=require('node:assert/strict');
const {fixture}=require('./test_avatar_performance_ui.js');
function advance(page,count,playing=true) {
  for(let i=0;i<count;i++) {if(playing)page.audio.currentTime+=.016;page.frame();}
}
const cues=page=>page.speechPerformanceFrames.filter(f=>f.controller===page.read('speechPerformance') && f.options.semanticCue);
const gaze=page=>page.gazeFrames.filter(f=>f.controller===page.read('eyeMotion'));
async function speak(text,duration=8,options={}) {
  const page=await fixture();page.queue({text,duration,...options});page.audio.fire('playing');
  advance(page,Math.floor(duration/.016));return page;
}
async function main() {
  const early=await speak('更喜欢后半段，前面也有值得细听的细节。');
  const late=await speak('前面也有值得细听的细节，不过我更喜欢后半段。');
  const earlyCue=cues(early).find(f=>f.options.semanticCue.kind==='emphasis');
  const lateCue=cues(late).find(f=>f.options.semanticCue.kind==='emphasis');
  assert(earlyCue && lateCue);
  assert(lateCue.options.mediaTime-earlyCue.options.mediaTime>3,
    'moving the focal phrase moves its gesture even with identical sound energy');
  assert(late.physicsFrames.some(p=>p.ParamAngleY<-.5),'semantic emphasis reaches the native model');
  const plain=await speak('前面是一段旋律，后面还有一段旋律。');
  assert.equal(cues(plain).length,0,'a plain sentence does not get invented emphasis');
  assert(plain.physicsFrames.some(p=>p.ParamMouthOpenY>.05),'ordinary lip sync remains alive');
  assert(gaze(plain).every(f=>Math.abs(f.result.x)<.08),'ordinary speaking avoids periodic large side glances');
  console.log('PASS: focal wording, not identical loudness, determines gesture position; ordinary speech keeps eye contact');

  const thought=await speak('我想想，前面也不错，不过我更喜欢后半段。');
  assert.deepEqual(cues(thought).map(f=>f.options.semanticCue.kind),['reflect','emphasis']);
  const looks=gaze(thought);
  assert(looks.some(f=>f.options.focus==='reflect' && Math.abs(f.result.x)>.12));
  const emphasisAt=cues(thought).find(f=>f.options.semanticCue.kind==='emphasis').options.mediaTime;
  const emphasisGaze=looks[Math.min(looks.length-1,Math.floor((emphasisAt+.7)/.016))];
  assert.equal(emphasisGaze.options.focus,'engage');
  assert(Math.abs(emphasisGaze.result.x)<.08,'the focal statement looks back toward the listener');
  assert(thought.speechPerformanceFrames.every(f=>f.result.gazeX===0 && f.result.gazeY===0),
    'speech accents cannot layer a contradictory random side glance over semantic gaze');
  console.log('PASS: reflection briefly looks away, then emphasis returns gaze to the listener');

  const question=await speak('这两个版本，你更喜欢哪一个？',4.5);
  assert(cues(question).some(f=>f.options.semanticCue.kind==='question'));
  question.audio.fire('ended');advance(question,50,false);
  assert.equal(gaze(question).at(-1).options.focus,'question','completed question briefly keeps attention');
  question.receive({type:'event',id:2,source:'companion',kind:'chat',text:'我选第二个。'});question.frame();
  assert.equal(gaze(question).at(-1).options.focus,'','a new answer clears old waiting gaze');
  const interrupted=await fixture();
  interrupted.queue({text:'这两个版本，你更喜欢哪一个？',duration:4.5});interrupted.audio.fire('playing');advance(interrupted,260);
  interrupted.receive({type:'audio_control',id:11,action:'interrupted'});advance(interrupted,100,false);
  assert.equal(gaze(interrupted).at(-1).options.focus,'','interruption never leaves a question hold');
  const unknown=await fixture();unknown.queue({text:'重点是让动作有意义。',duration:NaN});unknown.audio.fire('playing');advance(unknown,100);
  assert.equal(cues(unknown).length,0,'unknown duration never guesses a word time');
  unknown.audio.duration=4;advance(unknown,180);
  assert.equal(cues(unknown).length,0,'late metadata cannot replay a missed early emphasis');
  console.log('PASS: question endings yield to new input/interruption, and missing audio duration causes no guessed gestures');

  for(const kind of ['intent:focus','intent:question']) {
    const page=await fixture();
    const before={sent:JSON.stringify(page.sent),fetches:page.fetches.length,plays:page.audio.playCount};
    page.element('avatar-preview-kind').value=kind;page.element('avatar-preview').onclick();
    const preview=page.read('avatarPreview');advance(page,550,false);
    const previewFrames=page.speechPerformanceFrames.filter(f=>f.controller===preview.speechPerformance);
    assert(previewFrames.some(f=>f.options.semanticCue?.kind===(kind==='intent:question'?'question':'emphasis')));
    assert(previewFrames.some(f=>f.result.active),'preview uses the actual semantic movement controller');
    if(kind==='intent:question') assert(page.gazeFrames.some(f=>f.controller===preview.eyes && !f.options.speaking && f.options.focus==='question'));
    assert.equal(JSON.stringify(page.sent),before.sent);assert.equal(page.fetches.length,before.fetches);
    assert.equal(page.audio.playCount,before.plays);assert.equal(page.analyserSamples,0);
    assert.equal(cues(page).length,0,'the isolated preview never creates live cues');
    page.tick(2000);assert.equal(page.read('avatarPreview'),null);
  }
  console.log('PASS: both new silent previews demonstrate meaning and waiting without chat, TTS or live audio side effects');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
