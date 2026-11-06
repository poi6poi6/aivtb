"""受限的 Windows 游戏输入：窗口前台校验、白名单、频率限制与急停。"""
from __future__ import annotations

import ctypes
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

try:
    import win32gui
except ImportError:  # pragma: no cover
    win32gui = None


_VK = {
    **{chr(code).lower(): code for code in range(ord("A"), ord("Z") + 1)},
    "space": 0x20, "enter": 0x0D, "esc": 0x1B,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "shift": 0x10, "ctrl": 0x11, "tab": 0x09,
    **{f"f{index}": 0x6F + index for index in range(1, 13)},
}


def _begin_input(start_input, begin):
    """The agent may atomically authorize the physical press against Stop."""
    if start_input is not None:
        return start_input(begin)
    begin()
    return True


@dataclass(frozen=True)
class InputAction:
    kind: str                    # key | mouse_click | mouse_move | gamepad_button | wait
    key: str = ""
    duration_ms: int = 100
    x: float = 0.5               # 鼠标坐标为目标客户区内的归一化位置
    y: float = 0.5
    button: str = "left"
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "key": self.key, "duration_ms": self.duration_ms,
            "x": self.x, "y": self.y, "button": self.button, "reason": self.reason,
        }


class ActionRateLimiter:
    def __init__(self, max_per_minute: int, clock: Callable[[], float] = time.time):
        self.limit = max(1, max_per_minute)
        self._clock = clock
        self._actions: deque[float] = deque()

    def allow(self) -> bool:
        now = self._clock()
        while self._actions and now - self._actions[0] >= 60:
            self._actions.popleft()
        if len(self._actions) >= self.limit:
            return False
        self._actions.append(now)
        return True


class KeyboardMouseController:
    def __init__(
        self,
        allowed_keys: list[str],
        allow_mouse: bool,
        require_foreground: bool,
        expected_title: str = "",
    ):
        self.allowed_keys = {key.lower() for key in allowed_keys}
        self.allow_mouse = allow_mouse
        self.require_foreground = require_foreground
        self.expected_title = expected_title.strip().lower()
        self.available = hasattr(ctypes, "windll") and win32gui is not None
        self.error = "" if self.available else "Windows 输入接口不可用"

    def execute(self, hwnd: int, action: InputAction, *, start_input=None) -> bool:
        if action.kind == "wait":
            return True
        if not self.validate_target(hwnd):
            return False
        if action.kind == "key":
            return self._tap_key(action.key, action.duration_ms, start_input=start_input)
        if action.kind in {"mouse_click", "mouse_move"}:
            return self._mouse(hwnd, action, start_input=start_input)
        self.error = f"不支持的输入类型：{action.kind}"
        return False

    def validate_target(self, hwnd: int) -> bool:
        """所有输入后端共用的最后一道目标窗口校验。"""
        if not self.available or not hwnd or not win32gui.IsWindow(hwnd):
            self.error = "目标窗口无效"
            return False
        if self.require_foreground and win32gui.GetForegroundWindow() != hwnd:
            self.error = "目标游戏不在前台，已拒绝输入"
            return False
        current_title = win32gui.GetWindowText(hwnd).strip().lower()
        if self.expected_title and self.expected_title not in current_title:
            self.error = "目标窗口标题已变化，已拒绝输入"
            return False
        return True

    def release_all(self) -> None:
        if not self.available:
            return
        for key in self.allowed_keys:
            vk = _VK.get(key)
            if vk is not None:
                ctypes.windll.user32.keybd_event(vk, 0, 0x0002, 0)

    def _tap_key(self, key: str, duration_ms: int, *, start_input=None) -> bool:
        normalized = key.lower()
        vk = _VK.get(normalized)
        if normalized not in self.allowed_keys or vk is None:
            self.error = f"按键不在白名单：{key}"
            return False
        duration = max(20, min(600, int(duration_ms))) / 1000.0
        if not _begin_input(start_input, lambda: ctypes.windll.user32.keybd_event(vk, 0, 0, 0)):
            self.error = "输入授权已撤销"
            return False
        try:
            time.sleep(duration)
        finally:
            ctypes.windll.user32.keybd_event(vk, 0, 0x0002, 0)
        self.error = ""
        return True

    def _mouse(self, hwnd: int, action: InputAction, *, start_input=None) -> bool:
        if not self.allow_mouse:
            self.error = "鼠标控制未获授权"
            return False
        x = max(0.0, min(1.0, float(action.x)))
        y = max(0.0, min(1.0, float(action.y)))
        left, top, right, bottom = win32gui.GetClientRect(hwnd)
        px = left + round((right - left - 1) * x)
        py = top + round((bottom - top - 1) * y)
        screen_x, screen_y = win32gui.ClientToScreen(hwnd, (px, py))
        down, up = (0x0008, 0x0010) if action.button == "right" else (0x0002, 0x0004)
        def begin():
            ctypes.windll.user32.SetCursorPos(screen_x, screen_y)
            if action.kind == "mouse_click":
                ctypes.windll.user32.mouse_event(down, 0, 0, 0, 0)
        if not _begin_input(start_input, begin):
            self.error = "输入授权已撤销"
            return False
        if action.kind == "mouse_click":
            try:
                time.sleep(0.04)
            finally:
                ctypes.windll.user32.mouse_event(up, 0, 0, 0, 0)
        self.error = ""
        return True


class OptionalGamepadController:
    """vgamepad 可选后端；未安装时明确降级，不回退到不受限输入。"""

    def __init__(self, enabled: bool):
        self.enabled = enabled
        self.available = False
        self.error = "手柄控制未启用" if not enabled else ""
        self._pad = None
        self._vg = None
        if not enabled:
            return
        try:
            import vgamepad as vg
            self._vg = vg
            self._pad = vg.VX360Gamepad()
            self.available = True
        except Exception as exc:  # 驱动/包缺失均安全降级
            self.error = f"虚拟手柄不可用：{exc}"[:180]

    def tap(self, button: str, duration_ms: int = 100, *, start_input=None) -> bool:
        if not self.available:
            return False
        mapping = {
            "a": self._vg.XUSB_BUTTON.XUSB_GAMEPAD_A,
            "b": self._vg.XUSB_BUTTON.XUSB_GAMEPAD_B,
            "x": self._vg.XUSB_BUTTON.XUSB_GAMEPAD_X,
            "y": self._vg.XUSB_BUTTON.XUSB_GAMEPAD_Y,
            "start": self._vg.XUSB_BUTTON.XUSB_GAMEPAD_START,
        }
        target = mapping.get(button.lower())
        if target is None:
            self.error = f"不支持的手柄按键：{button}"
            return False
        def begin():
            self._pad.press_button(target)
            self._pad.update()
        if not _begin_input(start_input, begin):
            self.error = "输入授权已撤销"
            return False
        try:
            time.sleep(max(20, min(600, duration_ms)) / 1000.0)
        finally:
            self._pad.release_button(target)
            self._pad.update()
        return True

    def release_all(self) -> None:
        if self.available:
            self._pad.reset()
            self._pad.update()


def emergency_key_pressed(key: str) -> bool:
    if not hasattr(ctypes, "windll"):
        return False
    vk = _VK.get(key.lower())
    return bool(vk is not None and ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)
