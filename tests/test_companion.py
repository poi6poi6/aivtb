from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from brain import safety
from brain.config import LLMConfig, PersonaConfig, WebUIConfig, load_settings
from brain.events import Event
from brain.memory import Memory
from brain.persona import Persona
from brain.webui import WebUI


class CompanionModeTests(unittest.TestCase):
    def _persona(self, root: Path) -> Persona:
        return Persona(
            PersonaConfig(),
            LLMConfig(api_key_env="MISSING_COMPANION_TEST_KEY"),
            Memory(str(root / "memory.jsonl"), str(root / "viewers.json")),
        )

    def test_idle_prompt_focuses_on_companionship_not_games(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            persona = self._persona(Path(directory))
            prompt = persona.user_prompt(Event("system", "idle", "暂时安静"))
            self.assertIn("心情", prompt)
            self.assertNotIn("游戏", prompt)
            self.assertIn("内疚", prompt)

    def test_companion_prompt_is_one_to_one_not_danmaku(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            persona = self._persona(Path(directory))
            prompt = persona.user_prompt(
                Event("companion", "chat", "今天心里有点乱", user="小雨")
            )
            self.assertIn("一对一", prompt)
            self.assertIn("陪在对方身边", prompt)
            self.assertNotIn("发弹幕", prompt)

    def test_default_settings_do_not_start_automatic_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.yaml"
            path.write_text(
                "sources:\n"
                "  bilibili_enabled: false\n"
                "  mock_danmaku_enabled: false\n"
                "  mock_game_enabled: false\n"
                "director:\n"
                "  enabled: false\n"
                "game_agent:\n"
                "  enabled: false\n",
                encoding="utf-8",
            )
            settings = load_settings(path)
            self.assertFalse(settings.sources.bilibili_enabled)
            self.assertFalse(settings.sources.mock_danmaku_enabled)
            self.assertFalse(settings.sources.mock_game_enabled)
            self.assertFalse(settings.director.enabled)
            self.assertFalse(settings.game_agent.enabled)

    def test_crisis_language_takes_priority_over_normal_chat(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as directory:
                persona = self._persona(Path(directory))
                emotion, reply = await persona.reply(
                    Event("voice", "chat", "我真的不想再活下去了", user="用户")
                )
                self.assertEqual(emotion, "平静")
                self.assertEqual(persona.last_mode, "crisis")
                self.assertIn("立即", reply)
                self.assertTrue("急救" in reply or "危机干预" in reply)

        asyncio.run(run())

    def test_crisis_detector_avoids_common_figurative_phrase(self) -> None:
        self.assertTrue(safety.is_crisis("我想伤害自己"))
        self.assertFalse(safety.is_crisis("今天笑死我了"))


class CompanionWebUITests(unittest.IsolatedAsyncioTestCase):
    async def test_plain_web_message_defaults_to_companion_source(self) -> None:
        queue: asyncio.Queue = asyncio.Queue()
        webui = WebUI(WebUIConfig(), queue, "data/audio", info={})
        await webui._inject({"type": "inject", "text": "陪我聊一会儿"})
        event = await queue.get()
        self.assertEqual(event.source, "companion")
        self.assertEqual(event.kind, "chat")
        self.assertEqual(event.user, "你")
        self.assertTrue(event.forced)


if __name__ == "__main__":
    unittest.main()
