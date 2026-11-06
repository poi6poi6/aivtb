"""Real local HTTP/WebSocket and playback lifecycle; synthetic audio, no devices.

This exercises repeated turns alongside a drawing task. It is deliberately not
a listening-quality, microphone or real-model benchmark.
"""
import asyncio
import json
import tempfile
import unittest
import wave
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from brain.agent_tasks import AgentTasks
from brain.config import AgentTasksConfig, LLMConfig, PlaybackConfig, WebUIConfig
from brain.conversation import ConversationState
from brain.events import Event
from brain.metrics import RuntimeMetrics
from brain.playback import SpeechController
from brain.speech_pipeline import SpeechPipeline
from brain.webui import WebUI


class CompanionSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_voice_turns_during_drawing_and_audio_reconnect(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / 'fixture.wav'
            with wave.open(str(audio), 'wb') as output:
                output.setnchannels(1); output.setsampwidth(2); output.setframerate(16000)
                output.writeframes(b'\0\0' * 160)
            class FixtureVoice:
                async def speak(self, text, emotion):
                    await asyncio.sleep(.001)
                    return str(audio)
            queue = asyncio.Queue()
            metrics = RuntimeMetrics()
            playback = SpeechController(PlaybackConfig())
            conversation = ConversationState()
            delivered = []
            def receipt(item, completed):
                event = conversation.record_playback(item, completed)
                if event:
                    delivered.append(event)
            playback.on_delivery = receipt
            ui = WebUI(WebUIConfig(), queue, str(root), {}, playback=playback, metrics=metrics)
            manager = AgentTasks(root, AgentTasksConfig(enabled=True), LLMConfig())
            ui.agent_tasks = manager
            started, finish = asyncio.Event(), asyncio.Event()
            async def drawing(job):
                started.set()
                await finish.wait()
                folder = manager.folder / job['id']; folder.mkdir(exist_ok=True)
                (folder/'drawing.json').write_text(json.dumps({'version':1,'width':1000,'height':750,
                    'title':'验收样例','background':'#ffffff','strokes':[{'phase':'轮廓',
                    'color':'#000000','width':2,'duration':500,'points':[[0,0,1],[100,100,1]]}]}), 'utf-8')
                manager.update(job, state='succeeded', artifacts=['drawing.json'])
            manager.dispatch = drawing
            job = manager.submit('验收样例', 'drawing', owner=True)
            pipeline = SpeechPipeline(FixtureVoice(), playback, ui.broadcast, metrics)
            worker = asyncio.create_task(pipeline.run())
            app = web.Application()
            app.router.add_get('/ws', ui._ws)
            app.router.add_get('/audio/{name}', ui._audio)
            app.router.add_get('/drawing-state/{job_id}', ui._drawing_state)
            app.router.add_get('/experience-report', ui._experience_report)
            try:
                await asyncio.wait_for(started.wait(), 1)
                async with TestClient(TestServer(app)) as client:
                    socket = await client.ws_connect('/ws', origin=str(client.make_url('/')).rstrip('/'))
                    await socket.receive_json()
                    async def receive_audio():
                        while True:
                            message = await asyncio.wait_for(socket.receive_json(), 1)
                            if message.get('type') == 'audio':
                                return message
                    for turn in range(1, 41):
                        event = Event('danmaku','chat',f'陪伴联调第{turn}轮',user='同名',viewer_key=f'u{turn % 2}')
                        metrics.record_event(turn)
                        conversation.observe_event(event)
                        result = await pipeline.enqueue(turn_id=turn, segment_index=0,
                            text='这是合成占位音频对应的测试文字。', emotion='平静', actions=[], event=event)
                        self.assertFalse(result.cancelled)
                        message = await receive_audio()
                        response = await client.get(message['url'])
                        self.assertTrue((await response.read()).startswith(b'RIFF'))
                        self.assertEqual(message['event']['viewer_key'], event.viewer_key)
                        await socket.send_json({'type':'audio_started','id':message['id']})
                        await socket.send_json({'type':'audio_finished','id':message['id'],'reason':'ended'})
                        for _ in range(100):
                            if len(delivered) == turn:
                                break
                            await asyncio.sleep(.001)
                        self.assertEqual(len(delivered), turn)
                        self.assertEqual(delivered[-1].viewer_key, event.viewer_key)
                        if turn % 10 == 0:
                            await socket.close()
                            socket = await client.ws_connect('/ws', origin=str(client.make_url('/')).rstrip('/'))
                            await socket.receive_json()
                        self.assertEqual(job['state'], 'running')
                    finish.set()
                    for _ in range(100):
                        if job['state'] == 'succeeded': break
                        await asyncio.sleep(.005)
                    state = await client.get('/drawing-state/'+job['id'])
                    self.assertEqual((await state.json())['drawing']['stage'], 'sketch')
                    report = await (await client.get('/experience-report')).json()
                    self.assertEqual(report['metrics']['pending'], 0)
                    self.assertEqual(report['metrics']['latency']['audio_start']['samples'], 40)
                    self.assertNotIn('同名', json.dumps(report, ensure_ascii=False))
                    self.assertFalse(pipeline.busy)
                    self.assertIsNone(playback.current)
                    await socket.close()
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
                await manager.close()
