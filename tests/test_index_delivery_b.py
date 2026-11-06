from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import unittest

import httpx

from brain.config import TTSConfig
from brain.index_expression import index_emotion_options
from brain.tts import IndexTTS2TTS


CHAT = '今天这段旋律挺舒服的，前面轻轻铺开，后半段反而更耐听。'
EXPLAIN = '先把最重要的一件事做完，剩下的慢慢排，不用同时顾着所有事。'
TEASE = '你这计划排得挺满，连休息都要预约，倒也很有你的风格。'


class IndexAuditionBTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.voices = []
        self.payloads = []

    async def asyncTearDown(self):
        for voice in self.voices:
            await voice.close()
        self.folder.cleanup()

    def voice(self, **options):
        options.setdefault('indextts2_delivery_preset', 'expressive')
        voice = IndexTTS2TTS(TTSConfig(out_dir=self.folder.name, trim_edge_silence=False,
                                      indextts2_ref_audio_path='full.wav',
                                      indextts2_short_ref_audio_path='later.wav', **options))

        async def capture(request):
            self.payloads.append(json.loads(request.content))
            return httpx.Response(200, content=b'audio fixture')

        voice._client = httpx.AsyncClient(transport=httpx.MockTransport(capture))
        self.voices.append(voice)
        return voice

    async def test_three_accepted_auditions_reach_the_production_request(self):
        voice = self.voice()
        for text, emotion, expected_emotion, alpha, temperature, weight in (
            (CHAT, '平静', '开心', .552, .8, .12144),
            (EXPLAIN, '平静', '平静', .408, .9, 0.),
            (TEASE, '吐槽', '吐槽', .65, .8, .143),
        ):
            path = await voice.speak(text, emotion)
            payload = self.payloads[-1]
            self.assertEqual(payload['text'], text)
            self.assertEqual(payload['emotion'], expected_emotion)
            self.assertEqual(payload['emo_alpha'], alpha)
            self.assertEqual(payload['temperature'], temperature)
            self.assertEqual(payload['ref_audio_path'], 'later.wav')
            self.assertEqual(payload['num_beams'], 1)
            self.assertEqual(Path(path).read_bytes(), b'audio fixture')
            options = index_emotion_options(payload['emotion'], payload['emo_alpha'],
                                            reference_style=True, natural_delivery=True)
            actual_weight = sum(options['emo_vector'] or []) * options['emo_alpha']
            self.assertAlmostEqual(actual_weight, weight)

    async def test_mixed_preference_keeps_chat_and_explanation_b_but_teasing_a(self):
        voice = self.voice(indextts2_expressive_teasing=False)
        for text, emotion, expected_emotion, alpha, temperature, weight in (
            (CHAT, '平静', '开心', .552, .8, .12144),
            (EXPLAIN, '平静', '平静', .408, .9, 0.),
            (TEASE, '吐槽', '吐槽', .51, .8, .1122),
        ):
            with self.subTest(text=text):
                await voice.speak(text, emotion)
                payload = self.payloads[-1]
                self.assertEqual(payload['text'], text)
                self.assertEqual(payload['emotion'], expected_emotion)
                self.assertEqual(payload['emo_alpha'], alpha)
                self.assertEqual(payload['temperature'], temperature)
                self.assertEqual(payload['ref_audio_path'], 'later.wav')
                self.assertEqual(payload['num_beams'], 1)
                options = index_emotion_options(payload['emotion'], payload['emo_alpha'],
                                                reference_style=True, natural_delivery=True)
                actual_weight = sum(options['emo_vector'] or []) * options['emo_alpha']
                self.assertAlmostEqual(actual_weight, weight)

    async def test_mixed_preference_still_smooths_and_ends_explanation_at_teasing(self):
        voice = self.voice(indextts2_expressive_teasing=False)
        await voice.speak_segment('居然真的完成了！', '惊讶', turn_id=1, segment_index=0)
        await voice.speak_segment(TEASE, '吐槽', turn_id=1, segment_index=1)
        await voice.speak_segment(TEASE, '吐槽', turn_id=1, segment_index=2)
        self.assertEqual([p['emo_alpha'] for p in self.payloads], [.64, .52, .51])
        await voice.speak_segment(EXPLAIN, turn_id=2, segment_index=0)
        await voice.speak_segment('剩下的放到下一步处理。', turn_id=2, segment_index=1)
        await voice.speak_segment(TEASE, '吐槽', turn_id=2, segment_index=2)
        await voice.speak_segment('剩下的放到下一步处理。', turn_id=2, segment_index=3)
        self.assertEqual([p['temperature'] for p in self.payloads[-4:]], [.9, .9, .8, .8])
        self.assertEqual(self.payloads[-2]['emo_alpha'], .51)

    async def test_care_errors_negation_quotes_and_unknown_emotions_do_not_get_a_smile(self):
        voice = self.voice()
        for text, emotion in (
            ('你不舒服的话先休息，我在听。', '平静'),
            ('没关系，你慢慢说。', '平静'),
            ('搜索失败，没找到你喜欢的版本。', '平静'),
            ('这首我不喜欢，听着也不舒服。', '平静'),
            ('他说“今天挺开心”，然后就走了。', '平静'),
            ('“首先检查设置”是原话。', '平静'),
            ('嗯……怎么说呢，这首挺好听的。', '平静'),
            ('你愿意告诉我这些，我很开心。', '平静'),
            ('想到让你舒服一点的办法了。', '平静'),
            ('这首挺好听的。', '未知情绪'),
            ('这首挺好听的。', '委屈'),
        ):
            await voice.speak(text, emotion)
            payload = self.payloads[-1]
            self.assertEqual(payload['text'], text)
            self.assertEqual(payload['emotion'], emotion)
            self.assertEqual(payload['temperature'], .8)

    async def test_explanation_carries_only_through_successive_neutral_segments(self):
        voice = self.voice()
        await voice.speak_segment(EXPLAIN, turn_id=1, segment_index=0)
        await voice.speak_segment('剩下的放到下一步处理。', turn_id=1, segment_index=1)
        await voice.speak_segment('你累了就先休息。', turn_id=1, segment_index=2)
        await voice.speak_segment('剩下的放到下一步处理。', turn_id=1, segment_index=3)
        self.assertEqual([p['temperature'] for p in self.payloads], [.9, .9, .8, .8])

    async def test_new_turn_skipped_segment_and_standalone_speech_reset_cadence(self):
        voice = self.voice()
        await voice.speak_segment(EXPLAIN, turn_id=1, segment_index=0)
        await voice.speak_segment('剩下的放到下一步处理。', turn_id=2, segment_index=0)
        await voice.speak_segment(EXPLAIN, turn_id=3, segment_index=0)
        await voice.speak_segment('剩下的放到下一步处理。', turn_id=3, segment_index=2)
        await voice.speak(EXPLAIN)
        await voice.speak_segment('剩下的放到下一步处理。', turn_id=3, segment_index=3)
        self.assertEqual([p['temperature'] for p in self.payloads], [.9, .8, .9, .8, .9, .8])

    async def test_failed_or_cancelled_synthesis_cannot_leave_explanation_state(self):
        voice = self.voice()
        for failure in (httpx.ConnectError('fixture'), asyncio.CancelledError()):
            await voice.speak_segment(EXPLAIN, turn_id=1, segment_index=0)
            original = voice._client

            async def fail(request):
                if request.url.path == '/cancel':
                    return httpx.Response(200)
                raise failure

            voice._client = httpx.AsyncClient(transport=httpx.MockTransport(fail))
            with self.assertRaises(type(failure)):
                await voice.speak_segment(EXPLAIN, turn_id=1, segment_index=1)
            await voice._client.aclose()
            voice._client = original
            await voice.speak_segment('剩下的放到下一步处理。', turn_id=1, segment_index=1)
            self.assertEqual(self.payloads[-1]['temperature'], .8)

    async def test_b_targets_still_smooth_between_successful_segments(self):
        voice = self.voice()
        await voice.speak_segment('我在听，你慢慢说。', turn_id=1, segment_index=0)
        await voice.speak_segment(CHAT, turn_id=1, segment_index=1)
        await voice.speak_segment(TEASE, '吐槽', turn_id=1, segment_index=2)
        self.assertEqual([p['emo_alpha'] for p in self.payloads], [.408, .528, .648])
        await voice.speak_segment(TEASE, '吐槽', turn_id=2, segment_index=0)
        self.assertEqual(self.payloads[-1]['emo_alpha'], .65)

    async def test_rollback_and_legacy_mode_preserve_the_previous_contract(self):
        for settings in ({'indextts2_delivery_preset': 'reference'},
                         {'indextts2_natural_delivery': False},
                         {'indextts2_reference_style': False}):
            voice = self.voice(**settings)
            for text, emotion, alpha in ((CHAT, '平静', .408),
                                         (EXPLAIN, '平静', .408),
                                         (TEASE, '吐槽', .51)):
                await voice.speak(text, emotion)
                self.assertEqual(self.payloads[-1]['emotion'], emotion)
                self.assertEqual(self.payloads[-1]['emo_alpha'], alpha)
                self.assertNotIn('temperature', self.payloads[-1])

    def test_invalid_preset_is_reported_instead_of_silently_ignored(self):
        for value in ('B', 'typo', True, None, []):
            with self.assertRaisesRegex(ValueError, 'indextts2_delivery_preset'):
                self.voice(indextts2_delivery_preset=value)

    def test_invalid_teasing_flag_is_reported_instead_of_silently_ignored(self):
        for value in ('true', 'false', '', 0, 1, None, [], {}):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, 'indextts2_expressive_teasing'):
                    self.voice(indextts2_expressive_teasing=value)


if __name__ == '__main__':
    unittest.main()
