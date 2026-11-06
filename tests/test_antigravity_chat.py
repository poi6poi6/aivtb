from __future__ import annotations

import asyncio
from contextlib import aclosing
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from brain.antigravity_chat import AntigravityChat, AntigravityChatError, _subscription_env


_PRELUDE = '''
import json, sys, time
def send(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)
def init(tools=None):
    send({'event':'init','init':{'tools': [] if tools is None else tools}})
def delta(text):
    send({'event':'step_update','step_update':{'step_type':'agent_response','state':'ACTIVE','text_delta':text}})
def result(text, status='SUCCESS', error=''):
    send({'event':'result','result':{'status':status,'response':text,'error':error}})
'''


class AntigravityChatTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.settings = self.root / 'settings.json'
        self.settings.write_text('{"useG1Credits":false}', encoding='utf-8')
        self.config_patch = patch('brain.antigravity_chat._settings_path', return_value=self.settings)
        self.config_patch.start()
        self.chat = AntigravityChat(sys.executable, 'gemini-4-argon-low',
                                    timeout_sec=2, runtime_dir=self.root / 'runs')
        self.real_spawn = asyncio.create_subprocess_exec
        self.processes = []
        self.invocations = []
        self.profiles = []
        self.workspace_paths = []

    async def asyncTearDown(self):
        await self.chat.close()
        self.config_patch.stop()
        for process in self.processes:
            if process.returncode is None:
                process.kill()
                await process.wait()
        self.temporary.cleanup()

    def fake_cli(self, script):
        async def spawn(*args, **kwargs):
            if Path(args[0]).name.lower() == 'taskkill.exe':
                return await self.real_spawn(*args, **kwargs)
            self.invocations.append((args, kwargs))
            workspace = Path(kwargs['cwd'])
            self.workspace_paths.append(workspace)
            self.profiles.append((workspace / '.agents/agents/avatar.md').read_text(encoding='utf-8'))
            process = await self.real_spawn(sys.executable, '-u', '-X', 'utf8', '-c',
                                            _PRELUDE + '\n' + script, **kwargs)
            self.processes.append(process)
            return process
        return patch('brain.antigravity_chat.asyncio.create_subprocess_exec', side_effect=spawn)

    async def collect(self, script, *, system='角色提示', user='你好'):
        with self.fake_cli(script):
            return ''.join([piece async for piece in self.chat.stream(system, user)])

    def assert_cleaned(self):
        self.assertTrue(all(p.returncode is not None for p in self.processes))
        self.assertTrue(all(not p.exists() for p in self.workspace_paths))
        self.assertEqual(list((self.root / 'runs').iterdir()), [])

    async def test_stream_has_isolated_system_prompt_and_only_assistant_text(self):
        text = await self.collect('''
init()
request = json.loads(sys.stdin.readline())
assert request == {'event':'user','message':{'content':'你好; $(not-a-command)'}}
send({'event':'step_update','step_update':{'step_type':'thinking','text_delta':'private reasoning'}})
delta('[平静]你好')
send({'event':'step_update','step_update':{'step_type':'agent_response','state':'DONE','text_delta':'呀。'}})
result('[平静]你好呀。')
''', system='你是助手。\n不调用工具。', user='你好; $(not-a-command)')
        self.assertEqual(text, '[平静]你好呀。')
        profile = self.profiles[0]
        for field in ('mainAgent: true', 'subagent: false', 'tools: []', 'mcpServers: []',
                      'skills: []', 'plugins: []', 'commandExecutionPolicy: "off"'):
            self.assertIn(field, profile)
        self.assertTrue(profile.endswith('你是助手。\n不调用工具。\n'))
        args, kwargs = self.invocations[0]
        self.assertNotIn('你好; $(not-a-command)', args)
        self.assertNotIn('shell', kwargs)
        self.assertIn('--disable-slash-commands', args)
        self.assertEqual(args[args.index('--input-format') + 1], 'stream-json')
        self.assertEqual(args[args.index('--model') + 1], 'gemini-4-argon-low')
        if os.name == 'nt':
            self.assertNotEqual(kwargs['creationflags'], 0)
        self.assert_cleaned()

    async def test_no_user_input_sent_until_empty_tools_init_is_confirmed(self):
        marker = self.root / 'leaked.txt'
        script = f'''
init(['run_command'])
request = sys.stdin.readline()
if request:
    open({str(marker)!r}, 'w').write(request)
time.sleep(10)
'''
        with self.assertRaisesRegex(AntigravityChatError, '零工具'):
            await self.collect(script, user='PRIVATE INPUT')
        self.assertFalse(marker.exists())
        self.assert_cleaned()

    async def test_missing_tools_is_rejected(self):
        with self.assertRaisesRegex(AntigravityChatError, '零工具'):
            await self.collect("send({'event':'init','init':{}}); time.sleep(10)")
        self.assert_cleaned()

    async def test_each_turn_uses_new_workspace_and_no_resume(self):
        for _ in range(2):
            self.assertEqual(await self.collect("init(); sys.stdin.readline(); result('你好。')"), '你好。')
        self.assertNotEqual(*self.workspace_paths)
        self.assertTrue(all('--continue' not in args and '--conversation' not in args
                            for args, _ in self.invocations))
        self.assert_cleaned()

    async def test_final_response_can_supply_missing_tail_without_duplicate(self):
        self.assertEqual(await self.collect("init(); sys.stdin.readline(); delta('你好'); result('你好呀。')"),
                         '你好呀。')

    async def test_conflicting_final_response_fails(self):
        with self.assertRaisesRegex(AntigravityChatError, '不一致'):
            await self.collect("init(); sys.stdin.readline(); delta('第一版'); result('另一版')")
        self.assert_cleaned()

    async def test_error_and_stderr_do_not_expose_upstream_secrets(self):
        with self.assertRaisesRegex(AntigravityChatError, '额度') as caught:
            await self.collect("init(); sys.stdin.readline(); result('', 'ERROR', 'quota sk-secret-user@example.com')")
        self.assertNotIn('sk-secret', str(caught.exception))
        self.assertNotIn('user@', str(caught.exception))
        with self.assertRaisesRegex(AntigravityChatError, '登录') as caught:
            await self.collect("sys.stderr.write('authentication required secret=PRIVATE'); sys.stderr.flush(); sys.exit(1)")
        self.assertNotIn('PRIVATE', str(caught.exception))
        self.assert_cleaned()

    async def test_stderr_is_drained_without_deadlock(self):
        text = await self.collect("init(); sys.stdin.readline(); sys.stderr.write('x' * 200000); sys.stderr.flush(); result('好。')")
        self.assertEqual(text, '好。')
        self.assert_cleaned()

    async def test_nonzero_exit_after_result_still_fails(self):
        with self.assertRaises(AntigravityChatError):
            await self.collect("init(); sys.stdin.readline(); result('好。'); sys.exit(3)")
        self.assert_cleaned()

    async def test_timeout_kills_process_and_removes_persona_profile(self):
        self.chat.timeout_sec = .25
        with self.assertRaisesRegex(AntigravityChatError, '超时'):
            await self.collect("init(); sys.stdin.readline(); time.sleep(10)")
        self.assert_cleaned()

    async def test_cancellation_kills_owned_process(self):
        with self.fake_cli("init(); sys.stdin.readline(); delta('你好'); time.sleep(10)"):
            stream = self.chat.stream('system', 'user')
            self.assertEqual(await anext(stream), '你好')
            pending = asyncio.create_task(anext(stream))
            await asyncio.sleep(.02)
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
        self.assert_cleaned()

    async def test_early_break_with_persona_aclose_contract_cleans(self):
        with self.fake_cli("init(); sys.stdin.readline(); delta('第一句。'); time.sleep(10)"):
            async with aclosing(self.chat.stream('system', 'user')) as stream:
                async for text in stream:
                    self.assertEqual(text, '第一句。')
                    break
        self.assert_cleaned()

    async def test_close_terminates_active_stream(self):
        with self.fake_cli("init(); sys.stdin.readline(); delta('你好'); time.sleep(10)"):
            stream = self.chat.stream('system', 'user')
            await anext(stream)
            await self.chat.close()
            with self.assertRaisesRegex(AntigravityChatError, '取消'):
                await anext(stream)
        self.assertFalse(self.chat.available)
        self.assert_cleaned()

    async def test_unexpected_tool_event_fails_even_after_empty_init(self):
        with self.assertRaisesRegex(AntigravityChatError, '工具操作'):
            await self.collect("init(); sys.stdin.readline(); send({'event':'step_update','step_update':{'step_type':'tool_call'}}); time.sleep(10)")
        self.assert_cleaned()

    async def test_subscription_guards_fail_before_starting_process(self):
        for settings in ({}, {'useG1Credits': True}, {'useG1Credits': 0},
                         {'useG1Credits': False, 'modelProvider': 'custom-private'}):
            with self.subTest(settings=settings):
                self.settings.write_text(json.dumps(settings), encoding='utf-8')
                self.assertFalse(self.chat.available)
                with self.fake_cli("raise AssertionError('must not run')"):
                    with self.assertRaises(AntigravityChatError):
                        await anext(self.chat.stream('system', 'user'))
        self.assertEqual(self.processes, [])

    async def test_environment_cannot_enable_api_or_vertex_fallback(self):
        secret_env = {'GEMINI_API_KEY':'secret', 'GOOGLE_API_KEY':'secret',
                      'GOOGLE_GENAI_USE_VERTEXAI':'true', 'GOOGLE_APPLICATION_CREDENTIALS':'secret.json',
                      'AGY_ADC_AUTH':'true', 'GOOGLE_CLOUD_QUOTA_PROJECT':'project',
                      'CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE':'secret.json',
                      'CLOUDSDK_AUTH_ACCESS_TOKEN':'secret',
                      'ANTHROPIC_API_KEY':'secret', 'OPENAI_API_KEY':'secret', 'DEEPSEEK_API_KEY':'secret'}
        with patch.dict(os.environ, secret_env):
            env = _subscription_env()
            self.assertTrue(set(secret_env).isdisjoint(env))
            self.assertEqual(env['AGY_CLI_DISABLE_AUTO_UPDATE'], 'true')
        self.assertTrue(self.chat.available)

    async def test_invalid_protocol_and_missing_result_fail(self):
        for script in ("print('not json', flush=True)", "init(); sys.stdin.readline()",
                       "delta('premature')", "result('before-init')"):
            with self.subTest(script=script):
                with self.assertRaises(AntigravityChatError):
                    await self.collect(script)
        self.assert_cleaned()


if __name__ == '__main__':
    unittest.main()
