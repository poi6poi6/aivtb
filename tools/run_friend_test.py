"""Run an isolated invitation-only test room without changing daily settings."""
import os
import secrets
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ROOM = ROOT / 'data' / 'friend-test'


def main():
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    ROOM.mkdir(parents=True, exist_ok=True)
    token_file = ROOM / 'token.txt'
    if not token_file.exists():
        token_file.write_text(secrets.token_urlsafe(32), encoding='utf-8')
    os.environ['VTUBER_SHARE_TOKEN'] = token_file.read_text(encoding='utf-8').strip()
    settings = yaml.safe_load((ROOT / 'config/settings.yaml').read_text(encoding='utf-8'))
    settings.setdefault('webui', {}).update(enabled=True, host='127.0.0.1', port=int(os.environ.get('VTUBER_FRIEND_PORT', '8082')), public_test=True)
    settings['sources'] = dict(bilibili_enabled=False, mock_danmaku_enabled=False, mock_game_enabled=False)
    settings['memory'] = dict(path=str(ROOM / 'memory.jsonl'), viewer_db=str(ROOM / 'viewers.json'))
    for section, filename in [('selective_memory', 'autobiographical.json'), ('learning', 'learning.json'),
                              ('personality_evolution', 'personality.json')]:
        settings.setdefault(section, {}).update(enabled=False, state_path=str(ROOM / filename))
    empty_core = ROOM / 'empty-core.yaml'
    empty_core.write_text('memories: []\n', encoding='utf-8')
    settings['selective_memory']['core_memories_path'] = str(empty_core)
    settings.setdefault('cognition', {}).update(enabled=False, database_path=str(ROOM / 'cognition.db'))
    for section in ('director', 'game_agent'):
        settings.setdefault(section, {})['enabled'] = False
    (ROOM / 'audio').mkdir(exist_ok=True)
    settings['tts']['out_dir'] = str(ROOM / 'audio')
    settings_path = ROOM / 'settings.yaml'
    settings_path.write_text(yaml.safe_dump(settings, allow_unicode=True), encoding='utf-8')
    os.environ['VTUBER_SETTINGS_PATH'] = str(settings_path)
    from brain.main import main as run
    run()


if __name__ == '__main__':
    main()
