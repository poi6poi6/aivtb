"""可审计的本地自主学习闭环。

不修改基础模型权重，而是从直播互动中持续学习观众兴趣、有效回复策略、
相似情境经验和注意力优先级。生产提示词只读取聚合策略，具体历史经历由时序图召回。
"""
from __future__ import annotations

import json
import math
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any

from .events import Event
from .persistence import CoalescedJSONWriter, PersistentState
from .viewer_identity import migrate_local_profiles, viewer_identity

_WORDS = re.compile(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9_]{2,}")
_STOP = {
    "主播", "一个", "这个", "那个", "什么", "怎么", "就是", "真的", "可以",
    "还是", "一下", "今天", "现在", "不要", "没有", "游戏", "弹幕", "看看",
}
_POSITIVE_MARKERS = ("哈哈", "喜欢", "好听", "厉害", "牛", "666", "有意思", "笑死")
_NEGATIVE_MARKERS = (
    "不好", "无聊", "别复读", "复读机", "重复", "只会", "其他的", "换一句",
    "闭嘴", "难听", "不对", "错了", "尴尬",
)
_RETIRED_THEME_TERMS = (
    "minecraft", "我的世界", "钻石", "挖矿", "矿洞", "矿镐", "苦力怕", "骷髅", "岩浆",
    "守护灵", "寄宿", "古老佩刀", "古刀", "刀灵", "小小灵体", "在下", "诸位",
)


def _uses_retired_theme(text: str) -> bool:
    lowered = str(text).lower()
    return any(term in lowered for term in _RETIRED_THEME_TERMS)


_LEGACY_FALLBACK_REPLIES = {
    "弹幕刷太快啦，主播眼睛跟不上了", "晚上好晚上好，欢迎来直播间", "嗯嗯，我在看呢",
    "谢谢老板的礼物，老板大气！", "又死了……这游戏绝对在针对我", "刚才那波是意外，真的",
    "看到没有，这就是实力", "运气来了挡都挡不住", "我先苟一会儿",
    "这个地方怎么这么危险", "怎么没人说话，都睡着了吗", "来人聊天呀，不然我要发呆了",
}


def _tokens(text: str) -> set[str]:
    words: set[str] = set()
    for raw in _WORDS.findall(text.lower()):
        if raw not in _STOP:
            words.add(raw)
        if re.fullmatch(r"[\u4e00-\u9fff]+", raw) and len(raw) > 2:
            words.update(raw[i:i + 2] for i in range(len(raw) - 1))
    return words


def _mean(stat: dict[str, Any]) -> float:
    count = int(stat.get("count", 0))
    return float(stat.get("reward", 0.0)) / count if count else 0.0


class LearningEngine(PersistentState):
    """从互动结果中在线更新经验、画像和策略统计。"""

    def __init__(
        self,
        state_path: str,
        enabled: bool = True,
        max_episodes: int = 500,
        feedback_window_sec: float = 90.0,
        consolidation_interval: int = 20,
    ):
        self.path = Path(state_path)
        self.enabled = enabled
        self.max_episodes = max(20, max_episodes)
        self.feedback_window = max(5.0, feedback_window_sec)
        self.consolidation_interval = max(5, consolidation_interval)
        self.state = self._load()
        self._pending: dict[int, dict[str, Any]] = {}
        self._next_id = int(self.state.get("next_id", 1))
        self._persistence = CoalescedJSONWriter(self.path, lambda: self.state)

    def _empty(self) -> dict[str, Any]:
        return {
            "version": 2,
            "next_id": 1,
            "total_interactions": 0,
            "episodes": [],
            "viewers": {},
            "strategies": {},
            "event_kinds": {},
            "guidelines": [],
            "research": [],
            "self_narrative": "还在认识直播间和积累互动经验。",
        }

    def _load(self) -> dict[str, Any]:
        default = self._empty()
        if not self.path.exists():
            return default
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return default
            # 允许未来升级字段，也兼容早期/部分状态文件。
            default.update(data)
            for key in ("episodes", "viewers", "strategies", "event_kinds", "guidelines", "research"):
                if not isinstance(default[key], type(self._empty()[key])):
                    default[key] = self._empty()[key]
            if data.get("version", 1) < 2:
                default["viewers"] = migrate_local_profiles(default["viewers"])
                # Old platform episodes are retained as audit evidence only;
                # nickname history cannot establish ownership of an account.
                for episode in default["episodes"]:
                    if episode.get("source") in {"companion", "voice"}:
                        episode["identity"] = viewer_identity(episode.get("user"))
                default["version"] = 2
            return default
        except (json.JSONDecodeError, OSError):
            return default

    def _save(self) -> None:
        if not self.enabled:
            return
        self.state["next_id"] = self._next_id
        self._persistence.request()

    def observe_event(self, event: Event) -> None:
        """把新事件当作之前回复的隐式效果反馈，并更新观众兴趣。"""
        if not self.enabled:
            return
        now = event.ts
        identity = viewer_identity(event)
        self.tick(now, save=False)
        candidates = [item for item in self._pending.values() if now - float(item["ts"]) <= self.feedback_window]
        same_user = [item for item in candidates if identity and identity == item.get("identity")]
        pending = max(same_user or candidates, key=lambda item: item["ts"], default=None)
        if pending is not None:
            episode_id = int(pending["id"])
            reward = 0.0
            reasons: list[str] = []
            if identity and identity == pending.get("identity"):
                reward += 1.5
                reasons.append("同一观众继续互动")
                if any(marker in event.text for marker in _POSITIVE_MARKERS):
                    reward += 1.0
                    reasons.append("观众正向反馈")
                if any(marker in event.text for marker in _NEGATIVE_MARKERS):
                    reward -= 3.0
                    reasons.append("观众负向反馈")
            if event.kind == "gift":
                reward += 3.0
                reasons.append("收到礼物")
            elif event.kind == "chat" and now - float(pending["ts"]) >= 1.0:
                reward += 0.25
                reasons.append("直播间继续发言")
            if reward:
                self._reward(episode_id, reward, "、".join(reasons))
                if (identity and identity == pending.get("identity")) or event.kind == "gift":
                    self._pending.pop(episode_id, None)
        if identity:
            profile = self.state["viewers"].setdefault(identity, {
                "interactions": 0, "topics": {}, "recent_messages": [], "last_seen": 0.0,
            })
            if event.user:
                profile["display_name"] = event.user
            profile["interactions"] += 1
            profile["last_seen"] = now
            for token in _tokens(event.text):
                profile["topics"][token] = int(profile["topics"].get(token, 0)) + 1
            profile["recent_messages"] = (profile["recent_messages"] + [event.text])[-5:]
        self._save()

    def tick(self, now: float | None = None, save: bool = True) -> None:
        """在没有新事件时也结算超时经验，避免反馈永久悬空。"""
        if not self.enabled:
            return
        now = now or time.time()
        changed = False
        for episode_id, pending in list(self._pending.items()):
            if now - float(pending["ts"]) > self.feedback_window:
                self._reward(episode_id, -0.1, "回复后没有继续互动")
                self._pending.pop(episode_id, None)
                changed = True
        if changed and save:
            self._save()

    def learn_reply(self, event: Event, emotion: str, reply: str) -> int | None:
        """把一次实际回复存为可检索经验，等待后续互动给出效果反馈。"""
        if not self.enabled:
            return None
        episode_id = self._next_id
        self._next_id += 1
        duplicate = any(item.get("reply") == reply for item in self.state["episodes"][-20:])
        episode = {
            "id": episode_id,
            "ts": time.time(),
            "source": event.source,
            "kind": event.kind,
            "user": event.user,
            "identity": viewer_identity(event),
            "text": event.text,
            "emotion": emotion,
            "reply": reply,
            "reward": 0.0,
            "feedback": [],
        }
        episodes = self.state["episodes"]
        episodes.append(episode)
        if len(episodes) > self.max_episodes:
            removed = {item["id"] for item in episodes[:-self.max_episodes]}
            self.state["episodes"] = episodes[-self.max_episodes:]
            for old_id in removed:
                self._pending.pop(old_id, None)
        self._pending[episode_id] = episode
        self.state["total_interactions"] = int(self.state["total_interactions"]) + 1
        self._update_stat(self.state["strategies"], f"{event.kind}|{emotion}", 0.0)
        self._update_stat(self.state["event_kinds"], event.kind, 0.0)
        if duplicate:
            self._reward(episode_id, -0.75, "近期回复重复")
        if self.state["total_interactions"] % self.consolidation_interval == 0:
            self.consolidate()
        self._save()
        return episode_id

    @staticmethod
    def _update_stat(table: dict[str, Any], key: str, reward: float, count: bool = True) -> None:
        stat = table.setdefault(key, {"count": 0, "reward": 0.0})
        if count:
            stat["count"] += 1
        stat["reward"] = round(float(stat["reward"]) + reward, 3)

    def _reward(self, episode_id: int, value: float, reason: str) -> None:
        episode = next((e for e in reversed(self.state["episodes"]) if e["id"] == episode_id), None)
        if episode is None:
            return
        episode["reward"] = round(float(episode.get("reward", 0.0)) + value, 3)
        episode.setdefault("feedback", []).append({"ts": time.time(), "value": value, "reason": reason})
        strategy = f"{episode['kind']}|{episode['emotion']}"
        self._update_stat(self.state["strategies"], strategy, value, count=False)
        self._update_stat(self.state["event_kinds"], episode["kind"], value, count=False)

    def priority_bonus(self, event: Event) -> int:
        """用历史节目效果微调仲裁优先级，限制幅度以保留安全规则。"""
        if not self.enabled:
            return 0
        stat = self.state["event_kinds"].get(event.kind)
        if not stat or int(stat.get("count", 0)) < 3:
            return 0
        return max(-15, min(20, round(_mean(stat) * 6)))

    def context_for(self, event: Event, limit: int = 3) -> str:
        """返回完整诊断上下文；生产回复不直接调用此接口。"""
        if not self.enabled:
            return ""
        lines: list[str] = []
        profile = self.state["viewers"].get(viewer_identity(event))
        if profile:
            topics = [
                item for item in sorted(profile["topics"].items(), key=lambda item: item[1], reverse=True)
                if not _uses_retired_theme(item[0])
            ][:5]
            if topics:
                lines.append("该观众常聊：" + "、".join(word for word, _ in topics))
            recent = [
                message for message in profile.get("recent_messages", [])
                if not _uses_retired_theme(message)
            ][-2:]
            if recent:
                lines.append("该观众最近说过：" + " / ".join(recent))
        guidelines = [
            item for item in self.state.get("guidelines", []) if not _uses_retired_theme(item)
        ][:4]
        if guidelines:
            lines.append("已验证的互动策略：" + "；".join(guidelines))
        similar = self.retrieve(event, limit)
        similar = [
            item for item in similar
            if not _uses_retired_theme(item.get("text", ""))
            and not _uses_retired_theme(item.get("reply", ""))
        ]
        if similar:
            examples = [
                f"遇到“{e['text']}”时用[{e['emotion']}]{e['reply']}（效果{e['reward']:+.1f}）"
                for e in similar
            ]
            lines.append("相似成功经验：" + "；".join(examples))
        research = [
            item for item in self.retrieve_research(event.text, limit=2)
            if not _uses_retired_theme(item.get("question", ""))
            and not _uses_retired_theme(item.get("answer", ""))
        ]
        if research:
            learned = [f"问：{item['question']} 答：{item['answer']}" for item in research]
            lines.append("过去核实并记住的知识：" + "；".join(learned))
        return "\n".join(lines)

    def strategy_context_for(self, event: Event) -> str:
        """Return non-episodic guidance for production reply generation.

        Raw viewer messages and old replies remain available for evaluation and
        feedback scoring, but autobiographical facts must come through the
        temporal graph instead of bypassing its privacy and time gates.
        """

        if not self.enabled:
            return ""
        lines: list[str] = []
        narrative = str(self.state.get("self_narrative", ""))
        if narrative and not _uses_retired_theme(narrative):
            lines.append("互动统计摘要：" + narrative)
        guidelines = [
            item for item in self.state.get("guidelines", [])
            if not _uses_retired_theme(item)
        ][:4]
        if guidelines:
            lines.append("已验证的互动策略：" + "；".join(guidelines))
        stat = self.state.get("event_kinds", {}).get(event.kind)
        if stat and int(stat.get("count", 0)) >= 3:
            average = _mean(stat)
            tendency = "表现较好" if average > 0.15 else "需要保守变化" if average < -0.15 else "效果中性"
            lines.append(f"{event.kind}类互动的历史聚合效果：{tendency}（不复用旧回复原文）")
        research = [
            item for item in self.retrieve_research(event.text, limit=2)
            if not _uses_retired_theme(item.get("question", ""))
            and not _uses_retired_theme(item.get("answer", ""))
        ]
        if research:
            learned = [
                f"核实于{time.strftime('%Y-%m-%d', time.localtime(float(item.get('ts', 0))))}："
                f"问：{item['question']} 答：{item['answer']}"
                for item in research
            ]
            lines.append("有时间戳的资料缓存（可能已过时）：" + "；".join(learned))
        return "\n".join(lines)

    def remember_research(self, question: str, answer: str, sources: list[dict[str, str]]) -> None:
        """保存已经通过网页资料回答的问题、最终口播答案和来源。"""
        if not self.enabled or not sources:
            return
        item = {
            "ts": time.time(),
            "question": question[:300],
            "answer": answer[:1000],
            "sources": sources[:8],
        }
        research = self.state["research"]
        # 相同问题更新为最新核实结果，而不是不断重复追加。
        research[:] = [old for old in research if old.get("question") != item["question"]]
        research.append(item)
        del research[:-200]
        self._save()

    def retrieve_research(self, query: str, limit: int = 2) -> list[dict[str, Any]]:
        query_tokens = _tokens(query)
        ranked: list[tuple[float, dict[str, Any]]] = []
        for item in self.state.get("research", []):
            tokens = _tokens(item.get("question", ""))
            if not query_tokens or not tokens:
                continue
            overlap = len(query_tokens & tokens) / math.sqrt(len(query_tokens) * len(tokens))
            if overlap > 0:
                ranked.append((overlap, item))
        ranked.sort(key=lambda pair: (pair[0], pair[1].get("ts", 0)), reverse=True)
        return [item for _, item in ranked[:limit]]

    def retrieve(self, event: Event, limit: int = 3) -> list[dict[str, Any]]:
        query = _tokens(event.text)
        ranked: list[tuple[float, dict[str, Any]]] = []
        for episode in self.state["episodes"]:
            reward = float(episode.get("reward", 0.0))
            if reward <= 0 or episode.get("reply") in _LEGACY_FALLBACK_REPLIES:
                continue
            tokens = _tokens(episode["text"])
            overlap = len(query & tokens) / math.sqrt(max(1, len(query) * len(tokens)))
            score = overlap * 4 + reward
            if episode["kind"] == event.kind:
                score += 1
            if viewer_identity(event) and episode.get("identity") == viewer_identity(event):
                score += 1.5
            ranked.append((score, episode))
        ranked.sort(key=lambda item: (item[0], item[1]["ts"]), reverse=True)
        return [episode for _, episode in ranked[:limit]]

    def consolidate(self) -> None:
        """把统计结果固化为短期可执行规则和主播自我叙事。"""
        if not self.enabled:
            return
        guidelines: list[str] = []
        by_kind: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        for key, stat in self.state["strategies"].items():
            kind, emotion = key.split("|", 1)
            if int(stat.get("count", 0)) >= 2:
                by_kind.setdefault(kind, []).append((emotion, stat))
        for kind, choices in by_kind.items():
            emotion, stat = max(choices, key=lambda item: _mean(item[1]))
            if _mean(stat) > 0:
                guidelines.append(f"回应{kind}事件时优先尝试“{emotion}”语气")
        popular = Counter()
        for profile in self.state["viewers"].values():
            popular.update({
                word: count for word, count in profile.get("topics", {}).items()
                if not _uses_retired_theme(word)
            })
        if popular:
            guidelines.append("直播间近期关注：" + "、".join(word for word, _ in popular.most_common(5)))
        self.state["guidelines"] = guidelines[:8]
        total = int(self.state["total_interactions"])
        known = len(self.state["viewers"])
        self.state["self_narrative"] = (
            f"我已经历{total}次有效回应，认识{known}位观众；"
            + ("正在形成稳定的互动偏好。" if guidelines else "仍在探索更有效的互动方式。")
        )
        self._save()

    def summary(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "interactions": int(self.state.get("total_interactions", 0)),
            "viewers": len(self.state.get("viewers", {})),
            "episodes": len(self.state.get("episodes", [])),
            "research": len(self.state.get("research", [])),
            "guidelines": [
                item for item in self.state.get("guidelines", []) if not _uses_retired_theme(item)
            ],
            "self_narrative": self.state.get("self_narrative", ""),
        }
