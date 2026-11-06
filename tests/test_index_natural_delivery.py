from __future__ import annotations

import asyncio
import io
import json
import struct
import tempfile
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import httpx

from brain.config import TTSConfig
from brain.tts import IndexTTS2TTS, emotion_strength


SHORT = '我在听，你慢慢说。'
LONG = '今天如果有点累，就先歇一会儿吧。你刚才说的那件事，我还想听听后面怎么样了。'


def pcm_wav() -> bytes:
    output = io.BytesIO()
    with wave.open(output, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b'\x00\x10' * 160)
    return output.getvalue()


class IndexNaturalDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.voices = []
        self.requests = []

    async def asyncTearDown(self):
        for voice in self.voices:
            await voice.close()
        self.folder.cleanup()

    def voice(self, handler=None, **options):
        options.setdefault('trim_edge_silence', False)
        cfg = TTSConfig(out_dir=self.folder.name,
                        indextts2_ref_audio_path='full.wav',
                        indextts2_short_ref_audio_path='later.wav', **options)
        voice = IndexTTS2TTS(cfg)
        self.voices.append(voice)

        async def transport(request):
            self.requests.append((request.url.path, json.loads(request.content)))
            if handler is not None:
                return await handler(request)
            return httpx.Response(200, content=pcm_wav())

        voice._client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
        return voice

    def payloads(self):
        return [body for path, body in self.requests if path == '/tts']

    async def test_consecutive_turn_keeps_reference_but_beams_follow_segment_length(self):
        voice = self.voice()
        first = await voice.speak_segment(SHORT, turn_id=1, segment_index=0)
        second = await voice.speak_segment(LONG, turn_id=1, segment_index=1)
        payloads = self.payloads()
        self.assertEqual([p['ref_audio_path'] for p in payloads], ['later.wav', 'later.wav'])
        self.assertEqual([p['num_beams'] for p in payloads], [1, 3])
        self.assertTrue(all(p['natural_delivery'] is True for p in payloads))
        self.assertEqual([p['text'] for p in payloads], [SHORT, LONG])
        self.assertEqual(Path(first).read_bytes(), pcm_wav())
        self.assertEqual(Path(second).read_bytes(), pcm_wav())

    async def test_new_turn_selects_its_own_reference_and_emotion(self):
        voice = self.voice()
        await voice.speak_segment(SHORT, '平静', turn_id=1, segment_index=0)
        await voice.speak_segment(LONG, '惊讶', turn_id=2, segment_index=0)
        self.assertEqual(self.payloads()[-1]['ref_audio_path'], 'full.wav')
        self.assertEqual(self.payloads()[-1]['emo_alpha'], emotion_strength('惊讶', LONG, .60))

    async def test_only_consecutive_segments_smooth_emotion(self):
        voice = self.voice()
        await voice.speak_segment(SHORT, '平静', turn_id=1, segment_index=0)
        await voice.speak_segment(LONG, '惊讶', turn_id=1, segment_index=1)
        first, second = self.payloads()
        self.assertAlmostEqual(second['emo_alpha'] - first['emo_alpha'], .12)
        await voice.speak_segment(SHORT, '平静', turn_id=1, segment_index=0)
        await voice.speak_segment(LONG, '惊讶', turn_id=1, segment_index=3)
        self.assertEqual(self.payloads()[-1]['ref_audio_path'], 'full.wav')
        self.assertEqual(self.payloads()[-1]['emo_alpha'], emotion_strength('惊讶', LONG, .60))

    async def test_gap_breaks_reference_and_alpha_continuity(self):
        voice = self.voice()
        await voice.speak_segment(SHORT, '平静', turn_id=1, segment_index=0)
        voice._last_synthesis = time.monotonic() - 31
        await voice.speak_segment(LONG, '惊讶', turn_id=1, segment_index=1)
        last = self.payloads()[-1]
        self.assertEqual(last['ref_audio_path'], 'full.wav')
        self.assertEqual(last['emo_alpha'], emotion_strength('惊讶', LONG, .60))

    async def test_standalone_preview_is_independent_and_breaks_prior_turn(self):
        voice = self.voice()
        await voice.speak_segment(SHORT, '平静', turn_id=1, segment_index=0)
        await voice.speak(LONG, '惊讶')
        await voice.speak_segment(LONG, '惊讶', turn_id=1, segment_index=1)
        self.assertEqual([p['ref_audio_path'] for p in self.payloads()],
                         ['later.wav', 'full.wav', 'full.wav'])
        self.assertEqual([p['emo_alpha'] for p in self.payloads()[1:]], [.60, .60])
        # A standalone synthesis cannot seed an otherwise new streaming turn.
        await voice.speak(SHORT, '平静')
        await voice.speak_segment(LONG, '惊讶', turn_id=1, segment_index=2)
        self.assertEqual(self.payloads()[-1]['ref_audio_path'], 'full.wav')
        self.assertEqual(self.payloads()[-1]['emo_alpha'], .60)

    async def test_failed_segment_does_not_commit_reference_or_emotion(self):
        count = 0

        async def handler(_request):
            nonlocal count
            count += 1
            return httpx.Response(503 if count == 2 else 200, content=pcm_wav())

        voice = self.voice(handler)
        await voice.speak_segment(SHORT, '平静', turn_id=1, segment_index=0)
        with self.assertRaises(httpx.HTTPStatusError):
            await voice.speak_segment(LONG, '平静', turn_id=1, segment_index=1)
        await voice.speak_segment(LONG, '惊讶', turn_id=1, segment_index=1)
        self.assertEqual([p['ref_audio_path'] for p in self.payloads()],
                         ['later.wav', 'later.wav', 'full.wav'])
        self.assertEqual(self.payloads()[-1]['emo_alpha'], .60)
        self.assertEqual(len(list(Path(self.folder.name).glob('*.wav'))), 2)

    async def test_cancel_calls_matching_request_id_and_breaks_continuity(self):
        entered = asyncio.Event()
        block = asyncio.Event()

        async def handler(request):
            if request.url.path == '/tts' and len(self.payloads()) == 2:
                entered.set()
                await block.wait()
            return httpx.Response(200, content=pcm_wav())

        voice = self.voice(handler)
        await voice.speak_segment(SHORT, '平静', turn_id=1, segment_index=0)
        pending = asyncio.create_task(voice.speak_segment(LONG, '平静', turn_id=1, segment_index=1))
        await asyncio.wait_for(entered.wait(), 1)
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        cancelled = [body for path, body in self.requests if path == '/cancel']
        self.assertEqual(cancelled, [{'request_id': self.payloads()[1]['request_id']}])
        await voice.speak_segment(LONG, '惊讶', turn_id=1, segment_index=1)
        self.assertEqual(self.payloads()[-1]['ref_audio_path'], 'full.wav')
        self.assertEqual(self.payloads()[-1]['emo_alpha'], .60)
        self.assertEqual(len(list(Path(self.folder.name).glob('*.wav'))), 2)

    async def test_file_write_failure_does_not_commit_successful_http_result(self):
        voice = self.voice()
        await voice.speak_segment(SHORT, '平静', turn_id=1, segment_index=0)
        with patch.object(Path, 'write_bytes', side_effect=OSError('disk fixture')):
            with self.assertRaises(OSError):
                await voice.speak_segment(LONG, '平静', turn_id=1, segment_index=1)
        await voice.speak_segment(LONG, '惊讶', turn_id=1, segment_index=1)
        self.assertEqual(self.payloads()[-1]['ref_audio_path'], 'full.wav')
        self.assertEqual(self.payloads()[-1]['emo_alpha'], .60)

    async def test_switch_off_keeps_original_per_segment_reference_selection(self):
        voice = self.voice(indextts2_natural_delivery=False)
        await voice.speak_segment(SHORT, turn_id=1, segment_index=0)
        await voice.speak_segment(LONG, turn_id=1, segment_index=1)
        self.assertEqual([p['ref_audio_path'] for p in self.payloads()], ['later.wav', 'full.wav'])
        self.assertTrue(all(p['natural_delivery'] is False for p in self.payloads()))

    async def test_all_emotion_payloads_stay_bounded_without_altering_words(self):
        for base in (0., .6, 1.5):
            voice = self.voice(indextts2_emo_alpha=base)
            for index, emotion in enumerate(('平静', '开心', '吐槽', '惊讶', '委屈', '得意')):
                text = SHORT + '！'
                await voice.speak_segment(text, emotion, turn_id=base, segment_index=index)
                payload = self.payloads()[-1]
                self.assertGreaterEqual(payload['emo_alpha'], 0.)
                self.assertLessEqual(payload['emo_alpha'], 1.)
                self.assertEqual(payload['text'], text)
                self.assertTrue(payload['reference_style'])

    async def test_empty_segment_breaks_continuity_without_http(self):
        voice = self.voice()
        await voice.speak_segment(SHORT, '平静', turn_id=1, segment_index=0)
        self.assertIsNone(await voice.speak_segment('[平静]', turn_id=1, segment_index=1))
        await voice.speak_segment(LONG, '惊讶', turn_id=1, segment_index=1)
        self.assertEqual(len(self.payloads()), 2)
        self.assertEqual(self.payloads()[-1]['ref_audio_path'], 'full.wav')
        self.assertEqual(self.payloads()[-1]['emo_alpha'], .60)

    async def test_natural_delivery_preserves_existing_quiet_onset_without_new_audio(self):
        samples = [0] * 500 + [30] * 150 + [300] * 100 + [0] * 500
        original = io.BytesIO()
        with wave.open(original, 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(1000)
            wav.writeframes(struct.pack('<' + 'h' * len(samples), *samples))

        async def handler(_request):
            return httpx.Response(200, content=original.getvalue())

        for natural, start in ((True, 470), (False, 550)):
            with self.subTest(natural=natural):
                voice = self.voice(handler, trim_edge_silence=True,
                                   indextts2_natural_delivery=natural)
                path = await voice.speak(SHORT)
                with wave.open(path, 'rb') as wav:
                    actual = list(struct.unpack('<' + 'h' * wav.getnframes(),
                                                wav.readframes(wav.getnframes())))
                # Both use unchanged samples and keep 220ms of trailing silence.
                self.assertEqual(actual, samples[start:970])


if __name__ == '__main__':
    unittest.main()
