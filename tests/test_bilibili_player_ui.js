const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const BilibiliDeck = require('../brain/static/bilibili-player.js');
const createProvider = require('./fixtures/bilibili_browser_provider.cjs');
const html = fs.readFileSync(path.join(__dirname, '../brain/static/bilibili.html'), 'utf8');
const standalone = fs.readFileSync(path.join(__dirname, '../brain/static/bilibili.js'), 'utf8');
const moduleSource = fs.readFileSync(path.join(__dirname, '../brain/static/bilibili-player.js'), 'utf8');
const first = {bvid:'BV1xx411c7mD', title:'第一首', author:'歌手甲', duration:'03:10', cover:'https://i0.hdslb.com/test.jpg'};
const second = {bvid:'BV1B7411m7LV', title:'第二首', author:'歌手乙', duration:'02:15'};
const response = (results, options = {}) => ({
  ok: options.ok ?? true, status: options.status ?? 200,
  headers: {get: () => options.contentType ?? 'application/json; charset=utf-8'},
  json: options.json || (async () => options.data ?? {results}),
});
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
}
async function flush() { for (let i = 0; i < 40; i++) await Promise.resolve(); }
function fixture({fetcher = async () => response([first, second]), beforePlay, beforeRequest, narrate, narrationTimeoutMs, onCancel, reviewAvailable, readVideo, optional = true} = {}) {
  const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(match => match[1]);
  if (optional) ids.push('bilibili-close');
  const elements = new Map(), requests = [], phases = [], selections = [], created = [];
  function element(tag = 'div') {
    const node = {tagName:tag.toUpperCase(), children:[], hidden:false, textContent:'', value:'', disabled:false, parentNode:null,
      append(...nodes) { for (const child of nodes) { child.parentNode = this; this.children.push(child); } },
      replaceChildren(...nodes) { for (const child of this.children) child.parentNode = null; this.children = []; this.append(...nodes); },
      insertAdjacentElement(position, child) {
        assert.equal(position, 'afterend');
        assert(this.parentNode, 'adjacent insertion requires an actual parent');
        child.parentNode = this.parentNode;
        this.parentNode.children.splice(this.parentNode.children.indexOf(this) + 1, 0, child);
      },
      removeAttribute(name) { delete this[name]; },
      scrollIntoView() { this.scrolled = true; },
    };
    Object.defineProperty(node, 'innerHTML', {set() { throw new Error('Untrusted HTML insertion'); }});
    created.push(node);
    return node;
  }
  for (const id of ids) elements.set(id, element());
  if (!optional) for (const name of ['search', 'player-status', 'external-search']) elements.delete('bilibili-' + name);
  const get = name => elements.get('bilibili-' + name);
  get('panel').append(get('status'), get('results'), get('player-area'));
  get('player-area').hidden = true;
  const document = {getElementById:id => elements.get(id) || null, createElement:element};
  const fetch = (...args) => { requests.push(args); return fetcher(...args); };
  const provider = createProvider(fetch);
  if (readVideo) provider.readVideo = readVideo;
  const deck = new BilibiliDeck({documentRef:document, browserSearch:provider, beforeRequest, narrate, narrationTimeoutMs, onCancel, reviewAvailable,
    beforePlay(item) { selections.push(item.bvid); return beforePlay?.(item); },
    onChange(state) { phases.push(state); },
  });
  return {deck, provider, get, document, fetch, requests, phases, selections, created};
}

async function main() {
  const bv = fixture();
  const pending = bv.deck.request(first.bvid);
  assert.equal(bv.deck.active, true, 'loading reserves the media channel');
  assert.equal(bv.selections.length, 0, 'searching alone never interrupts speech');
  assert.equal(await pending, true);
  assert.deepEqual(bv.selections, [first.bvid]);
  assert.equal(bv.get('results').children.length, 0);
  assert.equal(bv.get('player').children.length, 1);
  const frame = bv.get('player').children[0];
  const src = new URL(frame.src);
  assert.equal(src.origin, 'https://player.bilibili.com');
  assert.equal(src.pathname, '/player.html');
  assert.equal(src.searchParams.get('bvid'), first.bvid);
  for (const [key, value] of Object.entries({autoplay:'1', muted:'0', danmaku:'0', p:'1'})) assert.equal(src.searchParams.get(key), value);
  assert.match(frame.allow, /autoplay/);
  assert.equal(bv.deck.phase, 'opened');
  assert.match(bv.get('status').textContent, /若浏览器限制/);
  frame.onload();
  assert.doesNotMatch(bv.get('status').textContent, /正在播放|已经播放|已开始播放/);
  assert.deepEqual(bv.phases.map(item => item.phase), ['preparing', 'opened']);
  bv.deck.stop();
  assert.equal(frame.parentNode, null);
  assert.equal(bv.deck.active, false);
  assert.equal(bv.get('stop').disabled, true);
  console.log('PASS: explicit BV opens official unmuted autoplay request, without claiming actual playback');

  const part = fixture();
  await part.deck.request('https://m.bilibili.com/video/' + first.bvid + '/?p=3&share_source=copy#reply');
  assert.equal(part.requests.length,0,'explicit parts do not require browser lookup');
  assert.equal(new URL(part.deck.frame.src).searchParams.get('p'), '3');
  assert.equal(part.get('original').href, 'https://www.bilibili.com/video/' + first.bvid + '?p=3');
  await part.deck.request('bv' + first.bvid.slice(2));
  assert.equal(new URL(part.deck.frame.src).searchParams.get('bvid'), first.bvid);
  for (const host of ['www.bilibili.com', 'm.bilibili.com', 'bilibili.com', '//www.bilibili.com']) {
    await part.deck.request(host + '/video/' + first.bvid + '/?p=2');
    assert.equal(new URL(part.deck.frame.src).searchParams.get('p'), '2');
    assert.equal(part.requests.length,0);
  }
  const av = fixture();
  assert.equal(await av.deck.request('www.bilibili.com/video/av1234'), false);
  assert.equal(av.requests.length, 0);
  assert.match(av.get('status').textContent, /暂不支持 AV.*BV/);
  console.log('PASS: official mobile/bare links preserve explicit part and normalize safely; AV fails clearly');

  const hostile = {...first, title:'<img src=x onerror=alert(1)>', author:'<script>x</script>', url:'javascript:alert(1)', cover:'https://i0.hdslb.com.evil.test/track'};
  const cards = fixture({fetcher:async () => response([hostile, {...first}, {...second, cover:'//i0.hdslb.com/cover.jpg'}, {bvid:'bad', title:'invalid'}])});
  await cards.deck.request('歌名 & 歌手');
  assert.equal(cards.requests[0][0], '/fixture/bilibili-browser?q=' + encodeURIComponent('歌名 & 歌手'));
  assert.equal(cards.deck.active, true, 'manual results retain a cancellable media reservation');
  assert.equal(cards.get('stop').disabled,false);
  assert.equal(cards.deck.frame, null);
  assert.equal(cards.selections.length, 0);
  assert.equal(cards.get('results').children.length, 2, 'invalid and duplicate backend entries are discarded');
  const card = cards.get('results').children[0];
  assert.equal(card.children.find(child => child.tagName === 'H2').textContent, hostile.title);
  assert.equal(card.children.some(child => child.tagName === 'IMG'), false);
  assert.equal(card.children.find(child => child.tagName === 'A').href, BilibiliDeck.videoURL(first.bvid));
  assert.equal(cards.get('results').children[1].children[0].src, 'https://i0.hdslb.com/cover.jpg');
  await card.children.find(child => child.tagName === 'BUTTON').onclick();
  assert.deepEqual(cards.selections, [first.bvid]);
  assert.equal(cards.deck.frame.title, hostile.title, 'title stays inert text in the selected frame');
  const previous = cards.deck.frame;
  await cards.get('results').children[1].children.find(child => child.tagName === 'BUTTON').onclick();
  assert.equal(previous.parentNode, null);
  assert.equal(new URL(cards.deck.frame.src).searchParams.get('bvid'), second.bvid);
  console.log('PASS: unknown play counts require a real result selection; result text, domains and generated links are safe');

  for (const bad of ['https://bilibili.com.evil.test/video/' + first.bvid, 'https://evil.test/video/' + first.bvid,
    'javascript:alert(1)', 'https://u:p@www.bilibili.com/video/' + first.bvid, 'https://www.bilibili.com:8080/video/' + first.bvid,
    'https://www.bilibili.com/video/' + first.bvid + '?p=0', 'https://www.bilibili.com/video/' + first.bvid + '?p=1&p=2', 'BV1234', '']) {
    const invalid = fixture();
    assert.equal(await invalid.deck.request(bad), false, bad);
    assert.equal(invalid.requests.length, 0, bad + ' never reaches the server');
    assert.equal(invalid.deck.active, false);
    assert.equal(invalid.deck.frame, null);
    assert.ok(invalid.get('status').textContent);
  }
  console.log('PASS: invalid BV, untrusted URLs, credentials, ports and invalid part are rejected before querying');

  const failures = [
    () => response([]),
    () => response([], {ok:false, status:503, data:{error:'B站暂时不可用'}}),
    () => response([], {ok:false, status:403, contentType:'text/html'}),
    () => response([], {json:async () => { throw new SyntaxError('invalid'); }}),
    () => response([], {data:{bad:true}}),
    () => { throw new TypeError('offline'); },
  ];
  for (const fetcher of failures) {
    const failed = fixture({fetcher});
    assert.equal(await failed.deck.request('missing keyword'), false);
    assert.equal(failed.get('results').children.length, 0);
    assert.equal(failed.deck.frame, null);
    assert.equal(failed.deck.active, false);
    assert.equal(failed.selections.length, 0);
    assert.equal(failed.get('player-area').hidden, true);
    assert.equal(failed.get('search').disabled, false);
  }
  const rejected = fixture({beforePlay:() => false});
  const offline = fixture({fetcher:async () => { throw new TypeError('Failed to fetch'); }});
  await offline.deck.request('offline keyword');
  assert.match(offline.get('status').textContent, /无法连接查询服务/);
  assert.equal(await rejected.deck.request(first.bvid), false);
  assert.equal(rejected.deck.frame, null);
  const throwing = fixture({beforePlay:() => { throw new Error('interruption failed'); }});
  assert.equal(await throwing.deck.request(first.bvid), false);
  assert.equal(throwing.deck.active, false);
  assert.match(throwing.get('status').textContent, /播放准备失败/);
  console.log('PASS: query and preparation failures show no invented results or frames, with official original fallback');

  const late = deferred();
  const cancelled = fixture({fetcher:() => late.promise});
  const query = cancelled.deck.request('cancelled keyword');
  cancelled.get('stop').onclick();
  const stopText = cancelled.get('status').textContent;
  assert.equal(cancelled.requests[0][1].signal.aborted, true);
  late.resolve(response([first]));
  assert.equal(await query, false);
  assert.equal(cancelled.deck.frame, null);
  assert.equal(cancelled.get('status').textContent, stopText);
  assert.equal(cancelled.selections.length, 0);
  const old = deferred(), latest = deferred();
  let queryCount = 0;
  const overlap = fixture({fetcher:() => ++queryCount === 1 ? old.promise : latest.promise});
  const oldPromise = overlap.deck.request('older keyword');
  const newPromise = overlap.deck.request('newer keyword');
  latest.resolve(response([{...second,play_count:300000}])); await newPromise;
  const newestFrame = overlap.deck.frame;
  old.resolve(response([first])); await oldPromise;
  assert.equal(overlap.deck.frame, newestFrame);
  assert.deepEqual(overlap.selections, [second.bvid]);
  console.log('PASS: stop aborts querying; responses that ignore abort cannot revive or replace newer playback');

  const jsonLate = deferred();
  const duringJSON = fixture({fetcher:async () => response([], {json:() => jsonLate.promise})});
  const parsing = duringJSON.deck.request('body parsing keyword'); await flush();
  duringJSON.deck.stop(); jsonLate.resolve({results:[first]}); await parsing;
  assert.equal(duringJSON.deck.frame, null, 'cancellation also covers response body parsing');
  const chooseOne = deferred(), chooseTwo = deferred();
  let preparationCount = 0;
  const preparing = fixture({beforePlay:() => ++preparationCount === 1 ? chooseOne.promise : chooseTwo.promise});
  await preparing.deck.request('versions');
  const buttons = preparing.get('results').children.map(item => item.children.find(child => child.tagName === 'BUTTON'));
  const firstChoice = buttons[0].onclick(); await flush();
  const secondChoice = buttons[1].onclick();
  chooseTwo.resolve(); assert.equal(await secondChoice, true);
  const selected = preparing.deck.frame;
  chooseOne.resolve(); assert.equal(await firstChoice, false);
  assert.equal(preparing.deck.frame, selected);
  const staleLoad = selected.onload;
  preparing.get('close').onclick();
  const closedText = preparing.get('status').textContent;
  staleLoad();
  assert.equal(preparing.get('status').textContent, closedText);
  assert.equal(preparing.get('panel').hidden, true);
  assert.equal(preparing.deck.active, false);
  assert.equal(await buttons[0].onclick(), false, 'detached cards cannot reopen after close');
  const gate = deferred();
  const stoppedPreparation = fixture({beforePlay:() => gate.promise});
  const preparingQuery = stoppedPreparation.deck.request(first.bvid); await flush();
  assert.equal(stoppedPreparation.deck.phase, 'preparing');
  stoppedPreparation.deck.stop(); gate.resolve(); await preparingQuery;
  assert.equal(stoppedPreparation.deck.frame, null);
  console.log('PASS: body parsing, async preparation, stale cards and iframe callbacks respect stop/new-selection ownership');

  const form = fixture({optional:false});
  form.get('query').value = '版本';
  let prevented = false;
  form.get('form').onsubmit({preventDefault() { prevented = true; }});
  await flush();
  assert.equal(prevented, true);
  assert.equal(form.get('results').children.length, 2);
  assert.equal(form.deck.frame, null);
  const direct = fixture();
  const context = vm.createContext({document:direct.document, window:{}, location:{search:'?q=' + first.bvid},
    BilibiliBrowserSearch:class {constructor(){return direct.provider;}},
    fetch:direct.fetch, URL, URLSearchParams, AbortController, console});
  vm.runInContext(moduleSource, context);
  vm.runInContext(standalone, context);
  await flush();
  assert.equal(context.window.bilibiliDeck.phase, 'opened');
  assert.equal(new URL(context.window.bilibiliDeck.frame.src).searchParams.get('bvid'), first.bvid);
  assert.ok(html.indexOf('/static/bilibili-player.js') < html.indexOf('/static/bilibili.js'));
  assert.equal(html.includes('id="bilibili-stop" type="button" disabled'), true);
  assert.equal(direct.document.getElementById('bilibili-console').href, '/?bilibili=' + first.bvid);
  direct.get('query').value = '歌名 & 歌手'; direct.get('query').oninput();
  assert.equal(direct.document.getElementById('bilibili-console').href, '/?bilibili=' + encodeURIComponent('歌名 & 歌手'));
  console.log('PASS: actual standalone scripts bind the shared DOM, support initial q, and optional console controls are optional');

  const ack = deferred(), found = deferred(), chronology = [], narrationSignals = [];
  const voiced = fixture({beforeRequest:({query, signal}) => { chronology.push('beforeRequest'); assert.equal(query, first.bvid); assert.equal(signal.aborted, false); },
    fetcher:async () => { chronology.push('fetch'); return response([first]); },
    beforePlay:() => chronology.push('beforePlay'),
    narrate:({stage, item, signal}) => {
      chronology.push(stage); narrationSignals.push(signal);
      if (stage === 'ack') { assert.equal(item, undefined); return ack.promise; }
      assert.equal(item.bvid, first.bvid); assert.equal(item.title, '指定视频 · ' + first.bvid); assert.equal(stage,'direct'); return found.promise;
    },
  });
  const voicedRequest = voiced.deck.request(first.bvid); await flush();
  assert.deepEqual(chronology, ['beforeRequest', 'ack']);
  assert.equal(voiced.deck.phase, 'ack'); assert.equal(voiced.deck.active, true);
  assert.equal(voiced.requests.length, 0, 'ack must finish before even querying');
  ack.resolve(true); await flush();
  assert.deepEqual(chronology, ['beforeRequest', 'ack', 'beforePlay', 'direct']);
  assert.equal(voiced.deck.phase, 'narrating');
  assert.equal(voiced.get('player').children.length, 0, 'a verified result is not permission to overlap introduction audio');
  found.resolve(true); assert.equal(await voicedRequest, true);
  assert.equal(voiced.deck.frame.tagName, 'IFRAME');
  assert.equal(narrationSignals.some(signal => signal.aborted), false);
  console.log('PASS: explicit BV acknowledgement and generic direct introduction finish before iframe creation without lookup');

  const stages = [];
  const choices = fixture({narrate:async event => { stages.push(event); return true; }});
  await choices.deck.request('不同版本');
  assert.deepEqual(stages.map(event => event.stage), ['ack', 'results']);
  assert.equal(stages[1].item, undefined, 'search results never pretend a version is already selected');
  assert.equal(stages[1].error, 'missing_play_count');
  assert.match(choices.get('status').textContent, /播放量.*手动选择/);
  assert.equal(choices.deck.frame, null);
  await choices.get('results').children[1].children.find(node => node.tagName === 'BUTTON').onclick();
  assert.equal(stages.at(-1).stage, 'found'); assert.equal(stages.at(-1).item.bvid, second.bvid);
  assert.equal(stages.at(-1).selection, undefined, 'manual selection never claims the largest audience');
  for (const [kind, fetcher] of [
    ['empty', async () => response([])],
    ['network', async () => { throw new TypeError('offline'); }],
    ['service', async () => response([], {ok:false, status:503, data:{error:'unavailable'}})],
  ]) {
    const events = [];
    const fail = fixture({fetcher, narrate:async event => { events.push(event); return true; }});
    assert.equal(await fail.deck.request('不存在的歌曲'), false);
    assert.deepEqual(events.map(event => event.stage), ['ack', 'error']);
    assert.equal(events[1].error, kind);
    assert.equal(events[1].item, undefined);
    assert.equal(fail.deck.frame, null); assert.equal(fail.deck.active, false);
  }
  console.log('PASS: unknown-count narration asks for selection; empty/network/service errors cannot claim a found video');

  const blockedAck = fixture({narrate:async () => false});
  assert.equal(await blockedAck.deck.request(first.bvid), false);
  assert.equal(blockedAck.requests.length, 0);
  assert.equal(blockedAck.deck.active, true);
  assert.match(blockedAck.get('status').textContent, /尚未打开指定视频/);
  assert.equal(blockedAck.deck.continueButton.textContent, '跳过回应，继续准备播放');
  await blockedAck.deck.continueButton.onclick();
  assert.equal(blockedAck.requests.length, 0, 'explicit BV continuation never queries a browser');
  assert.equal(blockedAck.deck.frame, null, 'found also failed; still no automatic iframe');
  assert.equal(blockedAck.deck.continueButton.textContent, '语音没播完，点击继续播放');
  assert.equal(await blockedAck.deck.continueButton.onclick(), true);
  assert.equal(blockedAck.deck.phase, 'opened');
  const ackTimeout = fixture({narrationTimeoutMs:10, narrate:() => new Promise(() => {})});
  assert.equal(await ackTimeout.deck.request(first.bvid), false);
  assert.equal(ackTimeout.requests.length, 0, 'an acknowledgement timeout never starts a hidden query');
  assert.equal(ackTimeout.deck.continueButton.textContent, '跳过回应，继续准备播放');
  for (const mode of ['false', 'reject', 'timeout']) {
    let signal;
    const failures = fixture({narrationTimeoutMs:10, narrate:async event => {
      if (event.stage === 'ack') return true;
      signal = event.signal;
      if (mode === 'false') return false;
      if (mode === 'reject') throw new Error('TTS failed');
      return new Promise(() => {});
    }});
    assert.equal(await failures.deck.request(first.bvid), false);
    assert.equal(signal.aborted, true, mode + ' cancels unfinished audio');
    assert.equal(failures.deck.frame, null, mode + ' cannot mount iframe automatically');
    assert.match(failures.deck.continueButton.textContent, /点击继续播放/);
    assert.equal(await failures.deck.continueButton.onclick(), true);
    assert.equal(failures.deck.frame.tagName, 'IFRAME');
  }
  console.log('PASS: false, rejected and timed-out narration require explicit continuation; failed acknowledgement never searches silently');

  for (const stage of ['ack', 'direct']) {
    const deferredVoice = deferred(); let signal, cancellations = 0;
    const stopped = fixture({onCancel:() => cancellations++, narrate:event => {
      if (event.stage !== stage) return Promise.resolve(true);
      signal = event.signal; return deferredVoice.promise;
    }});
    const request = stopped.deck.request(first.bvid); await flush();
    assert.equal(stopped.deck.phase, stage === 'ack' ? 'ack' : 'narrating');
    stopped.deck.stop();
    assert.equal(await request, false, 'abort releases callbacks that ignore cancellation');
    assert.equal(signal.aborted, true); assert.equal(cancellations, 1);
    const stoppedText = stopped.get('status').textContent;
    deferredVoice.resolve(true); await flush();
    assert.equal(stopped.deck.frame, null); assert.equal(stopped.deck.continueButton, null);
    assert.equal(stopped.get('status').textContent, stoppedText);
  }
  const pendingSetup = deferred();
  const setup = fixture({beforeRequest:() => pendingSetup.promise, narrate:async () => true});
  const setupRequest = setup.deck.request(first.bvid); await flush(); setup.deck.stop();
  assert.equal(await setupRequest, false); pendingSetup.resolve(); await flush();
  assert.equal(setup.requests.length, 0);
  const delayedResults = deferred(); let resultsSignal;
  const changed = fixture({narrate:event => {
    if (event.stage === 'results') { resultsSignal = event.signal; return delayedResults.promise; }
    return Promise.resolve(true);
  }});
  const resultsRequest = changed.deck.request('版本'); await flush();
  await changed.get('results').children[1].children.find(node => node.tagName === 'BUTTON').onclick();
  assert.equal(resultsSignal.aborted, true);
  assert.equal(await resultsRequest, false);
  delayedResults.resolve(true); await flush();
  assert.equal(changed.deck.phase, 'opened');
  assert.equal(new URL(changed.deck.frame.src).searchParams.get('bvid'), second.bvid);
  console.log('PASS: stopping ack/found/setup releases waiters, and selecting a result cancels outdated results narration');

  const pendingFirst = deferred(), pendingSecond = deferred(); let firstSignal;
  const switching = fixture({narrate:event => {
    if (event.stage !== 'found') return Promise.resolve(true);
    if (event.item.bvid === first.bvid) { firstSignal = event.signal; return pendingFirst.promise; }
    return pendingSecond.promise;
  }});
  await switching.deck.request('版本');
  const options = switching.get('results').children.map(node => node.children.find(child => child.tagName === 'BUTTON'));
  const oldChoice = options[0].onclick(); await flush();
  const newChoice = options[1].onclick(); await flush();
  assert.equal(firstSignal.aborted, true);
  pendingFirst.resolve(true); await flush();
  assert.equal(switching.deck.frame, null);
  pendingSecond.resolve(true); assert.equal(await newChoice, true); assert.equal(await oldChoice, false);
  assert.equal(new URL(switching.deck.frame.src).searchParams.get('bvid'), second.bvid);
  const failedNew = fixture({narrate:async ({stage}) => stage === 'ack'});
  await failedNew.deck.request(first.bvid); const staleContinue = failedNew.deck.continueButton;
  await failedNew.deck.request(second.bvid); assert.equal(await staleContinue.onclick(), false);
  assert.equal(failedNew.deck.frame, null);
  assert.equal(await failedNew.deck.continueButton.onclick(), true);
  assert.equal(new URL(failedNew.deck.frame.src).searchParams.get('bvid'), second.bvid);
  const staleAck = deferred(); let staleSignal;
  const newRequest = fixture({narrate:event => {
    if (event.stage === 'ack' && event.query === first.bvid) { staleSignal = event.signal; return staleAck.promise; }
    return Promise.resolve(true);
  }});
  const earlierRequest = newRequest.deck.request(first.bvid); await flush();
  assert.equal(await newRequest.deck.request(second.bvid), true);
  assert.equal(await earlierRequest, false); assert.equal(staleSignal.aborted, true);
  staleAck.resolve(true); await flush();
  assert.equal(newRequest.requests.length,0);
  assert.equal(new URL(newRequest.deck.frame.src).searchParams.get('bvid'), second.bvid);
  console.log('PASS: selecting another track aborts its predecessor, and stale continuation buttons cannot open old videos');

  const low = {...first, play_count:25};
  const high = {...second, play_count:420000};
  const tied = {bvid:'BV1Q541167Qg',title:'并列版本',play_count:420000};
  const unknown = {bvid:'BV1aK4y1i7Nk',title:'未知版本',play_count:null};
  const automaticAck = deferred(), automaticFound = deferred(), automaticStages = [];
  const automatic = fixture({fetcher:async()=>response([low,{...unknown,play_count:0},high,tied]), narrate:event=>{
    automaticStages.push(event);
    return event.stage === 'ack' ? automaticAck.promise : automaticFound.promise;
  }});
  const automaticRequest = automatic.deck.request('海色'); await flush();
  assert.deepEqual(automaticStages.map(event=>event.stage), ['ack']);
  assert.equal(automatic.requests.length, 0);
  automaticAck.resolve(true); await flush();
  assert.deepEqual(automaticStages.map(event=>event.stage), ['ack','found'],
    'a keyword result with reliable counts automatically introduces the selected video');
  assert.equal(automaticStages[1].item.bvid, high.bvid, 'the server-confirmed automatic selection reaches narration');
  assert.equal(automaticStages[1].selection, 'highest_play');
  assert.equal(automaticStages[1].item.play_count, 420000);
  assert.equal(automatic.deck.frame, null, 'the highest count never bypasses actual introduction completion');
  const rankedCards = automatic.get('results').children;
  assert.equal(rankedCards.length, 4, 'automatic selection keeps alternative versions available');
  const cardTitle = node => node.children.find(child=>child.tagName==='H2').textContent;
  assert.deepEqual(rankedCards.map(cardTitle), [high.title,tied.title,low.title,unknown.title],
    'ties are stable and unknown counts appear after known counts');
  assert.match(rankedCards[0].children.find(child=>child.tagName==='P').textContent, /播放量 420,000/);
  assert.match(rankedCards[3].children.find(child=>child.tagName==='P').textContent, /播放量 0/);
  automaticFound.resolve(true); assert.equal(await automaticRequest, true);
  assert.equal(new URL(automatic.deck.frame.src).searchParams.get('bvid'), high.bvid);
  await rankedCards[2].children.find(child=>child.tagName==='BUTTON').onclick();
  assert.equal(new URL(automatic.deck.frame.src).searchParams.get('bvid'), low.bvid);
  assert.equal(automaticStages.at(-1).selection, undefined, 'choosing an alternate version does not reuse the automatic highest claim');
  console.log('PASS: keyword playback uses the server-approved greatest-count snapshot, keeps version cards and waits for introduction completion');

  for (const invalidCount of [undefined,null,-1,NaN,Infinity,1.5,Number.MAX_SAFE_INTEGER+1,'999999',true]) {
    const zero = fixture({fetcher:async()=>response([{...first,play_count:invalidCount},{...second,play_count:0}])});
    assert.equal(await zero.deck.request('zero views'), true);
    assert.equal(zero.deck.frame, null, 'verified zero remains below the autoplay threshold');
    const zeroCard = zero.get('results').children[0];
    assert.equal(cardTitle(zeroCard), second.title,
      'verified zero still sorts before an unknown, negative, fractional, unsafe or nonnumeric count');
    assert.match(zero.get('status').textContent, /(?:没|未).*自动播放/);
    await zeroCard.children.find(child=>child.tagName==='BUTTON').onclick();
    assert.equal(new URL(zero.deck.frame.src).searchParams.get('bvid'), second.bvid,
      'a deliberate manual selection is not subject to the automatic threshold');
    assert.equal(BilibiliDeck.result({...first,play_count:invalidCount}).play_count, null);
  }
  const exactStages = [];
  const exactBV = fixture({fetcher:async()=>response([high,low]), narrate:async event=>{exactStages.push(event);return true;}});
  await exactBV.deck.request(BilibiliDeck.videoURL(low.bvid,3));
  assert.equal(new URL(exactBV.deck.frame.src).searchParams.get('bvid'), low.bvid,
    'a directly requested BV is authoritative even if a different result has more plays');
  assert.equal(new URL(exactBV.deck.frame.src).searchParams.get('p'), '3');
  assert.equal(exactStages.at(-1).selection, undefined);
  assert.equal(exactStages.at(-1).query, BilibiliDeck.videoURL(low.bvid,3));
  console.log('PASS: only safe integer counts rank, zero requires manual selection and explicit BV parts never change');

  assert.equal(BilibiliDeck.minAutoPlayCount, 200000);
  for (const playCount of [0,199999,200000]) {
    const thresholdEvents = [];
    const boundary = fixture({fetcher:async()=>response([{...first,play_count:playCount}]),
      narrate:async event=>{thresholdEvents.push(event);return true;}});
    await boundary.deck.request('门槛边界');
    if(playCount===200000) {
      assert.deepEqual(thresholdEvents.map(event=>event.stage), ['ack','found']);
      assert.equal(thresholdEvents.at(-1).selection, 'highest_play');
      assert.equal(new URL(boundary.deck.frame.src).searchParams.get('bvid'), first.bvid,
        'exactly 200000 is eligible for automatic playback');
    } else {
      assert.deepEqual(thresholdEvents.map(event=>event.stage), ['ack','results']);
      assert.equal(thresholdEvents.at(-1).error, 'below_play_threshold');
      assert.equal(thresholdEvents.at(-1).selection, undefined);
      assert.equal(boundary.deck.frame, null, 'results narration completion never opens a below-threshold video');
      assert.equal(boundary.get('results').children.length, 1);
      assert.match(boundary.get('status').textContent, /未.*自动播放/);
      await boundary.get('results').children[0].children.find(child=>child.tagName==='BUTTON').onclick();
      assert.equal(thresholdEvents.at(-1).stage, 'found');
      assert.equal(thresholdEvents.at(-1).selection, undefined);
      assert.equal(new URL(boundary.deck.frame.src).searchParams.get('bvid'), first.bvid);
    }
  }
  console.log('PASS: 199999 and zero never autoplay, exactly 200000 is eligible, and manual choices remain available');

  const cancelledHighest = deferred(); let highestSignal;
  const stopHighest = fixture({fetcher:async()=>response([low,high]), narrate:event=>{
    if(event.stage==='ack') return true;
    highestSignal=event.signal; return cancelledHighest.promise;
  }});
  const highestRequest=stopHighest.deck.request('海色'); await flush();
  stopHighest.deck.stop(); assert.equal(await highestRequest,false);
  assert.equal(highestSignal.aborted,true);
  cancelledHighest.resolve(true); await flush();
  assert.equal(stopHighest.deck.frame,null);
  assert.equal(stopHighest.get('results').children.length,0);
  const lateAutomatic=deferred(), manualNarration=deferred(); let automaticSignal;
  const overrideHighest=fixture({fetcher:async()=>response([low,high]), narrate:event=>{
    if(event.stage==='ack') return true;
    if(event.selection==='highest_play'){automaticSignal=event.signal;return lateAutomatic.promise;}
    return manualNarration.promise;
  }});
  const autoChoice=overrideHighest.deck.request('海色'); await flush();
  const manualChoice=overrideHighest.get('results').children[1].children.find(child=>child.tagName==='BUTTON').onclick(); await flush();
  assert.equal(automaticSignal.aborted,true);
  lateAutomatic.resolve(true); await flush();
  assert.equal(overrideHighest.deck.frame,null);
  manualNarration.resolve(true); assert.equal(await manualChoice,true); assert.equal(await autoChoice,false);
  assert.equal(new URL(overrideHighest.deck.frame.src).searchParams.get('bvid'),low.bvid);
  console.log('PASS: stopping or manually overriding automatic top selection cancels its late narration without playback');

  const failedHighest=fixture({fetcher:async()=>response([low,high]), narrate:async event=>event.stage==='ack'});
  assert.equal(await failedHighest.deck.request('海色'),false);
  assert.equal(failedHighest.deck.frame,null);
  assert.equal(failedHighest.get('results').children.length,2);
  assert.match(failedHighest.deck.continueButton.textContent,/点击继续播放/);
  assert.equal(await failedHighest.deck.continueButton.onclick(),true);
  assert.equal(new URL(failedHighest.deck.frame.src).searchParams.get('bvid'),high.bvid);
  console.log('PASS: failed highest-result narration still requires an explicit user continuation');

  for (const code of ['network','timeout','rate_limited','upstream_rejected','invalid_response','service','closed','unexpected','empty']) {
    const errors = [], message = '本次查询未成功：'+code+'，请稍后重试。';
    const classified = fixture({fetcher:async()=>response([], {ok:false,status:code==='rate_limited'?429:502,
      data:{error:message,error_code:code,retry_after:2}}), narrate:async event=>{errors.push(event);return true;}});
    assert.equal(await classified.deck.request("it's happening"),false);
    assert.deepEqual(errors.map(event=>event.stage),['ack','error']);
    assert.equal(errors[1].error,['closed','unexpected','empty'].includes(code)?'service':code);
    assert.equal(classified.get('status').textContent,message,'the concrete backend message remains visible verbatim as text');
    assert.equal(new URL(classified.requests[0][0],'https://fixture.test').searchParams.get('q'),"it's happening");
    assert.equal(new URL(classified.get('external-search').href).searchParams.get('keyword'),"it's happening");
    assert.equal(classified.requests.length,1,'the browser must not add retries to server retry or rate-limit policy');
    assert.equal(classified.deck.frame,null);
  }
  for(const malformed of [
    ()=>response([], {ok:false,status:403,contentType:'text/html'}),
    ()=>response([], {json:async()=>{throw new SyntaxError('bad JSON');}}),
    ()=>response([], {data:'unexpected'}),
    ()=>response([], {data:{unexpected:true}}),
  ]) {
    const errors=[];
    const invalid=fixture({fetcher:malformed,narrate:async event=>{errors.push(event);return true;}});
    assert.equal(await invalid.deck.request("it's happening"),false);
    assert.equal(errors.at(-1).error,'invalid_response');
    assert.doesNotMatch(invalid.get('status').textContent,/验证|登录/,'an HTTP status or malformed body does not prove a verification requirement');
    assert.equal(invalid.requests.length,1);
  }
  console.log('PASS: typed search failures retain safe categories and exact queries without retrying or guessing a verification requirement');

  for(const action of ['stop','new query']) {
    const lateError=deferred(), events=[]; let count=0;
    const superseded=fixture({fetcher:()=>++count===1?lateError.promise:Promise.resolve(response([high])),
      narrate:async event=>{events.push(event);return true;}});
    const obsolete=superseded.deck.request("it's happening");await flush();
    if(action==='stop') superseded.deck.stop();
    else assert.equal(await superseded.deck.request(second.bvid),true);
    const frame=superseded.deck.frame, notice=superseded.get('status').textContent;
    lateError.resolve(response([], {ok:false,status:429,data:{error:'过期的限流错误',error_code:'rate_limited',retry_after:2}}));
    assert.equal(await obsolete,false);await flush();
    assert.equal(events.some(event=>event.stage==='error'),false,'an obsolete query cannot emit a late error narration');
    assert.equal(superseded.deck.frame,frame);
    assert.equal(superseded.get('status').textContent,notice);
  }
  console.log('PASS: cancellation and newer requests discard late typed failures without error narration or playback changes');

  const snapshotEvents = [];
  const snapshot = {query:'server choice',result_id:'verified-snapshot',results:[{...first,play_count:900000},
    {...second,play_count:300000}],auto_bvid:second.bvid,selection_reason:'highest_play'};
  const chosen = fixture({fetcher:async()=>response([], {data:snapshot}),
    narrate:async event=>{snapshotEvents.push(event);return true;}});
  await chosen.deck.request('server choice');
  assert.equal(new URL(chosen.deck.frame.src).searchParams.get('bvid'), second.bvid,
    'only the server capability chooses autoplay; frontend must not independently pick the largest numeric field');
  assert.equal(snapshotEvents.at(-1).result_id,'verified-snapshot');
  assert.equal(snapshotEvents.at(-1).query,'server choice');
  assert.equal(await chosen.deck.select({bvid:'BV1Q541167Qg',title:'forged'}),false,
    'only rows in the verified snapshot can be selected');
  for (const reason of ['uncertain_play_count','missing_play_count','below_play_threshold']) {
    const events=[];
    const approximate=fixture({fetcher:async()=>response([], {data:{query:'approximate',result_id:'approximate-snapshot',
      results:[{...first,play_text:'20.1万',play_count:201000,play_count_exact:false,
        play_count_lower:200500,play_count_upper:201500}],auto_bvid:null,selection_reason:reason}}),
      narrate:async event=>{events.push(event);return true;}});
    await approximate.deck.request('approximate');
    assert.equal(approximate.deck.frame,null,'null server auto_bvid is final even above the numeric threshold');
    assert.equal(events.at(-1).stage,'results');
    assert.equal(events.at(-1).result_id,'approximate-snapshot');
    const details=approximate.get('results').children[0].children.find(node=>node.tagName==='P').textContent;
    assert.match(details,/20\.1万/);assert.doesNotMatch(details,/201,000/);
    assert.doesNotMatch(approximate.get('status').textContent,/20万/);
    await approximate.get('results').children[0].children.find(node=>node.tagName==='BUTTON').onclick();
    assert.equal(events.at(-1).selection,undefined);
    assert.equal(events.at(-1).result_id,'approximate-snapshot');
    assert(approximate.deck.frame);
  }
  console.log('PASS: server snapshots alone authorize automatic choices; approximate counts stay approximate and manual narration retains result_id');

  for(const code of ['browser_required','verification_required','login_required','page_unavailable','browser_disconnected']){
    const events=[];
    const helperFailure=fixture({fetcher:async()=>{throw Object.assign(new Error('请连接助手或处理B站页面'),{code});},
      narrate:async event=>{events.push(event);return true;}});
    await helperFailure.deck.request('query');
    assert.equal(events.at(-1).error,code);
    assert.equal(helperFailure.deck.frame,null);
    assert.match(helperFailure.get('browser-status').textContent,/助手|B站/);
    assert.doesNotMatch(helperFailure.get('status').textContent,/20万/);
  }
  const missing=fixture();missing.deck.browserSearch=null;
  assert.equal(await missing.deck.request('query'),false);
  assert.match(missing.get('status').textContent,/安装.*连接浏览器助手/);
  assert.equal(missing.requests.length,0);
  const connected=fixture();await connected.get('browser-connect').onclick();
  assert.match(connected.get('browser-status').textContent,/已连接/);
  assert.ok(html.indexOf('/static/bilibili-browser.js')<html.indexOf('/static/bilibili-player.js'));
  assert(html.includes('/static/bilibili-browser-setup.html'));
  console.log('PASS: missing/disconnected helpers expose setup instructions and safe narration instead of anonymous fallback');
  const diagnostic=fixture({fetcher:async()=>{throw Object.assign(new Error('B站页面暂时不可用'),
    {code:'page_unavailable',stage:'tab_create',diagnostic_reason:'chrome_api_error'});}});
  await diagnostic.deck.request('query');
  assert.match(diagnostic.get('status').textContent,/阶段：创建B站标签页；浏览器操作失败/);
  const unknownDiagnostic=fixture({fetcher:async()=>{throw Object.assign(new Error('页面暂时不可用'),
    {code:'page_unavailable',stage:'https://private.test/',diagnostic_reason:'private page contents'});}});
  await unknownDiagnostic.deck.request('query');
  assert.equal(unknownDiagnostic.get('status').textContent,'页面暂时不可用');
  const oldHelper=fixture({fetcher:async()=>{throw Object.assign(new Error('页面暂时不可用'),{code:'page_unavailable'});}});
  oldHelper.provider.connected=true;oldHelper.provider.extensionVersion=null;
  await oldHelper.get('browser-connect').onclick();
  assert.match(oldHelper.get('browser-status').textContent,/旧版助手.*更新并重新加载扩展，再刷新控制台并重连/);
  await oldHelper.deck.request('query');
  assert.match(oldHelper.get('status').textContent,/旧版助手未提供失败步骤/);
  const currentHelper=fixture();currentHelper.provider.extensionVersion='1.0.1';
  await currentHelper.get('browser-connect').onclick();
  assert.match(currentHelper.get('browser-status').textContent,/版本 1\.0\.1/);
  assert.doesNotMatch(currentHelper.get('browser-status').textContent,/旧版/);
  console.log('PASS: failures expose safe translated diagnostics, actual helper versions and honest legacy reload guidance');

  for(const [stage,label] of [['session_load','读取助手会话'],['connect_save','保存助手连接']]) {
    const failedConnect=fixture();
    failedConnect.provider.connect=async()=>{throw Object.assign(new Error('浏览器助手暂时不可用'),
      {code:'page_unavailable',stage,diagnostic_reason:'storage_error'});};
    await failedConnect.get('browser-connect').onclick();
    assert.equal(failedConnect.get('browser-status').textContent,
      '浏览器助手暂时不可用（阶段：'+label+'；助手设置未能保存）');
    assert.equal(failedConnect.get('browser-connect').disabled,false);
  }
  for(const [stage,reason,expected] of [
    ['https://private.test/secret','private page body','连接失败'],
    ['session_load','private page body','连接失败（阶段：读取助手会话）'],
  ]) {
    const safeConnect=fixture();
    safeConnect.provider.connect=async()=>{throw Object.assign(new Error('连接失败'),{stage,diagnostic_reason:reason});};
    await safeConnect.get('browser-connect').onclick();
    assert.equal(safeConnect.get('browser-status').textContent,expected);
  }
  assert.match(oldHelper.get('status').textContent,/更新并重新加载扩展，再刷新控制台并重连/);
  console.log('PASS: manual connection failures retain safe session/load diagnostics and complete extension reload instructions');

  const noHelper=fixture({fetcher:async()=>{throw Error('direct playback must never query');}});
  noHelper.deck.browserSearch=null;
  assert.equal(await noHelper.deck.request('https://www.bilibili.com/video/'+first.bvid+'?p=3'),true);
  assert.equal(noHelper.requests.length,0);
  assert.equal(noHelper.get('title').textContent,'指定视频 · '+first.bvid);
  assert.equal(new URL(noHelper.deck.frame.src).searchParams.get('p'),'3');
  assert.match(noHelper.get('browser-status').textContent,/按歌名搜索时才需要/);
  assert.equal(await noHelper.deck.select({bvid:first.bvid},noHelper.deck.revision,2),false,
    'a direct selection cannot silently change the explicit part');
  const noExtension=fixture();
  const standaloneContext=vm.createContext({document:noExtension.document,window:{},
    location:{search:'?q='+encodeURIComponent('https://www.bilibili.com/video/'+first.bvid+'?p=4')},
    fetch:()=>{throw Error('unexpected lookup');},URL,URLSearchParams,AbortController,console});
  vm.runInContext(moduleSource,standaloneContext);vm.runInContext(standalone,standaloneContext);await flush();
  assert.equal(new URL(standaloneContext.window.bilibiliDeck.frame.src).searchParams.get('p'),'4');
  assert.equal(standaloneContext.window.bilibiliDeck.resultId,null);
  assert.equal(standaloneContext.window.bilibiliDeck.verifiedResults.size,0);
  console.log('PASS: explicit BV and standalone multipart URLs play without a browser helper, lookup or fabricated title');

  const untrustedEvents=[];
  const keywordOnly=fixture({fetcher:async()=>response([{...first,direct:true,page:9,play_count:300000}]),
    narrate:async event=>{untrustedEvents.push(event);return true;}});
  await keywordOnly.deck.request('ordinary title');
  assert.deepEqual(untrustedEvents.map(event=>event.stage),['ack','found']);
  assert.equal(keywordOnly.deck.directTarget,null);
  assert.equal(typeof untrustedEvents.at(-1).result_id,'string');
  assert.equal(new URL(keywordOnly.deck.frame.src).searchParams.get('p'),'1');
  keywordOnly.deck.stop();
  assert.equal(await keywordOnly.deck.select({bvid:first.bvid,direct:true}),false);
  const olderDirect=deferred(), directEvents=[];
  const directSwitch=fixture({narrate:event=>{
    directEvents.push(event);
    return event.stage==='direct'&&event.query===first.bvid?olderDirect.promise:Promise.resolve(true);
  }});
  const oldDirectWork=directSwitch.deck.request(first.bvid);await flush();
  await directSwitch.deck.request('keyword versions');
  olderDirect.resolve(true);assert.equal(await oldDirectWork,false);await flush();
  assert.equal(directSwitch.deck.directTarget,null);
  assert.equal(directSwitch.deck.frame,null);
  assert.equal(directSwitch.deck.phase,'results');
  assert.equal(directEvents.at(-1).stage,'results');
  console.log('PASS: untrusted keyword rows cannot become direct requests and switching to search cancels late direct narration');

  const countRows=Array.from({length:15},(_,index)=>({bvid:'BV'+String(index).padStart(10,'0'),title:'真实版本 '+index,
    play_count:null,play_count_exact:false}));
  for(const scenario of ['all missing','partly missing','approximate']) {
    const events=[], introduction=deferred();
    const rows=countRows.map((item,index)=>scenario==='all missing'||(scenario==='partly missing'&&index>=12)?item:
      {...item,play_count:300000,play_text:'30万',play_count_exact:false,play_count_lower:295000,play_count_upper:305000});
    const countFixture=fixture({fetcher:async()=>response([], {data:{query:'real versions',result_id:'count-snapshot',results:rows,
      auto_bvid:null,selection_reason:scenario==='all missing'?'missing_play_count':'uncertain_play_count'}}),
      narrate:async event=>{events.push(event);return event.stage==='found'?introduction.promise:true;}});
    await countFixture.deck.request('real versions');
    const status=countFixture.get('status').textContent;
    assert.match(status,/已找到 15 个实际结果，当前展示前 12 个/);
    assert.match(status,/点击下方结果卡片的“播放这个版本”/);
    if(scenario==='all missing') {
      assert.match(status,/未读到任何结果的播放量，所以没自动播放/);
      assert.doesNotMatch(status,/部分结果|其中.*个缺少/);
      assert.match(status,/1\.0\.4.*重新加载扩展.*刷新页面并重连/);
    } else if(scenario==='partly missing') {
      assert.match(status,/12 个已读到播放量，3 个缺少播放量/,
        'coverage includes missing rows outside the visible twelve cards');
      assert.doesNotMatch(status,/未读到任何|1\.0\.3|页面播放量是近似值/);
    } else {
      assert.match(status,/页面播放量是近似值.*无法确认哪个版本最高/);
      assert.doesNotMatch(status,/缺少播放量|未读到任何/);
    }
    assert.equal(countFixture.get('results').children.length,12);
    assert.equal(countFixture.get('panel').hidden,false);
    const playButton=countFixture.get('results').children[0].children.find(node=>node.tagName==='BUTTON');
    assert.equal(playButton.textContent,'播放这个版本');assert.equal(playButton.disabled,false);
    assert.equal(countFixture.deck.frame,null);
    const playing=playButton.onclick();await flush();
    assert.equal(events.at(-1).stage,'found');assert.equal(events.at(-1).item.title,'真实版本 0');
    assert.equal(events.at(-1).result_id,'count-snapshot');assert.equal(events.at(-1).selection,undefined);
    assert.equal(countFixture.deck.frame,null,'missing or approximate counts do not bypass the selected-title introduction');
    introduction.resolve(true);assert.equal(await playing,true);
    assert.equal(new URL(countFixture.deck.frame.src).searchParams.get('bvid'),rows[0].bvid);
  }
  const standaloneCounts=fixture({fetcher:async()=>response(countRows)});
  const countContext=vm.createContext({document:standaloneCounts.document,window:{},location:{search:'?q=versions'},
    BilibiliBrowserSearch:class {constructor(){return standaloneCounts.provider;}},URL,URLSearchParams,AbortController,console});
  vm.runInContext(moduleSource,countContext);vm.runInContext(standalone,countContext);await flush();
  assert.match(standaloneCounts.get('status').textContent,/未读到任何结果的播放量/);
  const standalonePlay=standaloneCounts.get('results').children[0].children.find(node=>node.tagName==='BUTTON');
  assert(standalonePlay);await standalonePlay.onclick();
  assert(countContext.window.bilibiliDeck.frame,'the independent page exposes actual selectable cards despite absent statistics');
  console.log('PASS: full-snapshot play coverage and visible-card counts are accurate; all-missing, partial and approximate results retain real playable choices');

  for(const version of ['1.0.2','1.0.3','1.1.0']) {
    const statistics=fixture({fetcher:async()=>response(countRows)});
    statistics.provider.extensionVersion=version;
    await statistics.deck.request('missing statistics');
    const notice=statistics.get('status').textContent;
    if(version==='1.0.2') {
      assert.match(notice,/更新至 1\.0\.4.*重新加载扩展.*刷新页面并重连/);
      assert.doesNotMatch(notice,/助手已是/);
    } else {
      assert(notice.includes('助手已是 '+version+'，当前页面统计仍未读到'));
      assert.match(notice,/查看 B站原始搜索页.*封面旁的播放量/);
      assert.doesNotMatch(notice,/等待B站结果完整加载后重试/);
      assert.doesNotMatch(notice,/更新|重新加载扩展|刷新页面并重连/);
    }
    assert.match(notice,/点击下方结果卡片的“播放这个版本”/);
    assert.equal(statistics.deck.frame,null);
    assert.equal(statistics.get('results').children.length,12);
  }
  console.log('PASS: missing statistics suggest updates only for old/unknown helpers and direct current helpers to the actual search page');

  const statsDiagnostic={version:1,cards:[{card_index:0,stat_candidates:[],text:'<script>inert diagnostic</script>'}]};
  const searchQuery='海色 & 歌手 / live?';
  const introductionGate=deferred(), toolEvents=[];
  const searchTools=fixture({fetcher:async()=>response([], {data:{query:searchQuery,result_id:'diagnostic-result',
    results:[first,second],auto_bvid:null,selection_reason:'missing_play_count',stats_diagnostic:statsDiagnostic}}),
    narrate:async event=>{toolEvents.push(event);return event.stage==='found'?introductionGate.promise:true;}});
  searchTools.provider.extensionVersion='1.0.4';
  await searchTools.deck.request(searchQuery);
  const tools=searchTools.deck.resultTools, searchLink=searchTools.deck.searchPageLink;
  assert.equal(tools.hidden,false);
  const statusIndex=searchTools.get('panel').children.indexOf(searchTools.get('status'));
  assert.equal(searchTools.get('panel').children[statusIndex+1],tools,'recovery tools sit directly beside the status, before cards');
  assert.equal(searchLink.textContent,'查看 B站原始搜索页');
  const originalSearch=new URL(searchLink.href);
  assert.equal(originalSearch.origin,'https://search.bilibili.com');
  assert.equal(originalSearch.pathname,'/all');
  assert.equal(originalSearch.searchParams.get('keyword'),searchQuery);
  assert.equal(originalSearch.searchParams.get('order'),'click');
  assert.equal(searchLink.target,'_blank');assert.equal(searchLink.rel,'noopener noreferrer');
  assert.match(searchLink.parentNode.children[1].textContent,/新标签.*搜索原页.*封面旁.*播放量/);
  assert.equal(searchTools.deck.statisticsDetails.hidden,false);
  assert.equal(searchTools.deck.statisticsDetails.open,false);
  assert.equal(searchTools.deck.statisticsDetails.children[0].textContent,'播放量读取诊断');
  assert.match(searchTools.deck.statisticsDetails.children[1].textContent,/可复制.*不会改变选歌规则/);
  assert.equal(searchTools.deck.statisticsText.readOnly,true);
  assert.deepEqual(JSON.parse(searchTools.deck.statisticsText.value),statsDiagnostic);
  assert.match(searchTools.get('status').textContent,/展开“播放量读取诊断”/);
  assert.doesNotMatch(searchTools.get('status').textContent,/更新|等待.*重试/);
  searchTools.get('query').value='a different draft';searchTools.get('query').oninput();
  assert.equal(new URL(searchLink.href).searchParams.get('keyword'),searchQuery,'editing a draft cannot mislabel the already displayed results');
  const toolPlay=searchTools.get('results').children[0].children.find(node=>node.tagName==='BUTTON').onclick();await flush();
  assert.equal(toolEvents.at(-1).stage,'found');assert.equal(searchTools.deck.frame,null);
  assert.equal(tools.hidden,false);assert.equal(searchLink.href,originalSearch.href);
  introductionGate.resolve(true);assert.equal(await toolPlay,true);
  assert.equal(new URL(searchTools.deck.frame.src).hostname,'player.bilibili.com');
  assert.equal(new URL(searchLink.href).hostname,'search.bilibili.com','search recovery never points at the embedded player');
  searchTools.deck.stop();
  assert.equal(tools.hidden,true);assert.equal(searchLink.href,undefined);
  assert.equal(searchTools.deck.statisticsText.value,'');assert.equal(searchTools.deck.statisticsDetails.hidden,true);
  assert.equal(await searchTools.deck.request(first.bvid),true);
  assert.equal(tools.hidden,true);assert.equal(searchTools.deck.statisticsText.value,'');
  assert.equal(searchTools.requests.length,1,'direct BV continues to bypass the search helper');
  console.log('PASS: status-adjacent original-search link and copyable inert diagnostics preserve manual narration, cancellation and direct BV behavior');

  const circular={version:1,cards:[]};circular.cards.push(circular);
  for(const diagnostic of [undefined,null,[],{version:2,cards:[]},{version:1,cards:{}},
    {version:1,cards:['x'.repeat(6144)]},{version:1,cards:['汉'.repeat(2100)]},circular]) {
    const invalidDiagnostic=fixture({fetcher:async()=>response([], {data:{query:'diagnostic shape',result_id:'shape-result',
      results:[first],auto_bvid:null,selection_reason:'missing_play_count',stats_diagnostic:diagnostic}})});
    await invalidDiagnostic.deck.request('diagnostic shape');
    assert.equal(invalidDiagnostic.deck.resultTools.hidden,false);
    assert.equal(invalidDiagnostic.deck.statisticsDetails.hidden,true);
    assert.equal(invalidDiagnostic.deck.statisticsText.value,'');
    assert.doesNotMatch(invalidDiagnostic.get('status').textContent,/展开“播放量读取诊断”/);
  }
  for(const missingOutsideCards of [false,true]) {
    const diagnosticRows=countRows.map((item,index)=>({...item,play_count:missingOutsideCards&&index===14?null:300000,
      play_text:'约30万',play_count_lower:295000,play_count_upper:305000}));
    const coverage=fixture({fetcher:async()=>response([], {data:{query:'coverage',result_id:'coverage-result',
      results:diagnosticRows,auto_bvid:null,selection_reason:'uncertain_play_count',stats_diagnostic:statsDiagnostic}})});
    await coverage.deck.request('coverage');
    assert.equal(coverage.deck.statisticsDetails.hidden,!missingOutsideCards,
      'diagnostics only show for missing counts, including missing rows beyond the twelve visible cards');
  }
  console.log('PASS: diagnostic schema and 6 KiB UTF-8 limit reject malformed/oversized data; complete statistics keep diagnostics hidden');

  const lateSearch=deferred();
  const cancelledTools=fixture();cancelledTools.provider.search=()=>lateSearch.promise;
  const lateRequest=cancelledTools.deck.request('old search');await flush();
  await cancelledTools.deck.request(second.bvid);
  lateSearch.resolve({query:'old search',result_id:'late-result',results:[first],auto_bvid:null,
    selection_reason:'missing_play_count',stats_diagnostic:statsDiagnostic});
  assert.equal(await lateRequest,false);
  assert.equal(cancelledTools.deck.resultTools.hidden,true);
  assert.equal(cancelledTools.deck.statisticsText.value,'');
  assert.equal(new URL(cancelledTools.deck.frame.src).searchParams.get('bvid'),second.bvid);
  assert.equal(standaloneCounts.get('panel').children.includes(countContext.window.bilibiliDeck.resultTools),true);
  assert.equal(countContext.window.bilibiliDeck.resultTools.hidden,false);
  assert.equal(new URL(countContext.window.bilibiliDeck.searchPageLink.href).searchParams.get('keyword'),'versions');
  assert.equal(new URL(countContext.window.bilibiliDeck.searchPageLink.href).searchParams.get('order'),'click');
  console.log('PASS: late searches cannot restore tools after direct replacement, and standalone results expose the same original-search recovery link');

  const evidence = (bvid=first.bvid,part=1,extra={}) => ({evidence_id:'evidence-'+bvid+'-'+part,
    bvid,part,expires_at:Date.now()/1000+600,evidence_kind:'full_subtitles',subtitle_status:'ready',
    subtitle_truncated:false,subtitle_language:'中文',...extra});
  const subtitleGate=deferred(), reviewGate=deferred(), reviewEvents=[], subtitleCalls=[];
  const reviewing=fixture({reviewAvailable:()=>true,readVideo:(query,{signal})=>{
    subtitleCalls.push({query,signal});return subtitleGate.promise;
  },narrate:event=>{reviewEvents.push(event);return event.stage==='review'?reviewGate.promise:true;}});
  await reviewing.deck.request(BilibiliDeck.videoURL(first.bvid,3));await flush();
  const reviewFrame=reviewing.deck.frame;
  assert.equal(subtitleCalls.length,1);assert.equal(subtitleCalls[0].query,BilibiliDeck.videoURL(first.bvid,3));
  assert.equal(reviewing.deck.reviewState,'reading');assert.equal(reviewing.deck.reviewSpeak.disabled,true);
  assert.equal(reviewEvents.some(event=>event.stage==='review'),false,'subtitle preload does not spend a model call or speak');
  subtitleGate.resolve(evidence(first.bvid,3,{evidence_kind:'subtitle_excerpt',subtitle_truncated:true}));await flush();
  assert.equal(reviewing.deck.reviewState,'ready');assert.equal(reviewing.deck.frame,reviewFrame);
  assert.match(reviewing.deck.reviewStatus.textContent,/P3.*部分字幕/);
  const reading=reviewing.deck.reviewSpeak.onclick();await flush();
  assert.equal(reviewFrame.parentNode,null,'stop iframe sound before requesting evaluation');
  assert.equal(reviewing.deck.frame,null);assert.equal(reviewing.deck.active,true);
  assert.equal(reviewing.deck.phase,'opened','hold media focus through narration and afterwards');
  const review=reviewEvents.at(-1);
  assert.equal(review.stage,'review');assert.equal(review.query,BilibiliDeck.videoURL(first.bvid,3));
  assert.equal(review.evidence_id,'evidence-'+first.bvid+'-3');assert.equal(review.result_id,undefined);
  assert.equal(await reviewing.deck.reviewSpeak.onclick(),false,'double clicks cannot repeat evaluation');
  assert.equal(reviewEvents.filter(event=>event.stage==='review').length,1);
  reviewGate.resolve(true);assert.equal(await reading,true);
  assert.equal(reviewing.deck.reviewState,'completed');assert.equal(reviewing.deck.reviewSpeak.disabled,true);
  assert.match(reviewing.deck.reviewStatus.textContent,/评价已播完/);assert.equal(reviewing.deck.frame,null);
  assert.equal(reviewing.deck.active,true);reviewing.deck.stop();assert.equal(reviewing.deck.active,false);
  assert.equal(subtitleCalls[0].signal.aborted,true,'stopping revokes successful subtitle capability too');
  console.log('PASS: multipart subtitles preload silently; explicit review stops embed audio, binds evidence and waits for actual narration completion');

  for (const value of [evidence(first.bvid,1,{evidence_kind:'page_metadata',subtitle_status:'none',subtitle_reason:'没有字幕'}),
      evidence(second.bvid),evidence(first.bvid,2),{...evidence(),evidence_id:''}]) {
    const events=[];
    const invalid=fixture({reviewAvailable:()=>true,readVideo:async()=>value,narrate:async event=>{events.push(event);return true;}});
    await invalid.deck.request(first.bvid);await flush();
    assert.equal(invalid.deck.reviewSpeak.disabled,true);
    assert.equal(await invalid.deck.review(),false);
    assert.equal(events.some(event=>event.stage==='review'),false);
    assert(invalid.deck.frame,'missing/wrong subtitles never stop the current video');
    invalid.deck.stop();
  }
  const staleEvidence=fixture({reviewAvailable:()=>true,readVideo:async()=>evidence(first.bvid,1,{expires_at:1}),narrate:async()=>true});
  await staleEvidence.deck.request(first.bvid);await flush();
  assert.equal(await staleEvidence.deck.review(),false);
  assert.equal(staleEvidence.deck.reviewState,'expired');assert.match(staleEvidence.deck.reviewStatus.textContent,/重新读取/);
  assert(staleEvidence.deck.frame);staleEvidence.deck.stop();
  console.log('PASS: missing, mismatched and expired subtitles cannot trigger unsupported evaluation or stop media');

  const oldSubtitles=deferred(), currentSubtitles=deferred();let readCount=0,oldReadSignal;
  const lateReview=fixture({reviewAvailable:()=>true,readVideo:(query,{signal})=>{
    if(++readCount===1){oldReadSignal=signal;return oldSubtitles.promise;}return currentSubtitles.promise;
  },narrate:async()=>true});
  await lateReview.deck.request(first.bvid);await flush();
  await lateReview.deck.request(second.bvid);await flush();
  assert.equal(oldReadSignal.aborted,true);
  currentSubtitles.resolve(evidence(second.bvid));await flush();
  const reviewText=lateReview.deck.reviewStatus.textContent;
  oldSubtitles.resolve(evidence(first.bvid));await flush();
  assert.equal(lateReview.deck.reviewEvidence.bvid,second.bvid);assert.equal(lateReview.deck.reviewStatus.textContent,reviewText);
  assert.equal(new URL(lateReview.deck.frame.src).searchParams.get('bvid'),second.bvid);
  const blockedReview=deferred();let reviewSignal;
  lateReview.deck.narrate=event=>{reviewSignal=event.signal;return blockedReview.promise;};
  const speaking=lateReview.deck.review();await flush();lateReview.deck.stop();
  assert.equal(await speaking,false);assert.equal(reviewSignal.aborted,true);
  const stoppedReviewText=lateReview.deck.reviewStatus.textContent;
  blockedReview.resolve(true);await flush();
  assert.equal(lateReview.deck.reviewStatus.textContent,stoppedReviewText);assert.equal(lateReview.deck.reviewArea.hidden,true);
  console.log('PASS: switching cancels subtitle ownership; stopping evaluation rejects late speech completion and never remounts video');

  let failRead=true, failReview=true;const retryEvents=[];
  const retryReview=fixture({reviewAvailable:()=>true,readVideo:async()=>{
    if(failRead)throw Object.assign(new Error('请更新助手至 1.3.0。'),{code:'extension_upgrade_required'});return evidence();
  },narrate:async event=>{retryEvents.push(event);return event.stage!=='review'||!failReview;}});
  await retryReview.deck.request(first.bvid);await flush();
  assert.equal(retryReview.deck.reviewState,'failed');assert.match(retryReview.deck.reviewStatus.textContent,/1\.3\.0/);
  assert.equal(retryReview.deck.reviewSpeak.disabled,true);assert.equal(retryReview.deck.reviewRead.disabled,false);
  failRead=false;await retryReview.deck.reviewRead.onclick();assert.equal(retryReview.deck.reviewState,'ready');
  assert.equal(await retryReview.deck.review(),false);assert.equal(retryReview.deck.reviewState,'ready');
  failReview=false;assert.equal(await retryReview.deck.review(),true);
  assert.equal(retryEvents.filter(event=>event.stage==='review').length,2);
  assert.equal(new Set(retryEvents.filter(event=>event.stage==='review').map(event=>event.evidence_id)).size,1);
  retryReview.deck.stop();
  let unexpectedReads=0;
  const oldReviewService=fixture({reviewAvailable:()=>false,readVideo:async()=>{unexpectedReads++;return evidence();},narrate:async()=>true});
  await oldReviewService.deck.request(first.bvid);await flush();assert.equal(unexpectedReads,0);
  assert.equal(oldReviewService.deck.reviewSpeak.disabled,true);assert.match(oldReviewService.deck.reviewStatus.textContent,/主服务尚未启用/);
  const standaloneReview=fixture({reviewAvailable:()=>true,readVideo:async()=>{unexpectedReads++;return evidence();}});
  await standaloneReview.deck.request(first.bvid);await flush();assert.equal(standaloneReview.deck.reviewArea.hidden,true);
  assert.equal(unexpectedReads,0);
  console.log('PASS: read and narration failures permit explicit retry; old services and standalone players never auto-call review');

}

const watchdog = setTimeout(() => { console.error('FAIL: an async player test never settled'); process.exit(1); }, 5000);
main().catch(error => { console.error(error); process.exitCode = 1; }).finally(() => clearTimeout(watchdog));
