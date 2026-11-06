"""Temporal trunk and bounded semantic association graph for durable memories.

Every recallable memory is attached to a timeline event.  Concepts are shared
between those events, so retrieval can move from a directly mentioned concept
to a weakly related episode without turning the whole store into an unbounded
knowledge graph.
"""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from typing import Any, Callable

from .events import Event
from .memory_facts import record_owner, timeline_branch


_CONCEPT_WORDS = re.compile(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9_]{2,}")
_CONCEPT_STOP = {
    "这个", "那个", "什么", "怎么", "就是", "真的", "今天", "现在", "大家",
    "观众", "直播", "曾经", "发生", "一次", "第一次", "终于", "最近",
}

# A deliberately small ontology.  It provides explicit, auditable bridges for
# common livestream topics.  New entities can still become topic nodes without
# being forced into a category.
_ONTOLOGY: dict[str, tuple[str, float]] = {
    "苹果": ("水果", 0.95), "香蕉": ("水果", 0.95), "梨": ("水果", 0.94),
    "草莓": ("水果", 0.92), "葡萄": ("水果", 0.92), "橙子": ("水果", 0.92),
    "橘子": ("水果", 0.92), "西瓜": ("水果", 0.92), "芒果": ("水果", 0.92),
    "桃子": ("水果", 0.92), "樱桃": ("水果", 0.92), "蓝莓": ("水果", 0.92),
    "柠檬": ("水果", 0.90),
    "蛋糕": ("甜点", 0.95), "布丁": ("甜点", 0.92), "饼干": ("甜点", 0.90),
    "冰淇淋": ("甜点", 0.95), "巧克力": ("甜点", 0.82), "糖果": ("甜点", 0.90),
    "米饭": ("主食", 0.95), "面条": ("主食", 0.95), "面包": ("主食", 0.88),
    "猫": ("宠物", 0.92), "狗": ("宠物", 0.92), "兔子": ("宠物", 0.88),
    "仓鼠": ("宠物", 0.90), "鹦鹉": ("宠物", 0.88),
    "吉他": ("乐器", 0.94), "钢琴": ("乐器", 0.94), "小提琴": ("乐器", 0.94),
    "原神": ("游戏", 0.94), "Minecraft": ("游戏", 0.94), "我的世界": ("游戏", 0.94),
    "漫展": ("活动", 0.92), "演唱会": ("活动", 0.92), "旅行": ("活动", 0.82),
    "考试": ("学习", 0.90), "毕业": ("学习", 0.86), "作业": ("学习", 0.88),
}

_CATEGORY_PARENTS: dict[str, tuple[str, float]] = {
    "水果": ("食物", 0.90), "甜点": ("食物", 0.90), "主食": ("食物", 0.90),
    "宠物": ("动物", 0.90), "乐器": ("音乐", 0.88),
}

_ACTIONS: dict[str, tuple[str, ...]] = {
    "选择": ("挑", "选择", "选了", "挑了"),
    "吃": ("吃", "品尝"),
    "喜欢": ("喜欢", "最爱", "爱吃"),
    "不喜欢": ("不喜欢", "讨厌"),
    "参加": ("参加", "去了", "去过"),
    "学习": ("学习", "想学", "在学"),
    "完成": ("完成", "做完", "通关"),
    "收到": ("收到", "送出", "送来"),
    "承诺": ("待会", "下次", "之后", "回头", "答应"),
}


@dataclass(frozen=True)
class GraphActivation:
    values: dict[int, float]
    direct_ids: frozenset[int]
    temporal_values: dict[int, float]


@dataclass(frozen=True)
class GraphRecallSignal:
    activation: float = 0.0
    temporal_continuity: float = 0.0
    associative: bool = False
    concepts: tuple[str, ...] = ()
    event_time: float = 0.0


class TemporalMemoryGraph:
    """Maintain one global timeline with private/topic branches and concepts."""

    def __init__(
        self,
        state: dict[str, Any],
        persona_name: str,
        cfg: Any,
        *,
        clock: Callable[[], float],
    ) -> None:
        self.state = state
        self.persona_name = persona_name
        self.cfg = cfg
        self._clock = clock
        self._concept_index: dict[tuple[str, str], dict[str, Any]] = {}
        self._concept_id_index: dict[int, dict[str, Any]] = {}
        self._edge_index: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._semantic_edges: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._timeline_id_index: dict[int, dict[str, Any]] = {}
        self._reindex()

    def ensure_schema(self) -> bool:
        """Upgrade version-1 memory lists in place and anchor every old record."""

        changed = False
        defaults: dict[str, Any] = {
            "next_timeline_id": 1,
            "next_concept_id": 1,
            "timeline_events": [],
            "concepts": [],
            "graph_edges": [],
            "branch_heads": {},
        }
        for key, value in defaults.items():
            if key not in self.state or not isinstance(self.state[key], type(value)):
                self.state[key] = value.copy() if isinstance(value, (dict, list)) else value
                changed = True
        self._reindex()
        max_timeline = max(
            (int(item.get("id", 0)) for item in self.state.get("timeline_events", [])),
            default=0,
        )
        max_concept = max(
            (int(item.get("id", 0)) for item in self.state.get("concepts", [])),
            default=0,
        )
        if int(self.state.get("next_timeline_id", 1)) <= max_timeline:
            self.state["next_timeline_id"] = max_timeline + 1
            changed = True
        if int(self.state.get("next_concept_id", 1)) <= max_concept:
            self.state["next_concept_id"] = max_concept + 1
            changed = True
        stats = self.state.setdefault("stats", {})
        for key in ("timeline_events", "concept_nodes", "graph_edges", "associative_recalls", "graph_migrations"):
            if key not in stats:
                stats[key] = 0
                changed = True

        unanchored = [
            memory for memory in self.state.get("memories", [])
            if isinstance(memory, dict) and not memory.get("timeline_event_ids")
            and memory.get('visibility') != 'private'
        ]
        groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
        for memory in unanchored:
            source = memory.get("source") if isinstance(memory.get("source"), dict) else {}
            key = (
                source.get("ui_id"), source.get("event_ts"), source.get("text_hash"),
                source.get("user"), source.get("kind"),
                record_owner(memory),
            )
            groups.setdefault(key, []).append(memory)
        for memories in groups.values():
            source = memories[0].get("source") if isinstance(memories[0].get("source"), dict) else {}
            event_time = float(source.get("event_ts") or memories[0].get("created_at") or self._clock())
            self.anchor(memories, None, event_time=event_time, migrated=True)
            changed = True
        if unanchored:
            stats["graph_migrations"] = int(stats.get("graph_migrations", 0)) + len(unanchored)
        if int(self.state.get("version", 1)) < 2:
            self.state["version"] = 2
            changed = True
        by_id = {int(memory['id']): memory for memory in self.state.get('memories', [])}
        branches_changed = False
        for record in self.state.get('timeline_events', []):
            retained = [branch for branch in record.get('branches', []) if not branch.startswith('viewer:')]
            owners = {timeline_branch(memory=by_id[mid]) for mid in record.get('memory_ids', []) if mid in by_id}
            retained.extend(sorted(owner for owner in owners if owner))
            if retained != record.get('branches', []):
                record['branches'] = retained
                branches_changed = True
        if branches_changed:
            self._rebuild_branches()
            changed = True
        self._refresh_graph_stats()
        return changed

    def anchor(
        self,
        memories: list[dict[str, Any]],
        event: Event | None,
        *,
        event_time: float | None = None,
        migrated: bool = False,
    ) -> dict[str, Any] | None:
        """Attach one or more selected memories to one point on the timeline."""

        # Concept labels, relation edges and timeline summaries share this graph.
        # Owner-private records must stay outside it, including during migration.
        memories = [memory for memory in memories if memory.get('visibility') != 'private']

        if not memories:
            return None
        now = self._clock()
        occurred = float(event_time if event_time is not None else event.ts if event else now)
        timeline_id = int(self.state["next_timeline_id"])
        self.state["next_timeline_id"] = timeline_id + 1

        source_user = event.user if event else self._source_value(memories, "user")
        source_kind = event.kind if event else self._source_value(memories, "kind") or "core"
        branches = ["global", f"kind:{source_kind}"]
        branch = timeline_branch(event, memories[0])
        if branch:
            branches.append(branch)

        previous_by_branch = {
            branch: int(self.state["branch_heads"][branch])
            for branch in branches if branch in self.state["branch_heads"]
        }
        concept_ids: set[int] = set()
        for memory in memories:
            memory_concepts = self._index_memory_concepts(memory, now)
            concept_ids.update(memory_concepts)
            history = [int(value) for value in memory.get("timeline_event_ids", []) if str(value).isdigit()]
            if not history or history[-1] != timeline_id:
                history.append(timeline_id)
            memory["timeline_event_ids"] = history[-24:]
            memory["timeline_event_id"] = timeline_id
            memory["concept_ids"] = sorted(set(int(value) for value in memory.get("concept_ids", [])) | memory_concepts)
            memory.setdefault("learned_at", now)
            memory.setdefault("event_time", occurred)
            memory.setdefault("valid_from", occurred)
            memory.setdefault("valid_to", None)

        source_text = event.text if event else ""
        source_hash = hashlib.sha256(source_text.encode("utf-8")).hexdigest()[:16] if source_text else ""
        record = {
            "id": timeline_id,
            "event_time": occurred,
            "learned_time": now,
            "source": event.source if event else "migration" if migrated else "manual",
            "kind": source_kind,
            "user": source_user,
            "ui_id": getattr(event, "ui_id", None) if event else self._source_value(memories, "ui_id"),
            "text_hash": source_hash or self._source_value(memories, "text_hash") or "",
            "summary": " / ".join(str(memory.get("summary", ""))[:160] for memory in memories)[:420],
            "memory_ids": sorted({int(memory["id"]) for memory in memories}),
            "concept_ids": sorted(concept_ids),
            "branches": branches,
            "previous_by_branch": previous_by_branch,
        }
        self.state["timeline_events"].append(record)
        self._timeline_id_index[timeline_id] = record
        for branch in branches:
            previous = previous_by_branch.get(branch)
            if previous is not None:
                self._upsert_edge(f"t:{previous}", f"t:{timeline_id}", "precedes" if branch == "global" else "branch_precedes", 1.0, now)
            self.state["branch_heads"][branch] = timeline_id
        for concept_id in concept_ids:
            self._upsert_edge(f"t:{timeline_id}", f"c:{concept_id}", "mentions", 0.92, now)
        self.state["stats"]["timeline_events"] = int(self.state["stats"].get("timeline_events", 0)) + 1
        self._refresh_graph_stats()
        return record

    def activation_for(self, event: Event) -> GraphActivation:
        concepts = {
            int(item["id"]): item for item in self.state.get("concepts", [])
            if isinstance(item, dict) and "id" in item
        }
        values: dict[int, float] = {}
        direct: set[int] = set()
        text = str(event.text).lower()
        query_tokens = self._tokens(text)
        for concept_id, concept in concepts.items():
            label = str(concept.get("label", "")).lower()
            kind = str(concept.get("kind", ""))
            if kind == "person":
                continue
            label_tokens = self._tokens(label)
            overlap = len(query_tokens & label_tokens) / math.sqrt(max(1, len(query_tokens) * len(label_tokens)))
            if label and label in text:
                values[concept_id] = 1.0
                direct.add(concept_id)
            elif len(text) >= 2 and text in label:
                values[concept_id] = max(values.get(concept_id, 0.0), 0.84)
                direct.add(concept_id)
            elif overlap >= 0.45:
                values[concept_id] = max(values.get(concept_id, 0.0), min(0.82, overlap))
                direct.add(concept_id)

        by_label = {str(item.get("label", "")).lower(): int(item["id"]) for item in concepts.values()}
        for term, (category, _) in _ONTOLOGY.items():
            if term.lower() in text and category.lower() in by_label:
                category_id = by_label[category.lower()]
                values[category_id] = max(values.get(category_id, 0.0), 0.58)
        for action, markers in _ACTIONS.items():
            if any(marker.lower() in text for marker in markers) and action.lower() in by_label:
                action_id = by_label[action.lower()]
                values[action_id] = max(values.get(action_id, 0.0), 0.88)
                direct.add(action_id)

        adjacency: dict[int, list[tuple[int, float]]] = {}
        for edge in self._semantic_edges.values():
            left = self._concept_ref(edge.get("source"))
            right = self._concept_ref(edge.get("target"))
            if left is None or right is None:
                continue
            weight = max(0.0, min(1.0, float(edge.get("weight", 0.5))))
            adjacency.setdefault(left, []).append((right, weight))
            adjacency.setdefault(right, []).append((left, weight * 0.86))

        frontier = dict(values)
        decay = max(0.1, min(0.95, float(getattr(self.cfg, "associative_decay", 0.72))))
        hops = max(0, min(4, int(getattr(self.cfg, "associative_hops", 2))))
        for _ in range(hops):
            following: dict[int, float] = {}
            for source, source_activation in frontier.items():
                for target, weight in adjacency.get(source, []):
                    propagated = source_activation * weight * decay
                    if propagated > values.get(target, 0.0) + 1e-6:
                        values[target] = min(1.0, propagated)
                        following[target] = max(following.get(target, 0.0), propagated)
            frontier = following
            if not frontier:
                break
        return GraphActivation(values, frozenset(direct), self._temporal_values(event))

    def signal_for(
        self,
        memory: dict[str, Any],
        event: Event,
        activation: GraphActivation,
    ) -> GraphRecallSignal:
        concept_ids = [int(value) for value in memory.get("concept_ids", []) if str(value).isdigit()]
        ranked = sorted(
            ((activation.values.get(concept_id, 0.0), concept_id) for concept_id in concept_ids),
            reverse=True,
        )
        strength = ranked[0][0] if ranked else 0.0
        has_direct = any(concept_id in activation.direct_ids for concept_id in concept_ids)
        minimum = float(getattr(self.cfg, "minimum_association_score", 0.30))
        associative = bool(strength >= minimum and not has_direct)
        labels = tuple(
            str(self._concept_id_index.get(concept_id, {}).get("label", ""))
            for value, concept_id in ranked[:3]
            if value > 0 and self._concept_id_index.get(concept_id, {}).get("label")
        )
        timeline_ids = {int(value) for value in memory.get("timeline_event_ids", []) if str(value).isdigit()}
        temporal = max(
            (activation.temporal_values.get(timeline_id, 0.0) for timeline_id in timeline_ids),
            default=0.0,
        )
        event_time = float(memory.get("event_time") or memory.get("created_at") or 0.0)
        return GraphRecallSignal(strength, temporal, associative, labels, event_time)

    def close_validity(self, memory: dict[str, Any], when: float | None = None) -> None:
        memory["valid_to"] = float(when if when is not None else self._clock())

    def prune(self, valid_memory_ids: set[int], max_events: int) -> None:
        """Bound graph history while keeping every retained memory anchored."""

        original_timeline_ids = {
            int(item["id"]) for item in self.state.get("timeline_events", [])
        }
        original_concept_count = len(self.state.get("concepts", []))
        original_edge_count = len(self.state.get("graph_edges", []))
        for timeline in self.state.get("timeline_events", []):
            timeline["memory_ids"] = [
                int(value) for value in timeline.get("memory_ids", [])
                if int(value) in valid_memory_ids
            ]
        timelines = [item for item in self.state.get("timeline_events", []) if item.get("memory_ids")]
        if len(timelines) > max(100, max_events):
            timelines = sorted(timelines, key=lambda item: (float(item.get("event_time", 0)), int(item.get("id", 0))))[-max(100, max_events):]
        self.state["timeline_events"] = timelines
        retained_timeline_ids = {int(item["id"]) for item in timelines}
        retained_concept_ids = {
            int(value)
            for memory in self.state.get("memories", [])
            if int(memory.get("id", -1)) in valid_memory_ids
            for value in memory.get("concept_ids", [])
        }
        for timeline in timelines:
            timeline["concept_ids"] = [
                int(value) for value in timeline.get("concept_ids", [])
                if int(value) in retained_concept_ids
            ]
        self.state["concepts"] = [
            item for item in self.state.get("concepts", [])
            if int(item.get("id", -1)) in retained_concept_ids
        ]
        self.state["graph_edges"] = [
            edge for edge in self.state.get("graph_edges", [])
            if self._edge_refs_retained(edge, retained_timeline_ids, retained_concept_ids)
        ]
        timeline_changed = retained_timeline_ids != original_timeline_ids
        graph_changed = (
            len(self.state["concepts"]) != original_concept_count
            or len(self.state["graph_edges"]) != original_edge_count
        )
        if timeline_changed:
            self._reindex()
            self._rebuild_branches()
        elif graph_changed:
            self._reindex()
        self._refresh_graph_stats()

    def summary(self) -> dict[str, int]:
        return {
            "timeline_events": len(self.state.get("timeline_events", [])),
            "concept_nodes": len(self.state.get("concepts", [])),
            "graph_edges": len(self.state.get("graph_edges", [])),
            "branches": len(self.state.get("branch_heads", {})),
        }

    def _index_memory_concepts(self, memory: dict[str, Any], now: float) -> set[int]:
        descriptors: dict[str, str] = {}
        subject = str(memory.get("subject", "")).strip()
        if subject:
            descriptors[subject] = "person"
        combined = " ".join(
            str(memory.get(key, "")) for key in ("predicate", "value", "summary")
        )
        lowered = combined.lower()
        for item in memory.get("concepts", []):
            if isinstance(item, (list, tuple)) and len(item) == 2:
                label, kind = str(item[0])[:40], str(item[1])
                if kind in {"entity", "category", "action", "topic"} and label:
                    descriptors[label] = kind
        for term, (category, weight) in _ONTOLOGY.items():
            if term.lower() not in lowered:
                continue
            descriptors[term] = "entity"
            descriptors[category] = "category"
            entity_id = self._ensure_concept(term, "entity", now)
            category_id = self._ensure_concept(category, "category", now)
            self._upsert_edge(f"c:{entity_id}", f"c:{category_id}", "is_a", weight, now)
            parent = _CATEGORY_PARENTS.get(category)
            if parent:
                parent_id = self._ensure_concept(parent[0], "category", now)
                descriptors[parent[0]] = "category"
                self._upsert_edge(f"c:{category_id}", f"c:{parent_id}", "is_a", parent[1], now)
        for action, markers in _ACTIONS.items():
            if any(marker.lower() in lowered for marker in markers):
                descriptors[action] = "action"

        value = self._topic_value(str(memory.get("value", "")))
        if value and value not in descriptors:
            descriptors[value] = "topic"
        for token in self._tokens(str(memory.get("value", ""))):
            if 2 <= len(token) <= 12 and token not in _CONCEPT_STOP:
                descriptors.setdefault(token, "topic")
            if len(descriptors) >= 14:
                break
        by_label = {
            label: self._ensure_concept(label, kind, now)
            for label, kind in descriptors.items() if label
        }
        concept_ids = set(by_label.values())
        for item in memory.get("relations", []):
            if not isinstance(item, (list, tuple)) or len(item) != 4:
                continue
            source, target, relation, raw_weight = item
            if str(source) not in by_label or str(target) not in by_label:
                continue
            if str(relation) not in {"is_a", "related"}:
                continue
            try:
                weight = max(0.1, min(0.9, float(raw_weight)))
            except (TypeError, ValueError):
                continue
            self._upsert_edge(
                f"c:{by_label[str(source)]}", f"c:{by_label[str(target)]}",
                str(relation), weight, now,
            )
        return concept_ids

    def _ensure_concept(self, label: str, kind: str, now: float) -> int:
        normalized = self._normalize(label)
        existing = self._concept_index.get((normalized, kind))
        if existing is not None:
            existing["updated_at"] = now
            existing["reinforcement_count"] = int(existing.get("reinforcement_count", 0)) + 1
            return int(existing["id"])
        concept_id = int(self.state["next_concept_id"])
        self.state["next_concept_id"] = concept_id + 1
        concept = {
            "id": concept_id,
            "label": str(label)[:80],
            "normalized": normalized,
            "kind": kind,
            "created_at": now,
            "updated_at": now,
            "reinforcement_count": 0,
        }
        self.state["concepts"].append(concept)
        self._concept_index[(normalized, kind)] = concept
        self._concept_id_index[concept_id] = concept
        return concept_id

    def _upsert_edge(self, source: str, target: str, relation: str, weight: float, now: float) -> None:
        key = (source, target, relation)
        existing = self._edge_index.get(key)
        if existing is not None:
            existing["weight"] = round(min(1.0, max(float(existing.get("weight", 0)), weight) + 0.01), 3)
            existing["updated_at"] = now
            existing["reinforcement_count"] = int(existing.get("reinforcement_count", 0)) + 1
            return
        edge = {
            "source": source,
            "target": target,
            "relation": relation,
            "weight": round(max(0.0, min(1.0, weight)), 3),
            "created_at": now,
            "updated_at": now,
            "reinforcement_count": 0,
        }
        self.state["graph_edges"].append(edge)
        self._edge_index[key] = edge
        if relation in {"is_a", "related"}:
            self._semantic_edges[key] = edge

    def _temporal_values(self, event: Event) -> dict[int, float]:
        branch = timeline_branch(event) or 'global'
        head = self.state.get("branch_heads", {}).get(branch)
        if head is None:
            branch = "global"
            head = self.state.get("branch_heads", {}).get(branch)
        if head is None:
            return {}
        current = int(head)
        window = max(1, min(8, int(getattr(self.cfg, "timeline_context_window", 3))))
        values: dict[int, float] = {}
        for distance in range(window):
            values[current] = max(0.0, 1.0 - distance / window)
            record = self._timeline_id_index.get(current)
            if not record:
                break
            previous = record.get("previous_by_branch", {}).get(branch)
            if previous is None:
                break
            current = int(previous)
        return values

    def _rebuild_branches(self) -> None:
        edges = [edge for edge in self.state.get("graph_edges", []) if edge.get("relation") not in {"precedes", "branch_precedes"}]
        self.state["graph_edges"] = edges
        self._reindex_edges()
        self.state["branch_heads"] = {}
        for record in sorted(self.state.get("timeline_events", []), key=lambda item: (float(item.get("event_time", 0)), int(item.get("id", 0)))):
            previous: dict[str, int] = {}
            for branch in record.get("branches", ["global"]):
                old = self.state["branch_heads"].get(branch)
                if old is not None:
                    previous[branch] = int(old)
                    self._upsert_edge(f"t:{old}", f"t:{record['id']}", "precedes" if branch == "global" else "branch_precedes", 1.0, self._clock())
                self.state["branch_heads"][branch] = int(record["id"])
            record["previous_by_branch"] = previous

    def _edge_refs_retained(self, edge: dict[str, Any], timelines: set[int], concepts: set[int]) -> bool:
        for value in (edge.get("source"), edge.get("target")):
            text = str(value)
            if text.startswith("t:") and int(text[2:]) not in timelines:
                return False
            if text.startswith("c:") and int(text[2:]) not in concepts:
                return False
        return True

    def _refresh_graph_stats(self) -> None:
        stats = self.state.setdefault("stats", {})
        stats["concept_nodes"] = len(self.state.get("concepts", []))
        stats["graph_edges"] = len(self.state.get("graph_edges", []))

    def _reindex(self) -> None:
        self._concept_index = {
            (str(item.get("normalized") or self._normalize(item.get("label", ""))), str(item.get("kind", ""))): item
            for item in self.state.get("concepts", []) if isinstance(item, dict)
        }
        self._concept_id_index = {
            int(item["id"]): item
            for item in self.state.get("concepts", [])
            if isinstance(item, dict) and str(item.get("id", "")).isdigit()
        }
        self._timeline_id_index = {
            int(item["id"]): item
            for item in self.state.get("timeline_events", [])
            if isinstance(item, dict) and str(item.get("id", "")).isdigit()
        }
        self._reindex_edges()

    def _reindex_edges(self) -> None:
        self._edge_index = {
            (str(item.get("source", "")), str(item.get("target", "")), str(item.get("relation", ""))): item
            for item in self.state.get("graph_edges", []) if isinstance(item, dict)
        }
        self._semantic_edges = {
            key: item
            for key, item in self._edge_index.items()
            if key[2] in {"is_a", "related"}
        }

    @staticmethod
    def _source_value(memories: list[dict[str, Any]], key: str) -> Any:
        source = memories[0].get("source") if memories and isinstance(memories[0].get("source"), dict) else {}
        return source.get(key)

    @staticmethod
    def _concept_ref(value: Any) -> int | None:
        text = str(value)
        return int(text[2:]) if text.startswith("c:") and text[2:].isdigit() else None

    @staticmethod
    def _tokens(text: str) -> set[str]:
        values: set[str] = set()
        for raw in _CONCEPT_WORDS.findall(str(text).lower()):
            if raw in _CONCEPT_STOP:
                continue
            values.add(raw)
            if re.fullmatch(r"[\u4e00-\u9fff]+", raw) and len(raw) > 2:
                values.update(raw[index:index + 2] for index in range(len(raw) - 1))
        return values

    @staticmethod
    def _normalize(value: str) -> str:
        return re.sub(r"\s+", "", str(value).strip().lower())[:100]

    @staticmethod
    def _topic_value(value: str) -> str:
        clean = str(value).strip(" \t\r\n。！!？?，,；;")
        clean = re.sub(r"^(?:第一次|终于|最近|正在|准备|计划|想学|要参加)", "", clean)
        return clean[:40] if 2 <= len(clean) <= 40 else ""
