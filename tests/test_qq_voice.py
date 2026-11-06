import asyncio
import io
import tempfile
import unittest
import wave
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from brain.config import TTSConfig
from brain.qq_voice import QQVoiceError, QQVoiceRenderer
from brain.tts import index_voice_profile


def pcm_wav(*, seconds=.1, rate=16000):
    output = io.BytesIO()
    with wave.open(output, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b'\x00\x10' * int(seconds * rate))
    return output.getvalue()


class QQVoiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.public = self.root / 'audio'
        self.public.mkdir()
        self.cfg = TTSConfig(backend='indextts2', out_dir=str(self.public),
                             indextts2_ref_audio_path='configured-reference.wav',
                             indextts2_short_ref_audio_path='configured-short-reference.wav',
                             indextts2_delivery_preset='expressive')
        self.voices = []
        self.renderers = []

    async def asyncTearDown(self):
        for renderer in self.renderers:
            await renderer.close()
        self.temp.cleanup()

    def renderer(self, **kwargs):
        renderer = QQVoiceRenderer(self.root, self.cfg, **kwargs)
        self.renderers.append(renderer)
        return renderer

    def factory(self, payload=None, action=None):
        payload = pcm_wav() if payload is None else payload
        voices = self.voices

        class Voice:
            def __init__(self, cfg):
                self.cfg = cfg
                self.path = Path(cfg.out_dir) / 'voice.wav'
                self.calls = []
                self.close = AsyncMock()
                voices.append(self)

            async def speak(self, text, emotion):
                async with self._speak_lock:
                    self.calls.append((text, emotion))
                    if action:
                        return await action(self)
                    self.path.write_bytes(payload)
                    return str(self.path)

        return Voice

    def assert_no_private_files(self):
        self.assertEqual(list(self.public.iterdir()), [])
        folder = self.root / 'data/qq/voice-temp'
        if folder.exists():
            self.assertEqual(list(folder.iterdir()), [])

    async def test_uses_same_voice_config_but_only_private_temporary_files(self):
        renderer = self.renderer()
        with patch('brain.qq_voice.make_tts', self.factory()):
            result = await renderer.render('你好，欢迎回来。', '开心')
        self.assertEqual(result, pcm_wav())
        voice = self.voices[0]
        self.assertEqual(replace(voice.cfg, out_dir=self.cfg.out_dir),
                         replace(self.cfg, indextts2_num_beams=1, indextts2_long_num_beams=1))
        self.assertTrue(voice.path.is_relative_to(self.root / 'data/qq/voice-temp'))
        self.assertEqual(voice.calls, [('你好，欢迎回来。', '开心')])
        voice.close.assert_awaited_once()
        self.assert_no_private_files()

    async def test_loading_backend_skips_synthesis_and_recovers_without_mode_change(self):
        ready = [False]
        renderer = self.renderer(backend_ready=lambda: ready[0])
        with patch('brain.qq_voice.make_tts', self.factory()):
            self.assertFalse(renderer.available)
            with self.assertRaises(QQVoiceError):
                await renderer.render('你好。')
            self.assertEqual(self.voices, [])
            ready[0] = True
            self.assertTrue(renderer.available)
            self.assertEqual(await renderer.render('你好。'), pcm_wav())
        self.assertEqual(renderer.cfg.backend, 'indextts2')
        self.assert_no_private_files()

    async def test_single_beam_qq_profile_preserves_main_config_and_reference_choice(self):
        self.cfg.indextts2_num_beams = 2
        self.cfg.indextts2_long_num_beams = 3
        original = replace(self.cfg)
        renderer = self.renderer()
        long_text = '愿你今天一切顺利，睡个好觉，明天也有好心情。' * 3
        with patch('brain.qq_voice.make_tts', self.factory()):
            await renderer.render(long_text)
        self.assertEqual(self.cfg, original)
        voice_cfg = self.voices[0].cfg
        self.assertEqual(index_voice_profile(voice_cfg, long_text),
                         (original.indextts2_ref_audio_path, 1))
        self.assertEqual(index_voice_profile(voice_cfg, '你好'),
                         (original.indextts2_short_ref_audio_path, 1))
        self.assertEqual(replace(voice_cfg, out_dir=original.out_dir,
                                 indextts2_num_beams=original.indextts2_num_beams,
                                 indextts2_long_num_beams=original.indextts2_long_num_beams), original)
        self.assertEqual(renderer.timeout_sec, 60)

    def test_other_backends_keep_config_unchanged(self):
        for backend in ('gpt_sovits', 'fish_speech'):
            self.cfg.backend = backend
            renderer = self.renderer()
            self.assertEqual(renderer.cfg, self.cfg)
            self.assertIsNot(renderer.cfg, self.cfg)

    async def test_mock_and_unknown_backends_are_unavailable_without_printing_input(self):
        for backend in ('mock', 'unknown'):
            self.cfg.backend = backend
            renderer = self.renderer()
            self.assertFalse(renderer.available)
            with patch('brain.qq_voice.make_tts') as factory, patch('builtins.print') as output:
                with self.assertRaises(QQVoiceError) as caught:
                    await renderer.render('私聊不会打印到控制台')
                self.assertEqual(caught.exception.code, 'voice_unavailable')
            factory.assert_not_called()
            output.assert_not_called()

    async def test_rejects_empty_or_long_input_without_truncating(self):
        renderer = self.renderer()
        with patch('brain.qq_voice.make_tts') as factory:
            for text, reason in (('', 'voice_invalid_text'), ('   ', 'voice_invalid_text'),
                                 (None, 'voice_invalid_text'), ('啊' * 301, 'voice_text_too_long')):
                with self.assertRaises(QQVoiceError) as caught:
                    await renderer.render(text)
                self.assertEqual(caught.exception.code, reason)
            factory.assert_not_called()
        with patch('brain.qq_voice.make_tts', self.factory()):
            await renderer.render('啊' * 300)
        self.assertEqual(self.voices[-1].calls[0][0], '啊' * 300)

    async def test_invalid_audio_and_duration_fail_without_leaving_files(self):
        renderer = self.renderer()
        for audio in (b'not audio', pcm_wav()[:-2], pcm_wav(seconds=0),
                      pcm_wav(seconds=60.001), b'x' * 3_500_001):
            with self.subTest(size=len(audio)):
                with patch('brain.qq_voice.make_tts', self.factory(audio)):
                    with self.assertRaises(QQVoiceError) as caught:
                        await renderer.render('你好')
                    self.assertEqual(caught.exception.code, 'voice_invalid_audio')
                self.assert_no_private_files()
                self.voices[-1].close.assert_awaited_once()
        with patch('brain.qq_voice.make_tts', self.factory(pcm_wav(seconds=60))):
            self.assertEqual(await renderer.render('你好'), pcm_wav(seconds=60))

    async def test_outside_output_is_neither_read_nor_deleted(self):
        outside = self.public / 'existing.wav'
        outside.write_bytes(pcm_wav())

        async def output(voice):
            return str(outside)

        with patch('brain.qq_voice.make_tts', self.factory(action=output)):
            with self.assertRaises(QQVoiceError):
                await self.renderer().render('你好')
        self.assertTrue(outside.exists())
        self.assertEqual(list((self.root / 'data/qq/voice-temp').iterdir()), [])

    async def test_rejects_private_directory_inside_public_audio_root(self):
        self.cfg.out_dir = str(self.root / 'data')
        with patch('brain.qq_voice.make_tts') as factory:
            with self.assertRaises(QQVoiceError):
                await self.renderer().render('你好')
        factory.assert_not_called()
        self.assertFalse((self.root / 'data').exists())

    async def test_safe_error_hides_provider_text_and_closes_backend(self):
        async def failure(voice):
            voice.path.write_bytes(b'partial')
            raise RuntimeError('private message or provider token')

        with patch('brain.qq_voice.make_tts', self.factory(action=failure)):
            with self.assertRaises(QQVoiceError) as caught:
                await self.renderer().render('不应回显的私聊')
        self.assertNotIn('private', str(caught.exception))
        self.assertEqual(caught.exception.code, 'voice_failed')
        self.voices[0].close.assert_awaited_once()
        self.assert_no_private_files()

    async def test_timeout_waits_for_backend_cancellation_and_removes_partial_files(self):
        cancelled = asyncio.Event()

        async def blocked(voice):
            voice.path.write_bytes(b'partial')
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        renderer = self.renderer(timeout_sec=.03)
        with patch('brain.qq_voice.make_tts', self.factory(action=blocked)):
            with self.assertRaises(QQVoiceError) as caught:
                await renderer.render('你好')
        self.assertEqual(caught.exception.code, 'voice_timeout')
        self.assertTrue(cancelled.is_set())
        self.voices[0].close.assert_awaited_once()
        self.assertFalse(renderer._tasks)
        self.assert_no_private_files()

    async def test_timeout_includes_shared_synthesis_lock_wait(self):
        lock = asyncio.Lock()
        await lock.acquire()
        renderer = self.renderer(timeout_sec=.03, synthesis_lock=lock)
        try:
            with patch('brain.qq_voice.make_tts', self.factory()):
                with self.assertRaises(QQVoiceError) as caught:
                    await renderer.render('你好')
            self.assertEqual(caught.exception.code, 'voice_timeout')
            self.assertEqual(self.voices[0].calls, [])
            self.assertTrue(lock.locked())
            self.assert_no_private_files()
        finally:
            lock.release()

    async def test_resource_grant_is_checked_again_after_shared_lock(self):
        lock = asyncio.Lock()
        await lock.acquire()
        granted = True
        renderer = self.renderer(synthesis_lock=lock, resource_available=lambda: granted)
        with patch('brain.qq_voice.make_tts', self.factory()):
            task = asyncio.create_task(renderer.render('你好'))
            try:
                while not self.voices:
                    await asyncio.sleep(.001)
                granted = False
                lock.release()
                await asyncio.sleep(.02)
                self.assertEqual(self.voices[0].calls, [])
                self.assertTrue(renderer.available)
                granted = True
                await asyncio.wait_for(task, 1)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.voices[0].calls, [('你好', '平静')])
        self.assertFalse(lock.locked())

    async def test_concurrent_private_requests_are_serialized_and_use_separate_files(self):
        active, peak = 0, 0

        async def output(voice):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(.01)
            voice.path.write_bytes(pcm_wav())
            active -= 1
            return str(voice.path)

        renderer = self.renderer()
        with patch('brain.qq_voice.make_tts', self.factory(action=output)):
            replies = await asyncio.gather(renderer.render('第一句'), renderer.render('第二句'))
        self.assertEqual(replies, [pcm_wav(), pcm_wav()])
        self.assertEqual(peak, 1)
        self.assertNotEqual(self.voices[0].path, self.voices[1].path)
        self.assert_no_private_files()

    async def test_close_cancels_pending_requests_and_disables_renderer(self):
        started = asyncio.Event()

        async def blocked(voice):
            voice.path.write_bytes(b'partial')
            started.set()
            await asyncio.Event().wait()

        renderer = self.renderer()
        with patch('brain.qq_voice.make_tts', self.factory(action=blocked)):
            task = asyncio.create_task(renderer.render('你好'))
            await asyncio.wait_for(started.wait(), 1)
            await renderer.close()
            results = await asyncio.gather(task, return_exceptions=True)
        self.assertIsInstance(results[0], asyncio.CancelledError)
        self.assertFalse(renderer.available)
        self.voices[0].close.assert_awaited_once()
        self.assert_no_private_files()
        with self.assertRaises(QQVoiceError) as caught:
            await renderer.render('下一句')
        self.assertEqual(caught.exception.code, 'voice_unavailable')

    async def test_caller_cancellation_releases_shared_lock_and_cleans_files(self):
        started = asyncio.Event()
        lock = asyncio.Lock()

        async def blocked(voice):
            voice.path.write_bytes(b'partial')
            started.set()
            await asyncio.Event().wait()

        renderer = self.renderer(synthesis_lock=lock)
        with patch('brain.qq_voice.make_tts', self.factory(action=blocked)):
            task = asyncio.create_task(renderer.render('你好'))
            await asyncio.wait_for(started.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(lock.locked())
        self.voices[0].close.assert_awaited_once()
        self.assert_no_private_files()

    async def test_unavailable_resources_timeout_before_backend_creation(self):
        renderer = self.renderer(timeout_sec=.02, resource_available=lambda: False)
        with patch('brain.qq_voice.make_tts') as factory:
            with self.assertRaises(QQVoiceError) as caught:
                await renderer.render('你好')
        self.assertEqual(caught.exception.code, 'voice_timeout')
        self.assertTrue(renderer.available)
        factory.assert_not_called()
        self.assert_no_private_files()

    async def test_provider_failures_have_fixed_reasons_without_private_details(self):
        request = httpx.Request('POST', 'http://localhost/tts?private-token')
        cases = [(httpx.ReadTimeout('private text', request=request), 'voice_timeout'),
                 (httpx.ConnectError('private text', request=request), 'voice_unavailable')]
        for status, reason in ((408, 'voice_timeout'), (504, 'voice_timeout'),
                               (429, 'voice_unavailable'), (502, 'voice_unavailable'),
                               (503, 'voice_unavailable'), (409, 'voice_failed'),
                               (500, 'voice_failed')):
            response = httpx.Response(status, request=request, text='private text')
            cases.append((httpx.HTTPStatusError('private text', request=request, response=response), reason))
        for failure, reason in cases:
            with self.subTest(reason=reason, type=type(failure).__name__):
                async def fail(voice):
                    raise failure

                with patch('brain.qq_voice.make_tts', self.factory(action=fail)):
                    with self.assertRaises(QQVoiceError) as caught:
                        await self.renderer().render('不应回显的私聊')
                self.assertEqual(caught.exception.code, reason)
                self.assertNotIn('private', str(caught.exception))
                self.assertIsNone(caught.exception.__cause__)
                self.assert_no_private_files()

    async def test_missing_or_empty_backend_output_is_invalid_audio(self):
        for result in (None, '', self.root / 'missing.wav'):
            async def output(voice):
                return result

            with patch('brain.qq_voice.make_tts', self.factory(action=output)):
                with self.assertRaises(QQVoiceError) as caught:
                    await self.renderer().render('你好')
            self.assertEqual(caught.exception.code, 'voice_invalid_audio')
            self.assert_no_private_files()

    def test_reason_code_rejects_arbitrary_provider_data(self):
        for value in ('private provider message', None, [], {'code': 'private'}):
            error = QQVoiceError(value)
            self.assertEqual(error.code, 'voice_failed')
            self.assertNotIn('private', str(error))


if __name__ == '__main__':
    unittest.main()
