"""Input extraction is shared, but delivered reply commitments are per segment."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from brain.autobiographical_memory import AutobiographicalMemory, MemoryCandidate
from brain.config import LLMConfig, SelectiveMemoryConfig
from brain.events import Event


class MemoryExtractionCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 1800000000.
        cfg = SelectiveMemoryConfig(state_path=str(Path(self.temp.name) / 'memory.json'),
            core_memories_path=str(Path(self.temp.name) / 'none.yaml'), llm_extraction=True)
        self.memory = AutobiographicalMemory(cfg, LLMConfig(api_key_env='NO_CACHE_KEY'),
                                             '测试角色', clock=lambda: self.now)
        self.memory.llm = SimpleNamespace(api_key='offline-test')
        self.event = Event('voice', 'chat', '这是我第一次参加漫展', user='你', ts=self.now)
        self.candidate = MemoryCandidate('viewer_fact', '观众你', '自述', self.event.text,
                                         self.event.text, .8, .8)

    async def asyncTearDown(self):
        await self.memory.close()
        self.temp.cleanup()

    async def test_sequential_segments_extract_once_but_keep_both_commitments(self):
        with patch.object(self.memory, '_extract_with_llm', return_value=[self.candidate]) as extractor:
            await self.memory.consolidate(self.event, '下次我会和你聊绘画。', '平静')
            await self.memory.consolidate(self.event, '之后我会接着给你讲故事。', '开心')
            self.assertEqual(extractor.await_count, 1)
        records = self.memory.state['memories']
        self.assertEqual(len([m for m in records if m['kind'] == 'commitment']), 2)
        fact = next(m for m in records if m['kind'] == 'viewer_fact')
        self.assertEqual(fact['reinforcement_count'], 0)
        self.assertEqual(len(fact['timeline_event_ids']), 1)

    async def test_concurrent_callers_share_work_and_one_cancellation_is_independent(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def extract(*args):
            started.set()
            await release.wait()
            return [self.candidate]
        with patch.object(self.memory, '_extract_with_llm', side_effect=extract) as extractor:
            a = asyncio.create_task(self.memory.consolidate(self.event, '', '平静'))
            await started.wait()
            b = asyncio.create_task(self.memory.consolidate(self.event, '下次我会讲故事。', '开心'))
            await asyncio.sleep(0)
            a.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await a
            release.set()
            self.assertEqual(len(await b), 2)
            self.assertEqual(extractor.await_count, 1)
        self.assertFalse(self.memory._input_jobs)

    async def test_all_cancelled_extraction_is_cancelled_and_can_retry(self):
        started, cancelled = asyncio.Event(), asyncio.Event()
        async def extract(*args):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        with patch.object(self.memory, '_extract_with_llm', side_effect=extract):
            task = asyncio.create_task(self.memory.consolidate(self.event, '', '平静'))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await asyncio.wait_for(cancelled.wait(), 1)
        self.assertFalse(self.memory._input_cache)
        with patch.object(self.memory, '_extract_with_llm', return_value=[self.candidate]) as extractor:
            await self.memory.consolidate(self.event, '', '平静')
            self.assertEqual(extractor.await_count, 1)

    async def test_http_failure_fallback_does_not_poison_cache(self):
        async def failed(*args):
            self.memory.state['stats']['llm_errors'] += 1
            return []
        with patch.object(self.memory, '_extract_with_llm', side_effect=failed):
            await self.memory.consolidate(self.event, '', '平静')
        self.assertFalse(self.memory._input_cache)
        with patch.object(self.memory, '_extract_with_llm', return_value=[self.candidate]) as extractor:
            await self.memory.consolidate(self.event, '', '平静')
            self.assertEqual(extractor.await_count, 1)

    async def test_unexpected_error_cleans_inflight_and_can_retry(self):
        with patch.object(self.memory, '_extract_with_llm', side_effect=RuntimeError('offline error')):
            with self.assertRaises(RuntimeError):
                await self.memory.consolidate(self.event, '', '平静')
        self.assertFalse(self.memory._input_jobs)
        with patch.object(self.memory, '_extract_with_llm', return_value=[self.candidate]):
            self.assertTrue(await self.memory.consolidate(self.event, '', '平静'))

    async def test_cached_candidates_cannot_restore_forgotten_memory(self):
        with patch.object(self.memory, '_extract_with_llm', return_value=[self.candidate]) as extractor:
            record = (await self.memory.consolidate(self.event, '', '平静'))[0]
            self.now += 1
            await self.memory.forget(record['id'])
            self.assertEqual(await self.memory.consolidate(self.event, '', '开心'), [])
            self.assertEqual(extractor.await_count, 1)

    async def test_uncertain_or_negative_reply_is_not_a_pending_commitment(self):
        for reply in ['下次我可能会讲故事。', '之后我不会再讲故事。', '如果可以，下次我会讲故事吗？']:
            self.assertIsNone(self.memory._commitment_candidate(self.event, reply))


if __name__ == '__main__':
    unittest.main()
