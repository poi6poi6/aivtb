"""Health transitions use local fakes; no synthesis, account or model calls."""
import asyncio
import json
import unittest

import httpx

from brain.config import TTSConfig
from brain.tts_readiness import TTSReadiness


class TTSReadinessTests(unittest.IsolatedAsyncioTestCase):
    def make(self, backend='indextts2', **kwargs):
        state = TTSReadiness(TTSConfig(backend=backend), env=kwargs.pop('env', {}), **kwargs)
        self.addAsyncCleanup(state.close)
        return state

    async def test_mock_needs_no_server_or_background_worker(self):
        async def forbidden():
            self.fail('mock must not probe')
        state = self.make('mock', probe=forbidden, env={
            'AI_VTUBER_TTS_BACKEND': 'mock', 'AI_VTUBER_TTS_STARTUP_STATE': 'unavailable'})
        self.assertTrue(state.snapshot()['ready'])
        state.start()
        self.assertIsNone(state._task)
        self.assertTrue((await state.check())['ready'])

    async def test_start_does_not_wait_for_a_slow_model_load(self):
        pending = asyncio.Event()
        entered = asyncio.Event()
        async def probe():
            entered.set()
            await pending.wait()
            return True
        state = self.make(probe=probe)
        state.start()
        await asyncio.wait_for(entered.wait(), .5)
        self.assertEqual(state.snapshot()['state'], 'checking')
        pending.set()
        for _ in range(20):
            await asyncio.sleep(.001)
            if state.snapshot()['ready']:
                break
        self.assertTrue(state.snapshot()['ready'])

    async def test_loading_is_bounded_and_failed_service_can_recover(self):
        now, available = [10], [False]
        async def probe():
            return available[0]
        state = self.make(probe=probe, clock=lambda: now[0], startup_timeout=4,
                          env={'AI_VTUBER_TTS_BACKEND': 'indextts2',
                               'AI_VTUBER_TTS_STARTUP_STATE': 'loading'})
        self.assertEqual((await state.check())['state'], 'loading')
        now[0] += 5
        self.assertEqual((await state.check())['state'], 'unavailable')
        available[0] = True
        self.assertTrue((await state.check())['ready'])
        available[0] = False
        self.assertEqual((await state.check())['state'], 'unavailable')

    async def test_dead_or_reused_owned_process_stops_loading_immediately(self):
        async def offline():
            return False
        for identity in (None, 'win:replacement'):
            state = self.make(probe=offline, identity=lambda pid: identity,
                              env={'AI_VTUBER_TTS_BACKEND': 'indextts2',
                                   'AI_VTUBER_TTS_STARTUP_STATE': 'loading',
                                   'AI_VTUBER_TTS_OWNED_PID': '123',
                                   'AI_VTUBER_TTS_OWNED_IDENTITY': 'win:original'})
            self.assertEqual((await state.check())['state'], 'unavailable')

    async def test_probe_failure_does_not_expose_response_or_credentials(self):
        async def broken():
            raise ValueError('secret server URL and credentials')
        state = self.make(probe=broken)
        snapshot = await state.check()
        self.assertFalse(snapshot['ready'])
        self.assertNotIn('secret', json.dumps(snapshot))
        self.assertIn('文字功能可以使用', snapshot['detail'])

    async def test_close_cancels_the_pending_probe_and_prevents_restart(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()
        async def probe():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        state = self.make(probe=probe)
        state.start()
        await entered.wait()
        await state.close()
        self.assertTrue(cancelled.is_set())
        self.assertEqual(state.snapshot()['state'], 'stopped')
        state.start()
        self.assertIsNone(state._task)

    async def set_response(self, state, handler):
        state._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)

    async def test_index_health_requires_identified_backend_and_success(self):
        for code, data, expected in [
            (200, {'status': 'ok', 'backend': 'indextts2'}, True),
            (200, {'status': 'ok'}, False),
            (200, {'status': 'loading', 'backend': 'indextts2'}, False),
            (404, {'status': 'ok', 'backend': 'indextts2'}, False),
            (200, [], False),
        ]:
            state = self.make()
            def handler(request):
                self.assertEqual(request.method, 'GET')
                self.assertEqual(request.url.path, '/health')
                return httpx.Response(code, json=data)
            await self.set_response(state, handler)
            self.assertEqual((await state.check())['ready'], expected)

    async def test_fish_health_uses_its_configured_auth_without_returning_it(self):
        state = self.make('fish_speech', env={'FISH_SPEECH_API_KEY': 'fake-key-for-offline-test'})
        def handler(request):
            self.assertEqual(request.url.path, '/v1/health')
            self.assertEqual(request.headers['Authorization'], 'Bearer fake-key-for-offline-test')
            return httpx.Response(200, json={'status': 'ok'})
        await self.set_response(state, handler)
        snapshot = await state.check()
        self.assertTrue(snapshot['ready'])
        self.assertNotIn('fake-key', json.dumps(snapshot))

    async def test_gpt_sovits_checks_the_tts_service_schema_without_synthesizing(self):
        state = self.make('gpt_sovits')
        def handler(request):
            self.assertEqual(request.method, 'GET')
            self.assertEqual(request.url.path, '/openapi.json')
            return httpx.Response(200, json={'paths': {'/tts': {'post': {}}}})
        await self.set_response(state, handler)
        self.assertTrue((await state.check())['ready'])

    async def test_redirect_html_oversized_response_and_invalid_url_fail_closed(self):
        for response in (httpx.Response(302, headers={'Location': 'https://unexpected.test/'}),
                         httpx.Response(200, text='<html>login</html>'),
                         httpx.Response(200, content=b' ' * 131073)):
            state = self.make()
            calls = []
            def handler(request):
                calls.append(request)
                return response
            await self.set_response(state, handler)
            self.assertFalse((await state.check())['ready'])
            self.assertEqual(len(calls), 1)
        state = self.make()
        state.cfg.indextts2_url = 'http://name:password@127.0.0.1:9890'
        await self.set_response(state, lambda request: self.fail('invalid URL must not be requested'))
        self.assertFalse((await state.check())['ready'])


if __name__ == '__main__':
    unittest.main()
