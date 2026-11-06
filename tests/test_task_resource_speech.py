import asyncio
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from importlib.util import find_spec

from brain.config import PlaybackConfig
from brain.playback import SpeechController
from brain.speech_pipeline import SpeechPipeline


class ResourceSpeechTests(unittest.IsolatedAsyncioTestCase):
    async def test_synthesis_waits_for_grant_and_cancel_does_not_call_tts(self):
        voice = SimpleNamespace(speak=AsyncMock(return_value='audio.wav'))
        pipe = SpeechPipeline(voice, SpeechController(PlaybackConfig()), AsyncMock())
        granted = False
        pipe.resource_available = lambda: granted
        task = asyncio.create_task(pipe._speak_when_available('你好', '平静'))
        await asyncio.sleep(.01)
        voice.speak.assert_not_awaited()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        voice.speak.assert_not_awaited()
        granted = True
        self.assertEqual(await pipe._speak_when_available('你好', '平静'), 'audio.wav')
        voice.speak.assert_awaited_once()

    @unittest.skipUnless(find_spec('cv2') and find_spec('numpy'), 'Optional game vision dependencies required')
    async def test_game_cancellation_keeps_resource_until_input_thread_finishes(self):
        from brain.game_agent import GameAgent
        entered, release = threading.Event(), threading.Event()
        def execute(*args, start_input):
            if not start_input(entered.set):
                return False
            release.wait(3)
            return True
        action = SimpleNamespace(kind='key', as_dict=lambda: {})
        decision = SimpleNamespace(action=action, as_dict=lambda: {})
        agent = GameAgent.__new__(GameAgent)
        agent._clock = lambda: 100
        agent._last_decision_at = 0
        agent.cfg = SimpleNamespace(decision_interval_sec=.2)
        agent.planner = SimpleNamespace(decide=lambda view: decision)
        agent.control_enabled, agent.emergency_latched = True, False
        agent._input_generation = 0
        agent._input_lock = threading.RLock()
        agent.input_blocked = lambda: False
        agent.rate_limiter = SimpleNamespace(allow=lambda: True)
        agent.controller = SimpleNamespace(execute=execute)
        agent.hwnd, agent.input_busy = 1, False
        task = asyncio.create_task(agent._maybe_decide(None))
        try:
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(.01)
            self.assertTrue(entered.is_set())
            task.cancel()
            await asyncio.sleep(.01)
            self.assertFalse(task.done())
            self.assertTrue(agent.input_busy)
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            self.assertFalse(agent.input_busy)
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
