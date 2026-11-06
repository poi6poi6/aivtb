// Exercise the actual console, song deck and Bilibili player together.
const assert = require('node:assert/strict');
const {fixture} = require('./test_avatar_performance_ui.js');

async function main() {
  const title = '白fi - 尼古喵喵 OP「なんもねぇ」';
  const song = {id:'a'.repeat(24), title, url:'/songs/'+'a'.repeat(24)};
  const video = {bvid:'BV1B7411m7LV', title:'测试视频', author:'测试UP', duration:'3:00',
    url:'https://www.bilibili.com/video/BV1B7411m7LV', cover:'', play_count:300000};
  const response = data => ({ok:true, status:200, headers:{get:()=> 'application/json'}, json:async()=>data});
  let deferBilibili = false, finishBilibili, deferSongs = false, finishSongs;
  const f = await fixture({realMedia:true, mediaFetch:async url => {
    if (url === '/songs') return deferSongs
      ? new Promise(resolve=>{finishSongs=()=>resolve(response([song]));}) : response([song]);
    if (String(url).startsWith('/fixture/bilibili-browser?')) return deferBilibili
      ? new Promise(resolve=>{finishBilibili=()=>resolve(response({results:[video]}));}) : response({results:[video]});
    return {ok:false, status:404};
  }});
  const settle = async () => {for (let i=0;i<4;i++) await f.flush();};
  const send = async text => {f.element('text').value=text; f.element('send').onclick(); await settle();};
  const injects = () => f.sent.filter(message=>message.type==='inject').length;
  const pendingStage = (stage, page = f, media = 'bilibili') => {
    const pending = page.read('window.bilibiliNarration.pending');
    assert(pending, 'the narration must have a live completion waiter');
    const request = page.sent.find(message => message.type === (media === 'local' ? 'song_narrate' : 'bilibili_narrate')
      && message.request_id === pending.requestId);
    assert(request, 'the actual page must send the controlled narration request');
    const expectedStage = media === 'bilibili' && stage === 'found' && request.stage === 'direct' ? 'direct' : stage;
    assert.equal(request.stage, expectedStage);
    if (request.stage === 'direct') {
      assert.match(request.query,/^(?:BV[A-Za-z0-9]{10}|https:\/\/www\.bilibili\.com\/video\/BV[A-Za-z0-9]{10}\?p=[1-9]\d*)$/);
      assert.equal(request.result_id,undefined); assert.equal(request.selection,undefined);
    }
    if (media === 'bilibili' && ['found','results'].includes(expectedStage)) assert.equal(typeof request.result_id,'string','actual narration includes its verified browser snapshot');
    return request;
  };
  const finishStage = async (stage, completed = true, page = f, media = 'bilibili') => {
    const request = pendingStage(stage, page, media);
    // The server closes the shared audio-owner window before resolving the
    // requester's waiter; expiring audio must not lose the completion receipt.
    page.receive({type:'bilibili_narration_end', request_id:request.request_id});
    page.receive({type:'bilibili_narration_done', request_id:request.request_id, completed});
    for (let i=0;i<4;i++) await page.flush();
    return request;
  };
  const openVideo = async () => {
    await finishStage('ack');
    await finishStage('found');
  };
  const openSong = async ({ack = true, page = f} = {}) => {
    if (ack) await finishStage('ack', true, page, 'local');
    await finishStage('found', true, page, 'local');
  };
  const noFrame = (message, page = f) => assert.equal(page.read('window.bilibiliDeck.frame'), null, message);
  const deliverNarration = async (request, id, text, page = f) => {
    page.audio.currentTime = 0;
    page.receive({type:'audio', id, turn_id:id, url:`/audio/${id}.wav`, text, emotion:'平静',
      event:{media_narration_id:request.request_id}});
    for(let i=0;i<4;i++) await page.flush();
    assert.equal(page.read('currentAudioItem.id'), id, 'only the current controlled narration may enter the actual speech player');
    page.audio.fire('playing');
    assert(page.sent.some(message=>message.type==='audio_started' && message.id===id));
    assert.equal(page.read('audioPlaying'), true);
    noFrame('narration that is still speaking must not create the official player', page);
    page.audio.currentTime = 1;
    page.audio.fire('ended');
    for(let i=0;i<4;i++) await page.flush();
    assert(page.sent.some(message=>message.type==='audio_finished' && message.id===id && message.reason==='ended'));
    noFrame('a local ended event must still wait for the server completion receipt', page);
    assert.equal(page.read('window.bilibiliNarration.pending.requestId'), request.request_id);
  };
  await settle();
  f.queue({turn:1, emotion:'平静'}); f.audio.fire('playing');
  const priorInjects = injects();
  const localPlaysBeforeRequest = f.element('song-audio').playCount;
  await send('点歌：白ＦＩ - 尼古喵喵 OP「なんもねぇ」');
  assert.equal(f.element('song-audio').playCount, localPlaysBeforeRequest);
  assert(!f.element('song-audio').src, 'a local request must not set an audio source before its acknowledgement');
  const localAck = pendingStage('ack', f, 'local');
  await deliverNarration(localAck, 81, '我找一下。');
  await finishStage('ack', true, f, 'local');
  const localFound = pendingStage('found', f, 'local');
  assert.notEqual(localFound.request_id, localAck.request_id);
  assert.equal(localFound.song.url, song.url, 'found refers to the actual matched catalog recording');
  assert.equal(localFound.song.title, title);
  assert(!localFound.local_file, 'server catalog files are not reported as temporary browser files');
  assert.equal(f.element('song-audio').playCount, localPlaysBeforeRequest);
  assert(!f.element('song-audio').src, 'the introduction must finish before loading the song audio');
  await deliverNarration(localFound, 82, '找到了，放给你听。');
  assert.equal(f.element('song-audio').playCount, localPlaysBeforeRequest,
    'the local character ended event alone cannot start music');
  await finishStage('found', true, f, 'local');
  assert.equal(f.read('window.songDeck.current.title'), title);
  assert(f.element('song-audio').playCount > 0);
  assert.equal(injects(), priorInjects, 'a recognized request must execute instead of reaching the LLM');
  assert.equal(f.read('currentAudioItem'), null, 'music replaces the current character speech');
  f.receive({type:'audio_control', action:'clear', id:11});
  assert.equal(f.read('window.songDeck.active'), true, 'the late speech clear must not stop the song it yielded to');
  f.receive({type:'audio_control', action:'clear', id:null});
  assert.equal(f.read('window.songDeck.active'), false, 'a global ownership change still stops media');
  console.log('PASS: local colon/fullwidth requests acknowledge and introduce real catalog matches before music starts');

  f.read(`window.songDeck.local.push({title:'浏览器歌曲',url:'blob:fixture-local-audio'})`);
  await send('播放《浏览器歌曲》');
  await finishStage('ack', true, f, 'local');
  const blobFound = pendingStage('found', f, 'local');
  assert.equal(blobFound.song.local_file, true);
  assert.equal(blobFound.song.title, '浏览器歌曲');
  assert(!JSON.stringify(blobFound).includes('blob:'), 'the narration request must not upload or expose browser-local audio URLs');
  await finishStage('found', true, f, 'local');
  assert.equal(f.element('song-audio').src, 'blob:fixture-local-audio', 'browser-selected files need no backend catalog entry');
  const priorInterrupts = f.sent.filter(message=>message.type==='conversation_interrupt').length;
  const priorLookups = f.fetches.filter(([url])=>String(url).startsWith('/fixture/bilibili-browser?')).length;
  await send('BV1B7411m7LV');
  assert.equal(f.read('window.songDeck.active'), false);
  assert.equal(f.read('window.bilibiliDeck.active'), true);
  noFrame('a BV request must first await the character acknowledgement');
  assert.equal(f.fetches.filter(([url])=>String(url).startsWith('/fixture/bilibili-browser?')).length, priorLookups,
    'the real search starts only after the acknowledgement finishes');
  const ack = pendingStage('ack');
  assert.equal(ack.query, video.bvid);
  await deliverNarration(ack, 101, '好呀，收到这个视频了。');
  f.receive({type:'bilibili_narration_done',request_id:'obsolete-request',completed:true});
  assert.equal(f.read('window.bilibiliNarration.pending.requestId'), ack.request_id);
  await finishStage('ack');
  const found = pendingStage('found');
  assert.notEqual(found.request_id, ack.request_id, 'each narration stage owns a distinct audio authorization');
  assert.equal(found.stage,'direct');
  assert.equal(found.query,video.bvid);
  assert.equal(found.bvid,undefined,'direct introduction carries only the explicit reference, not invented metadata');
  f.receive({type:'audio', id:78, turn_id:7, url:'/audio/78.wav', text:'unrelated speech', emotion:'平静'});
  f.receive({type:'audio', id:79, turn_id:7, url:'/audio/79.wav', text:'old narration', emotion:'平静',
    event:{media_narration_id:ack.request_id}});
  assert.equal(f.read('currentAudioItem'), null, 'normal and stale narration TTS cannot replace the pending introduction');
  assert(f.sent.some(message=>message.type==='audio_finished' && message.id===78));
  assert(f.sent.some(message=>message.type==='audio_finished' && message.id===79));
  await deliverNarration(found, 102, '就放你指定的这个视频，来一起听吧。');
  await finishStage('found');
  assert.equal(f.sent.filter(message=>message.type==='conversation_interrupt').length, priorInterrupts,
    'media preparation must not issue a second interrupt that cancels its own narration');
  const frame = f.element('bilibili-player').children[0];
  const url = new URL(frame.src);
  assert.equal(url.origin, 'https://player.bilibili.com');
  assert.equal(url.searchParams.get('bvid'), video.bvid);
  assert.equal(url.searchParams.get('autoplay'), '1');
  f.receive({type:'bilibili_narration_done',request_id:found.request_id,completed:true});
  await settle();
  assert.equal(f.element('bilibili-player').children.length, 1, 'duplicate receipts cannot open another player');
  assert.equal(injects(), priorInjects);
  f.receive({type:'audio_control', action:'clear', id:11});
  assert.equal(f.read('window.bilibiliDeck.active'), true, 'speech cleanup also leaves the requested Bilibili video intact');
  f.receive({type:'audio', id:77, turn_id:7, url:'/audio/77.wav', text:'late speech', emotion:'平静'});
  assert.equal(f.read('currentAudioItem'), null, 'late TTS cannot talk over the selected media');
  assert(f.sent.some(message=>message.type==='audio_finished' && message.id===77 && message.reason==='song_playing'));
  await send('停止播放');
  assert.equal(f.read('window.bilibiliDeck.active'), false);
  assert.equal(f.element('bilibili-player').children.length, 0);
  console.log('PASS: local files and BV requests hand off through real acknowledgement/introduction audio and server completion');

  f.receive({type:'bilibili_request', query:'https://www.bilibili.com/video/BV1B7411m7LV/?p=2'});
  await settle();
  await openVideo();
  assert.equal(new URL(f.element('bilibili-player').children[0].src).searchParams.get('p'), '2');
  f.receive({type:'song_control', action:'stop'});
  assert.equal(f.read('window.bilibiliDeck.active'), false);
  f.receive({type:'song_request', song}); await settle();
  await openSong({ack:false});
  assert.equal(f.read('window.songDeck.current.title'), title);
  f.element('interrupt-chat').onclick();
  assert.equal(f.read('window.songDeck.active'), false);
  await send('BV1B7411m7LV');
  await openVideo();
  await send('今天怎么样');
  assert.equal(f.read('window.bilibiliDeck.active'), false, 'normal chat resumes speech and closes the external player');
  assert.equal(injects(), priorInjects+1);
  console.log('PASS: server media intents, multipart links, explicit interrupt and return to ordinary conversation');

  deferBilibili = true;
  f.read("continuousEl.checked=true; setInteraction('idle'); scheduleAutoListening()");
  await send('B站点歌 测试关键词');
  assert.equal(f.read('window.bilibiliDeck.active'), true);
  await finishStage('ack');
  f.tick(600);
  assert.equal(f.read('voiceActive || voiceStarting'), false,
    'an older continuous-recording timer cannot open the microphone while a media request is waiting');
  await send('点歌：没有这个标题');
  assert.equal(f.read('window.bilibiliDeck.active'), false);
  finishBilibili(); await settle();
  assert.equal(f.element('bilibili-player').children.length, 0,
    'an older Bilibili lookup cannot revive after a new local request, even when the local title is absent');
  console.log('PASS: newest command wins across remote lookup and local catalog matching');

  deferSongs = true;
  f.read('window.songDeck.load({force:true})');
  await send('点歌：'+title);
  assert(f.read('window.songDeck.pending'));
  f.element('bilibili-query').value = video.bvid;
  f.element('bilibili-form').onsubmit({preventDefault(){}});
  await settle();
  assert.equal(f.read('window.songDeck.pending'), null, 'a newer direct Bilibili form request also cancels pending local matching');
  await finishStage('ack');
  finishSongs(); await settle();
  assert.equal(f.read('window.songDeck.current'), null);
  finishBilibili(); await settle();
  assert.equal(f.read('window.bilibiliDeck.active'), true);
  pendingStage('found');
  noFrame('server-selected local music may supersede a verified video while its introduction is waiting');
  f.receive({type:'song_request', song}); await settle();
  assert.equal(f.read('window.bilibiliDeck.active'), false);
  await openSong({ack:false});
  assert.equal(f.read('window.songDeck.current.title'), title);
  console.log('PASS: direct search form and server song selections supersede older work across both decks');

  deferBilibili = false;
  await send(video.bvid);
  await finishStage('ack');
  const blocked = pendingStage('found');
  f.element('autoplay').checked = false;
  const priorSpeechPlays = f.audio.playCount;
  f.receive({type:'audio',id:201,turn_id:201,url:'/audio/201.wav',text:'找到了，放给你听。',emotion:'平静',
    event:{media_narration_id:blocked.request_id}});
  await settle();
  assert.equal(f.audio.playCount, priorSpeechPlays, 'disabled narration autoplay cannot be mistaken for audible completion');
  assert(f.sent.some(message=>message.type==='audio_finished' && message.id===201 && message.reason==='autoplay_disabled'));
  await finishStage('found', false);
  noFrame('failed narration must never silently unlock the video');
  assert.equal(f.read('window.bilibiliDeck.phase'), 'awaiting-continue');
  const continueButton = f.read('window.bilibiliDeck.continueButton');
  assert(continueButton && /继续/.test(continueButton.textContent), 'the user receives an explicit manual continuation');
  f.receive({type:'bilibili_narration_done',request_id:blocked.request_id,completed:true});
  await settle();
  noFrame('a late success cannot reverse a completed failure');
  await continueButton.onclick(); await settle();
  assert.equal(f.read('window.bilibiliDeck.phase'), 'opened', 'only the user continuation explicitly skips failed narration');
  f.element('autoplay').checked = true;
  console.log('PASS: inaudible or failed narration waits for an explicit user continuation');

  await send(video.bvid);
  await finishStage('ack');
  const cancelled = pendingStage('found');
  f.receive({type:'audio',id:301,turn_id:301,url:'/audio/301.wav',text:'找到了，放给你听。',emotion:'平静',
    event:{media_narration_id:cancelled.request_id}});
  await settle(); f.audio.fire('playing');
  await send('停止播放');
  assert.equal(f.read('window.bilibiliNarration.pending'), null);
  assert.equal(f.read('currentAudioItem'), null, 'cancellation also stops the in-flight character narration');
  assert(f.sent.some(message=>message.type==='bilibili_narration_cancel' && message.request_id===cancelled.request_id));
  f.audio.fire('ended');
  f.receive({type:'bilibili_narration_done',request_id:cancelled.request_id,completed:true});
  const speechPlaysAfterCancel = f.audio.playCount;
  f.receive({type:'audio',id:302,turn_id:301,url:'/audio/302.wav',text:'迟到的旧介绍',emotion:'平静',
    event:{media_narration_id:cancelled.request_id}});
  await settle();
  assert.equal(f.audio.playCount, speechPlaysAfterCancel, 'cancelled narration arriving after both decks become idle must stay cancelled');
  assert.equal(f.read('currentAudioItem'), null);
  noFrame('late ended, audio and completion messages cannot resurrect a cancelled video');
  assert.equal(f.read('window.bilibiliDeck.phase'), 'idle');
  await send(video.bvid);
  const newer = pendingStage('ack');
  f.receive({type:'bilibili_narration_done',request_id:cancelled.request_id,completed:true});
  await settle();
  assert.equal(f.read('window.bilibiliNarration.pending.requestId'), newer.request_id,
    'a previous completion cannot release the next request');
  await send('现在聊点别的');
  f.receive({type:'bilibili_narration_done',request_id:newer.request_id,completed:true});
  await settle();
  noFrame('ordinary conversation cancels narration as well as video playback');
  console.log('PASS: stopping and new conversation cancel narration, with late audio/ended/receipt messages unable to revive it');

  const obs = await fixture({realMedia:true, mediaFetch:async url => url === '/songs' ? response([song])
    : String(url).startsWith('/fixture/bilibili-browser?') ? response({results:[video]}) : {ok:false,status:404}});
  obs.receive({type:'audio_owner',audio_owner:false,owner:'display'});
  const obsSpeechPlays = obs.audio.playCount;
  obs.element('text').value=video.bvid; obs.element('send').onclick();
  for(let i=0;i<4;i++) await obs.flush();
  pendingStage('ack', obs);
  noFrame('a controller whose voice is in OBS still waits for acknowledgement', obs);
  await finishStage('ack', true, obs);
  pendingStage('found', obs);
  noFrame('no local Audio events are needed or synthesized while OBS owns speech', obs);
  await finishStage('found', true, obs);
  assert.equal(obs.audio.playCount, obsSpeechPlays, 'the controller must not duplicate OBS character audio');
  assert.equal(obs.read('window.bilibiliDeck.phase'), 'opened');
  assert.equal(obs.element('bilibili-player').children.length, 1,
    'the audio owner server receipt, not this page local ended event, opens the official player');
  assert.equal(obs.sent.filter(message=>message.type==='audio_started' || message.type==='audio_finished').length, 0);
  obs.element('text').value='点歌：'+title; obs.element('send').onclick();
  for(let i=0;i<4;i++) await obs.flush();
  await finishStage('ack', true, obs, 'local');
  assert.equal(obs.element('song-audio').playCount, 0);
  await finishStage('found', true, obs, 'local');
  assert.equal(obs.element('song-audio').src, song.url, 'local songs also wait for narration completed by the separate OBS audio owner');
  assert.equal(obs.audio.playCount, obsSpeechPlays);
  assert.equal(obs.sent.filter(message=>message.type==='audio_started' || message.type==='audio_finished').length, 0);
  console.log('PASS: OBS-owned narration releases both video and local music using server completion without local speech');

  const owner = await fixture({realMedia:true, mediaFetch:async url => url === '/songs' ? response([])
    : {ok:false,status:404}});
  const ownerRequest = 'bili-other-console-found';
  const ownerReply = {type:'reply',id:401,mode:'media',media_narration_id:ownerRequest,
    text:'找到了，放给你听。',emotion:'平静',actions:[]};
  assert.equal(owner.read('window.bilibiliNarration.pending'), null, 'the audio owner need not be the console that requested a video');
  owner.receive({type:'bilibili_playback_state',active:true});
  owner.receive(ownerReply);
  owner.receive({type:'audio',id:401,turn_id:401,url:'/audio/401.wav',text:ownerReply.text,emotion:'平静',
    event:{media_narration_id:ownerRequest}});
  for(let i=0;i<4;i++) await owner.flush();
  assert.equal(owner.read('currentAudioItem.id'), 401, 'a server-marked reply authorizes the separate audio owner to speak');
  owner.audio.fire('playing'); owner.audio.currentTime = 1; owner.audio.fire('ended');
  assert(owner.sent.some(message=>message.type==='audio_finished' && message.id===401 && message.reason==='ended'));
  owner.receive({type:'bilibili_narration_end',request_id:ownerRequest});
  assert.equal(owner.read('window.bilibiliNarration.playbackRequestId'), null);
  const ownerPlays = owner.audio.playCount;
  owner.receive(ownerReply);
  owner.receive({type:'audio',id:402,turn_id:401,url:'/audio/402.wav',text:'迟到的跨页面介绍',emotion:'平静',
    event:{media_narration_id:ownerRequest}});
  for(let i=0;i<4;i++) await owner.flush();
  assert.equal(owner.audio.playCount, ownerPlays, 'a late reply cannot reauthorize narration after its shared end message');
  assert.equal(owner.read('currentAudioItem'), null);
  noFrame('the audio owner must not independently create a second video player', owner);
  owner.receive({type:'audio',id:403,turn_id:403,url:'/audio/403.wav',text:'后台闲聊',emotion:'平静'});
  assert.equal(owner.read('currentAudioItem'), null, 'ordinary OBS speech cannot overlap a video playing in the control page');
  owner.receive({type:'bilibili_playback_state',active:false});
  owner.receive({type:'audio',id:404,turn_id:404,url:'/audio/404.wav',text:'恢复聊天',emotion:'平静'});
  assert.equal(owner.read('currentAudioItem.id'), 404, 'stopping the remote video releases normal speech');
  console.log('PASS: a separate audio owner accepts server-marked narration and permanently rejects its late replay');

  const draft = await fixture({realMedia:true, mediaFetch:async url => url === '/songs' ? response([])
    : String(url).startsWith('/fixture/bilibili-browser?') ? response({results:[video]}) : {ok:false,status:404}});
  draft.read("voiceDraft=true; pausedForMic=true; textInputEl.value='保留我的草稿'");
  draft.element('bilibili-query').value = video.bvid;
  draft.element('bilibili-form').onsubmit({preventDefault(){}});
  for(let i=0;i<4;i++) await draft.flush();
  const draftAck = pendingStage('ack', draft);
  draft.receive({type:'audio',id:501,turn_id:501,url:'/audio/501.wav',text:'我找找',emotion:'平静',
    event:{media_narration_id:draftAck.request_id}});
  for(let i=0;i<4;i++) await draft.flush();
  assert(draft.audio.playCount > 0, 'a reviewed draft must not pause an explicitly requested music acknowledgement');
  assert.equal(draft.read('textInputEl.value'), '保留我的草稿');
  assert.equal(draft.read('voiceDraft'), true, 'starting music must not discard unsent user text');
  assert.equal(draft.read('pausedForMic'), false);
  draft.read('stopAllMedia()');
  console.log('PASS: requesting Bilibili from its panel speaks normally while preserving an unsent voice draft');

  const second = {id:'b'.repeat(24),title:'测试第二首',url:'/songs/'+'b'.repeat(24)};
  const third = {id:'c'.repeat(24),title:'测试第三首',url:'/songs/'+'c'.repeat(24)};
  const versions = [{id:'d'.repeat(24),title:'同名歌 现场版',url:'/songs/'+'d'.repeat(24)},
    {id:'e'.repeat(24),title:'同名歌 录音室版',url:'/songs/'+'e'.repeat(24)}];
  const local = await fixture({realMedia:true, mediaFetch:async url => url === '/songs'
    ? response([song,second,third,...versions]) : {ok:false,status:404}});
  const settleLocal = async () => {for(let i=0;i<4;i++) await local.flush();};
  const sendLocal = async text => {local.element('text').value=text; local.element('send').onclick(); await settleLocal();};
  const localNarrations = () => local.sent.filter(message=>message.type==='song_narrate');
  const songRow = title => {
    const row = local.element('song-list').children.find(row=>row.children[0]?.textContent===title);
    assert(row, `the rendered library contains ${title}`);
    return row;
  };
  await settleLocal();
  songRow(title).children[1].onclick(); await settleLocal();
  assert.equal(localNarrations().length, 1, 'choosing a concrete library recording needs only its introduction');
  pendingStage('found', local, 'local');
  assert(!local.element('song-audio').src);
  assert.equal(local.element('song-audio').playCount, 0);
  await openSong({ack:false,page:local});
  assert.equal(local.element('song-audio').src, song.url);
  const narrationCountBeforeQueue = localNarrations().length;
  songRow(second.title).children[2].onclick(); await settleLocal();
  local.receive({type:'song_request',song:third}); await settleLocal();
  assert.equal(localNarrations().length, narrationCountBeforeQueue, 'adding future songs must not narrate or interrupt the current song');
  assert.equal(local.read('window.songDeck.current.title'), title);
  assert.equal(local.element('song-audio').src, song.url);
  assert.equal(local.element('song-audio').playCount, 1);
  assert.equal(local.read('window.songDeck.queue.length'), 2);
  local.element('song-next').onclick(); await settleLocal();
  assert.equal(pendingStage('found', local, 'local').song.url, second.url);
  assert.equal(localNarrations().length, narrationCountBeforeQueue+1, 'next uses found only, without repeating the lookup acknowledgement');
  assert(!local.element('song-audio').src, 'next must stop the prior song while its introduction is speaking');
  await openSong({ack:false,page:local});
  assert.equal(local.element('song-audio').src, second.url);
  local.element('song-audio').paused = true;
  local.element('song-audio').onended(); await settleLocal();
  assert.equal(pendingStage('found', local, 'local').song.url, third.url);
  assert.equal(localNarrations().length, narrationCountBeforeQueue+2, 'natural queue advancement also uses only found');
  assert.equal(local.element('song-audio').playCount, 2, 'queued music cannot start before the new introduction completes');
  await openSong({ack:false,page:local});
  assert.equal(local.element('song-audio').src, third.url);
  assert.equal(local.element('song-audio').playCount, 3);
  assert(local.sent.some(message=>message.type==='song_playback_state' && message.active===true),
    'local music publishes the shared media ownership lease');
  local.element('song-stop').onclick(); await settleLocal();
  assert.equal(local.sent.filter(message=>message.type==='song_playback_state').at(-1).active, false,
    'stopping local music releases its shared audio lease');
  console.log('PASS: local buttons, server enqueue, next and natural queues introduce selections without interrupting enqueue');

  const playsBeforeFailure = local.element('song-audio').playCount;
  await sendLocal('点歌：'+title);
  await finishStage('ack', true, local, 'local');
  const failedLocal = pendingStage('found', local, 'local');
  await finishStage('found', false, local, 'local');
  assert.equal(local.element('song-audio').playCount, playsBeforeFailure);
  assert(!local.element('song-audio').src);
  const localContinue = local.read('window.songDeck.continueButton');
  assert(localContinue && /继续/.test(localContinue.textContent), 'failed local narration exposes an explicit continuation');
  local.receive({type:'bilibili_narration_done',request_id:failedLocal.request_id,completed:true}); await settleLocal();
  assert.equal(local.element('song-audio').playCount, playsBeforeFailure, 'a stale successful receipt cannot undo failed local narration');
  await localContinue.onclick(); await settleLocal();
  assert.equal(local.element('song-audio').src, song.url);
  assert.equal(local.element('song-audio').playCount, playsBeforeFailure+1);
  local.element('song-stop').onclick(); await settleLocal();
  await sendLocal('点歌：'+second.title);
  await finishStage('ack', true, local, 'local');
  const stoppedLocal = pendingStage('found', local, 'local');
  await sendLocal('停歌');
  const playsAfterStop = local.element('song-audio').playCount;
  local.receive({type:'bilibili_narration_end',request_id:stoppedLocal.request_id});
  local.receive({type:'bilibili_narration_done',request_id:stoppedLocal.request_id,completed:true});
  local.receive({type:'audio',id:601,turn_id:601,url:'/audio/601.wav',text:'过期的本地介绍',emotion:'平静',
    event:{media_narration_id:stoppedLocal.request_id}});
  local.audio.fire('ended'); await settleLocal();
  assert.equal(local.read('window.bilibiliNarration.pending'), null);
  assert.equal(local.read('currentAudioItem'), null);
  assert.equal(local.element('song-audio').playCount, playsAfterStop);
  assert(!local.element('song-audio').src, 'stopped local narration cannot revive a song through late audio or done events');
  assert(local.sent.some(message=>message.type==='bilibili_narration_cancel' && message.request_id===stoppedLocal.request_id));
  console.log('PASS: failed local introductions require manual continuation and cancelled introductions cannot restart music');

  await sendLocal('点歌：曲库绝对不存在的歌曲');
  await finishStage('ack', true, local, 'local');
  pendingStage('error', local, 'local');
  await finishStage('error', true, local, 'local');
  assert(!local.element('song-audio').src);
  assert.equal(local.element('song-audio').playCount, playsAfterStop);
  assert.match(local.element('song-status').textContent, /没有|未找到|未匹配|还没有/);
  await sendLocal('点歌：同名歌');
  await finishStage('ack', true, local, 'local');
  pendingStage('results', local, 'local');
  await finishStage('results', true, local, 'local');
  assert(!local.element('song-audio').src, 'ambiguous library matches must never pick a random recording');
  assert.equal(local.element('song-audio').playCount, playsAfterStop);
  assert.match(local.element('song-status').textContent, /多首|版本|选择/);
  assert.equal(local.sent.filter(message=>message.type==='inject').length, 0,
    'not-found and ambiguous local songs remain deterministic media interactions, not LLM guesses');
  const narrationsBeforeChoice = localNarrations().length;
  songRow(versions[1].title).children[1].onclick(); await settleLocal();
  assert.equal(pendingStage('found', local, 'local').song.url, versions[1].url);
  await openSong({ack:false,page:local});
  assert.equal(localNarrations().length, narrationsBeforeChoice+1);
  assert.equal(local.element('song-audio').src, versions[1].url, 'the user can resolve ambiguity through an actual library play button');
  console.log('PASS: missing and ambiguous local requests speak accurate outcomes and require a real selection');

  for (const remoteMessage of [{type:'media_focus'}, {type:'bilibili_playback_state',active:true}]) {
    const microphone = await fixture({realMedia:true, mediaFetch:async()=>response([])});
    microphone.read("continuousEl.checked=true; setInteraction('idle'); scheduleAutoListening()");
    microphone.tick(600);
    assert.equal(microphone.read('voiceActive'), true, 'this regression begins with a microphone already listening');
    microphone.read("voiceDraft=true; textInputEl.value='保留我的语音草稿'");
    const stopMessages = microphone.sent.filter(message=>message.type==='media_stop').length;
    microphone.receive(remoteMessage); microphone.tick(1000);
    assert.equal(microphone.read('voiceActive || voiceStarting'), false,
      'remote music must stop existing recording, not merely block future microphone timers');
    assert.equal(microphone.read('continuousEl.checked'), false);
    assert.equal(microphone.read('voiceDraft'), true);
    assert.equal(microphone.read('textInputEl.value'), '保留我的语音草稿');
    assert.equal(microphone.sent.filter(message=>message.type==='media_stop').length, stopMessages,
      'receiving remote focus must not ask every page to stop the newly selected player');
    microphone.receive({type:'bilibili_playback_state',active:true});
    microphone.read("voiceDraft=false; textInputEl.value=''; continuousEl.checked=true; setInteraction('idle'); scheduleAutoListening()");
    microphone.tick(600);
    assert.equal(microphone.read('voiceActive || voiceStarting'), false, 'global media ownership also blocks new automatic listening');
  }
  console.log('PASS: remote media focus stops active and future continuous recording while retaining the unsent voice draft');

  const focus = await fixture({realMedia:true, mediaFetch:async()=>response([song])});
  const settleFocus = async () => {for(let i=0;i<4;i++) await focus.flush();};
  focus.receive({type:'song_request',song}); await settleFocus();
  const focusPending = pendingStage('found', focus, 'local');
  focus.receive({type:'media_focus'}); await settleFocus();
  assert.equal(focus.read('window.songDeck.active'), false);
  assert.equal(focus.read('window.bilibiliNarration.pending'), null);
  focus.receive({type:'bilibili_narration_end',request_id:focusPending.request_id});
  focus.receive({type:'bilibili_narration_done',request_id:focusPending.request_id,completed:true});
  await settleFocus();
  assert.equal(focus.element('song-audio').playCount, 0, 'a superseded page cannot start its pending song after remote focus changes');
  for (const stopMessage of [{type:'media_focus'}, {type:'song_control',action:'stop'}]) {
    focus.receive({type:'song_request',song}); await settleFocus();
    await openSong({ack:false,page:focus});
    assert.equal(focus.element('song-audio').src, song.url);
    focus.receive(stopMessage); await settleFocus();
    assert.equal(focus.read('window.songDeck.current'), null);
    assert(!focus.element('song-audio').src, 'a remote focus/stop actually detaches already-playing local audio');
    assert.equal(focus.sent.filter(message=>message.type==='song_playback_state').at(-1).active, false);
  }
  assert.equal(focus.sent.filter(message=>message.type==='media_stop').length, 0,
    'focus and server stop only release this page, avoiding a cross-page stop echo');
  console.log('PASS: cross-page focus cancels local pending and playing songs without global stop loops');

  const sea = {id:'f'.repeat(24),title:'海色',url:'/songs/'+'f'.repeat(24)};
  for (const source of ['catalog','browser']) {
    const greeting = await fixture({realMedia:true, mediaFetch:async url => url === '/songs'
      ? response(source === 'catalog' ? [sea] : []) : {ok:false,status:404}});
    for(let i=0;i<4;i++) await greeting.flush();
    if (source === 'browser') greeting.read("window.songDeck.local.push({title:'海色',url:'blob:fixture-sea'})");
    greeting.element('text').value='你好，点歌 海色';
    greeting.element('send').onclick();
    for(let i=0;i<4;i++) await greeting.flush();
    assert.equal(greeting.sent.filter(message=>message.type==='inject').length, 0,
      'the exact screenshot greeting must route to music instead of an LLM catalog guess');
    const greetingAck = pendingStage('ack', greeting, 'local');
    assert.equal(greetingAck.query, '海色');
    assert(!greeting.element('song-audio').src);
    await deliverNarration(greetingAck, 701, '好呀，我找找这首。', greeting);
    await finishStage('ack', true, greeting, 'local');
    const greetingFound = pendingStage('found', greeting, 'local');
    assert.equal(greetingFound.song.title, '海色');
    if (source === 'catalog') assert.equal(greetingFound.song.url, sea.url);
    else {
      assert.equal(greetingFound.song.local_file, true);
      assert(!JSON.stringify(greetingFound).includes('blob:'), 'browser selection still sends only filename metadata');
    }
    await deliverNarration(greetingFound, 702, '找到了，放给你听。', greeting);
    assert.equal(greeting.element('song-audio').playCount, 0, 'natural-language routing retains the actual introduction completion gate');
    assert(!greeting.element('song-audio').src);
    await finishStage('found', true, greeting, 'local');
    assert.equal(greeting.element('song-audio').src, source === 'catalog' ? sea.url : 'blob:fixture-sea');
    assert.equal(greeting.element('song-audio').playCount, 1);
    const narrationCount = greeting.sent.filter(message=>message.type==='song_narrate').length;
    let ordinaryMessages = 0;
    for (const text of ['你好，不要点歌 海色', '我说的是“点歌 海色”这句话',
      '刚才我点歌 海色', '如果我说点歌 海色会发生什么']) {
      greeting.element('text').value=text; greeting.element('send').onclick();
      for(let i=0;i<4;i++) await greeting.flush();
      assert.equal(greeting.sent.filter(message=>message.type==='inject').length, ++ordinaryMessages,
        'negation, discussion, past events and hypothetical commands remain ordinary conversation');
      assert.equal(greeting.sent.filter(message=>message.type==='song_narrate').length, narrationCount);
      assert.equal(greeting.element('song-audio').playCount, 1);
    }
  }
  console.log('PASS: greeting song requests play actual server and browser songs through narration while nonrequests remain chat');

  const lowerVideo={...video,title:'海色较少播放版本',play_count:200};
  const highestVideo={bvid:'BV1xx411c7mD',title:'海色最多播放版本',author:'测试UP',duration:'4:00',play_count:200000};
  const highest=await fixture({realMedia:true,mediaFetch:async url=>url==='/songs' ? response([])
    : String(url).startsWith('/fixture/bilibili-browser?') ? response({results:[lowerVideo,highestVideo]}) : {ok:false,status:404}});
  const settleHighest=async()=>{for(let i=0;i<4;i++)await highest.flush();};
  const sendHighest=async text=>{highest.element('text').value=text;highest.element('send').onclick();await settleHighest();};
  await sendHighest('你好，在B站点歌 海色');
  const highestAck=pendingStage('ack',highest);
  assert.equal(highestAck.query,'海色');
  assert.equal(highestAck.selection,undefined);
  await deliverNarration(highestAck,801,'好呀，我去找找这首。',highest);
  await finishStage('ack',true,highest);
  const highestFound=pendingStage('found',highest);
  assert.equal(highestFound.bvid,highestVideo.bvid,'the complete page automatically selects the actual greatest play count');
  assert.equal(highestFound.selection,'highest_play','only automatic ranking asks the server to verify and announce highest play count');
  assert.deepEqual(highest.sent.filter(message=>message.type==='bilibili_narrate').map(message=>message.stage),['ack','found']);
  assert.equal(highest.sent.filter(message=>message.type==='inject').length,0);
  assert.equal(highest.element('bilibili-results').children.length,2);
  await deliverNarration(highestFound,802,'这次搜到的结果里，播放量最高的是海色最多播放版本。',highest);
  noFrame('even the ranked automatic choice must wait for the true server narration completion',highest);
  await finishStage('found',true,highest);
  assert.equal(new URL(highest.element('bilibili-player').children[0].src).searchParams.get('bvid'),highestVideo.bvid);
  const otherVersion=highest.element('bilibili-results').children[1];
  otherVersion.children.find(child=>child.textContent==='播放这个版本').onclick();await settleHighest();
  const manualVersion=pendingStage('found',highest);
  assert.equal(manualVersion.bvid,lowerVideo.bvid);
  assert.equal(manualVersion.selection,undefined,'manual alternate versions must not inherit the highest-play claim');
  await finishStage('found',true,highest);
  assert.equal(new URL(highest.element('bilibili-player').children[0].src).searchParams.get('bvid'),lowerVideo.bvid);
  await sendHighest('https://www.bilibili.com/video/'+lowerVideo.bvid+'/?p=3');
  await finishStage('ack',true,highest);
  const directPart=pendingStage('found',highest);
  assert.equal(directPart.stage,'direct');
  assert.equal(directPart.bvid,undefined);
  assert.equal(directPart.query,'https://www.bilibili.com/video/'+lowerVideo.bvid+'?p=3');
  assert.equal(highest.read('window.bilibiliDeck.directTarget.page'),3,'direct narration binds the explicit part without a snapshot');
  assert.equal(directPart.selection,undefined);
  await finishStage('found',true,highest);
  assert.equal(new URL(highest.element('bilibili-player').children[0].src).searchParams.get('p'),'3');
  for(const action of ['停止播放','我们继续聊天']) {
    await sendHighest('B站点歌 海色');await finishStage('ack',true,highest);
    const cancelledTop=pendingStage('found',highest);
    assert.equal(cancelledTop.selection,'highest_play');
    await sendHighest(action);
    highest.receive({type:'bilibili_narration_end',request_id:cancelledTop.request_id});
    highest.receive({type:'bilibili_narration_done',request_id:cancelledTop.request_id,completed:true});
    await settleHighest();
    noFrame('stopping or returning to chat cancels a pending highest-play selection',highest);
    assert.equal(highest.read('window.bilibiliNarration.pending'),null);
  }
  console.log('PASS: keyword top-play selection reaches verified narration, actual speech completion and official playback with version/stop parity');

  const unknownPlays=await fixture({realMedia:true,mediaFetch:async url=>url==='/songs' ? response([])
    : String(url).startsWith('/fixture/bilibili-browser?') ? response({results:[{...lowerVideo,play_count:null},{...highestVideo,play_count:null}]})
    : {ok:false,status:404}});
  unknownPlays.element('text').value='B站点歌 海色';unknownPlays.element('send').onclick();
  for(let i=0;i<4;i++)await unknownPlays.flush();
  await finishStage('ack',true,unknownPlays);
  const unknownResults=pendingStage('results',unknownPlays);
  assert.equal(unknownResults.error,'missing_play_count');
  assert.equal(unknownResults.selection,undefined);
  await finishStage('results',true,unknownPlays);
  noFrame('unknown counts cannot fabricate a greatest-count video',unknownPlays);
  assert.match(unknownPlays.element('bilibili-status').textContent,/播放量.*手动选择/);
  unknownPlays.element('bilibili-results').children[0].children.find(child=>child.textContent==='播放这个版本').onclick();
  for(let i=0;i<4;i++)await unknownPlays.flush();
  assert.equal(pendingStage('found',unknownPlays).selection,undefined);
  await finishStage('found',true,unknownPlays);
  assert.equal(new URL(unknownPlays.element('bilibili-player').children[0].src).searchParams.get('bvid'),lowerVideo.bvid);
  console.log('PASS: unavailable play counts retain honest results narration and explicit manual selection');

  for (const playCount of [199999,0]) {
    const below=await fixture({realMedia:true,mediaFetch:async url=>url==='/songs' ? response([])
      : String(url).startsWith('/fixture/bilibili-browser?') ? response({results:[{...video,play_count:playCount}]})
      : {ok:false,status:404}});
    below.element('text').value='B站点歌 海色';below.element('send').onclick();
    for(let i=0;i<4;i++)await below.flush();
    await finishStage('ack',true,below);
    const belowResults=pendingStage('results',below);
    assert.equal(belowResults.error,'below_play_threshold','the real bridge reports why automatic selection was withheld');
    assert.equal(belowResults.selection,undefined);
    assert.equal(belowResults.bvid,undefined,'below-threshold results never queue a hidden found introduction');
    await deliverNarration(belowResults,901,'这次结果的播放量还没有达到20万，先不自动播放。',below);
    noFrame('finishing the low-count explanation must not start a video',below);
    await finishStage('results',true,below);
    noFrame('even a successful server receipt for results does not open a below-threshold video',below);
    assert.deepEqual(below.sent.filter(message=>message.type==='bilibili_narrate').map(message=>message.stage),['ack','results']);
    assert.match(below.element('bilibili-status').textContent,/未.*自动播放/);
    assert.equal(below.element('bilibili-results').children.length,1);
    below.element('bilibili-results').children[0].children.find(child=>child.textContent==='播放这个版本').onclick();
    for(let i=0;i<4;i++)await below.flush();
    const manualBelow=pendingStage('found',below);
    assert.equal(manualBelow.bvid,video.bvid);
    assert.equal(manualBelow.selection,undefined,'explicit low-count choices are not automatic highest-play claims');
    await deliverNarration(manualBelow,902,'找到了，来一起听吧。',below);
    await finishStage('found',true,below);
    assert.equal(new URL(below.element('bilibili-player').children[0].src).searchParams.get('bvid'),video.bvid);
  }
  console.log('PASS: below-200000 search results explain the threshold without autoplay, while manual selections retain narration gating');

  for(const code of ['network','timeout','rate_limited','upstream_rejected','invalid_response','service','closed']) {
    const message='当前查询未成功：'+code+'，稍后可重试。';
    const failure=await fixture({realMedia:true,mediaFetch:async url=>url==='/songs' ? response([])
      : {ok:false,status:code==='rate_limited'?429:502,headers:{get:()=> 'application/json'},
        json:async()=>({error:message,error_code:code,retry_after:2})}});
    failure.element('text').value="B站点歌 it's happening";failure.element('send').onclick();
    for(let i=0;i<4;i++)await failure.flush();
    assert.equal(pendingStage('ack',failure).query,"it's happening");
    await finishStage('ack',true,failure);
    const failed=pendingStage('error',failure);
    assert.equal(failed.error,code==='closed'?'service':code,'the backend search error category reaches the actual narration WebSocket request');
    assert.equal(failed.query,"it's happening");
    const request=failure.fetches.find(([url])=>String(url).startsWith('/fixture/bilibili-browser?'));
    assert.equal(new URL(request[0],'https://fixture.test').searchParams.get('q'),"it's happening");
    assert.equal(failure.element('bilibili-status').textContent,message);
    assert.equal(new URL(failure.element('bilibili-external-search').href).searchParams.get('keyword'),"it's happening");
    await finishStage('error',true,failure);
    assert.equal(failure.sent.filter(event=>event.type==='inject').length,0);
    assert.equal(failure.fetches.filter(([url])=>String(url).startsWith('/fixture/bilibili-browser?')).length,1,
      'error narration must not turn into a second search or an automatic retry');
    noFrame('classified search failures cannot fabricate playable results',failure);
  }
  console.log("PASS: it's happening preserves its apostrophe and typed search failures reach the real narration bridge without retries");

  const happeningVideo={bvid:video.bvid,title:"It's Happening",play_count:300000};
  const happening=await fixture({realMedia:true,mediaFetch:async url=>url==='/songs' ? response([])
    : String(url).startsWith('/fixture/bilibili-browser?') ? response({results:[happeningVideo]}) : {ok:false,status:404}});
  happening.element('text').value="B站点歌 it's happening";happening.element('send').onclick();
  for(let i=0;i<4;i++)await happening.flush();
  const happeningAck=pendingStage('ack',happening);
  assert.equal(happeningAck.query,"it's happening");
  await deliverNarration(happeningAck,1001,'我去B站找找。',happening);
  await finishStage('ack',true,happening);
  const happeningFound=pendingStage('found',happening);
  assert.equal(happeningFound.query,"it's happening");
  assert.equal(happeningFound.bvid,happeningVideo.bvid);
  assert.equal(happeningFound.selection,'highest_play');
  assert.equal(new URL(happening.fetches.find(([url])=>String(url).startsWith('/fixture/bilibili-browser?'))[0],
    'https://fixture.test').searchParams.get('q'),"it's happening");
  await deliverNarration(happeningFound,1002,"找到了，是 It's Happening。来一起听吧。",happening);
  noFrame('the successful apostrophe query still waits after real local ended for server completion',happening);
  await finishStage('found',true,happening);
  assert.equal(new URL(happening.element('bilibili-player').children[0].src).searchParams.get('bvid'),happeningVideo.bvid);
  assert.equal(happening.sent.filter(event=>event.type==='inject').length,0);
  console.log("PASS: it's happening with a verified above-threshold result completes actual acknowledgement/introduction audio before video playback");

  let finishObsolete;
  const obsolete=await fixture({realMedia:true,mediaFetch:async url=>url==='/songs' ? response([])
    : new Promise(resolve=>{finishObsolete=resolve;})});
  obsolete.element('text').value="B站点歌 it's happening";obsolete.element('send').onclick();
  for(let i=0;i<4;i++)await obsolete.flush();
  await finishStage('ack',true,obsolete);
  obsolete.element('text').value='先继续聊天';obsolete.element('send').onclick();
  finishObsolete({ok:false,status:429,headers:{get:()=> 'application/json'},
    json:async()=>({error:'过期的限流失败',error_code:'rate_limited',retry_after:2})});
  for(let i=0;i<4;i++)await obsolete.flush();
  assert.deepEqual(obsolete.sent.filter(event=>event.type==='bilibili_narrate').map(event=>event.stage),['ack']);
  assert.equal(obsolete.read('window.bilibiliNarration.pending'),null);
  assert.equal(obsolete.read('window.bilibiliDeck.phase'),'idle');
  noFrame('an obsolete typed failure after new chat does not revive either narration or video',obsolete);
  console.log('PASS: returning to chat discards late classified search failures without a second spoken error');

  for (const media of ['bilibili','local']) for (const stage of ['ack','found']) {
    const resource=await fixture({realMedia:true,mediaFetch:async url=>url==='/songs'?response([song])
      :String(url).startsWith('/fixture/bilibili-browser?')?response({results:[video]}):{ok:false,status:404}});
    const settleResource=async()=>{for(let i=0;i<5;i++)await resource.flush();};
    resource.element('text').value=media==='local'?'点歌 '+song.title:video.bvid;
    resource.element('send').onclick();await settleResource();
    if(stage==='found')await finishStage('ack',true,resource,media);
    const blocked=pendingStage(stage,resource,media);
    const reason=stage==='ack'
      ? '后台任务服务未连接，请在启动中心启动后台任务服务后重试。'
      : 'GPU正被后台任务占用，请在启动中心等待任务结束或停止任务后重试。';
    resource.receive({type:'bilibili_narration_done',request_id:blocked.request_id,completed:false,
      error_code:'voice_resource_unavailable',reason});await settleResource();
    const status=resource.element(media==='local'?'song-status':'bilibili-status').textContent;
    assert(status.startsWith(reason),'the actual bridge must preserve the resource preflight diagnosis');
    assert.doesNotMatch(status,/重启主程序|角色语音未完成/);
    assert.equal(resource.read('window.bilibiliNarration.pending'),null);
    assert.equal(resource.read(media==='local'?'window.songDeck.phase':'window.bilibiliDeck.phase'),'awaiting-continue');
    noFrame('resource failure never bypasses narration',resource);
    assert.equal(resource.element('song-audio').playCount,0);
    if(media==='local')assert.equal(resource.element('song-audio').src,undefined);
    if(stage==='ack'&&media==='bilibili')assert.equal(resource.fetches.some(([url])=>String(url).startsWith('/fixture/bilibili-browser?')),false,
      'no browser query begins before the failed acknowledgement is deliberately skipped');
    resource.receive({type:'bilibili_narration_done',request_id:blocked.request_id,completed:true});await settleResource();
    assert.equal(resource.element(media==='local'?'song-status':'bilibili-status').textContent,status,
      'a stale success cannot erase the diagnosis or advance playback');
    const continuation=resource.read(media==='local'?'window.songDeck.continueButton':'window.bilibiliDeck.continueButton').onclick();
    await settleResource();
    if(stage==='ack'){
      pendingStage('found',resource,media);
      noFrame('manual ack continuation still requires the subsequent introduction',resource);
      assert.equal(resource.element('song-audio').playCount,0);
      await finishStage('found',true,resource,media);
    }
    await continuation;await settleResource();
    if(media==='local')assert.equal(resource.element('song-audio').playCount,1);
    else assert(resource.read('window.bilibiliDeck.frame'));
  }
  console.log('PASS: resource preflight failures preserve exact startup-center guidance for both decks and require explicit continuation');

  const resourceFallback=await fixture({realMedia:true,mediaFetch:async url=>url==='/songs'?response([]):response({results:[video]})});
  resourceFallback.element('text').value=video.bvid;resourceFallback.element('send').onclick();
  for(let i=0;i<5;i++)await resourceFallback.flush();
  const unavailable=pendingStage('ack',resourceFallback);
  resourceFallback.receive({type:'bilibili_narration_done',request_id:unavailable.request_id,completed:false,
    error_code:'voice_resource_unavailable'});
  for(let i=0;i<5;i++)await resourceFallback.flush();
  assert.match(resourceFallback.element('bilibili-status').textContent,/启动中心启动后台任务服务.*授权恢复/);
  assert.equal(resourceFallback.fetches.some(([url])=>String(url).startsWith('/fixture/bilibili-browser?')),false);
  resourceFallback.element('text').value='先继续聊天';resourceFallback.element('send').onclick();
  for(let i=0;i<5;i++)await resourceFallback.flush();
  resourceFallback.receive({type:'bilibili_narration_done',request_id:unavailable.request_id,completed:false,
    error_code:'voice_resource_unavailable',reason:'过期的错误'});
  for(let i=0;i<5;i++)await resourceFallback.flush();
  assert.doesNotMatch(resourceFallback.element('bilibili-status').textContent,/过期的错误/);
  assert.equal(resourceFallback.read('window.bilibiliDeck.phase'),'idle');
  console.log('PASS: missing resource reasons get actionable fallback while obsolete failure receipts cannot revive stopped media');


  const pageFailure=await fixture({realMedia:true,mediaFetch:async url=>url==='/songs'?response([song]):{ok:false,status:404}});
  pageFailure.read("window.bilibiliDeck.browserSearch.connected=true; window.bilibiliDeck.browserSearch.extensionVersion='1.0.1'; window.bilibiliDeck.browserSearch.search=async()=>{throw Object.assign(new Error('B站页面暂时不可用，请查看浏览器标签页。'),{code:'page_unavailable',stage:'tab_create',diagnostic_reason:'chrome_api_error'});}");
  pageFailure.element('text').value='B站点歌 fly me to the moon';pageFailure.element('send').onclick();
  for(let i=0;i<5;i++)await pageFailure.flush();
  assert.equal(pageFailure.element('song-panel').hidden,true,'remote commands do not expose an empty local music panel');
  assert.equal(pageFailure.element('song-audio').hidden,true);
  await finishStage('ack',true,pageFailure);
  const diagnosticRequest=pendingStage('error',pageFailure);
  assert.equal(diagnosticRequest.error,'page_unavailable');
  assert.match(pageFailure.element('bilibili-status').textContent,/阶段：创建B站标签页；浏览器操作失败/);
  await finishStage('error',true,pageFailure);
  assert.equal(pageFailure.read('window.bilibiliDeck.phase'),'error');
  assert.equal(pageFailure.element('song-panel').hidden,true);
  assert.equal(pageFailure.element('song-audio').hidden,true);
  assert.doesNotMatch(pageFailure.element('song-status').textContent,/正在交给/);
  pageFailure.element('text').value='点歌 '+song.title;pageFailure.element('send').onclick();
  for(let i=0;i<5;i++)await pageFailure.flush();
  assert.equal(pageFailure.element('song-panel').hidden,false);
  assert.equal(pageFailure.element('song-audio').hidden,true,'local controls remain hidden until introduction permits a real src');
  await finishStage('ack',true,pageFailure,'local');
  await finishStage('found',true,pageFailure,'local');
  assert.equal(pageFailure.element('song-audio').hidden,false);
  const localNotice=pageFailure.element('song-status').textContent;
  pageFailure.receive({type:'bilibili_narration_done',request_id:diagnosticRequest.request_id,completed:false,
    error_code:'voice_resource_unavailable',reason:'迟到的旧错误'});
  for(let i=0;i<5;i++)await pageFailure.flush();
  assert.equal(pageFailure.element('song-status').textContent,localNotice);
  assert.equal(pageFailure.element('song-audio').hidden,false);
  console.log('PASS: actual Bilibili failure leaves one diagnostic panel, no local handoff spinner/empty player, and later local playback restores controls');


  const directWithoutHelper=await fixture({realMedia:true,mediaFetch:async url=>url==='/songs'?response([]):(()=>{throw Error('BV unexpectedly searched');})()});
  directWithoutHelper.read('window.bilibiliDeck.browserSearch=null');
  directWithoutHelper.element('text').value='https://www.bilibili.com/video/'+video.bvid+'?p=4';
  directWithoutHelper.element('send').onclick();for(let i=0;i<5;i++)await directWithoutHelper.flush();
  await finishStage('ack',true,directWithoutHelper);
  const directSpeech=pendingStage('direct',directWithoutHelper);
  assert.equal(directSpeech.query,'https://www.bilibili.com/video/'+video.bvid+'?p=4');
  assert.equal(directSpeech.result_id,undefined);assert.equal(directSpeech.bvid,undefined);
  await deliverNarration(directSpeech,1301,'就放你指定的这个视频，来一起听吧。',directWithoutHelper);
  noFrame('actual direct speech ending still waits for the server completion receipt',directWithoutHelper);
  await finishStage('direct',true,directWithoutHelper);
  assert.equal(new URL(directWithoutHelper.element('bilibili-player').children[0].src).searchParams.get('p'),'4');
  assert.equal(directWithoutHelper.element('bilibili-title').textContent,'指定视频 · '+video.bvid);
  assert.equal(directWithoutHelper.fetches.some(([url])=>String(url).includes('bilibili-browser')),false);
  const oldDirectService=await fixture({realMedia:true,mediaFetch:async url=>url==='/songs'?response([]):{ok:false,status:404}});
  oldDirectService.read('window.bilibiliNarration.directAvailable=false; window.bilibiliDeck.browserSearch=null');
  oldDirectService.element('text').value=video.bvid;oldDirectService.element('send').onclick();
  for(let i=0;i<5;i++)await oldDirectService.flush();
  await finishStage('ack',true,oldDirectService);
  assert.match(oldDirectService.element('bilibili-status').textContent,/主服务尚未启用指定视频介绍/);
  assert.equal(oldDirectService.sent.some(message=>message.type==='bilibili_narrate'&&message.stage==='direct'),false);
  noFrame('an older service cannot silently skip the introduction',oldDirectService);
  await oldDirectService.read('window.bilibiliDeck.continueButton').onclick();
  assert(oldDirectService.read('window.bilibiliDeck.frame'));
  console.log('PASS: actual console direct speech gates a helper-free multipart player; old services require explicit manual continuation');

  const reviewed=await fixture({realMedia:true,mediaFetch:async url=>url==='/songs'?response([]):{ok:false,status:404}});
  reviewed.receive({type:'hello',tts:'indextts2',music_protocol:6,bilibili_direct:true,bilibili_narration:true,
    bilibili_review:true,song_narration:true,audio_owner:true,playback:{state:'idle',queued:0,paused:false}});
  reviewed.read(`window.bilibiliDeck.browserSearch.readVideo=async (query,{signal})=>({
    evidence_id:'verified-review-P2',bvid:'${video.bvid}',part:2,expires_at:Date.now()/1000+600,
    evidence_kind:'subtitle_excerpt',subtitle_status:'ready',subtitle_truncated:true,subtitle_language:'中文'});`);
  const partQuery='https://www.bilibili.com/video/'+video.bvid+'?p=2';
  reviewed.element('text').value=partQuery;reviewed.element('send').onclick();
  for(let i=0;i<5;i++)await reviewed.flush();
  await finishStage('ack',true,reviewed);await finishStage('direct',true,reviewed);
  assert.equal(reviewed.read('window.bilibiliDeck.reviewState'),'ready');
  assert.equal(reviewed.sent.some(message=>message.type==='bilibili_narrate'&&message.stage==='review'),false);
  assert.match(reviewed.read('window.bilibiliDeck.reviewStatus.textContent'),/P2.*部分字幕/);
  const reviewPromise=reviewed.read('window.bilibiliDeck.review()');
  for(let i=0;i<5;i++)await reviewed.flush();
  const reviewRequest=pendingStage('review',reviewed);
  assert.equal(reviewRequest.query,partQuery);assert.equal(reviewRequest.evidence_id,'verified-review-P2');
  for(const key of ['bvid','result_id','selection','subtitles','text'])assert.equal(reviewRequest[key],undefined,
    'client passes the server evidence token, not claimed title/transcript or search selection: '+key);
  assert.equal(reviewed.read('window.bilibiliDeck.frame'),null);
  assert.equal(await reviewed.read('window.bilibiliDeck.review()'),false);
  await deliverNarration(reviewRequest,1501,'我读到的这部分字幕，最有意思的是它留出的余地。',reviewed);
  await finishStage('review',true,reviewed);assert.equal(await reviewPromise,true);
  assert.equal(reviewed.read('window.bilibiliDeck.reviewState'),'completed');
  assert.equal(reviewed.read('window.bilibiliDeck.active'),true);
  assert.equal(reviewed.sent.filter(message=>message.type==='bilibili_playback_state').at(-1).active,true,
    'review does not release media focus or start ordinary filler');
  await reviewed.read('window.bilibiliDeck.readForReview()');
  const retryPromise=reviewed.read('window.bilibiliDeck.review()');for(let i=0;i<5;i++)await reviewed.flush();
  const rejectedReview=pendingStage('review',reviewed);
  reviewed.receive({type:'bilibili_narration_done',request_id:rejectedReview.request_id,completed:false,
    error_code:'review_unavailable',reason:'字幕快照已过期，请重新读取。'});
  for(let i=0;i<5;i++)await reviewed.flush();assert.equal(await retryPromise,false);
  assert.match(reviewed.read('window.bilibiliDeck.reviewStatus.textContent'),/字幕快照已过期/);
  const interruptedReview=reviewed.read('window.bilibiliDeck.review()');for(let i=0;i<5;i++)await reviewed.flush();
  const cancelledReview=pendingStage('review',reviewed);
  reviewed.element('text').value='我们接着聊刚才的事';reviewed.element('send').onclick();
  for(let i=0;i<5;i++)await reviewed.flush();assert.equal(await interruptedReview,false);
  assert(reviewed.sent.some(message=>message.type==='bilibili_narration_cancel'&&message.request_id===cancelledReview.request_id));
  reviewed.receive({type:'bilibili_narration_done',request_id:cancelledReview.request_id,completed:true});
  for(let i=0;i<5;i++)await reviewed.flush();
  assert.equal(reviewed.read('window.bilibiliDeck.reviewState'),'idle');assert.equal(reviewed.read('window.bilibiliDeck.frame'),null);
  console.log('PASS: actual console advertises review capability, sends only bound evidence, plays owner audio and cancels evaluation on new chat');

}
main().catch(error=>{console.error(error);process.exitCode=1;});
