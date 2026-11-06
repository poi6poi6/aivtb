"""HTTP authorization and authoritative playback receipts for broadcast recovery."""
import asyncio
import ast
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from brain.config import DirectorConfig, PlaybackConfig, WebUIConfig
from brain.director import LiveDirector
from brain.playback import SpeechController, SpeechItem
from brain.rehearsal import RehearsalService
from brain.speech_pipeline import SpeechPipeline
from brain.webui import WebUI


class BroadcastWebUITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.playback = SpeechController(PlaybackConfig())
        self.ui = WebUI(WebUIConfig(), asyncio.Queue(), self.tmp.name, {}, playback=self.playback)
        self.ui.on_prepare_standby = AsyncMock()
        self.ui.rehearsal = Mock()
        self.ui.runtime_health = {'ready': True}
        app = web.Application()
        app.router.add_post('/broadcast-action', self.ui._broadcast_action)
        app.router.add_get('/runtime-status', self.ui._runtime_status)
        app.router.add_get('/ws', self.ui._ws)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.addAsyncCleanup(self.ui.close)
        self.origin = str(self.client.make_url('/')).rstrip('/')

    async def action(self, data, *, origin=None):
        return await self.client.post('/broadcast-action', json=data,
                                      headers={'Origin': origin or self.origin})

    async def wait_until(self, predicate):
        async def wait():
            while not predicate():
                await asyncio.sleep(.005)
        await asyncio.wait_for(wait(), 1)

    def item(self, audio_id=71, turn_id=700):
        return SpeechItem(audio_id, '/audio/fixture.wav', '演练测试', '平静', [],
                          {'rehearsal_id': 'fixture-run'}, 10, turn_id=turn_id)

    async def test_local_same_origin_can_prepare_standby(self):
        response = await self.action({'action': 'prepare_standby'})
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), {'ok': True})
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.ui.on_prepare_standby.assert_awaited_once()

    async def test_missing_foreign_origin_nonlocal_and_public_test_denied(self):
        self.assertEqual((await self.client.post('/broadcast-action', json={'action': 'prepare_standby'})).status, 403)
        self.assertEqual((await self.action({'action': 'prepare_standby'}, origin='https://foreign.invalid')).status, 403)
        self.assertEqual((await self.client.post('/broadcast-action', json={'action': 'prepare_standby'},
            headers={'Host': 'foreign.invalid', 'Origin': 'http://foreign.invalid'})).status, 403)
        transport = Mock()
        transport.get_extra_info.return_value = ('192.0.2.15', 1234)
        request = make_mocked_request('POST', '/broadcast-action', headers={
            'Host': 'localhost', 'Origin': 'http://localhost'}, transport=transport)
        with self.assertRaises(web.HTTPForbidden):
            await self.ui._broadcast_action(request)
        self.ui.cfg.public_test = True
        self.assertEqual((await self.action({'action': 'prepare_standby'})).status, 403)
        self.ui.on_prepare_standby.assert_not_awaited()

    async def test_bad_shapes_busy_reason_and_safe_unexpected_errors(self):
        for data in (None, [], {}, {'action': 'play_fake_sound'}):
            response = await self.action(data)
            self.assertEqual(response.status, 400)
        bad_json = await self.client.post('/broadcast-action', data='{bad', headers={
            'Origin': self.origin, 'Content-Type': 'application/json'})
        self.assertEqual(bad_json.status, 400)
        self.ui.on_prepare_standby.assert_not_awaited()
        self.ui.on_prepare_standby.side_effect = ValueError('播放已暂停，请先恢复播放')
        response = await self.action({'action': 'prepare_standby'})
        self.assertEqual(response.status, 400)
        self.assertIn('播放已暂停', (await response.json())['error'])
        for error in (RuntimeError('secret provider response'), OSError('C:/private/audio.wav')):
            self.ui.on_prepare_standby.side_effect = error
            response = await self.action({'action': 'prepare_standby'})
            self.assertEqual(response.status, 503)
            body = await response.json()
            self.assertEqual(body, {'error': '提示音准备未确认，请刷新状态后重试'})
        self.ui.on_prepare_standby = None
        self.assertEqual((await self.action({'action': 'prepare_standby'})).status, 400)

    async def test_runtime_snapshot_reports_recovery_warning_without_hiding_workers(self):
        self.ui.runtime_started = True
        self.ui.runtime_supervisor = Mock()
        self.ui.runtime_supervisor.summary.return_value = {
            'ready': True, 'workers': {'speech': {'state': 'running'}}, 'warnings': ['optional_worker']}
        self.ui.broadcast_status = lambda: {'automatic_paused': True, 'standby_ready': False}
        response = await self.client.get('/runtime-status')
        self.assertEqual(response.status, 200)
        state = await response.json()
        self.assertEqual(state['workers']['speech']['state'], 'running')
        self.assertIn('broadcast_recovery', state['warnings'])
        self.assertIn('optional_worker', state['warnings'])
        self.assertTrue(state['broadcast_recovery']['automatic_paused'])
        self.assertTrue(state['ready'], 'Recovery pause is a warning, not an invented worker crash')
        self.ui.broadcast_status = lambda: {'automatic_paused': False}
        self.assertNotIn('broadcast_recovery', self.ui.runtime_snapshot()['warnings'])

    async def test_audio_started_only_confirms_matching_current_audio_and_turn_once(self):
        self.playback.enqueue(self.item())
        for value in (None, 'not-an-id', 999):
            await self.ui._audio_started(value)
        self.ui.rehearsal.audio_started.assert_not_called()
        self.assertEqual(self.playback.state, 'dispatched')
        await self.ui._audio_started(71)
        self.assertEqual(self.playback.state, 'playing')
        self.ui.rehearsal.audio_started.assert_called_once_with(700)
        await self.ui._audio_started(71)
        self.ui.rehearsal.audio_started.assert_called_once_with(700)
        self.playback.clear()
        await self.ui._audio_started(71)
        self.ui.rehearsal.audio_started.assert_called_once_with(700)

    async def test_rejected_playback_item_does_not_confirm_rehearsal(self):
        self.playback.enqueue(self.item())
        self.playback.item_valid = lambda item: False
        self.ui.metrics = Mock()
        self.ui.director = Mock()
        await self.ui._audio_started(71)
        self.ui.rehearsal.audio_started.assert_not_called()
        self.ui.metrics.record_audio_started.assert_not_called()
        self.ui.metrics.record_stage.assert_not_called()
        self.ui.director.mark_audio_started.assert_not_called()
        self.assertIsNone(self.playback.current)

    async def test_paused_start_receipt_confirms_rehearsal_and_survives_resume(self):
        now = [1800000000.]
        self.playback._clock = lambda: now[0]
        bound = asyncio.Event()

        async def probe(report):
            report('llm', 'passed')
            report('tts', 'passed')
            report.bind_turn(700)
            bound.set()
            await report.wait_for_playback()

        service = RehearsalService(probe, clock=lambda: now[0])
        self.addAsyncCleanup(service.close)
        self.ui.rehearsal = service
        await service.start()
        await asyncio.wait_for(bound.wait(), 1)
        item = self.item()
        item.created_at = now[0]
        self.playback.enqueue(item)
        states = []
        self.playback.on_state = lambda item, state, reason: states.append(state)
        now[0] += .1
        self.playback.set_paused(True)
        now[0] += .1
        await self.ui._audio_started(item.id)
        self.assertTrue(service._started)
        self.assertEqual(self.playback.state, 'paused')
        await self.ui._audio_started(item.id)
        self.assertEqual(states, ['playing'])
        now[0] += 120
        self.playback.set_paused(False)
        self.assertEqual(self.playback.tick(), [])
        self.assertIs(self.playback.current, item)
        # The normal delivery callback completes the rehearsal only after the
        # actual matching finish receipt; a start alone never passes the run.
        self.assertFalse(service.snapshot()['automatic_passed'])
        self.playback.on_delivery = lambda item, completed: service.audio_finished(
            item.turn_id, completed=completed)
        self.playback.audio_finished(item.id)
        await service._task
        self.assertTrue(service.snapshot()['automatic_passed'])

    async def test_start_receipt_hooks_run_before_broadcast_can_replace_current(self):
        self.playback.enqueue(self.item())
        self.ui.metrics = Mock()
        self.ui.metrics.summary.return_value = {}
        self.ui.director = Mock()
        self.ui.director.summary.return_value = {}

        async def controls_during_broadcast(messages):
            self.ui.rehearsal.audio_started.assert_called_once_with(700)
            self.ui.metrics.record_audio_started.assert_called_once_with(700)
            self.ui.director.mark_audio_started.assert_called_once_with(700)
            self.playback.clear()

        self.ui._broadcast_many = controls_during_broadcast
        await self.ui._audio_started(71)
        self.assertIsNone(self.playback.current)

    async def test_only_audio_owner_receipts_and_disconnect_reach_rehearsal(self):
        live = await self.client.ws_connect('/ws?display=1&output=live', origin=self.origin)
        await live.receive_json()
        preview = await self.client.ws_connect('/ws?display=1', origin=self.origin)
        await preview.receive_json()
        await self.ui._broadcast_many(self.playback.enqueue(self.item()))
        await preview.send_json({'type': 'audio_started', 'id': 71})
        # A following injected event is a barrier showing preview's receipt was processed.
        await preview.send_json({'type': 'inject', 'source': 'companion', 'text': 'fixture barrier'})
        await asyncio.wait_for(self.ui.event_queue.get(), 1)
        self.ui.rehearsal.audio_started.assert_not_called()
        await live.send_json({'type': 'audio_started', 'id': 71})
        await self.wait_until(lambda: self.ui.rehearsal.audio_started.called)
        self.ui.rehearsal.audio_started.assert_called_once_with(700)
        await preview.close()
        await self.wait_until(lambda: len(self.ui._sockets) == 1)
        self.ui.rehearsal.playback_disconnected.assert_not_called()
        await live.close()
        await self.wait_until(lambda: self.ui.rehearsal.playback_disconnected.called)
        self.ui.rehearsal.playback_disconnected.assert_called_once()
        self.assertIsNone(self.playback.current)

    async def test_synthesized_old_program_tail_cannot_start_after_mode_or_stage_changes(self):
        # Exercise the actual main.py hooks without starting external providers.
        source = Path(__file__).resolve().parents[1] / 'brain/main.py'
        assignments = [node for node in ast.walk(ast.parse(source.read_text('utf-8')))
                       if isinstance(node, ast.Assign) and isinstance(node.value, ast.Lambda)]
        def production_hook(owner, attribute, environment):
            assignment = next(node for node in assignments if any(
                isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                and target.value.id == owner and target.attr == attribute for target in node.targets))
            return eval(compile(ast.Expression(assignment.value), str(source), 'eval'), environment)

        class FixtureVoice:
            async def speak(self, text, emotion):
                return 'C:/fixture/program.wav'
            async def close(self):
                pass

        async def broadcast(message):
            pass

        for change in ({'live_mode': False}, {'stage': 'vote', 'status_code': 'poll_open'}):
            with self.subTest(change=change):
                now = [1000.0]
                director = LiveDirector(DirectorConfig(enabled=False), clock=lambda: now[0])
                context = {'show_id': 'show', 'card_id': 'card', 'live_mode': True, 'fresh': True,
                           'stage': 'intro', 'status_code': 'intro', 'topic': 'fixture', 'status': '介绍主题'}
                director.set_program_context(context)
                now[0] += 40
                event = director.poll_proactive()
                event.ui_id = 77
                playback = SpeechController(PlaybackConfig(), clock=lambda: now[0])
                pipeline = SpeechPipeline(FixtureVoice(), playback, broadcast)
                environment = {'director': director, 'failed_turns': set(), 'cancelled_questions': set(), 'agent_tasks': None}
                from tests.test_runtime_integration import main_function
                main_function('companion_event_current', environment)
                playback.item_valid = production_hook('playback', 'item_valid', environment)
                pipeline.event_valid = production_hook('speech_pipeline', 'event_valid', environment)
                worker = asyncio.create_task(pipeline.run())
                try:
                    futures = [pipeline.enqueue(turn_id=77, segment_index=i, text=f'旧节目第{i}段',
                        emotion='平静', actions=[], event=event) for i in range(2)]
                    await asyncio.wait_for(asyncio.gather(*futures), 1)
                    self.assertEqual(len(playback.queue), 1)
                    self.assertEqual(playback.queue[0].event['program_key'], event.program_key)
                    playback.audio_started(playback.current.id)
                    director.set_program_context({**context, **change})
                    self.assertFalse(director.program_event_current(event))
                    messages = playback.audio_finished(playback.current.id)
                    self.assertFalse(any(message.get('type') == 'audio' for message in messages))
                    self.assertFalse(playback.queue)
                    self.assertIsNone(playback.current)
                finally:
                    await pipeline.close()
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)


if __name__ == '__main__':
    unittest.main()
