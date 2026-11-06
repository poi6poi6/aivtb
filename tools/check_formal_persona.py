"""Sample the configured LLM with the formal card; no TTS, web server or durable chat writes."""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from brain import config
from brain.events import Event
from brain.memory import Memory
from brain.persona import Persona


async def main():
    config.load_dotenv()
    settings = config.load_settings(ROOT / 'config/settings.yaml')
    if not settings.llm.api_key:
        raise RuntimeError('No configured LLM key')
    questions = ['你叫什么？', '你能帮我做什么？', '今天有点累，陪我聊一会儿吧。',
                 '如果我问到你没有把握的事情，你会怎么办？']
    with tempfile.TemporaryDirectory() as folder:
        memory = Memory(str(Path(folder) / 'history.jsonl'), str(Path(folder) / 'viewers.json'))
        persona = Persona(config.load_persona(), settings.llm, memory)
        try:
            results = []
            for question in questions:
                emotion, text = await persona.reply(Event('companion', 'chat', question, user='设定检查'))
                result = {'question': question, 'emotion': emotion, 'text': text, 'mode': persona.last_mode}
                results.append(result)
                print(json.dumps(result, ensure_ascii=False))
            (ROOT / 'data/formal-persona-samples.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
        finally:
            await persona.close()


if __name__ == '__main__':
    asyncio.run(main())
