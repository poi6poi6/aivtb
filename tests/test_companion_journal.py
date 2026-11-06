"""Private delivered diary quotes, account isolation and durable forgetting."""
import hashlib
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from brain.clock_context import LOCAL_ZONE
from brain.companion_journal import CompanionJournal
from brain.events import Event
from brain.viewer_identity import qq_private_binding, viewer_identity


class CompanionJournalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'journal.json'
        self.now = datetime(2026, 10, 29, 14, 0, tzinfo=LOCAL_ZONE).timestamp()
        self.journal = CompanionJournal(self.path, clock=lambda: self.now)
        self.addAsyncCleanup(self.journal.close)

    def event(self, text='我准备学画画', *, source='companion', owner=True, user='你', ts=None):
        event = Event(source, 'chat', text, user=user, ts=self.now if ts is None else ts)
        event.owner_control = owner
        return event

    @staticmethod
    def memory(event, value='咖啡'):
        return {'owner_key': viewer_identity(event), 'value': value, 'summary': '喜欢' + value,
                'source': {'source': event.source, 'user': event.user, 'event_ts': event.ts,
                           'text_hash': hashlib.sha256(event.text.encode()).hexdigest()[:16]}}

    async def test_record_keeps_quotes_without_promoting_plans_to_completed_events(self):
        entry = await self.journal.record(self.event(), '可以先试试速写。', '开心')
        self.assertIn('我准备学画画', entry['summary'])
        self.assertIn('可以先试试速写', entry['summary'])
        self.assertNotIn('完成', entry['summary'])
        self.assertEqual(entry['visibility'], 'private_local')
        self.assertEqual(entry['date'], '2026-10-29')

    async def test_only_local_owner_delivered_nonempty_turns_are_retained(self):
        for event in [self.event(owner=False), self.event(source='danmaku'), self.event(source='qq_group')]:
            self.assertIsNone(await self.journal.record(event, 'fixture'))
        private = self.event()
        private._qq_private_binding = qq_private_binding('app', 'peer')
        self.assertIsNone(await self.journal.record(private, 'fixture'))
        self.assertIsNone(await self.journal.record(self.event(), ''))
        self.assertEqual(await self.journal.list_entries(), [])

    async def test_repeated_receipts_are_idempotent_and_reply_segments_accumulate(self):
        event = self.event()
        await self.journal.record(event, '第一段。')
        await self.journal.record(event, '第一段。')
        await self.journal.record(event, '第二段。')
        entries = await self.journal.list_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(len(entries[0]['turns']), 1)
        self.assertEqual(entries[0]['turns'][0]['reply'], '第一段。 第二段。')

    async def test_retrieval_is_explicit_owner_scoped_and_bounded(self):
        await self.journal.record(self.event(), '可以。')
        await self.journal.record(self.event('另一个人的私事', user='别人'), '知道了。')
        self.assertEqual(self.journal.context_for(self.event('你好')), '')
        self.assertEqual(self.journal.context_for(self.event('看看日记', owner=False)), '')
        self.assertEqual(self.journal.context_for(self.event('看看日记', source='qq_group')), '')
        context = self.journal.context_for(self.event('看看今天的日记'))
        self.assertIn('学画画', context)
        self.assertNotIn('另一个人', context)
        self.assertLessEqual(len(context), 1700)
        self.now += 86400
        self.assertIn('学画画', self.journal.context_for(self.event('昨天聊了什么')))

    async def test_edit_replaces_old_text_and_blocks_late_original_receipts(self):
        event = self.event()
        entry = await self.journal.record(event, '好。')
        self.now += 10
        self.assertTrue(await self.journal.edit(entry['id'], '实际约定是明天学钢琴。'))
        self.assertIsNone(await self.journal.record(event, '迟到的第二段。'))
        context = self.journal.context_for(self.event('看看日记'))
        self.assertIn('钢琴', context)
        self.assertNotIn('画画', context)
        self.now += 1
        await self.journal.record(self.event('新的练习想法'), '好呀。')
        context = self.journal.context_for(self.event('看看日记'))
        self.assertIn('钢琴', context)
        self.assertIn('新的练习想法', context)

    async def test_delete_survives_restart_and_blocks_old_turn(self):
        event = self.event()
        entry = await self.journal.record(event, '好。')
        self.now += 10
        await self.journal.delete(entry['id'])
        self.journal = CompanionJournal(self.path, clock=lambda: self.now)
        self.assertIsNone(await self.journal.record(event, '旧回执'))
        self.assertEqual(await self.journal.list_entries(), [])

    async def test_clear_owner_does_not_erase_other_diary_and_blocks_inflight_records(self):
        first = self.event()
        await self.journal.record(first, '好。')
        await self.journal.record(self.event('别人的经历', user='别人'), '收到。')
        self.now += 1
        self.assertEqual(await self.journal.clear_owner(viewer_identity('你')), 1)
        self.journal = CompanionJournal(self.path, clock=lambda: self.now)
        self.assertIsNone(await self.journal.record(first, '迟到。'))
        entries = await self.journal.list_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['user'], '别人')
        self.now += 1
        self.assertIsNotNone(await self.journal.record(self.event('新的一天'), '嗯。'))

    async def test_paused_and_sensitive_turns_are_not_saved(self):
        self.assertIsNone(await self.journal.record(self.event('我的密码是123456'), '收到'))
        await self.journal.configure({'enabled': False})
        self.assertIsNone(await self.journal.record(self.event(), '好'))
        self.assertEqual(self.journal.context_for(self.event('日记')), '')
        self.assertEqual(await self.journal.list_entries(), [])

    async def test_memory_change_preserves_unrelated_turns_other_days_and_other_people(self):
        yesterday = self.event('昨天练了钢琴', ts=self.now - 86400)
        await self.journal.record(yesterday, '练习辛苦了。')
        original = self.event('我喜欢咖啡')
        await self.journal.record(original, '记得适量。')
        await self.journal.record(self.event('今天画了山水'), '很有意境。')
        await self.journal.record(self.event('我也喜欢咖啡', user='别人'), '收到。')
        self.now += 1
        self.assertEqual(await self.journal.apply_memory_change(self.memory(original)), 1)
        own = await self.journal.list_entries(owner_key=viewer_identity(original))
        self.assertEqual(len(own), 2)
        text = json.dumps(own, ensure_ascii=False)
        self.assertNotIn('咖啡', text)
        self.assertIn('钢琴', text)
        self.assertIn('山水', text)
        other = await self.journal.list_entries(owner_key=viewer_identity('别人'))
        self.assertIn('咖啡', json.dumps(other, ensure_ascii=False))

    async def test_content_barrier_survives_restart_without_blocking_unrelated_old_or_new_turns(self):
        original = self.event('我喜欢咖啡')
        await self.journal.record(original, '收到。')
        target = self.memory(original)
        self.now += 1
        unseen_related = self.event('我今天还想喝咖啡')
        unseen_unrelated = self.event('我今天画了水彩')
        self.now += 1
        await self.journal.apply_memory_change(target)
        self.assertNotIn('咖啡', self.path.read_text('utf-8'), 'barriers store hashes, not forgotten prose')
        self.journal = CompanionJournal(self.path, clock=lambda: self.now)
        self.addAsyncCleanup(self.journal.close)
        self.assertIsNone(await self.journal.record(original, '迟到的回复'))
        self.assertIsNone(await self.journal.record(unseen_related, '迟到的相关回复'))
        self.assertIsNotNone(await self.journal.record(unseen_unrelated, '迟到但无关的回复'))
        self.now += 1
        self.assertIsNotNone(await self.journal.record(self.event('现在我又想喝咖啡'), '这是新的陈述。'))

    async def test_targeted_change_preserves_independent_manual_summary(self):
        entry = await self.journal.record(self.event('今天去散步了'), '好呀。')
        self.now += 1
        await self.journal.edit(entry['id'], '人工补记：今天沿着河边走了一圈。')
        self.now += 1
        original = self.event('我喜欢咖啡')
        await self.journal.record(original, '收到。')
        self.now += 1
        await self.journal.apply_memory_change(self.memory(original))
        entries = await self.journal.list_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['turns'], [])
        self.assertIn('河边', entries[0]['summary'])
        self.assertNotIn('咖啡', entries[0]['summary'])

    async def test_targeted_change_removes_only_matching_manual_clauses(self):
        original = self.event('我喜欢咖啡')
        entry = await self.journal.record(original, '收到。')
        self.now += 1
        await self.journal.edit(entry['id'], '我喜欢咖啡。今天沿着河边散步，我还画了水彩。')
        self.now += 1
        target = self.memory(original)
        target.update(value='喜欢', predicate='对咖啡的偏好', subject='观众你', summary='观众你喜欢咖啡。')
        await self.journal.apply_memory_change(target)
        context = self.journal.context_for(self.event('看看日记'))
        self.assertNotIn('咖啡', context)
        self.assertIn('河边散步', context)
        self.assertIn('水彩', context)
        self.assertIsNone(await self.journal.record(original, '迟到的原始回复'))
        self.journal = CompanionJournal(self.path, clock=lambda: self.now)
        self.addAsyncCleanup(self.journal.close)
        self.assertNotIn('咖啡', self.journal.context_for(self.event('看看日记')))

    async def test_original_source_hash_handles_long_input_and_manual_memory_edits(self):
        original = self.event('画画的细节。' * 100 + '我喜欢咖啡')
        await self.journal.record(original, '收到。')
        target = self.memory(original, value='人工修订后原文不存在的表达')
        target['original_source'], target['source'] = target['source'], {'source': 'manual'}
        self.now += 1
        self.assertEqual(await self.journal.apply_memory_change(target), 1)
        self.assertIsNone(await self.journal.record(original, '迟到回复'))

    async def test_qq_or_unscoped_memory_cannot_erase_local_diary(self):
        original = self.event('我喜欢咖啡')
        await self.journal.record(original, '收到。')
        qq = self.memory(original)
        qq['private_binding'] = qq_private_binding('app', 'peer')
        self.assertEqual(await self.journal.apply_memory_change(qq), 0)
        self.assertEqual(await self.journal.apply_memory_change({'value': '咖啡'}), 0)
        self.assertEqual(len(await self.journal.list_entries()), 1)

    async def test_failed_write_does_not_create_visible_entry(self):
        with patch('brain.companion_journal.atomic_json_write', side_effect=OSError('fixture')):
            with self.assertRaises(OSError):
                await self.journal.record(self.event(), '好的')
        self.assertEqual(await self.journal.list_entries(), [])


if __name__ == '__main__':
    unittest.main()
