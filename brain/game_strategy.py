"""长期目标规划与可插拔的游戏操作策略。"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any

from .game_control import InputAction
from .game_vision import GamePerception


@dataclass
class PlannerDecision:
    goal: str
    plan: list[str]
    action: InputAction
    rationale: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "plan": list(self.plan),
            "action": self.action.as_dict(),
            "rationale": self.rationale,
        }


class BaseStrategy:
    def __init__(self, profile: dict[str, Any]):
        self.profile = profile
        self.controls = profile.get("controls") or {}
        self.step = 0

    def key(self, name: str, default: str) -> str:
        return str(self.controls.get(name, default)).lower()

    def decide(self, view: GamePerception) -> tuple[InputAction, str]:
        raise NotImplementedError


class GenericWASDStrategy(BaseStrategy):
    """适合第三人称/第一人称探索的保守策略，一次只执行一个短动作。"""

    def decide(self, view: GamePerception) -> tuple[InputAction, str]:
        self.step += 1
        if view.state == "dead":
            return InputAction("key", self.key("confirm", "enter"), 80, reason="失败后尝试确认重来"), "检测到失败界面"
        if view.state == "menu":
            return InputAction("wait", reason="菜单中等待人工确认"), "检测到菜单，避免误操作"
        if view.state == "interaction_available":
            return InputAction("key", self.key("interact", "e"), 80, reason="响应画面交互提示"), "检测到可交互提示"
        if view.state == "low_health":
            return InputAction("key", self.key("back", "s"), 180, reason="低状态时短暂后撤"), "角色状态偏低，优先脱离"
        if view.state == "stuck":
            escape = (
                self.key("jump", "space"), self.key("left", "a"), self.key("right", "d")
            )[self.step % 3]
            return InputAction("key", escape, 140, reason="停滞恢复动作"), "连续画面缺少运动，尝试脱困"
        if self.step % 9 == 0:
            turn = self.key("left", "a") if (self.step // 9) % 2 else self.key("right", "d")
            return InputAction("key", turn, 100, reason="周期性观察周围"), "探索时调整朝向"
        return InputAction("key", self.key("forward", "w"), 180, reason="继续安全探索"), "画面状态正常"


class SafePlatformerStrategy(BaseStrategy):
    """横版平台游戏示例策略：前进、停滞跳跃、失败重开。"""

    def decide(self, view: GamePerception) -> tuple[InputAction, str]:
        self.step += 1
        if view.state == "dead":
            return InputAction("key", self.key("confirm", "enter"), 80, reason="重新开始关卡"), "识别到失败/重试界面"
        if view.state == "menu":
            return InputAction("wait", reason="菜单内不自动选择"), "菜单需要人工确认"
        if view.state == "stuck" or self.step % 7 == 0:
            return InputAction("key", self.key("jump", "space"), 110, reason="越过障碍"), "停滞或周期性跳跃检查"
        return InputAction("key", self.key("forward", "right"), 160, reason="向关卡前方移动"), "继续横向探索"


class ScreenWaypointStrategy(BaseStrategy):
    """依赖游戏配置中的角色颜色标记，沿归一化屏幕坐标航点移动。"""

    def __init__(self, profile: dict[str, Any]):
        super().__init__(profile)
        navigation = profile.get("navigation") or {}
        self.waypoints = [point for point in navigation.get("waypoints", []) if len(point) == 2]
        self.tolerance = float(navigation.get("tolerance", 0.06))
        self.waypoint_index = 0

    def decide(self, view: GamePerception) -> tuple[InputAction, str]:
        if view.state in {"dead", "menu"}:
            return InputAction("wait", reason="特殊界面暂停路径输入"), "等待特殊界面处理"
        if not self.waypoints:
            return InputAction("wait", reason="没有配置路径航点"), "策略缺少 navigation.waypoints"
        if view.ui.get("player_visible", 0) < 0.5:
            return InputAction("wait", reason="未识别到角色位置"), "等待角色颜色标记出现"
        x, y = view.ui.get("player_x", 0.5), view.ui.get("player_y", 0.5)
        target_x, target_y = (float(value) for value in self.waypoints[self.waypoint_index])
        dx, dy = target_x - x, target_y - y
        if abs(dx) <= self.tolerance and abs(dy) <= self.tolerance:
            if self.waypoint_index < len(self.waypoints) - 1:
                self.waypoint_index += 1
                target_x, target_y = (float(value) for value in self.waypoints[self.waypoint_index])
                dx, dy = target_x - x, target_y - y
            else:
                return InputAction("wait", reason="已到达最后航点"), "路径目标已到达"
        if abs(dx) >= abs(dy):
            key = self.key("right", "d") if dx > 0 else self.key("left", "a")
        else:
            key = self.key("down", "s") if dy > 0 else self.key("up", "w")
        return InputAction("key", key, 110, reason="沿屏幕航点移动"), f"前往航点 {self.waypoint_index + 1}/{len(self.waypoints)}"

    def summary(self) -> dict[str, Any]:
        return {"waypoint": self.waypoint_index + 1, "waypoints": len(self.waypoints)}


class ProfileRuleStrategy(BaseStrategy):
    """由单个游戏配置声明状态到键鼠/手柄动作的映射。"""

    def decide(self, view: GamePerception) -> tuple[InputAction, str]:
        actions = self.profile.get("state_actions") or {}
        spec = actions.get(view.state, actions.get("default", {"kind": "wait"}))
        if not isinstance(spec, dict):
            spec = {"kind": "wait"}
        kind = str(spec.get("kind", "wait"))
        if kind not in {"key", "mouse_click", "mouse_move", "gamepad_button", "wait"}:
            return InputAction("wait", reason="配置动作类型无效"), "拒绝未知的配置动作"
        action = InputAction(
            kind=kind,
            key=str(spec.get("key", "")),
            duration_ms=int(spec.get("duration_ms", 100)),
            x=float(spec.get("x", 0.5)),
            y=float(spec.get("y", 0.5)),
            button=str(spec.get("button", "left")),
            reason=str(spec.get("reason", f"响应游戏状态 {view.state}")),
        )
        return action, f"使用配置规则处理 {view.state}"


STRATEGIES = {
    "generic_wasd": GenericWASDStrategy,
    "safe_platformer": SafePlatformerStrategy,
    "screen_waypoints": ScreenWaypointStrategy,
    "profile_rules": ProfileRuleStrategy,
}


class LongTermPlanner:
    """维护长期目标、近期状态和可解释计划；状态突变时自动重规划。"""

    def __init__(self, profile: dict[str, Any]):
        self.profile = profile
        strategy_name = str(profile.get("strategy", "generic_wasd"))
        strategy_cls = STRATEGIES.get(strategy_name, GenericWASDStrategy)
        self.strategy_name = strategy_name if strategy_name in STRATEGIES else "generic_wasd"
        self.strategy = strategy_cls(profile)
        self.goal = str(profile.get("long_term_goal", "安全观察并探索当前区域"))
        self.tasks = [task for task in profile.get("tasks", []) if isinstance(task, dict)]
        self.task_index = 0
        self.plan = ["观察游戏状态", "处理危险或交互", "执行一个有限动作", "检查动作结果"]
        self.history: deque[str] = deque(maxlen=12)
        self.last_state = ""
        self.replans = 0

    def decide(self, view: GamePerception) -> PlannerDecision:
        self._advance_completed_task(view.ocr_text)
        if self.last_state and view.state != self.last_state:
            self.replans += 1
            self.plan = self._plan_for(view.state)
        self.last_state = view.state
        self.history.append(view.state)
        action, rationale = self.strategy.decide(view)
        return PlannerDecision(self.goal, list(self.plan), action, rationale)

    def _advance_completed_task(self, ocr_text: str) -> None:
        if self.task_index >= len(self.tasks):
            return
        keywords = self.tasks[self.task_index].get("complete_keywords", [])
        lowered = ocr_text.lower()
        if keywords and any(str(word).lower() in lowered for word in keywords):
            self.task_index += 1
            self.replans += 1
            if self.task_index < len(self.tasks):
                task = str(self.tasks[self.task_index].get("name", "继续任务"))
                self.plan = [f"执行任务：{task}", "观察结果", "必要时重新规划"]
            else:
                self.plan = ["长期任务列表已完成", "保持安全观察", "等待人工下达新目标"]

    def _plan_for(self, state: str) -> list[str]:
        if state == "dead":
            return ["确认失败界面", "尝试一次重新开始", "重新观察初始状态"]
        if state == "low_health":
            return ["停止推进", "短暂后撤", "观察状态是否恢复"]
        if state == "stuck":
            return ["确认画面停滞", "尝试跳跃或转向", "检测运动是否恢复"]
        if state == "interaction_available":
            return ["确认交互提示", "执行一次交互", "观察UI或场景变化"]
        if state == "menu":
            return ["停止自动输入", "等待人工处理菜单"]
        return ["观察环境", "沿安全方向探索", "检查进展与危险", "必要时重新规划"]

    def summary(self) -> dict[str, Any]:
        current_task = ""
        if self.task_index < len(self.tasks):
            current_task = str(self.tasks[self.task_index].get("name", ""))
        strategy_summary = getattr(self.strategy, "summary", lambda: {})()
        return {
            "strategy": self.strategy_name,
            "goal": self.goal,
            "plan": list(self.plan),
            "replans": self.replans,
            "recent_states": list(self.history),
            "current_task": current_task,
            "task_progress": [self.task_index, len(self.tasks)],
            "strategy_state": strategy_summary,
        }
