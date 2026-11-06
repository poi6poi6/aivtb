import asyncio
from dataclasses import asdict
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import httpx

from brain.events import Event
from brain.task_client import TaskClient
from brain.task_coordination import TaskCoordination
from brain.task_daemon import TaskDaemon


class Manager(TaskCoordination):
    def __init__(self):
        self.jobs = []
        self.intent_prompts = {}
        self.active_resources = set()
        self.cancel = AsyncMock()
        self.events = []
        self.cfg = SimpleNamespace(notification_retries=1)

    def status(self):
        return {'jobs': self.jobs, 'subscription': {'auth': 'unchecked', 'astra': False}}

    def resource_in_use(self, resource):
        return resource in self.active_resources

    def get(self, key):
        return next(job for job in self.jobs if job['id'] == key)

    def save(self, *jobs):
        pass

    async def handle_event(self, event):
        self.events.append(event)

    async def resolve_task_intent(self, event):
        self.events.append(event)


class DaemonTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.manager = Manager()
        self.daemon = TaskDaemon(self.manager)
        app = web.Application(client_max_size=16384)
        app.router.add_post('/task-rpc', self.daemon.rpc)
        app.router.add_get('/task-service-health', self.daemon.health)
        self.http = TestClient(TestServer(app))
        await self.http.start_server()
        self.count = 0

    async def asyncTearDown(self):
        await self.http.close()

    async def rpc(self, method, args=None, client='foreground', request_id=None, headers=None):
        self.count += 1
        return await self.http.post('/task-rpc', headers={
            'X-Task-Service': self.daemon.token, **(headers or {})}, json={
                'client': client, 'request_id': request_id or str(self.count),
                'method': method, 'args': args or {}})

    async def test_browser_cannot_gain_owner_authority_by_forging_payload(self):
        body = {'client': 'forged', 'request_id': 'one', 'method': 'handle_event',
                'args': {'event': asdict(Event('companion', 'chat', '/任务 test')),
                         'owner_control': True}}
        for headers in ({}, {'X-Task-Service': 'wrong'},
                        {'X-Task-Service': self.daemon.token, 'Origin': 'http://127.0.0.1'},
                        {'X-Task-Service': self.daemon.token, 'Host': 'evil.example'}):
            with self.subTest(headers=set(headers)):
                response = await self.http.post('/task-rpc', headers=headers, json=body)
                self.assertEqual(response.status, 403)
        self.assertEqual(self.manager.events, [])
        # A field nested in the event does not become host authority either.
        body['args']['event']['owner_control'] = True
        body['args'].pop('owner_control')
        response = await self.http.post('/task-rpc', json=body,
                                       headers={'X-Task-Service': self.daemon.token})
        self.assertEqual(response.status, 200)
        self.assertFalse(self.manager.events[-1].owner_control)

    async def test_detach_does_not_cancel_background_task(self):
        self.manager.jobs.append({'id': 'background', 'state': 'running'})
        await self.rpc('attach', {'resources': {'desktop': '游戏控制'}})
        response = await self.rpc('detach')
        self.assertTrue((await response.json())['result']['ok'])
        self.manager.cancel.assert_not_awaited()
        self.assertEqual(self.manager.jobs[0]['state'], 'running')

    async def test_stale_lease_blocks_background_until_explicit_release(self):
        response = await self.rpc('attach', {'resources': {'desktop': '游戏控制'}})
        self.assertEqual((await response.json())['result']['granted'], ['desktop'])
        self.daemon.lease['seen'] -= 20
        self.assertIn('连接中断', self.daemon.busy_resources()['desktop'])
        response = await self.rpc('attach', {'resources': {'desktop': '另一界面'}}, client='other')
        self.assertEqual(response.status, 400)
        self.assertIn('desktop', self.daemon.busy_resources())
        await self.daemon.release_stale()
        self.assertEqual(self.daemon.busy_resources(), {})

    async def test_live_lease_cannot_be_released_as_stale_or_grant_busy_executor(self):
        self.manager.active_resources.add('desktop')
        response = await self.rpc('attach', {'resources': {'desktop': '游戏控制', 'local_gpu': '语音'}})
        self.assertEqual((await response.json())['result']['granted'], ['local_gpu'])
        with self.assertRaisesRegex(ValueError, '仍在线'):
            await self.daemon.release_stale()

    async def test_reconnect_skips_historical_speech_and_poll_delivers_new_receipts(self):
        self.manager.event_queue.put_nowait(Event('system', 'task_notice', '旧回执'))
        response = await self.rpc('attach')
        result = (await response.json())['result']
        self.assertEqual(result['notices'], [])
        cursor = result['cursor']
        self.manager.event_queue.put_nowait(Event('system', 'task_notice', '新回执'))
        response = await self.rpc('poll', {'cursor': cursor})
        result = (await response.json())['result']
        self.assertEqual([notice['event']['text'] for notice in result['notices']], ['新回执'])
        await self.rpc('detach')
        response = await self.rpc('attach', client='new-foreground')
        self.assertEqual((await response.json())['result']['notices'], [])

    async def test_operation_exception_is_an_error_not_a_success_receipt(self):
        with patch('brain.task_daemon.task_action', AsyncMock(side_effect=RuntimeError('尚未执行'))):
            response = await self.rpc('action', {'action': 'apply_code', 'id': 'absent'})
        payload = await response.json()
        self.assertEqual(response.status, 400)
        self.assertEqual(payload['error'], '尚未执行')
        self.assertNotIn('result', payload)
        self.assertEqual(payload['status']['jobs'], [])

    async def test_feedback_cannot_mark_cancelled_revision_as_played(self):
        job = {'id': 'abc', 'state': 'cancelled', 'kind': 'research', 'revision': 3,
               'delivery': {'state': 'cancelled', 'revision': 3}}
        self.manager.jobs.append(job)
        for revision in (2, 3):
            response = await self.rpc('feedback', {'event': {'task_id': 'abc',
                'task_revision': revision, 'task_notice': True, 'task_ts': time.time()},
                'id': 'audio', 'state': 'played'})
            self.assertTrue((await response.json())['result']['ignored'])
        self.assertEqual(job['delivery']['state'], 'cancelled')

    async def test_cancel_request_stops_only_that_clients_inflight_semantic_work(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def resolve(event):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        self.manager.resolve_task_intent = resolve
        task = asyncio.create_task(self.rpc('resolve_intent', {'event':
            asdict(Event('companion', 'chat', '把背景换成夜晚'))}, request_id='semantic'))
        await asyncio.wait_for(entered.wait(), 1)
        response = await self.rpc('cancel_request', {'request_id': 'semantic'}, client='other')
        self.assertEqual(response.status, 200)
        self.assertFalse(cancelled.is_set())
        response = await self.rpc('cancel_request', {'request_id': 'semantic'})
        self.assertEqual(response.status, 200)
        await asyncio.wait_for(cancelled.wait(), 1)
        # The original HTTP response may close when its handler is cancelled.
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.manager.events, [])

    async def test_cancellation_before_request_arrival_prevents_late_execution(self):
        await self.rpc('cancel_request', {'request_id': 'late'})
        response = await self.rpc('resolve_intent', {'event':
            asdict(Event('companion', 'chat', '停止刚才那个'))}, request_id='late')
        self.assertGreaterEqual(response.status, 400)
        self.assertEqual(self.manager.events, [])

    async def test_completed_request_id_is_not_executed_twice(self):
        action = AsyncMock(return_value={'ok': True})
        with patch('brain.task_daemon.task_action', action):
            first = await self.rpc('action', {'action': 'cancel'}, request_id='once')
            second = await self.rpc('action', {'action': 'cancel'}, request_id='once')
        self.assertEqual(first.status, 200)
        self.assertGreaterEqual(second.status, 400)
        action.assert_awaited_once()


class ClientProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.client = TaskClient(self.tmp.name, SimpleNamespace(enabled=True))
        self.client.folder.mkdir(parents=True)
        (self.client.folder / 'service.token').write_text('unit-token', encoding='utf-8')
        await self.client.http.aclose()

    async def asyncTearDown(self):
        await self.client.http.aclose()
        self.tmp.cleanup()

    def transport(self, payload, status=200):
        self.client.http = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(status, json=payload)))

    @staticmethod
    def payload(**values):
        return {'boot': 'boot', 'sequence': 1, 'status': {'jobs': [],
            'subscription': {'astra': True}}, 'result': {'ok': True}, **values}

    async def test_rpc_error_never_becomes_success_or_automatic_retry(self):
        calls = []

        def fail(request):
            calls.append(request)
            raise httpx.ReadError('lost receipt', request=request)
        self.client.http = httpx.AsyncClient(transport=httpx.MockTransport(fail))
        self.client.granted.add('desktop')
        with self.assertRaisesRegex(RuntimeError, '不会自动重试'):
            await self.client.rpc('action', {'action': 'submit'})
        self.assertEqual(len(calls), 1)
        self.assertFalse(self.client.connected)
        self.assertEqual(self.client.granted, set())

    async def test_malformed_snapshot_revokes_grants_instead_of_killing_poller(self):
        self.transport(self.payload(sequence='invalid'))
        self.client.granted.add('desktop')
        with self.assertRaises(RuntimeError):
            await self.client.rpc('poll')
        self.assertFalse(self.client.connected)
        self.assertEqual(self.client.granted, set())

    async def test_retired_service_response_cannot_restore_pre_restart_state(self):
        self.client.adopt(self.payload(boot='before-restart', sequence=20))
        self.client.adopt(self.payload(boot='after-restart', sequence=1))
        with self.assertRaises(ValueError):
            self.client.adopt(self.payload(boot='before-restart', sequence=21))
        self.assertEqual(self.client.boot, 'after-restart')
        self.assertEqual(self.client.sequence, 1)

    async def test_cached_resource_grant_expires_closed(self):
        self.client.resource_busy = lambda: {'desktop': '游戏'}
        self.client.connected = True
        self.client.granted.add('desktop')
        self.client.last_sync = time.monotonic()
        self.assertFalse(self.client.resource_in_use('desktop'))
        self.client.last_sync -= 3
        self.assertTrue(self.client.resource_in_use('desktop'))
        self.client.connected = False
        self.client.last_sync = time.monotonic()
        self.assertTrue(self.client.resource_in_use('desktop'))

    async def test_close_detaches_without_stopping_executor(self):
        self.transport(self.payload())
        self.client.rpc = AsyncMock(return_value={'ok': True})
        await self.client.close()
        self.assertEqual([call.args[0] for call in self.client.rpc.await_args_list], ['detach'])


if __name__ == '__main__':
    unittest.main()
