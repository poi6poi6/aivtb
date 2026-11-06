import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiohttp.test_utils import TestClient, TestServer

from brain.qq_channel import ERRORS, QQChannel, QQError, _qr_url, _write_json
from brain.private_chat import PrivateChatError
from brain.qq_voice import QQVoiceError
from brain.viewer_identity import viewer_identity


class Session:
    instances = []

    def __init__(self, parent, path, owner_name, **kwargs):
        self.owner = owner_name
        self.prepared, self.committed = [], []
        self.block = None
        self.closed = False
        self.changes = []
        self.started = asyncio.Event()
        Session.instances.append(self)

    async def prepare(self, message_id, text, *, reply_mode='text'):
        self.prepared.append((message_id, text))
        self.reply_mode = reply_mode
        self.started.set()
        if self.block:
            await self.block.wait()
        return SimpleNamespace(receipt_id='private-' + message_id, text='记得，我们接着聊。', emotion='平静')

    async def commit(self, receipt_id, *, sent):
        self.committed.append((receipt_id, sent))
        return True

    async def close(self):
        self.closed = True

    async def apply_memory_change(self, memory, replacement=None):
        self.changes.append(memory)


class GroupSession(Session):
    def __init__(self, parent, path, app_id, group_id, member_id):
        super().__init__(parent, path, '群友')
        self.identity = (app_id, group_id, member_id)
        self.path = path


class QQChannelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.persona = SimpleNamespace(model_name='same-persona', model_configured=True)
        self.now = 1000000.
        self.channel = self.make()
        self.addAsyncCleanup(self.channel.close)
        self.channel.credentials = {'appId': '123', 'appSecret': 'fixture-secret'}
        _write_json(self.channel.credentials_path, self.channel.credentials)
        await self.channel.event({'type': 'connected', 'app_id': '123'})

    def make(self, **kwargs):
        return QQChannel(self.root, self.persona, session_factory=Session,
                         group_session_factory=GroupSession, clock=lambda: self.now, **kwargs)

    def message(self, text='你好', message_id='one', peer='OWNER', app_id='123'):
        return {'app_id': app_id, 'peer_id': peer, 'message_id': message_id, 'text': text}

    def group_message(self, text='来聊聊', message_id='one', peer='MEMBER', group='GROUP'):
        return {**self.message(text, message_id, peer), 'kind': 'group', 'group_id': group, 'mentioned': True}

    async def pair(self):
        status = await self.channel.action({'action': 'pair', 'owner_name': '你'})
        reply = await self.channel.message(self.message(status['pair_code'], 'pair'))
        await self.channel.receipt({'receipt_id': reply['receipt_id'], 'status': 'sent'})
        return reply

    async def test_only_verified_owner_receives_persona_and_no_credentials_in_status(self):
        self.assertEqual(await self.channel.message(self.message()), {'ok': True})
        await self.pair()
        self.assertEqual(await self.channel.message(self.message(peer='OTHER')), {'ok': True})
        self.assertEqual(await self.channel.message(self.message(app_id='456')), {'ok': True})
        reply = await self.channel.message(self.message())
        self.assertIn('接着聊', reply['reply'])
        self.assertEqual(self.channel._session.owner, '你')
        self.assertEqual(self.channel._session.committed, [])
        self.assertEqual(self.channel.state['messages'][next(reversed(self.channel.state['messages']))]['state'], 'sending')
        status = json.dumps(self.channel.snapshot())
        for private in ('fixture-secret', '你好', '接着聊'):
            self.assertNotIn(private, status)
        await self.channel.receipt({'receipt_id': reply['receipt_id'], 'status': 'sent'})
        self.assertEqual(self.channel._session.committed, [('private-one', True)])

    async def test_verified_messages_preempt_background_before_waiting_for_delivery(self):
        foreground = Mock()
        self.channel.on_foreground = foreground
        await self.channel.message(self.message())
        await self.pair()
        await self.channel.message(self.message(peer='OTHER'))
        foreground.assert_not_called()
        first = await self.channel.message(self.message())
        foreground.assert_called_once_with('qq_private')
        await self.channel.message(self.message())
        self.assertEqual(foreground.call_count, 1, 'duplicate input does not interrupt again')
        second = asyncio.create_task(self.channel.message(self.group_message(message_id='second')))
        await asyncio.sleep(0)
        foreground.assert_called_with('qq_group')
        self.assertFalse(second.done(), 'preemption occurs before previous receipt arrives')
        await self.channel.receipt({'receipt_id': first['receipt_id'], 'status': 'sent'})
        reply = await second
        await self.channel.receipt({'receipt_id': reply['receipt_id'], 'status': 'sent'})

    async def test_private_memory_identity_uses_verified_app_and_peer(self):
        await self.pair()
        with patch.object(Session, 'bind_memory_identity', create=True) as bind:
            session = self.channel._get_session()
            bind.assert_called_once_with('123', 'OWNER')
            self.assertIs(self.channel._get_session(), session)
            bind.assert_called_once()

    async def test_pair_code_expiration_and_single_claim(self):
        status = await self.channel.action({'action': 'pair', 'owner_name': '你'})
        self.now += 601
        self.assertEqual(await self.channel.message(self.message(status['pair_code'])), {'ok': True})
        self.assertFalse(self.channel.snapshot()['pair_code'])
        status = await self.channel.action({'action': 'pair', 'owner_name': '你'})
        first, second = await asyncio.gather(
            self.channel.message(self.message(status['pair_code'], 'pair1', peer='A')),
            self.channel.message(self.message(status['pair_code'], 'pair2', peer='B')))
        self.assertEqual(sum('reply' in item for item in (first, second)), 1)
        self.assertEqual(self.channel.state['owner']['peer_id'], 'A')

    async def test_qq_opaque_message_ids_pair_reply_and_deduplicate(self):
        status = await self.channel.action({'action': 'pair', 'owner_name': '你'})
        incoming = self.message(status['pair_code'], 'ROBOT1.0_pair-example!')
        reply = await self.channel.message(incoming)
        self.assertIn('配对完成', reply['reply'])
        await self.channel.receipt({'receipt_id': reply['receipt_id'], 'status': 'sent'})
        self.assertEqual(await self.channel.message(incoming), {'ok': True})
        message_id = 'ROBOT1.0_chat-example!'
        reply = await self.channel.message(self.message(message_id=message_id))
        await self.channel.receipt({'receipt_id': reply['receipt_id'], 'status': 'sent'})
        self.assertEqual(self.channel._session.prepared, [(message_id, '你好')])
        self.assertEqual(self.channel.state['counts'], {'received': 2, 'replied': 2})
        for invalid in ('', 'line\nbreak', 'two words', 'null\0byte', 'x' * 257, '\x7f'):
            with self.assertRaises(ValueError):
                await self.channel.message(self.message(message_id=invalid))
        with self.assertRaises(ValueError):
            await self.channel.message(self.message(peer='invalid!peer'))

    async def test_unknown_send_is_not_committed_or_retried_even_after_restart(self):
        await self.pair()
        result = await self.channel.message(self.message())
        await self.channel.receipt({'receipt_id': result['receipt_id'], 'status': 'unknown'})
        self.assertEqual(self.channel._session.committed, [('private-one', False)])
        self.assertEqual(await self.channel.message(self.message()), {'ok': True})
        other = self.make()
        self.addAsyncCleanup(other.close)
        await other.event({'type': 'connected', 'app_id': '123'})
        self.assertTrue(other.snapshot()['owner_bound'])
        self.assertEqual(await other.message(self.message()), {'ok': True})
        self.assertIsNone(other._session)

    async def test_restart_pending_receipt_is_unknown_and_not_retried(self):
        await self.pair()
        await self.channel.message(self.message())
        other = self.make()
        self.addAsyncCleanup(other.close)
        await other.event({'type': 'connected', 'app_id': '123'})
        self.assertEqual(await other.message(self.message()), {'ok': True})
        self.assertIn('unknown', [r['state'] for r in other.state['messages'].values()])

    async def test_send_barrier_failure_returns_no_reply(self):
        await self.pair()
        original = self.channel._save
        calls = []
        def save():
            calls.append(True)
            if len(calls) == 2:
                self.channel._blocked = True
                raise QQError('storage_failed')
            original()
        with patch.object(self.channel, '_save', side_effect=save):
            with self.assertRaises(QQError):
                await self.channel.message(self.message())
        self.assertFalse(self.channel._pending)
        self.assertTrue(self.channel._blocked)

    async def test_receipt_timeout_releases_without_claiming_delivery(self):
        self.channel.receipt_timeout = .02
        await self.pair()
        await self.channel.message(self.message())
        await asyncio.sleep(.04)
        self.assertEqual(self.channel._session.committed, [('private-one', False)])
        self.assertFalse(self.channel.busy)
        self.assertEqual(self.channel.error, 'delivery_unknown')

    async def test_reset_and_unpair_do_not_allow_old_account_or_duplicate_send(self):
        await self.pair()
        result = await self.channel.message(self.message())
        with self.assertRaisesRegex(QQError, '当前回复'):
            await self.channel.action({'action': 'reset'})
        await self.channel.receipt({'receipt_id': result['receipt_id'], 'status': 'sent'})
        old_session = self.channel._session
        await self.channel.action({'action': 'reset'})
        self.assertTrue(old_session.closed)
        self.assertTrue(self.channel.snapshot()['owner_bound'])
        self.assertEqual(await self.channel.message(self.message()), {'ok': True})
        await self.channel.action({'action': 'unpair'})
        self.assertFalse(self.channel.snapshot()['owner_bound'])
        self.assertEqual(await self.channel.message(self.message(message_id='new')), {'ok': True})

    async def test_disconnect_cancels_generation_and_never_sends(self):
        await self.pair()
        session = self.channel._get_session()
        session.block = asyncio.Event()
        task = asyncio.create_task(self.channel.message(self.message()))
        await asyncio.wait_for(session.started.wait(), 1)
        await self.channel.action({'action': 'disconnect'})
        self.assertTrue(task.cancelled())
        self.assertFalse(self.channel.connected)
        self.assertFalse(self.channel._pending)

    async def test_changed_robot_clears_previous_owner(self):
        await self.pair()
        self.channel.phase = 'binding'
        await self.channel.event({'type': 'bound', 'app_id': '456', 'app_secret': 'new-secret'})
        self.assertFalse(self.channel.snapshot()['owner_bound'])
        self.assertEqual(self.channel._commands.get_nowait()['credentials']['appId'], '456')
        with self.assertRaises(ValueError):
            await self.channel.event({'type': 'connected', 'app_id': '123'})

    async def test_corrupt_storage_never_silently_replaces_owner(self):
        self.channel.path.write_text('{broken', encoding='utf-8')
        other = self.make()
        self.addAsyncCleanup(other.close)
        self.assertEqual(other.error, 'invalid_state')
        with self.assertRaises(QQError):
            await other.action({'action': 'pair', 'owner_name': '你'})
        self.assertEqual(self.channel.path.read_text('utf-8'), '{broken')

    async def test_http_auth_origin_host_and_credential_projection(self):
        self.channel._token = 'fixture-local-token'
        client = TestClient(TestServer(self.channel._application()))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        for headers in ({}, {'Authorization': 'Bearer wrong'},
                        {'Authorization': 'Bearer fixture-local-token', 'Origin': 'https://evil.test'},
                        {'Authorization': 'Bearer fixture-local-token', 'Host': 'evil.test'}):
            response = await client.post('/message', json=self.message(), headers=headers)
            self.assertEqual(response.status, 403)
        response = await client.post('/message', json=self.message(),
                                     headers={'Authorization': 'Bearer fixture-local-token'})
        self.assertEqual(await response.json(), {'ok': True})

    async def test_qr_urls_require_exact_tencent_binding_page(self):
        self.assertTrue(_qr_url('https://q.qq.com/qqbot/openclaw/connect.html?task_id=test'))
        for url in ('javascript:alert(1)', 'https://q.qq.com.evil.test/qqbot/openclaw/connect.html',
                    'https://q.qq.com@evil.test/qqbot/openclaw/connect.html',
                    'http://q.qq.com/qqbot/openclaw/connect.html', 'https://q.qq.com/other',
                    'https://q.qq.com:444/qqbot/openclaw/connect.html'):
            self.assertFalse(_qr_url(url), url)

    async def test_forgetting_still_clears_session_when_receipt_save_fails(self):
        await self.pair()
        await self.channel.message(self.message())
        memory = {'owner_key': viewer_identity('你')}
        with patch.object(self.channel, '_save', side_effect=QQError('storage_failed')):
            with self.assertRaises(QQError):
                await self.channel.apply_memory_change(memory)
        self.assertEqual(self.channel._session.changes, [memory])
        self.assertTrue(self.channel._blocked)
        self.assertTrue(self.channel.reset_marker.exists())

    async def test_reset_marker_removes_old_history_after_restart(self):
        await self.pair()
        history = self.channel.folder / 'conversation.json'
        history.write_text('old private fact', encoding='utf-8')
        _write_json(self.channel.reset_marker, {'reset': True})
        other = self.make()
        self.addAsyncCleanup(other.close)
        self.assertFalse(history.exists())
        self.assertFalse(other.reset_marker.exists())

    async def test_forgetting_another_owner_does_not_cancel_qq(self):
        await self.pair()
        await self.channel.message(self.message())
        await self.channel.apply_memory_change({'owner_key': viewer_identity('other')})
        self.assertTrue(self.channel.busy)
        self.assertFalse(self.channel._session.changes)

    async def test_disconnect_cancels_pending_start(self):
        entered = asyncio.Event()
        async def slow_start():
            entered.set()
            await asyncio.Event().wait()
        with patch.object(self.channel, 'readiness', return_value=True), patch.object(self.channel, '_start_runtime', side_effect=slow_start):
            state = await self.channel.action({'action': 'connect'})
            self.assertEqual(state['phase'], 'starting')
            await entered.wait()
            task = self.channel._launch_task
            await self.channel.action({'action': 'disconnect'})
            self.assertTrue(task.cancelled())
            self.assertEqual(self.channel.phase, 'offline')

    def enable_voice(self):
        self.channel.voice = SimpleNamespace(available=True,
            render=AsyncMock(return_value=b'RIFF' + b'private-audio' * 10), close=AsyncMock())
        return self.channel.voice

    async def test_reply_mode_is_explicit_persisted_and_available_while_disconnected(self):
        self.assertEqual(self.channel.snapshot()['reply_mode'], 'text')
        self.assertFalse(self.channel.snapshot()['voice_available'])
        with self.assertRaisesRegex(QQError, '语音服务'):
            await self.channel.action({'action': 'reply_mode', 'reply_mode': 'voice'})
        voice = self.enable_voice()
        await self.channel.action({'action': 'disconnect'})
        state = await self.channel.action({'action': 'reply_mode', 'reply_mode': 'voice'})
        self.assertEqual(state['reply_mode'], 'voice')
        other = self.make()
        self.addAsyncCleanup(other.close)
        self.assertEqual(other.snapshot()['reply_mode'], 'voice')
        self.assertFalse(other.snapshot()['voice_available'])
        self.assertEqual((await other.action({'action': 'reply_mode', 'reply_mode': 'text'}))['reply_mode'], 'text')
        voice.render.assert_not_awaited()
        for mode in (None, [], False, 'both', 'VOICE'):
            with self.assertRaises(QQError):
                await self.channel.action({'action': 'reply_mode', 'reply_mode': mode})

    async def test_old_channel_state_defaults_to_text_and_bad_modes_are_rejected(self):
        old = dict(self.channel.state)
        old.pop('reply_mode')
        _write_json(self.channel.path, old)
        other = self.make()
        self.addAsyncCleanup(other.close)
        self.assertEqual(other.state['reply_mode'], 'text')
        _write_json(self.channel.path, {**old, 'reply_mode': 'unknown'})
        broken = self.make()
        self.addAsyncCleanup(broken.close)
        self.assertEqual(broken.error, 'invalid_state')

    async def test_failed_setting_save_does_not_report_new_mode(self):
        self.enable_voice()
        with patch.object(self.channel, '_save', side_effect=QQError('storage_failed')):
            with self.assertRaises(QQError):
                await self.channel.action({'action': 'reply_mode', 'reply_mode': 'voice'})
        self.assertEqual(self.channel.snapshot()['reply_mode'], 'text')

    async def test_voice_is_private_receipt_gated_and_pairing_stays_text(self):
        voice = self.enable_voice()
        await self.channel.action({'action': 'reply_mode', 'reply_mode': 'voice'})
        pair_reply = await self.pair()
        self.assertEqual(pair_reply['reply_mode'], 'text')
        voice.render.assert_not_awaited()
        reply = await self.channel.message(self.message())
        self.assertEqual(reply['reply_mode'], 'voice')
        self.assertEqual(self.channel._session.reply_mode, 'voice')
        voice.render.assert_awaited_once_with(reply['reply'], '平静')
        self.assertEqual(self.channel._session.committed, [])
        self.assertNotIn('private-audio', json.dumps(self.channel.snapshot()))
        self.assertNotIn('private-audio', self.channel.path.read_text('utf-8'))
        self.channel._token = 'fixture-audio-token'
        client = TestClient(TestServer(self.channel._application()))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        path = '/audio/' + reply['receipt_id']
        auth = {'Authorization': 'Bearer fixture-audio-token'}
        self.assertEqual((await client.get(path)).status, 403)
        self.assertEqual((await client.get(path, headers={**auth, 'Origin': 'http://127.0.0.1'})).status, 403)
        self.assertEqual((await client.get('/audio/' + 'x' * 32, headers=auth)).status, 404)
        response = await client.get(path, headers=auth)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers['Content-Type'], 'audio/wav')
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(await response.read(), voice.render.return_value)
        with self.assertRaisesRegex(QQError, '当前回复'):
            await self.channel.action({'action': 'reply_mode', 'reply_mode': 'text'})
        await self.channel.receipt({'receipt_id': reply['receipt_id'], 'status': 'sent'})
        self.assertEqual(self.channel._session.committed, [('private-one', True)])
        self.assertEqual((await client.get(path, headers=auth)).status, 404)
        self.assertFalse(self.channel._pending)

    async def test_text_mode_never_synthesizes_and_unavailable_model_remains_text(self):
        voice = self.enable_voice()
        await self.pair()
        reply = await self.channel.message(self.message())
        self.assertEqual(reply['reply_mode'], 'text')
        voice.render.assert_not_awaited()
        await self.channel.receipt({'receipt_id': reply['receipt_id'], 'status': 'sent'})
        await self.channel.action({'action': 'reply_mode', 'reply_mode': 'voice'})
        self.persona.model_configured = False
        reply = await self.channel.message(self.message(message_id='two'))
        self.assertEqual(reply['reply_mode'], 'text')
        voice.render.assert_not_awaited()

    async def test_synthesis_failure_timeout_and_oversize_fall_back_before_send(self):
        voice = self.enable_voice()
        await self.pair()
        await self.channel.action({'action': 'reply_mode', 'reply_mode': 'voice'})
        for index, outcome in enumerate((RuntimeError('private upstream detail'), asyncio.TimeoutError(),
                                        b'x' * 3_500_001, None)):
            voice.render.side_effect = outcome if isinstance(outcome, Exception) else None
            voice.render.return_value = outcome
            reply = await self.channel.message(self.message(message_id=f'fallback-{index}'))
            self.assertEqual(reply['reply_mode'], 'text')
            self.assertEqual(reply['reply'], '记得，我们接着聊。')
            self.assertEqual(self.channel.error, 'voice_fallback')
            self.assertNotIn('private upstream', json.dumps(self.channel.snapshot()))
            self.assertIsNone(self.channel._pending[reply['receipt_id']]['audio'])
            await self.channel.receipt({'receipt_id': reply['receipt_id'], 'status': 'sent'})
            self.assertEqual(self.channel._session.committed[-1], (f'private-fallback-{index}', True))

    async def test_disconnect_cancels_voice_generation_and_disallows_mode_change_while_busy(self):
        voice = self.enable_voice()
        await self.pair()
        await self.channel.action({'action': 'reply_mode', 'reply_mode': 'voice'})
        started = asyncio.Event()
        async def render(*args):
            started.set()
            await asyncio.Event().wait()
        voice.render.side_effect = render
        task = asyncio.create_task(self.channel.message(self.message()))
        await asyncio.wait_for(started.wait(), 1)
        with self.assertRaisesRegex(QQError, '当前回复'):
            await self.channel.action({'action': 'reply_mode', 'reply_mode': 'text'})
        await self.channel.action({'action': 'disconnect'})
        self.assertTrue(task.cancelled())
        self.assertFalse(self.channel._pending)
        self.assertEqual(self.channel._session.committed, [('private-one', False)])

    async def test_voice_fallback_status_names_fixed_reason_without_provider_details(self):
        voice = self.enable_voice()
        await self.channel.action({'action': 'reply_mode', 'reply_mode': 'voice'})
        for code, expected in (('voice_text_too_long', '300'), ('voice_timeout', '超时'),
                               ('voice_unavailable', '暂时不可用'), ('voice_invalid_audio', '音频无效'),
                               ('voice_invalid_text', '无法用于语音'), ('voice_failed', '未完成')):
            voice.render.side_effect = QQVoiceError(code)
            result = await self.channel.message(self.group_message(message_id=code))
            self.assertEqual(result['reply_mode'], 'text')
            self.assertEqual(self.channel.snapshot()['voice_fallback_reason'], code)
            self.assertIn(expected, self.channel.snapshot()['error'])
            await self.channel.receipt({'receipt_id': result['receipt_id'], 'status': 'sent'})
        error = RuntimeError('PRIVATE_TTS_TOKEN')
        error.code = 'voice_timeout'
        voice.render.side_effect = error
        result = await self.channel.message(self.group_message(message_id='unknown-error'))
        self.assertEqual(self.channel.snapshot()['voice_fallback_reason'], 'voice_failed')
        self.assertNotIn('PRIVATE_TTS_TOKEN', json.dumps(self.channel.snapshot()))
        await self.channel.receipt({'receipt_id': result['receipt_id'], 'status': 'sent'})
        voice.render.side_effect = None
        result = await self.channel.message(self.group_message(message_id='voice-recovered'))
        self.assertEqual(result['reply_mode'], 'voice')
        self.assertEqual(self.channel.snapshot()['voice_fallback_reason'], '')
        self.assertEqual(self.channel.snapshot()['error'], '')

    async def test_unknown_voice_delivery_discards_bytes_without_committing_or_resending(self):
        self.enable_voice()
        await self.pair()
        await self.channel.action({'action': 'reply_mode', 'reply_mode': 'voice'})
        self.channel.receipt_timeout = self.channel.voice_receipt_timeout = .01
        result = await self.channel.message(self.message())
        self.assertEqual(result['reply_mode'], 'voice')
        await asyncio.sleep(.03)
        self.assertFalse(self.channel._pending)
        self.assertEqual(self.channel._session.committed, [('private-one', False)])
        self.assertEqual(await self.channel.message(self.message()), {'ok': True})

    async def test_group_mention_works_without_private_pairing_and_commits_exact_session(self):
        reply = await self.channel.message(self.group_message())
        self.assertIn('reply', reply)
        self.assertIsNone(self.channel.state['owner'])
        self.assertIsNone(self.channel._session)
        session = next(iter(self.channel._group_sessions.values()))
        self.assertEqual(session.identity, ('123', 'GROUP', 'MEMBER'))
        self.assertEqual(session.committed, [])
        await self.channel.receipt({'receipt_id': reply['receipt_id'], 'status': 'sent'})
        self.assertEqual(session.committed, [('private-one', True)])
        self.assertEqual(self.channel.snapshot()['group_session_count'], 1)
        self.assertTrue(self.channel.snapshot()['group_enabled'])
        self.assertFalse(self.channel.snapshot()['owner_bound'])
        self.assertEqual(await self.channel.message(self.message(peer='MEMBER')), {'ok': True})

    async def test_group_requires_valid_scope_and_explicit_mention_evidence(self):
        for mentioned in (False, None, 'true', 1):
            self.assertEqual(await self.channel.message({**self.group_message(), 'mentioned': mentioned}), {'ok': True})
        for data in ({**self.group_message(), 'group_id': ''},
                     {**self.group_message(), 'kind': 'guild'},
                     {**self.group_message(), 'kind': 'c2c'}):
            with self.assertRaises(ValueError):
                await self.channel.message(data)
        self.assertEqual(self.channel.state['counts']['received'], 0)
        self.assertFalse(self.channel._group_sessions)

    async def test_group_model_failure_uses_safe_reason_and_never_commits_or_synthesizes(self):
        voice = self.enable_voice()
        await self.channel.action({'action': 'reply_mode', 'reply_mode': 'voice'})
        for code in ('model_timeout', 'model_connection', 'model_access', 'model_quota',
                     'model_unavailable', 'model_response', 'reply_failed'):
            with patch.object(GroupSession, 'prepare', side_effect=PrivateChatError(
                    'PRIVATE_PROVIDER_BODY_AND_SECRET', code=code)):
                result = await self.channel.message(self.group_message(message_id=code))
            self.assertEqual(result['reply_mode'], 'text')
            self.assertEqual(result['reply'], ERRORS[code])
            self.assertEqual(self.channel.snapshot()['error_code'], code)
            self.assertNotIn('PRIVATE_', json.dumps(self.channel.snapshot()) + json.dumps(result))
            self.assertIsNone(self.channel._pending[result['receipt_id']]['prepared'])
            await self.channel.receipt({'receipt_id': result['receipt_id'], 'status': 'sent'})
        voice.render.assert_not_awaited()
        session = next(iter(self.channel._group_sessions.values()))
        self.assertEqual(session.committed, [])
        result = await self.channel.message(self.group_message(message_id='recovered'))
        self.assertEqual(result['reply_mode'], 'voice')
        self.assertEqual(self.channel.snapshot()['error_code'], '')
        await self.channel.receipt({'receipt_id': result['receipt_id'], 'status': 'sent'})
        self.assertEqual(session.committed, [('private-recovered', True)])

    async def test_group_outer_model_deadline_is_explicit_and_cancels_work(self):
        self.channel.reply_timeout = .02
        cancelled = asyncio.Event()
        async def blocked(*args, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        with patch.object(GroupSession, 'prepare', side_effect=blocked):
            result = await self.channel.message(self.group_message())
        self.assertTrue(cancelled.is_set())
        self.assertEqual(result['reply'], ERRORS['model_timeout'])
        self.assertEqual(self.channel.snapshot()['error_code'], 'model_timeout')
        await self.channel.receipt({'receipt_id': result['receipt_id'], 'status': 'sent'})
        self.assertEqual(await self.channel.message(self.group_message()), {'ok': True})

    async def test_host_blocks_draft_before_voice_or_text_fallback_and_does_not_commit_it(self):
        voice = self.enable_voice()
        await self.channel.action({'action': 'reply_mode', 'reply_mode': 'voice'})
        for index, draft in enumerate(('생각이\nThinking Process:\n- User wants a greeting.\n- Draft: 祝你好。',
                                        '- **Draft:** 祝你顺利。', '思考过程：先分析请求')):
            prepared = SimpleNamespace(receipt_id=f'bad-{index}', text=draft, emotion='平静')
            with patch.object(GroupSession, 'prepare', return_value=prepared):
                result = await self.channel.message(self.group_message(message_id=f'draft-{index}'))
            self.assertEqual(result['reply_mode'], 'text')
            self.assertEqual(result['reply'], ERRORS['model_response'])
            self.assertEqual(self.channel.error, 'model_response')
            self.assertIsNone(self.channel._pending[result['receipt_id']]['prepared'])
            await self.channel.receipt({'receipt_id': result['receipt_id'], 'status': 'sent'})
        voice.render.assert_not_awaited()
        session = next(iter(self.channel._group_sessions.values()))
        self.assertEqual(session.committed, [(f'bad-{i}', False) for i in range(3)])

    async def test_unknown_group_exception_is_not_exposed_as_detail(self):
        error = RuntimeError('PRIVATE_PROVIDER_BODY_AND_SECRET')
        error.code = 'model_access'  # Arbitrary exceptions cannot impersonate a safe model error.
        with patch.object(GroupSession, 'prepare', side_effect=error):
            result = await self.channel.message(self.group_message())
        self.assertEqual(result['reply'], ERRORS['reply_failed'])
        self.assertEqual(self.channel.snapshot()['error_code'], 'reply_failed')
        self.assertNotIn('PRIVATE_', json.dumps(self.channel.snapshot()) + json.dumps(result))

    async def test_group_pair_code_cannot_claim_private_identity(self):
        pairing = await self.channel.action({'action': 'pair', 'owner_name': '你'})
        reply = await self.channel.message(self.group_message(pairing['pair_code']))
        self.assertIsNone(self.channel.state['owner'])
        self.assertEqual(self.channel.pair_code, pairing['pair_code'])
        self.assertNotIn('配对完成', reply['reply'])
        await self.channel.receipt({'receipt_id': reply['receipt_id'], 'status': 'sent'})

    async def test_group_private_and_member_dedup_and_sessions_never_collide(self):
        await self.pair()
        messages = [self.message(message_id='same'),
                    self.group_message(message_id='same', peer='OWNER'),
                    self.group_message(message_id='same', peer='OWNER', group='OTHER_GROUP'),
                    self.group_message(message_id='member-message', peer='OTHER_MEMBER')]
        sessions = []
        for incoming in messages:
            reply = await self.channel.message(incoming)
            self.assertIn('reply', reply)
            sessions.append(self.channel._pending[reply['receipt_id']]['session'])
            await self.channel.receipt({'receipt_id': reply['receipt_id'], 'status': 'sent'})
            self.assertEqual(await self.channel.message(incoming), {'ok': True})
        self.assertEqual(len({id(session) for session in sessions}), 4)
        self.assertEqual(len({str(session.path) for session in sessions[1:]}), 3)
        self.assertIs(sessions[0], self.channel._session)
        self.assertEqual(self.channel.state['owner']['peer_id'], 'OWNER')
        restarted = self.make()
        self.addAsyncCleanup(restarted.close)
        await restarted.event({'type': 'connected', 'app_id': '123'})
        for incoming in messages:
            self.assertEqual(await restarted.message(incoming), {'ok': True})
        self.assertFalse(restarted._group_sessions)

    async def test_group_uses_voice_choice_and_unknown_receipt_never_commits_to_private(self):
        voice = self.enable_voice()
        await self.pair()
        private = self.channel._get_session()
        await self.channel.action({'action': 'reply_mode', 'reply_mode': 'voice'})
        reply = await self.channel.message(self.group_message())
        self.assertEqual(reply['reply_mode'], 'voice')
        group = self.channel._pending[reply['receipt_id']]['session']
        self.assertEqual(group.reply_mode, 'voice')
        voice.render.assert_awaited_once()
        await self.channel.receipt({'receipt_id': reply['receipt_id'], 'status': 'unknown'})
        self.assertEqual(group.committed, [('private-one', False)])
        self.assertEqual(private.committed, [])
        self.assertEqual(await self.channel.message(self.group_message()), {'ok': True})

    async def test_group_cache_eviction_closes_only_old_transient_session(self):
        first = None
        for index in range(33):
            reply = await self.channel.message(self.group_message(message_id=f'm{index}', peer=f'user{index}'))
            current = self.channel._pending[reply['receipt_id']]['session']
            first = first or current
            await self.channel.receipt({'receipt_id': reply['receipt_id'], 'status': 'sent'})
        self.assertEqual(len(self.channel._group_sessions), 32)
        self.assertTrue(first.closed)
        self.assertFalse(current.closed)
        await self.channel.close()
        self.assertTrue(current.closed)
        self.assertFalse(self.channel._group_sessions)


if __name__ == '__main__':
    unittest.main()
