"""Direct Persona callers cannot invent media receipts; no real model or memory."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from brain.config import PersonaConfig
from brain.events import Event
from brain.persona import Persona


class PersonaMediaGuardTests(unittest.IsolatedAsyncioTestCase):
    def persona(self):
        persona = Persona(PersonaConfig(), SimpleNamespace(api_key='fixture-only', first_clause_chars=0), None)
        persona._research_for = AsyncMock(return_value=[])
        persona._call_llm = AsyncMock(return_value='[平静]测试模型的普通聊天回复。')
        persona.fixture_stream_calls = []

        async def fixture_stream(*args):
            persona.fixture_stream_calls.append(args)
            yield '[平静]测试模型的普通聊天回复。'

        persona._stream_llm = fixture_stream
        return persona

    async def test_correct_music_commands_from_question_cards_never_reach_model(self):
        for text in ('点歌 海色', 'B站点歌 海色', '你好，点歌 海色', '下一首', '停止播放'):
            persona = self.persona()
            event = Event('danmaku', 'chat', text, forced=True)
            event.host_question_id = 'fixture-question'
            event.host_question_attempt = 1
            _, reply = await persona.reply(event)
            self.assertIn('还没有执行', reply, text)
            segments = [part async for part in persona.stream_reply(event)]
            self.assertIn('还没有执行', ''.join(part.text for part in segments), text)
            self.assertEqual(persona.last_mode, 'local_fact')
            persona._research_for.assert_not_awaited()
            persona._call_llm.assert_not_awaited()
            self.assertFalse(persona.fixture_stream_calls)

    async def test_unimplemented_platform_still_has_accurate_capability_response(self):
        persona = self.persona()
        event = Event('companion', 'chat', '播放QQ音乐海色')
        _, reply = await persona.reply(event)
        self.assertIn('暂时没有接入', reply)
        segments = [part async for part in persona.stream_reply(event)]
        self.assertIn('暂时没有接入', ''.join(part.text for part in segments))
        persona._call_llm.assert_not_awaited()
        self.assertFalse(persona.fixture_stream_calls)

    async def test_normal_music_discussion_keeps_normal_reply_path(self):
        persona = self.persona()
        event = Event('companion', 'chat', '你喜欢什么音乐')
        await persona.reply(event)
        self.assertEqual(persona._call_llm.await_count, 1)
        _ = [part async for part in persona.stream_reply(event)]
        self.assertEqual(len(persona.fixture_stream_calls), 1)

    async def test_existing_trusted_task_receipt_keeps_priority_over_unexecuted_guard(self):
        persona = self.persona()
        event = Event('system', 'chat', '点歌 海色', agent_reply='任务状态已更新。')
        self.assertEqual((await persona.reply(event))[1], '任务状态已更新。')
        segments = [part async for part in persona.stream_reply(event)]
        self.assertEqual(''.join(part.text for part in segments), '任务状态已更新。')
        self.assertEqual(persona.last_mode, 'task_receipt')
        persona._call_llm.assert_not_awaited()
        self.assertFalse(persona.fixture_stream_calls)
