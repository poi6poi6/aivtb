import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from importlib.util import find_spec

if find_spec('cv2') is None or find_spec('numpy') is None:
    raise unittest.SkipTest('Game vision needs the optional requirements-live.txt dependencies')

import numpy as np

from brain.config import GameAgentConfig
from brain.game_agent import GameAgent
from brain.game_control import ActionRateLimiter, InputAction, KeyboardMouseController, OptionalGamepadController
from brain.game_strategy import PlannerDecision
from brain.game_strategy import LongTermPlanner
from brain.game_vision import CapturedFrame, GamePerception, VisualRecognizer, WindowCapture, normalized_crop


class FakeOCR:
    available = True
    error = ""

    def __init__(self, text=""):
        self.text = text

    def read_regions(self, image, regions):
        return self.text


class FakeController:
    available = True
    error = ""

    def __init__(self):
        self.released = 0

    def release_all(self):
        self.released += 1

    def execute(self, hwnd, action, *, start_input=None):
        return start_input(lambda: None) if start_input else True


def perception(state="normal"):
    return GamePerception(
        ts=1, title="test", width=100, height=100, state=state,
        ocr_text="", health=1.0, motion=10, brightness=100,
        edge_density=0.1, stagnant_frames=0, ui={},
    )


class GameAgentTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name)

    def test_normalized_crop_never_escapes_frame(self):
        image = np.zeros((100, 200, 3), dtype=np.uint8)
        crop = normalized_crop(image, [-0.2, 0.2, 1.5, 0.5])
        assert crop.shape == (50, 200, 3)
        assert normalized_crop(image, [0, 0, 0, 0]).size == 0


    def test_visual_recognizer_ocr_state_and_health_bar(self):
        profile = {
            "recognition": {
                "death_keywords": ["game over"],
                "health_bar": {
                    "rect": [0, 0, 1, 0.1],
                    "hsv_min": [0, 80, 60],
                    "hsv_max": [12, 255, 255],
                },
            }
        }
        image = np.zeros((100, 200, 3), dtype=np.uint8)
        image[:10, :100] = [0, 0, 255]
        view = VisualRecognizer(profile, FakeOCR("GAME OVER")).analyze(
            CapturedFrame(1, "test", image, ts=2)
        )
        assert view.state == "dead"
        assert 0.45 <= view.health <= 0.55


    def test_visual_recognizer_finds_profile_color_marker(self):
        profile = {
            "recognition": {
                "color_objects": [{
                    "name": "player", "hsv_min": [35, 90, 80],
                    "hsv_max": [85, 255, 255], "min_area": 0.001,
                }]
            }
        }
        image = np.zeros((100, 200, 3), dtype=np.uint8)
        image[40:60, 140:160] = [0, 255, 0]
        view = VisualRecognizer(profile, FakeOCR()).analyze(CapturedFrame(1, "test", image))
        assert view.ui["player_visible"] == 1.0
        assert 0.70 <= view.ui["player_x"] <= 0.80


    def test_planner_uses_state_specific_strategy(self):
        profile = {
            "strategy": "generic_wasd",
            "long_term_goal": "完成测试关卡",
            "controls": {"interact": "e", "confirm": "enter"},
        }
        planner = LongTermPlanner(profile)
        interaction = planner.decide(perception("interaction_available"))
        assert interaction.action.key == "e"
        assert interaction.goal == "完成测试关卡"
        dead = planner.decide(perception("dead"))
        assert dead.action.key == "enter"
        assert "重新开始" in "".join(dead.plan)


    def test_waypoint_strategy_and_long_term_task_progress(self):
        profile = {
            "strategy": "screen_waypoints",
            "navigation": {"waypoints": [[0.8, 0.5]], "tolerance": 0.05},
            "controls": {"right": "d"},
            "tasks": [{"name": "找出口", "complete_keywords": ["出口已开"]}],
        }
        planner = LongTermPlanner(profile)
        view = perception()
        view.ui = {"player_visible": 1.0, "player_x": 0.2, "player_y": 0.5}
        decision = planner.decide(view)
        assert decision.action.key == "d"
        assert planner.summary()["current_task"] == "找出口"
        view.ocr_text = "出口已开"
        planner.decide(view)
        assert planner.summary()["task_progress"] == [1, 1]


    def test_profile_rule_strategy_can_declare_bounded_mouse_action(self):
        planner = LongTermPlanner({
            "strategy": "profile_rules",
            "state_actions": {
                "interaction_available": {
                    "kind": "mouse_click", "x": 0.4, "y": 0.7, "button": "left",
                }
            },
        })
        action = planner.decide(perception("interaction_available")).action
        assert action.kind == "mouse_click"
        assert action.x == 0.4
        assert action.y == 0.7


    def test_action_rate_limiter_has_hard_window(self):
        now = [100.0]
        limiter = ActionRateLimiter(2, lambda: now[0])
        assert limiter.allow()
        assert limiter.allow()
        assert not limiter.allow()
        now[0] += 60
        assert limiter.allow()


    def test_empty_window_title_never_falls_back_to_desktop(self):
        capture = WindowCapture("")
        assert capture.find_window() is None
        assert "未配置" in capture.error


    def test_game_agent_requires_two_control_gates_and_latched_estop(self):
        tmp_path = self.directory
        profile = tmp_path / "game.yaml"
        profile.write_text("name: test\nstrategy: generic_wasd\n", encoding="utf-8")
        controller = FakeController()
        cfg = GameAgentConfig(
            enabled=True, observe_only=True, window_title="Test Game",
            profile_path=str(profile), ocr_enabled=False,
        )
        agent = GameAgent(cfg, asyncio.Queue(), controller=controller)
        assert not agent.set_control(True)
        assert "observe_only" in agent.error

        agent.cfg.observe_only = False
        agent.input_blocked = lambda: True
        assert not agent.set_control(True)
        assert '后台桌面任务' in agent.error
        released = controller.released
        assert agent.set_control(False)
        assert controller.released == released
        agent.input_blocked = lambda: False
        assert agent.set_control(True)
        agent.emergency_stop("单元测试")
        assert not agent.control_enabled
        assert agent.emergency_latched
        assert controller.released >= 1
        assert not agent.set_control(True)
        with patch("brain.game_agent.emergency_key_pressed", return_value=False):
            assert agent.reset_emergency()
        assert agent.set_control(True)


class GameInputCancellationTests(unittest.IsolatedAsyncioTestCase):
    def agent(self, controller):
        return GameAgent(GameAgentConfig(
            enabled=True, observe_only=False, window_title="Test Game",
            profile_path="", ocr_enabled=False,
        ), asyncio.Queue(), controller=controller, clock=lambda: 100)

    async def test_queued_input_is_revoked_even_after_control_is_reenabled(self):
        loop = asyncio.get_running_loop()
        for stop in ("emergency", "emergency_restart", "control_restart", "none"):
            with self.subTest(stop=stop):
                submitted = asyncio.Event()
                release = threading.Event()
                class Executor(ThreadPoolExecutor):
                    calls = 0
                    def submit(self, fn, *args, **kwargs):
                        future = super().submit(fn, *args, **kwargs)
                        self.calls += 1
                        if self.calls == 2:
                            submitted.set()
                        return future
                executor = Executor(max_workers=1)
                loop.set_default_executor(executor)
                blocked = loop.run_in_executor(None, release.wait)
                actions = []
                controller = FakeController()
                controller.execute = lambda hwnd, action, start_input: start_input(lambda: actions.append(action.key))
                agent = self.agent(controller)
                agent.set_control(True)
                pending = asyncio.create_task(agent._maybe_decide(perception()))
                try:
                    await asyncio.wait_for(submitted.wait(), 2)
                    if stop.startswith("emergency"):
                        agent.emergency_stop("test")
                        if stop == "emergency_restart":
                            with patch("brain.game_agent.emergency_key_pressed", return_value=False):
                                self.assertTrue(agent.reset_emergency())
                            self.assertTrue(agent.set_control(True))
                    elif stop == "control_restart":
                        agent.set_control(False)
                        self.assertTrue(agent.set_control(True))
                finally:
                    release.set()
                    await asyncio.wait_for(asyncio.gather(blocked, pending), 2)
                    executor.shutdown(wait=True)
                self.assertEqual(actions, ["w"] if stop == "none" else [])
                self.assertEqual(agent.action_count, int(stop == "none"))
                self.assertFalse(agent.input_busy)

    async def test_gamepad_stop_during_window_check_prevents_press(self):
        controller = FakeController()
        agent = self.agent(controller)
        pressed = []
        def validate(hwnd):
            agent.emergency_stop("test")
            return True
        controller.validate_target = validate
        agent.gamepad = SimpleNamespace(
            release_all=lambda: None, tap=lambda *args: pressed.append(args) or True)
        agent.planner = SimpleNamespace(decide=lambda _: PlannerDecision(
            "test", [], InputAction("gamepad_button", button="a"), "test"))
        agent.set_control(True)
        await agent._maybe_decide(perception())
        self.assertEqual(pressed, [])
        self.assertTrue(agent.emergency_latched)

    async def test_keyboard_stop_during_window_check_prevents_native_keydown(self):
        controller = KeyboardMouseController(["w"], False, True)
        agent = self.agent(controller)
        def validate(hwnd):
            agent.emergency_stop("test")
            return True
        controller.validate_target = validate
        calls = []
        api = SimpleNamespace(keybd_event=lambda *args: calls.append(args))
        with patch("brain.game_control.ctypes.windll", SimpleNamespace(user32=api), create=True):
            agent.set_control(True)
            await agent._maybe_decide(perception())
        self.assertFalse(any(args[2] == 0 for args in calls))
        self.assertTrue(agent.emergency_latched)

    async def test_stop_releases_inputs_while_the_press_thread_is_sleeping(self):
        for backend in ("keyboard", "gamepad"):
            with self.subTest(backend=backend):
                controller = KeyboardMouseController(["w"], False, True)
                controller.available = True
                controller.validate_target = lambda hwnd: True
                agent = self.agent(controller)
                held, finish, stopped = threading.Event(), threading.Event(), threading.Event()
                calls = []
                api = SimpleNamespace(keybd_event=lambda vk, scan, flags, extra: calls.append(flags))
                if backend == "gamepad":
                    pad = OptionalGamepadController(False)
                    pad.available = True
                    pad._vg = SimpleNamespace(XUSB_BUTTON=SimpleNamespace(**{
                        "XUSB_GAMEPAD_" + name: name for name in ("A", "B", "X", "Y", "START")}))
                    pad._pad = SimpleNamespace(
                        press_button=lambda button: calls.append("down"),
                        release_button=lambda button: calls.append("up"),
                        reset=lambda: calls.append("reset"), update=lambda: None)
                    agent.gamepad = pad
                    agent.planner = SimpleNamespace(decide=lambda _: PlannerDecision(
                        "test", [], InputAction("gamepad_button", button="a"), "test"))
                def sleep(duration):
                    held.set()
                    finish.wait(3)
                def stop():
                    agent.emergency_stop("test")
                    stopped.set()
                with patch("brain.game_control.ctypes.windll", SimpleNamespace(user32=api), create=True), \
                        patch("brain.game_control.time.sleep", sleep):
                    agent.set_control(True)
                    pending = asyncio.create_task(agent._maybe_decide(perception()))
                    stopper = None
                    try:
                        self.assertTrue(await asyncio.to_thread(held.wait, 1))
                        stopper = threading.Thread(target=stop)
                        stopper.start()
                        self.assertTrue(await asyncio.to_thread(stopped.wait, 1), "Stop waited for the input sleep")
                        self.assertIn(2 if backend == "keyboard" else "reset", calls)
                    finally:
                        finish.set()
                        await asyncio.wait_for(pending, 2)
                        if stopper:
                            stopper.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
