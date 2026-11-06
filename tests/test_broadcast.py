import asyncio
import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from brain.config import WebUIConfig
from brain.webui import WebUI


class BroadcastTests(unittest.IsolatedAsyncioTestCase):
    async def receive_message(self, socket, expected_type):
        """Role changes may interleave; all other frames keep their semantics."""
        async def receive():
            while True:
                message = await socket.receive_json()
                if message.get('type') != 'audio_owner':
                    return message

        message = await asyncio.wait_for(receive(), 2)
        self.assertEqual(message.get('type'), expected_type)
        return message

    async def test_audio_owner_disconnect_and_reconnect(self):
        from brain.config import PlaybackConfig
        from brain.playback import SpeechController, SpeechItem
        ui = WebUI(WebUIConfig(), asyncio.Queue(), '.', {})
        player = ui.playback = SpeechController(PlaybackConfig())
        states=[]
        player.on_state=lambda item,state,reason='':states.append((state,reason))
        app=web.Application();app.router.add_get('/ws',ui._ws)
        async with TestClient(TestServer(app)) as client:
            async with client.ws_connect('/ws?display=1', origin=str(client.make_url('/')).rstrip('/')) as preview:
                await preview.receive_json()
                live=await client.ws_connect('/ws?display=1&output=live', origin=str(client.make_url('/')).rstrip('/'))
                await live.receive_json()
                await ui._broadcast_many(player.enqueue(SpeechItem(71,'/audio/test.wav','test','平静',[],{},10)))
                # Acknowledgements from the preview must not complete the live player's audio.
                await preview.send_json({'type':'audio_finished','id':71})
                await live.close()
                for _ in range(100):
                    if states:break
                    await asyncio.sleep(.01)
                self.assertIn(('failed','audio_client_disconnected'),states)
                self.assertFalse(any(state=='played' for state,_ in states))
                async with client.ws_connect('/ws?display=1&output=live', origin=str(client.make_url('/')).rstrip('/')) as reconnected:
                    await reconnected.receive_json()
                    await ui._broadcast_many(player.enqueue(SpeechItem(72,'/audio/test.wav','test','平静',[],{},10)))
                    await reconnected.send_json({'type':'audio_started','id':72})
                    await reconnected.send_json({'type':'audio_finished','id':72})
                    for _ in range(100):
                        if any(state=='played' for state,_ in states):break
                        await asyncio.sleep(.01)
                    self.assertTrue(any(state=='played' for state,_ in states))

    async def test_preview_cannot_steal_live_output(self):
        ui = WebUI(WebUIConfig(), asyncio.Queue(), '.', {})
        app = web.Application()
        app.router.add_get('/ws', ui._ws)
        async with TestClient(TestServer(app)) as client:
            async with client.ws_connect('/ws?display=1&output=live', origin=str(client.make_url('/')).rstrip('/')) as live:
                await live.receive_json()
                async with client.ws_connect('/ws?display=1', origin=str(client.make_url('/')).rstrip('/')) as preview:
                    await preview.receive_json()
                    await ui.broadcast({'type':'audio','id':8})
                    self.assertEqual((await self.receive_message(live, 'audio'))['id'],8)
                    await ui.broadcast({'type':'marker'})
                    await self.receive_message(preview, 'marker')

    async def test_danmaku_display_is_read_only_and_does_not_claim_audio(self):
        ui = WebUI(WebUIConfig(), asyncio.Queue(), '.', {})
        await ui.broadcast_danmaku({'user':'观众', 'text':'你好', 'kind':'chat'})
        app = web.Application()
        app.router.add_get('/danmaku-ws', ui._danmaku_ws)
        async with TestClient(TestServer(app)) as client:
            async with client.ws_connect('/danmaku-ws', origin=str(client.make_url('/')).rstrip('/')) as display:
                history = await display.receive_json()
                self.assertEqual(history['items'][0]['text'], '你好')
                self.assertFalse(ui._sockets)
                await display.send_json({'type':'inject', 'text':'cannot inject'})
                await ui.broadcast_danmaku({'user':'观众', 'text':'礼物', 'kind':'gift'})
                self.assertEqual((await display.receive_json())['item']['kind'], 'gift')
                self.assertTrue(ui.event_queue.empty())

    async def test_display_takes_audio_and_song_ownership(self):
        ui = WebUI(WebUIConfig(), asyncio.Queue(), '.', {})
        app = web.Application()
        app.router.add_get('/ws', ui._ws)
        async with TestClient(TestServer(app)) as client:
            async with client.ws_connect('/ws', origin=str(client.make_url('/')).rstrip('/')) as control:
                self.assertEqual((await control.receive_json())['type'], 'hello')
                async with client.ws_connect('/ws?display=1', origin=str(client.make_url('/')).rstrip('/')) as display:
                    self.assertEqual((await display.receive_json())['type'], 'hello')
                    self.assertEqual((await self.receive_message(control, 'audio_control'))['action'], 'clear')
                    await ui.broadcast({'type':'audio', 'id':1, 'url':'/audio/test.wav'})
                    self.assertEqual((await self.receive_message(display, 'audio'))['id'], 1)
                    await ui.broadcast({'type':'song_request', 'song':{'title':'test'}})
                    song = await self.receive_message(display, 'song_request')
                    self.assertEqual(song['song']['title'], 'test')
                    await ui.broadcast({'type':'marker'})
                    await self.receive_message(control, 'marker')

    async def test_bilibili_uses_one_visible_console_instead_of_hidden_live_player(self):
        ui = WebUI(WebUIConfig(), asyncio.Queue(), '.', {})
        self.addAsyncCleanup(ui.close)
        app = web.Application()
        app.router.add_get('/ws', ui._ws)
        async with TestClient(TestServer(app)) as client:
            origin = str(client.make_url('/')).rstrip('/')
            async with client.ws_connect('/ws', origin=origin) as first:
                await first.receive_json()
                async with client.ws_connect('/ws', origin=origin) as second:
                    await second.receive_json()
                    async with client.ws_connect('/ws?display=1&output=live', origin=origin) as live:
                        await live.receive_json()
                        await self.receive_message(first, 'audio_control')
                        await ui.broadcast({'type':'bilibili_request', 'query':'BV1B7411m7LV'})
                        self.assertEqual((await self.receive_message(first, 'bilibili_request'))['query'], 'BV1B7411m7LV')
                        await ui.broadcast({'type':'marker'})
                        await self.receive_message(second, 'marker')
                        await self.receive_message(live, 'marker')

    async def test_bilibili_without_visible_console_gives_actionable_notice(self):
        ui = WebUI(WebUIConfig(), asyncio.Queue(), '.', {})
        self.addAsyncCleanup(ui.close)
        app = web.Application()
        app.router.add_get('/ws', ui._ws)
        async with TestClient(TestServer(app)) as client:
            async with client.ws_connect('/ws?display=1', origin=str(client.make_url('/')).rstrip('/')) as display:
                await display.receive_json()
                await ui.broadcast({'type':'bilibili_request', 'query':'BV1B7411m7LV'})
                notice = await self.receive_message(display, 'music_status')
                self.assertIn('控制台', notice['text'])
