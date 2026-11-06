"""安全游戏代理：观察 -> 识别 -> 规划 -> 有限动作 -> 结果复查。"""
from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Any

import cv2
import yaml

from .config import GameAgentConfig
from .events import PRIO_AMBIENT, PRIO_DEATH, PRIO_FIND, Event
from .game_control import (
    ActionRateLimiter,
    KeyboardMouseController,
    OptionalGamepadController,
    emergency_key_pressed,
)
from .game_strategy import LongTermPlanner, PlannerDecision
from .game_vision import OCREngine, VisualRecognizer, WindowCapture


class GameAgent:
    def __init__(
        self,
        cfg: GameAgentConfig,
        event_queue: asyncio.Queue,
        *,
        capture: WindowCapture | None = None,
        recognizer: VisualRecognizer | None = None,
        controller: KeyboardMouseController | None = None,
        clock=time.time,
    ):
        self.cfg = cfg
        self.event_queue = event_queue
        self._clock = clock
        self.profile = self._load_profile(cfg.profile_path)
        self.capture = capture or WindowCapture(cfg.window_title)
        self.ocr = OCREngine(cfg.ocr_enabled, cfg.ocr_language, cfg.tesseract_cmd)
        self.recognizer = recognizer or VisualRecognizer(self.profile, self.ocr)
        self.controller = controller or KeyboardMouseController(
            cfg.allowed_keys, cfg.allow_mouse, cfg.require_foreground, cfg.window_title
        )
        self.gamepad = OptionalGamepadController(cfg.allow_gamepad)
        self.planner = LongTermPlanner(self.profile)
        self.rate_limiter = ActionRateLimiter(cfg.max_actions_per_minute, clock)

        self.observation_enabled = cfg.enabled
        self.control_enabled = False
        self.input_busy = False
        self._input_generation = 0
        self._input_lock = threading.RLock()
        self.input_blocked = lambda: False
        self.emergency_latched = False
        self.emergency_reason = ""
        self.status = "disabled" if not cfg.enabled else "waiting_window"
        self.error = ""
        self.window_title = ""
        self.hwnd = 0
        self.perception: dict[str, Any] = {}
        self.decision: dict[str, Any] = {}
        self.last_action: dict[str, Any] = {}
        self.action_count = 0
        self.frame_count = 0
        self.latest_jpeg: bytes | None = None
        self._last_decision_at = 0.0
        self._last_event_at = 0.0
        self._last_emitted_state = ""

    async def run(self) -> None:
        interval = 1.0 / max(0.2, min(15.0, self.cfg.capture_fps))
        while True:
            started = self._clock()
            if emergency_key_pressed(self.cfg.emergency_stop_key):
                self.emergency_stop(f"键盘急停 {self.cfg.emergency_stop_key.upper()}")
            if not self.observation_enabled:
                self.status = "disabled"
                await asyncio.sleep(min(0.5, interval))
                continue
            try:
                frame = await asyncio.to_thread(self.capture.capture)
                if frame is None:
                    self.status = "waiting_window"
                    self.error = self.capture.error
                else:
                    self.hwnd = frame.hwnd
                    self.window_title = frame.title
                    view = await asyncio.to_thread(self.recognizer.analyze, frame)
                    self.perception = view.as_dict()
                    self.frame_count += 1
                    self.latest_jpeg = self._encode_jpeg(frame.image)
                    self.status = "observing" if not self.control_enabled else "controlling"
                    self.error = self.ocr.error if self.cfg.ocr_enabled and not self.ocr.available else ""
                    await self._maybe_decide(view)
                    await self._emit_state_event(view.state)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.status = "error"
                self.error = str(exc)[:240]
                self.emergency_stop("视觉或控制循环异常")
            elapsed = self._clock() - started
            await asyncio.sleep(max(0.01, interval - elapsed))

    def set_observation(self, enabled: bool) -> bool:
        if not self.cfg.enabled:
            return False
        self.observation_enabled = enabled
        if not enabled:
            self.set_control(False)
            self.status = "disabled"
        return True

    def set_control(self, enabled: bool) -> bool:
        if not enabled:
            with self._input_lock:
                owned_input = self.control_enabled or self.input_busy
                self._input_generation += 1
                self.control_enabled = False
                if owned_input:
                    self.controller.release_all()
                    self.gamepad.release_all()
            return True
        if self.input_blocked():
            self.error = '后台桌面任务正在使用鼠标键盘，请等待完成或取消任务'
            return False
        if not self.cfg.enabled:
            self.error = "游戏代理总开关未启用"
            return False
        if self.cfg.observe_only:
            self.error = "配置仍为 observe_only，已拒绝控制"
            return False
        if not self.cfg.window_title.strip():
            self.error = "未配置目标窗口标题，已拒绝控制"
            return False
        if self.emergency_latched:
            self.error = "急停已锁定，请先手动复位"
            return False
        if not self.observation_enabled:
            self.error = "请先启用画面观察"
            return False
        self.control_enabled = True
        self.error = ""
        return True

    def emergency_stop(self, reason: str = "网页急停") -> None:
        with self._input_lock:
            self._input_generation += 1
            self.emergency_latched = True
            self.emergency_reason = reason
            self.control_enabled = False
            self.controller.release_all()
            self.gamepad.release_all()
        self.status = "emergency_stopped"

    def reset_emergency(self) -> bool:
        if emergency_key_pressed(self.cfg.emergency_stop_key):
            self.error = f"请先松开 {self.cfg.emergency_stop_key.upper()}"
            return False
        self.emergency_latched = False
        self.emergency_reason = ""
        self.error = ""
        self.status = "observing" if self.observation_enabled else "disabled"
        return True

    def summary(self) -> dict[str, Any]:
        return {
            "enabled": self.cfg.enabled,
            "observe_only": self.cfg.observe_only,
            "observation_enabled": self.observation_enabled,
            "control_enabled": self.control_enabled,
            "emergency_latched": self.emergency_latched,
            "emergency_key": self.cfg.emergency_stop_key.upper(),
            "emergency_reason": self.emergency_reason,
            "status": self.status,
            "error": self.error,
            "window": self.window_title or self.cfg.window_title,
            "profile": str(self.profile.get("name", "未命名策略")),
            "capture_available": self.capture.available,
            "ocr_available": self.ocr.available,
            "ocr_error": self.ocr.error,
            "gamepad_available": self.gamepad.available,
            "perception": dict(self.perception),
            "planner": self.planner.summary(),
            "decision": dict(self.decision),
            "last_action": dict(self.last_action),
            "action_count": self.action_count,
            "frame_count": self.frame_count,
            "has_frame": self.latest_jpeg is not None,
        }

    async def _maybe_decide(self, view) -> None:
        now = self._clock()
        if now - self._last_decision_at < max(0.2, self.cfg.decision_interval_sec):
            return
        self._last_decision_at = now
        decision: PlannerDecision = self.planner.decide(view)
        self.decision = decision.as_dict()
        action = decision.action
        if not self.control_enabled or self.emergency_latched or action.kind == "wait":
            return
        if self.input_blocked():
            self.error = '鼠标键盘已由后台桌面任务占用'
            return
        if not self.rate_limiter.allow():
            self.error = "达到每分钟动作上限，暂停输入"
            return
        self.input_busy = True
        generation, hwnd = self._input_generation, self.hwnd
        def permitted():
            return (generation == self._input_generation and self.control_enabled
                    and not self.emergency_latched and not self.input_blocked())
        def start_input(begin):
            # Serialize only the initial physical press with Stop's release.
            # Never hold this lock during a key/button's bounded sleep.
            with self._input_lock:
                if not permitted():
                    return False
                begin()
                return True
        def perform():
            # A queued executor thread can start after Stop (or Stop + Start).
            # Check the captured authorization in the thread doing the input.
            if not permitted():
                return False
            if action.kind == "gamepad_button":
                target_safe = self.controller.validate_target(hwnd)
                return target_safe and permitted() and self.gamepad.tap(
                    action.button, action.duration_ms, start_input=start_input)
            return self.controller.execute(hwnd, action, start_input=start_input)
        pending_input = asyncio.create_task(asyncio.to_thread(perform))
        try:
            success = await asyncio.shield(pending_input)
        except asyncio.CancelledError:
            # Cancelling to_thread does not stop physical input. Keep the lease
            # until its bounded operation actually finishes before detaching.
            await pending_input
            raise
        finally:
            self.input_busy = False
        if generation != self._input_generation:
            return
        self.last_action = {**action.as_dict(), "success": success, "ts": now}
        if success:
            self.action_count += 1
            self.error = ""
        else:
            self.error = self.controller.error or self.gamepad.error or "输入执行失败"

    async def _emit_state_event(self, state: str) -> None:
        if state == self._last_emitted_state:
            return
        now = self._clock()
        previous = self._last_emitted_state
        self._last_emitted_state = state
        if now - self._last_event_at < max(2.0, self.cfg.event_interval_sec):
            return
        descriptions = {
            "dead": ("death", "视觉系统识别到挑战失败或重试界面", PRIO_DEATH),
            "low_health": ("ambient", "视觉系统识别到角色状态偏低", PRIO_AMBIENT),
            "stuck": ("ambient", "角色似乎停在原地，规划器正在尝试脱困", PRIO_AMBIENT),
            "interaction_available": ("find", "画面中出现了可以交互的提示", PRIO_FIND),
            "menu": ("ambient", "游戏进入菜单，自动控制已保持等待", PRIO_AMBIENT),
        }
        payload = descriptions.get(state)
        if payload is None and state == "normal" and previous in descriptions:
            payload = ("find", "游戏状态已经恢复正常", PRIO_FIND)
        if payload is None:
            return
        kind, text, priority = payload
        self._last_event_at = now
        await self.event_queue.put(Event("game", kind, text, priority=priority))

    def _encode_jpeg(self, image) -> bytes | None:
        quality = max(35, min(90, int(self.cfg.jpeg_quality)))
        ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])
        return encoded.tobytes() if ok else None

    @staticmethod
    def _load_profile(path: str) -> dict[str, Any]:
        profile_path = Path(path)
        if not profile_path.is_file():
            return {
                "name": "缺失配置的安全观察策略",
                "strategy": "generic_wasd",
                "long_term_goal": "只观察目标窗口，等待有效策略配置",
            }
        try:
            data = yaml.safe_load(profile_path.read_text(encoding="utf-8")) or {}
            return data if isinstance(data, dict) else {}
        except (OSError, yaml.YAMLError):
            return {}
