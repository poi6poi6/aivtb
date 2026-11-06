"""Offline checks for public diaries, scoped recall and receipt-gated actions."""
import asyncio
import copy
import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from brain.bilibili_activity import BilibiliActivity, DEFAULTS
from brain.config import LLMConfig, PersonaConfig, PersonaLLMConfig
from brain.events import Event
from brain.persona import Persona
from brain.private_chat import PrivateChatSession
from brain.qq_group_chat import GroupChatSession


URL = 'https://www.bilibili.com/video/BV1B7411m7LV'
URL2 = 'https://www.bilibili.com/video/BV1B7411m7LW'
NOW = datetime(2026, 10, 26, 4, tzinfo=timezone.utc).timestamp()


def reading_steps(url=URL):
    return [('bili_search', dict(query='玫瑰')),
            ('bili_read', dict(url=url)),
            ('bili_note', dict(source_url=url, summary='介绍了玫瑰花盆的排水。', reaction='PUBLIC_REACTION'))]


def diary_step(url=URL):
    return 'bili_diary', dict(text='PUBLIC_DIARY：想多了解花盆的排水设计。', source_urls=[url])


def proposal_step(action='comment', url=URL):
    return 'bili_interaction', dict(action=action, url=url, text='排水孔的介绍很清楚。' if action == 'comment' else '')


class ScriptRunner:
    def __init__(self, steps=()):
        self.steps, self.results = list(steps), []

    def readiness(self):
        return dict(ready=True, detail='offline fixture')

    async def run(self, context, tool):
        self.context = copy.deepcopy(context)
        self.results = []
        for name, args in self.steps:
            self.results.append(await tool(name, copy.deepcopy(args)))
        return 'fixture complete'


class FixtureBrowser:
    def __init__(self):
        self.calls = []
        self.uid = '123'
        self.receipt = None
        self.before_call = None
        self.read_result = dict(description='花盆需要排水孔。', subtitles=['完整字幕。'],
                                evidence_kind='full_subtitles', comments=[dict(text='热评看法。')])

    def snapshot(self):
        return dict(connected=True, account=dict(uid=self.uid))

    def cancel(self):
        pass

    async def call(self, action, payload):
        self.calls.append((action, copy.deepcopy(payload)))
        if self.before_call is not None:
            await self.before_call(action, payload)
        if action == 'account':
            return dict(uid=self.uid)
        if action == 'search':
            return dict(items=[dict(url=url, title='玫瑰花盆养护', author='公开作者') for url in (URL, URL2)])
        if action == 'read':
            return dict(url=payload['url'], **self.read_result)
        if action in ('interact', 'verify_interaction'):
            if isinstance(self.receipt, Exception):
                raise self.receipt
            if self.receipt is not None:
                return copy.deepcopy(self.receipt)
            return dict(status='completed', uid=self.uid, url=payload['url'], action=payload['action'],
                        liked=True, text=payload['text'], comment_id='987654321')
        raise AssertionError('Unexpected external action: ' + action)


class BilibiliModuleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / 'activity.json'
        self.browser, self.runner = FixtureBrowser(), ScriptRunner()
        self.now = NOW
        self.service = BilibiliActivity(self.path, self.runner, dict(name='助手'),
                                        broker=self.browser, clock=lambda: self.now)
        self.addAsyncCleanup(self.service.close)

    async def browse(self, steps):
        self.runner.steps = steps
        self.service.run()
        await self.service._task
        return self.runner.results

    async def proposal(self, action='comment', url=URL):
        await self.browse(reading_steps(url) + [proposal_step(action, url)])
        return self.service.data['interactions'][-1]

    async def enable(self, **values):
        await self.service.action(dict(action='settings', expected_uid='123', **values))

    async def test_diary_and_proposal_save_public_evidence_without_external_writes(self):
        await self.browse(reading_steps() + [diary_step(), proposal_step()])
        self.assertEqual(len(self.service.data['diaries']), 1)
        self.assertEqual(self.service.data['notes'][0]['visibility'], 'public')
        self.assertEqual(self.service.data['notes'][0]['evidence_kind'], 'full_subtitles')
        self.assertEqual(self.service.data['interactions'][0]['state'], 'draft')
        self.assertEqual([action for action, _ in self.browser.calls], ['search', 'read'])
        saved = json.loads(self.path.read_text('utf-8'))
        self.assertEqual(saved['diaries'][0]['source_urls'], [URL])
        context = self.service.context_for(Event('qq_group', 'chat', '你的日记', ts=NOW))
        self.assertIn('PUBLIC_DIARY', context)
        self.assertIn(URL, context)

    async def test_diary_rejects_old_notes_unread_sources_and_second_diary(self):
        self.service.data['notes'].append(dict(id='old', url=URL, title='旧笔记', summary='旧内容',
                                               reaction='感想', evidence_kind='page_metadata',
                                               visibility='public', at=NOW - 100))
        results = await self.browse([diary_step()] + reading_steps() + [diary_step(URL2), diary_step(), diary_step()])
        self.assertFalse(results[0]['ok'])
        self.assertFalse(results[4]['ok'])
        self.assertTrue(results[5]['saved'])
        self.assertFalse(results[6]['ok'])
        self.assertEqual(len(self.service.data['diaries']), 1)

    async def test_proposals_require_current_reading_and_do_not_duplicate_across_runs(self):
        results = await self.browse([proposal_step()] + reading_steps() + [proposal_step(), proposal_step('like')])
        self.assertFalse(results[0]['ok'])
        self.assertEqual(results[4]['state'], 'draft')
        self.assertFalse(results[5]['ok'])
        self.assertEqual(len(self.service.data['interactions']), 1)
        results = await self.browse(reading_steps() + [proposal_step()])
        self.assertFalse(results[-1]['ok'])
        self.assertEqual(len(self.service.data['interactions']), 1)

    async def test_switches_block_manual_execution_and_validate_account_and_quota(self):
        item = await self.proposal()
        with self.assertRaisesRegex(ValueError, '开关'):
            await self.service.interact(item['id'])
        self.assertEqual(item['state'], 'draft')
        self.assertNotIn('attempt_at', item)
        self.assertFalse(any(action == 'account' for action, _ in self.browser.calls))
        for bad in (dict(allow_comments=True), dict(allow_comments=1, expected_uid='123'),
                    dict(daily_interactions=-1), dict(active_start_hour=5, active_end_hour=5)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                BilibiliActivity.validate_settings(bad)
        await self.enable(allow_comments=True, daily_interactions=0)
        with self.assertRaisesRegex(ValueError, '上限'):
            await self.service.interact(item['id'])
        self.assertFalse(any(action == 'interact' for action, _ in self.browser.calls))

    async def test_full_track_truncation_is_recorded_and_recalled_as_excerpt(self):
        for subtitles, upstream_flag in ((['甲' * 12001], False), (['字幕片段'], True)):
            with self.subTest(upstream_flag=upstream_flag):
                self.browser.read_result.update(subtitles=subtitles, subtitle_truncated=upstream_flag)
                await self.browse(reading_steps())
                note = self.service.data['notes'][0]
                self.assertEqual(note['evidence_kind'], 'subtitle_excerpt')
                self.assertTrue(note['subtitle_truncated'])
                context = self.service.context_for(Event('companion', 'chat', '最近在看什么', ts=NOW))
                self.assertIn('subtitle_excerpt', context)
                self.assertNotIn('full_subtitles', context)

    async def test_multi_part_scope_and_language_survive_read_note_storage_and_recall(self):
        self.browser.read_result.update(video_part=1, video_part_count=3, subtitle_language='ai-zh')
        results = await self.browse(reading_steps())
        for row in (results[1], self.service.data['notes'][0],
                    json.loads(self.path.read_text('utf-8'))['notes'][0]):
            self.assertEqual(row['video_part'], 1)
            self.assertEqual(row['video_part_count'], 3)
            self.assertEqual(row['subtitle_language'], 'ai-zh')
        context = self.service.context_for(Event('qq_group', 'chat', '最近在看什么', ts=NOW))
        self.assertIn('已读 P1 / 共 3P', context)
        self.assertIn('不表示多P全看', context)
        self.assertIn('ai-zh', context)

    async def test_missing_part_count_remains_unknown_but_invalid_part_evidence_is_rejected(self):
        self.browser.read_result['video_part'] = 1
        results = await self.browse(reading_steps())
        self.assertNotIn('video_part', results[1])
        self.assertNotIn('video_part_count', self.service.data['notes'][0])
        for update in (dict(video_part=True, video_part_count=2), dict(video_part=1, video_part_count=False),
                       dict(video_part=0, video_part_count=2), dict(video_part=3, video_part_count=2),
                       dict(video_part=1, video_part_count=10001), dict(video_part=1.0, video_part_count=2),
                       dict(video_part=1, video_part_count=2, subtitle_language='x' * 41),
                       dict(video_part=1, video_part_count=2, subtitle_language='zh\x00')):
            with self.subTest(update=update):
                self.service.data['notes'] = []
                self.browser.read_result = dict(description='简介', subtitles=['字幕'],
                                                evidence_kind='full_subtitles', **update)
                results = await self.browse(reading_steps())
                self.assertFalse(results[1]['ok'])
                self.assertFalse(results[2]['ok'])
                self.assertEqual(self.service.data['notes'], [])

    async def test_analysis_markers_never_become_diary_comment_or_dynamic(self):
        raw = 'Thinking Process:\n- 内部草稿\n最终正文。'
        steps = reading_steps() + [
            ('bili_diary', dict(text=raw, source_urls=[URL])),
            ('bili_interaction', dict(action='comment', url=URL, text=raw)),
            ('bili_draft', dict(text=raw, source_urls=[URL])),
        ]
        results = await self.browse(steps)
        self.assertTrue(all(result.get('ok') is False for result in results[-3:]))
        for key in ('diaries', 'interactions', 'drafts'):
            self.assertEqual(self.service.data[key], [])
        self.assertEqual([action for action, _ in self.browser.calls], ['search', 'read'])

    async def test_explicit_enabled_policy_can_execute_one_model_proposal(self):
        await self.enable(allow_likes=True)
        await self.proposal('like')
        self.assertEqual(self.service.data['interactions'][0]['state'], 'completed')
        self.assertEqual(sum(action == 'interact' for action, _ in self.browser.calls), 1)

    async def test_execution_is_persisted_before_dispatch_and_success_requires_receipt(self):
        item = await self.proposal('like')
        await self.enable(allow_likes=True)
        async def inspect(action, payload):
            if action == 'interact':
                saved = json.loads(self.path.read_text('utf-8'))['interactions'][0]
                self.assertEqual(saved['state'], 'executing')
                self.assertEqual(saved['uid'], '123')
                self.assertEqual(saved['attempt_at'], NOW)
        self.browser.before_call = inspect
        result = await self.service.interact(item['id'])
        self.assertEqual(result['state'], 'completed')
        with self.assertRaises(ValueError):
            await self.service.interact(item['id'])
        self.assertEqual(sum(action == 'interact' for action, _ in self.browser.calls), 1)

    async def test_account_mismatch_does_not_send_or_charge_quota(self):
        item = await self.proposal()
        await self.enable(allow_comments=True)
        self.browser.uid = '999'
        with self.assertRaisesRegex(ValueError, '账号'):
            await self.service.interact(item['id'])
        self.assertEqual(item['state'], 'draft')
        self.assertNotIn('attempt_at', item)
        self.assertFalse(any(action == 'interact' for action, _ in self.browser.calls))

    async def test_unknown_dispatch_is_durable_not_retried_and_uses_daily_quota(self):
        item = await self.proposal()
        await self.enable(allow_comments=True, daily_interactions=1)
        self.browser.receipt = RuntimeError('offline receipt lost')
        with self.assertRaises(RuntimeError):
            await self.service.interact(item['id'])
        self.assertEqual(item['state'], 'unknown')
        saved = json.loads(self.path.read_text('utf-8'))['interactions'][0]
        self.assertEqual(saved['state'], 'unknown')
        with self.assertRaises(ValueError):
            await self.service.interact(item['id'])
        # Build an independent pending item without automatically submitting it.
        await self.enable(allow_comments=False)
        second = await self.proposal(url=URL2)
        await self.enable(allow_comments=True)
        with self.assertRaisesRegex(ValueError, '上限'):
            await self.service.interact(second['id'])
        self.assertEqual(sum(action == 'interact' for action, _ in self.browser.calls), 1)

    async def test_false_or_mismatched_receipt_stays_unknown_until_readonly_verify(self):
        item = await self.proposal()
        await self.enable(allow_comments=True)
        self.browser.receipt = dict(status='completed', uid='123', url=URL, action='comment',
                                    text='另一段文字', comment_id='987654321')
        self.assertEqual((await self.service.interact(item['id']))['state'], 'unknown')
        self.browser.receipt = None
        result = await self.service.verify_interaction(item['id'])
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(result['comment_id'], '987654321')
        self.assertEqual(sum(action == 'interact' for action, _ in self.browser.calls), 1)
        self.assertEqual(sum(action == 'verify_interaction' for action, _ in self.browser.calls), 1)

    async def test_restart_converts_executing_to_unknown_without_dispatch(self):
        item = await self.proposal('like')
        await self.enable(allow_likes=True)
        item.update(state='executing', uid='123', attempt_at=NOW)
        self.service._save()
        restored = BilibiliActivity(self.path, self.runner, {}, broker=self.browser, clock=lambda: NOW)
        self.addAsyncCleanup(restored.close)
        self.assertEqual(restored.data['interactions'][0]['state'], 'unknown')
        with self.assertRaises(ValueError):
            await restored.interact(item['id'])
        self.assertFalse(any(action == 'interact' for action, _ in self.browser.calls))

    async def test_failed_persistence_prevents_browser_side_effect(self):
        item = await self.proposal('like')
        await self.enable(allow_likes=True)
        with patch.object(Path, 'replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                await self.service.interact(item['id'])
        self.assertFalse(any(action == 'interact' for action, _ in self.browser.calls))
        self.assertTrue(self.service._storage_error)
        self.assertFalse(self.service.data['settings']['allow_likes'])

    async def test_stop_during_account_check_prevents_dispatch(self):
        item = await self.proposal('like')
        await self.enable(allow_likes=True)
        started, release = asyncio.Event(), asyncio.Event()
        async def wait_for_account(action, payload):
            if action == 'account':
                started.set()
                await release.wait()
        self.browser.before_call = wait_for_account
        task = asyncio.create_task(self.service.interact(item['id']))
        await started.wait()
        await self.service.action(dict(action='stop'))
        release.set()
        with self.assertRaises(ValueError):
            await task
        self.assertFalse(any(action == 'interact' for action, _ in self.browser.calls))
        self.assertNotIn('attempt_at', item)

    async def test_cancel_after_dispatch_preserves_unknown_and_blocks_retry(self):
        item = await self.proposal()
        await self.enable(allow_comments=True)
        dispatched = asyncio.Event()
        async def wait_for_receipt(action, payload):
            if action == 'interact':
                dispatched.set()
                await asyncio.Event().wait()
        self.browser.before_call = wait_for_receipt
        task = asyncio.create_task(self.service.interact(item['id']))
        await dispatched.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(item['state'], 'unknown')
        self.assertEqual(json.loads(self.path.read_text('utf-8'))['interactions'][0]['state'], 'unknown')
        with self.assertRaises(ValueError):
            await self.service.interact(item['id'])

    async def test_hour_windows_include_cross_midnight_and_default_all_day(self):
        for start, end, allowed in ((22, 7, {22, 23, 0, 1, 6}), (9, 18, set(range(9, 18))),
                                    (0, 24, set(range(24)))):
            self.service.data['settings'].update(active_start_hour=start, active_end_hour=end)
            for hour in range(24):
                self.now = NOW + (hour - 12) * 3600
                with self.subTest(start=start, end=end, hour=hour):
                    expected = (hour >= 22 or hour < 7) if start == 22 else hour in allowed
                    self.assertEqual(self.service._within_hours(), expected)

    async def test_legacy_store_migrates_new_capabilities_to_disabled(self):
        old_settings = {k: v for k, v in DEFAULTS.items() if k not in (
            'allow_likes', 'allow_comments', 'daily_interactions', 'active_start_hour', 'active_end_hour')}
        self.path.write_text(json.dumps(dict(settings=old_settings, notes=[], drafts=[], history=[])), 'utf-8')
        restored = BilibiliActivity(self.path, self.runner, {}, broker=self.browser)
        self.addAsyncCleanup(restored.close)
        self.assertEqual(restored.data['diaries'], [])
        self.assertEqual(restored.data['interactions'], [])
        self.assertFalse(restored.data['settings']['allow_likes'])
        self.assertFalse(restored.data['settings']['allow_comments'])


class QQExperienceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        memory = Mock(viewer_note=Mock(return_value=''))
        self.parent = Persona(PersonaConfig(), LLMConfig(), memory,
                              persona_llm=PersonaLLMConfig(backend='inherit'))
        self.backend = Mock(available=True, complete=AsyncMock(return_value='我读到一段花盆排水的介绍。'))
        self.parent._chat_backend = self.backend
        self.service = BilibiliActivity(self.root / 'activity.json', ScriptRunner(), {})
        self.addAsyncCleanup(self.service.close)
        self.service.data['notes'] = [dict(url=URL, title='花盆排水', summary='PUBLIC_NOTE',
            reaction='公开阅读感想', evidence_kind='page_metadata', visibility='public', at=time.time() - 10,
            private_history='PRIVATE_SECRET')]

    async def test_private_session_gets_public_experiences_without_mutating_shared_history(self):
        session = PrivateChatSession(self.parent, self.root / 'private.json', '你')
        self.addAsyncCleanup(session.close)
        session.experience_context = self.service.context_for
        await session.prepare('private-1', '最近在看什么')
        system, user = self.backend.complete.call_args.args
        self.assertIn('PUBLIC_NOTE', user)
        self.assertNotIn('PRIVATE_SECRET', system + user)
        self.assertEqual(list(self.parent._recent_replies), [])

    async def test_group_receives_public_notes_but_not_legacy_private_fields_or_owner_history(self):
        self.parent._recent_replies.append('PRIVATE_OWNER_REPLY')
        legacy = {**self.service.data['notes'][0], 'url': URL2, 'summary': 'LEGACY_NOTE'}
        legacy.pop('visibility')
        self.service.data['notes'].append(legacy)
        session = GroupChatSession(self.parent, self.root / 'group.json', 'app', 'group', 'member')
        self.addAsyncCleanup(session.close)
        session.experience_context = self.service.context_for
        await session.prepare('group-1', '最近在看什么')
        system, user = self.backend.complete.call_args.args
        self.assertIn('PUBLIC_NOTE', user)
        for forbidden in ('PRIVATE_OWNER_REPLY', 'PRIVATE_SECRET', 'LEGACY_NOTE'):
            self.assertNotIn(forbidden, system + user)

    async def test_failed_public_recall_does_not_break_reply(self):
        session = PrivateChatSession(self.parent, self.root / 'private-failed.json', '你')
        self.addAsyncCleanup(session.close)
        session.experience_context = Mock(side_effect=RuntimeError('private internal failure'))
        prepared = await session.prepare('private-2', '最近在看什么')
        self.assertEqual(prepared.text, '我读到一段花盆排水的介绍。')
        self.assertNotIn('private internal failure', str(self.backend.complete.call_args))


if __name__ == '__main__':
    unittest.main()
