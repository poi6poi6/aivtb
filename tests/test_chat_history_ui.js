// Exercise the actual console's long-running history and active-row lifetime.
const assert = require('node:assert/strict');
const {fixture} = require('./test_avatar_performance_ui.js');

const event = (id, source = 'danmaku') => ({
  type:'event', id, source, kind:'chat', user:'fixture', text:'message ' + id,
});
const reply = id => ({type:'reply', id, text:'reply ' + id, emotion:'平静', mode:'llm', actions:[]});

(async () => {
  const f = await fixture();
  const limit = f.read('CHAT_HISTORY_LIMIT');
  for (let id = 1; id <= 10000; id++) {
    f.receive(event(id));
    if (id % 3 === 0) f.receive(reply(id));
  }
  assert.equal(f.element('events').children.length, limit);
  for (const name of ['rows', 'eventInfo', 'replyInfo']) {
    assert(f.read(`Object.keys(${name}).length`) <= limit, name + ' must not retain evicted messages');
  }
  assert.equal(f.read('rows[1]'), undefined);
  assert(f.read('rows[10000]'));
  await f.disconnect();
  assert.equal(f.element('events').children.length, limit, 'disconnect preserves a bounded readable history');

  const live = await fixture();
  live.receive(event(1, 'voice'));
  const voiceRow = live.read('rows[1]');
  live.receive(event(2));
  live.receive({type:'thinking', id:2});
  live.receive({type:'reply_partial', id:2, text:'still generating', emotion:'平静'});
  const partialRow = live.read('partialReplies[2]');
  live.receive(event(3));
  live.receive(reply(3));
  live.receive({type:'audio', id:31, turn_id:3, url:'/audio/31.wav', text:'speaking', emotion:'平静'});
  await live.flush();
  live.audio.fire('playing');
  live.receive(event(4));
  live.receive({type:'speech_stage', turn_id:4, stage:'synthesizing'});
  live.receive(event(5));
  const pinRow = live.read('rows[5]');
  pinRow.questionPinPending = true;
  for (let id = 10; id < 1010; id++) live.receive(event(id));
  for (const id of [1, 2, 3, 4, 5]) assert(live.read(`rows[${id}]`), 'active turn ' + id + ' is preserved');
  assert.equal(live.read('partialReplies[2]'), partialRow);
  assert.equal(live.read('currentAudioItem.turn_id'), 3);
  assert(live.element('events').children.length <= limit);

  // The retained voice row still performs the actual correction workflow.
  const edit = voiceRow.children.flatMap(child => child.children || [])
    .find(child => child.textContent === '修改识别');
  edit.onclick();
  assert.equal(live.read('correctingId'), 1);
  live.element('text').value = 'corrected transcript';
  live.element('confirm-voice').onclick();
  assert(live.sent.some(message => message.type === 'inject' && message.corrects === 1
    && message.text === 'corrected transcript'));
  live.receive({...event(2000, 'voice'), corrects:1});
  assert.equal(voiceRow.textContent, '这条语音转写已更正');

  live.receive(reply(2));
  assert.equal(live.element('events').children.filter(row => row === partialRow).length, 1,
    'final streaming reply updates its retained row exactly once');
  live.receive({type:'audio_control', action:'clear', id:31});
  live.receive({type:'speech_stage', turn_id:4, stage:'drained'});
  pinRow.questionPinPending = false;
  for (let id = 2010; id < 2410; id++) live.receive(event(id));
  for (const id of [1, 2, 3, 4, 5]) assert.equal(live.read(`rows[${id}]`), undefined,
    'completed turn ' + id + ' can now be evicted');
  assert.equal(live.element('events').children.length, limit);
  assert(live.read('rows[2000]'), 'latest editable transcript remains available');
  console.log('PASS: bounded console history, metadata cleanup, active speech, streaming, pin and voice correction');
})().catch(error => {console.error(error); process.exitCode = 1;});
