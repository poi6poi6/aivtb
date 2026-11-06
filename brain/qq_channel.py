"""Local QQ owner pairing, isolated chat and at-most-once reply delivery.

The dsh channel owns QQ transport only. Credentials never enter the persona,
public WebSocket events, or the status projection. Nothing connects on import.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import time
from collections import OrderedDict
from urllib.parse import urlsplit

from aiohttp import web

from .deepseek_harness import HarnessRunner
from .private_chat import PrivateChatError, PrivateChatSession, _has_reply_scaffolding
from .qq_group_chat import GroupChatSession
from .qq_voice import QQVoiceError
from .conversation import memory_change_owner
from .viewer_identity import viewer_identity
from .meme_library import MAX_IMAGE_BYTES, image_info


ERRORS = {
    'not_ready': 'QQ 通道依赖尚未就绪。',
    'model_unavailable': '当前主人格模型尚未就绪。',
    'storage_failed': 'QQ 本地记录保存失败，通道已暂停。',
    'invalid_state': 'QQ 本地记录异常，请检查后再连接。',
    'runtime_failed': 'QQ 通道启动或运行失败，请重新连接。',
    'not_bound': '请先扫码绑定 QQ 机器人。',
    'not_connected': '请先连接 QQ 机器人。',
    'busy': '请等当前回复处理完成后再操作。',
    'owner_bound': '已有配对账号，请先解除配对。',
    'invalid_name': '请填写与主聊天页面一致的名字（1—40 字）。',
    'invalid_action': '不支持的 QQ 操作。',
    'reply_failed': '这次回复没有完成，请稍后再试。',
    'model_timeout': '这次回复等待超时了，请稍后再试。',
    'model_connection': '暂时连不上回复服务，请稍后再试。',
    'model_access': '回复服务的授权暂时不可用，请在本机检查模型配置。',
    'model_quota': '回复服务额度不足或请求过于频繁，请稍后再试。',
    'model_response': '这次没有生成完整可发送的回复，请稍后再试。',
    'delivery_unknown': '上一条回复的发送结果未确认，未自动重发。',
    'history_full': 'QQ 消息记录已达到容量，请稍后再试。',
    'voice_unavailable': '语音服务尚未就绪，暂时可用文字回复。',
    'voice_fallback': '这条回复未能生成合适的语音，已改用文字回复。',
}
VOICE_FALLBACK_ERRORS = {
    'voice_text_too_long': '回复超过单条语音的 300 字符上限，已改用文字回复。',
    'voice_invalid_text': '回复内容无法用于语音合成，已改用文字回复。',
    'voice_timeout': '语音合成等待超时，已改用文字回复。',
    'voice_unavailable': '语音服务暂时不可用，已改用文字回复。',
    'voice_invalid_audio': '生成的音频无效或超出语音限制，已改用文字回复。',
    'voice_failed': '语音合成未完成，已改用文字回复。',
}
_ID = re.compile(r'^[A-Za-z0-9_.:=+/-]{1,256}$')
# QQ message IDs are opaque printable tokens (e.g. ROBOT1.0_...!).
# They are used only as JSON values and dedup keys, never paths or commands.
_MESSAGE_ID = re.compile(r'^[\x21-\x7e]{1,256}$')
_RECEIPT_ID = re.compile(r'^[A-Za-z0-9_-]{16,64}$')
_MAX_AUDIO_BYTES = 3_500_000


class QQError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(ERRORS.get(code, ERRORS['runtime_failed']))


def _write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + secrets.token_hex(6) + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            os.chmod(temporary, 0o600)
            json.dump(data, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(OSError):
            temporary.unlink(missing_ok=True)


def _read_json(path):
    if not path.exists():
        return None
    if path.stat().st_size > 8_000_000:
        raise ValueError('oversize')
    return json.loads(path.read_text('utf-8'))


def _valid_id(value):
    return isinstance(value, str) and bool(_ID.fullmatch(value))


def _qr_url(value):
    if not isinstance(value, str) or len(value) > 4096 or any(c.isspace() for c in value):
        return False
    try:
        url = urlsplit(value)
        return (url.scheme == 'https' and url.hostname == 'q.qq.com'
                and url.port in {None, 443} and url.username is None and url.password is None
                and url.path == '/qqbot/openclaw/connect.html' and not url.fragment)
    except ValueError:
        return False


class QQChannel:
    def __init__(self, root, persona, *, autobiography=None, session_factory=PrivateChatSession,
                 clock=time.time, reply_timeout=95., receipt_timeout=60., voice=None, voice_timeout=65.,
                 voice_receipt_timeout=90., group_session_factory=GroupChatSession, experience_context=None,
                 on_foreground=None, companion=None):
        self.root = Path(root).resolve()
        self.folder = self.root / 'data' / 'qq'
        self.path = self.folder / 'channel.json'
        self.credentials_path = self.folder / 'credentials.json'
        self.reset_marker = self.folder / 'reset-required.json'
        self.persona, self.autobiography = persona, autobiography
        self.experience_context = experience_context
        self.on_foreground = on_foreground
        self.companion = companion
        self.session_factory, self.clock = session_factory, clock
        self.group_session_factory = group_session_factory
        self.reply_timeout, self.receipt_timeout = reply_timeout, receipt_timeout
        self.voice, self.voice_timeout = voice, voice_timeout
        self.voice_receipt_timeout = voice_receipt_timeout
        self.state = {'version': 1, 'owner': None, 'messages': {}, 'counts': {'received': 0, 'replied': 0},
                      'reply_mode': 'text'}
        self.credentials = None
        self.phase, self.error = 'offline', ''
        self.voice_fallback_reason = ''
        self.connected, self._blocked = False, False
        self.qr_url, self.pair_code, self.pair_name = '', '', ''
        self.pair_expires_at, self.last_activity = 0., 0.
        self._actions, self._messages = asyncio.Lock(), asyncio.Lock()
        self._delivery_idle = asyncio.Event()
        self._delivery_idle.set()
        self._pending, self._inflight = {}, set()
        self._commands = asyncio.Queue(maxsize=8)
        self._session = None
        self._group_sessions = OrderedDict()
        self._process = self._reader = self._server = None
        self._ready = None
        self._launch_task = None
        self._token = ''
        self._runtime_generation = 0
        self._load()

    def _load(self):
        try:
            stored = _read_json(self.path)
            if stored is not None:
                if (not isinstance(stored, dict) or stored.get('version') != 1
                        or not isinstance(stored.get('messages'), dict)
                        or len(stored['messages']) > 10000
                        or not isinstance(stored.get('counts'), dict)
                        or any(type(stored['counts'].get(k)) is not int or stored['counts'][k] < 0
                               for k in ('received', 'replied'))):
                    raise ValueError('state')
                owner = stored.get('owner')
                if stored.get('reply_mode', 'text') not in ('text', 'voice'):
                    raise ValueError('reply_mode')
                stored.setdefault('reply_mode', 'text')
                if owner is not None and (not isinstance(owner, dict)
                        or not _valid_id(owner.get('app_id')) or not _valid_id(owner.get('peer_id'))
                        or not self._valid_name(owner.get('name'))):
                    raise ValueError('owner')
                for key, record in stored['messages'].items():
                    if (not re.fullmatch('[0-9a-f]{64}', key) or not isinstance(record, dict)
                            or record.get('state') not in {'processing', 'sending', 'sent', 'failed', 'unknown'}
                            or type(record.get('at')) not in (int, float) or not math.isfinite(record['at'])):
                        raise ValueError('receipt')
                    if record['state'] in {'processing', 'sending'}:
                        record['state'] = 'unknown'
                self.state = stored
            credentials = _read_json(self.credentials_path)
            if credentials is not None:
                if (not isinstance(credentials, dict) or set(credentials) != {'appId', 'appSecret'}
                        or not _valid_id(credentials['appId'])
                        or not isinstance(credentials['appSecret'], str)
                        or not 8 <= len(credentials['appSecret']) <= 512):
                    raise ValueError('credentials')
                self.credentials = credentials
            if self.state['owner'] and (not credentials or self.state['owner']['app_id'] != credentials['appId']):
                self.state['owner'] = None
            # A crash or storage failure during forgetting must not revive old chat.
            if self.reset_marker.exists():
                (self.folder / 'conversation.json').unlink(missing_ok=True)
                self.reset_marker.unlink()
        except (OSError, ValueError, TypeError):
            self.error, self._blocked, self.phase = 'invalid_state', True, 'error'

    @staticmethod
    def _valid_name(value):
        return (isinstance(value, str) and 1 <= len(value.strip()) <= 40
                and not any(ord(c) < 32 for c in value))

    def _save(self):
        try:
            _write_json(self.path, self.state)
        except (OSError, ValueError):
            self.error, self._blocked, self.phase = 'storage_failed', True, 'error'
            raise QQError('storage_failed') from None

    def readiness(self):
        folder = self.root / 'tools' / 'qq-harness'
        expected = {
            folder / 'node_modules/@tencent-connect/qqbot-nodejs/package.json': '1.0.5',
            folder / 'node_modules/@tencent-connect/qqbot-connector/package.json': '1.2.0',
            self.root / 'tools/deepseek-harness/node_modules/@deepseek-ai/dsh-sdk-client/package.json': '0.2.1-rc.2',
        }
        try:
            return (bool(shutil.which('node')) and (folder / 'runner.mjs').is_file()
                    and all(_read_json(p).get('version') == version for p, version in expected.items()))
        except (OSError, ValueError, AttributeError):
            return False

    @property
    def busy(self):
        return bool(self._inflight or self._pending)

    @property
    def voice_available(self):
        return self.voice is not None and self.voice.available is True

    def snapshot(self):
        owner = self.state.get('owner')
        live_pair = self.pair_expires_at > self.clock()
        return {'available': self.readiness(), 'model': self.persona.model_name,
                'phase': self.phase, 'connected': self.connected,
                'account_bound': bool(self.credentials), 'owner_bound': bool(owner),
                'reply_mode': self.state['reply_mode'], 'voice_available': self.voice_available,
                'group_enabled': True, 'group_session_count': len(self._group_sessions),
                'owner_name': owner['name'] if owner else '',
                'peer_hint': ('…' + owner['peer_id'][-4:]) if owner else '',
                'qr_url': self.qr_url, 'qr_svg': '',
                'pair_code': self.pair_code if live_pair else '',
                'pair_expires_at': self.pair_expires_at if live_pair else 0,
                'error': (VOICE_FALLBACK_ERRORS.get(self.voice_fallback_reason, ERRORS['voice_fallback'])
                          if self.error == 'voice_fallback' else ERRORS.get(self.error, '')),
                'error_code': self.error,
                'voice_fallback_reason': self.voice_fallback_reason if self.error == 'voice_fallback' else '',
                'last_activity': self.last_activity, 'counts': dict(self.state['counts']), 'busy': self.busy}

    async def action(self, data):
        if not isinstance(data, dict) or data.get('action') not in {
                'connect', 'bind', 'disconnect', 'pair', 'unpair', 'reset', 'reply_mode'}:
            raise QQError('invalid_action')
        action = data['action']
        async with self._actions:
            if action == 'disconnect':
                await self._stop_runtime()
                return self.snapshot()
            if self._blocked:
                raise QQError(self.error)
            if action == 'reply_mode':
                if self.busy:
                    raise QQError('busy')
                mode = data.get('reply_mode')
                if mode not in ('text', 'voice'):
                    raise QQError('invalid_action')
                if mode == 'voice' and not self.voice_available:
                    raise QQError('voice_unavailable')
                previous = self.state['reply_mode']
                self.state['reply_mode'] = mode
                try:
                    self._save()
                except QQError:
                    self.state['reply_mode'] = previous
                    raise
                if self.error in {'voice_unavailable', 'voice_fallback'}:
                    self.error = ''
            elif action in {'bind', 'connect'}:
                if not self.readiness():
                    raise QQError('not_ready')
                if action == 'connect' and not self.credentials:
                    raise QQError('not_bound')
                await self._stop_runtime()
                self.phase, self.error = 'starting', ''
                self._launch_task = asyncio.create_task(self._launch(action))
            elif action == 'pair':
                if self.busy:
                    raise QQError('busy')
                if not self.connected:
                    raise QQError('not_connected')
                if self.state['owner']:
                    raise QQError('owner_bound')
                if not self._valid_name(data.get('owner_name')):
                    raise QQError('invalid_name')
                self.pair_name = data['owner_name'].strip()
                self.pair_code = 'QQ-' + secrets.token_hex(6).upper()
                self.pair_expires_at = self.clock() + 600
            elif action in {'unpair', 'reset'}:
                if self.busy:
                    raise QQError('busy')
                await self._clear_session()
                if action == 'unpair':
                    self.state['owner'] = None
                    self.pair_code, self.pair_name, self.pair_expires_at = '', '', 0
                self._save()
            return self.snapshot()

    async def _clear_session(self):
        if self._session:
            await self._session.close()
            self._session = None
        # This is one known file under the fixed channel directory, never a recursive delete.
        path = self.folder / 'conversation.json'
        try:
            _write_json(self.reset_marker, {'reset': True})
            path.unlink(missing_ok=True)
            self.reset_marker.unlink(missing_ok=True)
        except OSError:
            self._blocked, self.error, self.phase = True, 'storage_failed', 'error'
            raise QQError('storage_failed') from None

    def _get_session(self):
        if self._session is None:
            owner = self.state['owner']
            self._session = self.session_factory(self.persona, self.folder / 'conversation.json',
                                                owner['name'], autobiography=self.autobiography)
            bind_identity = getattr(self._session, 'bind_memory_identity', None)
            if callable(bind_identity):
                bind_identity(owner['app_id'], owner['peer_id'])
            self._session.experience_context = self.experience_context
        return self._session

    async def _get_group_session(self, data):
        identity = [data[k] for k in ('app_id', 'group_id', 'peer_id')]
        key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        if key in self._group_sessions:
            self._group_sessions.move_to_end(key)
            return self._group_sessions[key]
        folder = self.folder / 'groups'
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (key + '.json')
        if not path.exists() and sum(1 for item in folder.iterdir()
                if item.is_file() and re.fullmatch(r'[0-9a-f]{64}\.json', item.name)) >= 512:
            raise QQError('history_full')
        # Called under the message lock after the previous delivery is settled.
        # Eviction closes transient state only; the isolated history stays on disk.
        if len(self._group_sessions) >= 32:
            _, previous = self._group_sessions.popitem(last=False)
            await previous.close()
        session = self.group_session_factory(self.persona, path, *identity)
        session.experience_context = self.experience_context
        self._group_sessions[key] = session
        return session

    def _application(self):
        @web.middleware
        async def authenticate(request, handler):
            token = request.headers.get('Authorization', '')
            if (request.remote != '127.0.0.1' or 'Origin' in request.headers or not self._token
                    or not secrets.compare_digest(token, 'Bearer ' + self._token)):
                raise web.HTTPForbidden(text='Forbidden')
            try:
                host = urlsplit('//' + request.host)
                if host.hostname != '127.0.0.1' or host.username is not None:
                    raise ValueError()
                host.port
            except ValueError:
                raise web.HTTPForbidden(text='Forbidden') from None
            try:
                return await handler(request)
            except (ValueError, KeyError, TypeError):
                return web.json_response({'ok': False}, status=400)
        app = web.Application(middlewares=[authenticate], client_max_size=7 * 1024 * 1024)
        app.router.add_get('/commands', self._http_commands)
        app.router.add_post('/events', self._http_events)
        app.router.add_post('/message', self._http_message)
        app.router.add_post('/receipt', self._http_receipt)
        app.router.add_get('/audio/{receipt_id}', self._http_audio)
        app.router.add_get('/image/{receipt_id}', self._http_image)
        return app

    async def _http_commands(self, request):
        try:
            command = await asyncio.wait_for(self._commands.get(), 15)
        except asyncio.TimeoutError:
            command = None
        return web.json_response({'ok': True, 'command': command}, headers={'Cache-Control': 'no-store'})

    async def _http_events(self, request):
        await self.event(await request.json())
        return web.json_response({'ok': True})

    async def _http_message(self, request):
        return web.json_response(await self.message(await request.json()))

    async def _http_receipt(self, request):
        await self.receipt(await request.json())
        return web.json_response({'ok': True})

    async def _http_audio(self, request):
        # Audio is private to this authenticated runtime and this pending send.
        # Neither a filesystem path nor a public/static URL is ever exposed.
        receipt_id = request.match_info['receipt_id']
        pending = self._pending.get(receipt_id) if _RECEIPT_ID.fullmatch(receipt_id) else None
        audio = pending.get('audio') if pending else None
        if not isinstance(audio, bytes) or not audio:
            raise web.HTTPNotFound(headers={'Cache-Control': 'no-store'})
        return web.Response(body=audio, content_type='audio/wav', headers={'Cache-Control': 'no-store'})

    async def _http_image(self, request):
        receipt_id = request.match_info['receipt_id']
        pending = self._pending.get(receipt_id) if _RECEIPT_ID.fullmatch(receipt_id) else None
        image = pending.get('image') if pending else None
        if not isinstance(image, dict) or not isinstance(image.get('data'), bytes):
            raise web.HTTPNotFound(headers={'Cache-Control': 'no-store'})
        return web.Response(body=image['data'], content_type=image['mime'],
                            headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})

    async def event(self, data):
        if not isinstance(data, dict):
            raise ValueError('event')
        kind = data.get('type')
        if self._blocked:
            raise QQError(self.error)
        if kind == 'qr':
            if self.phase != 'binding' or not _qr_url(data.get('url')):
                raise ValueError('qr')
            self.qr_url = data['url']
        elif kind == 'bound':
            app_id, secret = data.get('app_id'), data.get('app_secret')
            if (self.phase != 'binding' or not _valid_id(app_id)
                    or not isinstance(secret, str) or not 8 <= len(secret) <= 512):
                raise ValueError('bound')
            credentials = {'appId': app_id, 'appSecret': secret}
            try:
                _write_json(self.credentials_path, credentials)
            except (OSError, ValueError):
                self._blocked, self.error, self.phase = True, 'storage_failed', 'error'
                raise QQError('storage_failed') from None
            self.credentials = credentials
            if self.state['owner'] and self.state['owner']['app_id'] != app_id:
                self.state['owner'] = None
                await self._clear_session()
            self._save()
            self.qr_url, self.phase = '', 'connecting'
            self._commands.put_nowait({'id': secrets.token_hex(12), 'type': 'connect',
                                      'credentials': dict(credentials)})
        elif kind == 'connected':
            if not self.credentials or data.get('app_id') != self.credentials['appId']:
                raise ValueError('account')
            self.connected, self.phase, self.error = True, 'connected', ''
            self.qr_url = ''
        elif kind == 'disconnected':
            self.connected = False
            self.phase = 'connecting'
        elif kind == 'error':
            self.error = 'runtime_failed'
            if data.get('code') != 'gateway_error' or not self.connected:
                self.connected, self.phase = False, 'error'
                self.qr_url = ''
        else:
            raise ValueError('event')

    async def message(self, data):
        if (not isinstance(data, dict) or any(not _valid_id(data.get(k))
                for k in ('app_id', 'peer_id'))
                or not isinstance(data.get('message_id'), str) or not _MESSAGE_ID.fullmatch(data['message_id'])
                or not isinstance(data.get('text'), str) or len(data['text']) > 4000):
            raise ValueError('message')
        attachments = data.get('attachments', [])
        if not isinstance(attachments, list) or len(attachments) > 1:
            raise ValueError('attachments')
        for attachment in attachments:
            if (not isinstance(attachment, dict) or set(attachment) != {'content_type', 'data_base64'}
                    or attachment['content_type'] not in {'image/png', 'image/jpeg', 'image/gif', 'image/webp'}
                    or not isinstance(attachment['data_base64'], str)
                    or len(attachment['data_base64']) > ((MAX_IMAGE_BYTES + 2) // 3) * 4):
                raise ValueError('attachment')
            decoded = base64.b64decode(attachment['data_base64'], validate=True)
            if image_info(decoded)['mime'] != attachment['content_type']:
                raise ValueError('attachment type')
        if 'image_unavailable' in data and type(data['image_unavailable']) is not bool:
            raise ValueError('image status')
        if not data['text'].strip():
            if not attachments and not data.get('image_unavailable'):
                raise ValueError('empty message')
            data = {**data, 'text': '（用户发送了一张图片）'}
        kind = data.get('kind', 'c2c')
        if kind not in ('c2c', 'group'):
            raise ValueError('kind')
        is_group = kind == 'group'
        if is_group:
            if not _valid_id(data.get('group_id')):
                raise ValueError('group')
            if data.get('mentioned') is not True:
                return {'ok': True}
        elif 'group_id' in data or 'mentioned' in data:
            raise ValueError('scope')
        if (self._blocked or not self.connected or not self.credentials
                or data['app_id'] != self.credentials['appId']):
            return {'ok': True}
        owner = self.state['owner']
        pairing = (not is_group and owner is None and self.pair_expires_at > self.clock() and self.pair_code
                   and secrets.compare_digest(data['text'].strip(), self.pair_code))
        if not is_group and not pairing and (not owner or owner['app_id'] != data['app_id'] or owner['peer_id'] != data['peer_id']):
            return {'ok': True}
        # Keep the original private dedup key for backwards compatibility.
        identity = ([data['app_id'], 'group', data['group_id'], data['message_id']]
                    if is_group else [data[k] for k in ('app_id', 'peer_id', 'message_id')])
        key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        if key in self.state['messages']:
            return {'ok': True}  # Never redeliver after reconnect, restart, or an uncertain send.
        if len(self._inflight) >= 8:
            return {'ok': True}
        task = asyncio.current_task()
        generation = self._runtime_generation
        self._inflight.add(task)
        prepared = None
        session = None
        audio = None
        image = None
        reply_mode = self.state['reply_mode']
        try:
            if not pairing and self.on_foreground is not None:
                self.on_foreground('qq_group' if is_group else 'qq_private')
            async with self._messages:
                current_owner = self.state['owner']
                if pairing and current_owner is not None:
                    return {'ok': True}
                await asyncio.wait_for(self._delivery_idle.wait(),
                                       max(self.receipt_timeout, self.voice_receipt_timeout) + 5)
                if (generation != self._runtime_generation or self._blocked or not self.connected
                        or key in self.state['messages']):
                    return {'ok': True}
                # Pairing must be checked again inside the lock: only one account can claim it.
                owner = self.state['owner']
                if pairing and owner is not None:
                    return {'ok': True}
                if not is_group and not pairing and (not owner or owner['peer_id'] != data['peer_id']):
                    return {'ok': True}
                cutoff = self.clock() - 7 * 86400
                self.state['messages'] = {k: r for k, r in self.state['messages'].items() if r['at'] >= cutoff}
                if len(self.state['messages']) >= 10000:
                    self.error = 'history_full'
                    return {'ok': True}
                record = {'state': 'processing', 'at': self.clock()}
                self.state['messages'][key] = record
                self.state['counts']['received'] += 1
                self.last_activity = self.clock()
                if pairing:
                    self.state['owner'] = {'app_id': data['app_id'], 'peer_id': data['peer_id'], 'name': self.pair_name}
                    self.pair_code, self.pair_name, self.pair_expires_at = '', '', 0
                self._save()  # Persist claim/dedup before model work or any QQ send.
                if pairing:
                    reply = '配对完成，我认出你了。之后可以在这里和我聊天。'
                elif not self.persona.model_configured:
                    self.error, reply = 'model_unavailable', ERRORS['model_unavailable']
                else:
                    try:
                        session = await self._get_group_session(data) if is_group else self._get_session()
                        image_context = ''
                        if attachments or data.get('image_unavailable'):
                            image_context = '收到一张图片，但当前没有可用的画面说明。请如实说明暂时没看清，不猜测内容。'
                            if attachments and self.companion is not None:
                                try:
                                    description = await asyncio.wait_for(
                                        self.companion.describe_qq(attachments, data['text']), 135)
                                    if isinstance(description, str) and description.strip():
                                        image_context = description[:4000]
                                except asyncio.CancelledError:
                                    raise
                                except Exception:
                                    pass
                        kwargs = {'reply_mode': reply_mode}
                        if image_context:
                            kwargs['image_context'] = image_context
                        prepared = await asyncio.wait_for(session.prepare(data['message_id'], data['text'],
                                                          **kwargs),
                                                          self.reply_timeout)
                        reply = prepared.text
                        if not isinstance(reply, str) or not reply.strip() or len(reply) > 3500:
                            raise ValueError('reply')
                        if _has_reply_scaffolding(reply):
                            raise PrivateChatError('回复包含过程或草稿，已停止发送。', code='model_response')
                        self.error = ''
                        self.voice_fallback_reason = ''
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        if prepared is not None:
                            await session.commit(prepared.receipt_id, sent=False)
                        prepared = None
                        # Only fixed error codes may leave the model/session layer.
                        # Exception text can contain prompts, provider bodies or keys.
                        code = ('model_timeout' if isinstance(exc, asyncio.TimeoutError) else
                                exc.code if isinstance(exc, PrivateChatError) else 'reply_failed')
                        self.error = code if code in ERRORS else 'reply_failed'
                        reply = ERRORS[self.error]
                if prepared is not None and reply_mode == 'voice':
                    try:
                        if not self.voice_available:
                            raise QQVoiceError('voice_unavailable')
                        audio = await asyncio.wait_for(self.voice.render(reply, prepared.emotion), self.voice_timeout)
                        if not isinstance(audio, bytes) or not 44 < len(audio) <= _MAX_AUDIO_BYTES:
                            raise QQVoiceError('voice_invalid_audio')
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        # This fallback happens before any remote send. Keep the
                        # exact prepared text and its normal delivery receipt.
                        audio = None
                        self.error = 'voice_fallback'
                        reason = ('voice_timeout' if isinstance(exc, asyncio.TimeoutError) else
                                  exc.code if isinstance(exc, QQVoiceError) else 'voice_failed')
                        self.voice_fallback_reason = reason if reason in VOICE_FALLBACK_ERRORS else 'voice_failed'
                if prepared is not None and reply_mode == 'text' and self.companion is not None:
                    try:
                        candidate = self.companion.pick_meme(data['text'], prepared.emotion, reply)
                        if isinstance(candidate, dict) and isinstance(candidate.get('data'), bytes):
                            info = image_info(candidate['data'])
                            if info['mime'] == candidate.get('mime'):
                                image = {'data': candidate['data'], 'mime': info['mime']}
                    except Exception:
                        pass  # No remote send has occurred; retain the prepared text.
                if generation != self._runtime_generation or self._blocked or not self.connected:
                    if prepared:
                        await session.commit(prepared.receipt_id, sent=False)
                    record['state'] = 'unknown'
                    self._save()
                    return {'ok': True}
                receipt_id = secrets.token_urlsafe(24)
                record.update(state='sending', receipt_id=receipt_id)
                self._save()  # At-most-once barrier: no reply leaves host without this write.
                pending = {'key': key, 'prepared': prepared, 'session': session, 'timer': None,
                           'audio': audio, 'image': image}
                self._pending[receipt_id] = pending
                self._delivery_idle.clear()
                timeout = self.receipt_timeout if audio is None and image is None else max(self.receipt_timeout, self.voice_receipt_timeout)
                pending['timer'] = asyncio.create_task(self._expire_receipt(receipt_id, timeout))
                return {'ok': True, 'reply': reply, 'receipt_id': receipt_id,
                        'reply_mode': 'voice' if audio is not None else 'image' if image is not None else 'text'}
        except asyncio.CancelledError:
            if prepared is not None:
                await session.commit(prepared.receipt_id, sent=False)
            if key in self.state['messages']:
                self.state['messages'][key]['state'] = 'unknown'
                with contextlib.suppress(QQError):
                    self._save()
            raise
        except Exception:
            if prepared is not None:
                await session.commit(prepared.receipt_id, sent=False)
            raise
        finally:
            self._inflight.discard(task)

    async def _expire_receipt(self, receipt_id, timeout):
        await asyncio.sleep(timeout)
        with contextlib.suppress(QQError):
            await self.receipt({'receipt_id': receipt_id, 'status': 'unknown'})

    async def receipt(self, data):
        if (not isinstance(data, dict) or not isinstance(data.get('receipt_id'), str)
                or data.get('status') not in {'sent', 'unknown', 'failed'}):
            raise ValueError('receipt')
        pending = self._pending.pop(data['receipt_id'], None)
        if pending is None:
            return
        pending.pop('audio', None)
        timer = pending['timer']
        if timer and timer is not asyncio.current_task():
            timer.cancel()
        sent = data['status'] == 'sent'
        try:
            self.state['messages'][pending['key']]['state'] = data['status']
            if sent:
                self.state['counts']['replied'] += 1
            else:
                self.error = 'delivery_unknown' if data['status'] == 'unknown' else 'reply_failed'
            self._save()
            if pending['prepared']:
                await pending['session'].commit(pending['prepared'].receipt_id, sent=sent)
        except Exception:
            self._blocked, self.error, self.phase = True, 'storage_failed', 'error'
            raise QQError('storage_failed') from None
        finally:
            self._delivery_idle.set()

    async def _start_runtime(self):
        self.error, self.phase = '', 'starting'
        self._token = secrets.token_urlsafe(32)
        self._server = web.AppRunner(self._application(), access_log=None, shutdown_timeout=1)
        await self._server.setup()
        site = web.TCPSite(self._server, '127.0.0.1', 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        endpoint = f'http://127.0.0.1:{port}'
        workspace = self.folder / 'workspace'
        home = self.folder / 'harness'
        workspace.mkdir(parents=True, exist_ok=True)
        home.mkdir(parents=True, exist_ok=True)
        allowed = {'PATH', 'SystemRoot', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT',
                   'TEMP', 'TMP', 'LANG', 'LC_ALL', 'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY'}
        env = {k: v for k, v in os.environ.items() if k in allowed}
        env.update(VTUBER_QQ_BRIDGE=endpoint, VTUBER_QQ_TOKEN=self._token)
        self._ready = asyncio.get_running_loop().create_future()
        try:
            spawn = asyncio.create_task(asyncio.create_subprocess_exec('node', str(self.root / 'tools/qq-harness/runner.mjs'),
                cwd=workspace, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, limit=32768,
                **({'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session': True})))
            try:
                self._process = await asyncio.shield(spawn)
            except asyncio.CancelledError:
                self._process = await spawn
                raise
            self._reader = asyncio.create_task(self._read_runtime())
            payload = {'type': 'start', 'endpoint': endpoint, 'token': self._token,
                       'home': str(home), 'workspace': str(workspace)}
            self._process.stdin.write((json.dumps(payload) + '\n').encode())
            await self._process.stdin.drain()
            await asyncio.wait_for(asyncio.shield(self._ready), 40)
        except asyncio.CancelledError:
            await self._stop_runtime()
            raise
        except Exception:
            await self._stop_runtime()
            self.error, self.phase = 'runtime_failed', 'error'
            raise QQError('runtime_failed') from None

    async def _launch(self, action):
        try:
            await self._start_runtime()
            self.phase = 'binding' if action == 'bind' else 'connecting'
            command = {'id': secrets.token_hex(12), 'type': action}
            if action == 'connect':
                command['credentials'] = dict(self.credentials)
            self._commands.put_nowait(command)
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._stop_runtime()
            self.error, self.phase = 'runtime_failed', 'error'

    async def _read_runtime(self):
        try:
            while line := await self._process.stdout.readline():
                value = json.loads(line)
                if value.get('type') == 'ready' and not self._ready.done():
                    self._ready.set_result(True)
                elif value.get('type') == 'error':
                    raise ValueError('runtime')
            raise ValueError('closed')
        except asyncio.CancelledError:
            raise
        except Exception:
            self.error, self.phase, self.connected = 'runtime_failed', 'error', False
            self._token = ''
            if not self._ready.done():
                self._ready.set_exception(QQError('runtime_failed'))

    async def _stop_runtime(self):
        self._runtime_generation += 1
        self._token, self.connected, self.qr_url = '', False, ''
        self.pair_code, self.pair_name, self.pair_expires_at = '', '', 0
        current = asyncio.current_task()
        launch, self._launch_task = self._launch_task, None
        if launch and launch is not current:
            launch.cancel()
            await asyncio.gather(launch, return_exceptions=True)
        for task in tuple(self._inflight):
            if task is not current:
                task.cancel()
        await asyncio.gather(*(t for t in tuple(self._inflight) if t is not current), return_exceptions=True)
        for receipt_id in list(self._pending):
            with contextlib.suppress(QQError):
                await self.receipt({'receipt_id': receipt_id, 'status': 'unknown'})
        if self._reader:
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)
            self._reader = None
        if self._process:
            await HarnessRunner._stop(self._process)
            self._process = None
        if self._server:
            await self._server.cleanup()
            self._server = None
        self._commands = asyncio.Queue(maxsize=8)
        self.phase = 'error' if self._blocked else 'offline'

    async def apply_memory_change(self, memory, replacement=None):
        owner = self.state['owner']
        changed_owner = memory_change_owner(memory)
        if not owner or changed_owner is not None and changed_owner != viewer_identity(owner['name']):
            return
        async with self._actions:
            for task in tuple(self._inflight):
                task.cancel()
            await asyncio.gather(*tuple(self._inflight), return_exceptions=True)
            failed = False
            try:
                _write_json(self.reset_marker, {'reset': True})
            except OSError:
                failed = True
            for receipt_id in list(self._pending):
                try:
                    await self.receipt({'receipt_id': receipt_id, 'status': 'unknown'})
                except Exception:
                    failed = True
            try:
                await self._get_session().apply_memory_change(memory, replacement)
                if not failed:
                    self.reset_marker.unlink(missing_ok=True)
            except Exception:
                failed = True
                # Still attempt removal if corrupt history prevented loading.
                with contextlib.suppress(OSError):
                    (self.folder / 'conversation.json').unlink(missing_ok=True)
            if failed:
                self._blocked, self.error, self.phase = True, 'storage_failed', 'error'
                raise QQError('storage_failed') from None

    async def close(self):
        async with self._actions:
            await self._stop_runtime()
            if self._session:
                await self._session.close()
                self._session = None
            for session in self._group_sessions.values():
                await session.close()
            self._group_sessions.clear()
            if self.voice is not None:
                await self.voice.close()
