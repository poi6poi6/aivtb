import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from brain.astra_vision import AstraVision, AstraVisionConfig, MODEL, PROVIDER
from brain.codex_runtime import CodexNotFoundError
from brain.image_understanding import ImageUnderstandingError


class FakeRPC:
    def __init__(self, *, account='chatgpt', status='completed', output=None, stall=False):
        self.account, self.status, self.stall = account, status, stall
        self.output = output or json.dumps({'description': '绿色的圆形角色。', 'visible_text': '', 'uncertainties': ''})
        self.events = asyncio.Queue()
        self.calls, self.sent = [], []
        self.closed = False
        self.path = None

    async def call(self, method, params=None, **kwargs):
        self.calls.append((method, params))
        if method == 'account/read':
            return {'account': {'type': self.account}}
        if method == 'config/read':
            return {'config': {'mcp_servers': {'private.files': {}}, 'plugins': {'some@plugin': {}}}}
        if method == 'thread/start':
            return {'thread': {'id': 'thread1'}, 'model': MODEL}
        if method == 'turn/start':
            self.path = Path(params['input'][1]['path'])
            assert self.path.is_file()
            if not self.stall:
                await self.events.put({'method': 'item/completed', 'params': {
                    'threadId': 'thread1', 'turnId': 'turn1',
                    'item': {'type': 'agentMessage', 'phase': 'analysis', 'text': 'hidden analysis'}}})
                await self.events.put({'method': 'item/completed', 'params': {
                    'threadId': 'thread1', 'turnId': 'turn1',
                    'item': {'type': 'agentMessage', 'phase': 'final_answer', 'text': self.output}}})
                await self.events.put({'method': 'turn/completed', 'params': {
                    'threadId': 'thread1', 'turn': {'id': 'turn1', 'status': self.status}}})
            return {'turn': {'id': 'turn1'}}
        return {}

    async def send(self, message):
        self.sent.append(message)

    async def close(self):
        self.closed = True


class AstraVisionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.image = next((Path(__file__).parents[1] / 'brain/static/memes/basic').glob('*.png')).read_bytes()
        self.vision = AstraVision()
        self.addAsyncCleanup(self.vision.close)
        resolver = patch('brain.astra_vision.resolve_codex_command', return_value='fixture/codex.exe')
        self.resolver = resolver.start()
        self.addCleanup(resolver.stop)

    async def test_snapshot_uses_shared_discovery_and_recovers_after_desktop_update(self):
        self.resolver.return_value = None
        self.assertFalse(self.vision.available)
        missing = self.vision.snapshot()
        self.assertEqual(missing['state'], 'not_configured')
        self.assertFalse(missing['ready'])
        self.vision._state = 'not_configured'
        self.resolver.return_value = 'fixture/new-desktop-version/codex.exe'
        available = self.vision.snapshot()
        self.assertEqual(available['state'], 'configured')
        self.assertTrue(available['ready'])
        self.assertFalse(available['verified'], 'discovery alone does not verify model access')
        self.assertNotIn('fixture', str(available), 'snapshot must not disclose executable paths')
        self.resolver.assert_called_with('codex')

    async def test_missing_client_error_is_not_misreported_as_connection_failure(self):
        self.resolver.return_value = None
        with patch('brain.astra_vision.codex_client', side_effect=CodexNotFoundError('fixture-private-path')) as start:
            with self.assertRaises(ImageUnderstandingError) as error:
                await self.vision.describe(self.image)
        self.assertEqual(error.exception.code, 'codex_not_found')
        self.assertIn('Codex', str(error.exception))
        self.assertNotIn('独立视觉模型', str(error.exception))
        self.assertNotIn('fixture-private-path', str(error.exception))
        self.assertEqual(self.vision.snapshot()['state'], 'not_configured')
        folder = Path(start.call_args.args[1])
        self.assertFalse(folder.exists(), 'temporary image must be removed when the CLI is absent')

    async def test_subscription_local_image_final_only_and_cleanup(self):
        rpc = FakeRPC()
        with patch('brain.astra_vision.codex_client', return_value=rpc) as start:
            result = await self.vision.describe(self.image, '这是什么？', 'image/png')
        self.assertEqual(result.description, '绿色的圆形角色。')
        self.assertNotIn('hidden', result.context())
        self.assertTrue(rpc.closed)
        self.assertFalse(rpc.path.exists())
        self.assertFalse(start.call_args.kwargs['config_overrides']['features.shell_tool'])
        self.assertFalse(start.call_args.kwargs['config_overrides'][f'model_providers.{PROVIDER}.supports_websockets'])
        calls = dict(rpc.calls)
        self.assertEqual(calls['thread/start']['model'], MODEL)
        self.assertEqual(calls['thread/start']['modelProvider'], PROVIDER)
        self.assertTrue(calls['thread/start']['ephemeral'])
        self.assertEqual(calls['thread/start']['sandbox'], 'read-only')
        self.assertFalse(calls['thread/start']['config']['mcp_servers']['private.files']['enabled'])
        self.assertEqual(calls['turn/start']['input'][1]['type'], 'localImage')
        self.assertTrue(self.vision.snapshot()['verified'])

    async def test_api_auth_rejected_without_turn(self):
        rpc = FakeRPC(account='apiKey')
        with patch('brain.astra_vision.codex_client', return_value=rpc):
            with self.assertRaises(ImageUnderstandingError) as error:
                await self.vision.describe(self.image)
        self.assertEqual(error.exception.code, 'model_access')
        self.assertNotIn('turn/start', dict(rpc.calls))
        self.assertTrue(rpc.closed)

    async def test_windows_directory_lock_does_not_retain_image_or_hide_result(self):
        rpc = FakeRPC()
        with patch('brain.astra_vision.codex_client', return_value=rpc), patch.object(
                Path, 'rmdir', side_effect=PermissionError('directory still held by Windows')):
            result = await self.vision.describe(self.image)
        self.assertTrue(result.description)
        self.assertFalse(rpc.path.exists())
        # Only an empty directory remains; no recursive retry or lost result.
        self.assertEqual(list(rpc.path.parent.iterdir()), [])
        rpc.path.parent.rmdir()

    async def test_failed_turn_cannot_be_observation(self):
        rpc = FakeRPC(status='failed')
        with patch('brain.astra_vision.codex_client', return_value=rpc):
            with self.assertRaises(ImageUnderstandingError):
                await self.vision.describe(self.image)
        self.assertFalse(rpc.path.exists())

    async def test_malformed_structured_result_rejected(self):
        rpc = FakeRPC(output='Thinking Process: pretend success')
        with patch('brain.astra_vision.codex_client', return_value=rpc):
            with self.assertRaises(ImageUnderstandingError):
                await self.vision.describe(self.image)

    async def test_invalid_image_and_disabled_make_no_model_call(self):
        with patch('brain.astra_vision.codex_client') as start:
            with self.assertRaises(ImageUnderstandingError):
                await self.vision.describe(b'not an image')
            disabled = AstraVision(AstraVisionConfig(enabled=False))
            with self.assertRaises(ImageUnderstandingError) as error:
                await disabled.describe(self.image)
            self.assertEqual(error.exception.code, 'disabled')
            start.assert_not_called()
            await disabled.close()

    async def test_close_cancels_pending_request_and_removes_image(self):
        rpc = FakeRPC(stall=True)
        with patch('brain.astra_vision.codex_client', return_value=rpc):
            task = asyncio.create_task(self.vision.describe(self.image))
            while rpc.path is None:
                await asyncio.sleep(0)
            await self.vision.close()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(rpc.closed)
        self.assertFalse(rpc.path.exists())
        self.assertEqual(self.vision.snapshot()['state'], 'closed')

    async def test_tool_request_fails_closed(self):
        rpc = FakeRPC(stall=True)
        await rpc.events.put({'id': 'tool1', 'method': 'item/commandExecution/requestApproval',
                              'params': {'threadId': 'thread1'}})
        with patch('brain.astra_vision.codex_client', return_value=rpc):
            with self.assertRaises(ImageUnderstandingError):
                await self.vision.describe(self.image)
        self.assertIn('error', rpc.sent[0])
        self.assertFalse(rpc.path.exists())

    async def test_pending_images_bounded(self):
        rpc = FakeRPC(stall=True)
        with patch('brain.astra_vision.codex_client', return_value=rpc):
            tasks = [asyncio.create_task(self.vision.describe(self.image)) for _ in range(2)]
            while self.vision._pending < 2:
                await asyncio.sleep(0)
            with self.assertRaises(ImageUnderstandingError) as error:
                await self.vision.describe(self.image)
            self.assertEqual(error.exception.code, 'model_busy')
            await self.vision.close()
            await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == '__main__':
    unittest.main()
