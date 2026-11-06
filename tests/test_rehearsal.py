import asyncio
import itertools
import unittest

from brain.rehearsal import RehearsalService


class RehearsalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.reports = []
        self.turns = itertools.count(100)
        self.cancelled = []

        async def probe(report):
            self.reports.append(report)
            report('llm', 'running', '生成测试短句')
            report('llm', 'passed', '收到真实模型短句')
            report('tts', 'passed', '收到语音文件')
            report.bind_turn(next(self.turns))
            try:
                await report.wait_for_playback()
            except BaseException:
                self.cancelled.append(report.run_id)
                raise

        self.service = RehearsalService(probe, danmaku_status=lambda: {
            'state': 'connected', 'last_heartbeat': 1000}, clock=lambda: 1000)
        self.addAsyncCleanup(self.service.close)

    async def until(self, predicate):
        for _ in range(200):
            if predicate():
                return
            await asyncio.sleep(0)
        self.fail('rehearsal did not reach expected state')

    async def begin(self):
        result = await self.service.start()
        await self.until(lambda: self.service._turn_id is not None)
        return result['run_id'], self.service._turn_id

    async def finish(self):
        run_id, turn = await self.begin()
        self.assertTrue(self.service.audio_started(turn))
        self.assertTrue(self.service.audio_finished(turn))
        await self.service._task
        return run_id

    async def test_real_chain_requires_both_manual_checks_before_full_pass(self):
        run_id = await self.finish()
        status = self.service.snapshot()
        self.assertTrue(status['automatic_passed'])
        self.assertFalse(status['full_passed'])
        self.assertEqual(status['state'], 'awaiting_confirmation')
        status = self.service.confirm(run_id, 'microphone', True)
        self.assertFalse(status['full_passed'])
        status = self.service.confirm(run_id, 'obs_audio', True)
        self.assertTrue(status['full_passed'])
        self.assertEqual(status['state'], 'passed')

    async def test_manual_failure_cannot_be_presented_as_pass(self):
        run_id = await self.finish()
        self.service.confirm(run_id, 'microphone', True)
        result = self.service.confirm(run_id, 'obs_audio', False)
        self.assertEqual(result['state'], 'needs_attention')
        self.assertFalse(result['full_passed'])

    async def test_busy_and_concurrent_starts_do_not_call_probe_twice(self):
        self.service.can_start = lambda: False
        with self.assertRaises(ValueError):
            await self.service.start()
        self.assertEqual(self.reports, [])
        self.service.can_start = lambda: True
        results = await asyncio.gather(self.service.start(), self.service.start(), return_exceptions=True)
        self.assertEqual(sum(isinstance(result, ValueError) for result in results), 1)
        await self.until(lambda: bool(self.reports))
        self.assertEqual(len(self.reports), 1)

    async def test_specific_busy_reason_survives_host_validation(self):
        def busy():
            raise ValueError('当前没有主播放页，请先打开主控')
        self.service.can_start = busy
        with self.assertRaisesRegex(ValueError, '主播放页'):
            await self.service.start()

    async def test_unrelated_or_out_of_order_receipts_cannot_pass(self):
        run_id, turn = await self.begin()
        self.assertFalse(self.service.audio_started(turn + 1))
        self.assertFalse(self.service.audio_finished(turn + 1))
        self.assertTrue(self.service.audio_finished(turn))
        self.assertFalse(self.service.audio_started(turn))
        await self.service._task
        self.assertFalse(self.service.snapshot()['automatic_passed'])
        with self.assertRaises(ValueError):
            self.service.confirm(run_id, 'microphone', True)

    async def test_disconnect_and_late_receipts_never_restore_success(self):
        _, turn = await self.begin()
        self.service.audio_started(turn)
        self.assertTrue(self.service.playback_disconnected())
        self.assertFalse(self.service.audio_finished(turn))
        await self.service._task
        self.assertEqual(self.service.snapshot()['state'], 'failed')
        self.assertFalse(self.service.snapshot()['full_passed'])

    async def test_incomplete_delivery_fails_even_after_started(self):
        _, turn = await self.begin()
        self.service.audio_started(turn)
        self.service.audio_finished(turn, completed=False, reason='skip')
        await self.service._task
        self.assertEqual(self.service.snapshot()['state'], 'failed')

    async def test_cancel_waits_for_probe_cleanup_and_rejects_stale_confirmation(self):
        run_id, old_turn = await self.begin()
        await self.service.cancel(run_id)
        self.assertIn(run_id, self.cancelled)
        self.assertFalse(self.service.running)
        old_report = self.reports[-1]
        new_id, new_turn = await self.begin()
        self.assertNotEqual(new_id, run_id)
        self.assertNotEqual(old_turn, new_turn)
        self.assertFalse(self.service.audio_started(old_turn))
        self.assertFalse(old_report('llm', 'passed', 'stale'))
        with self.assertRaises(ValueError):
            self.service.confirm(run_id, 'obs_audio', True)
        with self.assertRaises(ValueError):
            await self.service.cancel(run_id)

    async def test_cancel_before_probe_starts_is_still_terminal(self):
        started = await self.service.start()
        result = await self.service.cancel(started['run_id'])
        self.assertEqual(result['state'], 'cancelled')
        self.assertFalse(result['running'])
        self.assertFalse(result['full_passed'])

    async def test_playback_timeout_cancels_probe_and_rejects_late_receipt(self):
        self.service.playback_timeout_sec = .01
        run_id, turn = await self.begin()
        await self.service._task
        self.assertIn(run_id, self.cancelled)
        self.assertEqual(self.service.snapshot()['state'], 'failed')
        self.assertFalse(self.service.audio_started(turn))
        self.assertFalse(self.service.audio_finished(turn))

    async def test_stale_danmaku_prevents_all_chain_success(self):
        self.service.danmaku_status = lambda: {'state': 'connected', 'last_heartbeat': 100}
        await self.finish()
        self.assertEqual(self.service.snapshot()['steps']['danmaku']['state'], 'failed')
        self.assertFalse(self.service.snapshot()['automatic_passed'])

    async def test_disabled_danmaku_is_explicitly_skipped(self):
        self.service.danmaku_status = lambda: {'state': 'disabled'}
        await self.finish()
        result = self.service.snapshot()
        self.assertEqual(result['steps']['danmaku']['state'], 'skipped')
        self.assertTrue(result['automatic_passed'])
        self.assertFalse(result['full_passed'])

    async def test_probe_failure_never_exposes_raw_exception_payload(self):
        async def probe(report):
            report('llm', 'running')
            raise RuntimeError('Bearer SECRET https://private.invalid/key')
        self.service.run_probe = probe
        await self.service.start()
        await self.service._task
        result = self.service.snapshot()
        self.assertEqual(result['state'], 'failed')
        self.assertNotIn('SECRET', str(result))
        self.assertNotIn('private.invalid', str(result))

    async def test_started_receipt_alone_and_reported_tts_cannot_mark_complete(self):
        run_id, turn = await self.begin()
        self.service.audio_started(turn)
        self.assertFalse(self.service.snapshot()['automatic_passed'])
        with self.assertRaises(ValueError):
            self.service.confirm(run_id, 'obs_audio', True)

    async def test_invalid_confirmation_types_and_closed_service_are_rejected(self):
        run_id = await self.finish()
        for field, passed in (('unknown', True), ('obs_audio', 'true'), ('microphone', 1)):
            with self.assertRaises(ValueError):
                self.service.confirm(run_id, field, passed)
        await self.service.close()
        with self.assertRaises(ValueError):
            await self.service.start()
