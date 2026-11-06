// Exercise the real console lifecycle, including speech recognition and Cubism
// parameter composition. No model service, microphone or TTS is contacted.
const assert = require('node:assert/strict');
const {fixture} = require('./test_avatar_performance_ui.js');
function advance(page, count, speaking=false) {
  for(let i=0;i<count;i++) {
    if(speaking) page.audio.currentTime += .016;
    page.frame();
  }
}
function hear(page,text,{final=true,confidence=.95}={}) {
  const result=[{transcript:text,confidence}]; result.isFinal=final;
  page.read('voiceRecognition').onresult({results:[result]});
}
async function completed(emotion) {
  const page=await fixture();
  advance(page,90);
  page.queue({emotion}); page.audio.fire('playing'); advance(page,100,true);
  page.audio.fire('ended');
  return page;
}

async function main() {
  const happy=await completed('开心'), neutral=await completed('平静');
  advance(happy,140); advance(neutral,140);
  assert(happy.faceFrames.at(-1).mouthForm > neutral.faceFrames.at(-1).mouthForm+.005,
    'the completed reply retains a subtle happy expression after the old return-to-idle boundary');
  assert.equal(happy.physicsFrames.at(-1).ParamMouthOpenY,0,'emotional residue never keeps a silent jaw open');
  advance(happy,400); advance(neutral,400);
  assert(Math.abs(happy.faceFrames.at(-1).mouthForm-neutral.faceFrames.at(-1).mouthForm)<.002,
    'the residue naturally returns to the same calm baseline');

  const sad=await completed('委屈'); advance(sad,140);
  assert(sad.faceFrames.at(-1).mouthForm<=.001,'sadness fades through a quiet face without an immediate resting smile');
  sad.receive({type:'event',id:9,source:'companion',kind:'chat',user:'你',text:'换个话题吧'});
  advance(sad,100);
  assert(sad.faceFrames.at(-1).mouthForm>.05,'a new topic clears the old emotional residue');

  const pending=await completed('委屈'); advance(pending,10);
  pending.element('text').value='现在讲点别的'; pending.element('send').onclick();
  advance(pending,180);
  assert(pending.faceFrames.at(-1).mouthForm>.05,
    'new input before the 900ms release callback prevents that old callback from restoring sadness');

  const interrupted=await fixture();
  interrupted.queue({emotion:'委屈'}); interrupted.audio.fire('playing'); advance(interrupted,100,true);
  interrupted.receive({type:'audio_control',id:11,action:'interrupted'}); advance(interrupted,140);
  assert(interrupted.faceFrames.at(-1).mouthForm>.05,'an interrupted reply does not leave a new afterglow');
  console.log('PASS: completed emotions linger, expire and yield to new input/interruption without affecting the jaw');

  const waiting=await fixture(); waiting.element('continuous-voice').checked=true;
  waiting.element('voice-input').onclick(); advance(waiting,150);
  const presence=waiting.read('presence');
  for(const text of ['今天发生了一件事','今天发生了一件事情，我还在想','让我想想','因为']) {
    hear(waiting,text,{final:text==='让我想想'||text==='因为'}); advance(waiting,20);
    assert.equal(waiting.read('coordinatedListening').nod,0,'word growth and unfinished speech do not manufacture a listening nod');
    assert.equal(waiting.read('coordinatedListening').curiosity,0);
  }
  assert.equal(waiting.sent.filter(m=>m.type==='inject').length,0,'the listener never rushes hesitant speech into a new turn');
  hear(waiting,'我终于把这件事做完了。'); advance(waiting,20);
  assert(waiting.read('coordinatedListening').nod>0,'a stable completed statement receives one quiet acknowledgement');
  const acknowledged=presence.nodAt;
  hear(waiting,'我终于把这件事做完了。');
  assert.equal(presence.nodAt,acknowledged,'repeated final recognition cannot repeat the acknowledgement');
  waiting.read('voiceRecognition').onspeechstart(); advance(waiting,50);
  assert.equal(waiting.sent.filter(m=>m.type==='inject').length,0,'speaking again cancels the send countdown');
  assert(waiting.read('coordinatedListening').readiness<.01,'new speech withdraws readiness to answer');

  const question=await fixture(); question.element('voice-input').onclick(); advance(question,150);
  hear(question,'你觉得这样可以吗？'); advance(question,20);
  assert.equal(question.read('presence.listening'),false,'click-to-talk stops recognition after the final result');
  assert(question.read('coordinatedListening').curiosity>.1,'the question reaction survives normal microphone closure');
  assert.equal(question.read('coordinatedListening').nod,0,'a question receives interest, not a nod of agreement');
  assert(question.physicsFrames.at(-1).ParamBrowLForm>.001,'question interest reaches the actual model brow');
  assert.equal(question.physicsFrames.at(-1).ParamMouthOpenY,0,'listening reactions remain silent');
  await question.disconnect(); advance(question,15);
  assert.equal(question.read('coordinatedListening').curiosity,0,'disconnect clears pending listening reactions');
  assert.equal(question.read('coordinatedListening').readiness,0);

  const uncertain=await fixture(); uncertain.element('voice-input').onclick(); advance(uncertain,150);
  hear(uncertain,'这样可以吗？',{confidence:.3}); advance(uncertain,20);
  assert.equal(uncertain.read('coordinatedListening').curiosity,0,'low-confidence recognition cannot trigger a confident visual response');
  assert.equal(uncertain.read('coordinatedListening').nod,0);
  console.log('PASS: listening feedback uses stable recognition, respects hesitation and survives normal mic closure');

  const disabled=await fixture(); disabled.element('face-intensity').value='0';disabled.element('face-intensity').oninput();
  disabled.element('motion-intensity').value='0';disabled.element('motion-intensity').oninput();
  disabled.element('voice-input').onclick();advance(disabled,150);
  hear(disabled,'这个办法可以吗？');advance(disabled,20);
  assert.equal(disabled.physicsFrames.at(-1).ParamBrowLForm,0,'face-off also disables listening curiosity');
  assert.equal(disabled.physicsFrames.at(-1).ParamAngleY,0,'motion-off also disables listening nods/leans');

  const preview=await fixture();preview.element('avatar-preview-kind').value='speech:开心';preview.element('avatar-preview').onclick();
  advance(preview,360);
  assert.equal(preview.read('avatarPreview.ended'),true,'emotional preview leaves time to see its afterglow');
  assert.equal(preview.physicsFrames.at(-1).ParamMouthOpenY,0);
  assert(preview.faceFrames.at(-1).mouthForm>.10,'the rendered silent preview retains its happy expression');
  assert.equal(preview.sent.filter(m=>m.type==='inject').length,0,'preview never sends a real conversation');
  preview.tick(10000);assert.equal(preview.read('avatarPreview'),null);
  console.log('PASS: controls apply to listening reactions and the silent preview demonstrates the emotional ending');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
