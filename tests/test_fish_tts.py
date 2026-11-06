from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import re
import tempfile
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import httpx

from brain.config import PlaybackConfig, TTSConfig, load_settings
from brain.events import Event
from brain.playback import SpeechController
from brain.speech_pipeline import SpeechPipeline
from brain.tts import FishSpeechTTS


def pcm_wav(frames: int = 1600) -> bytes:
    result = io.BytesIO()
    with wave.open(result, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b'\x00\x10' * frames)
    return result.getvalue()


class _InterruptedAudio(httpx.AsyncByteStream):
    def __init__(self, failure: BaseException):
        self.failure = failure
        self.closed = False

    async def __aiter__(self):
        yield pcm_wav()[:64]
        raise self.failure

    async def aclose(self):
        self.closed = True


class FishSpeechTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.output = self.root / 'audio'
        self.instances = []
        self.requests = []

    async def asyncTearDown(self):
        for voice in self.instances:
            await voice.close()
        self.temp.cleanup()

    def voice(self, handler=None, **options):
        # Protocol tests keep the original emotion mapping; natural delivery has
        # separate assertions so a style adjustment does not obscure failures.
        options.setdefault('fish_speech_natural_delivery', False)
        cfg = TTSConfig(backend='fish_speech', out_dir=str(self.output),
                        trim_edge_silence=False, **options)
        voice = FishSpeechTTS(cfg)
        self.instances.append(voice)

        async def transport(request):
            self.requests.append(request)
            if handler is not None:
                return await handler(request)
            return httpx.Response(200, content=pcm_wav(), headers={'Content-Type': 'audio/wav'})

        voice._client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
        return voice

    def payload(self, index=-1):
        return json.loads(self.requests[index].content)

    def assert_no_audio(self):
        self.assertEqual(list(self.output.iterdir()), [])

    async def test_official_json_contract_and_complete_wav(self):
        voice = self.voice(fish_speech_url='http://localhost:8081/')
        path = await voice.speak('你好，来听一首歌。')
        request = self.requests[0]
        self.assertEqual(str(request.url), 'http://localhost:8081/v1/tts')
        self.assertEqual(request.method, 'POST')
        self.assertEqual(request.headers['Content-Type'], 'application/json')
        self.assertEqual(request.headers['Accept'], 'audio/wav')
        self.assertEqual(self.payload(), {'text': '你好，来听一首歌。', 'format': 'wav', 'streaming': False})
        self.assertEqual(Path(path).parent, self.output)
        self.assertEqual(Path(path).read_bytes(), pcm_wav())

    async def test_reference_audio_is_encoded_with_its_transcript(self):
        reference = self.root / 'reference.wav'
        reference.write_bytes(pcm_wav())
        voice = self.voice(fish_speech_ref_audio_path=str(reference),
                           fish_speech_prompt_text='  这是参考音频的逐字转写。  ',
                           ref_audio_path='unrelated-sovits.wav', prompt_text='不能串到 Fish')
        await voice.speak('测试')
        payload = self.payload()
        self.assertNotIn('reference_id', payload)
        self.assertEqual(len(payload['references']), 1)
        self.assertEqual(base64.b64decode(payload['references'][0]['audio']), reference.read_bytes())
        self.assertEqual(payload['references'][0]['text'], '这是参考音频的逐字转写。')
        self.assertEqual(payload['use_memory_cache'], 'on')
        self.assertNotIn(str(reference), json.dumps(payload))

    async def test_reference_id_takes_precedence_over_local_audio(self):
        voice = self.voice(fish_speech_reference_id='  avatar  ',
                           fish_speech_ref_audio_path=str(self.root / 'missing.wav'))
        await voice.speak('测试')
        self.assertEqual(self.payload()['reference_id'], 'avatar')
        self.assertNotIn('references', self.payload())
        self.assertNotIn('use_memory_cache', self.payload())

    async def test_incomplete_reference_pair_rejected_before_request(self):
        for options in ({'fish_speech_ref_audio_path': 'reference.wav'},
                        {'fish_speech_prompt_text': '参考文本'}):
            with self.subTest(options=options):
                voice = self.voice(**options)
                with self.assertRaisesRegex(ValueError, '成对配置'):
                    await voice.speak('测试')
        self.assertEqual(self.requests, [])
        self.assert_no_audio()

    async def test_missing_and_oversized_reference_are_not_sent(self):
        missing = self.voice(fish_speech_ref_audio_path=str(self.root / 'missing.wav'),
                             fish_speech_prompt_text='参考文本')
        with self.assertRaisesRegex(ValueError, '不存在'):
            await missing.speak('测试')
        reference = self.root / 'reference.wav'
        reference.write_bytes(pcm_wav())
        oversized = self.voice(fish_speech_ref_audio_path=str(reference), fish_speech_prompt_text='参考文本')
        oversized.MAX_REFERENCE_BYTES = 256
        with self.assertRaises(ValueError):
            await oversized.speak('测试')
        self.assertEqual(self.requests, [])
        self.assert_no_audio()

    async def test_sanitizes_performance_tags_before_fish_emotion(self):
        voice = self.voice()
        original = '[开心][弯眼]你好（轻笑）'
        await voice.speak(original, '开心')
        self.assertEqual(self.payload()['text'], '[happy] 你好')
        self.assertEqual(original, '[开心][弯眼]你好（轻笑）')

    async def test_supported_emotions_and_neutral_unknown_emotions(self):
        voice = self.voice()
        for emotion, expected in [('平静', '你好'), ('开心', '[happy] 你好'),
                                  ('委屈', '[sad] 你好'), ('惊讶', '[surprised] 你好'),
                                  ('得意', '[confident] 你好'), ('吐槽', '[sarcastic] 你好'),
                                  ('unrecognized', '你好')]:
            with self.subTest(emotion=emotion):
                await voice.speak('你好', emotion)
                self.assertEqual(self.payload()['text'], expected)

    async def test_tags_can_be_disabled_and_empty_performance_is_not_synthesized(self):
        voice = self.voice(fish_speech_emotion_tags=False)
        self.assertIsNone(await voice.speak('[开心]（微笑）', '开心'))
        self.assertEqual(self.requests, [])
        await voice.speak('[开心]你好', '开心')
        self.assertEqual(self.payload()['text'], '你好')

    async def test_natural_delivery_changes_only_spoken_delivery_cues(self):
        voice = self.voice(fish_speech_natural_delivery=True)
        original = '今天过得怎么样？我已经准备好了，可以慢慢听你讲今天发生的事情。'
        await voice.speak(original, '平静')
        delivered = self.payload()['text']
        self.assertNotEqual(delivered, original)
        self.assertEqual(re.sub(r'\[[^\]\r\n]+\] ?', '', delivered), original)
        self.assertEqual(self.payload()['format'], 'wav')
        self.assertFalse(self.payload()['streaming'])

    async def test_disabling_emotion_tags_also_disables_natural_delivery(self):
        voice = self.voice(fish_speech_natural_delivery=True,
                           fish_speech_emotion_tags=False)
        original = '今天过得怎么样？我已经准备好了，可以慢慢听你讲今天发生的事情。'
        for emotion in ('平静', '开心', '委屈', '惊讶', '得意', '吐槽'):
            with self.subTest(emotion=emotion):
                await voice.speak(original, emotion)
                self.assertEqual(self.payload()['text'], original)

    async def test_contiguous_segments_share_one_breath_per_turn(self):
        voice = self.voice(fish_speech_natural_delivery=True, fish_speech_breathing=True)
        texts = [
            '今天想和你分享一个小小的发现。',
            '还想认真听听你今天发生了什么。',
            '我想慢慢讲给你听。',
            '今天终于把这个难题解开了，想到你也一定会跟着高兴起来，我就忍不住想多说几句。',
        ]
        for index, text in enumerate(texts):
            path = await voice.speak_segment(text, '开心', turn_id=10, segment_index=index)
            self.assertTrue(Path(path).is_file())
            self.assertEqual(re.sub(r'\[[^\]\r\n]+\] ?', '', self.payload()['text']), text)
        delivered = [json.loads(request.content)['text'] for request in self.requests]
        self.assertNotIn('[soft inhale]', delivered[0])
        self.assertNotIn('[soft inhale]', delivered[1])
        self.assertIn('[soft inhale]', delivered[2])
        self.assertNotIn('[soft inhale]', delivered[3])
        self.assertEqual(sum(text.count('[soft inhale]') for text in delivered), 1)

    async def test_direct_speech_is_independent_of_segment_breath_budget(self):
        voice = self.voice(fish_speech_natural_delivery=True, fish_speech_breathing=True)
        text = '今天想和你分享一个小小的发现。'
        for _ in range(3):
            await voice.speak(text, '平静')
            self.assertNotIn('[soft inhale]', self.payload()['text'])
        await voice.speak_segment(text, '平静', turn_id=11, segment_index=0)
        await voice.speak_segment(text, '平静', turn_id=11, segment_index=1)
        await voice.speak('今天终于把这个难题解开了，想到你也一定会跟着高兴起来，我就忍不住想多说几句。')
        await voice.speak_segment('我想慢慢讲给你听。', '平静', turn_id=11, segment_index=2)
        self.assertIn('[soft inhale]', self.payload()['text'])

    async def test_successive_happy_segments_keep_energy_until_reassurance(self):
        voice = self.voice(fish_speech_natural_delivery=True, fish_speech_breathing=False)
        texts = ['今天终于赢下这场比赛了！', '接下来还有很多新的故事。', '没关系，我们可以慢慢讲。']
        tones = []
        for index, text in enumerate(texts):
            await voice.speak_segment(text, '开心', turn_id=15, segment_index=index)
            delivered = self.payload()['text']
            self.assertEqual(re.sub(r'\[[^\]\r\n]+\] ?', '', delivered), text)
            tones.append(re.match(r'\[[^\]\r\n]+\]', delivered).group())
        self.assertRegex(tones[0], r'\b(?:excited|emphatic|emphasis)\b')
        self.assertRegex(tones[1], r'\bhappy\b')
        self.assertNotRegex(tones[1], r'\b(?:soft|softly|gentle|relaxed)\b')
        self.assertRegex(tones[2], r'\b(?:soft|softly|reassuring)\b')

    async def test_new_turn_gap_and_empty_segment_reset_continuation(self):
        text = '今天想和你分享一个小小的发现。'
        for reset in ('turn', 'gap', 'empty'):
            with self.subTest(reset=reset):
                voice = self.voice(fish_speech_natural_delivery=True, fish_speech_breathing=True)
                await voice.speak_segment(text, '平静', turn_id=20, segment_index=0)
                await voice.speak_segment(text, '平静', turn_id=20, segment_index=1)
                if reset == 'turn':
                    await voice.speak_segment(text, '平静', turn_id=21, segment_index=0)
                elif reset == 'gap':
                    await voice.speak_segment(text, '平静', turn_id=20, segment_index=3)
                else:
                    count = len(self.requests)
                    self.assertIsNone(await voice.speak_segment('', '平静', turn_id=20, segment_index=2))
                    self.assertEqual(len(self.requests), count)
                    await voice.speak_segment(text, '平静', turn_id=20, segment_index=3)
                self.assertNotIn('[soft inhale]', self.payload()['text'])

    async def test_long_idle_period_starts_a_fresh_breath_budget(self):
        voice = self.voice(fish_speech_natural_delivery=True, fish_speech_breathing=True)
        text = '今天想和你分享一个小小的发现。'
        # Replace only the adapter's time reference; asyncio keeps its real clock.
        with patch('brain.tts.time', wraps=time) as clock:
            clock.monotonic.return_value = 100.0
            await voice.speak_segment(text, '平静', turn_id=30, segment_index=0)
            await voice.speak_segment(text, '平静', turn_id=30, segment_index=1)
            clock.monotonic.return_value = 131.0
            await voice.speak_segment('我想慢慢讲给你听。', '平静', turn_id=30, segment_index=2)
        self.assertNotIn('[soft inhale]', self.payload()['text'])

    async def test_idle_or_segment_gap_does_not_restore_a_used_turn_breath(self):
        long_text = '今天终于把这个难题解开了，想到你也一定会跟着高兴起来，我就忍不住想多说几句。'
        for reset in ('idle', 'gap'):
            with self.subTest(reset=reset):
                voice = self.voice(fish_speech_natural_delivery=True, fish_speech_breathing=True)
                with patch('brain.tts.time', wraps=time) as clock:
                    clock.monotonic.return_value = 100.0
                    for index in range(3):
                        await voice.speak_segment('今天想和你分享一个小小的发现。', '平静',
                                                  turn_id=35, segment_index=index)
                    self.assertIn('[soft inhale]', self.payload()['text'])
                    clock.monotonic.return_value = 131.0 if reset == 'idle' else 100.0
                    await voice.speak_segment(long_text, '平静', turn_id=35,
                                              segment_index=3 if reset == 'idle' else 5)
                    self.assertNotIn('[soft inhale]', self.payload()['text'])
                    await voice.speak_segment(long_text, '平静', turn_id=36, segment_index=0)
                    self.assertIn('[soft inhale]', self.payload()['text'])

    async def test_failed_cancelled_or_empty_segment_does_not_restore_a_used_turn_breath(self):
        long_text = '今天终于把这个难题解开了，想到你也一定会跟着高兴起来，我就忍不住想多说几句。'
        for interruption in ('http', 'cancel', 'empty'):
            with self.subTest(interruption=interruption):
                count = 0
                entered, release = asyncio.Event(), asyncio.Event()

                async def handler(request):
                    nonlocal count
                    count += 1
                    if count == 2 and interruption == 'http':
                        return httpx.Response(503, json={'error': 'not ready'})
                    if count == 2 and interruption == 'cancel':
                        entered.set()
                        await release.wait()
                    return httpx.Response(200, content=pcm_wav(), headers={'Content-Type': 'audio/wav'})

                voice = self.voice(handler, fish_speech_natural_delivery=True, fish_speech_breathing=True)
                await voice.speak_segment(long_text, '平静', turn_id=37, segment_index=0)
                self.assertIn('[soft inhale]', self.payload()['text'])
                if interruption == 'http':
                    with self.assertRaises(httpx.HTTPStatusError):
                        await voice.speak_segment(long_text, '平静', turn_id=37, segment_index=1)
                elif interruption == 'cancel':
                    pending = asyncio.create_task(voice.speak_segment(
                        long_text, '平静', turn_id=37, segment_index=1))
                    await asyncio.wait_for(entered.wait(), 1)
                    pending.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await pending
                else:
                    self.assertIsNone(await voice.speak_segment(
                        '', '平静', turn_id=37, segment_index=1))
                await voice.speak_segment(long_text, '平静', turn_id=37, segment_index=2)
                self.assertNotIn('[soft inhale]', self.payload()['text'])

    async def test_disabling_any_delivery_switch_disables_segment_breathing(self):
        for option in ('fish_speech_breathing', 'fish_speech_natural_delivery',
                       'fish_speech_emotion_tags'):
            options = {'fish_speech_breathing': True, 'fish_speech_natural_delivery': True,
                       'fish_speech_emotion_tags': True, option: False}
            with self.subTest(option=option):
                voice = self.voice(**options)
                for index in range(4):
                    await voice.speak_segment('今天想和你分享一个小小的发现。', '开心',
                                              turn_id=40, segment_index=index)
                    self.assertNotIn('[soft inhale]', self.payload()['text'])

    async def test_failed_or_invalid_synthesis_clears_segment_breath_budget(self):
        for failure in ('http', 'wav', 'write'):
            with self.subTest(failure=failure):
                count = 0

                async def handler(request):
                    nonlocal count
                    count += 1
                    if count == 3 and failure == 'http':
                        return httpx.Response(503, json={'error': 'not ready'})
                    if count == 3 and failure == 'wav':
                        return httpx.Response(200, content=b'invalid', headers={'Content-Type': 'audio/wav'})
                    return httpx.Response(200, content=pcm_wav(), headers={'Content-Type': 'audio/wav'})

                voice = self.voice(handler, fish_speech_natural_delivery=True, fish_speech_breathing=True)
                text = '今天想和你分享一个小小的发现。'
                await voice.speak_segment(text, '平静', turn_id=50, segment_index=0)
                await voice.speak_segment(text, '平静', turn_id=50, segment_index=1)
                if failure == 'write':
                    with patch.object(Path, 'write_bytes', side_effect=OSError('disk full')):
                        with self.assertRaises(OSError):
                            await voice.speak_segment(text, '平静', turn_id=50, segment_index=2)
                else:
                    error = httpx.HTTPStatusError if failure == 'http' else ValueError
                    with self.assertRaises(error):
                        await voice.speak_segment(text, '平静', turn_id=50, segment_index=2)
                self.assertIn('[soft inhale]', self.payload()['text'])
                # Retrying the failed index must not inherit the previous attempt's
                # pending breath or the preceding segment's accumulated budget.
                await voice.speak_segment(text, '平静', turn_id=50, segment_index=2)
                self.assertNotIn('[soft inhale]', self.payload()['text'])
                await voice.speak_segment(text, '平静', turn_id=50, segment_index=0)
                self.assertNotIn('[soft inhale]', self.payload()['text'])
                await voice.speak_segment(text, '平静', turn_id=50, segment_index=1)
                self.assertNotIn('[soft inhale]', self.payload()['text'])
                await voice.speak_segment(text, '平静', turn_id=50, segment_index=2)
                self.assertIn('[soft inhale]', self.payload()['text'])

    async def test_cancelled_segment_clears_budget_and_allows_a_fresh_sequence(self):
        entered, release = asyncio.Event(), asyncio.Event()
        count = 0

        async def handler(request):
            nonlocal count
            count += 1
            if count == 3:
                entered.set()
                await release.wait()
            return httpx.Response(200, content=pcm_wav(), headers={'Content-Type': 'audio/wav'})

        voice = self.voice(handler, fish_speech_natural_delivery=True, fish_speech_breathing=True)
        text = '今天想和你分享一个小小的发现。'
        await voice.speak_segment(text, '平静', turn_id=60, segment_index=0)
        await voice.speak_segment(text, '平静', turn_id=60, segment_index=1)
        pending = asyncio.create_task(voice.speak_segment(text, '平静', turn_id=60, segment_index=2))
        await asyncio.wait_for(entered.wait(), 1)
        self.assertIn('[soft inhale]', self.payload()['text'])
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        await voice.speak_segment(text, '平静', turn_id=60, segment_index=2)
        self.assertNotIn('[soft inhale]', self.payload()['text'])
        for index in range(3):
            await asyncio.wait_for(voice.speak_segment(text, '平静', turn_id=60, segment_index=index), 1)
            self.assertEqual('[soft inhale]' in self.payload()['text'], index == 2)

    async def test_optional_auth_only_uses_configured_environment_variable(self):
        voice = self.voice(fish_speech_api_key_env='TEST_FISH_KEY')
        with patch.dict(os.environ, {'TEST_FISH_KEY': '  test-only-token  ',
                                    'OPENAI_API_KEY': 'unrelated'}, clear=True):
            await voice.speak('测试')
        self.assertEqual(self.requests[-1].headers['Authorization'], 'Bearer test-only-token')
        self.assertNotIn('test-only-token', self.requests[-1].content.decode())
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'unrelated'}, clear=True):
            await voice.speak('测试')
        self.assertNotIn('Authorization', self.requests[-1].headers)

    async def test_http_errors_never_create_audio(self):
        for status in (401, 404, 429, 500):
            with self.subTest(status=status):
                async def handler(request):
                    return httpx.Response(status, json={'detail': 'failed'})
                voice = self.voice(handler)
                with self.assertRaises(httpx.HTTPStatusError):
                    await voice.speak('测试')
                self.assert_no_audio()
        self.assertTrue(all(req.url.path == '/v1/tts' for req in self.requests))

    async def test_rejects_non_audio_and_malformed_or_truncated_wav(self):
        for content_type, data in [('application/json', b'{"error": "unavailable"}'),
                                   ('text/html', b'<html>wrong endpoint</html>'),
                                   ('audio/wav', b'not a WAV'),
                                   ('application/octet-stream', pcm_wav()[:-8]),
                                   ('audio/wav', pcm_wav(0))]:
            with self.subTest(content_type=content_type, length=len(data)):
                async def handler(request):
                    return httpx.Response(200, content=data, headers={'Content-Type': content_type})
                voice = self.voice(handler)
                with self.assertRaises(ValueError):
                    await voice.speak('测试')
                self.assert_no_audio()

    async def test_oversized_response_stops_before_writing(self):
        voice = self.voice()
        voice.MAX_AUDIO_BYTES = 256
        with self.assertRaisesRegex(ValueError, '超过'):
            await voice.speak('测试')
        self.assert_no_audio()

    async def test_disconnect_timeout_and_cancellation_discard_partial_audio(self):
        for failure in (httpx.ReadError('connection lost'), httpx.ReadTimeout('timeout'),
                        asyncio.CancelledError()):
            with self.subTest(error=type(failure).__name__):
                stream = _InterruptedAudio(failure)
                async def handler(request):
                    return httpx.Response(200, stream=stream, headers={'Content-Type': 'audio/wav'})
                voice = self.voice(handler)
                with self.assertRaises(type(failure)):
                    await voice.speak('测试')
                self.assertTrue(stream.closed)
                self.assert_no_audio()
        self.assertEqual(len(self.requests), 3)
        self.assertTrue(all(req.url.path == '/v1/tts' for req in self.requests),
                        'Fish Speech does not implement the IndexTTS /cancel endpoint')

    async def test_actual_task_cancel_closes_stream_and_releases_lock(self):
        entered, release = asyncio.Event(), asyncio.Event()

        class PendingAudio(httpx.AsyncByteStream):
            closed = False

            async def __aiter__(self):
                yield pcm_wav()[:64]
                entered.set()
                await release.wait()
                yield pcm_wav()[64:]

            async def aclose(self):
                self.closed = True

        stream = PendingAudio()

        async def handler(request):
            if len(self.requests) == 1:
                return httpx.Response(200, stream=stream, headers={'Content-Type': 'audio/wav'})
            return httpx.Response(200, content=pcm_wav(), headers={'Content-Type': 'audio/wav'})

        voice = self.voice(handler)
        task = asyncio.create_task(voice.speak('取消这段'))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(stream.closed)
        self.assert_no_audio()
        self.assertTrue(Path(await asyncio.wait_for(voice.speak('继续下一段'), 1)).exists())
        self.assertEqual(len(self.requests), 2)

    async def test_only_one_synthesis_in_flight_and_unique_output_files(self):
        entered, release = asyncio.Event(), asyncio.Event()
        active, peak = 0, 0

        async def handler(request):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            entered.set()
            await release.wait()
            active -= 1
            return httpx.Response(200, content=pcm_wav(), headers={'Content-Type': 'audio/wav'})

        voice = self.voice(handler)
        first = asyncio.create_task(voice.speak('第一句'))
        await asyncio.wait_for(entered.wait(), 1)
        second = asyncio.create_task(voice.speak('第二句'))
        await asyncio.sleep(0)
        self.assertEqual(len(self.requests), 1)
        release.set()
        paths = await asyncio.wait_for(asyncio.gather(first, second), 1)
        self.assertEqual(peak, 1)
        self.assertEqual(len(set(paths)), 2)
        self.assertTrue(all(Path(path).read_bytes() == pcm_wav() for path in paths))

    async def test_trim_silence_applies_to_valid_audio_only(self):
        voice = self.voice()
        voice.cfg.trim_edge_silence = True
        trimmed = pcm_wav(800)
        with patch('brain.tts.trim_edge_silence', return_value=trimmed) as trim:
            path = await voice.speak('测试')
        trim.assert_called_once_with(pcm_wav(), start_padding_sec=0.1)
        self.assertEqual(Path(path).read_bytes(), trimmed)

    async def test_leading_breath_keeps_more_audio_before_first_voiced_word(self):
        voice = self.voice(fish_speech_natural_delivery=True, fish_speech_breathing=True)
        voice.cfg.trim_edge_silence = True
        with patch('brain.tts.trim_edge_silence', return_value=pcm_wav()) as trim:
            for index in range(3):
                await voice.speak_segment('今天想和你分享一个小小的发现。', '平静',
                                          turn_id=70, segment_index=index)
        self.assertIn('[soft inhale]', self.payload()['text'])
        self.assertEqual([call.kwargs['start_padding_sec'] for call in trim.call_args_list],
                         [0.1, 0.1, 0.35])

    async def test_existing_pipeline_keeps_fish_tags_out_of_subtitles(self):
        voice = self.voice()
        playback = SpeechController(PlaybackConfig())
        messages = []

        async def broadcast(message):
            messages.append(message)

        pipeline = SpeechPipeline(voice, playback, broadcast)
        worker = asyncio.create_task(pipeline.run())
        try:
            result = await asyncio.wait_for(pipeline.enqueue(
                turn_id=1, segment_index=0, text='找到这首歌了。', emotion='开心',
                actions=['弯眼'], event=Event('companion', 'chat', '点歌')), 1)
            self.assertTrue(Path(result.audio_path).is_file())
            self.assertEqual(self.payload()['text'], '[happy] 找到这首歌了。')
            audio = next(message for message in messages if message['type'] == 'audio')
            self.assertEqual(audio['text'], '找到这首歌了。')
            self.assertEqual(audio['emotion'], '开心')
            self.assertEqual(audio['actions'], ['弯眼'])
            self.assertEqual(audio['url'], '/audio/' + Path(result.audio_path).name)
        finally:
            await pipeline.close()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_pipeline_reports_failure_without_queuing_bad_audio_and_can_retry(self):
        async def handler(request):
            if len(self.requests) == 1:
                return httpx.Response(503, json={'error': 'model not ready'})
            return httpx.Response(200, content=pcm_wav(), headers={'Content-Type': 'audio/wav'})

        voice = self.voice(handler)
        playback = SpeechController(PlaybackConfig())
        messages = []

        async def broadcast(message):
            messages.append(message)

        pipeline = SpeechPipeline(voice, playback, broadcast)
        worker = asyncio.create_task(pipeline.run())
        try:
            result = await asyncio.wait_for(pipeline.enqueue(
                turn_id=1, segment_index=0, text='第一次', emotion='平静', actions=[],
                event=Event('companion', 'chat', '你好')), 1)
            self.assertIsNone(result.audio_path)
            self.assertEqual(playback.state, 'idle')
            self.assertIsNone(playback.current)
            self.assertTrue(any(message['type'] == 'tts_error' for message in messages))
            self.assertFalse(any(message['type'] == 'audio' for message in messages))
            self.assert_no_audio()
            retry = await asyncio.wait_for(pipeline.enqueue(
                turn_id=2, segment_index=0, text='第二次', emotion='平静', actions=[],
                event=Event('companion', 'chat', '再试一次')), 1)
            self.assertTrue(Path(retry.audio_path).is_file())
            self.assertEqual(playback.current.turn_id, 2)
        finally:
            await pipeline.close()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_natural_delivery_cues_never_reach_subtitles_or_playback_text(self):
        voice = self.voice(fish_speech_natural_delivery=True)
        playback = SpeechController(PlaybackConfig())
        messages = []

        async def broadcast(message):
            messages.append(message)

        text = '今天过得怎么样？我已经准备好了，可以慢慢听你讲今天发生的事情。'
        pipeline = SpeechPipeline(voice, playback, broadcast)
        worker = asyncio.create_task(pipeline.run())
        try:
            result = await asyncio.wait_for(pipeline.enqueue(
                turn_id=3, segment_index=0, text=text, emotion='平静',
                actions=[], event=Event('companion', 'chat', '聊聊今天')), 1)
            self.assertTrue(Path(result.audio_path).is_file())
            self.assertNotEqual(self.payload()['text'], text)
            self.assertEqual(re.sub(r'\[[^\]\r\n]+\] ?', '', self.payload()['text']), text)
            audio = next(message for message in messages if message['type'] == 'audio')
            self.assertEqual(audio['text'], text)
            self.assertEqual(playback.current.text, text)
        finally:
            await pipeline.close()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_pipeline_passes_segment_context_and_keeps_breath_cues_out_of_subtitles(self):
        voice = self.voice(fish_speech_natural_delivery=True, fish_speech_breathing=True)
        playback = SpeechController(PlaybackConfig())
        messages = []

        async def broadcast(message):
            messages.append(message)

        texts = ['今天想和你分享一个小小的发现。', '还想认真听听你今天发生了什么。',
                 '我想慢慢讲给你听。']
        pipeline = SpeechPipeline(voice, playback, broadcast)
        worker = asyncio.create_task(pipeline.run())
        try:
            for index, text in enumerate(texts):
                result = await asyncio.wait_for(pipeline.enqueue(
                    turn_id=80, segment_index=index, text=text, emotion='开心', actions=[],
                    event=Event('companion', 'chat', '聊聊今天')), 1)
                self.assertTrue(Path(result.audio_path).is_file())
                self.assertEqual(playback.current.text, text)
                playback.audio_finished(playback.current.id)
            self.assertIn('[soft inhale]', self.payload()['text'])
            audio_messages = [message for message in messages if message['type'] == 'audio']
            self.assertEqual([message['text'] for message in audio_messages], texts)
            self.assertTrue(all(message['emotion'] == '开心' for message in audio_messages))
            self.assertTrue(all('[soft inhale]' not in message['text'] for message in messages
                                if isinstance(message.get('text'), str)))
        finally:
            await pipeline.close()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_local_rhythm_is_sent_to_fish_without_changing_playback_or_subtitles(self):
        voice = self.voice(fish_speech_natural_delivery=True, fish_speech_breathing=False)
        playback = SpeechController(PlaybackConfig())
        messages = []

        async def broadcast(message):
            messages.append(message)

        text = '不是想让你赶快说完，而是想听听你的真实想法。接下来我们可以慢慢聊。'
        pipeline = SpeechPipeline(voice, playback, broadcast)
        worker = asyncio.create_task(pipeline.run())
        try:
            result = await asyncio.wait_for(pipeline.enqueue(
                turn_id=90, segment_index=0, text=text, emotion='开心', actions=['弯眼'],
                event=Event('companion', 'chat', '聊聊今天')), 1)
            self.assertTrue(Path(result.audio_path).is_file())
            delivered = self.payload()['text']
            self.assertIn('slightly quicker', delivered)
            self.assertIn('slower, deliberate emphasis', delivered)
            self.assertIn('normal conversational pace', delivered)
            self.assertEqual(re.sub(r'\[[^\]\r\n]+\] ?', '', delivered), text)
            self.assertEqual(playback.current.text, text)
            audio = next(message for message in messages if message['type'] == 'audio')
            self.assertEqual(audio['text'], text)
            self.assertEqual(audio['emotion'], '开心')
            self.assertEqual(audio['actions'], ['弯眼'])
        finally:
            await pipeline.close()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_searching_words_remain_in_subtitles_but_delivery_cues_stay_in_fish(self):
        voice = self.voice(fish_speech_natural_delivery=True, fish_speech_breathing=True)
        playback = SpeechController(PlaybackConfig())
        messages = []

        async def broadcast(message):
            messages.append(message)

        text = '嗯……怎么说呢？这件事情需要先听听你的想法。'
        pipeline = SpeechPipeline(voice, playback, broadcast)
        worker = asyncio.create_task(pipeline.run())
        try:
            result = await asyncio.wait_for(pipeline.enqueue(
                turn_id=91, segment_index=0, text=text, emotion='平静', actions=[],
                event=Event('companion', 'chat', '聊聊今天')), 1)
            self.assertTrue(Path(result.audio_path).is_file())
            delivered = self.payload()['text']
            self.assertIn('thoughtful, brief hesitation', delivered)
            self.assertEqual(re.sub(r'\[[^\]\r\n]+\] ?', '', delivered), text)
            self.assertEqual(playback.current.text, text)
            audio = next(message for message in messages if message['type'] == 'audio')
            self.assertEqual(audio['text'], text)
            self.assertEqual(audio['emotion'], '平静')
            self.assertTrue(all('hesitation' not in message['text'] for message in messages
                                if isinstance(message.get('text'), str)))
        finally:
            await pipeline.close()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_close_releases_client_and_is_idempotent(self):
        voice = self.voice()
        client = voice._client
        await voice.close()
        self.assertTrue(client.is_closed)
        self.assertIsNone(voice._client)
        await voice.close()

    async def test_client_uses_configured_timeout(self):
        voice = FishSpeechTTS(TTSConfig(out_dir=str(self.output), fish_speech_timeout_sec=123))
        self.instances.append(voice)
        client = voice._http_client()
        self.assertEqual(client.timeout.read, 123)
        self.assertIs(voice._http_client(), client)

    def test_configuration_rejects_bad_urls_and_timeout(self):
        for url in ('file:///tmp/tts', 'http://u:p@localhost:8081',
                    'http://localhost:8081/v1/tts', 'http://localhost:8081?key=secret'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                FishSpeechTTS(TTSConfig(out_dir=str(self.output), fish_speech_url=url))
        for timeout in (0, -1, float('nan'), float('inf'), True):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                FishSpeechTTS(TTSConfig(out_dir=str(self.output), fish_speech_timeout_sec=timeout))

    def test_configuration_rejects_non_boolean_natural_delivery(self):
        for value in (None, 0, 1, 'true', 'false', [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(
                    ValueError, 'fish_speech_natural_delivery'):
                FishSpeechTTS(TTSConfig(out_dir=str(self.output),
                                       fish_speech_natural_delivery=value))

    def test_configuration_rejects_non_boolean_breathing(self):
        for value in (None, 0, 1, 'true', 'false', [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'fish_speech_breathing'):
                FishSpeechTTS(TTSConfig(out_dir=str(self.output), fish_speech_breathing=value))


class FishSpeechConfigTests(unittest.TestCase):
    def test_breathing_defaults_on_for_new_and_existing_settings(self):
        self.assertTrue(TTSConfig().fish_speech_breathing)
        with tempfile.TemporaryDirectory() as folder:
            settings_file = Path(folder) / 'settings.yaml'
            settings_file.write_text('tts:\n  backend: fish_speech\n', encoding='utf-8')
            self.assertTrue(load_settings(settings_file).tts.fish_speech_breathing)
            settings_file.write_text('tts:\n  backend: fish_speech\n  fish_speech_breathing: false\n',
                                     encoding='utf-8')
            self.assertFalse(load_settings(settings_file).tts.fish_speech_breathing)

    def test_natural_delivery_is_enabled_for_new_and_existing_fish_settings(self):
        self.assertTrue(TTSConfig().fish_speech_natural_delivery)
        with tempfile.TemporaryDirectory() as folder:
            settings_file = Path(folder) / 'settings.yaml'
            settings_file.write_text('tts:\n  backend: fish_speech\n', encoding='utf-8')
            self.assertTrue(load_settings(settings_file).tts.fish_speech_natural_delivery)

    def test_relative_reference_path_resolves_against_project_root(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            settings_file = root / 'settings.yaml'
            settings_file.write_text(
                'tts:\n  backend: fish_speech\n  fish_speech_ref_audio_path: voices/reference.wav\n'
                '  fish_speech_prompt_text: hello\n  fish_speech_reference_id: voice-1\n'
                '  fish_speech_emotion_tags: false\n'
                '  fish_speech_natural_delivery: false\n', encoding='utf-8')
            with patch('brain.config.ROOT', root):
                cfg = load_settings(settings_file).tts
            self.assertEqual(cfg.backend, 'fish_speech')
            self.assertEqual(cfg.fish_speech_ref_audio_path, str(root / 'voices/reference.wav'))
            self.assertEqual(cfg.fish_speech_prompt_text, 'hello')
            self.assertEqual(cfg.fish_speech_reference_id, 'voice-1')
            self.assertFalse(cfg.fish_speech_emotion_tags)
            self.assertFalse(cfg.fish_speech_natural_delivery)

    def test_empty_reference_stays_empty_and_old_settings_keep_their_backend(self):
        with tempfile.TemporaryDirectory() as folder:
            settings_file = Path(folder) / 'settings.yaml'
            settings_file.write_text('tts:\n  backend: indextts2\n', encoding='utf-8')
            cfg = load_settings(settings_file).tts
            self.assertEqual(cfg.backend, 'indextts2')
            self.assertEqual(cfg.fish_speech_ref_audio_path, '')
            self.assertEqual(cfg.fish_speech_reference_id, '')


if __name__ == '__main__':
    unittest.main()
