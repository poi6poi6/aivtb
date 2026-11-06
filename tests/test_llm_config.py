from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from brain.config import LLMConfig, PersonaConfig
from brain.events import Event
from brain.learning import LearningEngine
from brain.memory import Memory
from brain.persona import Persona


class DeepSeekPayloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_thinking_payload_omits_reasoning_effort(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            llm = LLMConfig(
                base_url="https://api.deepseek.com",
                api_key_env="TEST_DEEPSEEK_KEY",
                model="deepseek-v4-flash",
                reasoning_effort="low",
                thinking=False,
            )
            persona = Persona(
                PersonaConfig(),
                llm,
                Memory(str(root / "memory.jsonl"), str(root / "viewers.json")),
                LearningEngine(str(root / "learning.json"), enabled=False),
            )
            captured = {}

            class Response:
                def raise_for_status(self):
                    return None

                def json(self):
                    return {"choices": [{"message": {"content": "[平静]收到"}}]}

            class Client:
                def __init__(self, **kwargs):
                    pass

                async def post(self, url, *, json, headers):
                    captured.update({"url": url, "json": json, "headers": headers})
                    return Response()

                async def aclose(self):
                    return None

            with patch("brain.persona.httpx.AsyncClient", Client):
                result = await persona._call_llm(
                    Event("danmaku", "chat", "你好"), "test-key"
                )
                await persona.close()

            self.assertEqual(result, "[平静]收到")
            self.assertEqual(captured["url"], "https://api.deepseek.com/chat/completions")
            self.assertEqual(captured["json"]["model"], "deepseek-v4-flash")
            self.assertEqual(captured["json"]["thinking"], {"type": "disabled"})
            self.assertNotIn("reasoning_effort", captured["json"])
            self.assertEqual(captured["headers"]["Authorization"], "Bearer test-key")


if __name__ == "__main__":
    unittest.main()
