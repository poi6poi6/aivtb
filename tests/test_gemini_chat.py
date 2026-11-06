from __future__ import annotations

import asyncio
import json
import os
import unittest
from unittest.mock import patch

import httpx

from brain.gemini_chat import GeminiChat, GeminiChatError, _proxy_options, _status_error


_KEY_ENV = 'TEST_GEMINI_TRANSPORT_KEY'
_KEY = 'fake-key-for-offline-testing-only'


def event(delta=None, reason=None):
    return 'data: ' + json.dumps({
        'choices': [{'index': 0, 'delta': delta or {}, 'finish_reason': reason}],
    }, ensure_ascii=False) + '\n\n'


def success(text='嗯，我陪着你。'):
    return event({'content': text}) + event(reason='stop') + 'data: [DONE]\n\n'


def completion(text='嗯，我陪着你。'):
    return {'choices': [{'index': 0, 'finish_reason': 'stop',
                         'message': {'role': 'assistant', 'content': text}}]}


class TrackedBody(httpx.AsyncByteStream):
    def __init__(self, parts, *, block=False):
        self.parts = parts
        self.block = block
        self.closed = False
        self.waiting = asyncio.Event()

    async def __aiter__(self):
        for part in self.parts:
            yield part.encode('utf-8')
        if self.block:
            self.waiting.set()
            await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


class GeminiChatTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {_KEY_ENV: _KEY})
        self.env.start()
        self.addCleanup(self.env.stop)

    def adapter(self, handler, **kwargs):
        adapter = GeminiChat(api_key_env=_KEY_ENV, model='gemini-4-argon', **kwargs)
        adapter._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), trust_env=False,
        )
        self.addAsyncCleanup(adapter.close)
        return adapter

    def stream_adapter(self, content, **kwargs):
        body = TrackedBody([content])
        return self.adapter(lambda request: httpx.Response(
            200, headers={'content-type': 'text/event-stream; charset=utf-8'}, stream=body,
        ), **kwargs), body

    async def collect(self, adapter):
        return ''.join([text async for text in adapter.stream('角色提示', '固定测试消息')])

    def test_safe_error_codes_are_explicit_and_allowlisted(self):
        for status, code in ((401, 'model_access'), (403, 'model_access'),
                             (429, 'model_quota'), (404, 'model_unavailable'),
                             (503, 'model_unavailable'), (400, 'model_response')):
            self.assertEqual(_status_error(status).code, code)
        for code in ('PRIVATE', None, []):
            self.assertEqual(GeminiChatError('fixed', code=code).code, 'model_response')

    async def test_official_endpoint_header_and_parameters_keep_thoughts_out_of_speech(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, text=(
                ': heartbeat\n\n' + event({'role': 'assistant', 'reasoning_content': 'PRIVATE THOUGHT'})
                + event({'content': '嗯，'}) + event({'content': '我在呢。'})
                + event(reason='stop') + 'data: [DONE]\n\n'
            ))
        adapter = self.adapter(handler)
        self.assertEqual(await self.collect(adapter), '嗯，我在呢。')
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(str(request.url),
                         'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions')
        self.assertEqual(request.headers['authorization'], 'Bearer ' + _KEY)
        payload = json.loads(request.content)
        self.assertEqual(payload, {
            'model': 'gemini-4-argon', 'reasoning_effort': 'low', 'max_tokens': 4096,
            'stream': True, 'messages': [
                {'role': 'system', 'content': '角色提示'},
                {'role': 'user', 'content': '固定测试消息'},
            ],
        })
        self.assertNotIn(_KEY, str(request.url))
        self.assertNotIn(_KEY, request.content.decode())

    async def test_each_turn_reuses_connection_without_conversation_history(self):
        bodies = []
        def handler(request):
            bodies.append(json.loads(request.content))
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, text=success())
        adapter = self.adapter(handler)
        await self.collect(adapter)
        await self.collect(adapter)
        self.assertEqual(len(bodies), 2)
        self.assertEqual(bodies[0], bodies[1])

    async def test_status_errors_are_classified_without_reading_upstream_body(self):
        for status, message in ((401, '密钥'), (403, '权限'), (429, '配额'),
                                (404, '模型'), (503, '服务暂时'), (400, '未接受'),
                                (302, '未接受')):
            with self.subTest(status=status):
                body = TrackedBody([_KEY + ' PRIVATE INPUT'])
                adapter = self.adapter(lambda request: httpx.Response(
                    status, stream=body, headers={'location': 'https://example.invalid/redirect'},
                ))
                with self.assertRaises(GeminiChatError) as error:
                    await self.collect(adapter)
                self.assertIn(message, str(error.exception))
                self.assertNotIn(_KEY, str(error.exception))
                self.assertNotIn('PRIVATE', str(error.exception))
                self.assertTrue(body.closed)

    async def test_in_band_error_is_safe_and_classified(self):
        adapter, body = self.stream_adapter('data: ' + json.dumps({
            'error': {'code': 429, 'message': _KEY + ' PRIVATE INPUT'},
        }) + '\n\n')
        with self.assertRaisesRegex(GeminiChatError, '配额'):
            await self.collect(adapter)
        self.assertTrue(body.closed)

    async def test_network_errors_do_not_include_request_or_key(self):
        for error_type, expected in ((httpx.ConnectError, '无法连接'),
                                     (httpx.ReadTimeout, '超时')):
            with self.subTest(error_type=error_type):
                def handler(request):
                    raise error_type(_KEY + ' PRIVATE INPUT', request=request)
                adapter = self.adapter(handler)
                with self.assertRaises(GeminiChatError) as error:
                    await self.collect(adapter)
                self.assertIn(expected, str(error.exception))
                self.assertNotIn(_KEY, str(error.exception))

    async def test_missing_key_does_not_make_request(self):
        def handler(request):
            self.fail('No request should be sent without a key')
        adapter = self.adapter(handler)
        with patch.dict(os.environ, {_KEY_ENV: '  '}):
            self.assertFalse(adapter.available)
            with self.assertRaisesRegex(GeminiChatError, '未配置'):
                await self.collect(adapter)
        self.assertTrue(adapter.available)

    async def test_json_error_and_wrong_content_type_are_safe(self):
        adapter, body = self.stream_adapter('data: ' + _KEY + '\n\n')
        with self.assertRaisesRegex(GeminiChatError, '格式异常') as error:
            await self.collect(adapter)
        self.assertNotIn(_KEY, str(error.exception))
        self.assertTrue(body.closed)
        adapter = self.adapter(lambda request: httpx.Response(200, json={'key': _KEY}))
        with self.assertRaisesRegex(GeminiChatError, '不是.*流式回复'):
            await self.collect(adapter)

    async def test_truncated_stream_missing_finish_or_done_is_not_success(self):
        cases = (
            event({'content': '未说完'}),
            event({'content': '未说完'}) + 'data: [DONE]\n\n',
            event({'content': '未说完'}) + event(reason='stop'),
        )
        for content in cases:
            with self.subTest(content=content):
                adapter, body = self.stream_adapter(content)
                with self.assertRaisesRegex(GeminiChatError, '提前结束'):
                    await self.collect(adapter)
                self.assertTrue(body.closed)

    async def test_empty_content_and_reasoning_only_are_explicit_failures(self):
        for delta in ({}, {'content': '   '}, {'reasoning_content': 'PRIVATE THOUGHT'}):
            with self.subTest(delta=delta):
                adapter, body = self.stream_adapter(
                    event(delta) + event(reason='stop') + 'data: [DONE]\n\n')
                with self.assertRaisesRegex(GeminiChatError, '没有返回可朗读'):
                    await self.collect(adapter)
                self.assertTrue(body.closed)

    async def test_length_limit_failure_does_not_yield_truncated_final_chunk(self):
        adapter, _ = self.stream_adapter(event({'content': '不完整句'}, 'length') + 'data: [DONE]\n\n')
        stream = adapter.stream('system', 'user')
        with self.assertRaisesRegex(GeminiChatError, '长度上限'):
            await stream.__anext__()

    async def test_tool_calls_and_non_text_content_never_reach_voice(self):
        for delta in ({'tool_calls': [{'function': {'name': 'run'}}], 'content': '不能朗读'},
                      {'function_call': {'name': 'run'}, 'content': '不能朗读'},
                      {'content': [{'type': 'image_url'}]}):
            with self.subTest(delta=delta):
                adapter, _ = self.stream_adapter(event(delta) + event(reason='stop') + 'data: [DONE]\n\n')
                stream = adapter.stream('system', 'user')
                with self.assertRaises(GeminiChatError):
                    await stream.__anext__()

    async def test_early_consumer_close_releases_response(self):
        body = TrackedBody([event({'content': '第一句。'}), success('第二句。')])
        adapter = self.adapter(lambda request: httpx.Response(
            200, headers={'content-type': 'text/event-stream'}, stream=body))
        stream = adapter.stream('system', 'user')
        self.assertEqual(await stream.__anext__(), '第一句。')
        self.assertFalse(body.closed)
        await stream.aclose()
        self.assertTrue(body.closed)
        self.assertFalse(adapter._client.is_closed)
        await adapter.close()
        self.assertTrue(adapter._client.is_closed)
        self.assertFalse(adapter.available)

    async def test_cancellation_releases_response_and_is_not_reported_as_model_failure(self):
        body = TrackedBody([], block=True)
        adapter = self.adapter(lambda request: httpx.Response(
            200, headers={'content-type': 'text/event-stream'}, stream=body))
        task = asyncio.create_task(self.collect(adapter))
        await asyncio.wait_for(body.waiting.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(body.closed)

    async def test_total_stream_timeout_releases_response(self):
        body = TrackedBody([], block=True)
        adapter = self.adapter(lambda request: httpx.Response(
            200, headers={'content-type': 'text/event-stream'}, stream=body), timeout_sec=0.03)
        with self.assertRaisesRegex(GeminiChatError, '超时'):
            await self.collect(adapter)
        self.assertTrue(body.closed)

    async def test_utf8_split_across_network_packets_decodes_correctly(self):
        raw = success('嗯，没关系。').encode('utf-8')
        class ByteBody(httpx.AsyncByteStream):
            async def __aiter__(self):
                for byte in raw:
                    yield bytes([byte])
        adapter = self.adapter(lambda request: httpx.Response(
            200, headers={'content-type': 'text/event-stream'}, stream=ByteBody()))
        self.assertEqual(await self.collect(adapter), '嗯，没关系。')

    async def test_complete_uses_final_message_only_with_thought_summaries_disabled(self):
        requests = []
        result = completion('你好，我在呢。')
        result['choices'][0]['delta'] = {'content': 'PARTIAL DRAFT'}
        result['choices'][0]['message'].update(
            reasoning_content='PRIVATE THOUGHT', thoughts=['PRIVATE THOUGHT'],
            reasoning_details=[{'text': 'PRIVATE THOUGHT'}],
        )
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json=result)
        adapter = self.adapter(handler)
        self.assertEqual(await adapter.complete('角色提示', '固定测试消息', max_tokens=2048), '你好，我在呢。')
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(str(request.url),
                         'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions')
        self.assertEqual(request.headers['authorization'], 'Bearer ' + _KEY)
        self.assertEqual(request.headers['accept'], 'application/json')
        self.assertEqual(json.loads(request.content), {
            'model': 'gemini-4-argon', 'max_tokens': 2048,
            'stream': False, 'messages': [
                {'role': 'system', 'content': '角色提示'},
                {'role': 'user', 'content': '固定测试消息'},
            ],
            'extra_body': {'google': {'thinking_config': {
                'thinking_level': 'low', 'include_thoughts': False}}},
        })
        self.assertNotIn(_KEY, str(request.url))
        self.assertNotIn(_KEY, request.content.decode())
        self.assertEqual(adapter.max_tokens, 4096)

    async def test_complete_reuses_connection_without_saving_prior_turns(self):
        payloads = []
        def handler(request):
            payloads.append(json.loads(request.content))
            return httpx.Response(200, json=completion())
        adapter = self.adapter(handler, max_tokens=3072)
        client = adapter._client
        await adapter.complete('system', 'first')
        await adapter.complete('system', 'second')
        self.assertIs(adapter._client, client)
        self.assertFalse(client.is_closed)
        self.assertEqual([p['messages'][1]['content'] for p in payloads], ['first', 'second'])
        self.assertTrue(all(len(p['messages']) == 2 and p['max_tokens'] == 3072 for p in payloads))

    async def test_qq_json_contract_is_strict_and_does_not_change_other_calls(self):
        payloads = []
        def handler(request):
            payloads.append(json.loads(request.content))
            return httpx.Response(200, json=completion('{"reply":"你好。"}'))
        adapter = self.adapter(handler)
        self.assertEqual(await adapter.complete('system', 'hello', json_reply=True), '{"reply":"你好。"}')
        contract = payloads[0]['response_format']
        self.assertEqual(contract['type'], 'json_schema')
        self.assertIs(contract['json_schema']['strict'], True)
        schema = contract['json_schema']['schema']
        self.assertEqual(schema['type'], 'object')
        self.assertEqual(set(schema['properties']), {'reply'})
        self.assertEqual(schema['required'], ['reply'])
        self.assertIs(schema['additionalProperties'], False)
        self.assertEqual(schema['properties']['reply']['type'], 'string')
        self.assertIs(payloads[0]['extra_body']['google']['thinking_config']['include_thoughts'], False)
        await adapter.complete('system', 'regular completion')
        self.assertNotIn('response_format', payloads[1])
        for value in (1, 'yes', None, {}):
            with self.assertRaises(GeminiChatError):
                await adapter.complete('system', 'hello', json_reply=value)
        self.assertEqual(len(payloads), 2)

    async def test_complete_rejects_incomplete_nonassistant_tool_and_reasoning_only_responses(self):
        cases = [None, [], {}, {'choices': []}, {'choices': completion()['choices'] * 2}]
        for key, values in (
            ('finish_reason', (None, 'length', 'tool_calls', 'content_filter', '')),
            ('index', (None, False, '0', 1)),
            ('message', (None, {}, {'role': 'user', 'content': 'text'},
                         {'content': 'missing role'},
                         {'role': 'assistant', 'reasoning_content': 'PRIVATE THOUGHT'},
                         {'role': 'assistant', 'content': 'text', 'tool_calls': [{'id': 'tool'}]},
                         {'role': 'assistant', 'content': 'text', 'function_call': {'name': 'tool'}},
                         {'role': 'assistant', 'content': [{'type': 'text', 'text': 'text'}]},
                         {'role': 'assistant', 'content': '  '})),
        ):
            for value in values:
                result = completion()
                result['choices'][0][key] = value
                cases.append(result)
        for result in cases:
            with self.subTest(result=result):
                adapter = self.adapter(lambda request: httpx.Response(
                    200, content=json.dumps(result), headers={'content-type': 'application/json'}))
                with self.assertRaises(GeminiChatError) as error:
                    await adapter.complete('system', 'user')
                self.assertNotIn('PRIVATE THOUGHT', str(error.exception))
        adapter = self.adapter(lambda request: httpx.Response(200, json={
            'choices': [{'index': 0, 'finish_reason': 'length',
                         'message': {'role': 'assistant', 'content': 'partial'}}]}))
        with self.assertRaisesRegex(GeminiChatError, '长度上限'):
            await adapter.complete('system', 'user')

    async def test_complete_http_errors_are_safe_and_never_read_upstream_body(self):
        for status, expected in ((401, '密钥'), (403, '权限'), (429, '配额'),
                                  (404, '模型'), (503, '服务暂时'), (400, '未接受'),
                                  (302, '未接受')):
            with self.subTest(status=status):
                class UnreadBody(TrackedBody):
                    async def __aiter__(self):
                        raise AssertionError('Error response body must not be read')
                        yield b''
                body = UnreadBody([])
                adapter = self.adapter(lambda request: httpx.Response(status, stream=body))
                with self.assertRaisesRegex(GeminiChatError, expected) as error:
                    await adapter.complete('system', 'user')
                self.assertNotIn(_KEY, str(error.exception))
                self.assertTrue(body.closed)

    async def test_complete_in_band_and_network_errors_are_safe(self):
        adapter = self.adapter(lambda request: httpx.Response(200, json={
            'error': {'code': 429, 'message': _KEY + ' PRIVATE INPUT'}}))
        with self.assertRaisesRegex(GeminiChatError, '配额') as error:
            await adapter.complete('system', 'user')
        self.assertEqual(error.exception.code, 'model_quota')
        self.assertNotIn(_KEY, str(error.exception))
        for error_type, expected in ((httpx.ConnectError, '无法连接'), (httpx.ReadTimeout, '超时')):
            def handler(request):
                raise error_type(_KEY + ' PRIVATE INPUT', request=request)
            adapter = self.adapter(handler)
            with self.assertRaisesRegex(GeminiChatError, expected) as error:
                await adapter.complete('system', 'user')
            self.assertEqual(error.exception.code, 'model_timeout' if error_type is httpx.ReadTimeout else 'model_connection')
            self.assertNotIn(_KEY, str(error.exception))
            self.assertNotIn('PRIVATE', str(error.exception))

    async def test_complete_missing_key_closed_client_and_bad_limit_never_request(self):
        def handler(request):
            self.fail('Request should not be sent')
        adapter = self.adapter(handler)
        with patch.dict(os.environ, {_KEY_ENV: ''}):
            with self.assertRaisesRegex(GeminiChatError, '未配置'):
                await adapter.complete('system', 'user')
        for limit in (True, False, 0, -1, 1.5, '2048'):
            with self.assertRaisesRegex(GeminiChatError, '长度配置'):
                await adapter.complete('system', 'user', max_tokens=limit)
        await adapter.close()
        with self.assertRaisesRegex(GeminiChatError, '关闭'):
            await adapter.complete('system', 'user')

    async def test_complete_rejects_wrong_content_type_malformed_and_oversized_responses(self):
        cases = [
            ('text/event-stream', success(), '不是完整'),
            ('application/json', _KEY + ' INVALID JSON', '格式异常'),
            ('application/json', 'x' * (1024 * 1024 + 1), '内容过长'),
            ('application/json', json.dumps(completion('x' * (256 * 1024 + 1))), '内容过长'),
        ]
        for content_type, text, expected in cases:
            with self.subTest(content_type=content_type, expected=expected):
                body = TrackedBody([text])
                adapter = self.adapter(lambda request: httpx.Response(
                    200, stream=body, headers={'content-type': content_type}))
                with self.assertRaisesRegex(GeminiChatError, expected) as error:
                    await adapter.complete('system', 'user')
                self.assertNotIn(_KEY, str(error.exception))
                self.assertTrue(body.closed)

    async def test_complete_cancellation_releases_response_but_not_shared_client(self):
        body = TrackedBody([], block=True)
        adapter = self.adapter(lambda request: httpx.Response(
            200, headers={'content-type': 'application/json'}, stream=body))
        task = asyncio.create_task(adapter.complete('system', 'user'))
        await asyncio.wait_for(body.waiting.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(body.closed)
        self.assertFalse(adapter._client.is_closed)

    async def test_complete_total_timeout_includes_headers_and_response_body(self):
        async def blocked_headers(request):
            await asyncio.Event().wait()
        adapter = self.adapter(blocked_headers, timeout_sec=0.03)
        with self.assertRaisesRegex(GeminiChatError, '超时'):
            await adapter.complete('system', 'user')
        body = TrackedBody([], block=True)
        adapter = self.adapter(lambda request: httpx.Response(
            200, headers={'content-type': 'application/json'}, stream=body), timeout_sec=0.03)
        with self.assertRaisesRegex(GeminiChatError, '超时'):
            await adapter.complete('system', 'user')
        self.assertTrue(body.closed)

    async def test_complete_timeout_override_applies_only_to_this_call(self):
        requests = []
        async def delayed_response(request):
            requests.append(request)
            await asyncio.sleep(.04)
            return httpx.Response(200, json=completion())
        adapter = self.adapter(delayed_response, timeout_sec=.02)
        client = adapter._client
        original_client_timeout = client.timeout.as_dict()
        self.assertEqual(await adapter.complete('system', 'user', timeout_sec=.3), '嗯，我陪着你。')
        self.assertTrue(all(value == .3 for value in requests[0].extensions['timeout'].values()))
        self.assertEqual(adapter.timeout_sec, .02)
        self.assertIs(adapter._client, client)
        self.assertEqual(client.timeout.as_dict(), original_client_timeout)
        with self.assertRaises(GeminiChatError) as error:
            await adapter.complete('system', 'next')
        self.assertEqual(error.exception.code, 'model_timeout')
        self.assertTrue(all(value == .02 for value in requests[1].extensions['timeout'].values()))

    async def test_complete_short_override_bounds_body_and_releases_response(self):
        body = TrackedBody([], block=True)
        adapter = self.adapter(lambda request: httpx.Response(
            200, headers={'content-type': 'application/json'}, stream=body), timeout_sec=45)
        with self.assertRaises(GeminiChatError) as error:
            await adapter.complete('system', 'user', timeout_sec=.02)
        self.assertEqual(error.exception.code, 'model_timeout')
        self.assertEqual(adapter.timeout_sec, 45)
        self.assertTrue(body.closed)
        self.assertFalse(adapter._client.is_closed)

    async def test_complete_invalid_timeout_override_never_sends_request(self):
        def handler(request):
            self.fail('Invalid timeout must not send a request')
        adapter = self.adapter(handler)
        for timeout in (True, False, 0, -1, '90', float('nan'), float('inf')):
            with self.subTest(timeout=timeout):
                with self.assertRaises(GeminiChatError) as error:
                    await adapter.complete('system', 'user', timeout_sec=timeout)
                self.assertEqual(error.exception.code, 'model_response')

    async def test_complete_fragmented_utf8_json_is_read_to_the_end_before_return(self):
        raw = json.dumps(completion('嗯，我在呢。'), ensure_ascii=False).encode('utf-8')
        class ByteBody(httpx.AsyncByteStream):
            async def __aiter__(self):
                for byte in raw:
                    yield bytes([byte])
        adapter = self.adapter(lambda request: httpx.Response(
            200, headers={'content-type': 'application/json; charset=utf-8'}, stream=ByteBody()))
        self.assertEqual(await adapter.complete('system', 'user'), '嗯，我在呢。')
        class BrokenBody(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield raw
                raise httpx.ReadError(_KEY + ' PRIVATE INPUT')
        adapter = self.adapter(lambda request: httpx.Response(
            200, headers={'content-type': 'application/json'}, stream=BrokenBody()))
        with self.assertRaisesRegex(GeminiChatError, '无法连接') as error:
            await adapter.complete('system', 'user')
        self.assertNotIn(_KEY, str(error.exception))


class GeminiProxyTests(unittest.TestCase):
    def test_explicit_proxy_and_all_proxy_keep_native_httpx_env_priority(self):
        for key, value in (('HTTPS_PROXY', 'http://127.0.0.1:9'),
                           ('https_proxy', 'http://127.0.0.1:9'),
                           ('HTTP_PROXY', 'http://127.0.0.1:9'), ('ALL_PROXY', 'http://127.0.0.1:9')):
            with self.subTest(key=key):
                env = {key: value, 'NO_PROXY': 'localhost'}
                with patch.dict(os.environ, env, clear=True):
                    original = dict(os.environ)
                    with patch('brain.antigravity_network._windows_proxy_settings') as registry:
                        self.assertEqual(_proxy_options(), {'trust_env': True})
                    registry.assert_not_called()
                    self.assertEqual(dict(os.environ), original)

    def test_explicit_empty_proxies_disable_registry_fallback(self):
        with patch.dict(os.environ, {'HTTPS_PROXY': ''}, clear=True):
            with patch('brain.antigravity_network._windows_proxy_settings') as registry:
                self.assertEqual(_proxy_options(), {'trust_env': False})
            registry.assert_not_called()

    def test_windows_proxy_uses_https_without_changing_environment(self):
        env = {'NO_PROXY': 'localhost'}
        with patch.dict(os.environ, env, clear=True):
            with patch('brain.antigravity_network._windows_proxy_settings',
                       return_value=(True, 'http=127.0.0.1:7000;https=127.0.0.1:7890')):
                self.assertEqual(_proxy_options(), {'proxy': 'http://127.0.0.1:7890', 'trust_env': True})
            self.assertEqual(dict(os.environ), env)

    def test_no_proxy_can_bypass_windows_proxy_for_google(self):
        for name, bypass in (('NO_PROXY', '*'), ('NO_PROXY', 'localhost,*'), ('NO_PROXY', '.googleapis.com'),
                              ('no_proxy', 'localhost,generativelanguage.googleapis.com')):
            with self.subTest(name=name, bypass=bypass):
                with patch.dict(os.environ, {name: bypass}, clear=True):
                    with patch('brain.antigravity_network._windows_proxy_settings',
                               return_value=(True, '127.0.0.1:7890')):
                        self.assertEqual(_proxy_options(), {'trust_env': False})


if __name__ == '__main__':
    unittest.main()
