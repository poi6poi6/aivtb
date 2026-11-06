"""The Gemini persona override must preserve context and auxiliary LLM routing.

All transports are fake; these tests neither load .env nor contact a provider.
"""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from brain.config import LLMConfig, PersonaConfig, PersonaLLMConfig, load_settings
from brain.events import Event
from brain.memory import Memory
from brain.persona import Persona


class _TrackedStream:
    def __init__(self, parts=(), error=None):
        self.parts = iter(parts)
        self.error = error
        self.reads = 0
        self.aclose = AsyncMock()

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.error is not None:
            raise self.error
        try:
            part = next(self.parts)
        except StopIteration:
            raise StopAsyncIteration
        self.reads += 1
        return part


class GeminiPersonaBackendTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'TEST_GEMINI_AUXILIARY_KEY': ''})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.cfg = PersonaConfig(name='测试主人格', background='只属于角色的世界观',
                                 rules=['不要编造已经执行的操作'])
        self.llm = LLMConfig(api_key_env='TEST_GEMINI_AUXILIARY_KEY',
                             model='deepseek-flash', thinking=False,
                             first_clause_chars=0)
        self.memory = Memory(str(self.root / 'memory.jsonl'), str(self.root / 'viewers.json'))

    def persona(self, override=None):
        result = Persona(self.cfg, self.llm, self.memory, persona_llm=override)
        self.addAsyncCleanup(result.close)
        return result

    def gemini_persona(self, stream):
        backend = Mock(available=True, stream=Mock(return_value=stream), close=AsyncMock())
        override = PersonaLLMConfig(backend='gemini_api', api_key_env='TEST_GEMINI_KEY',
                                    model='gemini-4-argon', effort='low',
                                    max_tokens=4096, timeout_sec=23)
        with patch('brain.gemini_chat.GeminiChat', return_value=backend) as factory:
            persona = self.persona(override)
        factory.assert_called_once_with(api_key_env='TEST_GEMINI_KEY', model='gemini-4-argon',
                                        effort='low', max_tokens=4096, timeout_sec=23)
        persona._http_client = Mock(side_effect=AssertionError('Gemini must not call DeepSeek'))
        return persona, backend

    def assert_context(self, backend, event, extra=''):
        backend.stream.assert_called_once()
        system, user = backend.stream.call_args.args
        for value in (self.cfg.name, self.cfg.background, self.cfg.rules[0], '[情绪]'):
            self.assertIn(value, system)
        self.assertIn(event.text, user)
        self.assertIn(event.user, user)
        if extra:
            self.assertIn(extra, user)

    def test_config_keeps_legacy_inherit_and_separate_auxiliary_llm(self):
        path = self.root / 'settings.yaml'
        base = ('llm:\n  model: deepseek-flash\n  thinking: false\n'
                '  api_key_env: TEST_GEMINI_AUXILIARY_KEY\n  max_tokens: 320\n')
        path.write_text(base, encoding='utf-8')
        legacy = load_settings(path)
        self.assertEqual(legacy.persona_llm.backend, 'inherit')
        self.assertEqual(legacy.persona_llm.api_key_env, 'GEMINI_API_KEY')
        self.assertEqual(legacy.persona_llm.max_tokens, 4096)
        path.write_text(base + 'persona_llm:\n  backend: gemini_api\n'
                        '  api_key_env: TEST_GEMINI_KEY\n  model: gemini-4-argon\n'
                        '  effort: low\n  max_tokens: 8192\n  timeout_sec: 23\n', encoding='utf-8')
        loaded = load_settings(path)
        self.assertEqual(loaded.llm, legacy.llm)
        self.assertEqual(loaded.persona_llm.backend, 'gemini_api')
        self.assertEqual(loaded.persona_llm.api_key_env, 'TEST_GEMINI_KEY')
        self.assertEqual(loaded.persona_llm.model, 'gemini-4-argon')
        self.assertEqual(loaded.persona_llm.effort, 'low')
        self.assertEqual(loaded.persona_llm.max_tokens, 8192)
        self.assertEqual(loaded.persona_llm.timeout_sec, 23)

    async def test_default_persona_does_not_construct_gemini(self):
        with patch('brain.gemini_chat.GeminiChat') as factory:
            persona = self.persona()
        factory.assert_not_called()
        self.assertIsNone(persona._chat_backend)
        self.assertEqual(persona.model_name, 'deepseek-flash')
        self.assertFalse(persona.model_configured)
        with patch.dict(os.environ, {'TEST_GEMINI_AUXILIARY_KEY': 'fixture-key'}):
            self.assertTrue(persona.model_configured)

    async def test_api_reply_needs_no_deepseek_key_and_preserves_persona(self):
        stream = _TrackedStream(['[平静]', '听到了。'])
        persona, backend = self.gemini_persona(stream)
        event = Event('danmaku', 'chat', '你好', user='小林')
        self.assertIsNone(self.llm.api_key)
        self.assertEqual(await persona.reply(event), ('平静', '听到了。'))
        self.assertEqual(persona.last_mode, 'llm')
        self.assert_context(backend, event)
        stream.aclose.assert_awaited_once()
        persona._http_client.assert_not_called()
        self.assertEqual(persona.model_name, 'gemini-4-argon')
        self.assertTrue(persona.model_configured)
        backend.available = False
        self.assertFalse(persona.model_configured)
        self.assertIs(persona.llm, self.llm)
        self.assertEqual(self.llm.model, 'deepseek-flash')
        self.assertEqual(self.llm.max_tokens, 320)
        await persona.close()
        backend.close.assert_awaited_once()

    async def test_api_stream_preserves_extra_context_and_closes_at_sentence_limit(self):
        stream = _TrackedStream(['[开心]', '[动作:挥手]', '你好呀。', '这句不应该再读取。'])
        persona, backend = self.gemini_persona(stream)
        event = Event('danmaku', 'chat', '你好', user='小林')
        segments = [part async for part in persona.stream_reply(event, extra_context='刚结束绘画环节')]
        self.assertEqual([part.text for part in segments], ['你好呀。'])
        self.assertEqual(segments[0].actions, ['挥手'])
        self.assertEqual(persona.last_text, '你好呀。')
        self.assertEqual(persona.last_mode, 'llm')
        self.assert_context(backend, event, '刚结束绘画环节')
        self.assertEqual(stream.reads, 3)
        stream.aclose.assert_awaited_once()
        persona._http_client.assert_not_called()

    async def test_consumer_closing_reply_also_closes_api_stream(self):
        stream = _TrackedStream(['[平静]听到了。', '这句不应该再读取。'])
        persona, _ = self.gemini_persona(stream)
        output = persona.stream_reply(Event('danmaku', 'chat', '你好', user='小林'))
        first = await output.__anext__()
        self.assertEqual(first.text, '听到了。')
        await output.aclose()
        self.assertEqual(stream.reads, 1)
        stream.aclose.assert_awaited_once()
        persona._http_client.assert_not_called()

    async def test_api_error_never_calls_deepseek_even_with_an_auxiliary_key(self):
        os.environ['TEST_GEMINI_AUXILIARY_KEY'] = 'fixture-key'
        for streaming in (False, True):
            with self.subTest(streaming=streaming):
                stream = _TrackedStream(error=RuntimeError('Gemini API 暂不可用'))
                persona, backend = self.gemini_persona(stream)
                event = Event('danmaku', 'chat', '你好', user='小林')
                if streaming:
                    output = [part async for part in persona.stream_reply(event)]
                    self.assertTrue(output[0].text)
                else:
                    _, text = await persona.reply(event)
                    self.assertTrue(text)
                self.assertEqual(persona.last_mode, 'fallback')
                self.assertIn('Gemini API 暂不可用', persona.last_error)
                backend.stream.assert_called_once()
                stream.aclose.assert_awaited_once()
                persona._http_client.assert_not_called()


if __name__ == '__main__':
    unittest.main()
