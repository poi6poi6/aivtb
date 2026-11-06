const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const assert = require('node:assert/strict');
const source = fs.readFileSync(path.join(__dirname, '../brain/static/songs.js'), 'utf8');
const song = (title, id) => ({title, url:`/songs/${String(id).padStart(24, '0')}`});
const a = song('第一首', 1), b = song('第二首', 2);
const response = songs => ({ok:true, json:async () => songs});
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
}
async function flush() { for (let i = 0; i < 60; i++) await Promise.resolve(); }
function fixture({songs = [], fetcher = async () => response(songs), onBilibili, onStop,
  beforePlay, beforeRequest, narrate, onCancel, onChange, narrationTimeoutMs} = {}) {
  const elements = new Map(), requests = [], events = {}, revoked = [];
  let interrupts = 0, blobs = 0;
  function element() {
    return {value:'', hidden:false, children:[], textContent:'', playCount:0, pauseCount:0, loadCount:0,
      append(...nodes) { this.children.push(...nodes); },
      replaceChildren(...nodes) { this.children = [...nodes]; },
      pause() { this.pauseCount++; }, load() { this.loadCount++; },
      removeAttribute(name) { delete this[name]; },
      play() { this.playCount++; return Promise.resolve(); }};
  }
  const get = id => { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); };
  class LocalURL extends URL {}
  LocalURL.createObjectURL = () => `blob:test/${++blobs}`;
  LocalURL.revokeObjectURL = value => revoked.push(value);
  const context = vm.createContext({
    document:{getElementById:get, createElement:element},
    window:{addEventListener(name, callback) { events[name] = callback; }}, URL:LocalURL,
    AbortController, setTimeout, clearTimeout, console,
    fetch: (...args) => { requests.push(args); return fetcher(...args); },
  });
  vm.runInContext(source + '\nglobalThis.SongDeck = SongDeck;', context);
  const deck = new context.SongDeck({beforePlay(item) { interrupts++; return beforePlay?.(item); }, onBilibili, onStop,
    beforeRequest, narrate, onCancel, onChange, narrationTimeoutMs});
  return {deck, get, context, requests, events, revoked, get interrupts() { return interrupts; }};
}

async function main() {
  const ready = fixture({songs:[a,b]});
  await ready.deck.load();
  const parse = value => ready.context.SongDeck.parseCommand(value);
  const commandCases = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures/song_command_cases.json'), 'utf8'));
  assert.equal(commandCases.version, 1);
  for (const {text, expected} of commandCases.cases) {
    const actual = parse(text);
    assert.deepEqual(actual === null ? null : JSON.parse(JSON.stringify(actual)), expected,
      `the browser command parser follows the shared Python/JS contract: ${JSON.stringify(text)}`);
  }
  console.log(`PASS: ${commandCases.cases.length} shared natural-command cases agree with the backend parser contract`);
  for (const text of ['点歌: 第一首', '点歌：第一首', '点歌 第一首', '点歌第一首',
    '播放《第一首》', '放一首「第一首」', '来一首 第一首', '请点歌 第一首', '帮我播放第一首', '给我放首第一首',
    '请帮我点歌：第一首！', '请给我播放《第一首》。', '你好，点歌 第一首',
    '助手，我想听《第一首》', '能不能播放《第一首》？']) {
    ready.deck.stop();
    assert.equal(ready.deck.command(text), true, `${text} is consumed synchronously`);
    assert.equal(ready.deck.current?.url, a.url, `${text} resolves the existing song`);
  }
  for (const text of ['今天好吗', '我想聊聊点歌功能', '你播放的歌很好听', 'B站今天怎么样',
    '看看 BV1xx411c7mD 的评论', 'https://bilibili.com.evil.test/video/BV1xx411c7mD']) {
    assert.equal(ready.deck.command(text), false, 'ordinary chat is not a broad substring song match');
  }
  assert.equal(parse('点歌：').mode, 'open');
  assert.equal(ready.deck.command('点歌 《》'), true);
  assert.match(ready.deck.status.textContent, /歌名/);
  console.log('PASS: anchored commands, colon/fullwidth/space/no-space forms, song quotes and ordinary chat');

  const namedTitles = fixture({songs:[song('走',81),song('走吧',82),song('你好!',83),
    song('不要说话',84),song('如果可以',85),song('这是什么意思',86)]});
  await namedTitles.deck.load();
  for (const [command,title] of [['播放走吧','走吧'], ['播放《走吧》吧','走吧'],
    ['你好，播放《你好!》，谢谢','你好!'], ['我想听《不要说话》','不要说话'],
    ['能否播放《如果可以》吗','如果可以'], ['播放《这是什么意思》','这是什么意思']]) {
    namedTitles.deck.stop();
    assert.equal(namedTitles.deck.command(command), true);
    assert.equal(namedTitles.deck.current?.title, title,
      'courtesy handling and request guards must not rename an actual song or mistake quoted title words for intent');
  }
  console.log('PASS: punctuation, final 吧 and negative/conditional/discussion words inside actual titles remain playable');

  // The screenshot title was a browser-local file, which the server catalogue
  // cannot see. Its punctuation/case/width must work identically to server songs.
  const local = fixture(); await local.deck.load();
  local.get('song-files').onchange({target:{files:[
    {name:'白fi - 尼古喵喵 OP「なんもねぇ」.mp3', type:'audio/mpeg'},
    {name:'ignore.txt', type:'text/plain'},
  ], value:'fake-path'}});
  assert.equal(local.deck.local.length, 1);
  const localSong = local.deck.local[0];
  for (const text of ['点歌: 白fi - 尼古喵喵 OP「なんもねぇ」',
    '点歌：白ＦＩ—尼古喵喵　ｏｐ《なんもねぇ》', '播放《なんもねぇ》']) {
    local.deck.stop(); local.deck.command(text);
    assert.equal(local.deck.current, localSong, 'the selected local blob matches the normalized screenshot title');
  }
  assert.equal(local.requests.length, 1, 'local matching does not upload files or query a model');
  assert.equal(local.requests[0][0], '/songs');
  local.deck.stop(); local.deck.search.value = 'ＦＩ 尼古喵喵'; local.deck.render();
  assert.equal(local.get('song-list').children.length, 1);
  await local.get('song-list').children[0].children[1].onclick();
  assert.equal(local.deck.current, localSong, 'the original row play button still works for local blobs');
  local.events.beforeunload(); assert.deepEqual(local.revoked, [localSong.url]);
  console.log('PASS: browser-local screenshot title, NFKC/case/punctuation search, local buttons and no upload');

  const matching = fixture({songs:[a, song('歌手 - 第一首 live', 3), song('第一首 acoustic', 4)]});
  await matching.deck.load(); matching.deck.command('点歌 第一首');
  assert.equal(matching.deck.current.url, a.url, 'exact matches take priority over partial versions');
  matching.deck.stop(); matching.deck.command('点歌 歌手');
  assert.equal(matching.deck.current.url, song('', 3).url, 'a unique partial match is playable');
  matching.deck.stop(); matching.deck.command('点歌 一首');
  assert.equal(matching.deck.current, null);
  assert.match(matching.deck.status.textContent, /多首/);
  matching.deck.songs.push(song('第 一 首！', 5)); matching.deck.command('点歌《第一首》');
  assert.equal(matching.deck.current, null, 'different recordings with the same normalized title remain ambiguous');
  matching.deck.songs = [a,a]; matching.deck.command('点歌第一首');
  assert.equal(matching.deck.current.url, a.url, 'repeated catalogue rows for the same URL are not separate recordings');
  console.log('PASS: exact-first matching, unique partial matching and ambiguity without arbitrary selection');

  const loading = deferred();
  const delayed = fixture({fetcher:() => loading.promise});
  assert.equal(delayed.deck.command('点歌 第一首'), true);
  assert.equal(delayed.deck.current, null);
  assert.equal(delayed.deck.active, true);
  assert.equal(delayed.get('song-stop').disabled, false, 'pending library commands are stoppable before any song exists');
  assert.match(delayed.deck.status.textContent, /加载/);
  assert.equal(delayed.requests.length, 1, 'constructor and command share one in-flight library load');
  loading.resolve(response([a])); await flush();
  assert.equal(delayed.deck.current.url, a.url);
  assert.equal(delayed.deck.audio.playCount, 1);
  assert.match(delayed.deck.status.textContent, /正在播放/);
  console.log('PASS: deferred library commands wait instead of reporting an empty catalogue');

  for (const mode of ['stop', 'new chat', 'new command', 'button']) {
    const load = deferred(), stale = fixture({fetcher:() => load.promise});
    stale.deck.command('点歌第一首');
    if (mode === 'stop') stale.deck.command('停止播放');
    if (mode === 'new chat') stale.deck.stop();
    if (mode === 'new command') stale.deck.command('点歌第二首');
    if (mode === 'button') await stale.deck.play(b);
    load.resolve(response([a,b])); await flush();
    assert.equal(stale.deck.current?.url ?? null, ['new command','button'].includes(mode) ? b.url : null);
    assert.equal(stale.deck.audio.playCount, ['new command','button'].includes(mode) ? 1 : 0,
      `${mode} cannot be undone by a stale catalogue completion`);
  }
  const failure = deferred(), stoppedFailure = fixture({fetcher:() => failure.promise});
  stoppedFailure.deck.command('点歌第一首'); stoppedFailure.deck.stop();
  failure.reject(new Error('offline')); await flush();
  assert.match(stoppedFailure.deck.status.textContent, /已停止/, 'late load failure cannot overwrite a stop notice');
  console.log('PASS: stop/new-chat/new-command/manual selection cancel obsolete loading without autoplay');

  const oldLoad = deferred(), refreshedLoad = deferred(); let fetchNumber = 0;
  const refreshed = fixture({fetcher:() => ++fetchNumber === 1 ? oldLoad.promise : refreshedLoad.promise});
  refreshed.deck.command('点歌第二首'); refreshed.get('song-refresh').onclick();
  oldLoad.resolve(response([a])); await flush();
  assert.equal(refreshed.deck.current, null);
  refreshedLoad.resolve(response([b])); await flush();
  assert.equal(refreshed.deck.current.url, b.url, 'pending commands follow a newer refresh instead of an obsolete catalogue');
  const followingLoad = deferred();
  const followsOldTrack = fixture({songs:[a,b]}); await followsOldTrack.deck.load();
  await followsOldTrack.deck.play(a);
  followsOldTrack.context.fetch = () => followingLoad.promise;
  followsOldTrack.get('song-refresh').onclick(); followsOldTrack.deck.command('点歌第二首');
  followsOldTrack.deck.audio.onended();
  assert.equal(followsOldTrack.deck.active, true, 'the older song ending cannot cancel a newer loading command');
  followingLoad.resolve(response([a,b])); await flush();
  assert.equal(followsOldTrack.deck.current.url, b.url);
  let fail = true;
  const retry = fixture({fetcher:async () => fail ? {ok:false} : response([a])});
  await retry.deck.load(); assert.equal(retry.deck.loadState, 'error');
  retry.deck.command('点歌第一首'); await flush();
  assert.match(retry.deck.status.textContent, /加载失败/);
  assert.equal(retry.deck.active, false);
  fail = false; await retry.get('song-refresh').onclick(); retry.deck.command('点歌第一首');
  assert.equal(retry.deck.current.url, a.url, 'refresh after failure restores matching');
  const offlineLocal = fixture({fetcher:async () => ({ok:false})}); await offlineLocal.deck.load();
  offlineLocal.deck.local.push({title:'离线歌', url:'blob:test/offline'});
  offlineLocal.deck.command('点歌离线歌'); await flush();
  assert.equal(offlineLocal.deck.current.url, 'blob:test/offline', 'server errors do not hide selected local songs');
  console.log('PASS: refreshed-load ownership, recovery after failure and offline local-file playback');

  const queue = fixture({songs:[a,b]}); await queue.deck.load();
  await queue.deck.play(a); queue.deck.enqueue(b); queue.deck.audio.onended();
  assert.equal(queue.deck.current.url, b.url); queue.deck.audio.onended();
  assert.equal(queue.deck.active, false); assert.match(queue.deck.status.textContent, /播放结束/);
  queue.deck.request(a);
  for (let i = 2; i <= 14; i++) queue.deck.request(song(`第${i}首`, i));
  assert.equal(queue.deck.queue.length, 10, 'all request paths preserve the ten-song bound');
  assert.equal(queue.deck.request({title:'untrusted',url:'https://example.test/music.mp3'}), false);
  const plays = queue.deck.audio.playCount;
  assert.equal(await queue.deck.play({title:'invalid',url:'javascript:alert(1)'}), false);
  assert.equal(await queue.deck.play({title:'unknown local',url:'blob:unknown'}), false);
  assert.equal(queue.deck.audio.playCount, plays);
  queue.deck.songs = [a,b]; queue.deck.render(); queue.get('song-list').children[1].children[2].onclick();
  assert.equal(queue.deck.queue.length, 10, 'the UI queue button cannot bypass the same cap');
  console.log('PASS: completion/next, bounded and deduplicated queue, safe URLs and queue-button parity');

  const denied = fixture({songs:[a]}); await denied.deck.load();
  denied.deck.audio.play = async () => { const error = new Error('gesture required'); error.name = 'NotAllowedError'; throw error; };
  await denied.deck.play(a);
  assert.match(denied.deck.status.textContent, /浏览器阻止.*播放按钮/);
  assert.equal(denied.deck.current.url, a.url, 'the blocked track remains loaded for a user gesture');
  const blocked = deferred(); denied.deck.audio.play = () => blocked.promise;
  const pendingPlay = denied.deck.play(a); denied.deck.stop(); blocked.reject(new Error('aborted')); await pendingPlay;
  assert.equal(denied.deck.active, false); assert.match(denied.deck.status.textContent, /已停止/);
  const lateResolve = deferred(); denied.deck.audio.play = () => lateResolve.promise;
  const oldPlay = denied.deck.play(a); denied.deck.stop(); lateResolve.resolve(); await oldPlay;
  assert.match(denied.deck.status.textContent, /已停止/, 'a late play resolution also cannot restore a playing notice');
  console.log('PASS: accurate user-gesture hint, recoverable loaded audio and stale play resolution/rejection guards');

  const routed = []; let stopCallbacks = 0, routedDeck;
  const remote = fixture({songs:[a], onBilibili(query) {
    routed.push(query); routedDeck.stop(); return Promise.resolve();
  }, onStop() { stopCallbacks++; }});
  routedDeck = remote.deck; await remote.deck.load();
  const bv = 'BV1xx411c7mD', official = `https://www.bilibili.com/video/${bv}/?p=2`;
  for (const [command, expected] of [[bv,bv], [official,official], [`点歌：${bv}`,bv],
    [`播放 ${official}`,official], ['B站搜索 白fi なんもねぇ','白fi なんもねぇ'],
    ['在B站搜《なんもねぇ》','なんもねぇ'], ['点歌B站白fi','白fi'], ['帮我B站播放白fi','白fi'],
    ['请帮我去B站上搜一下《なんもねぇ》然后播放！','なんもねぇ'],
    ['请给我哔哩哔哩点开白fi并播放','白fi'], ['点歌：在B站上搜索白fi','白fi'],
    ['B站搜索',''], [`www.bilibili.com/video/${bv}`,`www.bilibili.com/video/${bv}`],
    ['B站点歌 放个飞我去星吧','飞我去星'], ['B站点歌 给我放个飞我去星吧','飞我去星'],
    ['B站点歌 放个走吧','走吧'], ['B站点歌 放个《飞我去星吧》','飞我去星吧']]) {
    await remote.deck.play(a);
    assert.equal(remote.deck.command(command), true);
    assert.equal(routed.at(-1), expected);
    assert.equal(remote.deck.panel.hidden,true,'Bilibili commands do not open the local-library panel');
    assert.equal(remote.deck.audio.hidden,true,'the emptied local player is hidden during remote playback');
    assert.doesNotMatch(remote.deck.status.textContent,/正在交给/);
    assert.equal(remote.deck.current, null);
    assert.equal(remote.deck.pending, null, 'Bilibili owns its lifecycle even if its callback stops SongDeck again');
  }
  for (const value of ['BV123', 'https://evil.test/video/BV1xx411c7mD',
    'https://www.bilibili.com.evil.test/video/BV1xx411c7mD', 'https://user@www.bilibili.com/video/BV1xx411c7mD',
    'https://www.bilibili.com:444/video/BV1xx411c7mD'])
    assert.equal(parse(value), null, 'only complete identifiers and official credential-free video URLs are direct media commands');
  remote.deck.stop(); remote.deck.next(); assert.equal(stopCallbacks, 0);
  remote.deck.command('请停止播放！'); assert.equal(stopCallbacks, 1, 'only an explicit stop command invokes onStop');
  assert.equal(remote.requests.length, 1, 'routing never fetches Bilibili or sends a chat/model request');
  const localLoad = deferred(); let handoffs = 0;
  const handoff = fixture({fetcher:() => localLoad.promise, onBilibili() { handoffs++; }});
  handoff.deck.command('点歌第一首'); handoff.deck.command(bv);
  localLoad.resolve(response([a])); await flush();
  assert.equal(handoffs, 1); assert.equal(handoff.deck.audio.playCount, 0);
  assert.equal(handoff.deck.active, false, 'Bilibili handoff invalidates the previous pending local autoplay');
  console.log('PASS: BV/official-link/explicit-search routing, callback stop safety and explicit cross-player stop');

  const declined=fixture({songs:[a],onBilibili:async()=>false});await declined.deck.load();
  declined.deck.command(bv);await flush();
  assert.equal(declined.deck.panel.hidden,true);assert.equal(declined.deck.audio.hidden,true);
  assert.match(declined.deck.status.textContent,/未完成.*B站面板/);
  for(const later of ['local','stop','bilibili'])for(const result of ['false','reject']){
    const old=deferred();let calls=0;
    const ownership=fixture({songs:[a],onBilibili:()=>++calls===1?old.promise:Promise.resolve(false)});
    await ownership.deck.load();ownership.deck.command(bv);
    if(later==='local')ownership.deck.command('点歌第一首');
    else if(later==='stop')ownership.deck.command('停止播放');
    else ownership.deck.command('B站点歌 新歌');
    await flush();const notice=ownership.deck.status.textContent, hidden=ownership.deck.panel.hidden;
    if(result==='false')old.resolve(false);else old.reject(Error('late failure'));
    await flush();assert.equal(ownership.deck.status.textContent,notice);assert.equal(ownership.deck.panel.hidden,hidden);
    if(later==='local')assert.equal(ownership.deck.audio.hidden,false,'a real local track restores native controls');
    else assert.equal(ownership.deck.audio.hidden,true);
  }
  console.log('PASS: resolved remote failures never leave local handoff loading text or empty controls; late outcomes respect new commands and stop');

  const ack = deferred(), found = deferred(), chronology = [];
  const spoken = fixture({songs:[a,b], beforeRequest:() => chronology.push('prepare'), beforePlay:() => chronology.push('beforePlay'),
    narrate:event => { chronology.push(event.stage); return event.stage === 'ack' ? ack.promise : found.promise; }});
  await spoken.deck.load();
  const originalMatch = spoken.deck.matchingSongs.bind(spoken.deck);
  spoken.deck.matchingSongs = query => { chronology.push('match'); return originalMatch(query); };
  assert.equal(spoken.deck.command('点歌第一首'), true);
  await flush();
  assert.deepEqual(chronology, ['prepare','ack']);
  assert.equal(spoken.deck.phase, 'ack'); assert.equal(spoken.deck.active, true);
  assert.equal(spoken.deck.current, null); assert.equal(spoken.deck.audio.src, undefined);
  ack.resolve(true); await flush();
  assert.deepEqual(chronology, ['prepare','ack','match','beforePlay','found']);
  assert.equal(spoken.deck.audio.src, undefined, 'native controls cannot bypass unfinished introduction');
  assert.equal(spoken.deck.audio.playCount, 0);
  found.resolve(true); assert.equal(await spoken.deck.pendingWork, true);
  assert.equal(spoken.deck.current.url, a.url); assert.equal(spoken.deck.audio.playCount, 1);
  console.log('PASS: typed local requests acknowledge before matching and wait for finished introduction before assigning audio src');

  for (const route of ['play', 'button', 'request', 'enqueue']) {
    const gate = deferred(), events = [];
    const selected = fixture({songs:[a], narrate:event => { events.push(event); return gate.promise; }});
    await selected.deck.load();
    if (route === 'play') selected.deck.play(a);
    if (route === 'button') selected.get('song-list').children[0].children[1].onclick();
    if (route === 'request') selected.deck.request(a);
    if (route === 'enqueue') selected.deck.enqueue(a);
    await flush();
    assert.deepEqual(events.map(event => event.stage), ['found'], route + ' introduces a concrete selection without pretending to search');
    assert.equal(events[0].item.url, a.url);
    assert.equal(selected.deck.audio.src, undefined); assert.equal(selected.deck.current, null);
    gate.resolve(true); await selected.deck.pendingWork;
    assert.equal(selected.deck.audio.playCount, 1);
  }
  const queuedEvents = [], nextGate = deferred();
  const voicedQueue = fixture({songs:[a,b], narrate:event => {
    queuedEvents.push(event); return event.item?.url === b.url ? nextGate.promise : Promise.resolve(true);
  }});
  await voicedQueue.deck.load(); await voicedQueue.deck.play(a);
  const firstPauses = voicedQueue.deck.audio.pauseCount;
  voicedQueue.deck.enqueue(b); assert.equal(voicedQueue.deck.enqueue(b), false);
  assert.equal(voicedQueue.deck.audio.pauseCount, firstPauses, 'adding to a playing queue never interrupts the current track');
  assert.equal(queuedEvents.length, 1);
  voicedQueue.deck.audio.onended(); await flush();
  assert.equal(voicedQueue.deck.audio.src, undefined); assert.equal(voicedQueue.deck.current, null);
  assert.deepEqual(queuedEvents.map(event => event.stage), ['found','found']);
  voicedQueue.deck.audio.onended(); assert.equal(voicedQueue.deck.active, true, 'duplicate old ended cannot cancel the pending next-track introduction');
  nextGate.resolve(true); await voicedQueue.deck.pendingWork;
  assert.equal(voicedQueue.deck.current.url, b.url);
  voicedQueue.deck.audio.onended(); assert.equal(voicedQueue.deck.active, false);
  console.log('PASS: buttons/server requests/empty enqueue and natural next all gate found; merely adding to a live queue is silent');

  const metadata = [];
  const narratedBlob = fixture({narrate:async event => { metadata.push(event); return true; }});
  await narratedBlob.deck.load();
  narratedBlob.get('song-files').onchange({target:{files:[{name:'浏览器独有歌曲.mp3',type:'audio/mpeg'}],value:'path'}});
  narratedBlob.deck.command('点歌浏览器独有歌曲'); await narratedBlob.deck.pendingWork;
  assert.equal(metadata.at(-1).item.url, narratedBlob.deck.local[0].url);
  assert.equal(narratedBlob.deck.audio.src, narratedBlob.deck.local[0].url);
  assert.equal(narratedBlob.requests.length, 1); assert.equal(narratedBlob.requests[0][0], '/songs');
  const ambiguousEvents = [];
  const ambiguous = fixture({songs:[a,song('第一首现场',3)], narrate:async event => { ambiguousEvents.push(event); return true; }});
  await ambiguous.deck.load(); ambiguous.deck.command('点歌第一'); await ambiguous.deck.pendingWork;
  assert.deepEqual(ambiguousEvents.map(event => event.stage), ['ack','results']);
  assert.equal(ambiguous.deck.phase, 'results'); assert.equal(ambiguous.deck.active, true);
  assert.equal(ambiguous.deck.audio.src, undefined); assert.equal(ambiguous.deck.current, null);
  await ambiguous.get('song-list').children[1].children[1].onclick();
  assert.deepEqual(ambiguousEvents.map(event => event.stage), ['ack','results','found']);
  for (const type of ['empty','library']) {
    const events = [];
    const missing = fixture({fetcher:async () => type === 'empty' ? response([]) : {ok:false}, narrate:async event => { events.push(event); return true; }});
    await missing.deck.load(); missing.deck.command('点歌没有这首歌'); await missing.deck.pendingWork;
    assert.deepEqual(events.map(event => event.stage), ['ack','error']); assert.equal(events[1].error, type);
    assert.equal(missing.deck.audio.playCount, 0); assert.equal(missing.deck.active, false);
  }
  console.log('PASS: local blobs stay local, ambiguous results ask for selection, and missing/failed catalogue narration cannot invent a song');

  const failedAck = fixture({songs:[a], narrate:async () => false});
  await failedAck.deck.load(); failedAck.deck.command('点歌第一首'); await failedAck.deck.pendingWork;
  assert.equal(failedAck.deck.phase, 'awaiting-continue');
  assert.equal(failedAck.deck.continueButton.textContent, '跳过回应，继续查找');
  assert.equal(failedAck.deck.audio.src, undefined);
  await failedAck.deck.continueButton.onclick();
  assert.equal(failedAck.deck.continueButton.textContent, '语音没播完，点击继续播放');
  assert.equal(failedAck.deck.audio.playCount, 0);
  assert.equal(await failedAck.deck.continueButton.onclick(), true);
  assert.equal(failedAck.deck.audio.playCount, 1);
  for (const mode of ['false','reject','timeout']) {
    let signal;
    const blocked = fixture({songs:[a], narrationTimeoutMs:10, narrate:event => {
      signal = event.signal;
      if (mode === 'false') return Promise.resolve(false);
      if (mode === 'reject') return Promise.reject(new Error('voice error'));
      return new Promise(() => {});
    }});
    await blocked.deck.load(); assert.equal(await blocked.deck.play(a), false);
    assert.equal(signal.aborted, true); assert.equal(blocked.deck.audio.src, undefined);
    assert.equal(blocked.deck.continueButton.textContent, '语音没播完，点击继续播放');
    assert.equal(await blocked.deck.continueButton.onclick(), true);
    assert.equal(blocked.deck.audio.playCount, 1);
  }
  console.log('PASS: incomplete/failed/timed-out acknowledgement and introduction require explicit continuation without exposing native src');

  for (const stage of ['ack','found']) {
    const voice = deferred(); let signal, cancelled = 0;
    const stale = fixture({songs:[a,b], onCancel:() => cancelled++, narrate:event => {
      if (event.stage === stage) { signal = event.signal; return voice.promise; }
      return Promise.resolve(true);
    }});
    await stale.deck.load(); stale.deck.command('点歌第一首'); await flush();
    const work = stale.deck.pendingWork;
    stale.deck.stop(); assert.equal(await work, false);
    assert.equal(cancelled, 1); assert.equal(signal.aborted, true);
    voice.resolve(true); await flush();
    assert.equal(stale.deck.audio.playCount, 0); assert.equal(stale.deck.audio.src, undefined);
    assert.match(stale.deck.status.textContent, /已停止/);
    stale.deck.stop(); assert.equal(cancelled, 1, 'stopping an idle deck must not cancel another player narration');
  }
  const oldIntroduction = deferred(), latestIntroduction = deferred(); let oldSignal;
  const switched = fixture({songs:[a,b], narrate:event => {
    if (event.item?.url === a.url) { oldSignal = event.signal; return oldIntroduction.promise; }
    return latestIntroduction.promise;
  }});
  await switched.deck.load(); const oldSelection = switched.deck.play(a); await flush();
  const newestSelection = switched.deck.play(b); await flush();
  assert.equal(oldSignal.aborted, true); oldIntroduction.resolve(true); await oldSelection;
  assert.equal(switched.deck.audio.playCount, 0);
  latestIntroduction.resolve(true); assert.equal(await newestSelection, true);
  assert.equal(switched.deck.audio.src, b.url);
  const failedSelection = fixture({songs:[a,b], narrate:async()=>false}); await failedSelection.deck.load();
  await failedSelection.deck.play(a); const oldButton = failedSelection.deck.continueButton;
  await failedSelection.deck.play(b); assert.equal(oldButton.onclick(), false);
  assert.equal(failedSelection.deck.audio.playCount, 0);
  await failedSelection.deck.continueButton.onclick(); assert.equal(failedSelection.deck.audio.src, b.url);
  console.log('PASS: stop/new choice invalidates ignored-abort voice promises and stale manual continuations; idle stop cannot cancel another deck');

  const pendingLibrary = deferred(), libraryStages = [];
  const cancelledLibrary = fixture({fetcher:()=>pendingLibrary.promise, narrate:async event => { libraryStages.push(event.stage); return true; }});
  cancelledLibrary.deck.command('点歌第一首'); await flush();
  const libraryWork = cancelledLibrary.deck.pendingWork;
  assert.equal(cancelledLibrary.deck.phase, 'querying'); cancelledLibrary.deck.stop();
  assert.equal(await libraryWork, false);
  pendingLibrary.resolve(response([a])); await flush();
  assert.deepEqual(libraryStages, ['ack']); assert.equal(cancelledLibrary.deck.audio.playCount, 0);
  const manualNextEvents = [], manualNextGate = deferred();
  const manualNext = fixture({songs:[a,b], narrate:event => {
    manualNextEvents.push(event.stage); return event.item?.url === b.url ? manualNextGate.promise : Promise.resolve(true);
  }});
  await manualNext.deck.load(); await manualNext.deck.play(a); manualNext.deck.enqueue(b);
  manualNext.deck.command('下一首'); await flush();
  assert.deepEqual(manualNextEvents, ['found','found']); assert.equal(manualNext.deck.audio.src, undefined);
  manualNext.deck.stop(); manualNextGate.resolve(true); await flush();
  assert.equal(manualNext.deck.audio.playCount, 1, 'stopping during next narration cannot start the queued second song');
  console.log('PASS: cancelled catalogue matching and explicit next never leak a late local playback');
}
const watchdog = setTimeout(() => { console.error('FAIL: an async song test never settled'); process.exit(1); }, 5000);
main().catch(error => { console.error(error); process.exitCode = 1; }).finally(() => clearTimeout(watchdog));
