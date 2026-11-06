"""Offline identity migration. Does not launch web, TTS, game or public tunnels."""
import asyncio
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from brain import config
from brain.cognition import CognitiveBridge
from brain.personality_evolution import PersonalityEvolution


async def main():
    settings = config.load_settings(ROOT / 'config/settings.yaml')
    persona = config.load_persona()
    backup = ROOT / 'data/backups' / ('formal-identity-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
    backup.mkdir(parents=True, exist_ok=True)
    database = Path(settings.cognition.database_path)
    if database.exists():
        with sqlite3.connect(database) as source, sqlite3.connect(backup / 'cognition.db') as dest:
            source.backup(dest)
    personality = PersonalityEvolution(settings.personality_evolution, persona.name)
    personality.close()
    bridge = await CognitiveBridge.create(settings.cognition, persona, strict=True)
    try:
        identity = await bridge.runtime.identities.get()
        assert identity.name == persona.name and persona.revision in identity.narrative
        print(json.dumps({'name': identity.name, 'revision': persona.revision,
            'profile': personality.summary()['profile'], 'energy': personality.summary()['traits']['energy'],
            'relationships_preserved': personality.summary()['relationships'], 'backup': str(backup)}, ensure_ascii=False))
    finally:
        await bridge.close()


if __name__ == '__main__':
    asyncio.run(main())
