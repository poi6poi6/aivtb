"""Poll participation integration without live services or per-vote model calls."""
import ast
import asyncio
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from brain.config import DirectorConfig
from brain.director import LiveDirector
from brain.events import Event
from brain.live_polls import LivePolls


class PollDirectorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = 1000.0
        self.polls = LivePolls(Path(self.tmp.name) / "poll.sqlite", clock=lambda: self.now)
        self.director = LiveDirector(DirectorConfig(enabled=True, proactive_interval_sec=600),
                                     clock=lambda: self.now)
        self.poll = self.create()

    def create(self, mode="live"):
        return self.polls.create(program="庭院连载", question="下一次为庭院添哪一处？",
                                 options=["温室", "书房"], mode=mode, duration=60)

    def announce(self, *, pending=False):
        event = self.polls.announcement_event(self.poll["id"])
        if pending:
            event.ui_id = 42
        self.director.after_reply(event, "平静", event.agent_reply)
        return event

    def ballot(self, key="bili:1", text="投票 1", **kwargs):
        self.now += 1
        return Event("danmaku", "chat", text, user="同名观众", viewer_key=key,
                     ts=self.now, **kwargs)

    def deliver(self, event):
        receipt = self.polls.handle_event(event)
        self.director.observe_poll_vote(event, receipt)
        return receipt

    async def test_real_ingestion_counts_votes_and_updates_director_without_chat_reply(self):
        self.announce()
        queue = asyncio.Queue()
        register = AsyncMock()
        root = Path(__file__).resolve().parents[1]
        tree = ast.parse((root / "brain/main.py").read_text(encoding="utf-8"))
        ingest = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.AsyncFunctionDef) and n.name == "ingest_events")
        namespace = {"queue": queue, "live_polls": self.polls, "director": self.director,
                     "webui": None, "register_event": register, "sqlite3": sqlite3, "time": time}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[ingest], type_ignores=[])),
                     "<production ingest_events>", "exec"), namespace)
        task = asyncio.create_task(namespace["ingest_events"]())
        try:
            for i in range(3):
                await queue.put(self.ballot(f"bili:{i}"))
            await asyncio.wait_for(queue.join(), timeout=2)
            self.assertEqual(self.polls.status()["current"]["total"], 3)
            state = self.director.summary()
            self.assertEqual(state["active_now"], 3)
            self.assertGreater(state["chat_rate"], 0)
            self.assertFalse(state["waiting_for_audience"])
            self.assertEqual(state["successful_interactions"], 1)
            self.assertEqual(self.director.total_turns, 1)
            register.assert_not_awaited()
            self.now += 65
            self.assertIsNone(self.director.poll_proactive())
            self.assertEqual(self.director.missed_interactions, 0)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_invalid_duplicate_replay_and_test_votes_do_not_inflate_activity(self):
        self.announce()
        self.deliver(self.ballot(text="投票 9"))
        self.deliver(self.ballot(key=""))
        self.deliver(self.ballot(forced=True))
        self.assertEqual(self.director.summary()["active_now"], 0)
        self.assertTrue(self.director.awaiting_audience)
        accepted = self.ballot()
        self.deliver(accepted)
        self.deliver(accepted)
        self.deliver(self.ballot())  # a newer duplicate selection is not new participation
        self.assertEqual(len(self.director._events), 1)
        self.deliver(self.ballot(text="投票 2"))
        self.assertEqual(len(self.director._events), 2)
        self.assertEqual(self.director.successful_interactions, 1)
        self.polls.finish(self.poll["id"])
        self.poll = self.create(mode="test")
        self.announce()
        event = self.ballot(forced=True)
        event.owner_control = True
        self.assertTrue(self.deliver(event)["ok"])
        self.assertEqual(len(self.director._events), 2)
        self.assertEqual(self.director.successful_interactions, 1)

    async def test_votes_keep_unrelated_question_waiting(self):
        self.director.set_phase("interaction")
        self.director.after_reply(Event("system", "idle", "邀请"), "平静", "大家想画什么花？")
        self.announce()
        self.deliver(self.ballot())
        self.assertEqual(self.director.summary()["active_now"], 1)
        self.assertEqual(self.director.audience_question, "大家想画什么花？")
        self.assertEqual(self.director.audience_poll_id, "")
        self.assertTrue(self.director.awaiting_audience)
        self.assertEqual(self.director.successful_interactions, 0)

    async def test_only_matching_poll_receipts_answer_and_plain_chat_does_not_vote(self):
        self.announce()
        self.director.observe_event(self.ballot(text="温室"))
        self.assertTrue(self.director.awaiting_audience)
        event = self.ballot()
        receipt = self.polls.handle_event(event)
        self.director.observe_poll_vote(event, {**receipt, "poll_id": "different-round"})
        self.assertTrue(self.director.awaiting_audience)
        self.director.observe_poll_vote(event, receipt)
        self.assertFalse(self.director.awaiting_audience)

    async def test_valid_vote_during_audio_pending_or_before_announcement_is_kept(self):
        self.announce(pending=True)
        self.assertTrue(self.director.question_pending)
        self.deliver(self.ballot())
        self.assertFalse(self.director.question_pending)
        self.assertFalse(self.director.mark_audio_started(42))
        self.announce(pending=True)
        self.assertFalse(self.director.question_pending)
        self.assertEqual(self.director.successful_interactions, 1)
        self.polls.finish(self.poll["id"])
        self.poll = self.create()
        # Announcement queued before a vote but processed after it.
        queued = self.polls.announcement_event(self.poll["id"])
        self.deliver(self.ballot())
        self.director.after_reply(queued, "平静", queued.agent_reply)
        self.assertFalse(self.director.awaiting_audience)
        self.assertEqual(self.director.successful_interactions, 2)

    async def test_poll_timeout_uses_poll_deadline_and_finished_announcements_do_not_ask_again(self):
        self.announce()
        self.now += 30
        self.assertIsNone(self.director.poll_proactive())
        self.assertTrue(self.director.awaiting_audience)
        self.now = self.poll["deadline"]
        self.assertIn("暂时没人接", self.director.poll_proactive().text)
        self.assertEqual(self.director.missed_interactions, 1)
        self.announce()
        self.assertFalse(self.director.awaiting_audience)

    async def test_disabled_director_does_not_change_its_state(self):
        self.director = LiveDirector(DirectorConfig(enabled=False), clock=lambda: self.now)
        before = self.director.summary()
        self.announce()
        self.assertTrue(self.deliver(self.ballot())["ok"])
        self.assertEqual(self.director.summary(), before)
        self.assertEqual(self.polls.status()["current"]["total"], 1)
