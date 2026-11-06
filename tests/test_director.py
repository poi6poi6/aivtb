from __future__ import annotations

import unittest

from brain.config import DirectorConfig
from brain.director import LiveDirector
from brain.events import Event


class LiveDirectorTests(unittest.TestCase):
    def test_moves_from_opening_to_chat_and_group_interaction(self) -> None:
        director = LiveDirector(DirectorConfig(
            opening_turns=1,
            interaction_every_turns=2,
            interaction_turns=1,
            interaction_min_viewers=2,
        ))
        first = Event("danmaku", "chat", "晚上好", user="小明")
        director.observe_event(first)
        director.prepare(first)
        self.assertEqual(director.phase, "opening")
        director.after_reply(first, "开心", "晚上好呀")
        self.assertEqual(director.phase, "chatting")

        for text, user in (("最近有什么新鲜事", "小明"), ("大家今天过得怎么样", "小红")):
            event = Event("danmaku", "chat", text, user=user)
            director.observe_event(event)
            director.prepare(event)
            director.after_reply(event, "平静", "让我想想")
        self.assertEqual(director.phase, "interaction")

    def test_highlight_temporarily_interrupts_current_segment(self) -> None:
        director = LiveDirector(DirectorConfig(opening_turns=1, highlight_turns=1))
        director.after_reply(Event("system", "idle", "开场"), "开心", "开始啦")
        gift = Event("danmaku", "gift", "小心心", user="小红")
        director.observe_event(gift)
        director.prepare(gift)
        self.assertEqual(director.phase, "highlight")
        self.assertIn("高能时刻", gift.director_context)
        director.after_reply(gift, "惊讶", "谢谢你")
        self.assertEqual(director.phase, "chatting")

    def test_idle_speech_continues_a_real_topic(self) -> None:
        director = LiveDirector(DirectorConfig(opening_turns=1))
        director.after_reply(Event("system", "idle", "开场"), "开心", "开始啦")
        chat = Event("danmaku", "chat", "最近开始学习做蛋糕了", user="甜点师")
        director.observe_event(chat)
        idle = Event("system", "idle", "直播间冷场了")
        director.prepare(idle)
        self.assertIn("做蛋糕", idle.text)
        self.assertIn("主动发言", idle.director_context)
        self.assertIn("甜点师", idle.director_context)

    def test_manual_phase_lock_and_auto_restore(self) -> None:
        director = LiveDirector(DirectorConfig(opening_turns=1))
        self.assertTrue(director.set_phase("closing"))
        self.assertEqual(director.summary()["mode"], "manual")
        director.after_reply(Event("system", "idle", "收尾"), "平静", "晚安")
        self.assertEqual(director.phase, "closing")
        self.assertTrue(director.set_phase("auto"))
        self.assertEqual(director.summary()["mode"], "auto")
        self.assertEqual(director.phase, "chatting")
        self.assertFalse(director.set_phase("unknown"))

    def test_can_auto_close_after_configured_duration(self) -> None:
        now = [100.0]
        director = LiveDirector(
            DirectorConfig(closing_after_minutes=1), clock=lambda: now[0]
        )
        now[0] += 61
        event = Event("danmaku", "chat", "还在播吗", user="观众")
        director.prepare(event)
        self.assertEqual(director.phase, "closing")

    def test_schedules_an_opening_without_waiting_for_chat(self) -> None:
        now = [100.0]
        director = LiveDirector(
            DirectorConfig(opening_delay_sec=2), clock=lambda: now[0]
        )
        self.assertIsNone(director.poll_proactive())
        now[0] += 2.1
        opening = director.poll_proactive()
        self.assertIsNotNone(opening)
        self.assertEqual(opening.source, "system")
        self.assertIn("开场", opening.text)
        self.assertIsNone(director.poll_proactive())

    def test_does_not_force_group_interaction_with_one_viewer(self) -> None:
        director = LiveDirector(DirectorConfig(
            opening_turns=1,
            interaction_every_turns=2,
            interaction_min_viewers=2,
        ))
        first = Event("danmaku", "chat", "聊聊今天吧", user="唯一观众")
        director.observe_event(first)
        director.after_reply(first, "开心", "好呀")
        for index in range(4):
            event = Event("danmaku", "chat", f"继续聊聊{index}", user="唯一观众")
            director.observe_event(event)
            director.after_reply(event, "平静", "继续聊")
        self.assertEqual(director.phase, "chatting")

    def test_unanswered_interaction_times_out_and_changes_topic(self) -> None:
        now = [100.0]
        director = LiveDirector(DirectorConfig(
            opening_turns=1,
            interaction_every_turns=2,
            interaction_min_viewers=2,
            interaction_response_timeout_sec=10,
            proactive_interval_sec=60,
        ), clock=lambda: now[0])
        director.after_reply(Event("system", "idle", "开场"), "开心", "开始啦")
        for index, name in enumerate(("小明", "小红")):
            event = Event("danmaku", "chat", f"热闹话题{index}", user=name)
            director.observe_event(event)
            director.after_reply(event, "平静", "接着聊")
        self.assertEqual(director.phase, "interaction")
        question = Event("system", "idle", "问问大家")
        director.prepare(question)
        director.after_reply(question, "开心", "大家更喜欢哪一个呢")
        self.assertTrue(director.awaiting_audience)

        now[0] += 11
        transition = director.poll_proactive()
        self.assertIsNotNone(transition)
        self.assertIn("不要责怪观众", transition.text)
        self.assertEqual(director.phase, "chatting")
        self.assertEqual(director.summary()["missed_interactions"], 1)

    def test_real_audience_reply_completes_interaction(self) -> None:
        now = [100.0]
        director = LiveDirector(DirectorConfig(), clock=lambda: now[0])
        director.set_phase("auto")
        director.phase = "interaction"
        director.after_reply(Event("system", "idle", "问大家"), "开心", "选A还是B呢")
        self.assertTrue(director.awaiting_audience)
        answer = Event("danmaku", "chat", "我选A", user="小明")
        now[0] += 1
        director.observe_event(answer)
        self.assertFalse(director.awaiting_audience)
        director.after_reply(answer, "开心", "原来你选A呀")
        self.assertEqual(director.phase, "chatting")
        self.assertEqual(director.summary()["successful_interactions"], 1)

    def test_response_timeout_starts_when_audio_really_plays(self) -> None:
        now = [100.0]
        director = LiveDirector(DirectorConfig(), clock=lambda: now[0])
        director.phase = "interaction"
        question = Event("system", "idle", "问大家")
        question.ui_id = 42
        director.after_reply(question, "开心", "今天想聊什么呢")
        self.assertTrue(director.question_pending)
        self.assertFalse(director.awaiting_audience)
        now[0] += 30
        self.assertFalse(director.mark_audio_started(41))
        self.assertTrue(director.mark_audio_started(42))
        self.assertTrue(director.awaiting_audience)
        self.assertEqual(director.audience_question_at, now[0])

    def test_flower_question_survives_feedback_and_unrelated_chat(self) -> None:
        now = [100.0]
        director = LiveDirector(DirectorConfig(topic_max_turns=2), clock=lambda: now[0])
        director.phase = "interaction"
        director.after_reply(Event("system", "idle", "邀请画花"), "平静", "你们想画什么花？")
        question_at = director.audience_question_at
        for text in ("声音太小", "今天花了很多钱", "刚才打游戏输了"):
            now[0] += 1
            event = Event("danmaku", "chat", text, user="p***", viewer_key="uid:1")
            director.observe_event(event)
            director.prepare(event)
            self.assertIn("你们想画什么花", event.director_context)
            if text == "声音太小":
                self.assertIn("设备或播放反馈", event.director_context)
                self.assertIn("不要声称已调音量或修好", event.director_context)
            director.after_reply(event, "平静", "我听见了。")
            self.assertTrue(director.awaiting_audience)
            self.assertEqual(director.current_topic, "你们想画什么花？")
            self.assertEqual(director.audience_question_at, question_at)
            self.assertEqual(director.successful_interactions, 0)
        answer = Event("danmaku", "chat", "画一朵玫瑰吧", user="p***", viewer_key="uid:2")
        director.observe_event(answer)
        self.assertFalse(director.awaiting_audience)
        self.assertEqual(director.successful_interactions, 1)
        director.after_reply(answer, "平静", "好，一朵玫瑰。")
        self.assertEqual(director.phase, "chatting")

    def test_unrelated_chat_does_not_extend_answer_timeout(self) -> None:
        now = [100.0]
        director = LiveDirector(DirectorConfig(interaction_response_timeout_sec=10), clock=lambda: now[0])
        director.phase = "interaction"
        director.after_reply(Event("system", "idle", "邀请"), "平静", "大家想画什么花？")
        now[0] += 9
        feedback = Event("danmaku", "chat", "听不到声音", user="观众")
        director.observe_event(feedback)
        director.after_reply(feedback, "平静", "我还不能确认音频输出。")
        now[0] += 2
        self.assertIn("暂时没人接", director.poll_proactive().text)
        self.assertEqual(director.missed_interactions, 1)
        self.assertEqual(director.successful_interactions, 0)
        self.assertEqual(director.audience_question, "")

    def test_choices_and_semantic_short_answers(self) -> None:
        for question, answer in (
            ("画玫瑰还是百合呢？", "百合"),
            ("选A还是B呢", "b"),
            ("今天心情怎么样？", "有点累"),
            ("裙子用什么颜色？", "薄荷绿"),
            ("今天想聊什么呢？", "最近看的小说"),
            ("你对量子纠缠有什么看法？", "量子纠缠很有趣"),
        ):
            with self.subTest(question=question):
                director = LiveDirector(DirectorConfig())
                director.phase = "interaction"
                director.after_reply(Event("system", "idle", "邀请"), "平静", question)
                director.observe_event(Event("danmaku", "chat", answer, user="观众"))
                self.assertFalse(director.awaiting_audience)
                self.assertEqual(director.successful_interactions, 1)

    def test_unclear_or_followup_question_does_not_claim_success(self) -> None:
        director = LiveDirector(DirectorConfig())
        director.phase = "interaction"
        director.after_reply(Event("system", "idle", "邀请"), "平静", "你对量子纠缠有什么看法？")
        for text in ("你好", "我不太懂你说的", "量子纠缠是什么？"):
            event = Event("danmaku", "chat", text, user="观众")
            director.observe_event(event)
            director.prepare(event)
            self.assertTrue(director.awaiting_audience)
            self.assertIn("量子纠缠", event.director_context)
        self.assertEqual(director.successful_interactions, 0)

    def test_only_actual_question_starts_waiting(self) -> None:
        director = LiveDirector(DirectorConfig())
        director.phase = "interaction"
        director.after_reply(Event("system", "idle", "向观众提问"), "平静", "我先画完这根线。")
        self.assertFalse(director.awaiting_audience)
        self.assertFalse(director.question_pending)
        director.after_reply(Event("system", "idle", "邀请"), "平静", "我想在旁边种花。你们喜欢玫瑰还是百合？")
        self.assertEqual(director.audience_question, "你们喜欢玫瑰还是百合？")

    def test_feedback_before_audio_keeps_pending_question(self) -> None:
        director = LiveDirector(DirectorConfig())
        director.phase = "interaction"
        question = Event("system", "idle", "邀请")
        question.ui_id = 42
        director.after_reply(question, "平静", "画玫瑰还是百合？")
        for text in ("听不到", "玫瑰"):
            director.observe_event(Event("danmaku", "chat", text, user="观众"))
        self.assertTrue(director.question_pending)
        self.assertEqual(director.audience_question_id, 42)
        self.assertEqual(director.successful_interactions, 0)
        director.mark_audio_started(42)
        director.observe_event(Event("danmaku", "chat", "玫瑰", user="观众"))
        self.assertEqual(director.successful_interactions, 1)
        director.observe_event(Event("danmaku", "chat", "百合", user="另一位观众"))
        self.assertEqual(director.successful_interactions, 1)

    def test_phase_exit_clears_question_but_highlight_preserves_it(self) -> None:
        director = LiveDirector(DirectorConfig(highlight_turns=1))
        director.phase = "interaction"
        director.after_reply(Event("system", "idle", "邀请"), "平静", "画什么花？")
        gift = Event("danmaku", "gift", "小心心", user="观众")
        director.observe_event(gift)
        director.prepare(gift)
        director.after_reply(gift, "开心", "谢谢。")
        self.assertEqual(director.phase, "interaction")
        self.assertTrue(director.awaiting_audience)
        self.assertEqual(director.audience_question, "画什么花？")
        director.set_phase("closing")
        self.assertFalse(director.awaiting_audience)
        director.observe_event(Event("danmaku", "chat", "玫瑰", user="观众"))
        self.assertEqual(director.successful_interactions, 0)

    def test_viewer_keys_distinguish_masked_names_without_speaking_ids(self) -> None:
        director = LiveDirector(DirectorConfig())
        for key, name in (("uid:1", "p***"), ("uid:2", "p***"), ("uid:1", "新昵称")):
            director.observe_event(Event("danmaku", "chat", "你好", user=name, viewer_key=key))
        summary = director.summary()
        self.assertEqual(summary["active_now"], 2)
        self.assertCountEqual(summary["active_viewers"], ["p***", "新昵称"])
        event = Event("system", "idle", "邀请")
        director.prepare(event)
        self.assertNotIn("uid:", event.director_context)
        self.assertIn("新昵称", event.director_context)


if __name__ == "__main__":
    unittest.main()
