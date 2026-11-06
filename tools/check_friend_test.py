"""Verify the live public invitation, assets, WebSocket and synthesized audio."""
import asyncio
import json
import re
from pathlib import Path
import aiohttp

ROOM = Path(__file__).resolve().parents[1] / 'data/friend-test'


async def main():
    log = (ROOM / 'tunnel-error.log').read_text(encoding='utf-8')
    base = re.search(r'https://[a-z0-9-]+\.trycloudflare\.com', log).group()
    token = (ROOM / 'token.txt').read_text().strip()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120), trust_env=True) as session:
        async with session.get(base) as response:
            assert response.status == 403, response.status
        async with session.get(base + '/?key=' + token) as response:
            assert response.status == 200, response.status
            html = await response.text()
            assert 'wss' in html
        for resource in ['/static/motion.js', '/static/presence.js']:
            async with session.get(base + resource) as response:
                assert response.status == 200, (resource, response.status)
        async with session.get(base + '/game-frame.jpg') as response:
            assert response.status == 403
        async with session.ws_connect(base + '/ws', origin=base) as ws:
            hello = await ws.receive_json(timeout=20)
            assert hello['public_test'] is True
            assert 'selective_memory' not in hello
            async with session.ws_connect(base + '/ws', origin=base) as second:
                assert (await second.receive_json(timeout=10))['type'] == 'room_busy'
            await ws.send_json({'type':'inject', 'source':'companion', 'kind':'chat', 'text':'你好，简单打个招呼吧。'})
            for _ in range(100):
                data = await ws.receive_json(timeout=100)
                if data.get('type') in {'reply_error', 'tts_error'}:
                    raise AssertionError(data)
                if data.get('type') == 'reply':
                    print('Text reply received')
                if data.get('type') == 'audio':
                    async with session.get(base + data['url']) as response:
                        audio = await response.read()
                        assert response.status == 200 and audio[:4] == b'RIFF'
                        print('Public audio OK:', len(audio), 'bytes')
                    await ws.send_json({'type':'audio_finished', 'id':data['id'], 'reason':'ended'})
                    break
            else:
                raise AssertionError('No audio received')
        (ROOM / 'invite.txt').write_text(base + '/?key=' + token, encoding='utf-8')
        print('Public auth, page, scripts, restricted endpoint, single guest, WebSocket and TTS passed.')
        print('Invitation saved to data/friend-test/invite.txt')


asyncio.run(main())
