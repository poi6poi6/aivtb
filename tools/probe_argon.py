"""One scripted persona probe using Google API or the official subscription CLI."""
from __future__ import annotations

import asyncio
import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from brain.config import load_dotenv, load_persona, load_settings
from brain.events import Event
from brain.memory import Memory
from brain.persona import Persona


async def _probe(memory: Memory, backend: str | None = None) -> int:
    settings = load_settings()
    selected = backend or settings.persona_llm.backend
    if selected == 'inherit':
        selected = 'antigravity_cli'
    override = replace(settings.persona_llm, backend=selected)
    if selected == 'gemini_api' and override.model == 'gemini-4-argon-medium':
        override = replace(override, model='gemini-4-argon', effort='low')
    elif selected == 'antigravity_cli' and override.model == 'gemini-4-argon':
        override = replace(override, model='gemini-4-argon-medium', effort='medium')
    persona = Persona(load_persona(), settings.llm, memory=memory, persona_llm=override)
    started = time.monotonic()
    try:
        event = Event('companion', 'chat', '我今天想安静待一会儿，你陪我聊两句吧。', user='测试用户')
        # A complete result is required: early streamed words alone do not
        # establish that authentication, model access and the whole turn worked.
        emotion, reply = await asyncio.wait_for(persona.reply(event), timeout=override.timeout_sec + 5)
        success = persona.last_mode == 'llm' and bool(reply)
        print(json.dumps({
            'ok': success, 'requested_model': persona.model_name,
            'reply': reply if success else '', 'emotion': emotion if success else '',
            'error': persona.last_error if not success else '',
            'elapsed_sec': round(time.monotonic() - started, 2),
            'scripted_input': True, 'production_history_used': False,
        }, ensure_ascii=False, indent=2))
        return 0 if success else 1
    except asyncio.TimeoutError:
        print('Argon probe timed out; the request will be closed.', file=sys.stderr)
        return 1
    finally:
        await persona.close()


async def main(backend: str | None = None) -> int:
    with tempfile.TemporaryDirectory(prefix='argon-probe-') as directory:
        root = Path(directory)
        memory = Memory(str(root / 'events.jsonl'), str(root / 'viewers.json'))
        try:
            return await _probe(memory, backend)
        finally:
            await memory.close_persistence()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', choices=('gemini_api', 'antigravity_cli'))
    args = parser.parse_args()
    load_dotenv()
    raise SystemExit(asyncio.run(main(args.backend)))
