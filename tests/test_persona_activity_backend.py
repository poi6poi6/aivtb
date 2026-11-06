"""Background decisions share the active persona transport without chat state."""
from __future__ import annotations

import asyncio
import json
import os
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx

from brain.config import LLMConfig, PersonaConfig, PersonaLLMConfig
from brain.persona import Persona, PersonaActivityError


_KEY = 'TEST_PERSONA_ACTIVITY_KEY'
_JSON = '{"action":"finish","text":"本轮结束"}'


class TrackedStream:
    def __init__(self, parts=(), error=None, *, block=False):
        self.parts = iter(parts)
        self.error = error
        self.block = block
        self.waiting = asyncio.Event()
        self.aclose = AsyncMock()

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self.parts)
        except StopIteration:
            if self.block:
                self.waiting.set()
                await asyncio.Event().wait()
            if self.error:
                raise self.error
            raise StopAsyncIteration


def completion(content=_JSON, *, reason='stop', **message):
    return {'choices': [{'finish_reason': reason,
                         'message': {'role': 'assistant', 'content': content, **message}}]}


def event(delta=None, reason=None):
    return 'data: ' + json.dumps({'choices': [{'index': 0, 'delta': delta or {},
                                              'finish_reason': reason}]}) + '\n\n'


class PersonaActivityBackendTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {_KEY: ''})
        self.env.start()
        self.addCleanup(self.env.stop)

    def persona(self, override=None):
        persona = Persona(PersonaConfig(name='活动角色'),
                          LLMConfig(api_key_env=_KEY, max_tokens=80, thinking=False),
                          memory=Mock(), persona_llm=override)
        self.addAsyncCleanup(persona.close)
        return persona

    def backend_persona(self, stream, *, backend='gemini_api', available=True, timeout=45):
        transport = Mock(available=available, stream=Mock(return_value=stream), close=AsyncMock())
        target = ('brain.gemini_chat.GeminiChat' if backend == 'gemini_api'
                  else 'brain.antigravity_chat.AntigravityChat')
        with patch(target, return_value=transport):
            persona = self.persona(PersonaLLMConfig(backend=backend, timeout_sec=timeout))
        persona._http_client = Mock(side_effect=AssertionError('must use existing backend'))
        return persona, transport

    def inherited(self, response):
        os.environ[_KEY] = 'offline-fixture'
        persona = self.persona()
        requests = []

        def respond(request):
            requests.append(request)
            return response if isinstance(response, httpx.Response) else httpx.Response(200, json=response)

        persona._client = httpx.AsyncClient(transport=httpx.MockTransport(respond), trust_env=False)
        return persona, requests

    async def test_gemini_and_cli_reuse_instance_without_chat_state(self):
        for backend in ('gemini_api', 'antigravity_cli'):
            with self.subTest(backend=backend):
                stream = TrackedStream([_JSON[:12], _JSON[12:]])
                persona, transport = self.backend_persona(stream, backend=backend)
                persona.system_prompt = Mock(side_effect=AssertionError('private chat prompt'))
                persona.user_prompt = Mock(side_effect=AssertionError('private chat prompt'))
                persona._recent_replies.append('private chat reply')
                persona.last_mode, persona.last_text = 'llm', 'private last reply'
                persona.topics, persona.dialogue, persona.behavior = Mock(), Mock(), Mock()
                state = {key: value for key, value in vars(persona).items() if key.startswith('last_')}
                self.assertEqual(await persona.activity_completion('公开人格设定', '公开阅读材料'), _JSON)
                self.assertIs(persona._chat_backend, transport)
                transport.stream.assert_called_once_with('公开人格设定', '公开阅读材料')
                transport.close.assert_not_awaited()
                stream.aclose.assert_awaited_once()
                persona.system_prompt.assert_not_called()
                persona.user_prompt.assert_not_called()
                self.assertFalse(persona.memory.mock_calls)
                for name in ('topics', 'dialogue', 'behavior'):
                    self.assertFalse(getattr(persona, name).mock_calls)
                self.assertEqual(list(persona._recent_replies), ['private chat reply'])
                self.assertEqual({key: value for key, value in vars(persona).items()
                                  if key.startswith('last_')}, state)

    async def test_inherited_transport_has_separate_budget_and_current_sampling(self):
        persona, requests = self.inherited(completion())
        persona.llm.temperature = 0.3
        persona.llm.reasoning_effort = 'medium'
        self.assertEqual(await persona.activity_completion('活动系统', '阅读数据'), _JSON)
        payload = json.loads(requests[0].content)
        self.assertEqual(str(requests[0].url), 'https://api.deepseek.com/chat/completions')
        self.assertEqual(requests[0].headers['Authorization'], 'Bearer offline-fixture')
        self.assertEqual(payload, {
            'model': persona.llm.model,
            'messages': [{'role': 'system', 'content': '活动系统'},
                         {'role': 'user', 'content': '阅读数据'}],
            'max_tokens': 2048, 'temperature': 0.3, 'thinking': {'type': 'disabled'},
        })
        self.assertEqual(persona.llm.max_tokens, 80)
        self.assertFalse(persona._client.is_closed)

    async def test_inherited_thinking_and_reasoning_are_preserved(self):
        for thinking in (None, True):
            with self.subTest(thinking=thinking):
                persona, requests = self.inherited(completion())
                persona.llm.thinking, persona.llm.reasoning_effort = thinking, 'high'
                await persona.activity_completion('system', 'user')
                payload = json.loads(requests[0].content)
                self.assertEqual(payload['reasoning_effort'], 'high')
                self.assertNotIn('temperature', payload)
                if thinking is None:
                    self.assertNotIn('thinking', payload)
                else:
                    self.assertEqual(payload['thinking'], {'type': 'enabled'})

    async def test_unconfigured_transport_never_falls_back(self):
        persona = self.persona()
        persona._http_client = Mock()
        with self.assertRaises(PersonaActivityError):
            await persona.activity_completion('system', 'user')
        persona._http_client.assert_not_called()
        for name in ('gemini_api', 'antigravity_cli'):
            with self.subTest(backend=name):
                persona, backend = self.backend_persona(TrackedStream([_JSON]), backend=name, available=False)
                os.environ[_KEY] = 'auxiliary-key-do-not-use'
                with self.assertRaises(PersonaActivityError):
                    await persona.activity_completion('system', 'user')
                backend.stream.assert_not_called()
                persona._http_client.assert_not_called()

    async def test_input_byte_budget_rejects_before_transport(self):
        for system, user in ((None, 'u'), ('s', {}), ('汉' * 85333, 'ab')):
            with self.subTest(system_type=type(system).__name__):
                persona, backend = self.backend_persona(TrackedStream([_JSON]))
                with self.assertRaises(PersonaActivityError):
                    await persona.activity_completion(system, user)
                backend.stream.assert_not_called()
        persona, backend = self.backend_persona(TrackedStream([_JSON]))
        # Exercise the independent hard byte ceiling with an explicitly larger
        # configured context allowance; the normal application budget is lower.
        persona.persona_llm.context_window_tokens = 400000
        persona.persona_llm.max_input_tokens = 300000
        self.assertEqual(await persona.activity_completion('汉' * 85333, 'a'), _JSON)
        backend.stream.assert_called_once()

    async def test_invalid_backend_output_is_not_a_decision(self):
        for parts in ([], ['   '], ['x' * 32769], [{'tool_calls': []}]):
            with self.subTest(parts_type=type(parts[0]).__name__ if parts else 'empty'):
                stream = TrackedStream(parts)
                persona, backend = self.backend_persona(stream)
                with self.assertRaises(PersonaActivityError):
                    await persona.activity_completion('system', 'user')
                stream.aclose.assert_awaited_once()
                backend.close.assert_not_awaited()

    async def test_valid_json_followed_by_transport_error_is_not_a_decision(self):
        stream = TrackedStream([_JSON], RuntimeError('PRIVATE KEY and upstream input'))
        persona, backend = self.backend_persona(stream)
        with self.assertRaises(PersonaActivityError) as raised:
            await persona.activity_completion('system', 'user')
        self.assertNotIn('PRIVATE', str(raised.exception))
        stream.aclose.assert_awaited_once()
        backend.close.assert_not_awaited()

    async def test_http_truncation_tools_malformed_and_empty_output_rejected(self):
        responses = [completion(reason=reason) for reason in (None, 'length', 'tool_calls', 'content_filter')]
        responses += [completion(tool_calls=[{'function': {'name': 'bili_publish'}}]),
                      completion(function_call={'name': 'run'}), completion(role='tool'),
                      completion(''), completion('  '), completion(['bad']),
                      completion('x' * 32769), {'choices': []}, [], {'choices': [None]},
                      {'choices': [completion()['choices'][0]] * 2}]
        for response in responses:
            with self.subTest(kind=type(response).__name__):
                persona, _ = self.inherited(response)
                with self.assertRaises(PersonaActivityError):
                    await persona.activity_completion('system', 'user')

    async def test_http_and_parse_errors_are_fixed_and_secret_safe(self):
        for response in (httpx.Response(403, text='PRIVATE API KEY'),
                         httpx.Response(200, text='PRIVATE user and invalid JSON')):
            persona, _ = self.inherited(response)
            with self.assertRaises(PersonaActivityError) as raised:
                await persona.activity_completion('system', 'user')
            self.assertNotIn('PRIVATE', str(raised.exception))
            self.assertNotIn('https://', str(raised.exception))

    async def test_cancel_closes_only_this_stream_and_propagates(self):
        for cleanup_failure in (False, True):
            with self.subTest(cleanup_failure=cleanup_failure):
                stream = TrackedStream([_JSON], block=True)
                if cleanup_failure:
                    stream.aclose.side_effect = RuntimeError('PRIVATE cleanup detail')
                persona, backend = self.backend_persona(stream)
                task = asyncio.create_task(persona.activity_completion('system', 'user'))
                await stream.waiting.wait()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                stream.aclose.assert_awaited_once()
                backend.close.assert_not_awaited()
                self.assertEqual(persona.last_mode, 'unknown')

    async def test_timeout_closes_stream_and_never_returns_partial_json(self):
        stream = TrackedStream([_JSON], block=True)
        persona, backend = self.backend_persona(stream, timeout=0.01)
        with self.assertRaisesRegex(PersonaActivityError, '超时'):
            await persona.activity_completion('system', 'user')
        stream.aclose.assert_awaited_once()
        backend.close.assert_not_awaited()

    async def test_real_gemini_transport_validates_final_marker_without_native_tools(self):
        os.environ[_KEY] = 'offline-fixture'
        for suffix, succeeds in ((event(reason='stop') + 'data: [DONE]\n\n', True),
                                  ('data: [DONE]\n\n', False),
                                  (event(reason='stop'), False),
                                  (event(reason='length') + 'data: [DONE]\n\n', False),
                                  (event({'tool_calls': [{'function': {'name': 'run'}}]}), False)):
            with self.subTest(succeeds=succeeds, suffix=suffix[:30]):
                persona = self.persona(PersonaLLMConfig(backend='gemini_api', api_key_env=_KEY,
                                                       model='gemini-4-argon'))
                backend = persona._chat_backend
                requests = []

                def respond(request):
                    requests.append(request)
                    return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                                          text=event({'content': _JSON}) + suffix)

                backend._client = httpx.AsyncClient(transport=httpx.MockTransport(respond), trust_env=False)
                if succeeds:
                    self.assertEqual(await persona.activity_completion('activity', 'public data'), _JSON)
                else:
                    with self.assertRaises(PersonaActivityError):
                        await persona.activity_completion('activity', 'public data')
                payload = json.loads(requests[0].content)
                self.assertNotIn('tools', payload)
                self.assertEqual(payload['max_tokens'], 4096)
                self.assertFalse(backend._client.is_closed)


if __name__ == '__main__':
    unittest.main()
