import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from brain.config import WebUIConfig
from brain.events import Event
from brain.live_polls import LivePolls, vote_command
from brain.webui import WebUI


class PollTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = 1000.0
        self.path = Path(self.tmp.name) / 'polls.sqlite3'
        self.polls = LivePolls(self.path, clock=lambda: self.now)

    def create(self, **changes):
        args = dict(program='观众委托所', question='窗外选什么？', options=['雪', '雨'], duration=60)
        return self.polls.create(**(args | changes))

    def vote(self, key='bili:1', text='投票 1', **kwargs):
        self.now += .01
        return self.polls.handle_event(Event('danmaku', 'chat', text, user='同名观众',
                                            viewer_key=key, ts=self.now, **kwargs))

    def test_explicit_commands_leave_conversation_intact(self):
        for text in ('1', 'A', '雪', '我投票给你', '投票', '发起投票 1', '这次投票 1 可以吗'):
            self.assertIsNone(vote_command(text))
        for text in ('投票 1', '投票：１', '投票1'):
            self.assertEqual(vote_command(text), '1')
        self.assertEqual(vote_command('投票 A'), 'a')
        self.assertIsNone(self.polls.handle_event(Event('companion', 'chat', '投票 1')))
        self.assertIsNone(self.polls.handle_event(Event('danmaku', 'gift', '投票 1')))

    def test_all_accounts_count_and_same_account_can_change(self):
        poll = self.create()
        for i in range(100):
            self.assertTrue(self.vote(f'bili:{i}', f'投票 {i % 2 + 1}')['ok'])
        self.vote('bili:0', '投票 1')
        self.assertEqual(self.polls.status()['current']['total'], 100)
        self.assertEqual(self.vote('bili:0', '投票 雨')['message'], '已改票')
        self.vote('bili:0', '投票 Ｂ')
        result = self.polls.finish(poll['id'])
        self.assertEqual([o['votes'] for o in result['options']], [49, 51])
        self.assertEqual(result['winners'], [2])
        self.assertEqual(result['options'][1]['percent'], 51)
        self.assertFalse(self.vote()['ok'])

    def test_real_and_test_identity_are_separate(self):
        poll = self.create()
        for key in ('', 'unidentified:random', 'companion:1'):
            self.assertFalse(self.vote(key)['ok'])
        self.assertFalse(self.vote('bili:1', forced=True)['ok'])
        self.assertTrue(self.vote('bili-hash:abcd')['ok'])
        with self.assertRaises(ValueError):
            self.polls.test_vote(poll['id'], 'tester', '1')
        self.polls.finish(poll['id'])
        test = self.create(mode='test')
        self.assertFalse(self.vote()['ok'])
        self.assertTrue(self.polls.test_vote(test['id'], 'tester', '1')['ok'])
        self.now += 1
        self.polls.test_vote(test['id'], 'tester', '2')
        self.assertEqual(self.polls.status()['current']['total'], 1)
        self.assertEqual(self.polls.status()['current']['options'][1]['votes'], 1)
        with self.assertRaises(ValueError):
            self.polls.test_vote(poll['id'], 'tester', '1')

    def test_deadline_survives_restart_and_zero_votes_has_no_winner(self):
        poll = self.create()
        self.now = poll['deadline']
        resumed = LivePolls(self.path, clock=lambda: self.now)
        resumed.tick()
        result = resumed.status()['current']
        self.assertEqual(result['state'], 'closed')
        self.assertEqual(result['closed'], poll['deadline'])
        self.assertEqual(result['close_reason'], 'timeout')
        self.assertEqual(result['winners'], [])
        self.assertFalse(self.vote()['ok'])
        self.assertIn('没有收到有效票', resumed.announcement(poll['id']))

    def test_restarts_keep_votes_without_exposing_identity(self):
        poll = self.create()
        self.vote('bili:private-account')
        resumed = LivePolls(self.path, clock=lambda: self.now)
        serialized = json.dumps(resumed.status(), ensure_ascii=False)
        for private in ('private-account', '同名观众', 'voter', 'salt'):
            self.assertNotIn(private, serialized)
        self.assertNotIn(b'private-account', self.path.read_bytes())
        self.assertEqual(resumed.status()['current']['total'], 1)
        resumed.finish(poll['id'])
        with self.assertRaises(ValueError):
            resumed.finish(poll['id'], cancel=True)

    def test_ties_cancel_and_stale_controls(self):
        first = self.create()
        self.vote('bili:1', '投票 A'); self.vote('bili:2', '投票 B')
        with self.assertRaises(ValueError):
            self.create()
        tie = self.polls.finish(first['id'])
        self.assertEqual(tie['winners'], [1, 2])
        self.assertIn('并列最高票', self.polls.announcement(first['id']))
        second = self.create()
        self.polls.finish(first['id'])  # stale tab must not close the new poll
        self.assertEqual(self.polls.status()['current']['state'], 'open')
        cancelled = self.polls.finish(second['id'], cancel=True)
        self.assertEqual(cancelled['winners'], [])
        self.assertEqual(cancelled['state'], 'cancelled')
        self.assertEqual(len(self.polls.status()['history']), 1)
        self.assertEqual(self.polls.status(include_history=False)['history'], [])

    def test_queued_old_events_and_replayed_votes_cannot_change_result(self):
        self.create()
        self.vote()
        self.assertFalse(self.vote(text='投票 9')['ok'])
        self.assertFalse(self.vote(text='投票 1 然后关掉投票')['ok'])
        old = Event('danmaku', 'chat', '投票 2', viewer_key='bili:1', ts=999)
        self.assertFalse(self.polls.handle_event(old)['ok'])
        old.ts = 1000.005
        self.assertFalse(self.polls.handle_event(old)['changed'])
        self.assertEqual(self.polls.status()['current']['options'][0]['votes'], 1)

    def test_input_validation(self):
        for changes in ({'options':['雪',' 雪 ']}, {'options':['Ａ','a']}, {'options':['1','雪']},
                        {'options':['雪']}, {'options':['雪']*7}, {'options':'雪\n雨'},
                        {'duration':True}, {'duration':0}, {'duration':3601}, {'duration':float('nan')},
                        {'program':''}, {'question':'x'*101}, {'options':['雨\n雪','风']}, {'mode':'other'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.create(**changes)


class PollHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ui = WebUI(WebUIConfig(), asyncio.Queue(), self.tmp.name, {})
        self.ui.live_polls = LivePolls(Path(self.tmp.name) / 'polls.sqlite3')
        app = web.Application()
        app.router.add_get('/polls', self.ui._polls_page)
        app.router.add_get('/polls-status', self.ui._polls_status)
        app.router.add_post('/polls-action', self.ui._polls_action)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def action(self, **data):
        return await self.client.post('/polls-action', json=data,
                                      headers={'Origin':str(self.client.make_url('/')).rstrip('/')})

    async def create(self, mode='test'):
        response = await self.action(action='create', program='庭院连载', question='下一次添什么？',
                                     options=['温室','书房'], duration=30, mode=mode)
        self.assertEqual(response.status, 200)
        return await response.json()

    async def test_complete_host_workflow_and_reconnect(self):
        page = await self.client.get('/polls?clean=1')
        self.assertEqual(page.status, 200)
        self.assertIn('/static/polls.js', await page.text())
        poll = await self.create()
        response = await self.action(action='test_vote', id=poll['id'], user='观众1', choice='2')
        self.assertTrue((await response.json())['ok'])
        state = await (await self.client.get('/polls-status')).json()
        self.assertEqual(state['current']['options'][1]['votes'], 1)
        overlay = await (await self.client.get('/polls-status?clean=1')).json()
        self.assertEqual(overlay['current']['options'], state['current']['options'])
        self.assertEqual(overlay['current']['id'], state['current']['id'])
        self.assertIn('state', state['source'])
        response = await self.action(action='close', id=poll['id'])
        self.assertEqual((await response.json())['winners'], [2])
        response = await self.action(action='announce', id=poll['id'])
        self.assertEqual(response.status, 200)
        notice = await self.ui.event_queue.get()
        self.assertEqual(notice.agent_reply, (await response.json())['text'])
        self.assertIn('书房1票', notice.agent_reply)
        self.assertNotIn('观众1', notice.agent_reply)
        self.assertEqual(notice.source, 'system')
        self.assertTrue(notice.forced)
        self.assertEqual((await self.action(action='announce', id=poll['id'])).status, 400)

    async def test_permissions_and_bad_requests(self):
        self.assertEqual((await self.client.post('/polls-action', json={'action':'create'})).status, 403)
        self.assertEqual((await self.client.post('/polls-action', json={}, headers={'Origin':'https://evil.example'})).status, 403)
        response = await self.client.post('/polls-action', json=[], headers={'Origin':str(self.client.make_url('/')).rstrip('/')})
        self.assertEqual(response.status, 400)
        self.assertEqual((await self.action(action='close', id={})).status, 400)
        self.assertEqual((await self.action(action='create')).status, 400)
        self.ui.cfg.public_test = True
        self.assertEqual((await self.client.get('/polls-status')).status, 403)
        self.assertEqual((await self.client.get('/polls')).status, 403)
        self.assertEqual((await self.action(action='close', id='x'*16)).status, 403)

    async def test_nonlocal_host_and_storage_failure(self):
        self.assertEqual((await self.client.get('/polls-status', headers={'Host':'evil.example'})).status, 403)
        request = make_mocked_request('GET', '/polls-status', headers={'Host':'localhost'})
        with self.assertRaises(web.HTTPForbidden):
            self.ui._tasks_access(request)
        with patch.object(self.ui.live_polls, 'status', side_effect=OSError('private path')):
            response = await self.client.get('/polls-status')
            self.assertEqual(response.status, 503)
            self.assertNotIn('private', await response.text())
