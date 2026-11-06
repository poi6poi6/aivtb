"""Memory edits target one stable identity across short-term and voice state."""
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from brain.conversation import ConversationState, VoiceRevisionWindow, memory_change_owner
from brain.events import Event
from brain.viewer_identity import viewer_identity
from tests.test_runtime_integration import main_function


def event(turn, *, key='a', name='同名', source='danmaku', ts=None):
    value = Event(source, 'chat', f'第{turn}条发言', user=name, viewer_key=key,
                  ts=time.time() - 10 if ts is None else ts)
    value.ui_id = turn
    return value


def memory_for(value):
    return {'slot': 'fact:' + viewer_identity(value), 'subject': '观众' + value.user,
            'owner_key': viewer_identity(value), 'source': {
                'source': value.source, 'user': value.user, 'viewer_key': value.viewer_key}}


class MemoryIdentityControlsTests(unittest.TestCase):
    def test_same_name_accounts_keep_separate_history_cutoffs_and_correction_notes(self):
        state = ConversationState()
        first, other = event(1), event(2, key='b')
        for value in (first, other):
            state.observe_event(value)
            state.record_reply(value, '回答' + value.viewer_key)
        cutoff = time.time() + 1
        state.apply_memory_change(memory_for(first), '账号 A 的修正事实', cutoff)
        self.assertEqual({turn.turn_id for turn in state.turns}, {2})
        state.observe_event(event(3, key='b', ts=cutoff - .1))
        state.observe_event(event(4, key='a', ts=cutoff - .1))
        self.assertEqual({turn.turn_id for turn in state.turns}, {2, 3})
        self.assertNotIn('账号 A 的修正事实', state.context_for(event(5, key='b', ts=cutoff + 1)))
        self.assertIn('账号 A 的修正事实', state.context_for(event(6, ts=cutoff + 1)))

    def test_renamed_account_keeps_identity_and_blocks_late_playback(self):
        state = ConversationState()
        original = event(1, name='旧昵称')
        renamed = event(2, name='新昵称')
        state.observe_event(original)
        state.observe_event(renamed)
        cutoff = time.time() + 1
        state.apply_memory_change(memory_for(original), '人工修正的事实', cutoff)
        self.assertFalse(state.turns)
        payload = dict(source=renamed.source, kind='chat', text=renamed.text,
                       user=renamed.user, viewer_key=renamed.viewer_key, ts=renamed.ts)
        item = SimpleNamespace(event=payload, created_at=renamed.ts, turn_id=renamed.ui_id,
                               text='不该恢复的旧回答', played_seconds=1)
        self.assertIsNone(state.record_playback(item, True))
        self.assertIn('人工修正的事实', state.context_for(event(3, name='新昵称', ts=cutoff + 1)))

    def test_local_voice_and_text_share_owner_but_same_name_platform_does_not(self):
        state = ConversationState()
        local = event(1, key='', source='voice')
        text = event(2, key='', source='companion')
        platform = event(3, source='danmaku')
        for value in (local, text, platform):
            state.observe_event(value)
        state.apply_memory_change(memory_for(local), None, time.time())
        self.assertEqual([turn.turn_id for turn in state.turns], [3])
        self.assertIn('忘记', state.context_for(text))
        self.assertNotIn('忘记', state.context_for(platform))

    def test_voice_revision_changes_only_matching_account_and_accepts_renames(self):
        revisions = VoiceRevisionWindow()
        original = event(1, source='voice')
        other = event(2, key='b', source='voice')
        revisions.latest = other
        revisions.pending = [(original, 'A 回答', '平静'), (other, 'B 回答', '平静')]
        self.assertIsNone(revisions.apply_memory_change(memory_for(original)))
        self.assertIs(revisions.latest, other)
        self.assertEqual([value.ui_id for value, _, _ in revisions.pending], [2])
        renamed = event(3, key='b', name='新昵称', source='voice')
        renamed.corrects = 2
        self.assertTrue(revisions.accepts(renamed))
        impostor = event(4, key='c', source='voice')
        impostor.corrects = 2
        self.assertFalse(revisions.accepts(impostor))
        self.assertEqual(revisions.apply_memory_change(memory_for(renamed)), 2)
        self.assertIsNone(revisions.latest)
        self.assertFalse(revisions.pending)

    def test_global_memory_change_still_clears_every_owner(self):
        state, revisions = ConversationState(), VoiceRevisionWindow()
        first, other = event(1), event(2, key='b')
        state.observe_event(first)
        state.observe_event(other)
        revisions.latest = first
        revisions.pending = [(first, '回答', '平静'), (other, '回答', '平静')]
        core = {'slot': 'core:manual:fixture', 'subject': '角色', 'source': {'source': 'manual'}}
        state.apply_memory_change(core, None, time.time())
        self.assertFalse(state.turns)
        self.assertIn('忘记', state.context_for(first))
        self.assertIn('忘记', state.context_for(other))
        self.assertEqual(revisions.apply_memory_change(core), 1)
        self.assertFalse(revisions.pending)

    def test_unknown_personal_owner_cannot_change_anonymous_or_system_context(self):
        state, revisions = ConversationState(), VoiceRevisionWindow()
        anonymous = event(1, key='', name=None, source='voice')
        system = event(2, key='', name=None, source='system')
        named = event(3, key='', source='voice')
        for value in (anonymous, system, named):
            state.observe_event(value)
        revisions.latest = anonymous
        revisions.pending = [(anonymous, '匿名回答', '平静')]
        unknown = {'slot': 'legacy:unknown', 'subject': '观众同名',
                   'source': {'user': '同名'}}
        cutoff = time.time()
        state.apply_memory_change(unknown, '未知归属的私人事实', cutoff)
        self.assertEqual({turn.turn_id for turn in state.turns}, {1, 2, 3})
        for value in (anonymous, system, named):
            self.assertFalse(state._obsolete(value))
            self.assertNotIn('未知归属的私人事实', state.context_for(value))
        self.assertIsNone(revisions.apply_memory_change(unknown))
        self.assertIs(revisions.latest, anonymous)
        self.assertEqual(len(revisions.pending), 1)


class MainMemoryIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_main_memory_management_preserves_other_account_voice_window(self):
        revisions = VoiceRevisionWindow()
        original = event(1, source='voice')
        other = event(2, key='b', source='voice')
        revisions.latest = other
        revisions.pending = [(other, '待确认回答', '平静')]
        namespace = dict(Event=Event, time=time, register_event=AsyncMock(),
                         conversation=ConversationState(), persona=Mock(),
                         director=Mock(), memory_change_owner=memory_change_owner,
                         qq_channel=None,
                         voice_revisions=revisions, push_event=AsyncMock())
        change = main_function('memory_changed', namespace)
        await change(memory_for(original), '账号 A 的修正事实')
        self.assertIs(revisions.latest, other)
        self.assertEqual(len(revisions.pending), 1)
        namespace['push_event'].assert_not_awaited()
        renamed = event(3, key='b', name='新昵称', source='voice')
        await change(memory_for(renamed), None)
        self.assertIsNone(revisions.latest)
        self.assertFalse(revisions.pending)
        self.assertEqual(namespace['push_event'].await_args.args[0]['id'], 2)


if __name__ == '__main__':
    unittest.main()
