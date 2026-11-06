"""Offline topic boundaries through the director and actual speech dispatch."""
from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace

from brain.config import DirectorConfig, PlaybackConfig
from brain.director import LiveDirector
from brain.events import Event
from brain.playback import SpeechController
from brain.speech_pipeline import SpeechPipeline
from brain.topic_flow import TopicFrame, TopicTracker
from brain.viewer_identity import viewer_identity


class DirectorTopicTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.serial = 0
        self.director = LiveDirector(
            DirectorConfig(enabled=True, proactive_interval_sec=8), clock=lambda: self.now)
        self.topics = TopicTracker(clock=lambda: self.now, wall_clock=lambda: self.now)

    def event(self, text, *, source="companion", user="你", **kwargs):
        self.serial += 1
        self.now += 1
        event = Event(source, "chat", text, user=user, ts=self.now, **kwargs)
        event.ui_id = self.serial
        return event

    def observe(self, event):
        self.director.observe_event(event)
        frame = self.topics.observe(event)
        self.director.sync_conversation_topic(event, frame)
        return frame

    def opening(self):
        event = self.event("聊聊玫瑰怎么养")
        self.observe(event)
        self.assertIn("玫瑰", self.director.current_topic)
        return event

    def test_close_clears_old_question_and_stays_quiet_across_late_reply_and_chat(self):
        opening = self.opening()
        self.director.set_phase("interaction")
        self.director.after_reply(opening, "平静", "大家喜欢玫瑰还是百合？")
        self.assertTrue(self.director.question_pending)
        closing = self.event("这个先不聊了")
        self.assertEqual(self.observe(closing).action, "close")
        self.director.prepare(closing)
        self.assertEqual(self.director.current_topic, "")
        self.assertFalse(self.director.question_pending)
        self.assertFalse(self.director.awaiting_audience)
        self.assertNotIn("玫瑰", closing.director_context)
        self.assertIn("不自行另起话题", closing.director_context)
        # An old generation completing after the boundary cannot invite again.
        self.director.after_reply(opening, "平静", "还是接着聊玫瑰好吗？")
        self.assertFalse(self.director.question_pending)
        for text, source in (("知道了", "companion"), ("聊聊电影吧", "danmaku")):
            self.observe(self.event(text, source=source))
        self.now += 60
        self.assertIsNone(self.director.poll_proactive())
        self.assertEqual(self.director.current_topic, "")
        idle = Event("system", "idle", "直播间冷场了")
        self.director.prepare(idle)
        self.assertIn("安静陪伴", idle.text)
        self.assertNotIn("玫瑰", idle.director_context)

    def test_new_substantive_topic_and_explicit_shift_replace_old_anchor(self):
        self.opening()
        self.observe(self.event("这个先不聊了"))
        for text in ("好", "嗯", "谢谢", "继续刚才的话题"):
            self.observe(self.event(text))
            self.now += 10
            self.assertIsNone(self.director.poll_proactive())
        fresh = self.event("聊聊最近的电影")
        self.assertEqual(self.observe(fresh).action, "start")
        self.director.prepare(fresh)
        self.assertIn("电影", fresh.director_context)
        self.assertNotIn("玫瑰", fresh.director_context)
        self.now += 10
        self.assertIn("电影", self.director.poll_proactive().text)
        shifted = self.event("换个话题，电脑内存怎么选？")
        self.assertEqual(self.observe(shifted).action, "shift")
        self.director.prepare(shifted)
        self.assertIn("电脑", shifted.director_context)
        self.assertNotIn("电影", shifted.director_context)

    def test_first_owner_message_can_end_a_room_topic_without_private_history(self):
        self.director.phase = "chatting"
        self.director.observe_event(self.event("今天想聊怎样养玫瑰", source="danmaku"))
        closing = self.event("这个先不聊了")
        frame = self.observe(closing)
        self.assertEqual(frame.reason, "explicit_shift")
        self.director.prepare(closing)
        self.assertNotIn("玫瑰", closing.director_context)
        self.assertEqual(self.director.current_topic, "")
        self.now += 10
        self.assertIsNone(self.director.poll_proactive())

    def test_aside_does_not_replace_anchor_and_explicit_resume_restores_it(self):
        opening = self.opening()
        anchor = self.director.current_topic
        side = self.event("顺便问一下，电影好看吗？")
        self.assertEqual(self.observe(side).action, "aside")
        self.assertEqual(self.director.current_topic, anchor)
        resumed = self.event("回到刚才的话题")
        frame = self.observe(resumed)
        self.assertEqual(frame.action, "resume")
        self.assertEqual(frame.topic_id, self.topics.inspect(opening).topic_id)
        self.assertEqual(self.director.current_topic, anchor)

    def test_only_same_trusted_owner_can_release_pause(self):
        self.opening()
        self.observe(self.event("这个先不聊了"))
        for source, user, kwargs in (
            ("danmaku", "你", {"forced": True}),
            ("game", "你", {}),
            ("companion", "另一人", {}),
            ("companion", "你", {"task_notice": True}),
            ("voice", "你", {"task_id": "task-1"}),
            ("companion", "你", {"agent_reply": "任务完成"}),
        ):
            with self.subTest(source=source, user=user, kwargs=kwargs):
                event = self.event("聊聊新电影", source=source, user=user, **kwargs)
                self.director.sync_conversation_topic(
                    event, TopicFrame(action="start", topic_id="new", anchor=event.text))
                self.now += 10
                self.assertIsNone(self.director.poll_proactive())
        self.observe(self.event("聊聊新电影", source="voice"))
        self.now += 10
        self.assertIn("电影", self.director.poll_proactive().text)

    def test_forgetting_other_owner_does_not_clear_current_anchor(self):
        opening = self.opening()
        other = self.event("你好", user="另一人")
        self.director.clear_conversation_topic(viewer_identity(other))
        self.assertIn("玫瑰", self.director.current_topic)
        self.director.clear_conversation_topic(viewer_identity(opening))
        self.assertEqual(self.director.current_topic, "")
        self.now += 10
        self.assertIsNone(self.director.poll_proactive())

    def test_retraction_requires_substantive_input_to_restore_continued_topic(self):
        opening = self.opening()
        closing = self.event("这个先不聊了", source="voice")
        self.observe(closing)
        self.topics.retract_turn(closing.ui_id)
        self.director.clear_conversation_topic(viewer_identity(opening))
        ack = self.event("好的谢谢")
        self.assertEqual(self.observe(ack).action, "continue")
        self.now += 10
        self.assertIsNone(self.director.poll_proactive())
        detail = self.event("主要是花盆的排水不好")
        self.assertEqual(self.observe(detail).action, "continue")
        self.assertIn("玫瑰", self.director.current_topic)
        self.now += 10
        self.assertIsNotNone(self.director.poll_proactive())

    def test_queued_proactive_is_invalid_after_close_shift_or_forgetting(self):
        for boundary in ("close", "shift", "forget"):
            with self.subTest(boundary=boundary):
                self.setUp()
                opening = self.opening()
                self.now += 10
                old = self.director.poll_proactive()
                self.assertTrue(self.director.program_event_current(old))
                if boundary == "forget":
                    self.director.clear_conversation_topic(viewer_identity(opening))
                else:
                    self.observe(self.event("这个先不聊了" if boundary == "close"
                                            else "换个话题，聊聊电影"))
                self.assertFalse(self.director.program_event_current(old))
                self.assertFalse(self.director.program_event_current(
                    {"director_topic_generation": old.director_topic_generation}))
                self.assertTrue(self.director.program_event_current(
                    Event("companion", "chat", "当前的普通聊天", user="你")))
                self.director.after_reply(old, "平静", "要接着说玫瑰吗？")
                self.assertFalse(self.director.question_pending)

    def test_private_boundaries_preserve_formal_program_and_live_poll(self):
        for formal in ("program", "poll"):
            with self.subTest(formal=formal):
                self.setUp()
                opening = self.opening()
                if formal == "program":
                    self.director.set_program_context({
                        "live_mode": True, "fresh": True, "show_id": "s", "card_id": "c",
                        "stage": "intro", "status_code": "intro", "topic": "正式庭院栏目",
                    })
                else:
                    event = Event("system", "chat", "投票开始")
                    event.live_poll = {
                        "id": "poll-1", "mode": "live", "state": "open",
                        "deadline": self.now + 60, "question": "选择温室还是书房？", "total": 0,
                    }
                    self.director.after_reply(event, "平静", event.live_poll["question"])
                expected = (self.director.current_topic, self.director.audience_poll_id,
                            self.director.awaiting_audience, self.director.phase)
                for text in ("这个先不聊了", "换个话题，聊聊电影"):
                    self.observe(self.event(text))
                    self.assertEqual((self.director.current_topic, self.director.audience_poll_id,
                                      self.director.awaiting_audience, self.director.phase), expected)
                self.director.clear_conversation_topic(viewer_identity(opening))
                self.assertEqual((self.director.current_topic, self.director.audience_poll_id,
                                  self.director.awaiting_audience, self.director.phase), expected)
                if formal == "program":
                    self.now += 10
                    cue = self.director.poll_proactive()
                    self.assertIsNotNone(cue)
                    self.assertTrue(self.director.program_event_current(cue))
                else:
                    self.director.observe_poll_vote(Event("danmaku", "chat", "投票 1"),
                        {"ok": True, "changed": True, "mode": "live", "poll_id": "poll-1"})
                    self.assertFalse(self.director.awaiting_audience)
                    self.assertEqual(self.director.successful_interactions, 1)


class DirectorTopicSpeechTests(unittest.IsolatedAsyncioTestCase):
    async def test_closed_topic_rejects_late_synthesis_and_queued_playback(self):
        # Both closures use real dispatch gates. No explicit pipeline cancellation
        # is needed, so the test also covers a model/TTS cancellation race.
        for close_during_synthesis in (True, False):
            with self.subTest(close_during_synthesis=close_during_synthesis):
                now = [1000.0]
                director = LiveDirector(DirectorConfig(enabled=True, proactive_interval_sec=8),
                                        clock=lambda: now[0])
                owner = Event("companion", "chat", "聊聊玫瑰", user="你", ts=now[0])
                frame = TopicFrame(action="start", topic_id="rose", anchor="聊聊玫瑰")
                director.sync_conversation_topic(owner, frame)
                now[0] += 10
                old = director.poll_proactive()
                old.ts = now[0]
                started, finish = asyncio.Event(), asyncio.Event()

                class Voice:
                    async def speak(self, text, emotion):
                        started.set()
                        await finish.wait()
                        return "C:/fake/old-topic.wav"

                    async def close(self):
                        pass

                messages = []

                async def broadcast(message):
                    messages.append(message)

                playback = SpeechController(PlaybackConfig(), clock=lambda: now[0])
                playback.item_valid = lambda item: director.program_event_current(item.event)
                pipeline = SpeechPipeline(Voice(), playback, broadcast)
                pipeline.event_valid = director.program_event_current
                worker = asyncio.create_task(pipeline.run())
                try:
                    future = pipeline.enqueue(turn_id=1, segment_index=0, text="玫瑰需要通风。",
                                              emotion="平静", actions=[], event=old)
                    await asyncio.wait_for(started.wait(), 1)
                    if not close_during_synthesis:
                        finish.set()
                        self.assertFalse((await asyncio.wait_for(future, 1)).cancelled)
                        self.assertIsNotNone(playback.current)
                        self.assertIn("director_topic_generation", playback.current.event)
                    end = Event("companion", "chat", "这个先不聊了", user="你", ts=now[0] + 1)
                    director.sync_conversation_topic(end, replace(frame, action="close"))
                    if close_during_synthesis:
                        finish.set()
                        self.assertTrue((await asyncio.wait_for(future, 1)).cancelled)
                        self.assertFalse(any(item["type"] == "audio" for item in messages))
                    else:
                        replay = playback.redispatch_current()
                        self.assertFalse(any(item["type"] == "audio" for item in replay))
                    self.assertIsNone(playback.current)
                    self.assertEqual(playback.queue, [])
                finally:
                    finish.set()
                    await pipeline.close()
                    await asyncio.gather(worker, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
