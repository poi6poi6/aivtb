import unittest

from brain.metrics import RuntimeMetrics


class RuntimeMetricsTests(unittest.TestCase):
    def test_tracks_pipeline_latency_and_outcomes(self):
        now = [10.0]
        metrics = RuntimeMetrics(clock=lambda: now[0])
        metrics.record_event(7)
        now[0] = 10.4
        metrics.record_first_sentence(7)
        metrics.record_first_sentence(7)
        metrics.record_reply(7, latency=0.8, llm_ttft=0.2)
        now[0] = 11.1
        metrics.record_audio_started(7)

        summary = metrics.summary()
        self.assertEqual(summary["events"], 1)
        self.assertEqual(summary["replies"], 1)
        self.assertEqual(summary["pending"], 0)
        self.assertAlmostEqual(summary["latency"]["first_sentence"]["latest"], 0.4)
        self.assertAlmostEqual(summary["latency"]["audio_start"]["latest"], 1.1)

    def test_cancellation_and_error_clear_pending_events(self):
        metrics = RuntimeMetrics(clock=lambda: 1.0)
        metrics.record_event(1)
        metrics.record_event(2)
        metrics.record_cancelled(1)
        metrics.record_error(2)
        summary = metrics.summary()
        self.assertEqual(summary["cancelled"], 1)
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(summary["pending"], 0)


if __name__ == "__main__":
    unittest.main()
