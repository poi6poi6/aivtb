"""Tarot HTTP controls use temporary state and never contact a live channel/model."""
import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from brain.config import WebUIConfig
from brain.tarot import Tarot
from brain.tarot_web import install_tarot_routes
from brain.webui import WebUI


class TarotWebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.tarot = Tarot(self.root / 'tarot.sqlite3')
        self.queue = asyncio.Queue()
        self.services = SimpleNamespace(bind=lambda ui: None, idle=Mock(return_value=True))
        self.ui = WebUI(WebUIConfig(), self.queue, str(self.root / 'audio'), {},
                        workspace_root=self.root, services=self.services)
        self.ui.tarot = self.tarot
        self.ui.runtime_started = True
        self.ui.on_tarot_cancel = AsyncMock()
        self.app = web.Application(client_max_size=16384)
        install_tarot_routes(self.ui, self.app)
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.origin = str(self.client.make_url('/')).rstrip('/')
        self.headers = {'Origin': self.origin}

    async def action(self, payload):
        return await self.client.post('/tarot-action', json=payload, headers=self.headers)

    async def success(self, action, **values):
        response = await self.action({'action': action, **values})
        body = await response.json()
        self.assertEqual(response.status, 200, body)
        self.assertTrue(body['ok'])
        return body.get('result', body)

    async def state(self, *, public=False):
        response = await self.client.get('/tarot-status' + ('?clean=1' if public else ''))
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        return await response.json()

    async def selected(self, *, spread='single'):
        session = await self.success('create', spread=spread, mode='test')
        receipt = await self.success('test_join', session_id=session['id'], user='测试观众', question='如何整理书桌？')
        reading = await self.success('select', session_id=session['id'], entry_id=receipt['entry']['id'])
        return session, reading

    async def revealed(self):
        session, reading = await self.selected()
        reading = await self.success('draw', session_id=session['id'], reading_id=reading['id'], positions=[7])
        return session, reading

    async def test_page_and_empty_snapshot_do_not_generate_events(self):
        response = await self.client.get('/tarot')
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertIn('/static/tarot.js', await response.text())
        state = await self.state()
        self.assertEqual(set(state), {'server_time', 'current', 'entries', 'reading', 'history'})
        self.assertIsNone(state['current'])
        self.assertTrue(self.queue.empty())
        self.services.idle.assert_not_called()

    async def test_complete_test_workflow_keeps_draw_and_finished_history(self):
        session, reading = await self.selected(spread='three')
        receipt = await self.success('test_choose', session_id=session['id'], user='测试观众', positions=[3, 12, 18])
        cards = receipt['reading']['cards']
        self.assertEqual(len(cards), 3)
        duplicate = await self.success('test_choose', session_id=session['id'], user='测试观众', positions=[3, 12, 18])
        self.assertFalse(duplicate['changed'])
        self.assertEqual(duplicate['reading']['cards'], cards)
        await self.success('interpret', session_id=session['id'], reading_id=reading['id'])
        event = self.queue.get_nowait()
        self.assertEqual(event.tarot_reading_id, reading['id'])
        self.assertEqual(event.tarot_session_id, session['id'])
        self.assertTrue(self.tarot.valid_event(event))
        self.assertEqual(json.loads(event.tarot_context)['cards'], cards)
        self.tarot.mark_result(event, 'generated')
        finished = await self.success('finish', session_id=session['id'], reading_id=reading['id'])
        self.assertEqual(finished['state'], 'finished')
        self.assertEqual(finished['cards'], cards)
        self.assertFalse(self.tarot.valid_event(event))
        self.ui.on_tarot_cancel.assert_awaited_once()
        state = await self.state()
        self.assertEqual(state['history'][0]['id'], reading['id'])
        await self.success('close', session_id=session['id'])
        self.assertEqual((await self.state())['current']['state'], 'closed')
        self.assertEqual(self.ui.on_tarot_cancel.await_count, 2)
        self.assertTrue(self.queue.empty())

    async def test_clean_status_only_projects_current_public_reading(self):
        session, reading = await self.revealed()
        await self.success('test_join', session_id=session['id'], user='未上场观众', question='尚未公开的问题')
        await self.success('interpret', session_id=session['id'], reading_id=reading['id'])
        event = self.queue.get_nowait()
        state = await self.state(public=True)
        self.assertEqual(set(state), {'server_time', 'current', 'reading'})
        self.assertEqual(state['reading']['cards'], reading['cards'])
        serialized = json.dumps(state, ensure_ascii=False)
        for secret in ('未上场观众', '尚未公开的问题', event.tarot_attempt_id, 'deck', 'attempt_id', 'viewer'):
            self.assertNotIn(secret, serialized)
        self.assertIn('测试观众', serialized)

    async def test_live_creation_blocks_existing_private_reminders_after_opening(self):
        async def block_private():
            current = self.tarot.status()['current']
            self.assertEqual(current['mode'], 'live')
            self.assertEqual(current['state'], 'open')
        with patch.object(self.ui, '_block_private_followups', new=AsyncMock(side_effect=block_private)) as block:
            test = await self.success('create', mode='test')
            block.assert_not_awaited()
            await self.success('close', session_id=test['id'])
            await self.success('create', mode='live')
            block.assert_awaited_once()
            response = await self.action({'action': 'create', 'mode': 'live'})
            self.assertEqual(response.status, 400)
            block.assert_awaited_once()

    async def test_private_reminder_stop_failure_does_not_hide_created_live_session(self):
        with patch.object(self.ui, '_block_private_followups', new=AsyncMock(side_effect=OSError('private path'))):
            response = await self.action({'action': 'create', 'mode': 'live'})
        self.assertEqual(response.status, 503)
        body = await response.json()
        self.assertIn('已开启', body['error'])
        self.assertNotIn('private', body['error'])
        current = (await self.state())['current']
        self.assertEqual(current['mode'], 'live')
        self.assertEqual(current['state'], 'open')

    async def test_every_route_rejects_public_test_and_foreign_hosts(self):
        endpoints = [('GET', '/tarot'), ('GET', '/tarot?clean=1'), ('GET', '/tarot-status'),
                     ('GET', '/tarot-status?clean=1'), ('POST', '/tarot-action')]
        with patch.object(self.tarot, 'status', wraps=self.tarot.status) as status, \
                patch.object(self.tarot, 'create', wraps=self.tarot.create) as create:
            for public, host in ((True, None), (False, 'foreign.invalid'),
                                 (False, 'localhost:70000'), (False, 'user@localhost')):
                self.ui.cfg.public_test = public
                for method, path in endpoints:
                    headers = dict(self.headers)
                    if host:
                        headers.update(Host=host, Origin='http://' + host)
                    with self.subTest(public=public, host=host, path=path):
                        response = await self.client.request(method, path, json={'action': 'create'}, headers=headers)
                        self.assertEqual(response.status, 403)
            status.assert_not_called()
            create.assert_not_called()
        self.assertTrue(self.queue.empty())

    async def test_nonlocal_peer_is_rejected_for_every_route(self):
        transport = Mock()
        transport.get_extra_info.return_value = ('192.0.2.10', 12345)
        for route in self.app.router.routes():
            request = make_mocked_request(route.method, route.resource.canonical, transport=transport,
                headers={'Host': 'localhost:8080', 'Origin': 'http://localhost:8080'})
            with self.subTest(path=route.resource.canonical, method=route.method):
                with self.assertRaises(web.HTTPForbidden):
                    await route.handler(request)

    async def test_writes_require_one_exact_same_origin(self):
        origins = ({}, {'Origin': 'null'}, {'Origin': 'https://foreign.invalid'},
                   {'Origin': self.origin + '/'}, {'Origin': self.origin.replace('http:', 'https:')},
                   [('Origin', self.origin), ('Origin', self.origin)])
        for headers in origins:
            with self.subTest(headers=headers):
                response = await self.client.post('/tarot-action', json={'action': 'create'}, headers=headers)
                self.assertEqual(response.status, 403)
        self.assertIsNone((await self.state())['current'])

    async def test_missing_service_is_unavailable_without_queueing(self):
        self.ui.tarot = None
        for method, path in (('GET', '/tarot-status'), ('POST', '/tarot-action')):
            response = await self.client.request(method, path, json={'action': 'create'}, headers=self.headers)
            self.assertEqual(response.status, 503)
            self.assertIn('尚未启动', await response.text())
        self.assertEqual((await self.client.get('/tarot')).status, 200)
        self.assertTrue(self.queue.empty())

    async def test_invalid_request_types_and_unknown_fields_do_not_create_a_session(self):
        payloads = ([], None, 'create', {}, {'action': []}, {'action': 'unknown'},
                    {'action': 'create', 'secret': True}, {'action': 'create', 'mode': True},
                    {'action': 'create', 'spread': []}, {'action': 'create', 'mode': 'other'},
                    {'action': 'create', 'spread': 'other'})
        for payload in payloads:
            with self.subTest(payload=payload):
                response = await self.action(payload)
                self.assertEqual(response.status, 400)
                self.assertIn('error', await response.json())
        response = await self.client.post('/tarot-action', data='{broken', headers=self.headers)
        self.assertEqual(response.status, 400)
        self.assertIsNone((await self.state())['current'])

    async def test_ids_and_positions_are_checked_before_drawing(self):
        session, reading = await self.selected()
        base = {'action': 'draw', 'session_id': session['id'], 'reading_id': reading['id'], 'positions': [7]}
        changes = ([{'session_id': value} for value in (None, True, 7, {}, '', ' ', 'x' * 129)]
                   + [{'reading_id': value} for value in (None, False, {}, '')]
                   + [{'positions': value} for value in (None, {}, '7', [], [True], ['7'], [7.0], [0], [23], [1, 1], [1, 2, 3, 4])])
        for change in changes:
            with self.subTest(change=change):
                response = await self.action({**base, **change})
                self.assertEqual(response.status, 400)
        state = await self.state()
        self.assertEqual(state['reading']['state'], 'selecting')
        self.assertFalse(state['reading']['cards'])

    async def test_invalid_test_viewer_and_rejected_receipt_are_not_success(self):
        session, reading = await self.selected()
        response = await self.action({'action': 'test_choose', 'session_id': session['id'], 'user': '另外一位', 'positions': [1]})
        self.assertEqual(response.status, 400)
        self.assertNotIn('ok', await response.json())
        for user, question in (({}, '问题'), ('', '问题'), ('测试', []), ('测试', '')):
            response = await self.action({'action': 'test_join', 'session_id': session['id'], 'user': user, 'question': question})
            self.assertEqual(response.status, 400)
        self.assertEqual((await self.state())['reading']['id'], reading['id'])

    async def test_not_started_or_busy_runtime_cannot_claim_reading(self):
        session, reading = await self.revealed()
        for started, idle in ((False, True), (True, False)):
            self.ui.runtime_started = started
            self.services.idle.return_value = idle
            response = await self.action({'action': 'interpret', 'session_id': session['id'], 'reading_id': reading['id']})
            self.assertEqual(response.status, 409)
            self.assertEqual((await self.state())['reading']['state'], 'revealed')
        self.assertTrue(self.queue.empty())

    async def test_concurrent_duplicate_interpretation_only_queues_one_attempt(self):
        session, reading = await self.revealed()
        data = {'action': 'interpret', 'session_id': session['id'], 'reading_id': reading['id']}
        responses = await asyncio.gather(self.action(data), self.action(data))
        self.assertEqual(sorted(response.status for response in responses), [200, 400])
        self.assertEqual(self.queue.qsize(), 1)
        self.assertEqual((await self.state())['reading']['state'], 'queued')
        self.assertEqual((await self.state())['reading']['cards'], reading['cards'])

    async def test_queue_failure_invalidates_attempt_and_retry_preserves_cards(self):
        session, reading = await self.revealed()
        uncertain_events = []
        async def fail_after_enqueue(event):
            uncertain_events.append(event)
            await self.queue.put(event)
            raise OSError('private internal failure')
        self.ui.event_queue = SimpleNamespace(put=fail_after_enqueue)
        response = await self.action({'action': 'interpret', 'session_id': session['id'], 'reading_id': reading['id']})
        self.assertEqual(response.status, 503)
        self.assertNotIn('private', await response.text())
        failed = (await self.state())['reading']
        self.assertEqual(failed['state'], 'failed')
        self.assertEqual(failed['cards'], reading['cards'])
        self.assertFalse(self.tarot.valid_event(uncertain_events[0]))
        self.ui.event_queue = self.queue
        await self.success('interpret', session_id=session['id'], reading_id=reading['id'])
        old, new = self.queue.get_nowait(), self.queue.get_nowait()
        self.assertFalse(self.tarot.valid_event(old))
        self.assertTrue(self.tarot.valid_event(new))
        self.assertNotEqual(old.tarot_attempt_id, new.tarot_attempt_id)
        self.assertEqual((await self.state())['reading']['cards'], reading['cards'])

    async def test_full_queue_times_out_and_can_retry(self):
        session, reading = await self.revealed()
        queue = asyncio.Queue(maxsize=1)
        queue.put_nowait('already queued')
        self.ui.event_queue = queue
        with patch('brain.tarot_web.QUEUE_TIMEOUT_SEC', .01):
            response = await self.action({'action': 'interpret', 'session_id': session['id'], 'reading_id': reading['id']})
        self.assertEqual(response.status, 503)
        self.assertEqual((await self.state())['reading']['state'], 'failed')
        self.assertEqual(queue.qsize(), 1)
        queue.get_nowait()
        await self.success('interpret', session_id=session['id'], reading_id=reading['id'])
        self.assertTrue(self.tarot.valid_event(queue.get_nowait()))

    async def test_cancelled_queue_request_does_not_leave_claim_stuck(self):
        session, reading = await self.revealed()
        started = asyncio.Event()
        async def blocked_put(event):
            started.set()
            await asyncio.Future()
        self.ui.event_queue = SimpleNamespace(put=blocked_put)
        request = SimpleNamespace(json=AsyncMock(return_value={
            'action': 'interpret', 'session_id': session['id'], 'reading_id': reading['id']}))
        handler = next(route.handler for route in self.app.router.routes() if route.method == 'POST')
        with patch.object(self.ui, '_local_access'):
            pending = asyncio.create_task(handler(request))
            await asyncio.wait_for(started.wait(), 1)
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
        self.assertEqual((await self.state())['reading']['state'], 'failed')
        self.assertTrue(self.queue.empty())

    async def test_finish_and_close_invalidate_before_stopping_existing_output(self):
        for action in ('finish', 'close'):
            with self.subTest(action=action):
                session, reading = await self.revealed()
                await self.success('interpret', session_id=session['id'], reading_id=reading['id'])
                event = self.queue.get_nowait()
                async def stopped():
                    self.assertFalse(self.tarot.valid_event(event))
                self.ui.on_tarot_cancel = AsyncMock(side_effect=stopped)
                data = {'session_id': session['id']}
                if action == 'finish':
                    data['reading_id'] = reading['id']
                await self.success(action, **data)
                self.ui.on_tarot_cancel.assert_awaited_once()
                if action == 'finish':
                    await self.success('close', session_id=session['id'])

    async def test_cancel_hook_failure_reports_uncertain_playback_and_keeps_closed_state(self):
        session, reading = await self.revealed()
        self.ui.on_tarot_cancel = AsyncMock(side_effect=OSError('private output path'))
        response = await self.action({'action': 'close', 'session_id': session['id']})
        self.assertEqual(response.status, 503)
        self.assertNotIn('private', await response.text())
        self.assertEqual((await self.state())['current']['state'], 'closed')

    async def test_storage_errors_do_not_expose_paths_or_claim_success(self):
        for error in (OSError('private path'), sqlite3.OperationalError('private database')):
            with patch.object(self.tarot, 'status', side_effect=error):
                response = await self.client.get('/tarot-status')
                self.assertEqual(response.status, 503)
                self.assertNotIn('private', await response.text())
            with patch.object(self.tarot, 'create', side_effect=error):
                response = await self.action({'action': 'create'})
                self.assertEqual(response.status, 503)
                self.assertNotIn('private', await response.text())
        self.assertTrue(self.queue.empty())

    async def test_guided_round_is_claimed_only_by_host_worker_and_text_needs_confirmation(self):
        session = await self.success('create', mode='test', hosting='guided')
        joined = await self.success('test_join', session_id=session['id'], user='来客', question='如何开始画一张草稿？')
        reading = await self.success('select', session_id=session['id'], entry_id=joined['entry']['id'])
        self.assertEqual(reading['state'], 'welcoming')
        self.assertEqual(reading['hosting'], 'guided')
        self.assertTrue(self.queue.empty())
        with patch.object(self.tarot, 'next_event', wraps=self.tarot.next_event) as next_event:
            await self.state()
            await self.state(public=True)
            next_event.assert_not_called()
        event = self.tarot.next_event()
        self.assertEqual(event.tarot_stage, 'welcome')
        self.tarot.mark_generated(event, '请确认这个问题。', segments=0, text_only=True)
        state = await self.state()
        self.assertEqual(state['reading']['state'], 'awaiting_host')
        self.assertEqual(state['reading']['display_text'], '请确认这个问题。')
        public = await self.state(public=True)
        self.assertNotIn('请确认这个问题。', json.dumps(public, ensure_ascii=False))
        self.assertNotIn('display_text', public['reading'])
        await self.success('confirm_delivered', session_id=session['id'], reading_id=reading['id'])
        self.assertEqual((await self.state())['reading']['state'], 'awaiting_confirmation')
        response = await self.action({'action': 'test_reply', 'session_id': session['id'], 'user': '其他人', 'text': '对'})
        self.assertEqual(response.status, 400)
        await self.success('test_reply', session_id=session['id'], user='来客', text='对')
        self.assertEqual((await self.state())['reading']['state'], 'selecting')
        self.assertTrue(self.queue.empty())

    async def test_guided_pause_resume_and_takeover_invalidate_before_cancel_hook(self):
        session = await self.success('create', mode='test', hosting='guided')
        joined = await self.success('test_join', session_id=session['id'], user='来客', question='怎样休息片刻？')
        reading = await self.success('select', session_id=session['id'], entry_id=joined['entry']['id'])
        event = self.tarot.next_event()
        async def stopped():
            self.assertFalse(self.tarot.valid_event(event))
        self.ui.on_tarot_cancel = AsyncMock(side_effect=stopped)
        await self.success('pause', session_id=session['id'], reading_id=reading['id'])
        self.ui.on_tarot_cancel.assert_awaited_once()
        self.assertEqual((await self.state())['reading']['state'], 'paused')
        await self.success('resume', session_id=session['id'], reading_id=reading['id'])
        self.assertEqual((await self.state())['reading']['state'], 'welcoming')
        await self.success('takeover', session_id=session['id'], reading_id=reading['id'])
        state = (await self.state())['reading']
        self.assertEqual(state['hosting'], 'manual')
        self.assertEqual(state['state'], 'selecting')
        self.assertEqual(self.ui.on_tarot_cancel.await_count, 2)
        self.assertIsNone(self.tarot.next_event())

    async def test_guided_action_fields_are_strict_and_require_local_origin(self):
        for payload in ({'action': 'create', 'hosting': 'automatic'}, {'action': 'create', 'hosting': []},
                        {'action': 'test_reply', 'session_id': 'x', 'user': '来客', 'text': '对', 'viewer_key': 'forged'},
                        {'action': 'pause', 'session_id': 'x', 'reading_id': 'x', 'text': 'ignored'}):
            response = await self.action(payload)
            self.assertEqual(response.status, 400)
        for action in ('pause', 'resume', 'takeover', 'confirm_delivered', 'test_reply'):
            response = await self.client.post('/tarot-action', json={'action': action},
                                               headers={'Origin': 'https://foreign.invalid'})
            self.assertEqual(response.status, 403)
        self.assertIsNone((await self.state())['current'])


if __name__ == '__main__':
    unittest.main()
