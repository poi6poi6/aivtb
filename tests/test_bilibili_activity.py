import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from brain.bilibili_activity import BilibiliActivity, BrowserBroker, DEFAULTS
from brain.config import WebUIConfig
from brain.events import Event
from brain.webui import WebUI


URL = 'https://www.bilibili.com/video/BV1B7411m7LV'
POST = 'https://t.bilibili.com/123456789012345'


class Runner:
    def readiness(self):
        return dict(ready=True, detail='offline fixture')

    async def run(self, context, tool):
        self.context = context
        found = await tool('bili_search', dict(query='玫瑰'))
        url = found['items'][0]['url']
        await tool('bili_read', dict(url=url))
        await tool('bili_note', dict(source_url=url, summary='介绍了盆土排水', reaction='想试试这种配土'))
        await tool('bili_draft', dict(text='刚读到这段养花介绍，排水的建议值得记住。', source_urls=[url]))
        return 'saved'


class Browser:
    def __init__(self):
        self.calls, self.account = [], dict(uid='123', name='fixture')
        self.receipt = None

    def snapshot(self):
        return dict(connected=True, account=self.account)

    def cancel(self):
        pass

    async def call(self, action, payload):
        self.calls.append((action, copy.deepcopy(payload)))
        if action == 'account':
            return self.account
        if action == 'search':
            return dict(items=[dict(url=URL, title='玫瑰配土', author='作者')])
        if action == 'read':
            return dict(url=URL, description='花盆要有排水孔。', subtitles=[])
        if self.receipt:
            return self.receipt
        return dict(status='published', uid='123', text=payload['text'],
                    dynamic_id='123456789012345', url=POST)


class ActivityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'activity.json'
        self.browser, self.runner = Browser(), Runner()
        self.service = BilibiliActivity(self.path, self.runner, dict(name='助手', interests=['花']),
                                       broker=self.browser, clock=lambda: 1792700000.)
        self.addAsyncCleanup(self.service.close)

    async def browse(self):
        self.service.run()
        await self.service._task
        return self.service.data['drafts'][0]

    async def test_real_tool_loop_saves_evidence_draft_and_context_without_publishing(self):
        draft = await self.browse()
        self.assertEqual(draft['state'], 'draft')
        self.assertIn(URL, draft['text'])
        self.assertEqual(self.service.data['notes'][0]['evidence_kind'], 'page_metadata')
        self.assertEqual([x[0] for x in self.browser.calls], ['search', 'read'])
        context = self.service.context_for(Event('companion', 'chat', '最近在看什么', user='你'))
        self.assertIn('不能说看完视频', context)
        self.assertEqual(self.service.context_for(Event('danmaku', 'chat', '你好', user='其他人')), '')
        loaded = BilibiliActivity(self.path, self.runner, {}, broker=self.browser)
        self.assertEqual(loaded.data['drafts'], self.service.data['drafts'])

    async def test_publish_checks_account_and_persists_before_browser_side_effect(self):
        draft = await self.browse()
        await self.service.action(dict(action='settings', expected_uid='123'))
        original = self.browser.call
        async def checking(action, payload):
            if action == 'publish':
                saved = json.loads(self.path.read_text(encoding='utf-8'))['drafts'][0]
                self.assertEqual(saved['state'], 'publishing')
                self.assertTrue(saved['attempt_at'])
            return await original(action, payload)
        self.browser.call = checking
        result = await self.service.publish(draft['id'])
        self.assertEqual(result['state'], 'published')
        self.assertEqual(result['url'], POST)
        with self.assertRaises(ValueError):
            await self.service.publish(draft['id'])
        self.assertEqual(sum(x[0] == 'publish' for x in self.browser.calls), 1)

    async def test_wrong_account_cannot_publish_and_does_not_consume_post_quota(self):
        draft = await self.browse()
        await self.service.action(dict(action='settings', expected_uid='999'))
        with self.assertRaisesRegex(ValueError, '账号'):
            await self.service.publish(draft['id'])
        self.assertEqual(draft['state'], 'draft')
        self.assertNotIn('attempt_at', draft)
        self.assertFalse(any(x[0] == 'publish' for x in self.browser.calls))

    async def test_discard_cannot_race_an_in_progress_account_check(self):
        draft = await self.browse()
        await self.service.action(dict(action='settings', expected_uid='123'))
        entered, resume = asyncio.Event(), asyncio.Event()
        original = self.browser.call
        async def waiting(action, payload):
            if action == 'account':
                entered.set()
                await resume.wait()
            return await original(action, payload)
        self.browser.call = waiting
        publishing = asyncio.create_task(self.service.publish(draft['id']))
        await entered.wait()
        try:
            with self.assertRaisesRegex(ValueError, '等待'):
                await self.service.action(dict(action='discard', draft_id=draft['id']))
            self.assertEqual(draft['state'], 'draft')
        finally:
            resume.set()
            await publishing
        self.assertEqual(draft['state'], 'published')

    async def test_discarded_unsubmitted_drafts_do_not_exhaust_the_record_limit(self):
        self.service.data['drafts'] = [dict(id=str(n), text=f'old {n}', state='discarded')
                                       for n in range(100)]
        draft = await self.browse()
        self.assertEqual(draft['state'], 'draft')
        self.assertEqual(len(self.service.data['drafts']), 1)

    async def test_publish_receipts_are_retained_and_partial_failure_is_visible(self):
        self.service.data['drafts'] = [dict(id=str(n), text=f'old {n}', state='unknown', attempt_at=1)
                                       for n in range(100)]
        self.service.run()
        await self.service._task
        self.assertEqual(len(self.service.data['drafts']), 100)
        self.assertTrue(self.service.data['notes'])
        self.assertIn('动态记录已满', self.service.error)

    async def test_unknown_result_requires_verify_not_republish_and_counts_quota(self):
        draft = await self.browse()
        await self.service.action(dict(action='settings', expected_uid='123'))
        self.browser.receipt = dict(status='unknown')
        await self.service.publish(draft['id'])
        self.assertEqual(draft['state'], 'unknown')
        with self.assertRaises(ValueError):
            await self.service.publish(draft['id'])
        with self.assertRaises(ValueError):
            self.service._check_limit()
        self.browser.receipt = None
        await self.service.verify(draft['id'])
        self.assertEqual(draft['state'], 'published')
        self.assertEqual([x[0] for x in self.browser.calls][-1], 'verify')

    async def test_false_publish_receipts_never_report_success(self):
        for change in (dict(uid='999'), dict(text='other'), dict(dynamic_id='99999'),
                       dict(url='https://attacker.invalid/123456789012345')):
            draft = dict(uid='123', text='test', state='publishing')
            receipt = dict(status='published', uid='123', text='test',
                           dynamic_id='123456789012345', url=POST)
            self.service._receipt(draft, {**receipt, **change})
            self.assertEqual(draft['state'], 'unknown')

    async def test_restart_keeps_attempt_barrier_and_disabled_defaults(self):
        draft = await self.browse()
        draft.update(state='publishing', attempt_at=1792700000., uid='123')
        self.service._save()
        restored = BilibiliActivity(self.path, self.runner, {}, broker=self.browser)
        self.assertEqual(restored.data['drafts'][0]['state'], 'unknown')
        self.assertFalse(restored.data['settings']['enabled'])
        self.assertFalse(restored.data['settings']['auto_publish'])

    async def test_corrupt_store_is_not_silently_replaced(self):
        self.path.write_text('{broken', encoding='utf-8')
        restored = BilibiliActivity(self.path, self.runner, {}, broker=self.browser)
        with self.assertRaises(ValueError):
            restored.run()
        self.assertEqual(self.path.read_text(encoding='utf-8'), '{broken')

    async def test_failed_settings_write_blocks_newly_enabled_policy(self):
        self.service._save()
        previous = self.path.read_text(encoding='utf-8')
        with patch.object(Path, 'replace', side_effect=PermissionError('offline disk failure')):
            with self.assertRaises(OSError):
                await self.service.action(dict(action='settings', enabled=True,
                                               auto_publish=True, expected_uid='123'))
        self.assertTrue(self.service._storage_error)
        self.assertFalse(self.service.data['settings']['enabled'])
        self.assertFalse(self.service.data['settings']['auto_publish'])
        self.assertEqual(self.path.read_text(encoding='utf-8'), previous)
        with self.assertRaises(ValueError):
            self.service.run()

    async def test_stop_cancels_work_even_when_persistence_fails(self):
        entered = asyncio.Event()
        async def waiting(context, tool):
            entered.set()
            await asyncio.Event().wait()
        self.runner.run = waiting
        cancellations = []
        self.browser.cancel = lambda: cancellations.append(True)
        self.service.run()
        await entered.wait()
        with patch.object(Path, 'replace', side_effect=PermissionError('offline disk failure')):
            with self.assertRaises(OSError):
                await self.service.action(dict(action='stop'))
        await asyncio.gather(self.service._task, return_exceptions=True)
        self.assertFalse(self.service.snapshot()['running'])
        self.assertTrue(cancellations)
        self.assertTrue(self.service._storage_error)

    async def test_failed_publish_barrier_write_never_dispatches_publish(self):
        draft = await self.browse()
        await self.service.action(dict(action='settings', expected_uid='123'))
        with patch.object(Path, 'replace', side_effect=PermissionError('offline disk failure')):
            with self.assertRaises(OSError):
                await self.service.publish(draft['id'])
        self.assertFalse(any(action == 'publish' for action, _ in self.browser.calls))
        self.assertTrue(self.service._storage_error)
        with self.assertRaises(ValueError):
            await self.service.publish(draft['id'])

    async def test_unread_urls_and_unsupported_tools_are_rejected(self):
        async def attacks(context, tool):
            for name, args in [('bili_read', dict(url='https://evil.invalid')),
                               ('bili_note', dict(source_url=URL, summary='fake', reaction='fake')),
                               ('bili_draft', dict(text='fake', source_urls=[URL])),
                               ('publish', dict(text='fake'))]:
                self.assertFalse((await tool(name, args))['ok'])
        self.runner.run = attacks
        self.service.run()
        await self.service._task
        self.assertFalse(self.service.data['notes'])
        self.assertFalse(self.service.data['drafts'])
        self.assertFalse(self.browser.calls)

    async def test_title_only_is_not_reading_evidence(self):
        original = self.browser.call
        async def title_only(action, payload):
            if action == 'read':
                return dict(url=URL, title='玫瑰', description='', subtitles=[])
            return await original(action, payload)
        self.browser.call = title_only
        self.service.run()
        await self.service._task
        self.assertFalse(self.service.data['drafts'])
        self.assertIn('只读取到标题', self.service.error)

    async def test_chat_interrupts_browsing_and_prevents_automatic_post(self):
        entered = asyncio.Event()
        async def waiting(context, tool):
            entered.set()
            await asyncio.Event().wait()
        self.runner.run = waiting
        self.service.run()
        await entered.wait()
        self.service.foreground()
        await asyncio.gather(self.service._task, return_exceptions=True)
        self.assertFalse(self.service.snapshot()['running'])
        self.assertFalse(self.browser.calls)
        with self.assertRaisesRegex(ValueError, '聊天'):
            self.service.run()

    async def test_automatic_publish_requires_enabled_policy_and_real_receipt(self):
        await self.service.action(dict(action='settings', auto_publish=True, expected_uid='123'))
        draft = await self.browse()
        self.assertEqual(draft['state'], 'published')
        self.assertEqual([x[0] for x in self.browser.calls], ['search', 'read', 'account', 'publish'])

    def test_settings_are_typed_and_bounded(self):
        for values in (dict(enabled='yes'), dict(max_items=True), dict(interval_minutes=0),
                       dict(daily_posts=11), dict(expected_uid='abc'), dict(auto_publish=True),
                       dict(auto_publish=True, expected_uid='123', daily_posts=0)):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.service.validate_settings(values)


class BrokerTests(unittest.IsolatedAsyncioTestCase):
    async def test_once_only_claim_receipts_and_stale_result(self):
        broker = BrowserBroker(timeout=1)
        task = asyncio.create_task(broker.call('search', dict(query='test')))
        await asyncio.sleep(0)
        command = broker.claim()
        self.assertIsNone(broker.claim())
        self.assertTrue(broker.result(dict(id=command['id'], ok=True, result=dict(items=[]))))
        self.assertEqual(await task, dict(items=[]))
        self.assertFalse(broker.result(dict(id=command['id'], ok=True, result=dict(items=[]))))

    async def test_lost_publication_receipt_is_not_reissued(self):
        broker = BrowserBroker(timeout=.01)
        task = asyncio.create_task(broker.call('publish', dict(text='test')))
        await asyncio.sleep(0)
        command = broker.claim()
        with self.assertRaises(ValueError):
            await task
        self.assertIsNone(broker.claim())
        self.assertFalse(broker.result(dict(id=command['id'], ok=True, result={})))


class ActivityHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.ui = WebUI(WebUIConfig(), asyncio.Queue(), str(root), {}, workspace_root=root)
        self.ui.bilibili_activity = BilibiliActivity(root / 'activity.json', Runner(), {})
        app = web.Application()
        app.router.add_get('/api/bilibili-activity', self.ui._activity_status)
        app.router.add_post('/api/bilibili-activity/action', self.ui._activity_action)
        app.router.add_post('/api/bilibili-activity/browser', self.ui._activity_browser)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.ui.close)
        self.addAsyncCleanup(self.client.close)
        self.origin = str(self.client.make_url('/')).rstrip('/')

    async def test_local_routes_work_but_foreign_origin_and_public_mode_cannot_control(self):
        response = await self.client.get('/api/bilibili-activity')
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())['settings'], DEFAULTS)
        path = '/api/bilibili-activity/action'
        for headers in ({}, {'Origin': 'https://evil.invalid'}, {'Origin': self.origin + '/'}):
            response = await self.client.post(path, headers=headers, json=dict(action='settings', enabled=True))
            self.assertEqual(response.status, 403)
        response = await self.client.post(path, headers={'Origin': self.origin},
                                          json=dict(action='settings', max_items=2))
        self.assertEqual(response.status, 200)
        self.ui.cfg.public_test = True
        response = await self.client.get('/api/bilibili-activity')
        self.assertEqual(response.status, 403)

    async def test_browser_round_trip_and_duplicate_result(self):
        broker = self.ui.bilibili_activity.browser
        task = asyncio.create_task(broker.call('search', dict(query='花')))
        await asyncio.sleep(0)
        path = '/api/bilibili-activity/browser'
        headers = {'Origin': self.origin}
        response = await self.client.post(path, headers=headers, json=dict(action='claim'))
        command = (await response.json())['command']
        payload = dict(action='result', id=command['id'], ok=True, result=dict(items=[]))
        response = await self.client.post(path, headers=headers, json=payload)
        self.assertTrue((await response.json())['ok'])
        self.assertEqual(await task, dict(items=[]))
        response = await self.client.post(path, headers=headers, json=payload)
        self.assertFalse((await response.json())['ok'])


if __name__ == '__main__':
    unittest.main()
