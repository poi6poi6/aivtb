from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from uuid import uuid5

from importlib.util import find_spec

if find_spec('cognitive_os') is None:
    raise unittest.SkipTest('Optional cognitive_os dependency is not installed')

from cognitive_os.domain.enums import ActionStatus, EventStatus, MemoryKind

from brain.cognition import CognitiveBridge
from brain.config import CognitionConfig, PersonaConfig
from brain.events import Event


class CognitionDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cfg = CognitionConfig(enabled=True, database_path=str(self.root / "audit.db"))
        self.persona = PersonaConfig(name="离线审计测试")
        self.bridge = await self.open_bridge()
        self.outcomes = Mock(wraps=self.bridge._motivation.on_outcome)
        self.bridge._motivation.on_outcome = self.outcomes
        self.next_id = 1

    async def asyncTearDown(self):
        await self.bridge.close()
        self.temp.cleanup()

    async def open_bridge(self):
        return await CognitiveBridge.create(self.cfg, self.persona, project_root=self.root, strict=True)

    async def begin(self):
        event = Event("danmaku", "chat", "合成测试输入", user="测试观众", viewer_key="bili:7")
        event.ui_id = self.next_id
        self.next_id += 1
        return event, await self.bridge.begin_turn(event, {})

    async def complete(self, event, turn, **overrides):
        args = dict(
            emotion="平静", text="第一段。第二段。", avatar_actions=[],
            response_mode="llm", response_latency=0.1, tts_backend="test",
            audio_path="test.wav", playback_messages=[], learning_episode_id=None,
            expected_segments=2,
        )
        args.update(overrides)
        await self.bridge.complete_turn(turn, event, **args)

    async def audit(self, turn):
        return await self.bridge.runtime.memories.get(uuid5(turn.event_id, "vtuber-turn-audit"))

    async def test_synthesis_waits_for_all_actual_playback_receipts(self):
        event, turn = await self.begin()
        await self.complete(event, turn)
        self.assertEqual((await self.audit(turn)).metadata["outcome"], "queued")
        self.assertEqual((await self.bridge.runtime.events.get(turn.event_id)).status, EventStatus.PROCESSING)
        self.outcomes.assert_not_called()
        await self.bridge.record_delivery(event.ui_id, 1, True)
        await self.bridge.record_delivery(event.ui_id, 1, True)
        self.assertEqual((await self.audit(turn)).metadata["outcome"], "queued")
        await self.bridge.record_delivery(event.ui_id, 0, True)
        await self.bridge.record_delivery(event.ui_id, 0, True)
        audit = await self.audit(turn)
        self.assertEqual(audit.metadata["outcome"], "delivered")
        self.assertEqual(json.loads(audit.content)["delivery"]["played_segments"], [0, 1])
        self.assertEqual((await self.bridge.runtime.events.get(turn.event_id)).status, EventStatus.COMPLETED)
        self.assertEqual(self.outcomes.call_count, 1)
        self.assertIs(self.outcomes.call_args.args[1], True)
        actions = await self.bridge.runtime.action_runs.list_by_event(turn.event_id)
        self.assertEqual(actions[-1].action.kind, "playback.finish")
        self.assertEqual(actions[-1].result.status, ActionStatus.SUCCEEDED)

    async def test_playback_can_finish_before_synthesis_finalizer(self):
        event, turn = await self.begin()
        await self.bridge.record_delivery(event.ui_id, 0, True)
        await self.bridge.record_delivery(event.ui_id, 1, True)
        self.outcomes.assert_not_called()
        await self.complete(event, turn)
        self.assertEqual((await self.audit(turn)).metadata["outcome"], "delivered")
        self.assertEqual(self.outcomes.call_count, 1)

    async def test_failed_early_segment_cannot_be_overwritten_by_duplicate_success(self):
        event, turn = await self.begin()
        await self.bridge.record_delivery(event.ui_id, 0, False, "播放设备断开")
        await self.bridge.record_delivery(event.ui_id, 0, True)
        await self.bridge.record_delivery(event.ui_id, 1, True)
        await self.complete(event, turn)
        audit = await self.audit(turn)
        self.assertEqual(audit.metadata["outcome"], "failed")
        self.assertEqual(json.loads(audit.content)["delivery"]["failed_segments"], [0])
        self.assertIn("播放设备断开", json.loads(audit.content)["delivery"]["failure_reason"])
        self.assertIs(self.outcomes.call_args.args[1], False)

    async def test_failure_after_partial_playback_is_terminal(self):
        event, turn = await self.begin()
        await self.complete(event, turn)
        await self.bridge.record_delivery(event.ui_id, 0, True)
        await self.bridge.record_delivery(event.ui_id, 1, False, "用户跳过")
        await self.bridge.record_delivery(event.ui_id, 1, True)
        await self.complete(event, turn)
        self.assertEqual((await self.audit(turn)).metadata["outcome"], "failed")
        self.assertEqual((await self.bridge.runtime.events.get(turn.event_id)).status, EventStatus.FAILED)
        self.assertEqual(self.outcomes.call_count, 1)

    async def test_missing_or_partial_synthesis_and_incomplete_generation_are_failures(self):
        for overrides in (
            {"audio_path": None}, {"synthesis_failed": True},
            {"response_mode": "partial"}, {"response_mode": "fallback"},
        ):
            with self.subTest(overrides=overrides):
                event, turn = await self.begin()
                await self.bridge.record_delivery(event.ui_id, 0, True)
                await self.bridge.record_delivery(event.ui_id, 1, True)
                await self.complete(event, turn, **overrides)
                audit = await self.audit(turn)
                self.assertEqual(audit.metadata["outcome"], "failed")
                if overrides.get("response_mode") in {"partial", "fallback"}:
                    self.assertEqual(json.loads(audit.content)["delivery"]["generation"], "failed")
                    self.assertEqual(json.loads(audit.content)["delivery"]["synthesis"], "complete")
                self.assertIs(self.outcomes.call_args.args[1], False)

    async def test_mock_is_text_only_without_success_reward(self):
        event, turn = await self.begin()
        await self.complete(event, turn, tts_backend="mock", audio_path=None)
        audit = await self.audit(turn)
        self.assertEqual(audit.metadata["outcome"], "text_only")
        self.assertEqual(json.loads(audit.content)["delivery"]["synthesis"], "mock")
        self.assertEqual((await self.bridge.runtime.events.get(turn.event_id)).status, EventStatus.COMPLETED)
        self.outcomes.assert_not_called()

    async def test_abort_retires_pending_audit_and_late_receipts(self):
        event, turn = await self.begin()
        await self.complete(event, turn)
        await self.bridge.abort_turn(turn, "被更高优先级事件打断")
        await self.bridge.record_delivery(event.ui_id, 0, True)
        await self.bridge.record_delivery(event.ui_id, 1, True)
        await self.bridge.abort_turn(turn, "重复取消")
        self.assertEqual((await self.audit(turn)).metadata["outcome"], "failed")
        self.assertEqual(self.outcomes.call_count, 1)

    async def test_abort_before_finalizer_does_not_resurrect_audit(self):
        event, turn = await self.begin()
        await self.bridge.abort_turn(turn, "取消生成")
        await self.complete(event, turn)
        await self.bridge.record_delivery(event.ui_id, 0, True)
        self.assertIsNone(await self.audit(turn))
        self.assertEqual((await self.bridge.runtime.events.get(turn.event_id)).status, EventStatus.FAILED)
        self.assertEqual(self.outcomes.call_count, 1)

    async def test_unknown_and_out_of_range_receipts_cannot_complete_a_turn(self):
        await self.bridge.record_delivery(123456, 0, True)
        event, turn = await self.begin()
        await self.complete(event, turn)
        await self.bridge.record_delivery(event.ui_id, -1, False)
        await self.bridge.record_delivery(event.ui_id, 99, False)
        await self.bridge.record_delivery(event.ui_id, True, False)
        await self.bridge.record_delivery(event.ui_id, 0, True)
        self.assertEqual((await self.audit(turn)).metadata["outcome"], "queued")
        self.outcomes.assert_not_called()

    async def test_close_persists_failure_for_unconfirmed_playback(self):
        event, turn = await self.begin()
        await self.complete(event, turn)
        await self.bridge.close()
        self.bridge = await self.open_bridge()
        self.assertEqual((await self.audit(turn)).metadata["outcome"], "failed")
        self.assertEqual((await self.bridge.runtime.events.get(turn.event_id)).status, EventStatus.FAILED)

    async def test_restart_retires_unconfirmed_audit_after_abrupt_shutdown(self):
        event, turn = await self.begin()
        await self.complete(event, turn)
        # Simulate a process exit with no graceful Bridge.close().
        await self.bridge.runtime.close()
        self.bridge.runtime = None
        self.bridge = await self.open_bridge()
        audit = await self.audit(turn)
        self.assertEqual(audit.metadata["outcome"], "failed")
        self.assertIn("上次进程", json.loads(audit.content)["delivery"]["failure_reason"])

    async def test_begin_timeout_cleans_already_appended_event_without_disabling(self):
        entered = asyncio.Event()
        original = self.bridge._memory.remember

        async def slow(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        self.bridge._memory.remember = AsyncMock(side_effect=slow)
        pending = asyncio.create_task(self.begin())
        await entered.wait()
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertEqual(await self.bridge.runtime.events.list_unfinished(), [])
        self.assertTrue(self.bridge.enabled)
        self.bridge._memory.remember = original
        _, turn = await self.begin()
        self.assertIsNotNone(turn)

    async def test_context_read_does_not_wait_for_audit_lock(self):
        await self.bridge._lock.acquire()
        try:
            result = await asyncio.wait_for(self.bridge.context_for(Event("companion", "chat", "测试")), 1)
            self.assertEqual(result, "")
        finally:
            self.bridge._lock.release()

    async def test_context_timeout_keeps_bridge_and_audit_working(self):
        entered = asyncio.Event()
        original = self.bridge.runtime.memories.search

        async def slow(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        self.bridge.runtime.memories.search = AsyncMock(side_effect=slow)
        task = asyncio.create_task(self.bridge.context_for(Event("companion", "chat", "测试")))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.bridge.runtime.memories.search = original
        self.assertTrue(self.bridge.enabled)
        _, turn = await self.begin()
        self.assertIsNotNone(turn)

    async def test_context_read_failure_reports_unavailable_without_disabling(self):
        self.bridge.strict = False
        original = self.bridge.runtime.memories.search
        self.bridge.runtime.memories.search = AsyncMock(side_effect=OSError("synthetic unavailable"))
        context = await self.bridge.context_for(Event("companion", "chat", "测试"))
        self.assertEqual(context, "")
        self.assertEqual(self.bridge.last_context_status, "unavailable")
        self.assertTrue(self.bridge.enabled)
        self.bridge.runtime.memories.search = original
        self.assertEqual(await self.bridge.context_for(Event("companion", "chat", "测试")), "")
        self.assertEqual(self.bridge.last_context_status, "empty")
        _, turn = await self.begin()
        self.assertIsNotNone(turn)

    async def test_context_requires_active_explicit_scope_and_matching_identity(self):
        records = [
            ("自己的事实", {"recallable": True, "viewer_key": "bili:7", "source": "danmaku"}),
            ("其他账号事实", {"recallable": True, "viewer_key": "bili:8", "source": "danmaku"}),
            ("跨来源相同账号事实", {"recallable": True, "viewer_key": "bili:7", "source": "game"}),
            ("缺来源账号事实", {"recallable": True, "viewer_key": "bili:7"}),
            ("历史同名事实", {"recallable": True, "user": "测试观众", "source": "danmaku"}),
            ("无归属旧记忆", {"recallable": True}),
            ("公开事实", {"recallable": True, "visibility": "public"}),
            ("失效事实", {"recallable": True, "scope": "global", "active": False}),
            ("已更正事实", {"recallable": True, "scope": "global", "status": "corrected"}),
            ("旧角色身份", {"recallable": True, "scope": "global", "type": "persona_identity"}),
            ("审计原文", {"recallable": True, "scope": "global", "type": "turn_audit"}),
        ]
        for text, metadata in records:
            await self.bridge._memory.remember(MemoryKind.SEMANTIC, text, salience=.8, metadata=metadata)
        context = await self.bridge.context_for(Event("danmaku", "chat", "事实", user="测试观众", viewer_key="bili:7"), limit=8)
        self.assertIn("自己的事实", context)
        self.assertIn("公开事实", context)
        for text, _ in records:
            if text not in {"自己的事实", "公开事实"}:
                self.assertNotIn(text, context)

    async def test_delivery_snapshot_contains_only_counts(self):
        event, turn = await self.begin()
        await self.complete(event, turn)
        await self.bridge.record_delivery(event.ui_id, 0, True)
        snapshot = self.bridge.delivery_snapshot()
        self.assertEqual(snapshot, {
            "turn_id": event.ui_id, "outcome": "queued", "expected_segments": 2,
            "completed_segments": 1, "failed_segments": 0,
        })
        snapshot["outcome"] = "tampered"
        self.assertEqual(self.bridge.delivery_snapshot()["outcome"], "queued")

    async def test_begin_cancellation_cleanup_has_its_own_budget(self):
        entered = asyncio.Event()
        original_remember = self.bridge._memory.remember
        original_status = self.bridge.runtime.events.set_status

        async def slow_remember(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        async def slow_failure(event_id, status, error=None):
            if status == EventStatus.FAILED:
                await asyncio.Event().wait()
            return await original_status(event_id, status, error)

        self.bridge._memory.remember = AsyncMock(side_effect=slow_remember)
        self.bridge.runtime.events.set_status = slow_failure
        task = asyncio.create_task(self.begin())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, .8)
        self.assertTrue(self.bridge.enabled)
        self.assertEqual(len(await self.bridge.runtime.events.list_unfinished()), 1)
        self.bridge._memory.remember = original_remember
        self.bridge.runtime.events.set_status = original_status
        await self.bridge.close()
        self.bridge = await self.open_bridge()
        self.assertEqual(await self.bridge.runtime.events.list_unfinished(), [])


if __name__ == "__main__":
    unittest.main()
