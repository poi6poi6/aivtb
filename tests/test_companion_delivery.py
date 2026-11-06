"""Private reminder output gates using real local sockets and inert models/TTS."""
import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, PropertyMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from brain.companion_features import CompanionFeatures
from brain.config import LLMConfig, PersonaConfig, PlaybackConfig, WebUIConfig
from brain.events import Event
from brain.persona import Persona
from brain.playback import SpeechController, SpeechItem
from brain.runtime_services import RuntimeServices
from brain.speech_pipeline import SpeechPipeline
from brain.webui import WebUI
from tests.test_runtime_integration import main_function
from tests import test_text_readiness


class CompanionDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.now = [time.time()]
        self.queue = asyncio.Queue()
        self.services = RuntimeServices(Path(folder.name), self.queue)
        self.features = self.services.companion = CompanionFeatures(folder.name, clock=lambda: self.now[0])
        self.addAsyncCleanup(self.services.close)
        await self.features.followups.configure({'quiet_start_hour': 0, 'quiet_end_hour': 0,
                                                 'min_idle_sec': 0, 'min_gap_sec': 0})
        self.playback = SpeechController(PlaybackConfig())
        self.ui = WebUI(WebUIConfig(), self.queue, folder.name, {}, playback=self.playback,
                        workspace_root=folder.name, services=self.services)
        self.addAsyncCleanup(self.ui.close)
        app = web.Application()
        app.router.add_get('/ws', self.ui._ws)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.origin = str(self.client.make_url('/')).rstrip('/')

    async def connect(self, query=''):
        socket = await self.client.ws_connect('/ws' + query, origin=self.origin)
        hello = await socket.receive_json()
        self.assertEqual(hello['type'], 'hello')
        return socket, hello

    async def until_marker(self, socket):
        result = []
        while True:
            item = await asyncio.wait_for(socket.receive_json(), 2)
            if item.get('type') == 'fixture_marker':
                return result
            result.append(item)

    async def claimed(self, turn=10):
        item = await self.features.followups.create('PRIVATE_APPOINTMENT', self.now[0] + 60)
        self.now[0] += 61
        claim = await self.features.followups.claim_due(idle=True)
        event = Event('companion', 'followup', claim['text'], user='你', ts=time.time())
        event.ui_id = turn
        event.companion_followup_token = claim['token']
        event.companion_activity_revision = self.features._activity_revision
        self.features._claims[claim['token']] = {'event': event, 'state': 'queued', 'changed_at': self.now[0]}
        await self.features.registered(event)
        return event, item

    def bind_main_gate(self):
        state = {'services': self.services, 'cancelled_questions': set(),
                 'arbiter': SimpleNamespace(remove_turn=Mock()),
                 'speech_pipeline': SimpleNamespace(cancel_turn=AsyncMock())}
        current = main_function('companion_event_current', state)
        cancel = main_function('cancel_invalid_private_followups', state)
        self.ui.on_private_followups_blocked = cancel
        self.playback.item_valid = lambda item: current(item.event)
        return state, current, cancel

    async def test_only_authenticated_controls_without_any_public_output_allow_scheduling(self):
        self.assertFalse(self.services.private_followups_available())
        control, _ = await self.connect()
        self.assertTrue(self.services.private_followups_available())
        for query in ('?display=1', '?output=live'):
            output, _ = await self.connect(query)
            self.assertFalse(self.services.private_followups_available())
            await output.close()
            for _ in range(30):
                if len(self.ui._sockets) == 1:
                    break
                await asyncio.sleep(.01)
            self.assertTrue(self.services.private_followups_available())
        self.ui._program_context = {'live_mode': True}
        self.assertFalse(self.services.private_followups_available())
        self.ui._program_context = {}
        self.ui._local_control_sockets.clear()
        self.assertFalse(self.services.private_followups_available(), 'socket presence is not ownership')
        await control.close()

    async def test_output_handoff_cancels_private_audio_before_hello_and_preserves_explicit_chat(self):
        control, _ = await self.connect()
        event, _ = await self.claimed()
        state, _, _ = self.bind_main_gate()
        metadata = dict(kind='followup', companion_followup_token=event.companion_followup_token,
                        companion_activity_revision=event.companion_activity_revision)
        private = SpeechItem(10, '/audio/private.wav', 'PRIVATE_APPOINTMENT', '平静', [], metadata, 1,
                             turn_id=event.ui_id)
        explicit = SpeechItem(20, '/audio/explicit.wav', '公开的主动聊天', '平静', [], {'kind': 'chat'}, 10, turn_id=20)
        await self.ui._broadcast_many(self.playback.enqueue(private))
        await self.ui._broadcast_many(self.playback.enqueue(explicit))
        self.playback.audio_started(private.id)
        output, hello = await self.connect('?output=live')
        self.assertNotIn('PRIVATE_APPOINTMENT', str(hello))
        self.assertIn(event.ui_id, state['cancelled_questions'])
        self.assertEqual(self.playback.current, explicit)
        await self.ui.broadcast({'type': 'reply_partial', 'id': event.ui_id, 'text': 'PRIVATE_APPOINTMENT'})
        await self.ui.broadcast({'type': 'audio', 'id': 999, 'event': metadata, 'text': 'PRIVATE_APPOINTMENT'})
        await self.ui.broadcast({'type': 'fixture_marker'})
        frames = await self.until_marker(output)
        self.assertNotIn('PRIVATE_APPOINTMENT', str(frames))
        self.assertTrue(any(frame.get('type') == 'audio' and frame['id'] == explicit.id for frame in frames))
        await self.ui.broadcast({'type': 'fixture_marker'})
        control_frames = await self.until_marker(control)
        self.assertTrue(any(frame.get('type') == 'audio_control' and frame.get('action') == 'stop'
                            and frame.get('id') == private.id for frame in control_frames))
        self.assertEqual((await self.features.followups.list_items())[0]['status'], 'cancelled')

    async def test_cancel_reschedule_and_disable_invalidate_generation_and_playback(self):
        await self.connect()
        _, current, cancel = self.bind_main_gate()
        for mutation in ('cancel', 'reschedule', 'disable'):
            event, item = await self.claimed(turn=10 + len(self.features._events))
            self.assertTrue(current(event))
            metadata = dict(kind='followup', companion_followup_token=event.companion_followup_token,
                            companion_activity_revision=event.companion_activity_revision)
            if mutation == 'cancel':
                await self.features.followups.cancel(item['id'])
            elif mutation == 'reschedule':
                await self.features.followups.update(item['id'], due_at=self.now[0] + 3600)
            else:
                await self.features.followups.configure({'enabled': False})
            self.assertFalse(current(event))
            self.assertFalse(current(metadata))
            await cancel()
            self.assertTrue(self.features._deliveries[event.ui_id]['finished'])
            self.assertFalse(current(Event('companion', 'followup', 'missing token')))
            self.assertTrue(current(Event('companion', 'chat', '仍可主动聊天')))

    async def test_main_rechecks_each_streamed_segment_and_does_not_publish_late_text(self):
        await self.connect()
        event, item = await self.claimed()
        generate, state, _, finalizers = test_text_readiness.TextReadinessTests().fixture()
        state['services'] = self.services
        state['services'].voice_available = lambda: False

        async def stream(*args, **kwargs):
            yield SimpleNamespace(text='第一段。', index=0, emotion='平静', actions=[])
            await self.features.followups.cancel(item['id'])
            yield SimpleNamespace(text='取消后不可发出的第二段。', index=1, emotion='平静', actions=[])

        state['persona'].stream_reply = stream
        with self.assertRaises(asyncio.CancelledError):
            await generate(event)
        messages = [call.args[0] for call in state['push_event'].await_args_list]
        self.assertEqual([message['text'] for message in messages if message['type'] == 'reply_partial'], ['第一段。'])
        self.assertFalse(any(message['type'] == 'reply' for message in messages))
        state['memory'].record.assert_not_called()
        state['learning'].learn_reply.assert_not_called()
        await asyncio.gather(*finalizers)

    async def test_completed_private_followup_does_not_enter_shared_history_or_learning(self):
        await self.connect()
        event, _ = await self.claimed()
        generate, state, _, finalizers = test_text_readiness.TextReadinessTests().fixture()
        state['services'] = self.services
        self.services.voice_available = lambda: False
        await generate(event)
        await asyncio.gather(*finalizers)
        state['conversation'].record_reply.assert_not_called()
        state['remember_spoken'].assert_not_called()
        state['memory'].record.assert_not_called()
        state['learning'].learn_reply.assert_not_called()
        state['director'].after_reply.assert_not_called()
        self.assertEqual((await self.features.followups.list_items())[0]['status'], 'delivered')

    async def test_mock_tts_uses_text_delivery_instead_of_waiting_for_impossible_audio_receipts(self):
        await self.connect()
        event, _ = await self.claimed()
        generate, state, _, finalizers = test_text_readiness.TextReadinessTests().fixture()
        state['services'] = self.services
        state['settings'].tts.backend = 'mock'
        await generate(event)
        await asyncio.gather(*finalizers)
        self.assertEqual((await self.features.followups.list_items())[0]['status'], 'delivered')

    async def test_private_playback_receipt_does_not_enter_shared_history(self):
        await self.connect()
        event, _ = await self.claimed()
        await self.features.generated(event, 'PRIVATE_APPOINTMENT', '平静', segments=1)
        finalizers = []
        state = dict(services=self.services, asyncio=asyncio, audit_delivery=Mock(),
                     question_delivery=Mock(), track_finalizer=finalizers.append,
                     conversation=Mock(), remember_spoken=Mock())
        delivered = main_function('delivered', state)
        delivered(SpeechItem(10, '/audio/private.wav', 'PRIVATE_APPOINTMENT', '平静', [],
                            {'companion_followup_token': event.companion_followup_token},
                            1, turn_id=event.ui_id), True)
        await asyncio.gather(*finalizers)
        state['conversation'].record_playback.assert_not_called()
        state['remember_spoken'].assert_not_called()
        self.assertEqual((await self.features.followups.list_items())[0]['status'], 'delivered')

    async def test_synthesis_preserves_private_token_for_playback_revalidation(self):
        await self.connect()
        event, item = await self.claimed()
        _, current, _ = self.bind_main_gate()
        ready, release = asyncio.Event(), asyncio.Event()

        async def speak(*_):
            ready.set()
            await release.wait()
            return 'fixture.wav'

        messages = []
        pipeline = SpeechPipeline(SimpleNamespace(speak=speak), self.playback, AsyncMock(side_effect=messages.append))
        pipeline.event_valid = current
        self.addAsyncCleanup(pipeline.close)
        asyncio.create_task(pipeline.run())
        result = pipeline.enqueue(turn_id=event.ui_id, segment_index=0, text='PRIVATE_APPOINTMENT',
                                  emotion='平静', actions=[], event=event)
        await asyncio.wait_for(ready.wait(), 2)
        release.set()
        await asyncio.wait_for(result, 2)
        self.assertEqual(self.playback.current.event['companion_followup_token'], event.companion_followup_token)
        await self.features.followups.cancel(item['id'])
        self.assertFalse(current(self.playback.current.event))
        self.assertFalse(any(message.get('type') == 'audio' for message in self.playback.redispatch_current()))

    async def test_persona_stream_complete_and_fallback_keep_private_reply_out_of_global_cache(self):
        persona = Persona(PersonaConfig(), LLMConfig(api_key_env='NO_REAL_COMPANION_DELIVERY_KEY'), None)
        self.addAsyncCleanup(persona.close)
        event = Event('companion', 'followup', 'PRIVATE_APPOINTMENT', user='你')
        event.companion_followup_token = 'fixture-token'

        async def stream(*args, **kwargs):
            yield '[平静]PRIVATE_APPOINTMENT 到了。'

        persona._stream_llm = stream
        persona._call_llm = AsyncMock(return_value='[平静]PRIVATE_APPOINTMENT 到了。')
        with patch.object(LLMConfig, 'api_key', new_callable=PropertyMock, return_value='fixture'):
            self.assertTrue([segment async for segment in persona.stream_reply(event)])
            await persona.reply(event)
        persona._fallback(event)
        self.assertEqual(list(persona._recent_replies), [])
        public_prompt = persona.system_prompt(Event('danmaku', 'chat', '你刚才说了什么', user='群友'))
        self.assertNotIn('PRIVATE_APPOINTMENT', public_prompt)


if __name__ == '__main__':
    unittest.main()
