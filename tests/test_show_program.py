import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from brain.config import DirectorConfig, WebUIConfig
from brain.director import LiveDirector
from brain.events import Event
from brain.live_polls import LivePolls
from brain.show_sessions import ShowSessions, program_context, public_program
from brain.webui import WebUI


class ShowProgramTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = 1000
        self.store = ShowSessions(Path(self.tmp.name) / 'show.db', clock=lambda: self.now)
        self.polls = LivePolls(Path(self.tmp.name) / 'poll.db', clock=lambda: self.now)
        self.show = self.store.start('庭院主题')
        self.show = self.store.add(self.show['id'], self.show['version'], '画温室', 'private prompt')
        self.card = self.show['cards'][0]
        self.command('start_card', card_id=self.card['id'])

    def command(self, name, **kwargs):
        self.show = self.store.program_command(self.show['id'], self.show['version'], name, **kwargs)

    def test_host_progression_never_submits_or_opens_poll(self):
        self.command('enable')
        for stage in ('intro', 'vote', 'sketch', 'discussion', 'naming', 'finished'):
            self.assertEqual(self.show['program']['stage'], stage)
            self.assertIsNone(self.show['cards'][0]['submission'])
            self.assertIsNone(self.polls.status()['current'])
            if stage != 'finished':
                self.command('next_stage')
        self.command('disable')
        self.assertFalse(self.show['program']['live_mode'])
        restarted = ShowSessions(self.store.path).get(self.show['id'])
        self.assertEqual(restarted['program']['stage'], 'finished')
        with self.assertRaises(ValueError):
            self.store.program_command(self.show['id'], 1, 'next_stage')

    def test_actual_poll_zero_tie_cancel_and_old_results_are_truthful(self):
        poll = self.polls.create(program='庭院', question='画什么？', options=['温室', '书房'], mode='test')
        self.command('attach_poll', poll_id=poll['id'])
        self.command('next_stage')
        self.command('enable')
        context = program_context(self.show, armed=True, poll=poll)
        self.assertEqual(context['status_code'], 'poll_open')
        self.assertIn('测试', context['status'])
        for user, choice in [('a', '1'), ('b', '2')]:
            self.now += 1
            self.polls.test_vote(poll['id'], user, choice)
        tied = self.polls.finish(poll['id'])
        self.assertIn('平票', program_context(self.show, armed=True, poll=tied)['status'])
        zero = {**tied, 'total': 0, 'winners': []}
        self.assertIn('没有有效票', program_context(self.show, armed=True, poll=zero)['status'])
        cancelled = {**tied, 'state': 'cancelled'}
        self.assertIn('已取消', program_context(self.show, armed=True, poll=cancelled)['status'])
        self.show['cards'][0].update(poll=tied, poll_origin_show='previous-show')
        self.assertIn('沿用往期结果', program_context(self.show, armed=True, poll=tied)['status'])

    def test_audience_projection_has_no_private_card_or_task_fields(self):
        self.command('enable')
        self.command('next_stage')
        self.command('next_stage')
        self.show['cards'][0].update(note='secret note', next_todo='secret todo', drawing_root='internal-id')
        context = program_context(self.show, armed=True, drawing={
            'state': 'waiting_approval', 'stage': 'sketch', 'ready_stage': None,
            'detail': 'secret error', 'tools': 'secret tool', 'requester': 'private account'}, pending_count=1)
        public = public_program(context)
        self.assertEqual(set(public), {'active', 'title', 'theme', 'stage_label', 'status', 'next_label', 'pending_count', 'updated_at', 'fresh'})
        encoded = json.dumps(public)
        for value in ('private', 'secret', 'internal-id', self.show['id'], self.card['id']):
            self.assertNotIn(value, encoded)
        self.assertIn('等待主播确认', public['status'])
        self.assertEqual(public['pending_count'], 1)
        unknown = program_context(self.show, armed=True, drawing=None)
        self.assertFalse(unknown['fresh'])
        self.assertIn('暂未确认', unknown['status'])
        paused = public_program(program_context(self.show, armed=False))
        self.assertFalse(paused['active'])
        self.assertEqual(paused['title'], '')


class ProgramDirectorTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000
        self.cfg = DirectorConfig(enabled=False, proactive_interval_sec=35)
        self.director = LiveDirector(self.cfg, clock=lambda: self.now)
        self.context = {'show_id': 's', 'card_id': 'c', 'live_mode': True, 'fresh': True,
                        'stage': 'intro', 'stage_label': '介绍委托', 'status_code': 'intro',
                        'topic': '雪夜温室', 'status': '介绍本轮委托与创作主题'}

    def test_explicit_mode_speaks_once_per_actual_stage_and_keeps_config_off(self):
        self.assertFalse(self.director.enabled)
        self.director.set_program_context(self.context)
        self.assertTrue(self.director.enabled)
        self.assertFalse(self.cfg.enabled)
        self.now += 40
        first = self.director.poll_proactive()
        self.assertIn('雪夜温室', first.text)
        self.assertTrue(self.director.program_event_current(first))
        self.director.after_reply(first, '平静', '先画一个温室的轮廓。')
        for _ in range(5):
            self.now += 60
            self.assertIsNone(self.director.poll_proactive())
        self.director.set_program_context({**self.context, 'stage': 'vote', 'status_code': 'poll_missing'})
        self.assertFalse(self.director.program_event_current(first))
        second = self.director.poll_proactive()
        self.assertIsNotNone(second)
        self.director.set_program_context({**self.context, 'live_mode': False})
        self.assertFalse(self.director.enabled)
        self.assertFalse(self.director.program_event_current(second))
        self.assertTrue(self.director.program_event_current(Event('danmaku', 'chat', 'hello')))

    def test_stale_context_allow_gate_and_chats_cannot_advance_rundown(self):
        self.director.set_program_context({**self.context, 'fresh': False})
        self.now += 100
        self.assertIsNone(self.director.poll_proactive())
        self.director.set_program_context(self.context)
        self.assertIsNone(self.director.poll_proactive(allow=False))
        for _ in range(12):
            event = Event('danmaku', 'chat', '画一朵花')
            self.director.prepare(event)
            self.assertIn('不替主播', event.director_context)
            self.director.after_reply(event, '开心', '先听听你的想法。')
        self.assertEqual(self.director._program_context['stage'], 'intro')

    def test_background_seed_topics_cool_down_for_ten_minutes(self):
        self.cfg.topic_seeds = ['今天的心情', '喜欢的花', '今天 的心情']
        first = self.director._next_topic_seed()
        second = self.director._next_topic_seed()
        self.assertNotEqual(first, second)
        self.assertEqual(self.director._next_topic_seed(), '')
        self.now += 599
        self.assertEqual(self.director._next_topic_seed(), '')
        self.now += 2
        self.assertTrue(self.director._next_topic_seed())


class ShowProgramHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.director = LiveDirector(DirectorConfig(enabled=False))
        self.ui = WebUI(WebUIConfig(), asyncio.Queue(), self.tmp.name, {}, director=self.director)
        self.ui.show_sessions = ShowSessions(Path(self.tmp.name) / 'show.db')
        self.ui.live_polls = LivePolls(Path(self.tmp.name) / 'poll.db')
        self.show = self.ui.show_sessions.start('测试节目')
        self.show = self.ui.show_sessions.add(self.show['id'], self.show['version'], '<b>温室</b>', 'private prompt')
        app = web.Application()
        app.router.add_get('/show-program-status', self.ui._show_program_status)
        app.router.add_get('/show-stage', self.ui._show_stage_page)
        app.router.add_get('/show-stage-status', self.ui._show_stage_status)
        app.router.add_post('/shows-action', self.ui._shows_action)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def command(self, command, **kwargs):
        self.show = self.ui.show_sessions.get(self.show['id'])
        return await self.client.post('/shows-action', json={'action': 'program', 'id': self.show['id'],
            'version': self.show['version'], 'command': command, **kwargs},
            headers={'Origin': str(self.client.make_url('/')).rstrip('/')})

    async def test_explicit_enable_and_restart_require_rearming(self):
        response = await self.command('start_card', card_id=self.show['cards'][0]['id'])
        self.assertEqual(response.status, 200)
        self.assertFalse(self.director.enabled)
        await self.command('enable')
        self.assertTrue(self.director.enabled)
        public = await (await self.client.get('/show-stage-status')).json()
        self.assertTrue(public['active'])
        self.assertEqual(public['title'], '<b>温室</b>')
        self.assertNotIn('private prompt', json.dumps(public))
        restarted = WebUI(WebUIConfig(), asyncio.Queue(), self.tmp.name, {}, director=LiveDirector(DirectorConfig(enabled=False)))
        restarted.show_sessions = ShowSessions(self.ui.show_sessions.path)
        context = await restarted.refresh_program_context(force=True)
        self.assertFalse(context['live_mode'])
        self.assertFalse(restarted.director.enabled)
        self.assertTrue(restarted.show_sessions.current()['program']['live_mode'])
        await self.command('disable')
        self.assertFalse(self.director.enabled)

    async def test_obs_is_readonly_and_permissions_cover_all_endpoints(self):
        before = deepcopy(self.ui.show_sessions.current())
        self.assertEqual((await self.client.get('/show-stage?clean=1')).status, 200)
        for _ in range(3):
            self.assertEqual((await self.client.get('/show-stage-status')).status, 200)
        self.assertEqual(self.ui.show_sessions.current(), before)
        self.assertEqual((await self.client.post('/show-stage-status', json={})).status, 405)
        self.assertEqual((await self.client.get('/show-stage-status', headers={'Host': 'evil.example'})).status, 403)
        self.assertEqual((await self.client.post('/shows-action', json={'action': 'program'})).status, 403)
        self.ui.cfg.public_test = True
        for route in ('/show-stage', '/show-stage-status', '/show-program-status'):
            self.assertEqual((await self.client.get(route)).status, 403)
        self.assertEqual((await self.command('enable')).status, 403)

    async def test_stale_task_cache_is_unknown_not_old_progress_and_never_rpcs(self):
        await self.command('start_card', card_id=self.show['cards'][0]['id'])
        await self.command('enable')
        await self.command('next_stage')
        await self.command('next_stage')
        show = self.ui.show_sessions.current()
        show['cards'][0]['drawing_root'] = 'root'
        self.ui.show_sessions.save(show)
        task = {'id': 'root', 'kind': 'drawing', 'state': 'running', 'drawing_stage': 'sketch', 'detail': 'secret tools'}
        self.ui.agent_tasks = SimpleNamespace(remote=True, connected=True, closing=False,
            last_sync=time.monotonic(), get=lambda key: task)
        context = await self.ui.refresh_program_context(force=True)
        self.assertIn('正在准备草稿', context['status'])
        self.assertEqual(context['pending_count'], 1)
        self.ui.agent_tasks.last_sync -= 20
        context = await self.ui.refresh_program_context(force=True)
        self.assertFalse(context['fresh'])
        self.assertIn('未确认', context['status'])
        self.assertIsNone(context['pending_count'])
        with patch.object(self.ui.show_sessions, 'current', side_effect=OSError('secret path')):
            await self.ui.refresh_program_context(force=True)
            response = await self.client.get('/show-stage-status')
            public = await response.json()
        self.assertFalse(public['fresh'])
        self.assertNotIn('secret', json.dumps(public))


if __name__ == '__main__':
    unittest.main()
