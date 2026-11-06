"""Persistent memory and audit bridge for the live VTuber pipeline.

The bridge retrieves compact durable context for Persona while leaving final
wording, attention decisions and game control with their dedicated components.
It also records selected turns and actions so the cognitive kernel can retain
identity, goals, layered memories and an auditable event history.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4, uuid5

from .config import ROOT, CognitionConfig, PersonaConfig
from .events import Event
from .viewer_identity import viewer_identity


@dataclass(frozen=True)
class CognitiveTurn:
    event_id: UUID
    plan_id: UUID
    started_at: datetime
    priority: float


@dataclass
class _DeliveryAudit:
    turn: CognitiveTurn
    ui_id: int | None
    receipts: dict[int, bool] = field(default_factory=dict)
    reasons: dict[int, str] = field(default_factory=dict)
    expected_segments: int | None = None
    finalized: bool = False
    synthesis_failed: bool = False
    generation_failed: bool = False
    text_only: bool = False
    audit: Any | None = None
    document: dict[str, Any] = field(default_factory=dict)
    terminal: str = ""


def _load_cognitive_os() -> SimpleNamespace:
    """Load the optional local package only when cognition is enabled."""

    try:
        import cognitive_os  # noqa: F401
    except ModuleNotFoundError:
        # Keep this checked-out workspace runnable before the user reinstalls
        # requirements; packaged deployments use the declared local dependency.
        sibling_source = ROOT.parent / "构想" / "src"
        if sibling_source.is_dir():
            sys.path.insert(0, str(sibling_source))

    from cognitive_os.application.attention import MotivationRegulator
    from cognitive_os.application.memory import MemoryService
    from cognitive_os.bootstrap import build_runtime
    from cognitive_os.config import Settings
    from cognitive_os.domain.enums import (
        ActionStatus,
        CognitivePhase,
        EventKind,
        EventStatus,
        GoalStatus,
        MemoryKind,
    )
    from cognitive_os.domain.models import (
        Action,
        ActionResult,
        CognitiveEvent,
        Goal,
        MemoryQuery,
        MemoryRecord,
        utc_now,
    )

    return SimpleNamespace(
        Action=Action,
        ActionResult=ActionResult,
        ActionStatus=ActionStatus,
        CognitiveEvent=CognitiveEvent,
        CognitivePhase=CognitivePhase,
        EventKind=EventKind,
        EventStatus=EventStatus,
        Goal=Goal,
        GoalStatus=GoalStatus,
        MemoryKind=MemoryKind,
        MemoryQuery=MemoryQuery,
        MemoryRecord=MemoryRecord,
        MemoryService=MemoryService,
        MotivationRegulator=MotivationRegulator,
        Settings=Settings,
        build_runtime=build_runtime,
        utc_now=utc_now,
    )


class CognitiveBridge:
    """Fail-open adapter from VTuber turns to the persistent cognitive stores."""

    def __init__(
        self,
        cfg: CognitionConfig,
        persona: PersonaConfig,
        *,
        strict: bool = False,
    ) -> None:
        self.cfg = cfg
        self.persona = persona
        self.strict = strict
        self.runtime: Any | None = None
        self._cog: SimpleNamespace | None = None
        self._memory: Any | None = None
        self._motivation: Any | None = None
        self._lock = asyncio.Lock()
        self._read_lock = asyncio.Lock()
        self._active: dict[UUID, CognitiveTurn] = {}
        self._deliveries: dict[UUID, _DeliveryAudit] = {}
        self._ui_turns: dict[int, UUID] = {}
        self._last_delivery: dict[str, Any] = {}
        self._known_procedures: set[str] = set()
        self.last_error = ""
        self.last_context_status = "idle"

    @classmethod
    async def create(
        cls,
        cfg: CognitionConfig,
        persona: PersonaConfig,
        *,
        project_root: Path = ROOT,
        strict: bool = False,
    ) -> CognitiveBridge:
        bridge = cls(cfg, persona, strict=strict)
        if not cfg.enabled:
            return bridge
        try:
            bridge._cog = _load_cognitive_os()
            database_path = Path(cfg.database_path).resolve()
            database_path.parent.mkdir(parents=True, exist_ok=True)
            settings = bridge._cog.Settings(
                database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
                workspace=project_root.resolve(),
                llm_enabled=False,
                allow_shell=False,
            )
            bridge.runtime = await bridge._cog.build_runtime(settings)
            bridge._memory = bridge._cog.MemoryService(bridge.runtime.memories)
            bridge._motivation = bridge._cog.MotivationRegulator()
            await bridge._recover_interrupted_turns()
            await bridge._sync_persona_identity()
            await bridge._ensure_livestream_goal()
            print(f"[cognition] 持久认知已启用：{database_path}")
        except Exception as exc:
            await bridge._disable("初始化", exc)
        return bridge

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.enabled and self.runtime is not None and self._cog is not None)

    def summary(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "configured": self.cfg.enabled,
            "database": self.cfg.database_path if self.cfg.enabled else "",
            "error": self.last_error[:180],
        }

    def delivery_snapshot(self) -> dict[str, Any]:
        """Expose receipt counts only, without user text, identities or file paths."""
        return dict(self._last_delivery)

    def _snapshot_delivery(self, delivery: _DeliveryAudit, outcome: str) -> None:
        received = delivery.receipts if delivery.expected_segments is None else {
            index: completed for index, completed in delivery.receipts.items()
            if index < delivery.expected_segments
        }
        self._last_delivery = {
            "turn_id": delivery.ui_id,
            "outcome": outcome,
            "expected_segments": delivery.expected_segments,
            "completed_segments": sum(bool(completed) for completed in received.values()),
            "failed_segments": sum(not completed for completed in received.values()),
        }

    async def begin_turn(self, event: Event, director_state: dict[str, Any]) -> CognitiveTurn | None:
        if not self.enabled:
            return None
        async with self._lock:
            cognitive_event = None
            try:
                assert self._cog is not None and self.runtime is not None and self._memory is not None
                priority = max(0.0, min(1.0, float(event.priority) / 100.0))
                payload = {
                    "request": event.text,
                    "source": event.source,
                    "kind": event.kind,
                    "user": event.user,
                    "viewer_key": event.viewer_key,
                    "forced": event.forced,
                    "director_context": event.director_context,
                    "director": self._json_safe(director_state),
                    "ui_id": getattr(event, "ui_id", None),
                }
                cognitive_event = self._cog.CognitiveEvent(
                    kind=self._cog.EventKind.EXTERNAL_INPUT,
                    payload=payload,
                    priority=priority,
                    created_at=datetime.fromtimestamp(event.ts, tz=timezone.utc),
                )
                await self.runtime.events.append(cognitive_event)
                await self.runtime.events.set_status(
                    cognitive_event.id, self._cog.EventStatus.PROCESSING
                )
                working = await self._memory.remember(
                    self._cog.MemoryKind.WORKING,
                    json.dumps(payload, ensure_ascii=False, default=str),
                    summary=f"待处理直播事件：{event.kind} · {event.text[:80]}",
                    salience=max(0.2, priority),
                    confidence=1.0,
                    source_event_id=cognitive_event.id,
                    metadata={"source": event.source, "event_kind": event.kind},
                )
                state = await self.runtime.states.get()
                if state is not None:
                    state = self._motivation.on_event(state, cognitive_event)
                    state.phase = self._cog.CognitivePhase.EXECUTING
                    state.last_event_id = cognitive_event.id
                    state.attention_score = priority
                    state.working_memory_ids = (state.working_memory_ids + [working.id])[-20:]
                    state.updated_at = self._cog.utc_now()
                    await self.runtime.states.save(state)
                turn = CognitiveTurn(
                    event_id=cognitive_event.id,
                    plan_id=uuid4(),
                    started_at=self._cog.utc_now(),
                    priority=priority,
                )
                self._active[turn.event_id] = turn
                ui_id = getattr(event, "ui_id", None)
                if not isinstance(ui_id, int) or isinstance(ui_id, bool):
                    ui_id = None
                self._deliveries[turn.event_id] = _DeliveryAudit(turn, ui_id)
                self._snapshot_delivery(self._deliveries[turn.event_id], "generating")
                if ui_id is not None:
                    self._ui_turns[ui_id] = turn.event_id
                return turn
            except asyncio.CancelledError:
                # A context budget can expire after append committed. Retire that
                # partial event without disabling the otherwise healthy bridge.
                if cognitive_event is not None:
                    try:
                        await asyncio.wait_for(self._cancel_unfinished_event(cognitive_event.id), 0.2)
                    except asyncio.TimeoutError:
                        pass  # Startup recovery retires any event whose write could not finish.
                raise
            except Exception as exc:
                await self._disable("记录事件", exc)
                return None

    async def complete_turn(
        self,
        turn: CognitiveTurn | None,
        event: Event,
        *,
        emotion: str,
        text: str,
        avatar_actions: list[str],
        response_mode: str,
        response_latency: float,
        tts_backend: str,
        audio_path: str | None,
        playback_messages: list[dict[str, Any]],
        learning_episode_id: int | None,
        expected_segments: int | None = None,
        synthesis_failed: bool = False,
    ) -> None:
        if turn is None or not self.enabled:
            return
        async with self._lock:
            try:
                assert self._cog is not None and self.runtime is not None and self._memory is not None
                delivery = self._deliveries.get(turn.event_id)
                if delivery is None or delivery.finalized or delivery.terminal:
                    return
                delivery.expected_segments = max(0, int(expected_segments)) if expected_segments is not None else (1 if audio_path else 0)
                delivery.text_only = (tts_backend == "mock" or getattr(event, 'text_only_delivery', False)) and not audio_path
                delivery.synthesis_failed = bool(
                    synthesis_failed
                    or not delivery.text_only and (not audio_path or not delivery.expected_segments)
                )
                delivery.generation_failed = response_mode in {"fallback", "partial", "cancelled"}
                await self._record_action(
                    turn,
                    "reply.generate",
                    parameters={"mode": response_mode},
                    expected="Generate a safe, persona-consistent spoken reply.",
                    output=text,
                    changes={
                        "emotion": emotion,
                        "avatar_actions": list(avatar_actions),
                        "latency_seconds": response_latency,
                    },
                    succeeded=response_mode not in {"fallback", "partial", "cancelled"},
                )
                await self._record_action(
                    turn,
                    "avatar.show",
                    parameters={"emotion": emotion, "actions": list(avatar_actions)},
                    expected="Update the visible avatar performance state.",
                    output=f"已生成展示指令，情绪：{emotion}；未包含画面展示回执。",
                )
                await self._record_action(
                    turn,
                    "speech.synthesize",
                    parameters={"backend": tts_backend, "emotion": emotion},
                    expected="Synthesize audio; mock mode produces text only.",
                    output=audio_path or ("本轮仅文字，没有生成或播放语音。" if delivery.text_only else "未生成可播放音频。"),
                    changes={"audio_path": audio_path or "", "failed": delivery.synthesis_failed, "text_only": delivery.text_only},
                    succeeded=not delivery.synthesis_failed,
                )
                if audio_path:
                    await self._record_action(
                        turn,
                        "playback.enqueue",
                        parameters={"priority": event.priority},
                        expected="Place generated speech on the bounded playback timeline.",
                        output=f"产生 {len(playback_messages)} 条播放状态消息。",
                        changes={"messages": self._json_safe(playback_messages)},
                    )

                episode_document = {
                    "input": {
                        "source": event.source,
                        "kind": event.kind,
                        "user": event.user,
                        "viewer_key": event.viewer_key,
                        "text": event.text,
                    },
                    "response": {
                        "emotion": emotion,
                        "text": text,
                        "mode": response_mode,
                        "avatar_actions": list(avatar_actions),
                        "latency_seconds": response_latency,
                    },
                    "delivery": {
                        "tts_backend": tts_backend,
                        "audio_path": audio_path,
                        "playback_messages": self._json_safe(playback_messages),
                        "expected_segments": delivery.expected_segments,
                        "synthesis": "failed" if delivery.synthesis_failed else (
                            ("mock" if tts_backend == "mock" else "text_only") if delivery.text_only else "complete"),
                        "generation": "failed" if delivery.generation_failed else "complete",
                        "outcome": "queued",
                    },
                }
                delivery.document = episode_document
                delivery.audit = self._cog.MemoryRecord(
                    id=uuid5(turn.event_id, "vtuber-turn-audit"),
                    kind=self._cog.MemoryKind.EPISODIC,
                    content=json.dumps(episode_document, ensure_ascii=False, default=str),
                    summary=f"{event.kind}：{event.text[:70]} → [{emotion}]{text[:90]}",
                    salience=max(0.5, turn.priority),
                    confidence=0.85 if response_mode == "llm" else 0.65,
                    source_event_id=turn.event_id,
                    metadata={
                        "type": "turn_audit",
                        "recallable": False,
                        "outcome": "queued",
                        "plan_id": str(turn.plan_id),
                        "user": event.user,
                        "viewer_key": event.viewer_key,
                        "source": event.source,
                        "response_mode": response_mode,
                        "learning_episode_id": learning_episode_id,
                    },
                )
                await self.runtime.memories.store(delivery.audit)
                procedure = "reply.generate -> avatar.show -> speech.synthesize"
                if audio_path:
                    procedure += " -> playback.enqueue"
                await self._ensure_procedure(procedure, turn.event_id)
                delivery.finalized = True
                await self._update_delivery(delivery)
            except Exception as exc:
                await self._disable("完成认知记录", exc)

    async def record_delivery(
        self, turn_id: int, segment_index: int, completed: bool, reason: str = ""
    ) -> None:
        """Record an actual playback receipt, including receipts before synthesis finalization."""
        if not self.enabled or not isinstance(segment_index, int) or isinstance(segment_index, bool) or not 0 <= segment_index < 4096:
            return
        async with self._lock:
            delivery = self._deliveries.get(self._ui_turns.get(turn_id))
            if delivery is None or delivery.terminal:
                return
            if delivery.expected_segments is not None and segment_index >= delivery.expected_segments:
                return
            # A duplicate success must never erase a failure for the same segment.
            previous = delivery.receipts.get(segment_index)
            value = bool(completed) and previous is not False
            if previous is value:
                return
            delivery.receipts[segment_index] = value
            if not value:
                delivery.reasons[segment_index] = str(reason or "播放未完整完成")[:180]
            self._snapshot_delivery(delivery, "generating" if not delivery.finalized else "queued")
            if delivery.finalized:
                try:
                    await self._update_delivery(delivery)
                except Exception as exc:
                    await self._disable("记录播放回执", exc)

    async def _update_delivery(self, delivery: _DeliveryAudit, failure: str = "") -> None:
        assert self._cog is not None and self.runtime is not None
        if delivery.terminal:
            return
        count = delivery.expected_segments or 0
        received = {index: complete for index, complete in delivery.receipts.items() if index < count}
        failed = failure or delivery.synthesis_failed or delivery.generation_failed or any(not complete for complete in received.values())
        outcome = (
            "failed" if failed else "text_only" if delivery.text_only
            else "delivered" if count > 0 and all(received.get(index) is True for index in range(count))
            else "queued"
        )
        self._snapshot_delivery(delivery, outcome)
        if delivery.audit is not None:
            details = delivery.document["delivery"]
            details.update({
                "outcome": outcome,
                "played_segments": sorted(index for index, complete in received.items() if complete),
                "failed_segments": sorted(index for index, complete in received.items() if not complete),
                "failure_reason": str(failure or (
                    "回复未完整生成" if delivery.generation_failed else "语音未完整合成"
                    if delivery.synthesis_failed else next(iter(delivery.reasons.values()), "")
                ))[:180],
            })
            delivery.audit.metadata["outcome"] = outcome
            delivery.audit.content = json.dumps(delivery.document, ensure_ascii=False, default=str)
            delivery.audit.updated_at = self._cog.utc_now()
            await self.runtime.memories.store(delivery.audit)
        if outcome == "queued":
            return
        if delivery.finalized and not delivery.text_only:
            await self._record_action(
                delivery.turn, "playback.finish", parameters={"expected_segments": count},
                expected="Confirm every spoken segment using actual playback receipts.",
                output="全部语音段落已确认播完。" if outcome == "delivered" else "本轮未确认完整播放。",
                changes={"outcome": outcome, "played_segments": sorted(index for index, complete in received.items() if complete)},
                succeeded=outcome == "delivered",
            )
        state = await self.runtime.states.get()
        if state is not None:
            if outcome != "text_only":
                state = self._motivation.on_outcome(state, outcome == "delivered")
            if state.last_event_id == delivery.turn.event_id:
                state.phase = self._cog.CognitivePhase.IDLE
            state.iteration += 1
            state.updated_at = self._cog.utc_now()
            await self.runtime.states.save(state)
        await self.runtime.events.set_status(
            delivery.turn.event_id,
            self._cog.EventStatus.FAILED if outcome == "failed" else self._cog.EventStatus.COMPLETED,
            str(failure)[:500] if failure else None,
        )
        delivery.terminal = outcome
        self._active.pop(delivery.turn.event_id, None)
        self._deliveries.pop(delivery.turn.event_id, None)
        if delivery.ui_id is not None and self._ui_turns.get(delivery.ui_id) == delivery.turn.event_id:
            self._ui_turns.pop(delivery.ui_id, None)

    async def _cancel_unfinished_event(self, event_id: UUID) -> None:
        assert self._cog is not None and self.runtime is not None
        self._active.pop(event_id, None)
        delivery = self._deliveries.pop(event_id, None)
        if delivery is not None and self._ui_turns.get(delivery.ui_id) == event_id:
            self._ui_turns.pop(delivery.ui_id, None)
        try:
            await self.runtime.events.set_status(event_id, self._cog.EventStatus.FAILED, "认知准备超时或被取消")
            state = await self.runtime.states.get()
            if state is not None and state.last_event_id == event_id:
                state.phase = self._cog.CognitivePhase.IDLE
                state.updated_at = self._cog.utc_now()
                await self.runtime.states.save(state)
        except Exception:
            # Shutdown recovery will retire any event whose database write was interrupted.
            pass

    async def context_for(self, event: Event, limit: int = 5) -> str:
        """Retrieve compact durable memories for the reply planner."""

        if not self.enabled:
            self.last_context_status = "disabled"
            return ""
        async with self._read_lock:
            try:
                assert self._cog is not None and self.runtime is not None
                memories = await self.runtime.memories.search(
                    self._cog.MemoryQuery(
                        text=event.text,
                        kinds=[
                            self._cog.MemoryKind.SEMANTIC,
                            self._cog.MemoryKind.EPISODIC,
                            self._cog.MemoryKind.PROCEDURAL,
                        ],
                        limit=max(20, min(limit * 10, 80)),
                        minimum_salience=0.25,
                    )
                )
                lines: list[str] = []
                for memory in memories:
                    if not self._recall_visible(memory.metadata, event):
                        continue
                    label = {
                        self._cog.MemoryKind.SEMANTIC: "事实",
                        self._cog.MemoryKind.EPISODIC: "经历",
                        self._cog.MemoryKind.PROCEDURAL: "经验",
                    }.get(memory.kind, "记忆")
                    value = (memory.summary or memory.content).strip().replace("\n", " ")
                    if value:
                        lines.append(f"- {label}（置信度{memory.confidence:.2f}）：{value[:220]}")
                    if len(lines) >= max(1, min(limit, 8)):
                        break
                self.last_context_status = "ready" if lines else "empty"
                return "长期认知记忆：\n" + "\n".join(lines) if lines else ""
            except Exception as exc:
                # A temporarily busy read must not close the runtime underneath
                # an independent audit transaction. Caller budgets may cancel it.
                self.last_error = f"检索回复上下文：{type(exc).__name__}: {exc}"
                self.last_context_status = "unavailable"
                if self.strict:
                    raise
                return ""

    @staticmethod
    def _recall_visible(metadata: dict[str, Any], event: Event) -> bool:
        if metadata.get("recallable") is not True or metadata.get("active") is False:
            return False
        if metadata.get("status", "active") != "active":
            return False
        if metadata.get("type") in {"persona_identity", "turn_audit"}:
            return False
        key = metadata.get("viewer_key")
        if key:
            source = str(metadata.get("source") or "")
            owner = Event(source, "chat", "", viewer_key=str(key))
            return bool(source and event.viewer_key and viewer_identity(owner) == viewer_identity(event))
        user = metadata.get("user")
        if user:
            # Legacy nickname records cannot establish ownership of a platform
            # account, and private memories must retain their channel scope.
            return bool(not event.viewer_key and event.user == user and metadata.get("source") == event.source)
        return metadata.get("visibility") == "public" or metadata.get("scope") == "global"

    async def abort_turn(self, turn: CognitiveTurn | None, reason: str) -> None:
        if turn is None or not self.enabled:
            return
        async with self._lock:
            try:
                assert self._cog is not None and self.runtime is not None
                delivery = self._deliveries.get(turn.event_id)
                if delivery is not None:
                    await self._update_delivery(delivery, failure=reason or "回应被中断")
                    return
                if turn.event_id not in self._active:
                    return
                await self.runtime.events.set_status(
                    turn.event_id, self._cog.EventStatus.FAILED, reason[:500]
                )
                state = await self.runtime.states.get()
                if state is not None:
                    state.phase = self._cog.CognitivePhase.IDLE
                    state.iteration += 1
                    state.updated_at = self._cog.utc_now()
                    await self.runtime.states.save(state)
                self._active.pop(turn.event_id, None)
            except Exception as exc:
                await self._disable("中断认知事件", exc)

    async def close(self) -> None:
        runtime = self.runtime
        if runtime is None:
            return
        if self._cog is not None:
            for turn in list(self._active.values()):
                try:
                    await self.abort_turn(turn, "服务停止前未确认完整播放")
                except Exception:
                    pass
        self._active.clear()
        self._deliveries.clear()
        self._ui_turns.clear()
        try:
            await runtime.close()
        finally:
            self.runtime = None

    async def _record_action(
        self,
        turn: CognitiveTurn,
        kind: str,
        *,
        parameters: dict[str, Any],
        expected: str,
        output: str,
        changes: dict[str, Any] | None = None,
        succeeded: bool = True,
    ) -> None:
        assert self._cog is not None and self.runtime is not None
        action = self._cog.Action(
            kind=kind,
            parameters=self._json_safe(parameters),
            expected_outcome=expected,
        )
        result = self._cog.ActionResult(
            action_id=action.id,
            status=self._cog.ActionStatus.SUCCEEDED if succeeded else self._cog.ActionStatus.FAILED,
            output=output,
            started_at=turn.started_at,
            completed_at=self._cog.utc_now(),
            environment_changes=self._json_safe(changes or {}),
        )
        await self.runtime.action_runs.record(turn.event_id, turn.plan_id, action, result)

    async def _sync_persona_identity(self) -> None:
        assert self._cog is not None and self.runtime is not None and self._memory is not None
        identity = await self.runtime.identities.get()
        if identity is None:
            return
        persona_document = asdict(self.persona)
        narrative = (
            f"{self.persona.name}，{self.persona.species}。{self.persona.tone} "
            f"角色版本：{self.persona.revision}。年龄：{self.persona.age}。当前时间层：{self.persona.current_era}。"
            "人格设定由 config/persona.yaml 管理，持久记忆只能补充经历，不能覆盖当前角色卡。"
        )
        values = list(dict.fromkeys([
            "保持当前角色身份与表达一致",
            "从可验证的观众反馈中学习",
            "不确定时诚实说明",
            "安全与人工控制优先",
            *self.persona.rules,
        ]))
        changed = identity.name != self.persona.name or identity.narrative != narrative
        if changed or identity.values != values:
            identity.name = self.persona.name
            identity.narrative = narrative
            identity.values = values
            identity.limitations = [
                "外部 LLM、TTS、直播平台和游戏窗口可能不可用",
                "游戏输入必须经过现有观察、白名单、前台窗口与人工控制门",
                "历史学习记忆不得覆盖当前 persona.yaml",
            ]
            identity.version += 1
            identity.updated_at = self._cog.utc_now()
            await self.runtime.identities.save(identity)

        serialized = json.dumps(persona_document, ensure_ascii=False, sort_keys=True)
        signature = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        candidates = await self.runtime.memories.search(
            self._cog.MemoryQuery(
                text=f"角色身份 {self.persona.name}",
                kinds=[self._cog.MemoryKind.SEMANTIC],
                limit=100,
            )
        )
        matching = False
        for memory in candidates:
            if memory.metadata.get("type") != "persona_identity":
                continue
            if memory.metadata.get("signature") == signature:
                matching = True
                continue
            if memory.metadata.get("active", True):
                memory.metadata["active"] = False
                memory.salience = min(memory.salience, 0.1)
                await self.runtime.memories.store(memory)
        if not matching:
            await self._memory.remember(
                self._cog.MemoryKind.SEMANTIC,
                serialized,
                summary=f"当前角色身份：{self.persona.name}（{self.persona.species}）",
                salience=1.0,
                confidence=1.0,
                metadata={
                    "type": "persona_identity",
                    "signature": signature,
                    "active": True,
                    "authority": "config/persona.yaml",
                },
            )

    async def _ensure_livestream_goal(self) -> None:
        assert self._cog is not None and self.runtime is not None
        description = "以当前角色设定安全、自然地完成直播，并根据可验证的观众反馈持续改进。"
        goals = await self.runtime.goals.list(self._cog.GoalStatus.ACTIVE)
        if any(goal.description == description for goal in goals):
            return
        await self.runtime.goals.save(
            self._cog.Goal(
                description=description,
                success_criteria=[
                    "回复符合 persona.yaml、安全规则和当前节目上下文",
                    "真实执行的演出动作及结果可追踪",
                    "后续互动反馈能够关联到对应直播经历",
                ],
                priority=0.95,
            )
        )

    async def _ensure_procedure(self, procedure: str, event_id: UUID) -> None:
        assert self._cog is not None and self.runtime is not None and self._memory is not None
        if procedure in self._known_procedures:
            return
        existing = await self.runtime.memories.search(
            self._cog.MemoryQuery(
                text=procedure,
                kinds=[self._cog.MemoryKind.PROCEDURAL],
                limit=20,
            )
        )
        if not any(memory.content == procedure for memory in existing):
            await self._memory.remember(
                self._cog.MemoryKind.PROCEDURAL,
                procedure,
                summary="已验证的直播回复演出流程",
                salience=0.7,
                confidence=0.9,
                source_event_id=event_id,
                metadata={"owner": "brain.cognition.CognitiveBridge"},
            )
        self._known_procedures.add(procedure)

    async def _recover_interrupted_turns(self) -> None:
        assert self._cog is not None and self.runtime is not None
        for event in await self.runtime.events.list_unfinished():
            audit = await self.runtime.memories.get(uuid5(event.id, "vtuber-turn-audit"))
            outcome = audit.metadata.get("outcome") if audit is not None else None
            if outcome in {"delivered", "text_only"}:
                # The receipt transaction committed before the event status did.
                await self.runtime.events.set_status(event.id, self._cog.EventStatus.COMPLETED)
                continue
            if audit is not None:
                audit.metadata["outcome"] = "failed"
                document = json.loads(audit.content)
                document.setdefault("delivery", {}).update({
                    "outcome": "failed", "failure_reason": "上次进程结束前未确认完整播放",
                })
                audit.content = json.dumps(document, ensure_ascii=False, default=str)
                audit.updated_at = self._cog.utc_now()
                await self.runtime.memories.store(audit)
            await self.runtime.events.set_status(
                event.id,
                self._cog.EventStatus.FAILED,
                "Recovered after the prior VTuber process ended before turn completion.",
            )

    async def _disable(self, operation: str, exc: Exception) -> None:
        self.last_error = f"{type(exc).__name__}: {exc}"
        if self.strict:
            raise exc
        print(f"[cognition] {operation}失败，直播继续使用原链路：{self.last_error}")
        runtime = self.runtime
        self.runtime = None
        if runtime is not None:
            try:
                await runtime.close()
            except Exception:
                pass

    @staticmethod
    def _json_safe(value: Any) -> Any:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
