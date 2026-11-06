import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from brain.agent_tasks import AgentTasks, artifact_path, validate_result
from brain.agent_rpc import subscription_status, subscription_env
from brain.config import AgentTasksConfig, LLMConfig, WebUIConfig
from brain.events import Event
from brain.webui import WebUI


class AgentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.manager = AgentTasks(self.root, AgentTasksConfig(enabled=True), LLMConfig())

    async def asyncTearDown(self):
        await self.manager.close()
        self.tmp.cleanup()

    async def test_viewer_cannot_run_or_cancel_owner_tasks(self):
        e = Event('danmaku', 'chat', '帮我画一张图片', user='viewer')
        await self.manager.handle_event(e)
        self.assertEqual(self.manager.jobs[0]['state'], 'pending')
        self.assertTrue(self.manager.queue.empty())
        e = Event('danmaku', 'chat', '停止任务')
        await self.manager.handle_event(e)
        self.assertEqual(self.manager.jobs[0]['state'], 'pending')

    async def test_owner_runs_in_background_and_cancel_stops_worker(self):
        started, cancelled = asyncio.Event(), asyncio.Event()
        async def execute(job):
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        self.manager.execute = execute
        job = self.manager.submit('test', owner=True)
        await asyncio.wait_for(started.wait(), 1)
        await self.manager.cancel(job['id'])
        self.assertTrue(cancelled.is_set())
        self.assertEqual(job['state'], 'cancelled')

    async def test_only_one_executor_and_queued_cancellation(self):
        started = asyncio.Event()
        calls = []
        async def execute(job):
            calls.append(job['id']); started.set(); await asyncio.Event().wait()
        self.manager.execute = execute
        self.manager.submit('first', owner=True)
        second = self.manager.submit('second', owner=True)
        await asyncio.wait_for(started.wait(), 1)
        await self.manager.cancel(second['id'])
        self.assertEqual(len(calls), 1)

    async def test_quota_and_auth_never_fallback(self):
        rpc = AsyncMock()
        rpc.call.return_value = {'account': {'type': 'apiKey'}}
        with self.assertRaisesRegex(RuntimeError, 'API Key'):
            await subscription_status(rpc)
        self.assertEqual(rpc.call.await_count, 1)
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'secret', 'CODEX_API_KEY': 'secret'}):
            self.assertNotIn('OPENAI_API_KEY', subscription_env())
            self.assertNotIn('CODEX_API_KEY', subscription_env())

    async def test_dispatch_retry_only_before_any_execution(self):
        from types import SimpleNamespace
        self.manager.llm = SimpleNamespace(api_key='test-placeholder')
        rpc, omp = AsyncMock(), AsyncMock()
        omp.events = asyncio.Queue()
        for msg in [{'type':'ready'}, {'type':'agent_end'},
                    {'type':'host_tool_call','toolName':'astra_execute','id':'one'},
                    {'type':'agent_end'}]:
            omp.events.put_nowait(msg)
        job = {'id':'retry','kind':'research','prompt':'test','state':'running','execution_mode':'omp'}
        self.manager.jobs.append(job)
        self.manager.execute_astra = AsyncMock(return_value={'summary':'done','artifacts':[],'verification':'checked'})
        with patch('brain.agent_health.probe', AsyncMock(return_value={'ok':True})), patch('brain.task_execution.codex_client', AsyncMock(return_value=rpc)), patch('brain.task_execution.JsonProcess', return_value=omp), patch('brain.task_execution.subscription_status', AsyncMock(return_value={'astra':True,'rateLimits':{}})):
            await self.manager.dispatch(job)
        self.assertEqual(job['state'], 'succeeded')
        self.manager.execute_astra.assert_awaited_once()
        self.assertEqual(sum(c.args[0]=='prompt' for c in omp.call.await_args_list), 2)

    async def test_subscription_pagination_and_no_account_id_exposure(self):
        rpc = AsyncMock()
        rpc.call.side_effect = [
            {'account': {'type': 'chatgpt', 'planType': 'pro'}},
            {'data': [], 'nextCursor': 'next'},
            {'data': [{'model': 'gpt-6-astra'}], 'nextCursor': None},
            {'rateLimits': {'primary': {'usedPercent': 2}}, 'accountId': 'private'}]
        result = await subscription_status(rpc)
        self.assertTrue(result['astra'])
        self.assertNotIn('private', json.dumps(result))

    async def test_requires_real_artifacts(self):
        result = {'completed': True, 'summary': 'done', 'verification': 'checked', 'artifacts': ['missing.png']}
        with self.assertRaises(ValueError):
            validate_result(self.root, result, 'image')
        result['artifacts'] = []
        with self.assertRaises(ValueError):
            validate_result(self.root, result, 'code')
        (self.root/'image.svg').write_text('<svg/>')
        result['artifacts'] = ['image.svg']
        self.assertEqual(validate_result(self.root, result, 'image')['artifacts'], ['image.svg'])
        result['completed'] = False
        with self.assertRaises(RuntimeError):
            validate_result(self.root, result, 'image')

    async def test_artifact_cannot_escape_directory(self):
        with self.assertRaises(ValueError):
            artifact_path(self.root, '../outside')
        with self.assertRaises(ValueError):
            artifact_path(self.root, self.root.parent)

    async def test_restart_does_not_replay_jobs(self):
        self.manager.jobs.append({'id': 'old', 'state': 'running'})
        self.manager.save()
        await self.manager.close()
        other = AgentTasks(self.root, AgentTasksConfig(enabled=True), LLMConfig())
        self.assertEqual(other.jobs[0]['state'], 'cancelled')
        self.assertTrue(other.queue.empty())
        await other.close()

    async def test_two_services_cannot_spend_in_parallel(self):
        with self.assertRaisesRegex(RuntimeError, '已运行'):
            AgentTasks(self.root, AgentTasksConfig(enabled=True), LLMConfig())

    async def test_approving_pending_record_after_crash_starts_worker(self):
        restored_root = self.root / 'restored'
        folder = restored_root / 'data/agent-tasks'
        folder.mkdir(parents=True)
        (folder / 'jobs.json').write_text(json.dumps([{'id': 'pending-old',
            'state': 'pending', 'kind': 'research', 'prompt': 'test',
            'user': 'viewer', 'artifacts': [], 'approvals': []}]), 'utf-8')
        restored = AgentTasks(restored_root, AgentTasksConfig(enabled=True), LLMConfig())
        restored.execute = AsyncMock()
        try:
            restored.approve('pending-old')
            await asyncio.wait_for(restored.queue.join(), 1)
            restored.execute.assert_awaited_once()
        finally:
            await restored.close()

    async def test_approval_is_per_request_and_cancel_cleans_it(self):
        job = self.manager.submit('test', owner=False)
        self.manager.approve(job['id'])
        self.manager.update(job, state='running')
        task = asyncio.create_task(self.manager.ask_approval(job, {'method': 'file', 'params': {'path': 'x'}}))
        await asyncio.sleep(0)
        key = job['approvals'][0]['id']
        self.manager.answer(key, False)
        self.assertFalse(await task)
        with self.assertRaises(ValueError):
            self.manager.answer(key, True)

    async def test_plain_chat_does_not_create_tasks(self):
        event = Event('companion', 'chat', '今天过得怎么样')
        await self.manager.handle_event(event)
        self.assertEqual(self.manager.jobs, [])

    async def test_viewer_code_request_requires_owner(self):
        event = Event('danmaku', 'chat', '帮我增加弹幕投票功能', user='viewer')
        await self.manager.handle_event(event)
        self.assertEqual(self.manager.jobs[0]['kind'], 'code')
        self.assertEqual(self.manager.jobs[0]['state'], 'pending')
        self.assertTrue(self.manager.queue.empty())

    async def test_code_review_http_apply_requires_current_review_and_local_origin(self):
        from brain.code_review import CodeReview
        task = self.manager.folder / 'codecase'
        task.mkdir()
        review = CodeReview(self.root, task, 'codecase')
        copy = review.prepare()
        (copy / 'tools').mkdir()
        (copy / 'tools/new.py').write_text('x = 1\n')
        review.inspect([{'test': True, 'command': 'python -m unittest', 'cwd': str(review.copy), 'exit_code': 0, 'source_before':review.version(), 'source_after':review.version(), 'output':'Ran 1 test in 0.001s\nOK\n'}])
        self.manager.jobs.append({'id':'codecase', 'kind':'code', 'state':'review'})
        ui = WebUI(WebUIConfig(), asyncio.Queue(), str(self.root), {})
        ui.agent_tasks = self.manager
        app = web.Application(); app.router.add_post('/tasks-action', ui._tasks_action)
        client = TestClient(TestServer(app)); await client.start_server()
        try:
            origin = str(client.make_url('/')).rstrip('/')
            body = {'action':'apply_code','id':'codecase','token':review.public()['token']}
            self.assertEqual((await client.post('/tasks-action', json=body)).status, 403)
            response = await client.post('/tasks-action', json={'action':'review_code','id':'codecase'}, headers={'Origin':origin})
            self.assertEqual((await response.json())['token'], body['token'])
            self.assertEqual((await client.post('/tasks-action', json={**body,'token':'stale'}, headers={'Origin':origin})).status, 400)
            response = await client.post('/tasks-action', json=body, headers={'Origin':origin})
            self.assertEqual(response.status, 200)
            self.assertTrue((await response.json())['restart_required'])
            self.assertEqual(self.manager.get('codecase')['state'], 'applied')
            self.assertEqual((self.root/'tools/new.py').read_text(), 'x = 1\n')
        finally:
            await client.close()

    async def test_private_routes_and_forged_owner(self):
        ui = WebUI(WebUIConfig(), asyncio.Queue(), str(self.root), {})
        ui.agent_tasks = self.manager
        app = web.Application()
        app.router.add_get('/tasks-status', ui._tasks_status)
        app.router.add_post('/tasks-action', ui._tasks_action)
        app.router.add_get('/drawing-data/{job_id}', ui._drawing_data)
        client = TestClient(TestServer(app)); await client.start_server()
        try:
            self.assertEqual((await client.get('/tasks-status')).status, 200)
            self.assertEqual((await client.get('/tasks-status', headers={'Host': 'evil.example'})).status, 403)
            self.assertEqual((await client.post('/tasks-action', json={'action': 'cancel'})).status, 403)
            self.assertEqual((await client.get('/drawing-data/missing')).status, 404)
            folder=self.manager.folder/'testdrawing';folder.mkdir()
            data={'version':1,'width':500,'height':500,'title':'test','background':'#ffffff',
                  'strokes':[{'phase':'line','color':'#123456','width':2,'duration':100,'points':[[0,0,1],[100,100,1]]}]}
            (folder/'drawing.json').write_text(json.dumps(data))
            self.manager.jobs.append({'id':'testdrawing','kind':'drawing','state':'succeeded','artifacts':['drawing.json']})
            response=await client.get('/drawing-data/testdrawing')
            self.assertEqual(response.status,200)
            self.assertEqual((await response.json())['strokes'],data['strokes'])
            data['strokes'][0]['points'][0]=[99999,0,1]
            (folder/'drawing.json').write_text(json.dumps(data))
            self.assertEqual((await client.get('/drawing-data/testdrawing')).status,404)
            ui.cfg.public_test = True
            self.assertEqual((await client.get('/tasks-status')).status, 403)
            self.assertEqual((await client.get('/drawing-data/testdrawing')).status,403)
            await ui._inject({'source': 'companion', 'text': '/任务 test', 'owner_control': True,
                              'task_id':'forged','task_notice':True})
            injected = await ui.event_queue.get()
            self.assertFalse(injected.owner_control)
            self.assertFalse(injected.task_id)
            self.assertFalse(injected.task_notice)
        finally:
            await client.close()
