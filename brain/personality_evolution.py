"""Bounded, auditable personality evolution driven by durable memories and feedback.

The immutable role card remains in ``persona.yaml``.  This module only supplies
an adaptive overlay: slow global traits, user-scoped relationship state and a
short-lived mood.  It never edits prompts, model weights or core memories.
"""
from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path
from typing import Any

from .config import PersonalityEvolutionConfig
from .events import Event
from .persistence import CoalescedJSONWriter, PersistentState
from .viewer_identity import identity_key, viewer_identity, migrate_local_profiles


TRAIT_LABELS = {
    "energy": "活力",
    "warmth": "温暖",
    "playfulness": "俏皮",
    "initiative": "主动性",
    "directness": "表达直接度",
    "emotional_openness": "情绪表达",
    "boundary_strength": "健康边界",
    "resilience": "韧性",
}

# Safety-related lower bounds are deliberately high. Memories may shape style,
# but cannot erode the healthy relationship boundary of the role card.
TRAIT_BOUNDS = {
    "energy": (0.30, 0.95),
    "warmth": (0.50, 0.96),
    "playfulness": (0.20, 0.92),
    "initiative": (0.25, 0.90),
    "directness": (0.25, 0.80),
    "emotional_openness": (0.30, 0.86),
    "boundary_strength": (0.72, 0.98),
    "resilience": (0.50, 0.95),
}

_SAD_OR_STRESSED = (
    "难过", "伤心", "焦虑", "紧张", "害怕", "很累", "好累", "崩溃", "失眠",
    "委屈", "压力", "不开心", "心里有点乱",
)
_POSITIVE_MOOD = ("开心", "高兴", "成功", "完成了", "好耶", "太好了", "激动")
MOOD_HALF_LIFE_SEC = 2 * 3600.0
_QUOTED = re.compile(r'“[^”]*”|‘[^’]*’|「[^」]*」|『[^』]*』|"[^"]*"|\'[^\']*\'|`[^`]*`')
_REPORTED = re.compile(
    r'(?:他|她|他们|她们|别人|朋友|同事|同学|老师|观众|网友|妈妈|爸爸|老板|'
    r'(?:小|阿)[\u4e00-\u9fff]{1,3})(?:刚才|刚刚|之前)?(?:说|讲|觉得|认为|评价|告诉我)'
    r'|转述|引用|原话|台词|歌词|这句话|假如|如果|假设|要是|例如|比如')
_REPLY_TARGET = re.compile(
    r'你(?:刚才|刚刚|这次)?(?:的)?(?:回答|回复|解释|建议|安慰|表达|说话|语气)'
    r'|(?:你)?(?:刚才|刚刚|这次|上一条|上一轮|这条|这个|这次的|刚才的)(?:的)?'
    r'(?:回答|回复|解释|建议|安慰|表达)'
    r'|你(?:刚才|刚刚)?说(?:的|得)')
_EVALUATION_DENIAL = re.compile(
    r'不是说|并非|不觉得|不认为|没觉得|没有觉得|没有说|没说|不算|谈不上|不这么认为|才怪|个鬼|反讽')
_CORRECTION = re.compile(
    r'你(?:刚才|刚刚)?(?:说得不对|说错了|错了)|(?:事实|时间|日期|地点|名字|数据)(?:是|不对|错了)'
    r'|我(?:说的是|问的是|要的是|需要的是)|不是我的意思|别说教|不要说教|别复读|换个回答')
_POSITIVE_EVALUATION = re.compile(r'有帮助|有用|帮到我|安慰到我|让我好多了|让我好受|很贴心|很清楚|说得对')
_STYLE_ADJUSTMENT = re.compile(r'太长|太短|太快|太慢|太吵|复读|重复|说教|简短一点|慢一点|快一点')
_NEGATIVE_EVALUATION = re.compile(r'敷衍|无聊|没用|没有帮助|不喜欢|不满意|让我不舒服|让我更难受|令人失望')
_SUPPORT_REQUESTS = {
    'listening': re.compile(
        r'只想(?:倾诉|说说)|听我说|陪我聊|'
        r'(?:别|不要|先别|不需要|不想要|不想|不用)(?:再)?(?:让你|你)?(?:给我|给|提供|提)?(?:建议|意见|解决方案)'
        r'|别说教|不要说教'),
    'advice': re.compile(r'给我(?:一点|一些|点)?建议|给点建议|我(?:现在|这次)?(?:想要|需要|要)(?:具体)?建议|怎么办|该怎么|帮我想|有什么办法'),
    'playful': re.compile(r'逗逗我|逗我|讲(?:个)?笑话|轻松一点|开心一点'),
}


def _direct_statements(text: str) -> list[str]:
    """Conservative local cues: quoted/reported/questioned words are not votes."""
    text = re.sub(r'(?m)^\s*>[^\n]*', '', text)
    text = _QUOTED.sub('', text)
    return [part for part in re.split(r'(?<=[。！？!?；;\n])', text)
            if part.strip() and not _REPORTED.search(part) and not re.search(r'[？?]', part)]


def _negated(text: str, start: int) -> bool:
    return re.search(r'(?:不|没|没有|不是|并非|别|不要|先别|不用|不想|不需要|不必)'
                     r'(?:再|只|仅|总是|现在|继续|真的|那么|很|太|让你|你){0,2}$',
                     text[max(0, start - 10):start]) is not None


def _feedback_kind(text: str) -> str:
    positive = negative = correction = False
    for statement in _direct_statements(text):
        if _EVALUATION_DENIAL.search(statement):
            continue
        correction |= bool(_CORRECTION.search(statement))
        for clause in re.split(r'[，,]|但是|不过|但', statement):
            if not _REPLY_TARGET.search(clause):
                continue
            correction |= bool(_STYLE_ADJUSTMENT.search(clause))
            negative |= any(not _negated(clause, match.start())
                            for match in _NEGATIVE_EVALUATION.finditer(clause))
            positive |= any(not re.search(r'不|没|未|无', clause[:match.start()])
                            for match in _POSITIVE_EVALUATION.finditer(clause))
    # A polite thanks or a positive fragment must not override a real complaint.
    return 'negative' if negative else 'correction' if correction else 'positive' if positive else 'none'


def _support_request(text: str) -> str | None:
    latest = None
    for statement in _direct_statements(text):
        if re.search(r'不是说|不是让你|没让你|没有让你', statement):
            continue
        matches = [(match.start(), kind) for kind, pattern in _SUPPORT_REQUESTS.items()
                   for match in pattern.finditer(statement) if not _negated(statement, match.start())]
        if matches:
            latest = max(matches)[1]
    return latest


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


class PersonalityEvolution(PersistentState):
    """Persist and render a slowly evolving personality overlay."""

    def __init__(
        self,
        cfg: PersonalityEvolutionConfig,
        persona_name: str,
        *,
        clock=time.time,
    ) -> None:
        self.cfg = cfg
        self.persona_name = persona_name
        self.path = Path(cfg.state_path)
        self._clock = clock
        self.base_traits = self._normalize_traits(cfg.base_traits)
        self.state = self._load()
        self._pending_replies: dict[str, dict[str, Any]] = {}
        self._persistence = CoalescedJSONWriter(self.path, lambda: self.state)

    def _normalize_traits(self, values: dict[str, Any]) -> dict[str, float]:
        defaults = PersonalityEvolutionConfig().base_traits
        if not isinstance(values, dict):
            values = {}
        result: dict[str, float] = {}
        for trait, bounds in TRAIT_BOUNDS.items():
            try:
                value = float(values.get(trait, defaults[trait]))
            except (TypeError, ValueError):
                value = float(defaults[trait])
            result[trait] = round(_clamp(value, *bounds), 4)
        return result

    def _empty(self) -> dict[str, Any]:
        now = self._clock()
        return {
            "version": 2,
            "profile": self.cfg.profile,
            "base_traits": dict(self.base_traits),
            "traits": dict(self.base_traits),
            "mood": {
                "label": "沉静",
                "valence": 0.55,
                "arousal": self.base_traits["energy"],
                "updated_at": now,
                "source": {"type": "initial"},
            },
            "relationships": {},
            "relationship_events": [],
            "evidence": [],
            "snapshots": [],
            "next_evidence_id": 1,
            "next_snapshot_id": 1,
            "total_turns": 0,
            "last_consolidated_turn": 0,
            "last_change": f"以{self.cfg.profile}的正式角色基线开始。",
        }

    def _load(self) -> dict[str, Any]:
        default = self._empty()
        if not self.cfg.enabled or not self.path.exists():
            return default
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return default
        if not isinstance(data, dict):
            return default
        profile_changed = data.get('profile') != self.cfg.profile
        for key in (
            "relationships", "relationship_events", "evidence", "snapshots",
            "next_evidence_id", "next_snapshot_id", "total_turns",
            "last_consolidated_turn", "last_change", "mood",
        ):
            if key in data:
                default[key] = data[key]
        if not isinstance(default["relationships"], dict):
            default["relationships"] = {}
        if data.get("version", 1) < 2:
            # Nickname-only history cannot be assigned to a platform account.
            # Keep it solely in the explicitly unkeyed local companion branch.
            default["relationships"] = migrate_local_profiles(default["relationships"])
        for key in ("relationship_events", "evidence", "snapshots"):
            if not isinstance(default[key], list):
                default[key] = []
        default["profile"] = self.cfg.profile
        # Config is the authoritative immutable baseline; existing adaptive
        # offsets survive a restart but cannot introduce unknown traits.
        default["base_traits"] = dict(self.base_traits)
        default["traits"] = self._normalize_traits(
            data.get("traits", self.base_traits)
            if isinstance(data.get("traits"), dict) else self.base_traits
        )
        if profile_changed:
            # Preserve relationship/audit history, retire the test persona's learned style.
            default['traits'] = dict(self.base_traits)
            default['mood'] = self._empty()['mood']
            default['last_change'] = f"正式人设更新：{data.get('profile', '旧设定')} → {self.cfg.profile}；保留相处记录，重置性格倾向。"
            default['profile_migrations'] = list(data.get('profile_migrations', [])) + [{
                'from': data.get('profile', ''), 'to': self.cfg.profile, 'at': self._clock(),
            }]
            for item in default['evidence']:
                if item.get('applied_snapshot_id') is None:
                    item['applied_snapshot_id'] = -1
                    item['retired_reason'] = 'role_profile_changed'
        elif 'profile_migrations' in data:
            default['profile_migrations'] = data['profile_migrations']
        mood = default.get("mood")
        if not isinstance(mood, dict):
            default["mood"] = self._empty()["mood"]
        return default

    def _save(self) -> None:
        if not self.cfg.enabled:
            return
        self._persistence.request()

    @staticmethod
    def _identity_key(*parts: str) -> str:
        return identity_key(*parts)

    @classmethod
    def _user_key(cls, event: Event) -> str:
        return viewer_identity(event)

    def _event_source(self, event: Event, kind: str) -> dict[str, Any]:
        return {"type": kind, "ui_id": getattr(event, "ui_id", None),
                "event_ts": event.ts, "identity": self._user_key(event),
                "user": event.user or "", "platform": event.source}

    def _relationship(self, user: str, now: float, display_name: str = "") -> dict[str, Any]:
        relationships = self.state["relationships"]
        relationship = relationships.setdefault(user, {
            "interactions": 0,
            "familiarity": 0.0,
            "trust": 0.50,
            "support_counts": {"listening": 0, "advice": 0, "playful": 0},
            "preferred_support": "balanced",
            "last_seen": now,
        })
        relationship.setdefault("support_counts", {
            "listening": 0, "advice": 0, "playful": 0,
        })
        if display_name:
            relationship["display_name"] = display_name
        return relationship

    def _trim_relationships(self) -> None:
        maximum = max(10, int(self.cfg.max_relationships))
        relationships = self.state["relationships"]
        if len(relationships) <= maximum:
            return
        keep = sorted(
            relationships,
            key=lambda user: float(relationships[user].get("last_seen", 0)),
            reverse=True,
        )[:maximum]
        self.state["relationships"] = {user: relationships[user] for user in keep}

    def _record_relationship_change(
        self,
        user: str,
        changes: dict[str, float],
        source: dict[str, Any],
        reason: str,
    ) -> None:
        if not changes:
            return
        self.state["relationship_events"].append({
            "ts": self._clock(),
            "user": self.state["relationships"][user].get("display_name", "观众"),
            "identity": user,
            "changes": {key: round(value, 4) for key, value in changes.items()},
            "source": source,
            "reason": reason,
        })
        del self.state["relationship_events"][:-max(100, self.cfg.max_evidence)]

    def _apply_relationship_delta(
        self,
        user: str,
        *,
        source: dict[str, Any],
        reason: str,
        familiarity: float = 0.0,
        trust: float = 0.0,
        display_name: str = "",
    ) -> None:
        if not user:
            return
        now = self._clock()
        relationship = self._relationship(user, now, display_name)
        changes: dict[str, float] = {}
        for key, delta, bounds in (
            ("familiarity", familiarity, (0.0, 1.0)),
            ("trust", trust, (0.20, 0.95)),
        ):
            old = float(relationship.get(key, 0.0))
            new = round(_clamp(old + delta, *bounds), 4)
            if new != old:
                relationship[key] = new
                changes[key] = new - old
        relationship["last_seen"] = now
        self._record_relationship_change(user, changes, source, reason)

    def _update_support_preference(self, relationship: dict[str, Any], text: str) -> None:
        counts = relationship["support_counts"]
        requested = _support_request(text)
        if requested:
            counts[requested] = int(counts.get(requested, 0)) + 1
        preferred, count = max(counts.items(), key=lambda item: item[1])
        tied = sum(value == count for value in counts.values()) > 1
        relationship["preferred_support"] = preferred if count >= 2 and not tied else "balanced"

    def _decay_mood(self, now: float) -> dict[str, Any]:
        """Project from the last real change; reading never compounds decay.

        Persisted mood is an anchor, not a periodically rewritten accumulator.
        The same anchor and time produce the same value across polls/restarts.
        """
        mood = self.state.get('mood') or {}

        def finite(value, fallback):
            try:
                number = float(value)
            except (ValueError, TypeError):
                return fallback
            return number if math.isfinite(number) else fallback

        last = finite(mood.get('updated_at'), 0.0)
        elapsed = max(0.0, now - last)
        remaining = math.exp(-math.log(2) * elapsed / MOOD_HALF_LIFE_SEC)
        base_arousal = self.base_traits['energy']
        original_valence = _clamp(finite(mood.get('valence'), 0.55), 0.2, 0.95)
        original_arousal = _clamp(finite(mood.get('arousal'), base_arousal), 0.25, 0.95)
        valence = 0.55 + (original_valence - 0.55) * remaining
        arousal = base_arousal + (original_arousal - base_arousal) * remaining
        relaxed = (elapsed >= MOOD_HALF_LIFE_SEC and
                   (max(abs(valence - 0.55), abs(arousal - base_arousal)) < .02
                    or elapsed >= MOOD_HALF_LIFE_SEC * 3))
        label = str(mood.get('label') or '沉静')[:20]
        return {'label': '沉静' if relaxed else label,
                'valence': round(valence, 4), 'arousal': round(arousal, 4),
                'last_changed_at': last if last > 0 else None,
                'half_life_sec': MOOD_HALF_LIFE_SEC}

    def mood_snapshot(self) -> dict[str, Any]:
        """Public style state only: no triggering words, account, or source."""
        return {'enabled': bool(self.cfg.enabled), **self._decay_mood(self._clock())}

    def _set_mood(
        self,
        label: str,
        valence: float,
        arousal: float,
        source: dict[str, Any],
        strength: float = 0.28,
    ) -> None:
        now = self._clock()
        current = self._decay_mood(now)
        mood = self.state["mood"]
        mood["valence"] = round(
            _clamp(float(current["valence"]) * (1 - strength) + valence * strength, 0.2, 0.95),
            4,
        )
        mood["arousal"] = round(
            _clamp(float(current["arousal"]) * (1 - strength) + arousal * strength, 0.25, 0.95),
            4,
        )
        mood.update({"label": label, "updated_at": now, "source": source})

    def observe_event(self, event: Event) -> None:
        """Observe a new message as relationship input and prior-reply feedback."""

        if not self.cfg.enabled:
            return
        now = float(event.ts or self._clock())
        user = self._user_key(event)
        source = self._event_source(event, "event")
        if user:
            relationship = self._relationship(user, now, event.user or "观众")
            relationship["interactions"] = int(relationship.get("interactions", 0)) + 1
            relationship["last_seen"] = now
            self._update_support_preference(relationship, event.text)

        if any(marker in event.text for marker in _SAD_OR_STRESSED):
            self._set_mood("关切", 0.48, 0.48, source)
        elif any(marker in event.text for marker in _POSITIVE_MOOD):
            self._set_mood("明快", 0.82, 0.84, source)
        else:
            self._decay_mood(now)

        pending = self._pending_replies.pop(user, None) if user else None
        if pending and event.kind == 'chat' and 0 <= now - float(pending.get("ts", now)) <= 180:
            feedback = _feedback_kind(event.text)
            if feedback in {'positive', 'negative'}:
                direction = 1.0 if feedback == 'positive' else -1.0
                rate = float(self.cfg.relationship_learning_rate)
                self._apply_relationship_delta(
                    user,
                    source={**source, "reply_ui_id": pending.get("ui_id")},
                    reason="用户对上一轮回复给出明确正向反馈" if direction > 0 else "用户对上一轮回复给出明确负向反馈",
                    familiarity=rate * 0.18,
                    trust=rate * 0.55 * direction,
                )
                self._add_feedback_evidence(event, pending, direction)
            else:
                self._apply_relationship_delta(
                    user,
                    source={**source, "reply_ui_id": pending.get("ui_id")},
                    reason="用户补充事实或需求，不视为关系受损" if feedback == 'correction' else "同一用户在回复后继续交流",
                    familiarity=float(self.cfg.relationship_learning_rate) * 0.10,
                )
        self._trim_relationships()
        self.consolidate()
        self._save()

    def _add_feedback_evidence(
        self, event: Event, pending: dict[str, Any], direction: float
    ) -> None:
        rate = float(self.cfg.global_learning_rate)
        emotion = str(pending.get("emotion", "平静"))
        # A complaint is evidence to express things more clearly, not a reason
        # to become colder toward this user or every other audience member.
        deltas = {"warmth": rate * 0.20} if direction > 0 else {"directness": rate * 0.12}
        if direction > 0 and emotion in {"开心", "得意", "惊讶"}:
            deltas.update({
                "energy": rate * 0.28 * direction,
                "playfulness": rate * 0.34 * direction,
            })
        elif direction > 0:
            deltas.update({
                "emotional_openness": rate * 0.20 * direction,
                "directness": rate * 0.12 * direction,
            })
        self._add_evidence(
            source={
                **self._event_source(event, "feedback"),
                "event_ui_id": getattr(event, "ui_id", None),
                "reply_ui_id": pending.get("ui_id"),
                "event_ts": event.ts,
            },
            reason="后续互动对上一轮表达方式的明确反馈",
            deltas=deltas,
        )

    def record_reply(self, event: Event, reply: str, emotion: str) -> None:
        """Record one completed turn and make it eligible for later feedback."""

        if not self.cfg.enabled:
            return
        now = self._clock()
        self.state["total_turns"] = int(self.state.get("total_turns", 0)) + 1
        user = self._user_key(event)
        if user:
            rate = float(self.cfg.relationship_learning_rate)
            self._apply_relationship_delta(
                user,
                source=self._event_source(event, "reply"),
                reason="完成了一轮直接交流",
                familiarity=rate * 0.12,
                display_name=event.user or "观众",
            )
            self._pending_replies[user] = {
                "ui_id": getattr(event, "ui_id", None),
                "ts": now,
                "emotion": emotion,
            }
        mood_targets = {
            "开心": ("浅淡欣喜", 0.72, 0.52),
            "得意": ("含蓄得意", 0.68, 0.45),
            "惊讶": ("意外", 0.62, 0.60),
            "吐槽": ("冷幽默", 0.56, 0.42),
            "委屈": ("轻微低落", 0.43, 0.36),
            "平静": ("沉静", 0.55, 0.36),
        }
        label, valence, arousal = mood_targets.get(emotion, mood_targets["平静"])
        self._set_mood(
            label, valence, arousal,
            {"type": "reply", "ui_id": getattr(event, "ui_id", None), "event_ts": event.ts},
            strength=0.22,
        )
        self.consolidate()
        self._save()

    def absorb_memories(self, event: Event, memories: list[dict[str, Any]]) -> None:
        """Turn accepted autobiographical memories into bounded evidence."""

        if not self.cfg.enabled or not memories:
            return
        user = self._user_key(event)
        existing_refs = {
            str(item.get("source", {}).get("ref", ""))
            for item in self.state["evidence"]
        }
        for memory in memories:
            memory_id = int(memory.get("id", 0))
            reinforcement = int(memory.get("reinforcement_count", 0))
            ref = f"memory:{memory_id}:{reinforcement}"
            if not memory_id or ref in existing_refs:
                continue
            existing_refs.add(ref)
            salience = _clamp(float(memory.get("salience", 0.5)), 0.0, 1.0)
            confidence = _clamp(float(memory.get("confidence", 0.5)), 0.0, 1.0)
            strength = salience * confidence
            source = {
                **self._event_source(event, "memory"),
                "ref": ref,
                "memory_id": memory_id,
                "timeline_event_ids": list(memory.get("timeline_event_ids", []))[:6],
                "event_ts": event.ts,
            }
            kind = str(memory.get("kind", ""))
            predicate = str(memory.get("predicate", ""))
            summary = str(memory.get("summary", ""))

            if user and kind in {"viewer_fact", "shared_episode", "commitment"}:
                rate = float(self.cfg.relationship_learning_rate) * strength
                self._apply_relationship_delta(
                    user,
                    source=source,
                    reason=f"时序图保存了一条{kind}记忆",
                    familiarity=rate * 0.55,
                    trust=rate * (0.32 if kind != "commitment" else 0.20),
                    display_name=event.user or "观众",
                )

            deltas = self._memory_trait_deltas(
                kind, f"{predicate} {summary}", strength
            )
            if deltas:
                self._add_evidence(
                    source=source,
                    reason=f"时序图记忆#{memory_id}形成可塑人格证据",
                    deltas=deltas,
                )
        self.consolidate()
        self._save()

    def _memory_trait_deltas(
        self, kind: str, text: str, strength: float
    ) -> dict[str, float]:
        # Viewer facts primarily shape the private relationship branch, not the
        # host's global personality. This prevents one user from rewriting how
        # the character treats everybody else.
        if kind in {"core", "viewer_fact"}:
            return {}
        rate = float(self.cfg.global_learning_rate) * strength
        if kind == "commitment":
            return {
                "initiative": rate * 0.24,
                "warmth": rate * 0.16,
                "boundary_strength": rate * 0.08,
            }
        positive = any(marker in text for marker in ("礼物", "成功", "完成", "开心", "发现", "成就"))
        difficult = any(marker in text for marker in ("失败", "死亡", "难过", "挫折", "压力", "挑战"))
        if positive:
            return {
                "energy": rate * 0.38,
                "playfulness": rate * 0.32,
                "warmth": rate * 0.18,
            }
        if difficult:
            return {
                "resilience": rate * 0.42,
                "warmth": rate * 0.16,
                "energy": -rate * 0.10,
            }
        if kind == "shared_episode":
            return {
                "emotional_openness": rate * 0.16,
                "warmth": rate * 0.12,
            }
        return {}

    def _add_evidence(
        self,
        *,
        source: dict[str, Any],
        reason: str,
        deltas: dict[str, float],
    ) -> None:
        clean = {
            trait: round(float(delta), 6)
            for trait, delta in deltas.items()
            if trait in TRAIT_BOUNDS and abs(float(delta)) >= 0.000001
        }
        if not clean:
            return
        evidence_id = int(self.state.get("next_evidence_id", 1))
        self.state["next_evidence_id"] = evidence_id + 1
        self.state["evidence"].append({
            "id": evidence_id,
            "ts": self._clock(),
            "source": source,
            "reason": reason,
            "trait_deltas": clean,
            "applied_snapshot_id": None,
        })
        del self.state["evidence"][:-max(50, int(self.cfg.max_evidence))]

    def consolidate(self, force: bool = False) -> bool:
        """Apply pending global evidence as one time-axis personality snapshot."""

        if not self.cfg.enabled:
            return False
        total = int(self.state.get("total_turns", 0))
        previous_turn = int(self.state.get("last_consolidated_turn", 0))
        interval = max(1, int(self.cfg.consolidation_interval))
        pending = [
            item for item in self.state["evidence"]
            if item.get("applied_snapshot_id") is None
        ]
        due = total - previous_turn >= interval
        if not force and not due:
            return False
        away_from_baseline = any(
            abs(float(self.state["traits"][trait]) - self.base_traits[trait]) >= 0.0001
            for trait in TRAIT_BOUNDS
        )
        if not pending and not away_from_baseline:
            return False

        now = self._clock()
        current = dict(self.state["traits"])
        proposed = {trait: 0.0 for trait in TRAIT_BOUNDS}
        for evidence in pending:
            for trait, delta in evidence.get("trait_deltas", {}).items():
                if trait in proposed:
                    proposed[trait] += float(delta)
        rebound = _clamp(float(self.cfg.return_to_baseline), 0.0, 0.1)
        for trait in proposed:
            proposed[trait] += (self.base_traits[trait] - float(current[trait])) * rebound

        recent_used = {trait: 0.0 for trait in TRAIT_BOUNDS}
        for snapshot in self.state["snapshots"]:
            if now - float(snapshot.get("ts", 0)) > 86400:
                continue
            for trait, delta in snapshot.get("changes", {}).items():
                if trait in recent_used:
                    recent_used[trait] += abs(float(delta))

        per_snapshot = max(0.001, float(self.cfg.max_global_delta_per_consolidation))
        daily_limit = max(per_snapshot, float(self.cfg.daily_global_delta_limit))
        changes: dict[str, float] = {}
        updated = dict(current)
        for trait, raw in proposed.items():
            remaining = max(0.0, daily_limit - recent_used[trait])
            limited = _clamp(raw, -min(per_snapshot, remaining), min(per_snapshot, remaining))
            new = round(_clamp(float(current[trait]) + limited, *TRAIT_BOUNDS[trait]), 4)
            delta = round(new - float(current[trait]), 4)
            if delta:
                updated[trait] = new
                changes[trait] = delta

        snapshot_id = int(self.state.get("next_snapshot_id", 1))
        self.state["next_snapshot_id"] = snapshot_id + 1
        evidence_ids = [int(item["id"]) for item in pending]
        for evidence in pending:
            evidence["applied_snapshot_id"] = snapshot_id
        self.state["last_consolidated_turn"] = total
        if not changes:
            self.state["last_change"] = "本轮证据不足或已达到变化上限，人格保持稳定。"
            return False
        self.state["traits"] = updated
        snapshot = {
            "id": snapshot_id,
            "ts": now,
            "turn": total,
            "previous_traits": current,
            "traits": updated,
            "changes": changes,
            "evidence_ids": evidence_ids,
            "reason": "memory_and_feedback" if evidence_ids else "return_to_baseline",
            "supersedes": self.state["snapshots"][-1]["id"] if self.state["snapshots"] else None,
        }
        self.state["snapshots"].append(snapshot)
        del self.state["snapshots"][:-max(10, int(self.cfg.max_snapshots))]
        rendered = "、".join(
            f"{TRAIT_LABELS[trait]}{delta:+.3f}" for trait, delta in changes.items()
        )
        prefix = "人格固化" if evidence_ids else "人格自然回稳"
        self.state["last_change"] = f"第{snapshot_id}次{prefix}：{rendered}"
        return True

    @staticmethod
    def _level(value: float, *, high: str, medium: str, low: str) -> str:
        if value >= 0.72:
            return high
        if value >= 0.45:
            return medium
        return low

    def context_for(self, event: Event | None = None) -> str:
        """Render a compact prompt overlay without exposing raw audit data."""

        if not self.cfg.enabled:
            return ""
        traits = self.state["traits"]
        style = [
            self._level(float(traits["energy"]), high="活力充足、反应明快", medium="活力自然克制", low="语气偏安静"),
            self._level(float(traits["warmth"]), high="待人热心温暖", medium="友善但有分寸", low="表达较为冷静"),
            self._level(float(traits["playfulness"]), high="可以自然俏皮和轻轻吐槽", medium="偶尔开一点轻松玩笑", low="少用玩笑"),
            self._level(float(traits["initiative"]), high="适合主动接话和推进交流", medium="根据对方意愿推进", low="更多等待对方引导"),
            self._level(float(traits["directness"]), high="表达清楚直接", medium="直接与委婉保持平衡", low="措辞更柔和委婉"),
            self._level(float(traits["emotional_openness"]), high="情绪表达鲜明但不夸张", medium="适度表达自己的感受", low="情绪表达较含蓄"),
            self._level(float(traits["boundary_strength"]), high="始终保持清晰健康的关系边界", medium="保持必要的关系边界", low="仍必须遵守核心安全边界"),
            self._level(float(traits["resilience"]), high="遇到挫折会较快振作", medium="遇事保持稳定", low="遇到挫折时表达更谨慎"),
        ]
        mood = self.mood_snapshot()
        lines = [
            f"可塑人格状态：当前以“{self.state['profile']}”为基础，" + "；".join(style) + "。",
            f"短期心境：{mood.get('label', '沉静')}；只影响本轮语气，不得把用户的情绪说成自己的经历。",
        ]
        user = self._user_key(event) if event else ""
        if user and user in self.state["relationships"]:
            relationship = self.state["relationships"][user]
            familiarity = float(relationship.get("familiarity", 0.0))
            relation_label = "已经熟悉" if familiarity >= 0.55 else "逐渐熟悉" if familiarity >= 0.18 else "仍在认识"
            trust = float(relationship.get("trust", 0.5))
            trust_label = "交流反馈较稳定" if trust >= 0.68 else "正在建立交流默契" if trust >= 0.48 else "需要更谨慎地确认对方需求"
            support = {
                "listening": "对方反复表现出更偏好先被倾听",
                "advice": "对方反复表现出更偏好得到具体建议",
                "playful": "对方反复表现出更喜欢轻松俏皮的陪伴",
                "balanced": "尚未形成稳定的陪伴方式偏好",
            }.get(str(relationship.get("preferred_support", "balanced")), "尚未形成稳定偏好")
            name = event.user or relationship.get("display_name", "观众")
            lines.append(f"与当前用户“{name}”的关系：{relation_label}，{trust_label}；{support}。")
            if familiarity >= 0.18:
                lines.append("熟悉应体现为默契：少重复询问已确认的背景；只有话题相关且本轮确有证实记忆时，"
                             "才自然轻触一句，不列举记忆来证明亲近。没有证据就不补写共同经历。")
            lines.append("长期陪伴偏好只是默认参考，当前明确表达优先；不拿历史次数代替这一轮的需求。"
                         "事实纠正、需求变化与审美分歧不等于关系变差。")
        lines.append(
            "可塑人格只调整表达倾向：不得覆盖核心身份、安全边界和当前事实；"
            "保持当前角色设定，受到赞美也保留自己的判断，不一味赞同。"
            "不得仅凭熟悉度声称现实中不存在的共同经历或亲密关系，也不随机编造新的私人爱好。"
        )
        return "\n".join(lines)

    def summary(self) -> dict[str, Any]:
        mood = self.mood_snapshot()
        return {
            "enabled": self.cfg.enabled,
            "profile": self.state.get("profile", self.cfg.profile),
            "turns": int(self.state.get("total_turns", 0)),
            "traits": {
                key: round(float(value), 3)
                for key, value in self.state.get("traits", {}).items()
            },
            "mood": mood.get("label", "沉静"),
            "relationships": len(self.state.get("relationships", {})),
            "snapshots": len(self.state.get("snapshots", [])),
            "pending_evidence": sum(
                item.get("applied_snapshot_id") is None
                for item in self.state.get("evidence", [])
            ),
            "last_change": self.state.get("last_change", ""),
            "database": str(self.path) if self.cfg.enabled else "",
        }

    def close(self) -> None:
        self._save()
