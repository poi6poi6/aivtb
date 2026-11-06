// Run the real page's close handler with hanging and stale HTTP probes.
const assert = require('node:assert/strict');
const {fixture} = require('./test_avatar_performance_ui.js');

function deferredProbe() {
  let resolve;
  const promise = new Promise(done => {resolve = done;});
  return {promise, resolve};
}

(async () => {
  const requests = [];
  const hanging = await fixture({mediaFetch: (url, options) => {
    if (url !== '/') return Promise.resolve({ok:false});
    requests.push(options);
    return new Promise((resolve, reject) => {
      options.signal.addEventListener('abort', () => reject(new Error('aborted')), {once:true});
    });
  }});
  let original = hanging.read('ws');
  let closed = hanging.disconnect();
  await hanging.flush();
  hanging.tick(4999);
  assert.equal(hanging.read('ws'), original, 'the normal retry delay remains five seconds');
  hanging.tick(1);
  await closed;
  assert.notEqual(hanging.read('ws'), original, 'a pending root request cannot block retry');
  assert.equal(requests.length, 1);
  assert(requests[0].signal.aborted, 'retry cancels the old HTTP request');
  assert.equal(hanging.read('reconnectProbe'), null);
  // Repeated outages must not retain a growing collection of HTTP requests.
  for (let attempt = 0; attempt < 10; attempt++) {
    original = hanging.read('ws');
    closed = hanging.disconnect();
    hanging.tick(5000);
    await closed;
    assert.notEqual(hanging.read('ws'), original);
    assert(requests.every(request => request.signal.aborted));
    assert.equal(hanging.read('reconnectProbe'), null);
  }

  const expired = await fixture({mediaFetch: async () => ({status:403})});
  original = expired.read('ws');
  await expired.disconnect();
  expired.tick(60000);
  assert.equal(expired.read('ws'), original, 'an expired invitation stops automatic retries');
  assert.equal(expired.element('status').textContent, '邀请已过期，请重新打开完整邀请链接');
  assert(expired.fetches.find(([url]) => url === '/')[1].signal.aborted,
    'a finished probe does not leave its unread response body open');

  const late = deferredProbe();
  const manual = await fixture({mediaFetch: url => url === '/' ? late.promise : Promise.resolve({ok:false})});
  closed = manual.disconnect();
  const oldSignal = manual.fetches.find(([url]) => url === '/')[1].signal;
  manual.read('connectSocket()');
  const current = manual.read('ws');
  manual.receive({type:'hello', tts:'indextts2', audio_owner:true,
    playback:{state:'idle',queued:0}, conversation_state:{}});
  const connectedStatus = manual.element('status').textContent;
  assert(oldSignal.aborted, 'manual retry aborts the pending probe immediately');
  late.resolve({status:403}); // Also protect against a transport that settles after cancellation.
  await closed;
  manual.tick(60000);
  assert.equal(manual.read('ws'), current, 'old timers cannot replace the new socket');
  assert.equal(manual.element('status').textContent, connectedStatus,
    'an old invitation response cannot revoke the new connection');

  const busyProbe = deferredProbe();
  const busy = await fixture({mediaFetch: url => url === '/' ? busyProbe.promise : Promise.resolve({ok:false})});
  busy.receive({type:'room_busy'});
  original = busy.read('ws');
  closed = busy.disconnect();
  busy.tick(9999);
  assert.equal(busy.read('ws'), original, 'an occupied test room retains its ten-second retry delay');
  busy.tick(1);
  assert.notEqual(busy.read('ws'), original);
  busyProbe.resolve({status:200});
  await closed;

  console.log('PASS: pending probes cannot block reconnect; retries cancel probes, 403 stops retries, and stale completions are ignored');
})().catch(error => {console.error(error);process.exitCode = 1;});
