"""Real local stores with simulated host delivery; no model or channel traffic."""
import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from brain.clock_context import LOCAL_ZONE
from brain.companion_features import CompanionFeatures
from brain.events import Event
from brain.viewer_identity import qq_private_binding, viewer_identity


class CompanionFeaturesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.now = datetime(2026, 10, 29, 14, 0, tzinfo=LOCAL_ZONE).timestamp()
        self.features = CompanionFeatures(self.root, clock=lambda: self.now)
        self.addAsyncCleanup(self.features.close)

    def event(self, text='我准备学画画', *, turn=1, owner=True, source='companion', user='你'):
        event = Event(source, 'chat', text, user=user, ts=self.now)
        event.ui_id, event.owner_control = turn, owner
        return event

    async def test_registered_request_creates_real_reminder_and_truthful_context(self):
        event = self.event('十分钟后提醒我喝水')
        await self.features.registered(event)
        items = await self.features.followups.list_items()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['due_at'], self.now + 600)
        self.assertIn('created', self.features.context_for(event))
        self.assertIn('已安排', self.features.context_for(event))
        await self.features.registered(event)
        self.assertEqual(len(await self.features.followups.list_items()), 1)
        vague = self.event('晚点再聊', turn=2)
        await self.features.registered(vague)
        self.assertIn('needs_time', self.features.context_for(vague))
        self.assertEqual(len(await self.features.followups.list_items()), 1)

    async def test_registration_failure_never_reports_scheduled(self):
        event = self.event('十分钟后提醒我喝水')
        with patch.object(self.features.followups, 'observe_event', side_effect=OSError('fixture')):
            await self.features.registered(event)
        self.assertIn('未完成', event.companion_receipt)
        self.assertEqual(await self.features.followups.list_items(), [])

    async def test_friend_and_qq_cannot_create_or_retrieve_local_data(self):
        event = self.event('十分钟后提醒我喝水')
        await self.features.registered(event)
        await self.features.generated(event, '已经安排。', '平静', segments=0)
        outsider_events = [self.event('看看日记和今天的提醒', owner=False),
                           self.event('看看日记和今天的提醒', source='qq_group')]
        qq = self.event('看看日记和今天的提醒')
        qq._qq_private_binding = qq_private_binding('app', 'peer')
        outsider_events.append(qq)
        for outsider in outsider_events:
            self.assertNotIn('喝水', self.features.context_for(outsider))
            outsider.text = '十分钟后提醒我看电影'
            await self.features.registered(outsider)
        self.assertEqual(len(await self.features.followups.list_items()), 1)

    async def test_only_all_expected_segments_write_journal_once(self):
        event = self.event()
        await self.features.registered(event)
        await self.features.generated(event, '第一段和第二段。', '开心', segments=2)
        self.assertEqual(await self.features.journal.list_entries(), [])
        await self.features.played(1, 0, True)
        await self.features.played(1, 0, True)
        self.assertEqual(await self.features.journal.list_entries(), [])
        await self.features.played(1, 1, True)
        entries = await self.features.journal.list_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(len(entries[0]['turns']), 1)
        await self.features.played(1, 1, True)
        self.assertEqual(len((await self.features.journal.list_entries())[0]['turns']), 1)

    async def test_out_of_order_receipts_wait_for_generation_manifest(self):
        event = self.event()
        await self.features.registered(event)
        await self.features.played(1, 0, True)
        self.assertEqual(await self.features.journal.list_entries(), [])
        await self.features.generated(event, '已播完。', '开心', segments=1)
        self.assertEqual(len(await self.features.journal.list_entries()), 1)

    async def test_failed_generation_or_segment_never_writes_diary(self):
        first = self.event(turn=1)
        await self.features.generated(first, '不能保存的失败文本。', '平静', segments=0, successful=False)
        second = self.event(turn=2)
        await self.features.played(2, 20, False)
        await self.features.generated(second, '未完全播出的文本。', '平静', segments=2)
        await self.features.played(2, 21, True)
        self.assertEqual(await self.features.journal.list_entries(), [])

    async def test_receipt_can_retry_after_persistence_failure(self):
        event = self.event()
        original = self.features.journal.record
        with patch.object(self.features.journal, 'record', side_effect=OSError('fixture')):
            with self.assertRaises(OSError):
                await self.features.generated(event, '已送达。', '平静', segments=0)
        self.assertEqual(await self.features.journal.list_entries(), [])
        with patch.object(self.features.journal, 'record', wraps=original):
            await self.features.generated(event, '已送达。', '平静', segments=0)
        self.assertEqual(len(await self.features.journal.list_entries()), 1)

    async def test_forget_handles_early_receipt_and_blocks_old_generation(self):
        event = self.event()
        await self.features.registered(event)
        await self.features.played(1, 10, True)
        self.now += 1
        await self.features.forget_owner(viewer_identity(event))
        await self.features.generated(event, '迟到的完整回复。', '平静', segments=1)
        self.assertEqual(await self.features.journal.list_entries(), [])

    async def test_start_queue_claim_and_receipt_follow_same_lifecycle(self):
        await self.features.followups.create('喝水', self.now + 600)
        self.now += 601
        queue = asyncio.Queue()
        self.features.start(queue, idle=lambda: True, connected=lambda: True)
        event = await asyncio.wait_for(queue.get(), 2)
        self.assertEqual(event.kind, 'followup')
        self.assertTrue(await self.features.valid_followup(event))
        event.ui_id = 8
        await self.features.registered(event)
        await self.features.generated(event, '到了约好的时间，喝口水吧。', '平静', segments=1)
        self.assertEqual((await self.features.followups.list_items())[0]['status'], 'claimed')
        await self.features.played(8, 0, True)
        self.assertEqual((await self.features.followups.list_items())[0]['status'], 'delivered')
        self.assertEqual(await self.features.journal.list_entries(), [], 'scheduled text is not a new user event')

    async def test_human_foreground_invalidates_queued_followup(self):
        await self.features.followups.create('喝水', self.now + 600)
        self.now += 601
        queue = asyncio.Queue()
        self.features.start(queue, idle=lambda: True, connected=lambda: True)
        event = await asyncio.wait_for(queue.get(), 2)
        self.now += .01
        self.features.foreground()
        self.assertFalse(await self.features.valid_followup(event))
        self.assertEqual((await self.features.followups.list_items())[0]['status'], 'cancelled')

    async def test_foreground_revision_invalidates_even_without_clock_advancing(self):
        await self.features.followups.create('喝水', self.now + 600)
        self.now += 601
        queue = asyncio.Queue()
        self.features.start(queue, idle=lambda: True, connected=lambda: True)
        event = await asyncio.wait_for(queue.get(), 2)
        self.features.foreground()
        self.assertFalse(await self.features.valid_followup(event))

    async def test_queued_drop_and_started_timeout_do_not_block_future_reminders(self):
        await self.features.followups.create('喝水', self.now + 600)
        self.now += 601
        queue = asyncio.Queue()
        self.features.start(queue, idle=lambda: True, connected=lambda: True)
        event = await asyncio.wait_for(queue.get(), 2)
        self.now += 31
        await self.features._settle_stale_claims()
        self.assertFalse(await self.features.valid_followup(event))
        self.assertEqual((await self.features.followups.list_items())[0]['status'], 'cancelled')
        await self.features.followups.create('散步', self.now + 600)
        self.now += 601
        event = await asyncio.wait_for(queue.get(), 2)
        self.assertTrue(await self.features.valid_followup(event))
        self.now += 601
        await self.features._settle_stale_claims()
        statuses = {item['title']: item['status'] for item in await self.features.followups.list_items()}
        self.assertEqual(statuses['散步'], 'unknown')

    async def test_abandon_before_generation_settles_followup_and_rejects_late_receipts(self):
        await self.features.followups.create('喝水', self.now + 600)
        self.now += 601
        queue = asyncio.Queue()
        self.features.start(queue, idle=lambda: True, connected=lambda: True)
        event = await asyncio.wait_for(queue.get(), 2)
        event.ui_id = 20
        await self.features.abandon(event, 'cancelled')
        await self.features.generated(event, '已经失效的提醒', '平静', segments=0)
        self.assertEqual((await self.features.followups.list_items())[0]['status'], 'cancelled')

    async def test_forgetting_diary_does_not_cancel_explicit_calendar(self):
        event = self.event('十分钟后提醒我喝水')
        await self.features.registered(event)
        await self.features.generated(event, '已安排。', '平静', segments=0)
        self.now += 1
        await self.features.forget_owner(viewer_identity(event))
        self.assertEqual(await self.features.journal.list_entries(), [])
        self.assertEqual((await self.features.followups.list_items())[0]['status'], 'pending')

    async def test_memory_management_removes_only_related_diary_and_pending_receipts(self):
        from brain.autobiographical_memory import AutobiographicalMemory
        from brain.config import LLMConfig, SelectiveMemoryConfig, WebUIConfig
        from brain.webui import WebUI
        memory = AutobiographicalMemory(
            SelectiveMemoryConfig(state_path=str(self.root / 'memory.json'),
                                  core_memories_path=str(self.root / 'missing.yaml'), llm_extraction=False),
            LLMConfig(api_key_env='NO_COMPANION_INTEGRATION_TEST_KEY'), '测试角色', clock=lambda: self.now)
        self.addAsyncCleanup(memory.close)
        event = self.event('我喜欢咖啡')
        stored = (await memory.consolidate(event, '知道了。', '平静'))[0]
        await self.features.generated(event, '知道了。', '平静', segments=0)
        unrelated = self.event('我喜欢画画', turn=2)
        await self.features.generated(unrelated, '画画真不错。', '平静', segments=0)
        related_pending = self.event('我也喜欢咖啡', turn=3)
        await self.features.registered(related_pending)
        await self.features.generated(related_pending, '知道了。', '平静', segments=1)
        unrelated_pending = self.event('我练了钢琴', turn=4)
        await self.features.registered(unrelated_pending)
        await self.features.generated(unrelated_pending, '练琴辛苦了。', '平静', segments=1)
        await self.features.followups.create('喝水', self.now + 600)
        ui = WebUI(WebUIConfig(), asyncio.Queue(), str(self.root), {},
                   selective_memory=memory, workspace_root=self.root)
        self.addAsyncCleanup(ui.close)
        ui.companion, ui._send_socket = self.features, Mock()
        self.now += 1
        await ui._manage_memory({'action': 'forget', 'id': stored['id']}, object())
        entries = await self.features.journal.list_entries()
        self.assertEqual([turn['input'] for turn in entries[0]['turns']], ['我喜欢画画'])
        self.assertEqual((await self.features.followups.list_items())[0]['status'], 'pending')
        await self.features.generated(event, '迟到的旧回复', '平静', segments=0)
        await self.features.played(3, 0, True)
        await self.features.played(4, 0, True)
        entries = await self.features.journal.list_entries()
        self.assertEqual([turn['input'] for turn in entries[0]['turns']], ['我喜欢画画', '我练了钢琴'])

    async def test_memory_edit_preserves_unrelated_diary_and_manual_summary(self):
        from brain.autobiographical_memory import AutobiographicalMemory
        from brain.config import LLMConfig, SelectiveMemoryConfig, WebUIConfig
        from brain.webui import WebUI
        memory = AutobiographicalMemory(
            SelectiveMemoryConfig(state_path=str(self.root / 'memory.json'),
                                  core_memories_path=str(self.root / 'missing.yaml'), llm_extraction=False),
            LLMConfig(api_key_env='NO_COMPANION_INTEGRATION_TEST_KEY'), '测试角色', clock=lambda: self.now)
        self.addAsyncCleanup(memory.close)
        manual = await self.features.journal.record(self.event('散步回来了'), '欢迎回来。')
        self.now += 1
        await self.features.journal.edit(manual['id'], '今天沿着河边散步。')
        self.now += 1
        event = self.event('我喜欢咖啡', turn=2)
        stored = (await memory.consolidate(event, '知道了。', '平静'))[0]
        await self.features.generated(event, '知道了。', '平静', segments=0)
        other = self.event('我在练钢琴', turn=3)
        await self.features.generated(other, '慢慢练。', '平静', segments=0)
        ui = WebUI(WebUIConfig(), asyncio.Queue(), str(self.root), {},
                   selective_memory=memory, workspace_root=self.root)
        self.addAsyncCleanup(ui.close)
        ui.companion, ui._send_socket = self.features, Mock()
        self.now += 1
        await ui._manage_memory({'action': 'edit', 'id': stored['id'], 'text': '我现在更喜欢红茶'}, object())
        entries = await self.features.journal.list_entries()
        self.assertEqual([turn['input'] for turn in entries[0]['turns']], ['我在练钢琴'])
        self.assertIn('河边', entries[0]['summary'])
        self.assertNotIn('咖啡', entries[0]['summary'])
        self.assertIsNone(await self.features.journal.record(event, '迟到的旧内容'))

    async def test_no_turn_id_cannot_merge_unrelated_delivery_records(self):
        first = self.event()
        del first.ui_id
        await self.features.generated(first, '无编号不应该保存', '平静', segments=0)
        await self.features.played(None, 0, True)
        self.assertEqual(await self.features.journal.list_entries(), [])
        self.assertNotIn(None, self.features._deliveries)

    async def test_foreground_during_claim_does_not_queue_old_intent(self):
        await self.features.followups.create('喝水', self.now + 600)
        self.now += 601
        original = self.features.followups.claim_due
        complete = asyncio.Event()

        async def interrupted_claim(**kwargs):
            claim = await original(**kwargs)
            self.now += .01
            self.features.foreground()
            complete.set()
            return claim

        queue = asyncio.Queue()
        with patch.object(self.features.followups, 'claim_due', side_effect=interrupted_claim):
            self.features.start(queue, idle=lambda: True, connected=lambda: True)
            await asyncio.wait_for(complete.wait(), 2)
            # Give the scheduler one turn to settle its post-claim decision.
            await asyncio.sleep(.05)
            self.assertTrue(queue.empty())
        self.assertEqual((await self.features.followups.list_items())[0]['status'], 'cancelled')

    async def test_restart_of_unconfirmed_claim_never_resends(self):
        await self.features.followups.create('喝水', self.now + 600)
        self.now += 601
        claim = await self.features.followups.claim_due(idle=True)
        await self.features.close()
        restarted = CompanionFeatures(self.root, clock=lambda: self.now)
        self.addAsyncCleanup(restarted.close)
        self.assertEqual((await restarted.followups.list_items())[0]['status'], 'unknown')
        self.assertIsNone(await restarted.followups.claim_due(idle=True))
        self.assertFalse(restarted.followups.is_claim_active(claim['token']))

    async def test_close_stops_loop_and_rejects_late_diary(self):
        queue = asyncio.Queue()
        self.features.start(queue, idle=lambda: True, connected=lambda: True)
        await self.features.close()
        self.assertTrue(self.features._task.done())
        event = self.event()
        await self.features.generated(event, '关闭后的旧回执。', '平静', segments=0)
        self.assertEqual(await self.features.journal.list_entries(), [])


if __name__ == '__main__':
    unittest.main()
