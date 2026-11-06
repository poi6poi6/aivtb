"""目标游戏窗口捕获、OCR、UI指标与角色状态识别。"""
from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from typing import Any

import cv2
import numpy as np

try:
    import mss
except ImportError:  # pragma: no cover - 由运行环境决定
    mss = None

try:
    import win32gui
except ImportError:  # pragma: no cover - 非 Windows 测试环境
    win32gui = None


@dataclass
class CapturedFrame:
    hwnd: int
    title: str
    image: np.ndarray
    ts: float = field(default_factory=time.time)


@dataclass
class GamePerception:
    ts: float
    title: str
    width: int
    height: int
    state: str
    ocr_text: str
    health: float | None
    motion: float
    brightness: float
    edge_density: float
    stagnant_frames: int
    ui: dict[str, float]

    def as_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "title": self.title,
            "size": [self.width, self.height],
            "state": self.state,
            "ocr_text": self.ocr_text,
            "health": None if self.health is None else round(self.health, 3),
            "motion": round(self.motion, 2),
            "brightness": round(self.brightness, 1),
            "edge_density": round(self.edge_density, 3),
            "stagnant_frames": self.stagnant_frames,
            "ui": {key: round(value, 3) for key, value in self.ui.items()},
        }


class WindowCapture:
    """只捕获标题匹配的可见窗口客户区，永不回退到全屏捕获。"""

    def __init__(self, title_contains: str):
        self.title_contains = title_contains.strip().lower()
        self.available = mss is not None and win32gui is not None
        self.error = "" if self.available else "缺少 mss 或 pywin32"

    def find_window(self) -> tuple[int, str] | None:
        if not self.available or not self.title_contains:
            if not self.title_contains:
                self.error = "未配置目标窗口标题"
            return None
        matches: list[tuple[int, str]] = []

        def visit(hwnd: int, _: object) -> None:
            if not win32gui.IsWindowVisible(hwnd):
                return
            title = win32gui.GetWindowText(hwnd).strip()
            if title and self.title_contains in title.lower():
                matches.append((hwnd, title))

        win32gui.EnumWindows(visit, None)
        if not matches:
            self.error = f"未找到窗口：{self.title_contains}"
            return None
        self.error = ""
        return matches[0]

    def capture(self) -> CapturedFrame | None:
        found = self.find_window()
        if found is None:
            return None
        hwnd, title = found
        left, top, right, bottom = win32gui.GetClientRect(hwnd)
        screen_left, screen_top = win32gui.ClientToScreen(hwnd, (left, top))
        screen_right, screen_bottom = win32gui.ClientToScreen(hwnd, (right, bottom))
        width, height = screen_right - screen_left, screen_bottom - screen_top
        if width < 32 or height < 32:
            self.error = "目标窗口已最小化或客户区过小"
            return None
        region = {"left": screen_left, "top": screen_top, "width": width, "height": height}
        with mss.mss() as grabber:
            bgra = np.asarray(grabber.grab(region))
        return CapturedFrame(hwnd=hwnd, title=title, image=cv2.cvtColor(bgra, cv2.COLOR_BGRA2BGR))


class OCREngine:
    def __init__(self, enabled: bool, language: str, tesseract_cmd: str = ""):
        self.enabled = enabled
        self.language = language
        self.error = ""
        self._pytesseract = None
        if not enabled:
            self.error = "OCR 已关闭"
            return
        try:
            import pytesseract
        except ImportError:
            self.error = "缺少 pytesseract Python 包"
            return
        command = tesseract_cmd.strip() or shutil.which("tesseract") or ""
        if not command:
            self.error = "未找到 tesseract.exe"
            return
        pytesseract.pytesseract.tesseract_cmd = command
        self._pytesseract = pytesseract

    @property
    def available(self) -> bool:
        return self._pytesseract is not None

    def read_regions(self, image: np.ndarray, regions: list[dict[str, Any]]) -> str:
        if not self.available:
            return ""
        texts: list[str] = []
        targets = regions or [{"name": "full", "rect": [0, 0, 1, 1]}]
        for target in targets:
            crop = normalized_crop(image, target.get("rect", [0, 0, 1, 1]))
            if crop.size == 0:
                continue
            gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
            gray = cv2.resize(gray, None, fx=1.6, fy=1.6, interpolation=cv2.INTER_CUBIC)
            processed = cv2.adaptiveThreshold(
                gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 9
            )
            try:
                text = self._pytesseract.image_to_string(
                    processed, lang=self.language, config="--psm 6"
                ).strip()
            except Exception as exc:  # OCR 语言包缺失等错误只降级视觉，不终止代理
                self.error = str(exc)[:160]
                return ""
            if text:
                texts.append(f"{target.get('name', 'ui')}:{' '.join(text.split())}")
        return " | ".join(texts)[:1000]


class VisualRecognizer:
    def __init__(self, profile: dict[str, Any], ocr: OCREngine):
        self.profile = profile
        self.rules = profile.get("recognition") or {}
        self.ocr = ocr
        self._previous_gray: np.ndarray | None = None
        self._stagnant_frames = 0

    def analyze(self, frame: CapturedFrame) -> GamePerception:
        image = frame.image
        height, width = image.shape[:2]
        sample_width = min(640, width)
        scale = sample_width / width
        small = cv2.resize(image, (sample_width, max(1, round(height * scale))))
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        brightness = float(np.mean(gray))
        edge_density = float(np.mean(cv2.Canny(gray, 80, 180) > 0))
        motion = 0.0
        if self._previous_gray is not None and self._previous_gray.shape == gray.shape:
            motion = float(np.mean(cv2.absdiff(gray, self._previous_gray)))
        self._previous_gray = gray

        threshold = float(self.rules.get("stuck_motion_threshold", 1.8))
        if motion < threshold:
            self._stagnant_frames += 1
        else:
            self._stagnant_frames = 0

        ocr_text = self.ocr.read_regions(image, list(self.rules.get("ocr_regions") or []))
        lowered = ocr_text.lower()
        health = self._health_ratio(image)
        ui = self._ui_metrics(image)
        state = "normal"
        if any(str(word).lower() in lowered for word in self.rules.get("death_keywords", [])):
            state = "dead"
        elif any(str(word).lower() in lowered for word in self.rules.get("menu_keywords", [])):
            state = "menu"
        elif health is not None and health <= float(self.rules.get("low_health_threshold", 0.22)):
            state = "low_health"
        elif any(str(word).lower() in lowered for word in self.rules.get("interact_keywords", [])):
            state = "interaction_available"
        elif self._stagnant_frames >= int(self.rules.get("stuck_frames", 5)):
            state = "stuck"

        return GamePerception(
            ts=frame.ts, title=frame.title, width=width, height=height,
            state=state, ocr_text=ocr_text, health=health, motion=motion,
            brightness=brightness, edge_density=edge_density,
            stagnant_frames=self._stagnant_frames, ui=ui,
        )

    def _health_ratio(self, image: np.ndarray) -> float | None:
        rule = self.rules.get("health_bar")
        if not isinstance(rule, dict):
            return None
        crop = normalized_crop(image, rule.get("rect", [0, 0, 0, 0]))
        if crop.size == 0:
            return None
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        low = np.array(rule.get("hsv_min", [0, 80, 60]), dtype=np.uint8)
        high = np.array(rule.get("hsv_max", [12, 255, 255]), dtype=np.uint8)
        mask = cv2.inRange(hsv, low, high) > 0
        occupied_columns = np.mean(mask, axis=0) > 0.18
        return float(np.mean(occupied_columns))

    def _ui_metrics(self, image: np.ndarray) -> dict[str, float]:
        metrics: dict[str, float] = {}
        for item in self.rules.get("ui_regions", []):
            crop = normalized_crop(image, item.get("rect", [0, 0, 0, 0]))
            if crop.size:
                metrics[str(item.get("name", "region"))] = float(np.mean(crop) / 255.0)
        hsv = None
        image_area = max(1, image.shape[0] * image.shape[1])
        for item in self.rules.get("color_objects", []):
            name = str(item.get("name", "object"))
            hsv = hsv if hsv is not None else cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
            low = np.array(item.get("hsv_min", [0, 0, 0]), dtype=np.uint8)
            high = np.array(item.get("hsv_max", [179, 255, 255]), dtype=np.uint8)
            mask = cv2.inRange(hsv, low, high)
            count, _, stats, centroids = cv2.connectedComponentsWithStats(mask, 8)
            if count <= 1:
                metrics[f"{name}_visible"] = 0.0
                continue
            component = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
            area_ratio = float(stats[component, cv2.CC_STAT_AREA] / image_area)
            if area_ratio < float(item.get("min_area", 0.0001)):
                metrics[f"{name}_visible"] = 0.0
                continue
            center_x, center_y = centroids[component]
            metrics[f"{name}_visible"] = 1.0
            metrics[f"{name}_x"] = float(center_x / max(1, image.shape[1] - 1))
            metrics[f"{name}_y"] = float(center_y / max(1, image.shape[0] - 1))
            metrics[f"{name}_area"] = area_ratio
        return metrics


def normalized_crop(image: np.ndarray, rect: list[float]) -> np.ndarray:
    if len(rect) != 4:
        return image[0:0, 0:0]
    height, width = image.shape[:2]
    x, y, w, h = (float(value) for value in rect)
    left = max(0, min(width, round(x * width)))
    top = max(0, min(height, round(y * height)))
    right = max(left, min(width, round((x + w) * width)))
    bottom = max(top, min(height, round((y + h) * height)))
    return image[top:bottom, left:right]
