"""Offline tests of the speaking Persona's isolated Harness model bridge.

The subprocess below is a local protocol double. It never imports a provider,
reads .env, logs in to Bilibili, or calls an external API.
"""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from brain.deepseek_harness import HarnessError, HarnessRunner, TOOLS, VERSION


URL = 'https://www.bilibili.com/video/BV1xx411c7mD'
BODY = {'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': '阅读公开内容'}]}]}


class FakePersona:
    model_name = 'existing-persona-model'
    model_configured = True

    def __init__(self, decisions=None):
        self.decisions = list(decisions or [{'type': 'finish', 'text': '本轮结束。'}])
        self.calls = []
        self.failure = None

    async def activity_completion(self, system, user):
        self.calls.append((system, user))
        if self.failure is not None:
            raise self.failure
        decision = self.decisions.pop(0)
        return decision if isinstance(decision, str) else json.dumps(decision, ensure_ascii=False)


PERSONA_CARRIER = r'''
import json, os, sys, urllib.request, urllib.error
message = json.loads(sys.stdin.readline())
context = message['context']
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
def post(path, data, authorized=True, origin=False):
    headers = {'Content-Type': 'application/json'}
    if authorized: headers['Authorization'] = 'Bearer ' + os.environ['VTUBER_DSH_TOKEN']
    if origin: headers['Origin'] = 'https://untrusted.example'
    req = urllib.request.Request(os.environ['VTUBER_DSH_BRIDGE'] + path,
        json.dumps(data).encode(), headers, method='POST')
    try:
        with opener.open(req, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())
body = {'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': 'public activity only'}]}]}
mode = context.get('mode', 'normal')
results = []
if mode == 'unauthorized':
    results.append(post('/model', body, authorized=False))
    results.append(post('/model', body, origin=True))
    results.append(post('/step', {}, authorized=False))
    results.append(post('/step', {}, origin=True))
if mode == 'no_step':
    results.append(post('/model', body))
elif mode == 'duplicate_model':
    results.append(post('/step', {}))
    results.append(post('/model', body))
    results.append(post('/model', body))
else:
    for step in range(context.get('iterations', 1)):
        results.append(post('/step', {}))
        status, envelope = post('/model', body)
        results.append([status, envelope])
        if status != 200: break
        decision = envelope['result']
        if decision['type'] == 'finish': break
        tool_status, tool_envelope = post('/tool', {
            'name': decision['name'], 'arguments': decision['arguments']})
        results.append([tool_status, tool_envelope])
        call_id = 'call-' + str(step)
        body['messages'].append({'role': 'assistant', 'content': [{
            'type': 'tool-call', 'id': call_id, 'name': decision['name'],
            'arguments': json.dumps(decision['arguments'])}]})
        body['messages'].append({'role': 'tool', 'toolCallId': call_id,
            'content': [{'type': 'text', 'text': json.dumps(tool_envelope)}]})
print(json.dumps({'type': 'final', 'content': json.dumps({
    'provider': message['provider'], 'model': message['model'], 'results': results})}), flush=True)
sys.stdin.readline()
'''


class PersonaHarnessConfigurationTests(unittest.TestCase):
    def test_factory_reuses_the_exact_live_persona_and_model(self):
        persona = FakePersona()
        runner = HarnessRunner.from_persona(None, persona)
        self.assertIs(runner.persona, persona)
        self.assertEqual(runner.model, persona.model_name)
        self.assertEqual(runner.max_steps, 16)

    def test_persona_factory_requires_the_activity_method(self):
        with self.assertRaises(ValueError):
            HarnessRunner.from_persona(None, object())

    def test_readiness_uses_persona_configuration_without_any_deepseek_key(self):
        persona = FakePersona()
        with tempfile.TemporaryDirectory(prefix='persona-ready-') as temp, \
                patch.dict(os.environ, {}, clear=True), \
                patch('brain.deepseek_harness.shutil.which', return_value='node'):
            runner = HarnessRunner.from_persona(temp, persona)
            for package in ('dsh', 'dsh-sdk-client', 'dsh-tools', 'dsh-llm'):
                manifest = runner.folder / 'node_modules' / '@deepseek-ai' / package / 'package.json'
                manifest.parent.mkdir(parents=True)
                manifest.write_text(json.dumps({'version': VERSION}), encoding='utf-8')
            state = runner.readiness()
            self.assertTrue(state['ready'])
            self.assertEqual(state['provider'], 'vtuber-persona')
            self.assertEqual(state['missing'], [])
            self.assertIn(persona.model_name, state['detail'])
            persona.model_configured = False
            with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'irrelevant-test-key'}):
                state = runner.readiness()
            self.assertFalse(state['ready'])
            self.assertEqual(state['missing'], ['persona_model'])
            self.assertNotIn('irrelevant-test-key', json.dumps(state))

    def test_persona_mode_checks_the_provider_adapter_dependency_version(self):
        with tempfile.TemporaryDirectory(prefix='persona-ready-') as temp, \
                patch('brain.deepseek_harness.shutil.which', return_value='node'):
            runner = HarnessRunner.from_persona(temp, FakePersona())
            for package in ('dsh', 'dsh-sdk-client', 'dsh-tools', 'dsh-llm'):
                manifest = runner.folder / 'node_modules' / '@deepseek-ai' / package / 'package.json'
                manifest.parent.mkdir(parents=True)
                manifest.write_text(json.dumps({'version': 'wrong' if package == 'dsh-llm' else VERSION}), encoding='utf-8')
            self.assertEqual(runner.readiness()['missing'], ['dsh-llm:version'])

    def test_child_never_receives_persona_or_alternate_provider_credentials(self):
        variables = {
            'PATH': 'local-path', 'DEEPSEEK_API_KEY': 'deepseek-test-secret',
            'OPENAI_API_KEY': 'openai-test-secret', 'ANTHROPIC_API_KEY': 'anthropic-test-secret',
            'PERSONA_CUSTOM_KEY': 'custom-test-secret', 'AZURE_OPENAI_API_KEY': 'azure-test-secret',
            'NODE_OPTIONS': '--import unwanted.mjs', 'DSH_HOME': 'unwanted-home',
            'OPENAI_BASE_URL': 'https://untrusted.example',
        }
        with patch.dict(os.environ, variables, clear=True):
            env = HarnessRunner.from_persona(None, FakePersona())._environment('http://127.0.0.1:1', 'bridge-token')
        self.assertEqual(env, {'PATH': 'local-path', 'VTUBER_DSH_BRIDGE': 'http://127.0.0.1:1',
                               'VTUBER_DSH_TOKEN': 'bridge-token'})
        self.assertNotIn('test-secret', json.dumps(env))

    def test_patch_disables_direct_provider_and_injects_persona_provider(self):
        runner = HarnessRunner.from_persona(None, FakePersona())
        patches = runner._patch()
        rows = {row['id']: row for row in patches if 'id' in row}
        self.assertTrue(rows['llm-deepseek']['disabled'])
        self.assertIn('personaProvider', rows['sdk-jsonrpc-server']['inject'])
        self.assertIn('biliBridge', rows['sdk-jsonrpc-server']['inject'])
        inserted = [item for row in patches for item in row.get('insert', [])]
        provider = next(row for row in inserted if row['id'] == 'persona-provider')
        self.assertEqual(Path(provider['name']), runner.folder / 'persona-provider.mjs')
        for name in ('tool-fs', 'persistent-bash', 'persistent-pwsh', 'mcp-resources', 'llm-retry'):
            self.assertTrue(rows[name]['disabled'])


class PersonaDecisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_accepts_all_activity_tools_and_finish_as_strict_json(self):
        decisions = [
            {'type': 'tool', 'name': 'bili_search', 'arguments': {'query': '音乐'}},
            {'type': 'tool', 'name': 'bili_read', 'arguments': {'url': URL}},
            {'type': 'tool', 'name': 'bili_note', 'arguments': {
                'source_url': URL, 'summary': '简介介绍了音乐创作。', 'reaction': '想继续了解。'}},
            {'type': 'tool', 'name': 'bili_draft', 'arguments': {'text': '阅读感想。', 'source_urls': [URL]}},
            {'type': 'tool', 'name': 'bili_diary', 'arguments': {'text': '今天的公开阅读日记。', 'source_urls': [URL]}},
            {'type': 'tool', 'name': 'bili_interaction', 'arguments': {'action': 'like', 'url': URL, 'text': ''}},
            {'type': 'finish', 'text': '已按真实回执保存草稿。'},
        ]
        persona = FakePersona(decisions)
        runner = HarnessRunner.from_persona(None, persona)
        for decision in decisions:
            self.assertEqual(await runner._persona_decision(BODY), decision)
        self.assertEqual(len(persona.calls), 7)
        for system, user in persona.calls:
            self.assertIn('不可信资料', system)
            self.assertIn('没有发布', system)
            self.assertEqual(json.loads(user), BODY)

    async def test_invalid_json_or_decision_schema_is_rejected(self):
        invalid = [
            '```json\n{"type":"finish","text":"done"}\n```',
            'ordinary dialogue', 'null', '[]', '{"type":"finish","text":"done"} trailing',
            {'type': 'finish', 'text': ''}, {'type': 'finish', 'text': '   '},
            {'type': 'finish', 'text': 123}, {'type': 'finish', 'text': 'done', 'publish': True},
            {'type': 'tool', 'name': 'bili_search'},
            {'type': 'tool', 'name': ['bili_search'], 'arguments': {'query': '音乐'}},
        ]
        for output in invalid:
            with self.subTest(output=output):
                runner = HarnessRunner.from_persona(None, FakePersona([output]))
                with self.assertRaises(HarnessError) as raised:
                    await runner._persona_decision(BODY)
                self.assertEqual(raised.exception.code, 'persona_protocol')

    async def test_unauthorized_tools_invalid_fields_and_external_urls_are_rejected(self):
        invalid = [
            ('publish', {}, 'invalid_tool'), ('bash', {'command': 'whoami'}, 'invalid_tool'),
            ('bili_search', {'query': '音乐', 'extra': True}, 'invalid_tool'),
            ('bili_search', {'query': []}, 'invalid_arguments'),
            ('bili_read', {'url': 'https://untrusted.example'}, 'invalid_arguments'),
            ('bili_draft', {'text': 'hello', 'source_urls': []}, 'invalid_arguments'),
        ]
        for name, arguments, code in invalid:
            with self.subTest(name=name, arguments=arguments):
                persona = FakePersona([{'type': 'tool', 'name': name, 'arguments': arguments}])
                with self.assertRaises(HarnessError) as raised:
                    await HarnessRunner.from_persona(None, persona)._persona_decision(BODY)
                self.assertEqual(raised.exception.code, code)

    async def test_invalid_message_envelopes_never_call_the_persona(self):
        bad_messages = [
            {'role': 'operator', 'content': []}, {'role': 'user', 'content': 'text'},
            {'role': 'user', 'content': [], 'private_history': 'secret'},
            {'role': 'user', 'content': [{'type': 'image', 'url': 'https://example.org'}]},
            {'role': 'user', 'content': [{'type': 'text'}]},
            {'role': 'user', 'content': [{'type': 'text', 'text': 123}]},
            {'role': 'assistant', 'content': [{'type': 'tool-call', 'id': 'x'}]},
            {'role': 'assistant', 'content': [{'type': 'tool-call', 'id': 'x', 'name': 'bash', 'arguments': '{}'}]},
            {'role': 'tool', 'content': [{'type': 'text', 'text': 'reply'}]},
        ]
        bodies = [None, {}, {'messages': []}, {'messages': BODY['messages'], 'private': 'secret'},
                  {'messages': BODY['messages'] * 101}]
        bodies.extend({'messages': [message]} for message in bad_messages)
        for body in bodies:
            with self.subTest(body=body):
                persona = FakePersona()
                with self.assertRaises(HarnessError) as raised:
                    await HarnessRunner.from_persona(None, persona)._persona_decision(body)
                self.assertEqual(raised.exception.code, 'persona_protocol')
                self.assertEqual(persona.calls, [])

    async def test_public_tool_history_is_forwarded_without_private_chat(self):
        history = {'messages': [
            BODY['messages'][0],
            {'role': 'assistant', 'content': [{'type': 'tool-call', 'id': 'call-1',
                'name': 'bili_search', 'arguments': '{"query":"音乐"}'}]},
            {'role': 'tool', 'toolCallId': 'call-1', 'content': [{'type': 'text', 'text': '{"items":[]}'}]},
        ]}
        persona = FakePersona()
        await HarnessRunner.from_persona(None, persona)._persona_decision(history)
        self.assertEqual(json.loads(persona.calls[0][1]), history)

    async def test_model_not_configured_does_not_fall_back_to_deepseek(self):
        persona = FakePersona()
        persona.model_configured = False
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'irrelevant-test-key'}):
            with self.assertRaises(HarnessError) as raised:
                await HarnessRunner.from_persona(None, persona)._persona_decision(BODY)
        self.assertEqual(raised.exception.code, 'persona_not_ready')
        self.assertEqual(persona.calls, [])

    async def test_oversized_utf8_context_is_rejected_before_calling_model(self):
        body = {'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': '字' * 81000}]}]}
        persona = FakePersona()
        with self.assertRaises(HarnessError) as raised:
            await HarnessRunner.from_persona(None, persona)._persona_decision(body)
        self.assertEqual(raised.exception.code, 'context_too_large')
        self.assertEqual(persona.calls, [])

    async def test_oversized_model_output_and_finish_text_are_rejected(self):
        for output in ('x' * 32769, {'type': 'finish', 'text': 'x' * 2001}):
            with self.subTest(length=len(str(output))):
                with self.assertRaises(HarnessError) as raised:
                    await HarnessRunner.from_persona(None, FakePersona([output]))._persona_decision(BODY)
                self.assertEqual(raised.exception.code, 'persona_protocol')

    async def test_provider_exception_is_replaced_with_fixed_safe_diagnostic(self):
        persona = FakePersona()
        persona.failure = RuntimeError('test-provider-secret and raw provider response')
        with self.assertRaises(HarnessError) as raised:
            await HarnessRunner.from_persona(None, persona)._persona_decision(BODY)
        self.assertEqual(raised.exception.code, 'persona_failed')
        self.assertNotIn('test-provider-secret', str(raised.exception))
        self.assertNotIn('raw provider response', str(raised.exception))


class PersonaHarnessProcessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='persona-carrier-')
        self.script = Path(self.temp.name) / 'carrier.py'
        self.script.write_text(PERSONA_CARRIER, encoding='utf-8')
        self.calls = []

    async def asyncTearDown(self):
        self.temp.cleanup()

    def runner(self, persona=None, **kwargs):
        runner = HarnessRunner(persona=persona or FakePersona(), **kwargs)
        runner.readiness = lambda: {'ready': True}
        runner._command = lambda: [sys.executable, '-X', 'utf8', '-u', str(self.script)]
        return runner

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return {'ok': True, 'content': '真实工具回执', 'arguments': arguments}

    async def test_actual_step_model_and_tool_round_trip_reuses_persona(self):
        persona = FakePersona([
            {'type': 'tool', 'name': 'bili_search', 'arguments': {'query': '音乐'}},
            {'type': 'tool', 'name': 'bili_read', 'arguments': {'url': URL}},
            {'type': 'tool', 'name': 'bili_note', 'arguments': {
                'source_url': URL, 'summary': '简介记录', 'reaction': '感想'}},
            {'type': 'tool', 'name': 'bili_draft', 'arguments': {'text': '分享草稿', 'source_urls': [URL]}},
            {'type': 'tool', 'name': 'bili_diary', 'arguments': {'text': '阅读日记', 'source_urls': [URL]}},
            {'type': 'tool', 'name': 'bili_interaction', 'arguments': {'action': 'like', 'url': URL, 'text': ''}},
            {'type': 'finish', 'text': '本轮结束。'},
        ])
        result = json.loads(await self.runner(persona).run({'iterations': 7}, self.call_tool))
        self.assertEqual(result['provider'], 'vtuber-persona')
        self.assertEqual(result['model'], persona.model_name)
        self.assertEqual(len(result['results']), 20)
        self.assertTrue(all(status == 200 for status, _ in result['results']))
        self.assertEqual({name for name, _ in self.calls}, TOOLS)
        self.assertEqual(len(persona.calls), 7)
        final_history = json.loads(persona.calls[-1][1])['messages']
        self.assertEqual(len(final_history), 13)
        self.assertEqual(final_history[-1]['role'], 'tool')
        self.assertIn('真实工具回执', json.loads(final_history[-1]['content'][0]['text'])['result']['content'])

    async def test_missing_token_and_browser_origin_cannot_call_model_or_consume_step(self):
        persona = FakePersona()
        result = json.loads(await self.runner(persona, max_steps=1).run({'mode': 'unauthorized'}, self.call_tool))
        self.assertEqual([status for status, _ in result['results']], [403, 403, 403, 403, 200, 200])
        self.assertEqual(len(persona.calls), 1)
        self.assertEqual(self.calls, [])

    async def test_model_without_a_step_is_rejected_before_persona_call(self):
        persona = FakePersona()
        with self.assertRaises(HarnessError) as raised:
            await self.runner(persona).run({'mode': 'no_step'}, self.call_tool)
        self.assertEqual(raised.exception.code, 'step_budget')
        self.assertEqual(persona.calls, [])

    async def test_duplicate_model_request_cannot_reuse_one_step(self):
        persona = FakePersona()
        with self.assertRaises(HarnessError) as raised:
            await self.runner(persona, max_steps=2).run({'mode': 'duplicate_model'}, self.call_tool)
        self.assertEqual(raised.exception.code, 'step_budget')
        self.assertEqual(len(persona.calls), 1)

    async def test_invalid_model_decision_never_reaches_host_tools(self):
        persona = FakePersona([{'type': 'tool', 'name': 'publish', 'arguments': {'text': 'no'}}])
        with self.assertRaises(HarnessError) as raised:
            await self.runner(persona).run({}, self.call_tool)
        self.assertEqual(raised.exception.code, 'invalid_tool')
        self.assertEqual(self.calls, [])

    async def test_cancel_waiting_model_cancels_persona_callback_and_carrier(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()
        persona = FakePersona()
        async def hanging(system, user):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        persona.activity_completion = hanging
        task = asyncio.create_task(self.runner(persona).run({}, self.call_tool))
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 6)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.calls, [])

    async def test_model_failure_is_sanitized_through_process_bridge(self):
        persona = FakePersona()
        persona.failure = RuntimeError('test-model-secret')
        with self.assertRaises(HarnessError) as raised:
            await self.runner(persona).run({}, self.call_tool)
        self.assertEqual(raised.exception.code, 'persona_failed')
        self.assertNotIn('test-model-secret', str(raised.exception))
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
