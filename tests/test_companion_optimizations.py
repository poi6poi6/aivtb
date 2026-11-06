import asyncio
import math
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from brain.autobiographical_memory import AutobiographicalMemory
from brain.config import LLMConfig, PersonaConfig, SelectiveMemoryConfig
from brain.events import Event
from brain.memory import Memory
from brain.metrics import RuntimeMetrics
from brain.persona import Persona
from brain.search import WebSearch, is_search_question
from brain.speech_text import SentenceChunker


class SearchRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_questions_do_not_wait_for_web(self):
        messages = ('能陪我聊一会儿吗？', '你记得我喜欢喝什么吗？', '我今天很难过，怎么办？',
                    '你愿意听我说说吗？', '你准备好了吗？', '我刚才告诉过你什么？')
        for message in messages:
            with self.subTest(message=message):
                self.assertFalse(is_search_question(message))
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'OPT_TEST_KEY':'test'}):
            search = WebSearch()
            search.search = AsyncMock(side_effect=AssertionError('private chat used web'))
            persona = Persona(PersonaConfig(), LLMConfig(api_key_env='OPT_TEST_KEY'),
                              Memory(str(Path(folder)/'log'), str(Path(folder)/'viewers')), web_search=search)
            async def stream(*args):
                self.assertIsNone(args[2])
                yield '[平静]我在听。'
            persona._stream_llm = stream
            response = [s.text async for s in persona.stream_reply(Event('voice','chat',messages[0], user='你'))]
            self.assertEqual(response, ['我在听。'])
            search.search.assert_not_awaited()

    async def test_explicit_lookup_and_factual_queries_remain_available(self):
        for message in ('查一下陪伴聊天的研究', '最新天气预报', '请问量子计算是什么',
                        '核实一下这条新闻', '怎么安装这个软件', '这个事实是真的吗？'):
            with self.subTest(message=message):
                self.assertTrue(is_search_question(message))

    async def test_deadline_covers_slow_response_and_does_not_cache_timeout(self):
        cancelled = asyncio.Event()
        async def slow(request):
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.set()
        search = WebSearch(timeout_sec=.02, transport=httpx.MockTransport(slow))
        with self.assertRaises(asyncio.TimeoutError):
            await search.search('实时问题')
        self.assertTrue(cancelled.is_set())
        self.assertEqual(search._cache, {})


class FirstClauseTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_clause_starts_before_rest_and_keeps_full_reply(self):
        first = '这件事可以先从你最在意的那一部分慢慢说起，不需要一下子想好。'
        second = '我会认真听完。'
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'OPT_TEST_KEY':'test'}):
            persona = Persona(PersonaConfig(), LLMConfig(api_key_env='OPT_TEST_KEY', first_clause_chars=18),
                              Memory(str(Path(folder)/'log'), str(Path(folder)/'viewers')))
            rest_allowed = asyncio.Event()
            async def stream(*args):
                yield '[平静][动作:挥手]'+first.split('，')[0]+'，'
                await rest_allowed.wait()
                yield first.split('，')[1]+second
            persona._stream_llm = stream
            iterator = persona.stream_reply(Event('voice','chat','陪我聊聊',user='你'))
            opening = await asyncio.wait_for(anext(iterator), .3)
            self.assertTrue(opening.text.endswith('，'))
            self.assertEqual(opening.actions, ['挥手'])
            rest_allowed.set()
            remaining = [s async for s in iterator]
            self.assertEqual(opening.text+''.join(s.text for s in remaining), first+second)
            self.assertNotIn('动作', persona.last_text)

    def test_metadata_commas_and_opt_out_do_not_split_first_clause(self):
        chunker = SentenceChunker(first_clause_chars=12)
        self.assertEqual(chunker.feed('[开心][动作:挥手,脸红]短句，'), [])
        self.assertFalse(chunker.first_clause_split)
        normal = SentenceChunker(max_chars=96, first_clause_chars=0)
        self.assertEqual(normal.feed('较长的一段话在这里仍然等待完整句子，'), [])


class SemanticMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 1800000000.
        self.cfg = SelectiveMemoryConfig(state_path=str(Path(self.temp.name)/'memory.json'), llm_extraction=False)
        self.memory = AutobiographicalMemory(self.cfg, LLMConfig(api_key_env='UNUSED_OPT_KEY'), '助手', clock=lambda:self.now)

    async def asyncTearDown(self):
        await self.memory.close()
        self.temp.cleanup()

    async def test_paraphrase_recalls_self_report_and_does_not_invent_a_preference(self):
        entries = await self.memory.consolidate(Event('voice','chat','我喝咖啡容易睡不着',user='你'), '', '平静')
        self.assertEqual(len(entries), 1)
        context = await self.memory.context_for(Event('voice','chat','晚上喝点什么好？',user='你'))
        self.assertIn('喝咖啡容易睡不着', context)
        self.assertNotIn('不喜欢咖啡', context)
        self.assertEqual(await self.memory.context_for(Event('voice','chat','晚上喝点什么好？',user='别人')), '')
        await self.memory.forget(entries[0]['id'])
        self.assertEqual(await self.memory.context_for(Event('voice','chat','睡前有什么饮品？',user='你')), '')

    async def test_same_name_accounts_remain_separate_after_edit_and_reload(self):
        a = Event('danmaku','chat','我喜欢拿铁', user='小明', viewer_key='u1', ts=self.now)
        b = Event('danmaku','chat','我喜欢绿茶', user='小明', viewer_key='u2', ts=self.now)
        saved_a = (await self.memory.consolidate(a, '', '平静'))[0]
        await self.memory.consolidate(b, '', '平静')
        self.assertTrue(await self.memory.edit_memory(saved_a['id'], '观众小明不喜欢咖啡'))
        await self.memory.close()
        self.memory = AutobiographicalMemory(self.cfg, LLMConfig(), '助手', clock=lambda:self.now)
        for key, expected, absent in (('u1','不喜欢咖啡','绿茶'),('u2','绿茶','不喜欢咖啡')):
            context = await self.memory.context_for(Event('danmaku','chat','我喜欢什么饮料？',user='小明',viewer_key=key))
            self.assertIn(expected, context)
            self.assertNotIn(absent, context)

    async def test_expired_state_and_questions_are_not_new_facts(self):
        await self.memory.consolidate(Event('voice','chat','我今天喝咖啡容易睡不着',user='你',ts=self.now), '', '平静')
        self.now += 86401
        self.assertEqual(await self.memory.context_for(Event('voice','chat','睡前喝点什么？',user='你')), '')
        self.assertEqual(await self.memory.consolidate(Event('voice','chat','我喝咖啡会不会容易睡不着？',user='你'), '', '平静'), [])


class MeasurementTests(unittest.TestCase):
    def test_invalid_measurement_does_not_pollute_export(self):
        metrics = RuntimeMetrics()
        metrics.record_stage('search', math.nan)
        metrics.record_stage('search', 1.25)
        self.assertEqual(metrics.summary()['latency']['search'], {'latest':1.25,'p50':1.25,'p95':1.25,'samples':1})
