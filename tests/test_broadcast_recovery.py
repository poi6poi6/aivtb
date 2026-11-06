from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import wave
from unittest.mock import patch

from brain.broadcast_recovery import BroadcastRecovery, STANDBY_TEXT, StandbyVoice, TurnDeliveryTracker
from brain.config import TTSConfig


def wav_bytes():
    data = io.BytesIO()
    with wave.open(data, 'wb') as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(24000)
        target.writeframes(b'\x01\x00' * 1200)
    return data.getvalue()


class RecoveryCircuitTests(unittest.TestCase):
    def test_distinct_turn_failures_cooldown_and_stage_success(self):
        now = [100.0]
        circuit = BroadcastRecovery(failure_limit=3, cooldown_sec=30, clock=lambda: now[0])
        for _ in range(10):
            circuit.failure('tts', 1)
        self.assertTrue(circuit.allow_automatic())
        self.assertEqual(circuit.failures['tts'], 1)
        circuit.failure('tts', 2)
        circuit.failure('tts', 3)
        self.assertFalse(circuit.allow_automatic())
        circuit.success('llm')
        self.assertFalse(circuit.allow_automatic(), 'LLM success cannot clear a failing TTS circuit')
        now[0] += 31
        self.assertTrue(circuit.allow_automatic())
        circuit.failure('tts', 4)
        self.assertFalse(circuit.allow_automatic())
        circuit.success('tts')
        self.assertTrue(circuit.allow_automatic())
        self.assertEqual(circuit.failures['tts'], 0)

    def test_standby_notice_is_rate_limited_independently_of_circuit(self):
        now = [0]
        circuit = BroadcastRecovery(clock=lambda: now[0])
        self.assertTrue(circuit.claim_notice())
        self.assertFalse(circuit.claim_notice())
        now[0] = 29.9
        self.assertFalse(circuit.claim_notice())
        now[0] = 30
        self.assertTrue(circuit.claim_notice())
        circuit.failure('unknown', 1)
        self.assertEqual(circuit.summary()['failures'], {'llm': 0, 'tts': 0})


class DeliveryTrackerTests(unittest.TestCase):
    def setUp(self):
        self.tracker = TurnDeliveryTracker()
        self.receipts = []
        self.tracker.begin(10, lambda completed, reason: self.receipts.append((completed, reason)))

    def test_generation_done_and_every_segment_are_both_required(self):
        for index in range(3):
            self.tracker.expect(10, index)
        self.tracker.played(10, 0, True)
        self.tracker.generated(10, True)
        self.tracker.played(10, 2, True)
        self.tracker.played(10, 2, True)
        self.assertEqual(self.receipts, [])
        self.tracker.played(10, 1, True)
        self.assertEqual(self.receipts, [(True, '')])
        self.assertNotIn(10, self.tracker.turns)
        self.tracker.cancel(10)
        self.tracker.played(10, 0, False)
        self.assertEqual(len(self.receipts), 1)

    def test_playback_before_generation_complete_waits_for_final_generation(self):
        self.tracker.expect(10, 0)
        self.tracker.played(10, 0, True)
        self.assertEqual(self.receipts, [])
        self.tracker.expect(10, 1)
        self.tracker.played(10, 1, True)
        self.assertEqual(self.receipts, [])
        self.tracker.generated(10, True)
        self.assertEqual(self.receipts, [(True, '')])

    def test_interruption_failed_generation_and_empty_reply_never_complete(self):
        self.tracker.expect(10, 0)
        self.tracker.expect(10, 1)
        self.tracker.played(10, 0, True)
        self.tracker.played(10, 1, False)
        self.tracker.generated(10, True)
        self.assertFalse(self.receipts[0][0])
        for turn_id, success in ((11, True), (12, False)):
            self.tracker.begin(turn_id, lambda ok, reason: self.receipts.append((ok, reason)))
            self.tracker.generated(turn_id, success)
        self.assertEqual([ok for ok, _ in self.receipts], [False, False, False])

    def test_sqlite_receipt_failure_preserves_proof_for_retry(self):
        failures = [True]
        def finish(ok, reason):
            if failures[0]:
                raise sqlite3.OperationalError('locked')
            self.receipts.append((ok, reason))
        self.tracker.begin(10, finish)
        self.tracker.expect(10, 0)
        self.tracker.generated(10, True)
        self.tracker.played(10, 0, True)
        self.assertIn(10, self.tracker.turns)
        self.assertEqual(self.receipts, [])
        failures[0] = False
        self.tracker.retry()
        self.assertEqual(self.receipts, [(True, '')])
        self.assertNotIn(10, self.tracker.turns)

    def test_cancelled_receipt_stays_failed_and_stale_receipt_can_be_discarded(self):
        failures = [True]
        def finish(ok, reason):
            if failures[0]:
                raise OSError('disk unavailable')
            self.receipts.append((ok, reason))
            return False  # The store may report a stale/withdrawn attempt.
        self.tracker.begin(10, finish)
        self.tracker.expect(10, 0)
        self.tracker.cancel(10, '已撤回')
        self.tracker.played(10, 0, True)
        self.tracker.generated(10, True)
        failures[0] = False
        self.tracker.retry()
        self.assertEqual(self.receipts, [(False, '已撤回')])
        self.assertNotIn(10, self.tracker.turns)


class StandbyVoiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = TTSConfig(backend='indextts2', out_dir=str(self.root))
        self.cache = StandbyVoice(self.root, self.cfg)
        self.source = self.root / 'fixture.wav'
        self.source.write_bytes(wav_bytes())
        source = self.source
        class Voice:
            async def speak(self, text, emotion):
                assert text == STANDBY_TEXT
                assert emotion == '平静'
                return str(source)
        self.voice = Voice()

    async def test_explicit_preparation_validates_voice_and_audio_changes(self):
        self.assertIsNone(self.cache.available())
        await self.cache.prepare(self.voice)
        self.assertEqual(self.cache.available(), str(self.root / self.cache.name))
        self.cfg.indextts2_emo_alpha += .1
        self.assertIsNone(self.cache.available())
        self.cfg.indextts2_emo_alpha -= .1
        self.assertIsNotNone(self.cache.available())
        (self.root / self.cache.name).write_bytes(wav_bytes() + b'corrupt')
        self.assertIsNone(self.cache.available())

    async def test_reference_file_change_invalidates_prepared_clip(self):
        reference = self.root / 'reference.wav'
        reference.write_bytes(wav_bytes())
        self.cfg.indextts2_ref_audio_path = str(reference)
        await self.cache.prepare(self.voice)
        self.assertIsNotNone(self.cache.available())
        reference.write_bytes(wav_bytes() + b'new')
        self.assertIsNone(self.cache.available())

    async def test_fish_reference_change_invalidates_prepared_clip(self):
        self.cfg.backend = 'fish_speech'
        reference = self.root / 'fish-reference.wav'
        reference.write_bytes(wav_bytes())
        self.cfg.fish_speech_ref_audio_path = str(reference)
        await self.cache.prepare(self.voice)
        self.assertIsNotNone(self.cache.available())
        reference.write_bytes(wav_bytes() + b'changed')
        self.assertIsNone(self.cache.available())

    async def test_header_only_empty_and_malformed_manifests_are_not_available(self):
        self.source.write_bytes(b'RIFF\x04\x00\x00\x00WAVE')
        with self.assertRaises(ValueError):
            await self.cache.prepare(self.voice)
        target = self.root / self.cache.name
        target.write_bytes(self.source.read_bytes())
        self.cache.manifest.write_text(json.dumps({'fingerprint': self.cache.fingerprint(),
            'text': STANDBY_TEXT, 'sha256': hashlib.sha256(target.read_bytes()).hexdigest()}), encoding='utf-8')
        self.assertIsNone(self.cache.available())
        self.cache.manifest.write_text('[]', encoding='utf-8')
        self.assertIsNone(self.cache.available())

    async def test_failed_manifest_commit_does_not_expose_unverified_clip(self):
        with patch('brain.broadcast_recovery.atomic_json_write', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                await self.cache.prepare(self.voice)
        self.assertIsNone(self.cache.available())

    async def test_outside_source_and_mock_no_audio_are_rejected(self):
        with tempfile.TemporaryDirectory() as outside:
            source = Path(outside) / 'external.wav'
            source.write_bytes(wav_bytes())
            class OutsideVoice:
                async def speak(self, *args):
                    return str(source)
            with self.assertRaises(ValueError):
                await self.cache.prepare(OutsideVoice())
        class MockVoice:
            async def speak(self, *args):
                return None
        with self.assertRaises(ValueError):
            await self.cache.prepare(MockVoice())
