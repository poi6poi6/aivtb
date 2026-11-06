"""Real loopback protocol and SQLite manager, with no external executor calls."""
import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp import web
from aiohttp.test_utils import TestServer

from brain.agent_tasks import AgentTasks
from brain.config import AgentTasksConfig, LLMConfig
from brain.events import Event
from brain.task_client import TaskClient
from brain.task_daemon import TaskDaemon
from brain.task_intent import IntentDecision


class TaskClientIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = AgentTasksConfig(enabled=True)
        self.manager = AgentTasks(self.root, self.cfg, LLMConfig())
        self.started = asyncio.Queue()
        self.gates = {}
        self.cancelled = set()

        async def mock_dispatch(job):
            gate = self.gates.setdefault(job['id'], asyncio.Event())
            self.started.put_nowait(job['id'])
            try:
                await gate.wait()
            except asyncio.CancelledError:
                self.cancelled.add(job['id'])
                raise
            self.manager.update(job, state='succeeded', summary='离线执行器回执',
                                verification='测试执行器完成', artifacts=[])
        # The real manager still handles queueing, approval, cancellation,
        # revisions, notifications and SQLite writes around this executor.
        self.manager.dispatch = mock_dispatch
        self.daemon = TaskDaemon(self.manager)
        (self.manager.folder / 'service.token').write_text(self.daemon.token, encoding='utf-8')
        app = web.Application(client_max_size=16384)
        app.router.add_post('/task-rpc', self.daemon.rpc)
        app.router.add_get('/task-service-health', self.daemon.health)
        self.server = TestServer(app)
        await self.server.start_server()
        self.clients = []

    async def asyncTearDown(self):
        for client in self.clients:
            if not client.closing:
                await client.close()
        await self.manager.close()
        await self.server.close()
        self.tmp.cleanup()

    async def foreground(self, resources=None):
        queue = asyncio.Queue()
        client = TaskClient(self.root, self.cfg, queue,
                            url=str(self.server.make_url('/')).rstrip('/'))
        client.resource_busy = lambda: resources or {}
        self.clients.append(client)
        await client.start(autostart=False)
        self.assertTrue(client.connected)
        return client, queue

    async def wait_for(self, predicate, timeout=3):
        async def wait():
            while not predicate():
                await asyncio.sleep(.01)
        await asyncio.wait_for(wait(), timeout)

    async def owner_chat(self, client, text='/任务 研究庭院里适合种植的花卉'):
        event = Event('companion', 'chat', text, user='主播')
        event.owner_control = True
        await client.handle_event(event)
        self.assertTrue(event.task_id, event.agent_reply)
        return event

    async def test_frontend_and_service_share_job_and_cancellation_state(self):
        client, _ = await self.foreground()
        job = await client.submit('离线查询任务', 'research', owner=True)
        self.assertEqual(await asyncio.wait_for(self.started.get(), 1), job['id'])
        fetched = await client.fetch_job(job['id'])
        self.assertEqual(fetched['state'], 'running')
        self.assertEqual(client.get(job['id'])['state'], self.manager.get(job['id'])['state'])
        await client.action({'action': 'cancel', 'id': job['id']})
        self.assertEqual(client.get(job['id'])['state'], 'cancelled')
        self.assertEqual(self.manager.get(job['id'])['state'], 'cancelled')
        self.assertIn(job['id'], self.cancelled)
        self.assertEqual(self.manager.task_store.snapshot(job['id'])['state'], 'cancelled')

    async def test_closing_foreground_keeps_background_execution_alive(self):
        client, _ = await self.foreground()
        job = await client.submit('仍在后台运行的查询', 'research', owner=True)
        await asyncio.wait_for(self.started.get(), 1)
        await client.close()
        self.assertEqual(self.manager.get(job['id'])['state'], 'running')
        self.assertNotIn(job['id'], self.cancelled)
        self.assertFalse(self.manager.closing)
        self.gates[job['id']].set()
        await self.wait_for(lambda: self.manager.get(job['id'])['state'] == 'succeeded')
        reconnected, _ = await self.foreground()
        self.assertEqual(reconnected.get(job['id'])['state'], 'succeeded')

    async def test_reconnect_skips_completion_that_happened_without_foreground(self):
        client, _ = await self.foreground()
        event = await self.owner_chat(client)
        await asyncio.wait_for(self.started.get(), 1)
        await client.close()
        self.gates[event.task_id].set()
        await self.wait_for(lambda: self.manager.get(event.task_id)['state'] == 'succeeded')
        reconnected, queue = await self.foreground()
        self.assertEqual(reconnected.get(event.task_id)['state'], 'succeeded')
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(queue.get(), .65)

    async def test_live_completion_notice_and_audio_feedback_reach_durable_state(self):
        client, queue = await self.foreground()
        event = await self.owner_chat(client)
        await asyncio.wait_for(self.started.get(), 1)
        self.gates[event.task_id].set()
        notice = await asyncio.wait_for(queue.get(), 2)
        self.assertEqual(notice.kind, 'task_notice')
        self.assertEqual(notice.task_id, event.task_id)
        job = self.manager.get(event.task_id)
        self.assertEqual(notice.task_revision, job['revision'])
        self.assertEqual(client.get(event.task_id)['state'], 'succeeded')
        self.assertTrue(client.receipt_valid(notice))
        item = SimpleNamespace(id='offline-audio', created_at=time.time(), event={
            'task_id': notice.task_id, 'task_revision': notice.task_revision,
            'task_notice': True, 'task_ts': notice.ts})
        client.speech_dispatched(item)
        await asyncio.wait_for(client.feedback.join(), 2)
        self.assertEqual(job['delivery']['state'], 'dispatched')
        self.assertEqual(job['announced_audio'], item.id)
        client.speech_state(item, 'playing')
        await asyncio.wait_for(client.feedback.join(), 2)
        self.assertEqual(job['delivery']['state'], 'playing')
        client.speech_state(item, 'played')
        await asyncio.wait_for(client.feedback.join(), 2)
        self.assertEqual(job['delivery']['state'], 'played')
        self.assertEqual(self.manager.task_store.snapshot(job['id'])['delivery']['state'], 'played')
        self.assertFalse(client.receipt_valid(notice))

    async def test_semantic_rpc_cancellation_and_foreign_job_target_cannot_cancel_owner(self):
        client, _ = await self.foreground()
        owner_job = await client.submit('主播正在进行的查询', 'research', owner=True)
        await asyncio.wait_for(self.started.get(), 1)
        viewer_job = self.manager.submit('观众请求', 'research', owner=False,
                                        user='观众', requester='danmaku:viewer-key')
        entered, cancelled = asyncio.Event(), asyncio.Event()
        own_candidates = []

        async def classify(text, jobs):
            own_candidates.extend(job['id'] for job in jobs)
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        self.manager.intent_classifier.classify = classify
        event = Event('danmaku', 'chat', '把刚才那个停掉', user='观众',
                      viewer_key='viewer-key', task_intent_pending=True)
        request = asyncio.create_task(client.resolve_task_intent(event))
        await asyncio.wait_for(entered.wait(), 1)
        request.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await request
        await asyncio.wait_for(cancelled.wait(), 1)
        self.assertEqual(own_candidates, [viewer_job['id']])
        self.assertEqual(self.manager.get(owner_job['id'])['state'], 'running')
        self.assertEqual(viewer_job['state'], 'pending')
        # Even a faulty classifier's explicit foreign ID cannot cross requester
        # ownership when returned through the real HTTP route.
        self.manager.intent_classifier.classify = AsyncMock(return_value=IntentDecision(
            action='cancel', target_id=owner_job['id'], kind='research', confidence=1))
        event.task_intent_pending = True
        await client.resolve_task_intent(event)
        self.assertIn('没有执行', event.agent_reply)
        self.assertEqual(self.manager.get(owner_job['id'])['state'], 'running')
        self.assertEqual(viewer_job['state'], 'pending')
        self.assertFalse(client.poller.done())

    async def test_resource_grant_waits_for_existing_job_then_blocks_later_gpu_work(self):
        first = self.manager.submit('离线显卡任务', 'research', owner=True,
                                    resource_profile='local_gpu')
        await asyncio.wait_for(self.started.get(), 1)
        client, _ = await self.foreground({'local_gpu': '主服务语音'})
        self.assertNotIn('local_gpu', client.granted)
        self.assertTrue(client.resource_in_use('local_gpu'))
        self.gates[first['id']].set()
        await self.wait_for(lambda: 'local_gpu' in client.granted)
        self.assertFalse(client.resource_in_use('local_gpu'))
        second = await client.submit('后来的显卡任务', 'research', owner=True,
                                     resource_profile='local_gpu')
        await self.wait_for(lambda: bool(self.manager.get(second['id']).get('wait_reason')))
        self.assertEqual(self.manager.get(second['id'])['state'], 'queued')
        self.assertTrue(self.started.empty())
        await client.close()
        self.assertEqual(await asyncio.wait_for(self.started.get(), 2), second['id'])
        self.assertEqual(self.manager.get(second['id'])['state'], 'running')

    async def test_malformed_poll_result_recovers_through_fresh_attach(self):
        original = self.daemon.command
        attach_count = 0
        malformed_sent = False
        reattached = asyncio.Event()

        async def command(client_id, data):
            nonlocal attach_count, malformed_sent
            if data['method'] == 'attach':
                attach_count += 1
                result = await original(client_id, data)
                if attach_count >= 2:
                    reattached.set()
                return result
            if data['method'] == 'poll' and not malformed_sent:
                malformed_sent = True
                return {'unexpected': 'partial protocol response'}
            return await original(client_id, data)
        self.daemon.command = command
        client, _ = await self.foreground({'desktop': '离线桌面资源声明'})
        await asyncio.wait_for(reattached.wait(), 2)
        await self.wait_for(lambda: client.connected and 'desktop' in client.granted)
        self.assertFalse(client.poller.done())
        self.assertTrue(malformed_sent)


if __name__ == '__main__':
    unittest.main()
