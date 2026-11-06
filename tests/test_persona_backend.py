"""Persona transport selection must not change auxiliary LLM calls or lose context."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx

from brain.config import LLMConfig, PersonaConfig, PersonaLLMConfig, ROOT, Settings, load_settings
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


class PersonaBackendTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'TEST_PERSONA_BACKEND_KEY': ''})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.cfg = PersonaConfig(name='测试主人格', background='只属于角色的世界观',
                                 rules=['不要编造已经执行的操作'])
        self.llm = LLMConfig(base_url='https://api.deepseek.com',
                             api_key_env='TEST_PERSONA_BACKEND_KEY',
                             model='deepseek-flash', thinking=False,
                             first_clause_chars=0)
        self.memory = Memory(str(self.root / 'memory.jsonl'), str(self.root / 'viewers.json'))

    def persona(self, override=None):
        result = Persona(self.cfg, self.llm, self.memory, persona_llm=override)
        self.addAsyncCleanup(result.close)
        return result

    def cli_persona(self, stream):
        backend = Mock(available=True, stream=Mock(return_value=stream), close=AsyncMock())
        override = PersonaLLMConfig(backend='antigravity_cli', command='test-agy.exe',
                                    model='gemini-4-argon-medium', effort='low',
                                    timeout_sec=12, runtime_dir=str(self.root / 'turns'))
        with patch('brain.antigravity_chat.AntigravityChat', return_value=backend) as factory:
            persona = self.persona(override)
        factory.assert_called_once_with(command='test-agy.exe', model=override.model,
                                        effort='low', timeout_sec=12,
                                        runtime_dir=self.root / 'turns')
        persona._http_client = Mock(side_effect=AssertionError('CLI must not call DeepSeek'))
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

    def test_legacy_config_inherits_and_override_leaves_auxiliary_llm_intact(self):
        path = self.root / 'settings.yaml'
        base = 'llm:\n  model: deepseek-flash\n  thinking: false\n  api_key_env: TEST_PERSONA_BACKEND_KEY\n'
        path.write_text(base, encoding='utf-8')
        legacy = load_settings(path)
        self.assertEqual(legacy.persona_llm.backend, 'inherit')
        self.assertEqual(legacy.llm.model, 'deepseek-flash')
        for command, expected in (('agy', 'agy'),
                                  ('data/argon-runtime/bin/agy.exe',
                                   str(ROOT / 'data/argon-runtime/bin/agy.exe')),
                                  (r'data\argon-runtime\bin\agy.exe',
                                   str(ROOT / r'data\argon-runtime\bin\agy.exe'))):
            with self.subTest(command=command):
                path.write_text(base + 'persona_llm:\n  backend: antigravity_cli\n'
                                f'  command: {command}\n  model: gemini-4-argon-medium\n'
                                '  runtime_dir: data/argon-runtime/turns\n', encoding='utf-8')
                settings = load_settings(path)
                self.assertEqual(settings.llm, legacy.llm)
                self.assertEqual(settings.persona_llm.backend, 'antigravity_cli')
                self.assertEqual(settings.persona_llm.model, 'gemini-4-argon-medium')
                self.assertEqual(settings.persona_llm.command, expected)
                self.assertEqual(settings.persona_llm.runtime_dir,
                                 str(ROOT / 'data/argon-runtime/turns'))

    async def test_inherited_nonstream_and_stream_keep_deepseek_wire_contract(self):
        os.environ['TEST_PERSONA_BACKEND_KEY'] = 'fixture-key'
        requests = []

        def respond(request):
            requests.append(request)
            payload = json.loads(request.content)
            if payload.get('stream'):
                data = {'choices': [{'delta': {'content': '[平静]听到了。'}}]}
                return httpx.Response(200, text='data: ' + json.dumps(data) + '\n\ndata: [DONE]\n\n',
                                      headers={'content-type': 'text/event-stream'})
            return httpx.Response(200, json={'choices': [{'message': {'content': '[平静]听到了。'}}]})

        with patch('brain.antigravity_chat.AntigravityChat') as factory:
            persona = self.persona()
        factory.assert_not_called()
        persona._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        event = Event('danmaku', 'chat', '你好', user='小林')
        self.assertEqual(await persona.reply(event), ('平静', '听到了。'))
        segments = [part async for part in persona.stream_reply(event, extra_context='刚刚结束画画')]
        self.assertEqual([part.text for part in segments], ['听到了。'])
        self.assertEqual(persona.model_name, 'deepseek-flash')
        self.assertTrue(persona.model_configured)
        self.assertEqual(len(requests), 2)
        for index, request in enumerate(requests):
            payload = json.loads(request.content)
            self.assertEqual(str(request.url), 'https://api.deepseek.com/chat/completions')
            self.assertEqual(request.method, 'POST')
            self.assertEqual(request.headers['Authorization'], 'Bearer fixture-key')
            self.assertEqual(payload['model'], 'deepseek-flash')
            self.assertEqual(payload['thinking'], {'type': 'disabled'})
            self.assertEqual(payload['max_tokens'], 80)
            self.assertEqual([m['role'] for m in payload['messages']], ['system', 'user'])
            self.assertIn(self.cfg.name, payload['messages'][0]['content'])
            self.assertIn('你好', payload['messages'][1]['content'])
            if index:
                self.assertTrue(payload['stream'])
                self.assertIn('刚刚结束画画', payload['messages'][1]['content'])
            else:
                self.assertNotIn('stream', payload)

    async def test_cli_reply_works_without_api_key_and_preserves_persona(self):
        stream = _TrackedStream(['[平静]', '听到了。'])
        persona, backend = self.cli_persona(stream)
        event = Event('danmaku', 'chat', '你好', user='小林')
        self.assertIsNone(self.llm.api_key)
        self.assertEqual(await persona.reply(event), ('平静', '听到了。'))
        self.assertEqual(persona.last_mode, 'llm')
        self.assert_context(backend, event)
        stream.aclose.assert_awaited_once()
        persona._http_client.assert_not_called()
        self.assertEqual(persona.model_name, 'gemini-4-argon-medium')
        self.assertTrue(persona.model_configured)
        backend.available = False
        self.assertFalse(persona.model_configured)
        self.assertEqual(self.llm.model, 'deepseek-flash')
        await persona.close()
        backend.close.assert_awaited_once()

    async def test_cli_stream_preserves_extra_context_and_closes_at_sentence_limit(self):
        stream = _TrackedStream(['[开心]', '[动作:挥手]', '你好呀。', '这句不应该再读取。'])
        persona, backend = self.cli_persona(stream)
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

    async def test_consumer_closing_reply_also_closes_cli_stream(self):
        stream = _TrackedStream(['[平静]听到了。', '这句不应该再读取。'])
        persona, _ = self.cli_persona(stream)
        output = persona.stream_reply(Event('danmaku', 'chat', '你好', user='小林'))
        first = await output.__anext__()
        self.assertEqual(first.text, '听到了。')
        await output.aclose()
        self.assertEqual(stream.reads, 1)
        stream.aclose.assert_awaited_once()
        persona._http_client.assert_not_called()

    async def test_cli_failure_never_falls_back_to_paid_deepseek_even_with_key(self):
        os.environ['TEST_PERSONA_BACKEND_KEY'] = 'fixture-key'
        for streaming in (False, True):
            with self.subTest(streaming=streaming):
                stream = _TrackedStream(error=RuntimeError('套餐暂不可用'))
                persona, backend = self.cli_persona(stream)
                event = Event('danmaku', 'chat', '你好', user='小林')
                if streaming:
                    output = [part async for part in persona.stream_reply(event)]
                    self.assertTrue(output[0].text)
                else:
                    _, text = await persona.reply(event)
                    self.assertTrue(text)
                self.assertEqual(persona.last_mode, 'fallback')
                self.assertIn('套餐暂不可用', persona.last_error)
                backend.stream.assert_called_once()
                stream.aclose.assert_awaited_once()
                persona._http_client.assert_not_called()

    async def test_real_probe_uses_isolated_memory_and_requires_complete_success(self):
        from tools import probe_argon

        settings = Settings(llm=self.llm)
        for fail_after_text in (False, True):
            with self.subTest(fail_after_text=fail_after_text):
                state = {'terminal_reached': False, 'closed': False}
                memories = []
                paths = []

                async def stream(system, user):
                    try:
                        yield '[平静]你想安静待着，我陪你。'
                        yield '不用急着找话题。'
                        yield '窗外的树影也挺好看。'
                        state['terminal_reached'] = True
                        if fail_after_text:
                            raise RuntimeError('测试终态失败')
                    finally:
                        state['closed'] = True

                def make_memory(events_path, viewers_path):
                    paths.extend([Path(events_path), Path(viewers_path)])
                    memory = Memory(events_path, viewers_path)
                    memory.close_persistence = AsyncMock(wraps=memory.close_persistence)
                    memories.append(memory)
                    return memory

                backend = Mock(available=True, stream=Mock(side_effect=stream), close=AsyncMock())
                with (patch.object(probe_argon, 'load_settings', return_value=settings),
                      patch.object(probe_argon, 'load_persona', return_value=self.cfg),
                      patch.object(probe_argon, 'Memory', side_effect=make_memory),
                      patch.object(probe_argon, 'print') as printed,
                      patch('brain.antigravity_chat.AntigravityChat', return_value=backend),
                      patch.object(Persona, '_http_client', side_effect=AssertionError('No HTTP')) as http):
                    result = await probe_argon.main()

                report = json.loads(printed.call_args.args[0])
                self.assertEqual(result, 1 if fail_after_text else 0)
                self.assertEqual(report['ok'], not fail_after_text)
                self.assertTrue(report['scripted_input'])
                self.assertFalse(report['production_history_used'])
                self.assertEqual(report['requested_model'], 'gemini-4-argon-medium')
                if fail_after_text:
                    self.assertEqual(report['reply'], '')
                    self.assertIn('测试终态失败', report['error'])
                else:
                    self.assertIn('你想安静待着，我陪你。', report['reply'])
                    self.assertEqual(report['error'], '')
                backend.stream.assert_called_once()
                system, user = backend.stream.call_args.args
                self.assertIn(self.cfg.name, system)
                self.assertIn('测试用户', user)
                self.assertIn('我今天想安静待一会儿，你陪我聊两句吧。', user)
                self.assertTrue(state['terminal_reached'])
                self.assertTrue(state['closed'])
                backend.close.assert_awaited_once()
                http.assert_not_called()
                self.assertEqual(len(memories), 1)
                memories[0].close_persistence.assert_awaited_once()
                self.assertEqual(paths[0].parent, paths[1].parent)
                self.assertTrue(paths[0].parent.name.startswith('argon-probe-'))
                self.assertFalse(paths[0].parent.exists())


if __name__ == '__main__':
    unittest.main()
