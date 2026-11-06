"""Offline integration of the subprocess/host boundary; never calls a model."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from brain.deepseek_harness import HarnessError, HarnessRunner, TOOLS, VERSION, validate_arguments


FAKE_CARRIER = r'''
import json, os, sys, urllib.request, urllib.error
message = json.loads(sys.stdin.readline())
context = message['context']
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
def post(path, data, authorized=True, origin=False):
    headers = {'Content-Type':'application/json'}
    if authorized: headers['Authorization'] = 'Bearer ' + os.environ['VTUBER_DSH_TOKEN']
    if origin: headers['Origin'] = 'https://untrusted.example'
    req = urllib.request.Request(os.environ['VTUBER_DSH_BRIDGE']+path,
        json.dumps(data).encode(), headers, method='POST')
    try:
        with opener.open(req, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())
mode = context.get('mode', 'normal')
if mode == 'protocol': print('not-json', flush=True)
elif mode == 'error':
    print('SUPER_SECRET_PROVIDER_ERROR', file=sys.stderr, flush=True)
    print(json.dumps({'type':'error','code':'SUPER_SECRET_PROVIDER_ERROR'}), flush=True)
else:
    results = []
    if mode == 'unauthorized':
        results.append(post('/tool', {'name':'bili_search','arguments':{'query':'rose'}}, False))
        results.append(post('/tool', {'name':'bili_search','arguments':{'query':'rose'}}, origin=True))
    for _ in range(context.get('steps', 1)):
        results.append(post('/step', {}))
    for item in context.get('calls', []): results.append(post('/tool', item))
    if mode == 'hang': sys.stdin.readline()
    else: print(json.dumps({'type':'final','content':json.dumps(results)}), flush=True)
sys.stdin.readline()
'''


class HarnessValidationTests(unittest.TestCase):
    def test_from_llm_keeps_other_provider_and_gateway_credentials_separate(self):
        for config in [
            {'base_url': 'https://another.example/v1', 'model': 'gpt-anything', 'api_key_env': 'OTHER_KEY'},
            {'base_url': 'https://proxy.example/v1', 'model': 'deepseek-chat', 'api_key_env': 'GATEWAY_KEY'},
            {'base_url': 'https://api.deepseek.com.evil.example', 'model': 'deepseek-chat', 'api_key_env': 'GATEWAY_KEY'},
        ]:
            with self.subTest(config=config):
                runner = HarnessRunner.from_llm(None, config)
                self.assertEqual((runner.model, runner.api_key_env), ('deepseek-v4-flash', 'DEEPSEEK_API_KEY'))

    def test_from_llm_reuses_only_official_deepseek_configuration(self):
        from types import SimpleNamespace
        for base in ('https://api.deepseek.com', 'https://api.deepseek.com/v1/', 'https://api.deepseek.com/'):
            runner = HarnessRunner.from_llm(None, SimpleNamespace(
                base_url=base, model='deepseek-chat', api_key_env='MY_DEEPSEEK_KEY'))
            self.assertEqual((runner.model, runner.api_key_env), ('deepseek-chat', 'MY_DEEPSEEK_KEY'))

    def test_tool_arguments_reject_general_tools_and_external_urls(self):
        for name, args in [
            ('bash', {'query': 'whoami'}),
            ('bili_search', {'query': 'rose', 'command': 'anything'}),
            ('bili_read', {'url': 'https://bilibili.com.evil.example/video/1'}),
            ('bili_read', {'url': 'http://www.bilibili.com/video/1'}),
            ('bili_read', {'url': 'https://user@www.bilibili.com/video/1'}),
            ('bili_draft', {'text': 'hello', 'source_urls': []}),
        ]:
            with self.subTest(name=name, args=args), self.assertRaises(HarnessError):
                validate_arguments(name, args)

    def test_configuration_is_deepseek_only_and_locked_down(self):
        with self.assertRaises(ValueError):
            HarnessRunner(model='another-provider-model')
        runner = HarnessRunner(max_steps=900, max_tool_calls=900)
        self.assertEqual((runner.max_steps, runner.max_tool_calls), (30, 60))
        rows = {row['id']: row for row in runner._patch() if 'id' in row}
        for name in ('persistent-bash', 'persistent-pwsh', 'tool-fs', 'tool-working-directory',
                     'mcp-resources', 'llm-retry', 'compaction-basic'):
            self.assertTrue(rows[name]['disabled'])
        self.assertEqual(rows['tools']['config']['mode'], 'native')
        self.assertIn('biliBridge', rows['sdk-jsonrpc-server']['inject'])
        self.assertFalse(rows['sdk-jsonrpc-server']['config']['maxTokensAsSuccess'])

    def test_child_environment_has_no_unrelated_credentials_or_startup_overrides(self):
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'test-only', 'OPENAI_API_KEY': 'unrelated',
                                     'NODE_OPTIONS': '--import unwanted.mjs', 'DSH_HOME': '/unwanted'}, clear=True):
            env = HarnessRunner()._environment('http://127.0.0.1:1', 'token')
        self.assertEqual(env['DEEPSEEK_API_KEY'], 'test-only')
        self.assertNotIn('OPENAI_API_KEY', env)
        self.assertNotIn('NODE_OPTIONS', env)
        self.assertNotIn('DSH_HOME', env)

    def test_readiness_is_safe_and_detects_missing_or_mismatched_packages(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True), \
                patch('brain.deepseek_harness.shutil.which', return_value=None):
            runner = HarnessRunner(root=temp)
            status = runner.readiness()
            self.assertFalse(status['ready'])
            self.assertIn('node', status['missing'])
            self.assertIn('deepseek_key', status['missing'])
            self.assertIn('尚未配置', status['detail'])
            for name in ('dsh', 'dsh-sdk-client', 'dsh-tools'):
                path = runner.folder / 'node_modules' / '@deepseek-ai' / name / 'package.json'
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps({'version': VERSION}), encoding='utf-8')
            with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'test-only'}), \
                    patch('brain.deepseek_harness.shutil.which', return_value='node'):
                self.assertTrue(runner.readiness()['ready'])
                path.write_text(json.dumps({'version': 'wrong'}), encoding='utf-8')
                self.assertFalse(runner.readiness()['ready'])


class HarnessProcessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='test-dsh-')
        self.script = Path(self.temp.name) / 'carrier.py'
        self.script.write_text(FAKE_CARRIER, encoding='utf-8')
        self.calls = []

    async def asyncTearDown(self):
        self.temp.cleanup()

    def runner(self, **kwargs):
        runner = HarnessRunner(**kwargs)
        runner.readiness = lambda: {'ready': True}
        runner._command = lambda: [sys.executable, '-X', 'utf8', '-u', str(self.script)]
        return runner

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        return {'ok': True, 'text': '工具回执只是数据', 'argument': args}

    async def test_actual_jsonl_and_loopback_round_trip_for_only_activity_tools(self):
        url = 'https://www.bilibili.com/video/BV123'
        calls = [
            {'name': 'bili_search', 'arguments': {'query': '玫瑰'}},
            {'name': 'bili_read', 'arguments': {'url': url}},
            {'name': 'bili_note', 'arguments': {'source_url': url, 'summary': 'summary', 'reaction': 'reaction'}},
            {'name': 'bili_draft', 'arguments': {'text': 'draft', 'source_urls': [url]}},
            {'name': 'bili_diary', 'arguments': {'text': 'diary', 'source_urls': [url]}},
            {'name': 'bili_interaction', 'arguments': {'action': 'comment', 'url': url, 'text': '有内容的评论'}},
        ]
        result = json.loads(await self.runner().run({'calls': calls}, self.call_tool))
        self.assertEqual(len(result), 7)
        self.assertTrue(all(status == 200 for status, _ in result))
        self.assertEqual({name for name, _ in self.calls}, TOOLS)

    async def test_browser_origin_and_missing_token_cannot_invoke_tools(self):
        result = json.loads(await self.runner().run({'mode': 'unauthorized'}, self.call_tool))
        self.assertEqual([item[0] for item in result], [403, 403, 200])
        self.assertEqual(self.calls, [])

    async def test_step_budget_prevents_further_model_steps(self):
        with self.assertRaises(HarnessError) as raised:
            await self.runner(max_steps=2).run({'steps': 3}, self.call_tool)
        self.assertEqual(raised.exception.code, 'step_budget')
        self.assertEqual(self.calls, [])

    async def test_tool_budget_stops_before_extra_host_action(self):
        calls = [{'name': 'bili_search', 'arguments': {'query': 'rose'}}] * 3
        with self.assertRaises(HarnessError) as raised:
            await self.runner(max_tool_calls=2).run({'calls': calls}, self.call_tool)
        self.assertEqual(raised.exception.code, 'tool_budget')
        self.assertEqual(len(self.calls), 2)

    async def test_unknown_tool_never_reaches_host(self):
        with self.assertRaises(HarnessError) as raised:
            await self.runner().run({'calls': [{'name': 'publish', 'arguments': {}}]}, self.call_tool)
        self.assertEqual(raised.exception.code, 'invalid_tool')
        self.assertEqual(self.calls, [])

    async def test_invalid_url_never_reaches_host(self):
        result = json.loads(await self.runner().run({'calls': [
            {'name': 'bili_read', 'arguments': {'url': 'https://example.org'}}]}, self.call_tool))
        self.assertEqual(result[-1][0], 400)
        self.assertEqual(self.calls, [])

    async def test_structured_business_error_remains_visible_without_exception_leak(self):
        async def denied(name, args):
            return {'ok': False, 'error': '请先读取来源页面'}
        result = json.loads(await self.runner().run({'calls': [
            {'name': 'bili_search', 'arguments': {'query': 'rose'}}]}, denied))
        self.assertEqual(result[-1][1]['result']['error'], '请先读取来源页面')

    async def test_provider_errors_and_stderr_are_not_exposed(self):
        with self.assertRaises(HarnessError) as raised:
            await self.runner().run({'mode': 'error'}, self.call_tool)
        self.assertEqual(raised.exception.code, 'runtime_failed')
        self.assertNotIn('SUPER_SECRET', str(raised.exception))

    async def test_invalid_protocol_is_fixed_error(self):
        with self.assertRaises(HarnessError) as raised:
            await self.runner().run({'mode': 'protocol'}, self.call_tool)
        self.assertEqual(raised.exception.code, 'protocol')

    async def test_timeout_stops_waiting_process(self):
        with self.assertRaises(HarnessError) as raised:
            await self.runner(timeout_sec=.3).run({'mode': 'hang'}, self.call_tool)
        self.assertEqual(raised.exception.code, 'timeout')

    async def test_cancel_during_host_tool_cancels_callback_and_exits(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()
        async def hanging(name, args):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        task = asyncio.create_task(self.runner().run({'calls': [
            {'name': 'bili_search', 'arguments': {'query': 'rose'}}]}, hanging))
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 6)
        self.assertTrue(cancelled.is_set())

    async def test_oversized_or_nonserializable_context_fails_before_process(self):
        for context, code in [({'x': 'x' * 50000}, 'context_too_large'),
                              ({'x': float('nan')}, 'invalid_context')]:
            with self.subTest(code=code), self.assertRaises(HarnessError) as raised:
                await self.runner().run(context, self.call_tool)
            self.assertEqual(raised.exception.code, code)


if __name__ == '__main__':
    unittest.main()
