// This channel owns no LLM, chat history or account credentials on disk.
// Python owns pairing, durable deduplication and the send-attempt barrier.
const quiet = Object.freeze({info() {}, error() {}, warn() {}, debug() {}});
const validId = value => typeof value === 'string' && value.length >= 1 && value.length <= 256 &&
  !/[^A-Za-z0-9_.:=+\/-]/.test(value);
// QQ message IDs are opaque protocol strings (real IDs may end in '!').
// Keep command, peer and local receipt identifiers on their narrower alphabet.
const validMessageId = value => typeof value === 'string' && value.length >= 1 && value.length <= 256
  && !/[^\x21-\x7e]/.test(value);
// Count Unicode code points to match Python len(), including supplementary emoji.
const textLength = value => Array.from(value).length;
const MAX_VOICE_BYTES = 3500000;
const MAX_IMAGE_BYTES = 5 * 1024 * 1024;
const IMAGE_MIMES = new Set(['image/png', 'image/jpeg', 'image/gif', 'image/webp']);
// Exact Tencent media hosts, not suffix matching or arbitrary model URLs.
// Tencent's current attachment examples use multimedia.nt.qq.com.cn/download.
const IMAGE_HOSTS = new Set(['multimedia.nt.qq.com.cn', 'multimedia.nt.qq.com', 'gchat.qpic.cn', 'c2cpicdw.qpic.cn']);
export function validImageUrl(value) {
  try {
    if (typeof value !== 'string' || value.length > 8192 || /[\s\\]/.test(value)) return false;
    const url = new URL(value);
    return url.protocol === 'https:' && IMAGE_HOSTS.has(url.hostname) && !url.username && !url.password &&
      (!url.port || url.port === '443') && !url.hash;
  } catch { return false; }
}
function imageMime(buffer) {
  if (!Buffer.isBuffer(buffer) || buffer.length < 12 || buffer.length > MAX_IMAGE_BYTES) return '';
  if (buffer.subarray(0, 8).equals(Buffer.from([137,80,78,71,13,10,26,10]))) return 'image/png';
  if (buffer[0] === 255 && buffer[1] === 216 && buffer.at(-2) === 255 && buffer.at(-1) === 217) return 'image/jpeg';
  if (['GIF87a', 'GIF89a'].includes(buffer.toString('ascii', 0, 6))) return 'image/gif';
  if (buffer.toString('ascii', 0, 4) === 'RIFF' && buffer.toString('ascii', 8, 12) === 'WEBP') return 'image/webp';
  return '';
}
async function readImage(response, signal) {
  if (!response.ok) { await response.body?.cancel(); throw new Error('Image download failed'); }
  const length = response.headers.get('content-length');
  if (length !== null && (!/^\d+$/.test(length) || Number(length) > MAX_IMAGE_BYTES)) {
    await response.body?.cancel(); throw new Error('Image too large');
  }
  const chunks = []; let size = 0;
  for await (const chunk of response.body) {
    size += chunk.length;
    if (signal.aborted || size > MAX_IMAGE_BYTES) throw new Error('Image download stopped');
    chunks.push(chunk);
  }
  const data = Buffer.concat(chunks), mime = imageMime(data);
  if (!mime || signal.aborted) throw new Error('Invalid image');
  return {data, mime};
}
export async function downloadQQImage(attachment, signal, fetchImpl = fetch) {
  if (!IMAGE_MIMES.has(attachment?.content_type) || !validImageUrl(attachment?.url) ||
      (attachment.size !== undefined && (!Number.isFinite(attachment.size) || attachment.size > MAX_IMAGE_BYTES))) {
    throw new Error('Invalid QQ image attachment');
  }
  return boundedStage(async boundedSignal => {
    const response = await fetchImpl(attachment.url, {method: 'GET', redirect: 'error', signal: boundedSignal,
      headers: {Accept: 'image/png,image/jpeg,image/gif,image/webp'}});
    return readImage(response, boundedSignal);
  }, signal, 10000);
}
const validAudioReceipt = value => typeof value === 'string' && value.length === 32 && /^[A-Za-z0-9_-]{32}$/.test(value);
const validWav = value => Buffer.isBuffer(value) && value.length >= 44 && value.length <= MAX_VOICE_BYTES &&
  value.toString('ascii', 0, 4) === 'RIFF' && value.toString('ascii', 8, 12) === 'WAVE';
const validFileInfo = value => typeof value === 'string' && value.length > 0 && value.length <= 65536 &&
  !/[^\x21-\x7e]/.test(value);
function wasMentioned(message) {
  return message.rawEventType === 'GROUP_AT_MESSAGE_CREATE' ||
    (message.rawEventType === 'GROUP_MESSAGE_CREATE' && Array.isArray(message.mentions) &&
      message.mentions.some(mention => mention?.is_you === true));
}
function groupText(message, appId) {
  // Platform metadata grants mention status; content never does. Remove only
  // this bot's known markers so another member's mention keeps its meaning.
  const selfIds = new Set([appId]);
  for (const mention of Array.isArray(message.mentions) ? message.mentions : []) {
    if (mention?.is_you !== true) continue;
    for (const id of [mention.id, mention.member_openid, mention.user_openid]) {
      if (validId(id)) selfIds.add(id);
    }
  }
  const text = message.content.replace(/<@!?([^<>\s]+)>/g,
    (marker, id) => selfIds.has(id) ? '' : marker).trim();
  return text || '（群友在群里@了你）';
}
async function boundedStage(operation, signal, timeoutMs) {
  const controller = new AbortController();
  const combined = AbortSignal.any([signal, controller.signal]);
  let onAbort;
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    if (combined.aborted) throw new Error('Voice stage cancelled');
    const aborted = new Promise((_, reject) => {
      onAbort = () => reject(new Error('Voice stage cancelled'));
      combined.addEventListener('abort', onAbort, {once: true});
    });
    return await Promise.race([Promise.resolve().then(() => operation(combined)), aborted]);
  } finally {
    clearTimeout(timer);
    if (onAbort) combined.removeEventListener('abort', onAbort);
  }
}
export function validBridge(endpoint, token) {
  if (!/^http:\/\/127\.0\.0\.1:[1-9]\d{0,4}$/.test(endpoint || '') ||
      Number(endpoint.split(':').at(-1)) > 65535 || typeof token !== 'string' ||
      !/^[A-Za-z0-9_-]{16,256}$/.test(token)) throw new Error('Invalid local bridge');
}
export function validQrUrl(value) {
  try {
    const url = new URL(value);
    return typeof value === 'string' && value.length <= 4096 && !/\s/.test(value) && url.origin === 'https://q.qq.com' &&
      !url.username && !url.password && !url.hash && url.pathname === '/qqbot/openclaw/connect.html' &&
      !!url.searchParams.get('task_id');
  } catch { return false; }
}
export function sendFailureStatus(error) {
  // A network exception, timeout, 5xx, or malformed success is ambiguous.
  return error?.name === 'ApiError' && error.httpStatus >= 400 && error.httpStatus < 500 ? 'failed' : 'unknown';
}
export function createHost(endpoint, token, fetchImpl = fetch) {
  validBridge(endpoint, token);
  const call = async (path, body, signal, timeoutMs = 35000) => {
    const response = await fetchImpl(endpoint + path, {
      method: body === undefined ? 'GET' : 'POST', redirect: 'error',
      signal: AbortSignal.any([signal, AbortSignal.timeout(timeoutMs)]),
      headers: {Authorization: `Bearer ${token}`, 'Content-Type': 'application/json'},
      ...(body === undefined ? {} : {body: JSON.stringify(body)}),
    });
    if (!response.ok) throw new Error('Bridge rejected request');
    const chunks = []; let length = 0;
    for await (const chunk of response.body) {
      length += chunk.length;
      if (length > 65536) throw new Error('Bridge response too large');
      chunks.push(chunk);
    }
    const value = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    if (!value || value.ok !== true) throw new Error('Bridge rejected request');
    return value;
  };
  call.audio = async (receiptId, signal) => {
    if (!validAudioReceipt(receiptId)) throw new Error('Invalid voice receipt');
    return boundedStage(async boundedSignal => {
      if (boundedSignal.aborted) throw new Error('Voice download cancelled');
      const response = await fetchImpl(`${endpoint}/audio/${receiptId}`, {
        method: 'GET', redirect: 'error', signal: boundedSignal,
        headers: {Authorization: `Bearer ${token}`, Accept: 'audio/wav'},
      });
      if (!response.ok || response.headers.get('content-type')?.split(';')[0].trim().toLowerCase() !== 'audio/wav') {
        await response.body?.cancel();
        throw new Error('Bridge rejected voice download');
      }
      const length = response.headers.get('content-length');
      if (length !== null && (!/^\d+$/.test(length) || Number(length) > MAX_VOICE_BYTES)) {
        await response.body?.cancel();
        throw new Error('Bridge voice too large');
      }
      const chunks = []; let size = 0;
      for await (const chunk of response.body) {
        size += chunk.length;
        if (boundedSignal.aborted || size > MAX_VOICE_BYTES) throw new Error('Bridge voice download stopped');
        chunks.push(chunk);
      }
      const buffer = Buffer.concat(chunks);
      if (boundedSignal.aborted || !validWav(buffer)) throw new Error('Bridge voice is invalid');
      return buffer;
    }, signal, 10000);
  };
  call.image = async (receiptId, signal) => {
    if (!validAudioReceipt(receiptId)) throw new Error('Invalid image receipt');
    return boundedStage(async boundedSignal => readImage(await fetchImpl(`${endpoint}/image/${receiptId}`, {
      method: 'GET', redirect: 'error', signal: boundedSignal,
      headers: {Authorization: `Bearer ${token}`, Accept: 'image/png,image/jpeg,image/gif,image/webp'},
    }), boundedSignal), signal, 10000);
  };
  return call;
}
export class QQTransport {
  constructor({host, QQBot, startQrConnect, maxQueued = 32, maxPerPeer = 8,
    voiceUploadTimeoutMs = 20000, diagnostic = () => {}, fetchImage = fetch}) {
    Object.assign(this, {host, QQBot, startQrConnect, maxQueued, maxPerPeer});
    this.voiceUploadTimeoutMs = voiceUploadTimeoutMs;
    this.diagnostic = diagnostic;
    this.fetchImage = fetchImage;
    this.lifetime = new AbortController();
    this.connection = new AbortController();
    this.generation = 0;
    this.pending = 0;
    this.peers = new Map();
    this.seen = new Set();
    this.commands = new Set();
    this.events = Promise.resolve();
  }
  report(value) { try { this.diagnostic(value); } catch {} }
  event(value, generation = this.generation) {
    // Serialize QR/bound events so stale UI state cannot overwrite a binding.
    const task = this.events.catch(() => {}).then(async () => {
      if (this.lifetime.signal.aborted || generation !== this.generation) return;
      try { await this.host('/events', value, this.lifetime.signal); }
      catch (error) { this.report({event: 'bridge_failed', reason: 'events'}); throw error; }
    });
    this.events = task;
    return task;
  }
  safeEvent(value, generation = this.generation) { void this.event(value, generation).catch(() => {}); }
  async run() {
    while (!this.lifetime.signal.aborted) {
      try {
        const result = await this.host('/commands', undefined, this.lifetime.signal);
        if (result.command) await this.command(result.command);
        else await this.delay(100);
      } catch {
        if (!this.lifetime.signal.aborted) {
          this.report({event: 'bridge_failed', reason: 'commands'});
          await this.delay(1000);
        }
      }
    }
  }
  delay(ms) {
    return new Promise(resolve => {
      if (this.lifetime.signal.aborted) return resolve();
      const done = () => { clearTimeout(timer); this.lifetime.signal.removeEventListener('abort', done); resolve(); };
      const timer = setTimeout(done, ms);
      this.lifetime.signal.addEventListener('abort', done, {once: true});
    });
  }
  reset() {
    this.generation += 1;
    this.connection.abort();
    this.cancelBind?.(); this.cancelBind = undefined;
    this.bot?.stop(); this.bot = undefined;
    this.connection = new AbortController();
  }
  stop() { this.lifetime.abort(); this.reset(); this.report({event: 'stopped'}); }
  async command(command) {
    if (this.lifetime.signal.aborted || !validId(command?.id) || this.commands.has(command.id)) return;
    this.commands.add(command.id);
    if (this.commands.size > 2048) this.commands.delete(this.commands.values().next().value);
    if (!['bind', 'connect', 'disconnect'].includes(command.type)) {
      return this.safeEvent({type: 'error', code: 'invalid_command'});
    }
    this.reset();
    const generation = this.generation;
    const signal = AbortSignal.any([this.connection.signal, this.lifetime.signal]);
    if (command.type === 'disconnect') {
      this.report({event: 'disconnected'});
      return this.safeEvent({type: 'disconnected'});
    }
    if (command.type === 'bind') {
      this.report({event: 'binding'});
      try {
        this.cancelBind = this.startQrConnect({
          onQrDisplayed: url => {
            if (signal.aborted) return;
            if (validQrUrl(url)) this.safeEvent({type: 'qr', url}, generation);
            else { this.connection.abort(); this.safeEvent({type: 'error', code: 'invalid_qr'}, generation); }
          },
          onSuccess: credentials => {
            if (signal.aborted) return;
            const item = credentials?.[0];
            if (!this.validCredentials(item)) return this.safeEvent({type: 'error', code: 'invalid_credentials'}, generation);
            // Host must persist credentials before it sends a separate connect command.
            this.safeEvent({type: 'bound', app_id: item.appId, app_secret: item.appSecret}, generation);
          },
          onFailure: () => { if (!signal.aborted) this.safeEvent({type: 'error', code: 'bind_failed'}, generation); },
        }, {displayQrCodeToConsole: false, source: 'ai-vtuber', signal});
      } catch { this.safeEvent({type: 'error', code: 'bind_failed'}, generation); }
      return;
    }
    if (!this.validCredentials(command.credentials)) return this.safeEvent({type: 'error', code: 'invalid_credentials'});
    const {appId, appSecret} = command.credentials;
    this.report({event: 'connecting'});
    let bot;
    try {
      // QQ combines private/group events in one intent; other intents are unnecessary.
      bot = new this.QQBot({appId, appSecret, transport: 'websocket', intents: 1 << 25, markdownSupport: false,
        baseUrl: 'https://api.bot.qq.com', tokenBaseUrl: 'https://api.bot.qq.com',
        logger: {...quiet, error: () => {
          if (!signal.aborted) this.report({event: 'sdk_error', reason: 'sdk_log'});
        }}});
      this.bot = bot;
      const connected = event => {
        if (!signal.aborted) { this.report({event}); this.safeEvent({type: 'connected', app_id: appId}, generation); }
      };
      bot.on('ready', () => connected('ready'));
      bot.on('resumed', () => connected('resumed'));
      bot.on('disconnected', () => {
        if (!signal.aborted) { this.report({event: 'disconnected'}); this.safeEvent({type: 'disconnected'}, generation); }
      });
      bot.on('error', () => {
        if (!signal.aborted) { this.report({event: 'sdk_error', reason: 'gateway_error'}); this.safeEvent({type: 'error', code: 'gateway_error'}, generation); }
      });
      bot.on('rawEvent', ctx => {
        if (!signal.aborted) {
          const allowed = ['FRIEND_ADD', 'FRIEND_DEL', 'C2C_MSG_REJECT', 'C2C_MSG_RECEIVE',
            'GROUP_ADD_ROBOT', 'GROUP_DEL_ROBOT', 'GROUP_MSG_REJECT', 'GROUP_MSG_RECEIVE'];
          this.report({event: 'raw_event', raw_event: allowed.includes(ctx?.eventType) ? ctx.eventType : 'other'});
        }
      });
      bot.on('message', (ctx, message) => this.inbound(bot, appId, message ?? ctx?.message, signal));
      void Promise.resolve(bot.start(signal)).catch(() => {
        if (!signal.aborted) {
          this.report({event: 'sdk_error', reason: 'start_failed'});
          this.safeEvent({type: 'error', code: 'connect_failed'}, generation);
        }
      });
    } catch {
      this.report({event: 'sdk_error', reason: 'start_failed'});
      bot?.stop(); this.safeEvent({type: 'error', code: 'connect_failed'}, generation);
    }
  }
  validCredentials(value) {
    return value && typeof value.appId === 'string' && /^\d{1,32}$/.test(value.appId) &&
      typeof value.appSecret === 'string' && value.appSecret.length >= 8 && value.appSecret.length <= 512 &&
      !/[\r\n\0]/.test(value.appSecret);
  }
  async sendVoice(bot, target, receiptId, signal) {
    let upload;
    try {
      const audio = await this.host.audio(receiptId, signal);
      if (signal.aborted || !validWav(audio)) return 'failed';
      upload = await boundedStage(async boundedSignal => {
        const credentials = bot.getCredentials();
        if (boundedSignal.aborted) throw new Error('Voice upload cancelled');
        const token = await bot.tokenManager.getAccessToken(credentials.appId, credentials.clientSecret);
        if (boundedSignal.aborted) throw new Error('Voice upload cancelled');
        // SDK 1.0.5 omits file_name for VOICE in its media wrapper. The current
        // platform requires it for file_data, so use the public protocol client.
        // One upload only; srv_send_msg:false prevents any user-visible send.
        const collection = target.scope === 'group' ? 'groups' : 'users';
        return bot.apiClient.request(token, 'POST', `/v2/${collection}/${encodeURIComponent(target.targetId)}/files`, {
          file_type: 3, file_name: 'reply.wav', file_data: audio.toString('base64'), srv_send_msg: false,
        }, {uploadRequest: true, timeoutMs: this.voiceUploadTimeoutMs, redactBodyKeys: ['file_data']});
      }, signal, this.voiceUploadTimeoutMs);
      if (signal.aborted || !validFileInfo(upload?.file_info)) return 'failed';
    } catch {
      this.report({event: 'voice_failed', reason: 'prepare'});
      return 'failed';
    }
    if (signal.aborted) return 'failed';
    try {
      this.report({event: 'send_attempted'});
      // This is the only user-visible POST. Never fall back to text or retry an
      // ambiguous result; the host commits history only after a sent receipt.
      const result = await bot.mediaApi.sendMediaMessage(
        target.scope, encodeURIComponent(target.targetId), upload.file_info, bot.getCredentials(), {msgId: target.msgId});
      return validMessageId(result?.id) ? 'sent' : 'unknown';
    } catch (error) { return sendFailureStatus(error); }
  }
  async sendImage(bot, target, receiptId, content, signal) {
    let upload;
    try {
      const image = await this.host.image(receiptId, signal);
      if (signal.aborted || !imageMime(image?.data)) return 'upload_failed';
      upload = await boundedStage(async boundedSignal => {
        const credentials = bot.getCredentials();
        const token = await bot.tokenManager.getAccessToken(credentials.appId, credentials.clientSecret);
        if (boundedSignal.aborted) throw new Error('Image upload cancelled');
        const collection = target.scope === 'group' ? 'groups' : 'users';
        const extension = {'image/png':'png','image/jpeg':'jpg','image/gif':'gif','image/webp':'webp'}[imageMime(image.data)];
        return bot.apiClient.request(token, 'POST', `/v2/${collection}/${encodeURIComponent(target.targetId)}/files`, {
          file_type: 1, file_name: `reply.${extension}`, file_data: image.data.toString('base64'), srv_send_msg: false,
        }, {uploadRequest: true, timeoutMs: this.voiceUploadTimeoutMs, redactBodyKeys: ['file_data']});
      }, signal, this.voiceUploadTimeoutMs);
      if (signal.aborted || !validFileInfo(upload?.file_info)) return 'upload_failed';
    } catch { this.report({event: 'image_failed', reason: 'prepare'}); return 'upload_failed'; }
    if (signal.aborted) return 'failed';
    try {
      this.report({event: 'send_attempted'});
      const result = await bot.mediaApi.sendMediaMessage(target.scope, encodeURIComponent(target.targetId),
        upload.file_info, bot.getCredentials(), {msgId: target.msgId, content});
      return validMessageId(result?.id) ? 'sent' : 'unknown';
    } catch (error) { return sendFailureStatus(error); }
  }
  async inbound(bot, appId, message, signal) {
    const target = message?.replyTarget;
    const isGroup = message?.kind === 'group';
    const scope = isGroup ? 'group' : 'c2c';
    const targetId = isGroup ? message?.groupOpenid : message?.senderId;
    const mentioned = isGroup && wasMentioned(message);
    const fields = {
      sender_type: typeof message?.senderId, message_type: typeof message?.messageId, content_type: typeof message?.content,
      sender_length: typeof message?.senderId === 'string' ? Math.min(message.senderId.length, 65536) : 0,
      message_length: typeof message?.messageId === 'string' ? Math.min(message.messageId.length, 65536) : 0,
      content_length: typeof message?.content === 'string' ? Math.min(textLength(message.content), 65536) : 0,
      sender_valid: validId(message?.senderId), message_valid: validMessageId(message?.messageId),
      target_matches: !!target && target.targetId === targetId,
      msg_id_matches: !!target && target.msgId === message?.messageId,
      group_valid: !isGroup || validId(message?.groupOpenid),
      mentioned,
      sender_is_bot: !!message?.senderIsBot,
      has_attachments: Array.isArray(message?.attachments) && message.attachments.length > 0,
      has_elements: Array.isArray(message?.msgElements) && message.msgElements.length > 0,
    };
    this.report({event: 'inbound', kind: ['c2c', 'group', 'guild', 'dm'].includes(message?.kind) ? message.kind : 'other', fields});
    const reject = reason => this.report({event: 'rejected', reason});
    if (signal.aborted) return reject('aborted');
    if (!['c2c', 'group'].includes(message?.kind) || target?.scope !== scope) return reject('non_chat');
    if (message.senderIsBot) return reject('bot_sender');
    if (!fields.sender_valid) return reject('sender_id');
    if (!fields.message_valid) return reject('message_id');
    if (!fields.group_valid) return reject('group_id');
    if (!fields.target_matches || !fields.msg_id_matches) return reject('target_mismatch');
    if (isGroup && !mentioned) return reject('not_mentioned');
    if (typeof message.content !== 'string') return reject('content_type');
    const imageAttachment = Array.isArray(message.attachments)
      ? message.attachments.find(item => IMAGE_MIMES.has(item?.content_type)) : undefined;
    if (!isGroup && !message.content.trim() && !imageAttachment) return reject('empty_content');
    if (fields.content_length > 4000) return reject('content_length');
    const text = isGroup ? groupText(message, appId) : message.content.trim() || '（用户发送了一张图片）';
    const key = JSON.stringify([appId, scope, targetId]);
    const identity = JSON.stringify([appId, scope, targetId, message.messageId]);
    if (this.seen.has(identity)) return reject('duplicate');
    const previous = this.peers.get(key);
    if (this.pending >= this.maxQueued || (previous?.count ?? 0) >= this.maxPerPeer) return reject('queue_full');
    this.seen.add(identity);
    if (this.seen.size > 4096) this.seen.delete(this.seen.values().next().value);
    this.pending += 1;
    const entry = {count: (previous?.count ?? 0) + 1};
    entry.task = (previous?.task ?? Promise.resolve()).catch(() => {}).then(async () => {
      if (signal.aborted) return reject('aborted');
      this.report({event: 'forwarded'});
      let answer;
      try {
        let attachments, imageUnavailable = false;
        if (imageAttachment) {
          try {
            const image = await downloadQQImage(imageAttachment, signal, this.fetchImage);
            attachments = [{content_type: image.mime, data_base64: image.data.toString('base64')}];
          } catch { imageUnavailable = true; this.report({event: 'image_failed', reason: 'download'}); }
        }
        if (signal.aborted) return reject('aborted');
        answer = await this.host('/message', {app_id: appId, peer_id: message.senderId,
          message_id: message.messageId, text,
          ...(attachments ? {attachments} : {}), ...(imageUnavailable ? {image_unavailable: true} : {}),
          ...(isGroup ? {kind: 'group', group_id: targetId, mentioned: true} : {}),
          ...(typeof message.senderName === 'string' ? {nickname: message.senderName.slice(0, 128)} : {})}, signal, imageAttachment ? 300000 : 180000);
      } catch (error) { this.report({event: 'bridge_failed', reason: 'message'}); throw error; }
      if (typeof answer.reply !== 'string' || !answer.reply.trim() || !validId(answer.receipt_id)) {
        this.report({event: 'no_reply', reason: answer.reply === undefined ? 'host_no_reply' : 'host_reply_invalid'});
        return;
      }
      let status = 'failed';
      const replyMode = answer.reply_mode === undefined ? 'text' : answer.reply_mode;
      if (!['text', 'voice', 'image'].includes(replyMode)) {
        this.report({event: 'no_reply', reason: 'host_reply_mode'});
      } else if (!signal.aborted && textLength(answer.reply) <= 4000 && replyMode === 'voice') {
        status = await this.sendVoice(bot,
          {scope, targetId, msgId: message.messageId}, answer.receipt_id, signal);
      } else if (!signal.aborted && textLength(answer.reply) <= 4000 && replyMode === 'image') {
        status = await this.sendImage(bot, {scope, targetId, msgId: message.messageId}, answer.receipt_id, answer.reply, signal);
        // An upload does not publish anything. Only this pre-send failure may
        // fall back to one text POST; ambiguous media sends never do.
        if (status === 'upload_failed') {
          status = 'failed';
          if (!signal.aborted) {
            try {
              this.report({event: 'send_attempted'});
              const result = await bot.sendText({scope, targetId: encodeURIComponent(targetId), msgId: message.messageId}, answer.reply);
              status = validMessageId(result?.id) ? 'sent' : 'unknown';
            } catch (error) { status = sendFailureStatus(error); }
          }
        }
      } else if (!signal.aborted && textLength(answer.reply) <= 4000) {
        try {
          this.report({event: 'send_attempted'});
          // SDK 1.0.5 sendText -> sendAndNotify -> ApiClient.request performs one POST,
          // without automatic retry. Never retry an unknown send or use proactive fallback.
          const result = await bot.sendText({scope, targetId: encodeURIComponent(targetId), msgId: message.messageId}, answer.reply);
          status = validMessageId(result?.id) ? 'sent' : 'unknown';
        } catch (error) { status = sendFailureStatus(error); }
      }
      this.report({event: 'send_result', reason: status});
      // Failure to record this receipt must never cause a second send.
      try {
        await this.host('/receipt', {receipt_id: answer.receipt_id, status}, this.lifetime.signal);
        this.report({event: 'receipt_recorded', reason: status});
      } catch (error) { this.report({event: 'receipt_failed', reason: 'receipt'}); throw error; }
    }).catch(() => {}).finally(() => {
      this.pending -= 1;
      const current = this.peers.get(key);
      if (current) current.count -= 1;
      if (current === entry) this.peers.delete(key);
    });
    this.peers.set(key, entry);
    return entry.task;
  }
}
