"""Offline scheduling, local ownership, persistence and delivery receipts."""
import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from brain.clock_context import LOCAL_ZONE
from brain.events import Event
from brain.followups import FollowupService, parse_reminder
from brain.viewer_identity import qq_private_binding, viewer_identity


class FollowupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'followups.json'
        self.now = datetime(2026, 10, 29, 14, 0, tzinfo=LOCAL_ZONE).timestamp()
        self.service = FollowupService(self.path, clock=lambda: self.now)
        self.addAsyncCleanup(self.service.close)

    def event(self, text, *, source='companion', owner=True, user='你'):
        event = Event(source, 'chat', text, user=user, ts=self.now)
        event.owner_control = owner
        return event

    async def create(self, **kwargs):
        return await self.service.create('喝水', self.now + 600, **kwargs)

    def test_relative_and_absolute_chinese_times(self):
        examples = [('十分钟后提醒我喝水', 600), ('提醒我十分钟后喝水', 600),
                    ('半小时后提醒我休息', 1800), ('两小时后和我聊画画', 7200)]
        for text, delay in examples:
            with self.subTest(text=text):
                result = parse_reminder(text, self.now)
                self.assertEqual(result['status'], 'parsed')
                self.assertEqual(result['due_at'], self.now + delay)
        for text in ['明天晚上八点提醒我喝水', '提醒我明天晚上八点喝水']:
            result = parse_reminder(text, self.now)
            self.assertEqual(result['due_at'], datetime(2026, 10, 30, 20, tzinfo=LOCAL_ZONE).timestamp())

    def test_vague_invalid_and_past_times_are_not_scheduled(self):
        for text in ['晚点再聊', '明天提醒我喝水', '今天八点提醒我喝水',
                     '9999天后提醒我喝水', '明天二十五点提醒我喝水']:
            with self.subTest(text=text):
                self.assertEqual(parse_reminder(text, self.now)['status'], 'needs_time')
        self.assertIsNone(parse_reminder('这是一段普通聊天', self.now))

    async def test_only_verified_local_owner_can_schedule(self):
        for event in [self.event('十分钟后提醒我喝水', owner=False),
                      self.event('十分钟后提醒我喝水', source='danmaku'),
                      self.event('十分钟后提醒我喝水', source='qq_group')]:
            self.assertIsNone(await self.service.observe_event(event))
        private = self.event('十分钟后提醒我喝水')
        private._qq_private_binding = qq_private_binding('app', 'peer')
        self.assertIsNone(await self.service.observe_event(private))
        self.assertEqual(await self.service.list_items(), [])
        result = await self.service.observe_event(self.event('十分钟后提醒我喝水'))
        self.assertEqual(result['status'], 'created')
        self.assertEqual(len(await self.service.list_items()), 1)

    async def test_event_retries_are_idempotent_and_do_not_reactivate_cancelled(self):
        event = self.event('十分钟后提醒我喝水')
        first = await self.service.observe_event(event)
        second = await self.service.observe_event(event)
        self.assertEqual(first['item']['id'], second['item']['id'])
        await self.service.cancel(first['item']['id'])
        repeated = await self.service.observe_event(event)
        self.assertEqual(repeated['status'], 'cancelled')
        self.assertEqual(len(await self.service.list_items()), 1)

    async def test_claim_is_persisted_once_and_only_receipt_completes(self):
        await self.create()
        self.now += 601
        claim = await self.service.claim_due(idle=True)
        self.assertIsNotNone(claim)
        self.assertIsNone(await self.service.claim_due(idle=True))
        self.assertEqual((await self.service.list_items())[0]['status'], 'claimed')
        self.assertFalse(await self.service.resolve('wrong-token'))
        self.assertTrue(await self.service.resolve(claim['token']))
        self.assertFalse(await self.service.resolve(claim['token']))
        self.assertEqual((await self.service.list_items())[0]['status'], 'delivered')

    async def test_restart_restores_pending_but_does_not_replay_unknown_delivery(self):
        await self.create()
        self.service = FollowupService(self.path, clock=lambda: self.now)
        self.assertEqual((await self.service.list_items())[0]['status'], 'pending')
        self.now += 601
        claim = await self.service.claim_due(idle=True)
        self.service = FollowupService(self.path, clock=lambda: self.now)
        self.assertEqual((await self.service.list_items())[0]['status'], 'unknown')
        self.assertFalse(self.service.is_claim_active(claim['token']))
        self.assertIsNone(await self.service.claim_due(idle=True))

    async def test_busy_foreground_quiet_hours_and_disabled_pause_claims(self):
        await self.create()
        self.now += 601
        self.assertIsNone(await self.service.claim_due(idle=False))
        self.assertIsNone(await self.service.claim_due(idle=True, last_activity_at=self.now - 10))
        await self.service.configure({'enabled': False})
        self.assertIsNone(await self.service.claim_due(idle=True))
        await self.service.configure({'enabled': True})
        self.now = datetime(2026, 10, 29, 23, 30, tzinfo=LOCAL_ZONE).timestamp()
        self.assertIsNone(await self.service.claim_due(idle=True))
        self.now = datetime(2026, 10, 30, 8, 0, tzinfo=LOCAL_ZONE).timestamp()
        self.assertIsNotNone(await self.service.claim_due(idle=True))

    async def test_stale_backlog_expires_instead_of_flooding(self):
        await self.create()
        self.now += 2 * 86400
        self.assertIsNone(await self.service.claim_due(idle=True))
        self.assertEqual((await self.service.list_items())[0]['status'], 'expired')

    async def test_daily_budget_gap_and_claim_cancellation(self):
        await self.service.configure({'daily_budget': 1})
        first = await self.create()
        await self.service.create('画画', self.now + 610)
        self.now += 611
        claim = await self.service.claim_due(idle=True)
        self.assertEqual(claim['id'], first['id'])
        await self.service.cancel(first['id'])
        self.assertFalse(self.service.is_claim_active(claim['token']))
        self.assertFalse(await self.service.resolve(claim['token']))
        self.now += 301
        self.assertIsNone(await self.service.claim_due(idle=True))

    async def test_calendar_edit_reschedule_cancel_and_history(self):
        item = await self.create()
        item = await self.service.update(item['id'], title='散步', due_at=self.now + 900)
        self.assertEqual(item['title'], '散步')
        self.assertTrue(await self.service.cancel(item['id']))
        with self.assertRaises(ValueError):
            await self.service.update(item['id'], title='不能无时间恢复')
        item = await self.service.update(item['id'], due_at=self.now + 1200)
        self.assertEqual(item['status'], 'pending')
        self.assertEqual(len(await self.service.list_items()), 1)

    async def test_context_and_clear_are_owner_scoped(self):
        await self.create(user='你')
        await self.service.create('另一个人的约定', self.now + 900, user='别人')
        own = self.service.context_for(self.event('今天有什么安排'))
        self.assertIn('喝水', own)
        self.assertNotIn('另一个人的约定', own)
        self.assertEqual(self.service.context_for(self.event('今天有什么安排', owner=False)), '')
        self.assertEqual(self.service.context_for(self.event('今天有什么安排', source='qq_group')), '')
        self.assertEqual(await self.service.clear_owner(viewer_identity('你')), 1)
        self.assertNotIn('喝水', self.service.context_for(self.event('今天有什么安排')))

    async def test_failed_write_never_claims_or_reports_creation(self):
        with patch('brain.followups.atomic_json_write', side_effect=OSError('fixture')):
            with self.assertRaises(OSError):
                await self.create()
        self.assertEqual(await self.service.list_items(), [])
        await self.create()
        self.now += 601
        with patch('brain.followups.atomic_json_write', side_effect=OSError('fixture')):
            with self.assertRaises(OSError):
                await self.service.claim_due(idle=True)
        self.assertEqual((await self.service.list_items())[0]['status'], 'pending')

    async def test_bad_store_stays_blocked_and_is_not_overwritten(self):
        self.path.write_text('{broken', encoding='utf-8')
        service = FollowupService(self.path, clock=lambda: self.now)
        self.assertFalse(service.summary()['available'])
        with self.assertRaises(ValueError):
            await service.create('喝水', self.now + 600)
        self.assertEqual(self.path.read_text(encoding='utf-8'), '{broken')


if __name__ == '__main__':
    unittest.main()
