// Validate the pinned official sender against fake fetch; no socket/login is opened.
import assert from 'node:assert/strict';
import {QQBot} from '../tools/qq-harness/node_modules/@tencent-connect/qqbot-nodejs/dist/index.js';
import {dispatchEvent} from '../tools/qq-harness/node_modules/@tencent-connect/qqbot-nodejs/dist/protocol/gateway/event-dispatcher.js';
import {QQTransport, sendFailureStatus} from '../tools/qq-harness/transport.mjs';
const originalFetch = globalThis.fetch;
const requests = [];
let fail = false, failUpload = false;
let targetRoot = 'https://api.bot.qq.com/v2/users/test-peer';
globalThis.fetch = async (url, init) => {
  requests.push({url, init});
  if (url.endsWith('/app/getAppAccessToken')) {
    return Response.json({access_token: 'offline-fake-token', expires_in: 7200});
  }
  if (url.endsWith('/files')) {
    assert.equal(url, `${targetRoot}/files`);
    assert.equal(init.method, 'POST');
    if (failUpload) throw new Error('simulated upload failure');
    return Response.json({file_info: 'offline-file-info', file_uuid: 'offline-file-uuid', ttl: 300});
  }
  assert.equal(url, `${targetRoot}/messages`);
  assert.equal(init.method, 'POST');
  if (fail) throw new Error('simulated unknown network send');
  return Response.json({id: 'offline-send-receipt', timestamp: '2026-10-23T00:00:00Z'});
};
const bot = new QQBot({appId: '123', appSecret: 'offline-test-secret',
  baseUrl: 'https://api.bot.qq.com', tokenBaseUrl: 'https://api.bot.qq.com', markdownSupport: false});
try {
  let observed;
  bot.on('message', (ctx, message) => { observed = {ctx, message}; });
  // Exercise the pinned SDK's own inbound augmentation, without opening transport.
  await bot.handleInboundMessage({kind: 'c2c', rawEventType: 'C2C_MESSAGE_CREATE', senderId: 'test-peer',
    messageId: 'incoming-one', content: 'offline incoming text', timestamp: '2026-10-23T00:00:00Z', raw: {}});
  assert.equal(observed.ctx.message, observed.message);
  assert.deepEqual(observed.message.replyTarget, {scope: 'c2c', targetId: 'test-peer', msgId: 'incoming-one'});
  const result = await bot.sendText({scope: 'c2c', targetId: 'test-peer', msgId: 'incoming-one'}, 'test reply');
  assert.equal(result.id, 'offline-send-receipt');
  assert.equal(requests.length, 2);
  const {msg_seq, ...payload} = JSON.parse(requests[1].init.body);
  assert(Number.isSafeInteger(msg_seq) && msg_seq >= 0 && msg_seq <= 65535);
  assert.deepEqual(payload, {content: 'test reply', msg_type: 0, msg_id: 'incoming-one'});
  fail = true;
  await assert.rejects(bot.sendText({scope: 'c2c', targetId: 'test-peer', msgId: 'incoming-two'}, 'test reply'), error => {
    assert.equal(sendFailureStatus(error), 'unknown'); return true;
  });
  assert.equal(requests.length, 3, 'official SDK must perform one send attempt on network failure');
  fail = false;
  const wav = Buffer.alloc(48);
  wav.write('RIFF', 0); wav.writeUInt32LE(40, 4); wav.write('WAVE', 8);
  wav.write('fmt ', 12); wav.writeUInt32LE(16, 16); wav.writeUInt16LE(1, 20); wav.writeUInt16LE(1, 22);
  wav.writeUInt32LE(24000, 24); wav.writeUInt32LE(48000, 28); wav.writeUInt16LE(2, 32);
  wav.writeUInt16LE(16, 34); wav.write('data', 36); wav.writeUInt32LE(4, 40);
  const host = async () => ({ok: true});
  host.audio = async () => wav;
  const transport = new QQTransport({host});
  const target = {scope: 'c2c', targetId: 'test-peer', msgId: 'voice-incoming!'};
  assert.equal(await transport.sendVoice(bot, target, 'A'.repeat(32), transport.lifetime.signal), 'sent');
  assert.equal(requests.length, 5, 'voice must do one upload and one media send using the cached SDK token');
  const uploadBody = JSON.parse(requests[3].init.body);
  assert.deepEqual(uploadBody, {file_type: 3, file_name: 'reply.wav', file_data: wav.toString('base64'), srv_send_msg: false});
  const voiceBody = JSON.parse(requests[4].init.body);
  assert.equal(voiceBody.msg_type, 7);
  assert.deepEqual(voiceBody.media, {file_info: 'offline-file-info'});
  assert.equal(voiceBody.msg_id, 'voice-incoming!');
  assert(Number.isSafeInteger(voiceBody.msg_seq) && voiceBody.msg_seq >= 0 && voiceBody.msg_seq <= 65535);
  assert(!Object.hasOwn(voiceBody, 'content'), 'voice mode sends no text copy');
  fail = true;
  assert.equal(await transport.sendVoice(bot, target, 'B'.repeat(32), transport.lifetime.signal), 'unknown');
  assert.equal(requests.length, 7, 'unknown voice delivery must not retry or send fallback text');
  fail = false; failUpload = true;
  assert.equal(await transport.sendVoice(bot, target, 'C'.repeat(32), transport.lifetime.signal), 'failed');
  assert.equal(requests.length, 8, 'failed upload is one attempt and must never proceed to media send');
  transport.stop();
  failUpload = false;
  targetRoot = 'https://api.bot.qq.com/v2/groups/test%2Fgroup%3D%2B';
  let replyMode = 'text';
  const hostCalls = [];
  const groupHost = async (path, body) => {
    hostCalls.push({path, body});
    return path === '/message'
      ? {ok: true, reply: 'group reply', receipt_id: 'D'.repeat(32), reply_mode: replyMode}
      : {ok: true};
  };
  groupHost.audio = async () => wav;
  const groupTransport = new QQTransport({host: groupHost});
  const groupRaw = {id: 'group-text!', content: '<@123> hello', timestamp: '2026-10-23T00:00:00Z',
    author: {member_openid: 'test-member', username: 'offline group member', bot: false},
    group_openid: 'test/group=+', mentions: [{id: '123', is_you: true}],
  };
  const mapped = dispatchEvent('GROUP_AT_MESSAGE_CREATE', groupRaw, '123', {});
  assert.equal(mapped.action, 'message');
  assert.equal(mapped.msg.kind, 'group');
  assert.equal(mapped.msg.senderId, 'test-member');
  assert.equal(mapped.msg.groupOpenid, 'test/group=+');
  assert.equal(mapped.msg.rawEventType, 'GROUP_AT_MESSAGE_CREATE');
  assert.deepEqual(mapped.msg.mentions, [{id: '123', is_you: true}]);
  await bot.handleInboundMessage(mapped.msg);
  assert.deepEqual(observed.message.replyTarget, {scope: 'group', targetId: 'test/group=+', msgId: 'group-text!'});
  await groupTransport.inbound(bot, '123', observed.message, groupTransport.lifetime.signal);
  assert.equal(requests.length, 9);
  const groupTextBody = JSON.parse(requests[8].init.body);
  assert.equal(groupTextBody.msg_type, 0);
  assert.equal(groupTextBody.msg_id, 'group-text!');
  assert.equal(groupTextBody.content, 'group reply');
  assert.deepEqual(hostCalls[0], {path: '/message', body: {app_id: '123', peer_id: 'test-member',
    group_id: 'test/group=+', kind: 'group', mentioned: true, message_id: 'group-text!', text: 'hello', nickname: 'offline group member'}});
  assert.equal(hostCalls.at(-1).body.status, 'sent');

  replyMode = 'voice';
  const voiceMapped = dispatchEvent('GROUP_AT_MESSAGE_CREATE', {...groupRaw, id: 'group-voice!'}, '123', {});
  await bot.handleInboundMessage(voiceMapped.msg);
  await groupTransport.inbound(bot, '123', observed.message, groupTransport.lifetime.signal);
  assert.equal(requests.length, 11, 'group voice uses one group upload and one group message');
  const groupUploadBody = JSON.parse(requests[9].init.body);
  assert.equal(groupUploadBody.file_name, 'reply.wav');
  assert.equal(groupUploadBody.file_type, 3);
  assert.equal(groupUploadBody.srv_send_msg, false);
  const groupVoiceBody = JSON.parse(requests[10].init.body);
  assert.equal(groupVoiceBody.msg_type, 7);
  assert.equal(groupVoiceBody.msg_id, 'group-voice!');
  assert.deepEqual(groupVoiceBody.media, {file_info: 'offline-file-info'});
  assert(!Object.hasOwn(groupVoiceBody, 'content'));

  const beforeIgnored = hostCalls.length;
  const plainMapped = dispatchEvent('GROUP_MESSAGE_CREATE', {...groupRaw, id: 'plain-group!', mentions: []}, '123', {});
  await bot.handleInboundMessage(plainMapped.msg);
  await groupTransport.inbound(bot, '123', observed.message, groupTransport.lifetime.signal);
  assert.equal(hostCalls.length, beforeIgnored, 'a literal bot marker without a platform mention must never reach the model');
  assert.equal(requests.length, 11);
  groupTransport.stop();
  console.log('Pinned Tencent SDK: C2C/group event identity, mention filtering, encoded passive text/voice routes, no send retry: PASS');
} finally { bot.stop(); globalThis.fetch = originalFetch; }
