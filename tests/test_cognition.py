from __future__ import annotations

import tempfile
import json
import unittest
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid5

from importlib.util import find_spec

if find_spec('cognitive_os') is None:
    raise unittest.SkipTest('Optional cognitive_os dependency is not installed')

from cognitive_os.domain.enums import CognitivePhase, EventStatus, GoalStatus, MemoryKind
from cognitive_os.domain.models import MemoryQuery

from brain.cognition import CognitiveBridge
from brain.config import CognitionConfig, PersonaConfig
from brain.events import Event


class CognitiveBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bridge = await CognitiveBridge.create(
            CognitionConfig(
                enabled=True,
                database_path=str(self.root / "data" / "cognition.db"),
            ),
            PersonaConfig(
                name="测试助手",
                species="在测试直播间成长的 AI 主播",
                tone="温柔、诚实",
                speaking_style="简短自然地回应。",
                rules=["不编造", "安全优先"],
            ),
            project_root=self.root,
            strict=True,
        )

    async def asyncTearDown(self) -> None:
        await self.bridge.close()
        self.temp.cleanup()

    async def test_records_identity_goal_turn_actions_and_layered_memories(self) -> None:
        event = Event(
            "danmaku",
            "chat",
            "助手最喜欢什么甜点？",
            user="小明",
            priority=80,
        )
        event.ui_id = 7
        turn = await self.bridge.begin_turn(
            event,
            {"phase": "chatting", "phase_label": "日常闲聊"},
        )
        self.assertIsNotNone(turn)

        await self.bridge.complete_turn(
            turn,
            event,
            emotion="开心",
            text="我喜欢草莓蛋糕，酸酸甜甜的很适合直播间。",
            avatar_actions=["脸红"],
            response_mode="llm",
            response_latency=0.2,
            tts_backend="mock",
            audio_path=None,
            playback_messages=[],
            learning_episode_id=12,
        )

        runtime = self.bridge.runtime
        self.assertIsNotNone(runtime)
        stored = await runtime.events.get(turn.event_id)
        self.assertEqual(stored.status, EventStatus.COMPLETED)
        self.assertEqual(stored.payload["source"], "danmaku")
        self.assertAlmostEqual(stored.priority, 0.8)

        action_runs = await runtime.action_runs.list_by_event(turn.event_id)
        self.assertEqual(
            [run.action.kind for run in action_runs],
            ["reply.generate", "avatar.show", "speech.synthesize"],
        )
        self.assertEqual(action_runs[0].result.environment_changes["emotion"], "开心")

        identity = await runtime.identities.get()
        self.assertEqual(identity.name, "测试助手")
        self.assertIn("persona.yaml", identity.narrative)
        goals = await runtime.goals.list(GoalStatus.ACTIVE)
        self.assertTrue(any("完成直播" in goal.description for goal in goals))

        memories = await runtime.memories.search(
            MemoryQuery(text="小明甜点草莓蛋糕", limit=20)
        )
        kinds = {memory.kind for memory in memories}
        self.assertTrue(
            {MemoryKind.WORKING, MemoryKind.EPISODIC, MemoryKind.SEMANTIC, MemoryKind.PROCEDURAL}
            <= kinds
        )
        episode = next(memory for memory in memories if memory.kind == MemoryKind.EPISODIC)
        self.assertEqual(episode.metadata["learning_episode_id"], 12)

        state = await runtime.states.get()
        self.assertEqual(state.phase, CognitivePhase.IDLE)
        self.assertEqual(state.iteration, 1)

    async def test_disabled_bridge_is_a_noop(self) -> None:
        disabled = await CognitiveBridge.create(
            CognitionConfig(enabled=False, database_path=str(self.root / "disabled.db")),
            PersonaConfig(),
            project_root=self.root,
            strict=True,
        )
        try:
            self.assertFalse(disabled.enabled)
            turn = await disabled.begin_turn(Event("system", "idle", "test"), {})
            self.assertIsNone(turn)
        finally:
            await disabled.close()

    async def test_unready_real_backend_records_text_without_speech_delivery_success(self) -> None:
        event = Event('companion', 'chat', '语音加载时用文字回复。', user='离线观众')
        event.ui_id = 32
        event.text_only_delivery = True
        turn = await self.bridge.begin_turn(event, {})
        outcomes = Mock(wraps=self.bridge._motivation.on_outcome)
        self.bridge._motivation.on_outcome = outcomes
        await self.bridge.complete_turn(
            turn, event, emotion='平静', text='第一段文字。第二段文字。', avatar_actions=[],
            response_mode='llm', response_latency=.1, tts_backend='indextts2',
            audio_path=None, playback_messages=[], learning_episode_id=None,
            expected_segments=0, synthesis_failed=False,
        )
        runtime = self.bridge.runtime
        audit = await runtime.memories.get(uuid5(turn.event_id, 'vtuber-turn-audit'))
        details = json.loads(audit.content)['delivery']
        self.assertEqual(audit.metadata['outcome'], 'text_only')
        self.assertEqual(details['synthesis'], 'text_only')
        self.assertEqual(details['outcome'], 'text_only')
        self.assertIsNone(details['audio_path'])
        self.assertEqual(details['played_segments'], [])
        self.assertEqual(details['expected_segments'], 0)
        self.assertEqual(details['playback_messages'], [])
        self.assertEqual((await runtime.events.get(turn.event_id)).status, EventStatus.COMPLETED)
        actions = await runtime.action_runs.list_by_event(turn.event_id)
        self.assertFalse(any(action.action.kind.startswith('playback.') for action in actions))
        synthesis = next(action for action in actions if action.action.kind == 'speech.synthesize')
        self.assertTrue(synthesis.result.environment_changes['text_only'])
        self.assertEqual(synthesis.result.environment_changes['audio_path'], '')
        outcomes.assert_not_called()
        # Late or duplicate playback receipts cannot convert this text turn to spoken success.
        await self.bridge.record_delivery(event.ui_id, 0, True)
        audit = await runtime.memories.get(uuid5(turn.event_id, 'vtuber-turn-audit'))
        self.assertEqual(audit.metadata['outcome'], 'text_only')
        outcomes.assert_not_called()


if __name__ == "__main__":
    unittest.main()
