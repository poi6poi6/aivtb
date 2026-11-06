import assert from 'node:assert/strict';
import {QQTransport, createHost, validBridge, validQrUrl, sendFailureStatus} from '../tools/qq-harness/transport.mjs';

for (const endpoint of ['https://127.0.0.1:80', 'http://localhost:80', 'http://127.0.0.1:65536', 'http://127.0.0.1:80/x']) {
  assert.throws(() => validBridge(endpoint, 'local-test-token-123'));
}
validBridge('http://127.0.0.1:12345', 'local-test-token-123');
assert(validQrUrl('https://q.qq.com/qqbot/openclaw/connect.html?task_id=test&source=ai-vtuber&_wv=2'));
for (const url of ['https://evil.example/qqbot/openclaw/connect.html?task_id=x',
  'https://q.qq.com.evil.example/qqbot/openclaw/connect.html?task_id=x',
  'https://user@q.qq.com/qqbot/openclaw/connect.html?task_id=x', 'javascript:alert(1)',
  'https://q.qq.com/other?task_id=x']) assert(!validQrUrl(url));
assert.equal(sendFailureStatus({name: 'ApiError', httpStatus: 400}), 'failed');
assert.equal(sendFailureStatus({name: 'ApiError', httpStatus: 503}), 'unknown');
assert.equal(sendFailureStatus(new Error('network secret')), 'unknown');

let callbacks, bindSignal, cancelled = 0, sendMode = 'ok', releaseMessage;
const calls = [], sent = [], bots = [], diagnostics = [];
let rejectReceipt = false, hold = false;
class MockBot {
  constructor(options) { this.options = options; this.handlers = {}; bots.push(this); }
  on(event, callback) { this.handlers[event] = callback; }
  start(signal) { this.signal = signal; this.handlers.ready(); return new Promise(resolve => signal.addEventListener('abort', resolve, {once: true})); }
  stop() { this.stopped = true; }
  async sendText(target, text) {
    sent.push({target, text});
    if (sendMode === 'throw') throw new Error('ambiguous private message failure');
    if (sendMode === 'malformed') return {};
    if (sendMode === 'opaque') return {id: 'ROBOT1.0_outgoing!'};
    return {id: 'receipt-from-qq'};
  }
}
const channel = new QQTransport({QQBot: MockBot, diagnostic: value => diagnostics.push(value), startQrConnect: (cb, options) => {
  callbacks = cb; bindSignal = options.signal;
  assert.equal(options.displayQrCodeToConsole, false);
  return () => { cancelled++; };
}, host: async (path, body) => {
  calls.push({path, body});
  if (path === '/message') {
    if (hold) await new Promise(resolve => { releaseMessage = resolve; });
    if (body.text === 'unpaired') return {ok: true};
    return {ok: true, reply: '同一个人格的回复', receipt_id: `r-${body.message_id.replace(/[^A-Za-z0-9_-]/g, '_')}`};
  }
  if (path === '/receipt' && rejectReceipt) throw new Error('receipt dropped');
  return {ok: true};
}});
await channel.command({id: 'bind-1', type: 'bind'});
callbacks.onQrDisplayed('https://q.qq.com/qqbot/openclaw/connect.html?task_id=test');
callbacks.onSuccess([{appId: '12345', appSecret: 'test-secret'}]);
await channel.events;
assert.deepEqual(calls.map(x => x.body.type), ['qr', 'bound']);
assert.equal(bots.length, 0, 'binding must not automatically connect');
await channel.command({id: 'connect-1', type: 'connect', credentials: {appId: '12345', appSecret: 'test-secret'}});
assert(bindSignal.aborted);
assert.equal(cancelled, 1);
await channel.events;
assert.equal(bots.length, 1);
const bot = bots[0];
assert.equal(bot.options.baseUrl, 'https://api.bot.qq.com');
const incoming = (id, content = 'hello', extra = {}) => ({kind: 'c2c', senderId: 'owner-openid', messageId: id,
  content, replyTarget: {scope: 'c2c', targetId: 'owner-openid', msgId: id}, ...extra});
const groupIncoming = (id, content = '<@!12345> hello', groupId = 'group-openid', extra = {}) => ({
  kind: 'group', rawEventType: 'GROUP_AT_MESSAGE_CREATE', senderId: 'member-openid', groupOpenid: groupId,
  messageId: id, content, replyTarget: {scope: 'group', targetId: groupId, msgId: id}, ...extra,
});
const receive = msg => bot.handlers.message({message: msg}, msg);
await receive(incoming('group', 'hello', {kind: 'group'}));
await receive(incoming('guild', 'hello', {kind: 'dm'}));
await receive(incoming('bot', 'hello', {senderIsBot: true}));
await receive(incoming('wrong-target', 'hello', {replyTarget: {scope: 'c2c', targetId: 'someone-else', msgId: 'wrong-target'}}));
await receive(incoming('too-long', '中'.repeat(4001)));
await receive(incoming('too-many-emoji', '🙂'.repeat(4001)));
assert.equal(calls.filter(x => x.path === '/message').length, 0);
await receive(incoming('pair', 'unpaired'));
assert.equal(sent.length, 0);
await receive(incoming('one'));
await receive(incoming('one'));
assert.equal(sent.length, 1, 'duplicate must not send twice');
assert.deepEqual(sent[0].target, {scope: 'c2c', targetId: 'owner-openid', msgId: 'one'});
assert.equal(calls.at(-1).body.status, 'sent');
sendMode = 'throw';
await receive(incoming('two'));
assert.equal(calls.at(-1).body.status, 'unknown');
await receive(incoming('two'));
assert.equal(sent.length, 2, 'unknown sends are not retried');
sendMode = 'malformed';
await receive(incoming('three'));
assert.equal(calls.at(-1).body.status, 'unknown');
sendMode = 'ok'; rejectReceipt = true;
await receive(incoming('four'));
await receive(incoming('four'));
assert.equal(sent.length, 4, 'lost receipt never retries original send');
rejectReceipt = false;

hold = true;
const pendingFirst = receive(incoming('first-queued'));
await new Promise(resolve => setImmediate(resolve));
const pendingSecond = receive(incoming('second-queued'));
await new Promise(resolve => setImmediate(resolve));
assert.equal(calls.filter(x => x.path === '/message' && x.body.message_id === 'second-queued').length, 0);
hold = false; releaseMessage();
await Promise.all([pendingFirst, pendingSecond]);
assert.equal(sent.length, 6);
assert.equal(channel.peers.size, 0);

hold = true;
const cancelledReply = receive(incoming('cancelled'));
await new Promise(resolve => setImmediate(resolve));
await channel.command({id: 'disconnect-1', type: 'disconnect'});
hold = false; releaseMessage();
await cancelledReply;
assert.equal(sent.length, 6, 'disconnect before model completion must not send');
assert.equal(calls.find(x => x.path === '/receipt' && x.body.receipt_id === 'r-cancelled').body.status, 'failed');
callbacks.onSuccess([{appId: '12345', appSecret: 'stale-secret'}]);
await channel.events;
assert.equal(calls.filter(x => x.path === '/events' && x.body.type === 'bound').length, 1);
await channel.command({id: 'connect-2', type: 'connect', credentials: {appId: '12345', appSecret: 'test-secret'}});
const nextBot = bots.at(-1);
await nextBot.handlers.message({}, incoming('unicode/=+boundary', '🙂'.repeat(4000)));
assert.equal(calls.filter(x => x.path === '/message' && x.body.message_id === 'unicode/=+boundary').length, 1,
  '4000 Unicode code points and the host ID alphabet must both be accepted');
sendMode = 'opaque';
await nextBot.handlers.message({}, incoming('ROBOT1.0_incoming!'));
assert.equal(calls.filter(x => x.path === '/message' && x.body.message_id === 'ROBOT1.0_incoming!').length, 1,
  'QQ opaque message IDs containing punctuation must reach the host');
assert.equal(calls.at(-1).body.status, 'sent', 'QQ opaque send receipt IDs are valid delivery confirmation');
await nextBot.handlers.message({}, incoming('bad\nmessage'));
assert.equal(diagnostics.at(-1).reason, 'message_id', 'control characters remain invalid');
await nextBot.handlers.message({}, incoming('trailing-newline\n'));
assert.equal(diagnostics.at(-1).reason, 'message_id', 'terminal newlines remain invalid in JavaScript too');
await nextBot.handlers.message({}, incoming('x'.repeat(257)));
assert.equal(diagnostics.at(-1).reason, 'message_id', 'opaque IDs remain bounded');
await nextBot.handlers.message({}, incoming('valid-message', 'sensitive-private-text', {senderId: 'peer!'}));
assert.equal(diagnostics.at(-1).reason, 'sender_id', 'peer IDs retain the narrower validation');
nextBot.handlers.rawEvent({eventType: 'unknown-private-value', data: {secret: 'sensitive-raw-data'}});
assert.deepEqual(diagnostics.at(-1), {event: 'raw_event', raw_event: 'other'});
nextBot.handlers.error(new Error('sensitive-sdk-error'));
nextBot.options.logger.error('sensitive-parser-error');
const diagnosticText = JSON.stringify(diagnostics);
for (const secret of ['sensitive-private-text', 'sensitive-raw-data', 'sensitive-sdk-error', 'sensitive-parser-error',
  'owner-openid', 'ROBOT1.0_incoming!', 'ROBOT1.0_outgoing!', 'test-secret', '同一个人格的回复', 'unknown-private-value']) {
  assert(!diagnosticText.includes(secret), 'diagnostic callback must never receive raw private values');
}
assert(diagnostics.some(value => value.event === 'inbound' && value.fields.message_valid === true));
assert(diagnostics.some(value => value.event === 'receipt_failed'));
assert(diagnostics.some(value => value.event === 'no_reply'));

const groupReceive = msg => nextBot.handlers.message({}, msg);
const beforeGroups = calls.filter(row => row.path === '/message').length;
for (const msg of [
  groupIncoming('group-plain', '<@!12345> fake mention', 'group-openid', {rawEventType: 'GROUP_MESSAGE_CREATE'}),
  groupIncoming('group-other', '<@!12345> fake mention', 'group-openid', {
    rawEventType: 'GROUP_MESSAGE_CREATE', mentions: [{is_you: false, id: '12345'}],
  }),
  groupIncoming('group-wrong-event', 'hi', 'group-openid', {rawEventType: 'C2C_MESSAGE_CREATE', mentions: [{is_you: true}]}),
  groupIncoming('group-bot', 'hi', 'group-openid', {senderIsBot: true}),
  groupIncoming('group-bad-id', 'hi', 'group-openid\n'),
  groupIncoming('group-target-mismatch', 'hi', 'group-openid', {
    replyTarget: {scope: 'group', targetId: 'other-group', msgId: 'group-target-mismatch'},
  }),
  groupIncoming('group-msg-mismatch', 'hi', 'group-openid', {
    replyTarget: {scope: 'group', targetId: 'group-openid', msgId: 'different-message'},
  }),
  groupIncoming('group-scope-mismatch', 'hi', 'group-openid', {
    replyTarget: {scope: 'c2c', targetId: 'group-openid', msgId: 'group-scope-mismatch'},
  }),
]) await groupReceive(msg);
assert.equal(calls.filter(row => row.path === '/message').length, beforeGroups,
  'only validated platform mentions may reach the group host');

await groupReceive(groupIncoming('group-one', '<@!12345> 你好 <@other-member>', 'group/=+openid', {senderName: '群友'}));
await groupReceive(groupIncoming('group-one', 'duplicate', 'group/=+openid'));
const groupForward = calls.find(row => row.path === '/message' && row.body.message_id === 'group-one').body;
assert.deepEqual(groupForward, {app_id: '12345', peer_id: 'member-openid', message_id: 'group-one',
  text: '你好 <@other-member>', kind: 'group', group_id: 'group/=+openid', mentioned: true, nickname: '群友'});
assert.equal(calls.filter(row => row.path === '/message' && row.body.message_id === 'group-one').length, 1);
assert.deepEqual(sent.at(-1).target, {scope: 'group', targetId: 'group%2F%3D%2Bopenid', msgId: 'group-one'});
await groupReceive(groupIncoming('group-structured', '<@self-openid> 嗨 <@other-member>', 'group-openid', {
  rawEventType: 'GROUP_MESSAGE_CREATE', mentions: [{is_you: true, member_openid: 'self-openid'}, {id: 'other-member'}],
}));
assert.equal(calls.find(row => row.path === '/message' && row.body.message_id === 'group-structured').body.text, '嗨 <@other-member>');
await groupReceive(groupIncoming('group-only-at', '<@12345>  '));
assert.equal(calls.find(row => row.path === '/message' && row.body.message_id === 'group-only-at').body.text, '（群友在群里@了你）');
await groupReceive(groupIncoming('group-only-event', '  '));
assert.equal(calls.find(row => row.path === '/message' && row.body.message_id === 'group-only-event').body.text, '（群友在群里@了你）');

// A member ID is never a C2C owner ID. Dedupe and ordering belong to the group.
await groupReceive(groupIncoming('scope-isolated', 'one', 'group-a', {senderId: 'owner-openid'}));
await groupReceive(groupIncoming('scope-isolated', 'two', 'group-b', {senderId: 'owner-openid'}));
await groupReceive(incoming('scope-isolated', 'private'));
assert.equal(calls.filter(row => row.path === '/message' && row.body.message_id === 'scope-isolated').length, 3);
hold = true;
const firstGroupTask = groupReceive(groupIncoming('group-queued-first', 'first', 'queue-group'));
await new Promise(resolve => setImmediate(resolve));
hold = false;
const secondGroupTask = groupReceive(groupIncoming('group-queued-second', 'second', 'queue-group', {senderId: 'other-member'}));
await groupReceive(groupIncoming('group-independent', 'other', 'independent-group'));
assert(!calls.some(row => row.path === '/message' && row.body.message_id === 'group-queued-second'),
  'different members of one group must share a queue');
releaseMessage();
await Promise.all([firstGroupTask, secondGroupTask]);
assert(calls.some(row => row.path === '/message' && row.body.message_id === 'group-queued-second'));
assert.equal(channel.peers.size, 0);
assert(diagnostics.some(row => row.reason === 'not_mentioned'));
assert(diagnostics.some(row => row.event === 'inbound' && row.kind === 'group' && row.fields.mentioned));
for (const secret of ['member-openid', 'group/=+openid', 'queue-group', '群友']) {
  assert(!JSON.stringify(diagnostics).includes(secret), 'group diagnostics must never include member or group data');
}
channel.stop();

const abort = new AbortController();
const host = createHost('http://127.0.0.1:12345', 'local-test-token-123', async (url, init) => {
  assert.equal(url, 'http://127.0.0.1:12345/commands');
  assert.equal(init.method, 'GET');
  assert.equal(init.redirect, 'error');
  assert.equal(init.headers.Authorization, 'Bearer local-test-token-123');
  return new Response(JSON.stringify({ok: true, command: null}));
});
assert.deepEqual(await host('/commands', undefined, abort.signal), {ok: true, command: null});

const wav = Buffer.alloc(48);
wav.write('RIFF', 0); wav.writeUInt32LE(40, 4); wav.write('WAVE', 8);
wav.write('fmt ', 12); wav.writeUInt32LE(16, 16); wav.writeUInt16LE(1, 20); wav.writeUInt16LE(1, 22);
wav.writeUInt32LE(24000, 24); wav.writeUInt32LE(48000, 28); wav.writeUInt16LE(2, 32);
wav.writeUInt16LE(16, 34); wav.write('data', 36); wav.writeUInt32LE(4, 40);
const audioReceipt = 'Abcd0123_-'.repeat(3) + 'AB';
assert.equal(audioReceipt.length, 32);
let audioFetches = 0;
const audioHost = createHost('http://127.0.0.1:12345', 'local-test-token-123', async (url, init) => {
  audioFetches++;
  assert.equal(url, `http://127.0.0.1:12345/audio/${audioReceipt}`);
  assert.equal(init.method, 'GET'); assert.equal(init.redirect, 'error');
  assert.equal(init.headers.Authorization, 'Bearer local-test-token-123');
  assert.equal(init.headers.Accept, 'audio/wav');
  assert(!Object.hasOwn(init.headers, 'Origin'));
  return new Response(wav, {headers: {'content-type': 'audio/wav', 'content-length': String(wav.length)}});
});
assert.deepEqual(await audioHost.audio(audioReceipt, abort.signal), wav);
for (const id of ['', '../secrets', 'a'.repeat(31), 'a'.repeat(33), 'a'.repeat(31) + '/',
  'a'.repeat(31) + '\n', 'a'.repeat(32) + '\n']) {
  await assert.rejects(audioHost.audio(id, abort.signal));
}
assert.equal(audioFetches, 1, 'invalid audio receipt must never issue a request');
for (const response of [
  new Response(wav, {status: 403, headers: {'content-type': 'audio/wav'}}),
  new Response(wav, {headers: {'content-type': 'text/plain'}}),
  new Response(wav, {headers: {'content-type': 'audio/wav', 'content-length': '3500001'}}),
  new Response(Buffer.alloc(3500001), {headers: {'content-type': 'audio/wav'}}),
  new Response('not wave data', {headers: {'content-type': 'audio/wav'}}),
]) {
  const invalidHost = createHost('http://127.0.0.1:12345', 'local-test-token-123', async () => response);
  await assert.rejects(invalidHost.audio(audioReceipt, abort.signal));
}
const alreadyAborted = AbortSignal.abort();
await assert.rejects(audioHost.audio(audioReceipt, alreadyAborted));
assert.equal(audioFetches, 1, 'already-cancelled audio must not fetch');
let cancelDownload;
const heldHost = createHost('http://127.0.0.1:12345', 'local-test-token-123', async (_url, init) => {
  cancelDownload = init.signal;
  return new Promise(() => {});
});
const downloadAbort = new AbortController();
const heldDownload = heldHost.audio(audioReceipt, downloadAbort.signal);
await new Promise(resolve => setImmediate(resolve));
downloadAbort.abort();
await assert.rejects(heldDownload);
assert(cancelDownload.aborted, 'audio download receives the cancellation signal');

async function voiceFixture(options = {}) {
  const records = [], logs = [];
  let instance;
  class VoiceBot {
    constructor() {
      this.handlers = {}; instance = this;
      this.tokenManager = {getAccessToken: async () => {
        records.push({kind: 'token'}); return options.token ? options.token() : 'private-test-token';
      }};
      this.apiClient = {request: async (token, method, path, body, requestOptions) => {
        records.push({kind: 'upload', token, method, path, body, requestOptions});
        return options.upload ? options.upload() : {file_info: 'private-upload-reference'};
      }};
      this.mediaApi = {sendMediaMessage: async (...args) => {
        records.push({kind: 'voice-send', args});
        return options.send ? options.send() : {id: 'VOICE1.0_sent!'};
      }};
    }
    on(name, callback) { this.handlers[name] = callback; }
    start(signal) { this.handlers.ready(); return new Promise(resolve => signal.addEventListener('abort', resolve, {once: true})); }
    stop() {}
    getCredentials() { return {appId: '12345', clientSecret: 'test-secret'}; }
    async sendText(...args) { records.push({kind: 'text-send', args}); return {id: 'text-result'}; }
  }
  const bridge = async (path, body) => {
    records.push({kind: 'host', path, body});
    return path === '/message' ? {ok: true, reply: '这是一句私聊回复', receipt_id: audioReceipt,
      reply_mode: options.replyMode ?? 'voice'} : {ok: true};
  };
  bridge.audio = async (receiptId, signal) => {
    records.push({kind: 'audio', receiptId});
    return options.audio ? options.audio(signal) : wav;
  };
  const transport = new QQTransport({host: bridge, QQBot: VoiceBot,
    voiceUploadTimeoutMs: options.timeoutMs ?? 20000, diagnostic: value => logs.push(value)});
  await transport.command({id: 'connect', type: 'connect', credentials: {appId: '12345', appSecret: 'test-secret'}});
  await transport.events;
  return {transport, records, logs, receive: (message = incoming('voice-incoming')) => instance.handlers.message({}, message),
    status: () => records.findLast(row => row.kind === 'host' && row.path === '/receipt')?.body.status};
}

const voice = await voiceFixture();
await voice.receive(); await voice.receive();
assert.equal(voice.status(), 'sent');
assert.equal(voice.records.filter(row => row.kind === 'voice-send').length, 1, 'voice duplicates must not send twice');
assert.equal(voice.records.filter(row => row.kind === 'text-send').length, 0);
const uploaded = voice.records.find(row => row.kind === 'upload');
assert.equal(uploaded.path, '/v2/users/owner-openid/files');
assert.equal(uploaded.method, 'POST');
assert.deepEqual(uploaded.body, {file_type: 3, file_name: 'reply.wav', file_data: wav.toString('base64'), srv_send_msg: false});
assert.deepEqual(uploaded.requestOptions.redactBodyKeys, ['file_data']);
const voiceSend = voice.records.find(row => row.kind === 'voice-send');
assert.deepEqual(voiceSend.args, ['c2c', 'owner-openid', 'private-upload-reference',
  {appId: '12345', clientSecret: 'test-secret'}, {msgId: 'voice-incoming'}]);
assert.equal(voice.logs.filter(row => row.event === 'send_attempted').length, 1);
for (const privateValue of ['private-test-token', 'test-secret', 'private-upload-reference', '这是一句私聊回复', audioReceipt]) {
  assert(!JSON.stringify(voice.logs).includes(privateValue));
}
voice.transport.stop();

const groupVoice = await voiceFixture();
await groupVoice.receive(groupIncoming('group-voice', '<@!12345> hello', 'group/=+voice'));
assert.equal(groupVoice.status(), 'sent');
assert.equal(groupVoice.records.find(row => row.kind === 'upload').path, '/v2/groups/group%2F%3D%2Bvoice/files');
assert.deepEqual(groupVoice.records.find(row => row.kind === 'voice-send').args,
  ['group', 'group%2F%3D%2Bvoice', 'private-upload-reference',
    {appId: '12345', clientSecret: 'test-secret'}, {msgId: 'group-voice'}]);
assert(!groupVoice.records.some(row => row.kind === 'text-send'));
groupVoice.transport.stop();

for (const options of [
  {audio: () => { throw new Error('private download failure'); }},
  {audio: () => Buffer.from('wrong format')},
  {upload: () => { throw new Error('private upload error'); }},
  {upload: () => ({})}, {upload: () => ({file_info: 'bad\nreference'})},
  {replyMode: 'unsupported'},
]) {
  const test = await voiceFixture(options);
  await test.receive();
  assert.equal(test.status(), 'failed');
  assert(!test.records.some(row => row.kind.endsWith('-send')), 'preparation failure must not send voice or fallback text');
  assert(!test.logs.some(row => row.event === 'send_attempted'), 'upload does not count as sending');
  test.transport.stop();
}
for (const [send, expected] of [
  [() => { throw new Error('unknown private send failure'); }, 'unknown'],
  [() => ({}), 'unknown'],
  [() => { throw {name: 'ApiError', httpStatus: 400}; }, 'failed'],
  [() => { throw {name: 'ApiError', httpStatus: 503}; }, 'unknown'],
]) {
  const test = await voiceFixture({send});
  await test.receive(); await test.receive();
  assert.equal(test.status(), expected);
  assert.equal(test.records.filter(row => row.kind === 'voice-send').length, 1);
  assert.equal(test.records.filter(row => row.kind === 'text-send').length, 0, 'ambiguous voice delivery must never fallback');
  test.transport.stop();
}

let releaseUpload;
const interrupted = await voiceFixture({upload: () => new Promise(resolve => { releaseUpload = resolve; })});
const interruptedTask = interrupted.receive();
await new Promise(resolve => setImmediate(resolve));
interrupted.transport.stop();
await interruptedTask;
releaseUpload({file_info: 'late-private-upload'});
await new Promise(resolve => setImmediate(resolve));
assert.equal(interrupted.status(), 'failed');
assert(!interrupted.records.some(row => row.kind.endsWith('-send')), 'late upload cannot send after disconnect');

let releaseTimedUpload;
const timedUpload = await voiceFixture({timeoutMs: 5, upload: () => new Promise(resolve => { releaseTimedUpload = resolve; })});
await timedUpload.receive();
assert.equal(timedUpload.status(), 'failed');
releaseTimedUpload({file_info: 'late-private-upload'});
await new Promise(resolve => setImmediate(resolve));
assert(!timedUpload.records.some(row => row.kind.endsWith('-send')), 'timed-out upload cannot later send');
timedUpload.transport.stop();

let releaseToken;
const interruptedToken = await voiceFixture({token: () => new Promise(resolve => { releaseToken = resolve; })});
const tokenTask = interruptedToken.receive();
await new Promise(resolve => setImmediate(resolve));
interruptedToken.transport.stop();
await tokenTask;
releaseToken('late-private-token');
await new Promise(resolve => setImmediate(resolve));
assert(!interruptedToken.records.some(row => ['upload', 'voice-send', 'text-send'].includes(row.kind)),
  'late token cannot start an upload after disconnect');
console.log('QQ transport: text/voice selection, bounded private WAV download, passive voice upload/send, cancellation and unknown receipts: PASS');
