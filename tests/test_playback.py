from __future__ import annotations

import unittest

from brain.config import PlaybackConfig
from brain.playback import SpeechController, SpeechItem


def speech(
    item_id: int,
    priority: int = 10,
    created_at: float = 100.0,
) -> SpeechItem:
    return SpeechItem(
        id=item_id,
        url=f"/audio/{item_id}.wav",
        text=f"回复{item_id}",
        emotion="平静",
        actions=[],
        event={"source": "danmaku", "kind": "chat", "text": "测试"},
        priority=priority,
        created_at=created_at,
    )


class SpeechControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = [100.0]
        self.controller = SpeechController(
            PlaybackConfig(max_queue=2, max_age_sec=60, start_timeout_sec=10),
            clock=lambda: self.now[0],
        )

    def test_only_dispatches_next_after_current_finishes(self) -> None:
        first = self.controller.enqueue(speech(1))
        self.assertEqual(first[0]["type"], "audio")
        second = self.controller.enqueue(speech(2))
        self.assertFalse(any(message["type"] == "audio" for message in second))
        self.assertEqual(self.controller.summary()["queued_ids"], [2])
        self.controller.audio_started(1)
        messages = self.controller.audio_finished(1)
        audio = next(message for message in messages if message["type"] == "audio")
        self.assertEqual(audio["id"], 2)

    def test_high_priority_audio_interrupts_normal_speech(self) -> None:
        self.controller.enqueue(speech(1, priority=10))
        messages = self.controller.enqueue(speech(2, priority=100))
        self.assertEqual(messages[0], {"type": "audio_control", "action": "stop", "id": 1})
        self.assertEqual(messages[1]["id"], 2)
        self.assertEqual(self.controller.current.id, 2)
        self.assertEqual(self.controller.dropped, 1)

    def test_full_queue_drops_lowest_priority_item(self) -> None:
        self.controller.enqueue(speech(1, priority=30))
        self.controller.enqueue(speech(2, priority=20))
        self.controller.enqueue(speech(3, priority=40))
        self.controller.enqueue(speech(4, priority=10))
        self.assertEqual(self.controller.summary()["queued_ids"], [3, 2])
        self.assertEqual(self.controller.dropped, 1)

    def test_stale_generated_audio_is_never_dispatched(self) -> None:
        self.now[0] = 200.0
        messages = self.controller.enqueue(speech(1, created_at=100.0))
        self.assertFalse(any(message["type"] == "audio" for message in messages))
        self.assertIsNone(self.controller.current)
        self.assertEqual(self.controller.dropped, 1)

    def test_start_timeout_stops_current_and_advances(self) -> None:
        self.controller.enqueue(speech(1))
        self.controller.enqueue(speech(2))
        self.now[0] += 11
        messages = self.controller.tick()
        self.assertEqual(messages[0]["type"], "audio_control")
        self.assertEqual(messages[1]["type"], "audio")
        self.assertEqual(messages[1]["id"], 2)

    def test_skip_and_clear_are_backend_authoritative(self) -> None:
        self.controller.enqueue(speech(1))
        self.controller.enqueue(speech(2))
        skipped = self.controller.skip_current()
        self.assertEqual(skipped[0]["action"], "stop")
        self.assertEqual(self.controller.current.id, 2)
        cleared = self.controller.clear()
        self.assertEqual(cleared[0]["action"], "clear")
        self.assertEqual(self.controller.summary()["state"], "idle")
        self.assertEqual(self.controller.summary()["queued"], 0)

    def test_wrong_browser_ack_cannot_advance_queue(self) -> None:
        self.controller.enqueue(speech(1))
        self.controller.enqueue(speech(2))
        self.assertEqual(self.controller.audio_finished(999), [])
        self.assertEqual(self.controller.current.id, 1)

    def test_cancel_turn_removes_all_of_its_segments(self) -> None:
        first = speech(1)
        first.turn_id = 10
        second = speech(2)
        second.turn_id = 10
        other = speech(3)
        other.turn_id = 11
        self.controller.enqueue(first)
        self.controller.enqueue(second)
        self.controller.enqueue(other)
        messages = self.controller.cancel_turn(10)
        self.assertTrue(any(message.get("action") == "stop" for message in messages))
        self.assertEqual(self.controller.current.id, 3)
        self.assertEqual(self.controller.summary()["queued"], 0)

    def test_finished_while_paused_does_not_let_new_segment_overtake_queue(self) -> None:
        items = [speech(i) for i in (1, 2, 3)]
        for item in items:
            item.turn_id = 10
        self.controller.enqueue(items[0])
        self.controller.enqueue(items[1])
        self.controller.set_paused(True)
        self.controller.audio_finished(1)
        self.controller.enqueue(items[2])
        self.assertIsNone(self.controller.current)
        self.assertEqual(self.controller.summary()['queued_ids'], [2, 3])
        self.controller.set_paused(False)
        self.assertEqual(self.controller.current.id, 2)

    def test_admitted_turn_keeps_tail_but_invalid_receipt_still_expires(self) -> None:
        first, second = speech(1), speech(2)
        first.turn_id = second.turn_id = 10
        self.controller.enqueue(first)
        self.controller.enqueue(second)
        self.now[0] = 200
        self.controller.item_valid = lambda item: item.id != 2
        self.controller.audio_finished(1)
        self.assertIsNone(self.controller.current)
        self.assertFalse(self.controller.queue)


if __name__ == "__main__":
    unittest.main()
