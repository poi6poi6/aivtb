import asyncio
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from brain.agent_tasks import AgentTasks
from brain.config import AgentTasksConfig, LLMConfig, WebUIConfig
from brain.live_polls import LivePolls
from brain.show_sessions import ShowSessions, poll_commission
from brain.task_actions import task_action
from brain.webui import WebUI


class ShowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.store = ShowSessions(self.path / 'shows.db')
        self.polls = LivePolls(self.path / 'polls.db')

    def poll(self, choice=None):
        p = self.polls.create(program='庭院连载', question='添什么？', options=['温室', '书房'], mode='test')
        if choice:
            self.polls.test_vote(p['id'], '测试账号', str(choice))
        return self.polls.finish(p['id'])

    def test_choices_are_truthful_and_require_host_for_zero_or_tie(self):
        zero = self.poll()
        for choice in (None, True, '1', 0, 3):
            with self.assertRaises(ValueError):
                poll_commission(zero, choice)
        self.assertIn('零票', poll_commission(zero, 1)['decision'])
        winner = self.poll(2)
        self.assertEqual(poll_commission(winner)['choice'], 2)
        self.assertEqual(poll_commission(winner)['decision'], '观众最高票')
        self.assertEqual(poll_commission(winner, 1)['decision'], '主播选择')
        tied = deepcopy(winner)
        tied['winners'] = [1, 2]
        with self.assertRaises(ValueError):
            poll_commission(tied)
        self.assertEqual(poll_commission(tied, 1)['decision'], '主播选择')
        for state in ('open', 'cancelled'):
            tied['state'] = state
            with self.assertRaises(ValueError):
                poll_commission(tied, 1)

    def test_durable_cards_recap_and_continuation_keep_versions(self):
        show = self.store.start('今晚的庭院')
        show = self.store.add(show['id'], show['version'], '画温室', poll=self.poll(1))
        card = show['cards'][0]
        show = self.store.edit(show['id'], show['version'], card['id'], {'prompt': '第一行\n第二行', 'name': '冬日之窗', 'next_todo': '加一盏灯'})
        submission = self.store.begin_submission(show['id'], show['version'], card['id'])
        # Reopening the database preserves the pending command and its exact key.
        self.store = ShowSessions(self.path / 'shows.db')
        pending = self.store.get(show['id'])
        retry = self.store.begin_submission(show['id'], pending['version'], card['id'])
        self.assertEqual(retry, submission)
        job = {'id': 'drawing1', 'drawing_root': 'drawing1', 'kind': 'drawing', 'commission_key': submission['key']}
        show = self.store.submitted(show['id'], card['id'], submission['key'], job)
        drawing = {'root': 'drawing1', 'latest': {'id': 'drawing2', 'state': 'succeeded'},
                   'drawing': {'job_id': 'drawing2', 'version': 'abc123', 'stage': 'sketch', 'title': '温室'}}
        closed = self.store.record_drawings(show['id'], show['version'], {'drawing1': drawing}, close=True)
        self.assertIn('测试', closed['recap'])
        self.assertIn('草稿', closed['recap'])
        self.assertNotIn('上色版本', closed['recap'])
        self.assertIn('冬日之窗', closed['recap'])
        next_show = self.store.start('下场继续', closed['id'])
        continued = next_show['cards'][0]
        self.assertNotEqual(continued['id'], card['id'])
        self.assertEqual(continued['drawing_root'], 'drawing1')
        self.assertEqual(continued['drawing']['drawing']['version'], 'abc123')
        self.assertEqual(continued['note'], '加一盏灯')
        self.assertEqual(continued['next_todo'], '')
        self.assertEqual(self.store.get(closed['id'])['recap'], closed['recap'])
        next_closed = self.store.record_drawings(next_show['id'], next_show['version'], {'drawing1': drawing}, close=True)
        self.assertIn('沿用上场投票结果', next_closed['recap'])
        self.assertIn(closed['id'], next_closed['recap'])
        self.assertIn('不计为本场参与', next_closed['recap'])
        with self.assertRaises(ValueError):
            self.store.edit(closed['id'], closed['version'], card['id'], {'name': '篡改'})

    def test_stale_edits_pending_requests_and_duplicate_polls_are_rejected(self):
        show = self.store.start('主题')
        p = self.poll(1)
        updated = self.store.add(show['id'], show['version'], '栏目', poll=p)
        with self.assertRaises(ValueError):
            self.store.add(show['id'], show['version'], '迟到重复请求')
        with self.assertRaises(ValueError):
            self.store.add(show['id'], updated['version'], '同轮另一张', poll=p)
        card = updated['cards'][0]
        self.store.begin_submission(show['id'], updated['version'], card['id'])
        pending = self.store.get(show['id'])
        with self.assertRaises(ValueError):
            self.store.edit(show['id'], pending['version'], card['id'], {'prompt': '偷偷换要求'})
        with self.assertRaises(ValueError):
            self.store.record_drawings(show['id'], pending['version'], {}, close=True)
        with self.assertRaises(ValueError):
            self.store.start('重复开播')

    def test_unknown_drawing_state_does_not_claim_completion(self):
        show = self.store.start('断线收播')
        show = self.store.add(show['id'], show['version'], '委托', '画树')
        card = show['cards'][0]
        sub = self.store.begin_submission(show['id'], show['version'], card['id'])
        show = self.store.submitted(show['id'], card['id'], sub['key'],
                                    {'id': 'x', 'kind': 'drawing', 'commission_key': sub['key']})
        closed = self.store.record_drawings(show['id'], show['version'], {'x': {'error': 'offline'}}, close=True)
        self.assertIn('未确认', closed['recap'])
        self.assertNotIn('任务已完成', closed['recap'])

    def test_explicit_unconfirmed_close_preserves_key_without_claiming_cancel(self):
        show = self.store.start('服务断线')
        show = self.store.add(show['id'], show['version'], '画树', '画一棵树')
        card_id = show['cards'][0]['id']
        sub = self.store.begin_submission(show['id'], show['version'], card_id)
        pending = self.store.get(show['id'])
        with self.assertRaises(ValueError):
            self.store.record_drawings(show['id'], pending['version'], {}, close=True)
        closed = self.store.record_drawings(show['id'], pending['version'], {}, close=True, allow_unconfirmed=True)
        self.assertEqual(closed['cards'][0]['submission']['key'], sub['key'])
        self.assertIn('提交未确认', closed['recap'])
        self.assertNotIn('绘画：未提交', closed['recap'])
        self.assertIn('不表示后台任务未创建或已取消', closed['recap'])
        with self.assertRaises(ValueError):
            self.store.submitted(show['id'], card_id, sub['key'],
                {'id': 'x', 'kind': 'drawing', 'commission_key': sub['key']})
        self.assertEqual(self.store.get(show['id']), closed)
        self.assertEqual(self.store.start('下一场')['state'], 'open')


class ShowHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ui = WebUI(WebUIConfig(), asyncio.Queue(), self.tmp.name, {})
        self.ui.show_sessions = ShowSessions(self.root / 'shows.db')
        self.ui.live_polls = LivePolls(self.root / 'polls.db')
        self.manager = AgentTasks(self.root, AgentTasksConfig(enabled=True), LLMConfig())
        self.ui.agent_tasks = self.manager
        self.enqueue = patch.object(self.manager, '_enqueue_submission')
        self.enqueue.start()
        self.addCleanup(self.enqueue.stop)
        self.addAsyncCleanup(self.manager.close)
        app = web.Application()
        app.router.add_get('/shows', self.ui._shows_page)
        app.router.add_get('/shows-status', self.ui._shows_status)
        app.router.add_get('/shows-poll/{poll_id}', self.ui._shows_poll)
        app.router.add_get('/shows-drawing', self.ui._shows_drawing_page)
        app.router.add_get('/shows-drawing-data/{show_id}/{card_id}', self.ui._shows_drawing_data)
        app.router.add_post('/shows-action', self.ui._shows_action)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def action(self, **data):
        return await self.client.post('/shows-action', json=data,
            headers={'Origin': str(self.client.make_url('/')).rstrip('/')})

    async def test_complete_ui_api_workflow_and_lost_response_retry(self):
        show = await (await self.action(action='start', theme='直播主题')).json()
        show = await (await self.action(action='add', id=show['id'], version=show['version'], title='画一棵树', prompt='春天的树')).json()
        card_id = show['cards'][0]['id']
        original = self.ui._show_task_action
        async def lost(data):
            await original(data)
            raise RuntimeError('模拟已创建，但回执丢失')
        with patch.object(self.ui, '_show_task_action', side_effect=lost):
            response = await self.action(action='submit', id=show['id'], version=show['version'], card_id=card_id)
        self.assertEqual(response.status, 503)
        self.assertEqual(len(self.manager.jobs), 1)
        show = (await (await self.client.get('/shows-status')).json())['current']
        self.assertEqual(show['cards'][0]['submission']['state'], 'pending')
        show = await (await self.action(action='submit', id=show['id'], version=show['version'], card_id=card_id)).json()
        self.assertEqual(len(self.manager.jobs), 1)
        root = show['cards'][0]['drawing_root']
        self.assertEqual(root, self.manager.jobs[0]['id'])
        show = await (await self.action(action='sync', id=show['id'], version=show['version'])).json()
        self.assertEqual(show['cards'][0]['drawing']['latest']['state'], 'queued')
        show = await (await self.action(action='edit', id=show['id'], version=show['version'], card_id=card_id, next_todo='下场加小鸟')).json()
        closed = await (await self.action(action='close', id=show['id'], version=show['version'])).json()
        self.assertIn('排队中', closed['recap'])
        self.assertNotIn('任务已完成', closed['recap'])
        next_show = await (await self.action(action='start', theme='继续画树', previous=closed['id'])).json()
        self.assertEqual(next_show['cards'][0]['drawing_root'], root)

    async def test_submission_dedup_survives_task_service_restart(self):
        key = 'a' * 32
        first = await task_action(self.manager, {'action': 'submit_commission', 'key': key, 'prompt': '画树'})
        await self.manager.close()
        manager = AgentTasks(self.root, AgentTasksConfig(enabled=True), LLMConfig())
        try:
            with patch.object(manager, '_enqueue_submission') as enqueue:
                repeated = await task_action(manager, {'action': 'submit_commission', 'key': key, 'prompt': '画树'})
                self.assertEqual(first['id'], repeated['id'])
                self.assertEqual(len(manager.jobs), 1)
                enqueue.assert_not_called()
                with self.assertRaises(ValueError):
                    await task_action(manager, {'action': 'submit_commission', 'key': key, 'prompt': '画猫'})
        finally:
            await manager.close()

    async def test_permissions_and_poll_preview(self):
        p = self.ui.live_polls.create(program='庭院', question='选什么？', options=['温室', '书房'])
        self.assertEqual((await self.client.get('/shows-poll/' + p['id'])).status, 400)
        self.ui.live_polls.finish(p['id'])
        preview = await (await self.client.get('/shows-poll/' + p['id'])).json()
        self.assertIsNone(preview['choice'])
        self.assertEqual(preview['prompt'], '')
        self.assertEqual((await self.client.post('/shows-action', json={'action': 'start', 'theme': 'x'})).status, 403)
        self.assertEqual((await self.client.get('/shows-status', headers={'Host': 'evil.example'})).status, 403)
        self.ui.cfg.public_test = True
        for path in ('/shows', '/shows-status', '/shows-poll/' + p['id'], '/shows-drawing', '/shows-drawing-data/a/b'):
            self.assertEqual((await self.client.get(path)).status, 403)
        self.assertEqual((await self.action(action='start', theme='x')).status, 403)

    async def test_history_reads_frozen_artifact_even_after_live_revision_changes(self):
        store = self.ui.show_sessions
        show = store.start('第一场')
        show = store.add(show['id'], show['version'], '作品', '画树')
        card_id = show['cards'][0]['id']
        sub = store.begin_submission(show['id'], show['version'], card_id)
        job = self.manager.submit_commission(sub['prompt'], sub['key'])
        document = {'version': 1, 'width': 256, 'height': 256, 'background': '#ffffff', 'title': '旧版本',
            'strokes': [{'phase': '轮廓', 'color': '#123456', 'width': 2, 'duration': 80,
                         'points': [[10, 10, 1], [100, 100, 1]]}]}
        def complete(task, data):
            folder = self.manager.folder / task['id']
            folder.mkdir()
            (folder / 'drawing.json').write_text(json.dumps(data), 'utf-8')
            self.manager.task_service.update(task, state='running')
            self.manager.task_service.update(task, state='succeeded', artifacts=['drawing.json'])
        complete(job, document)
        show = store.submitted(show['id'], card_id, sub['key'], job)
        drawing = await task_action(self.manager, {'action': 'drawing_state', 'id': job['id']})
        closed = store.record_drawings(show['id'], show['version'], {job['id']: drawing}, close=True)
        child = self.manager.submit_commission('修改树', 'b' * 32)
        complete(child, {**document, 'title': '新版本'})
        self.manager.task_service.update(job, superseded_by=child['id'])
        live = await task_action(self.manager, {'action': 'drawing_state', 'id': job['id']})
        self.assertEqual(live['drawing']['title'], '新版本')
        route = f"/shows-drawing-data/{closed['id']}/{card_id}"
        response = await self.client.get(route)
        self.assertEqual(response.status, 200)
        frozen = await response.json()
        self.assertEqual(frozen['document']['title'], '旧版本')
        self.assertEqual(frozen['snapshot']['job_id'], job['id'])
        # A modified original artifact is rejected instead of silently lying.
        (self.manager.folder / job['id'] / 'drawing.json').write_text(json.dumps({**document, 'title': '被改写'}), 'utf-8')
        self.assertEqual((await self.client.get(route)).status, 409)

    async def test_task_rejection_can_end_with_explicit_unconfirmed_flag(self):
        show = await (await self.action(action='start', theme='无法提交')).json()
        show = await (await self.action(action='add', id=show['id'], version=show['version'], title='画树', prompt='画树')).json()
        self.manager.cfg.enabled = False
        self.assertEqual((await self.action(action='submit', id=show['id'], version=show['version'], card_id=show['cards'][0]['id'])).status, 400)
        show = self.ui.show_sessions.get(show['id'])
        self.assertEqual((await self.action(action='close', id=show['id'], version=show['version'])).status, 400)
        response = await self.action(action='close', id=show['id'], version=show['version'], allow_unconfirmed=True)
        self.assertEqual(response.status, 200)
        self.assertIn('提交未确认', (await response.json())['recap'])
        self.assertEqual(len(self.manager.jobs), 0)


if __name__ == '__main__':
    unittest.main()
