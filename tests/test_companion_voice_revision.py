"""Voice transcripts must pass the existing correction window before writes."""
import asyncio
import hashlib
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from brain.clock_context import LOCAL_ZONE
from brain.companion_features import CompanionFeatures
from brain.conversation import VoiceRevisionWindow, viewer_identity
from brain.events import Event
from tests.test_runtime_integration import main_function


class CompanionVoiceRevisionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.now = datetime(2026, 10, 29, 14, tzinfo=LOCAL_ZONE).timestamp()
        self.features = CompanionFeatures(Path(self.folder.name), clock=lambda: self.now)
        self.addAsyncCleanup(self.features.close)

    def event(self, text, turn, *, source='voice', corrects=None):
        event = Event(source, 'chat', text, user='你', ts=self.now, corrects=corrects)
        event.ui_id, event.owner_control = turn, True
        return event

    async def delivered(self, event, text='收到你的语音，请先确认转写。'):
        await self.features.registered(event)
        await self.features.generated(event, text, '平静', segments=0)

    async def test_voice_calendar_and_journal_wait_for_confirmation(self):
        event = self.event('十分钟后提醒我去机场', 1)
        await self.delivered(event)
        self.assertEqual(await self.features.followups.list_items(), [])
        self.assertEqual(await self.features.journal.list_entries(), [])
        self.assertIn('尚未保存', self.features.context_for(event))
        original_time = self.now
        self.now += 120
        receipt = await self.features.confirm_voice(event)
        self.assertEqual(receipt['status'], 'created')
        self.assertEqual(receipt['item']['due_at'], original_time + 600)
        self.assertEqual(len(await self.features.journal.list_entries()), 1)
        await self.features.confirm_voice(event)
        self.assertEqual(len(await self.features.followups.list_items()), 1)
        self.assertEqual(len((await self.features.journal.list_entries())[0]['turns']), 1)

    async def test_correction_discards_original_schedule_and_diary_even_with_late_receipts(self):
        original = self.event('十分钟后提醒我去机场', 1)
        await self.delivered(original)
        self.now += 10
        corrected = self.event('十分钟后提醒我去公园', 2, corrects=1)
        await self.features.discard_voice(1)
        await self.delivered(corrected, '这次转写是去公园。')
        self.assertIsNone(await self.features.confirm_voice(original))
        await self.features.played(1, 0, True)
        await self.features.confirm_voice(corrected)
        entries = await self.features.journal.list_entries()
        self.assertIn('公园', entries[0]['summary'])
        self.assertNotIn('机场', json.dumps(entries, ensure_ascii=False))
        reminders = await self.features.followups.list_items()
        self.assertEqual([item['title'] for item in reminders], ['去公园'])

    async def test_retraction_and_forgetting_block_unconfirmed_late_delivery(self):
        event = self.event('十分钟后提醒我去机场', 1)
        await self.features.registered(event)
        await self.features.discard_voice(1)
        await self.features.generated(event, '晚到的回复', '平静', segments=0)
        self.assertIsNone(await self.features.confirm_voice(event))
        self.assertEqual(await self.features.followups.list_items(), [])
        self.assertEqual(await self.features.journal.list_entries(), [])
        second = self.event('我喜欢咖啡', 2)
        await self.delivered(second)
        self.now += 1
        await self.features.forget_owner(viewer_identity(second))
        self.assertIsNone(await self.features.confirm_voice(second))
        self.assertEqual(await self.features.journal.list_entries(), [])

    async def test_confirmation_without_delivery_can_schedule_but_cannot_invent_diary(self):
        event = self.event('十分钟后提醒我喝水', 1)
        await self.features.registered(event)
        await self.features.confirm_voice(event)
        self.assertEqual(len(await self.features.followups.list_items()), 1)
        self.assertEqual(await self.features.journal.list_entries(), [])
        await self.features.generated(event, '已交付的回复', '平静', segments=2)
        await self.features.played(1, 0, True)
        self.assertEqual(await self.features.journal.list_entries(), [])
        await self.features.played(1, 1, True)
        self.assertEqual(len(await self.features.journal.list_entries()), 1)

    async def test_late_confirmation_never_moves_old_relative_deadline_forward(self):
        event = self.event('十秒后提醒我喝水', 1)
        await self.delivered(event)
        self.now += 60
        result = await self.features.confirm_voice(event)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(await self.features.followups.list_items(), [])

    async def test_wrong_segment_numbers_cannot_complete_confirmed_voice_diary(self):
        event = self.event('我今天画了一幅画', 1)
        await self.features.registered(event)
        await self.features.confirm_voice(event)
        await self.features.played(1, 2, True)
        await self.features.played(1, 300, True)
        await self.features.generated(event, '画画的事我听到了。', '平静', segments=2)
        await self.features.played(1, 0, True)
        self.assertEqual(await self.features.journal.list_entries(), [])
        await self.features.played(1, 1, True)
        self.assertEqual(len(await self.features.journal.list_entries()), 1)

    async def test_targeted_memory_change_blocks_voice_diary_without_cancelling_schedule(self):
        original = self.event('十分钟后提醒我喝咖啡', 1)
        await self.delivered(original)
        self.now += 1
        target = {'owner_key': viewer_identity(original), 'value': '咖啡',
                  'source': {'source': 'voice', 'user': original.user, 'event_ts': original.ts,
                             'text_hash': hashlib.sha256(original.text.encode()).hexdigest()[:16]}}
        await self.features.apply_memory_change(target)
        receipt = await self.features.confirm_voice(original)
        self.assertEqual(receipt['status'], 'created')
        self.assertEqual((await self.features.followups.list_items())[0]['title'], '喝咖啡')
        self.assertEqual(await self.features.journal.list_entries(), [])
        await self.features.played(1, 0, True)
        self.assertEqual(await self.features.journal.list_entries(), [])

    def main_namespace(self, revisions):
        finalizers = []
        namespace = dict(
            asyncio=asyncio, json=json, Event=Event, viewer_identity=viewer_identity,
            manual_mode=asyncio.Event(), ids=iter(range(100, 200)),
            services=SimpleNamespace(companion=self.features, foreground=Mock()),
            media_narrator=SimpleNamespace(cancel=AsyncMock()), voice_revisions=revisions,
            responding=asyncio.Event(), playback=SimpleNamespace(current=None, queue=[], paused=False),
            speech_pipeline=SimpleNamespace(busy=False, interrupt_before=AsyncMock()),
            persist_delivery=AsyncMock(), personality=Mock(), push_event=AsyncMock(),
            is_backchannel=lambda _: False, agent_tasks=None, webui=None,
            interrupt_generation=asyncio.Event(), arbiter=Mock(), metrics=Mock(),
            conversation=Mock(), persona=Mock(), director=Mock(), memory=Mock(), learning=Mock(),
            track_finalizer=finalizers.append,
        )
        namespace['arbiter'].discard_before.return_value = []
        namespace['arbiter'].summary.return_value = {}
        namespace['learning'].priority_bonus.return_value = 0
        return namespace, finalizers

    async def test_main_explicit_confirmation_commits_original_voice_once(self):
        event = self.event('十分钟后提醒我喝水', 1)
        await self.delivered(event)
        revisions = VoiceRevisionWindow()
        revisions.latest = event
        revisions.stage(event, '已交付回答', '平静')
        namespace, _ = self.main_namespace(revisions)
        register = main_function('register_event', namespace)
        confirm = self.event(event.text, 2, corrects=1)
        confirm.confirm_memory = True
        await register(confirm)
        self.assertIsNone(revisions.latest)
        self.assertEqual(len(await self.features.followups.list_items()), 1)
        self.assertEqual(len(await self.features.journal.list_entries()), 1)
        self.assertIn('日程已安排', namespace['push_event'].await_args.args[0]['text'])

    async def test_main_advance_commits_previous_voice_and_supplies_actual_receipt(self):
        previous = self.event('十分钟后提醒我喝水', 1)
        await self.delivered(previous)
        revisions = VoiceRevisionWindow()
        revisions.latest = previous
        revisions.stage(previous, '已交付回答', '平静')
        namespace, finalizers = self.main_namespace(revisions)
        register = main_function('register_event', namespace)
        self.now += 60
        next_event = self.event('我们聊聊画画', 2, source='companion')
        await register(next_event)
        await asyncio.gather(*finalizers)
        self.assertEqual(len(await self.features.followups.list_items()), 1)
        self.assertEqual(len(await self.features.journal.list_entries()), 1)
        self.assertIn('created', self.features.context_for(next_event))
        namespace['persist_delivery'].assert_awaited_once()

    async def test_main_correction_discard_then_advance_only_saves_corrected_voice(self):
        previous = self.event('十分钟后提醒我去机场', 1)
        await self.delivered(previous)
        revisions = VoiceRevisionWindow()
        revisions.latest = previous
        revisions.stage(previous, '已交付回答', '平静')
        namespace, finalizers = self.main_namespace(revisions)
        register = main_function('register_event', namespace)
        self.now += 10
        correction = self.event('十分钟后提醒我去公园', 2, corrects=1)
        await register(correction)
        await self.features.generated(correction, '转写已更正。', '平静', segments=0)
        revisions.stage(correction, '转写已更正。', '平静')
        self.assertEqual(await self.features.followups.list_items(), [])
        self.assertEqual(await self.features.journal.list_entries(), [])
        self.now += 1
        await register(self.event('好的', 3, source='companion'))
        await asyncio.gather(*finalizers)
        self.assertEqual([item['title'] for item in await self.features.followups.list_items()], ['去公园'])
        self.assertNotIn('机场', json.dumps(await self.features.journal.list_entries(), ensure_ascii=False))

    async def test_main_retraction_closes_window_without_persistent_side_effects(self):
        previous = self.event('十分钟后提醒我去机场', 1)
        await self.delivered(previous)
        revisions = VoiceRevisionWindow()
        revisions.latest = previous
        revisions.stage(previous, '已交付回答', '平静')
        namespace, finalizers = self.main_namespace(revisions)
        register = main_function('register_event', namespace)
        retraction = self.event('', 2, corrects=1)
        retraction.retract = True
        await register(retraction)
        self.assertIsNone(revisions.latest)
        self.assertEqual(revisions.pending, [])
        self.assertEqual(finalizers, [])
        self.assertIsNone(await self.features.confirm_voice(previous))
        await self.features.played(1, 0, True)
        self.assertEqual(await self.features.followups.list_items(), [])
        self.assertEqual(await self.features.journal.list_entries(), [])
        self.assertEqual(namespace['push_event'].await_args.args[0]['type'], 'voice_retracted')


if __name__ == '__main__':
    unittest.main()
