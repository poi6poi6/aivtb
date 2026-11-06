"""Check the private friend room before publishing a tunnel."""
import asyncio
import json
from pathlib import Path
from urllib.parse import quote
import aiohttp

ROOT = Path(__file__).resolve().parents[1]


async def main():
    token = (ROOT / 'data/friend-test/token.txt').read_text().strip()
    base = 'http://127.0.0.1:8083'
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
        async with session.get(base) as response:
            assert response.status == 403
        session.headers['Cookie'] = 'vtuber_test=' + token
        for resource in ['/', '/static/motion.js', '/static/presence.js']:
            async with session.get(base + resource) as response:
                assert response.status == 200, resource
        for resource in ['/minecraft', '/minecraft-status', '/game-frame.jpg']:
            async with session.get(base + resource) as response:
                assert response.status == 403, resource
        model_dir = ROOT / 'skin/avatar'
        manifest = model_dir / 'avatar.model3.json'
        if manifest.is_file():
            data = json.loads(manifest.read_text(encoding='utf-8'))['FileReferences']
            resources = [manifest.name, data['Moc'], data['Physics'], *data['Textures']]
            for resource in resources:
                url = base + quote('/skin/avatar/' + resource)
                async with session.get(url) as response:
                    assert response.status == 200, (resource, response.status)
                    payload = await response.read()
                    assert len(payload) == (model_dir / resource).stat().st_size
                    print('Model resource OK:', resource, len(payload))
        else:
            print('Skipping optional local model resource check (no model installed).')
        for _ in range(45):
            try:
                async with session.get('http://127.0.0.1:9890/health') as response:
                    if response.status == 200:
                        break
            except aiohttp.ClientError:
                pass
            await asyncio.sleep(2)
        else:
            raise RuntimeError('TTS not ready')
        async with session.ws_connect(base + '/ws', origin=base) as ws:
            assert (await ws.receive_json())['public_test']
            await ws.send_json({'type':'inject', 'source':'companion', 'kind':'chat', 'text':'你好，简单打个招呼吧。'})
            for _ in range(100):
                msg = await ws.receive_json(timeout=90)
                assert msg.get('type') not in {'reply_error', 'tts_error'}, msg.get('type')
                if msg.get('type') == 'audio':
                    async with session.get(base + msg['url']) as response:
                        audio = await response.read()
                        assert response.status == 200 and audio[:4] == b'RIFF'
                    await ws.send_json({'type':'audio_finished', 'id':msg['id'], 'reason':'ended'})
                    print('Local speech OK:', len(audio), 'bytes')
                    break
            else:
                raise RuntimeError('No speech received')
        print('Local friend page, access restrictions and speech passed.')


if __name__ == '__main__':
    asyncio.run(main())
