import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from brain.config import LLMConfig, PersonaConfig
from brain.context_preparation import prepare_context
from brain.events import Event
from brain.memory import Memory
from brain.persona import Persona
from brain.tarot_prompt import reading_prompt
from tests.test_runtime_integration import main_function


def reading_event():
    event = Event('system', 'ambient', '我该如何面对新工作？', user='观众甲', forced=True)
    event.ui_id = 10
    event.tarot_session_id = 'session'
    event.tarot_reading_id = 'reading'
    event.tarot_attempt_id = 'attempt'
    event.tarot_context = json.dumps({'user': '观众甲', 'question': event.text,
        'cards': [{'name': '星星', 'orientation': 'reversed', 'label': '眼下',
                   'keywords': ['修复', '耐心'], 'meaning': '可以留一点时间给自己。'}]}, ensure_ascii=False)
    return event


class TarotPersonaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        self.persona = Persona(PersonaConfig(), LLMConfig(), Memory(str(root/'m.jsonl'), str(root/'v.json')))

    def test_reading_preserves_real_cards_and_excludes_private_context(self):
        self.persona._recent_replies.append('PRIVATE_PREVIOUS_REPLY')
        self.persona.personality = SimpleNamespace(context_for=Mock(side_effect=AssertionError('private personality')))
        self.persona.learning = SimpleNamespace(strategy_context_for=Mock(side_effect=AssertionError('history')))
        self.persona.memory.viewer_note = Mock(side_effect=AssertionError('private viewer note'))
        event = reading_event()
        payload = self.persona._request_payload(event, extra_context='PRIVATE_HISTORY')
        text = json.dumps(payload, ensure_ascii=False)
        self.assertIn('星星', text)
        self.assertIn('reversed', text)
        self.assertIn('不自行重抽', text)
        self.assertIn('娱乐性', text)
        self.assertNotIn('PRIVATE_', text)
        self.assertGreaterEqual(self.persona.reply_segment_limit(event), 4)

    async def test_tarot_skips_both_external_memory_reads(self):
        autobiography = SimpleNamespace(context_for=AsyncMock(side_effect=AssertionError('memory read')))
        cognition = SimpleNamespace(context_for=AsyncMock(side_effect=AssertionError('cognition read')))
        context, status = await prepare_context(reading_event(), autobiography, cognition)
        self.assertFalse(context)
        self.assertTrue(all(item['status'] == 'skipped' for item in status.values()))
        autobiography.context_for.assert_not_awaited()
        cognition.context_for.assert_not_awaited()

    def test_guided_stages_carry_only_this_guests_turn_and_have_a_real_ending(self):
        event = reading_event()
        data = json.loads(event.tarot_context)
        data.update(hosting='guided', clarification='我更想聊怎么开始',
                    response='我其实是担心画得不好', prior_reading='先给自己一张草稿的时间')
        event.tarot_context = json.dumps(data, ensure_ascii=False)
        event.tarot_stage = 'welcome'
        prompt = reading_prompt(event)
        self.assertIn('此时尚未选牌', prompt)
        self.assertIn('只问一个', prompt)
        self.assertEqual(self.persona.reply_segment_limit(event), 3)
        event.tarot_stage = 'reading'
        prompt = reading_prompt(event)
        self.assertIn('按cards顺序逐张', prompt)
        self.assertIn('停下来等待', prompt)
        self.assertEqual(self.persona.reply_segment_limit(event), 6)
        event.tarot_stage = 'closing'
        prompt = reading_prompt(event)
        self.assertIn('我其实是担心画得不好', prompt)
        self.assertIn('先给自己一张草稿的时间', prompt)
        self.assertIn('不要再提问', prompt)
        self.assertIn('不能说对方不懂自己', prompt)
        event.tarot_stage = 'unknown'
        with self.assertRaises(ValueError):
            reading_prompt(event)

    async def test_tarot_never_searches_or_remembers_the_generated_reading(self):
        self.persona.web_search = SimpleNamespace(search=AsyncMock(side_effect=AssertionError('no research')))
        self.assertIsNone(await self.persona._research_for(reading_event()))
        # No configured provider: even the error fallback must not enter later private conversations.
        await self.persona.reply(reading_event())
        self.assertEqual(list(self.persona._recent_replies), [])

    async def test_finish_cancels_pending_and_synthesized_work_for_same_reading(self):
        event = reading_event()
        tarot = SimpleNamespace(valid_event=Mock(return_value=False))
        pipeline = SimpleNamespace(cancel_turn=AsyncMock())
        arbiter = SimpleNamespace(remove_turn=Mock())
        turns = {event.ui_id: event}
        namespace = dict(tarot=tarot, tarot_events=turns, arbiter=arbiter,
                         speech_pipeline=pipeline, cancelled_questions=set(), services=None)
        main_function('companion_event_current', namespace)
        cancel = main_function('cancel_invalid_tarot', namespace)
        await cancel()
        self.assertIn(event.ui_id, namespace['cancelled_questions'])
        arbiter.remove_turn.assert_called_once_with(event.ui_id)
        pipeline.cancel_turn.assert_awaited_once_with(event.ui_id)
        self.assertFalse(turns)

    def test_tarot_delivery_cannot_become_private_or_autobiographical_memory(self):
        store = SimpleNamespace(mark_result=Mock(), played=Mock())
        namespace = dict(tarot=store)
        audit = main_function('audit_delivery', namespace)
        item = SimpleNamespace(event={'tarot_reading_id': 'reading'}, segment_index=0)
        audit(item, True)
        store.mark_result.assert_not_called()
        audit(item, False)
        store.mark_result.assert_called_once()
        # Actual delivered() must return before conversation, journal, or personality hooks.
        namespace['audit_delivery'] = Mock()
        delivered = main_function('delivered', namespace)
        delivered(item, True)
        namespace['audit_delivery'].assert_called_once()
        store.played.assert_called_once_with(item.event, 0, True)


if __name__ == '__main__':
    unittest.main()
