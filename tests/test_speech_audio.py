import io
import struct
import unittest
import wave

from brain.speech_audio import trim_edge_silence


def wav(samples):
    output = io.BytesIO()
    with wave.open(output, 'wb') as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(1000)
        target.writeframes(struct.pack('<' + 'h' * len(samples), *samples))
    return output.getvalue()


class SpeechAudioTests(unittest.TestCase):
    def test_selected_timbre_profiles_follow_spoken_segment_length(self):
        from brain.config import TTSConfig
        from brain.tts import index_voice_profile
        cfg = TTSConfig(indextts2_ref_audio_path='full.wav', indextts2_short_ref_audio_path='later.wav')
        self.assertEqual(index_voice_profile(cfg, '今天想聊点什么？慢慢说，我在听。'), ('later.wav', 1))
        self.assertEqual(index_voice_profile(cfg, '今天如果有点累，就先歇一会儿吧。你刚才说的那件事，我还想听听后面怎么样了。'), ('full.wav', 3))
        self.assertEqual(index_voice_profile(cfg, '字' * 28), ('full.wav', 3))
        self.assertEqual(index_voice_profile(cfg, '字' * 27 + '！ ' * 20), ('later.wav', 1))
        cfg.indextts2_short_ref_audio_path = ''
        self.assertEqual(index_voice_profile(cfg, '字' * 40), ('full.wav', 1))

    def test_trims_edges_but_preserves_internal_pause_and_samples(self):
        speech = [300] * 100 + [0] * 400 + [-300] * 100
        cleaned = trim_edge_silence(wav([0] * 800 + speech + [0] * 900))
        with wave.open(io.BytesIO(cleaned), 'rb') as source:
            actual = struct.unpack('<' + 'h' * source.getnframes(), source.readframes(source.getnframes()))
        self.assertEqual(list(actual), [0] * 100 + speech + [0] * 220)

    def test_short_boundaries_and_silent_audio_remain_unchanged(self):
        for samples in ([0] * 100 + [100] * 100 + [0] * 200, [0] * 1000):
            original = wav(samples)
            self.assertEqual(trim_edge_silence(original), original)

    def test_breath_padding_keeps_quiet_onset_before_the_first_voiced_sample(self):
        # A quiet breath can be below the normal edge-silence detection floor.
        original = wav([0] * 600 + [30] * 250 + [300] * 100 + [0] * 600)
        cleaned = trim_edge_silence(original, start_padding_sec=0.35)
        with wave.open(io.BytesIO(cleaned), 'rb') as source:
            actual = struct.unpack('<' + 'h' * source.getnframes(), source.readframes(source.getnframes()))
        self.assertEqual(list(actual), [0] * 100 + [30] * 250 + [300] * 100 + [0] * 220)

    def test_unknown_audio_is_not_modified(self):
        self.assertEqual(trim_edge_silence(b'not a wav'), b'not a wav')
