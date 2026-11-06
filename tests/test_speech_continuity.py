"""Offline streaming contracts for grouping already-arrived speech phrases."""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from brain.config import LLMConfig, PersonaConfig
from brain.events import Event
from brain.memory import Memory
from brain.persona import Persona
from brain.speech_text import can_join_ready_speech


class ReadySpeechBoundaryTests(unittest.TestCase):
    def test_short_ack_and_short_statements_keep_their_punctuation(self):
        self.assertTrue(can_join_ready_speech('对。', '我更喜欢后半段。'))
        self.assertTrue(can_join_ready_speech('前半段很轻。', '后半段的节奏更快。'))

    def test_questions_early_commas_unfinished_and_long_clauses_stay_separate(self):
        for left, right in [('你也这么想？', '我更喜欢后半段。'),
                            ('对。', '你觉得呢？'), ('先说这里，', '后面的部分再展开。'),
                            ('对。', '后面还有'), ('慢' * 25 + '。', '后半段更快。'),
                            ('对。', '长' * 49 + '。')]:
            with self.subTest(left=left, right=right):
                self.assertFalse(can_join_ready_speech(left, right))


class ReadySpeechStreamingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        key = patch.dict(os.environ, {'SPEECH_CONTINUITY_TEST_KEY': 'offline-fixture'})
        key.start()
        self.addCleanup(key.stop)
        network = patch('brain.persona.httpx.AsyncClient', side_effect=AssertionError('no network'))
        network.start()
        self.addCleanup(network.stop)
        self.persona = Persona(
            PersonaConfig(), LLMConfig(api_key_env='SPEECH_CONTINUITY_TEST_KEY', first_clause_chars=0),
            Memory(str(root / 'm.jsonl'), str(root / 'v.json')))
        self.closed = False

    def stream(self, *deltas):
        async def fake_stream(*args, **kwargs):
            try:
                for delta in deltas:
                    yield delta
            finally:
                self.closed = True
        self.persona._stream_llm = fake_stream

    async def collect(self, request='聊聊这首曲子', source='voice'):
        return [part async for part in self.persona.stream_reply(Event(source, 'chat', request))]

    async def test_ready_ack_and_body_are_one_request_without_rewriting_words(self):
        original = '对。我更喜欢后半段。'
        self.stream('[平静]' + original)
        parts = await self.collect()
        self.assertEqual([(p.index, p.text, p.emotion, p.actions) for p in parts],
                         [(0, original, '平静', [])])
        self.assertEqual(self.persona.last_text, original)
        self.assertTrue(self.closed)

    async def test_sentence_budget_ignores_group_size_and_does_not_parse_excess_metadata(self):
        self.stream('[平静]前半段很轻。后半段更快。[惊讶][动作:瞳孔地震]预算外内容。')
        with patch.object(self.persona, '_parse_response', wraps=self.persona._parse_response) as parser:
            parts = await self.collect()
        self.assertEqual([p.text for p in parts], ['前半段很轻。后半段更快。'])
        self.assertEqual(parser.call_count, 2)
        self.assertEqual(self.persona.last_emotion, '平静')
        self.assertEqual(self.persona.last_actions, [])

    async def test_groups_at_most_two_chunks_and_keeps_transport_indexes_contiguous(self):
        self.stream('[平静]第一处很轻。第二处更快。第三处放慢。第四处停下。第五处不说。')
        parts = await self.collect('详细聊聊这首曲子')
        self.assertEqual([(p.index, p.text) for p in parts],
                         [(0, '第一处很轻。第二处更快。'), (1, '第三处放慢。第四处停下。')])

    async def test_single_sentence_mode_does_not_expand_due_to_grouping(self):
        self.stream('[平静]对。我更喜欢后半段。')
        parts = await self.collect('一句话说说这首曲子')
        self.assertEqual([p.text for p in parts], ['对。'])

    async def test_explicit_emotion_or_action_start_is_not_pulled_forward(self):
        self.stream('[平静]前半段很轻。[开心]后半段很有趣。'
                    '[开心][动作:认可]这一处接得好。[开心][动作:认可]结尾也不错。')
        parts = await self.collect('详细聊聊这首曲子')
        self.assertEqual([p.text for p in parts],
                         ['前半段很轻。', '后半段很有趣。', '这一处接得好。', '结尾也不错。'])
        self.assertEqual([p.emotion for p in parts], ['平静', '开心', '开心', '开心'])
        self.assertEqual([p.actions for p in parts], [[], [], ['认可'], ['认可']])

    async def test_initial_action_does_not_spread_to_following_statement(self):
        self.stream('[平静][动作:认可]对。我更喜欢后半段。')
        parts = await self.collect()
        self.assertEqual([p.text for p in parts], ['对。', '我更喜欢后半段。'])
        self.assertEqual([p.actions for p in parts], [['认可'], []])

    async def test_closing_after_a_boundary_does_not_apply_unheard_later_metadata(self):
        self.stream('[平静]前半段很轻。[惊讶][动作:瞳孔地震]后半段突然响起来。')
        iterator = self.persona.stream_reply(Event('voice', 'chat', '聊聊这首曲子'))
        with patch.object(self.persona, '_parse_response', wraps=self.persona._parse_response) as parser:
            self.assertEqual((await anext(iterator)).text, '前半段很轻。')
            self.assertEqual(parser.call_count, 1)
            await iterator.aclose()
        self.assertEqual(self.persona.behavior.inspect(None).mood, '平静')
        self.assertTrue(self.closed)

    async def test_first_ready_sentence_does_not_request_or_wait_for_the_next_delta(self):
        advanced = asyncio.Event()
        release = asyncio.Event()
        async def fake_stream(*args, **kwargs):
            try:
                yield '[平静]对。'
                advanced.set()
                await release.wait()
                yield '我更喜欢后半段。'
            finally:
                self.closed = True
        self.persona._stream_llm = fake_stream
        iterator = self.persona.stream_reply(Event('voice', 'chat', '聊聊这首曲子'))
        first = await asyncio.wait_for(anext(iterator), .3)
        self.assertEqual(first.text, '对。')
        self.assertFalse(advanced.is_set())
        release.set()
        rest = [part async for part in iterator]
        self.assertEqual([part.text for part in rest], ['我更喜欢后半段。'])
        self.assertTrue(self.closed)

    async def test_early_comma_latency_and_extra_transport_budget_are_preserved(self):
        self.persona.llm.first_clause_chars = 8
        original = '这一段我更喜欢后半部分，前面的铺垫也很好。结尾留了一点余味。'
        self.stream('[平静]' + original)
        parts = await self.collect()
        self.assertEqual(parts[0].text, '这一段我更喜欢后半部分，')
        self.assertEqual(''.join(p.text for p in parts), original)
        self.assertEqual([p.index for p in parts], list(range(len(parts))))

    async def test_final_incomplete_tail_is_flushed_without_reaching_back_to_old_audio(self):
        self.stream('[平静]前半段很轻。后半段更快。', '最后慢慢停下来')
        parts = await self.collect('详细聊聊这首曲子')
        self.assertEqual([p.text for p in parts], ['前半段很轻。后半段更快。', '最后慢慢停下来'])

    async def test_bad_later_sentence_preserves_valid_partial_reply(self):
        self.stream('[平静]前半段很轻。后半段更快。禁止播出。')
        with patch('brain.persona.safety.is_blocked', side_effect=lambda text: text == '禁止播出。'):
            parts = await self.collect('详细聊聊这首曲子')
        self.assertEqual([p.text for p in parts], ['前半段很轻。后半段更快。'])
        self.assertEqual(self.persona.last_mode, 'partial')
        self.assertEqual(self.persona.last_text, parts[0].text)
        self.assertTrue(self.closed)

    async def test_failure_before_any_valid_sentence_still_uses_fallback(self):
        self.stream('[平静]禁止播出。')
        with patch('brain.persona.safety.is_blocked', side_effect=lambda text: text == '禁止播出。'):
            parts = await self.collect()
        self.assertEqual(self.persona.last_mode, 'fallback')
        self.assertEqual(len(parts), 1)
        self.assertNotIn('禁止播出', parts[0].text)

    async def test_cancel_during_later_generation_never_flushes_partial_tail(self):
        waiting = asyncio.Event()
        async def fake_stream(*args, **kwargs):
            try:
                yield '[平静]前半段很轻。后半段更快。'
                yield '还没有说完的'
                waiting.set()
                await asyncio.Event().wait()
            finally:
                self.closed = True
        self.persona._stream_llm = fake_stream
        iterator = self.persona.stream_reply(Event('voice', 'chat', '详细聊聊这首曲子'))
        self.assertEqual((await anext(iterator)).text, '前半段很轻。后半段更快。')
        pending = asyncio.create_task(anext(iterator))
        await asyncio.wait_for(waiting.wait(), .3)
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertEqual(self.persona.last_mode, 'cancelled')
        self.assertTrue(self.closed)


if __name__ == '__main__':
    unittest.main()
