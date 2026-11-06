"""Runtime status and main-loop wiring; no external services or real audio data."""
import ast
import asyncio
import inspect
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from brain.audio_retention import AudioRetention
from brain.broadcast_recovery import TurnDeliveryTracker
from brain.config import PlaybackConfig
from brain.events import Event
from brain.playback import SpeechController
from brain.runtime_supervisor import RuntimeSupervisor
from brain.speech_pipeline import SpeechPipeline
from brain.webui import WebUI


def main_function(name, namespace):
    """Execute the actual nested helper without starting main or reading secrets."""
    dependencies = {
        'generate_event': ('companion_event_current',),
        'scheduler_loop': ('companion_event_current',),
        'maintenance_loop': ('companion_event_current', 'cancel_invalid_private_followups'),
        'cancel_invalid_private_followups': ('companion_event_current',),
    }
    for dependency in dependencies.get(name, ()):
        if dependency not in namespace:
            main_function(dependency, namespace)
    path = Path(__file__).parents[1] / 'brain' / 'main.py'
    run = next(node for node in ast.parse(path.read_text('utf-8')).body
               if isinstance(node, ast.AsyncFunctionDef) and node.name == 'run')
    function = next(node for node in run.body
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name)
    module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    exec(compile(module, str(path), 'exec'), namespace)
    return namespace[name]


def runtime_ui(supervisor):
    ui = WebUI.__new__(WebUI)
    ui.runtime_supervisor = supervisor
    ui.runtime_started = True
    ui.persistence_status = lambda: {}
    ui.agent_tasks = None
    ui.audio_retention = None
    ui.upgrade_idle = lambda: False
    ui._outboxes = {}
    ui._slow_connections = 0
    ui.metrics = None
    return ui


class RuntimeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_memory_correction_does_not_enter_shared_conversation(self):
        namespace = dict(time=time, Event=Event, qq_channel=SimpleNamespace(apply_memory_change=AsyncMock()),
            register_event=AsyncMock(), conversation=Mock(), persona=Mock(), director=Mock(),
            memory_change_owner=lambda _: 'owner',
            voice_revisions=SimpleNamespace(apply_memory_change=lambda _: None), push_event=AsyncMock())
        changed = main_function('memory_changed', namespace)
        memory = {'visibility': 'private', 'slot': 'fixture', 'source': {'user': 'owner'}}
        await changed(memory, 'PRIVATE_CORRECTION')
        namespace['qq_channel'].apply_memory_change.assert_awaited_once_with(memory, 'PRIVATE_CORRECTION')
        call = namespace['conversation'].apply_memory_change.call_args
        self.assertIsNone(call.args[1])
        self.assertNotIn('PRIVATE_CORRECTION', str(namespace['push_event'].call_args_list))

    async def test_cleanup_failure_stays_visible_until_cleanup_recovers(self):
        supervisor = RuntimeSupervisor()
        self.addAsyncCleanup(supervisor.close)
        supervisor.start('event_ingest', lambda: asyncio.Event().wait())
        ui = runtime_ui(supervisor)
        with tempfile.TemporaryDirectory() as directory:
            import os
            path = Path(directory) / '1700000000_1.wav'
            path.write_bytes(b'fixture')
            os.utime(path, (time.time() - 8 * 86400,) * 2)
            retention = AudioRetention(directory)
            ui.audio_retention = retention
            with patch.object(Path, 'unlink', side_effect=PermissionError('private path')):
                await retention.cleanup()
            self.assertTrue(ui.runtime_snapshot()['ready'])
            self.assertIn('audio_retention', ui.runtime_snapshot()['warnings'])
            await retention.list_files()
            await retention.pin(path.name)
            self.assertIn('audio_retention', ui.runtime_snapshot()['warnings'])
            await retention.cleanup()
            self.assertNotIn('audio_retention', ui.runtime_snapshot()['warnings'])

    async def test_main_shutdown_flushes_despite_optional_close_failures(self):
        path = Path(__file__).parents[1] / 'brain' / 'main.py'
        tree = ast.parse(path.read_text('utf-8'))
        shutdown = next(node for node in tree.body
                        if isinstance(node, ast.AsyncFunctionDef) and node.name == 'shutdown_step')
        run = next(node for node in tree.body
                   if isinstance(node, ast.AsyncFunctionDef) and node.name == 'run')
        finalizer = next(node for node in reversed(run.body) if isinstance(node, ast.Try)).finalbody
        wrapper = ast.parse('async def shutdown_fixture():\n    pass').body[0]
        wrapper.body = finalizer
        operations = []

        def closer(name, fail=False):
            async def close():
                operations.append(name)
                if fail:
                    raise RuntimeError('DO_NOT_PRINT_PRIVATE_PATH')
            return close

        def personality_close():
            operations.append('personality')
            raise RuntimeError('DO_NOT_PRINT_PRIVATE_PATH')

        namespace = dict(asyncio=asyncio, inspect=inspect, game_agent=None, finalizers=set(),
                         supervisor=SimpleNamespace(close=closer('supervisor', True)),
                         services=SimpleNamespace(close=closer('services', True)),
                         webui=SimpleNamespace(close=closer('webui'), rehearsal=None,
                                               minecraft=SimpleNamespace(close=closer('minecraft', True))),
                         question_delivery=TurnDeliveryTracker(),
                         personality=SimpleNamespace(close=personality_close),
                         autobiography=SimpleNamespace(close=closer('autobiography')),
                         persistence={'memory': SimpleNamespace(close_persistence=closer('flush-memory')),
                                      'learning': SimpleNamespace(close_persistence=closer('flush-learning'))},
                         persona=SimpleNamespace(close=closer('persona')),
                         media_narrator=SimpleNamespace(close=closer('media')),
                         speech_pipeline=SimpleNamespace(close=closer('speech')),
                         cognition=SimpleNamespace(close=closer('cognition')),
                         agent_tasks=SimpleNamespace(close=closer('tasks')),
                         qq_channel=SimpleNamespace(close=closer('qq')))
        module = ast.fix_missing_locations(ast.Module(body=[shutdown, wrapper], type_ignores=[]))
        exec(compile(module, str(path), 'exec'), namespace)
        with patch('builtins.print') as output:
            await namespace['shutdown_fixture']()
        for name in ('supervisor', 'webui', 'personality', 'autobiography', 'flush-memory',
                     'flush-learning', 'persona', 'media', 'speech', 'cognition', 'services', 'tasks'):
            self.assertIn(name, operations)
        self.assertLess(operations.index('services'), operations.index('flush-memory'))
        self.assertLess(operations.index('qq'), operations.index('persona'))
        self.assertLess(operations.index('webui'), operations.index('qq'))
        self.assertLess(operations.index('webui'), operations.index('services'))
        self.assertLess(operations.index('services'), operations.index('flush-learning'))
        self.assertNotIn('DO_NOT_PRINT_PRIVATE_PATH', str(output.call_args_list))

    async def test_webui_reports_critical_failure_startup_and_shutdown(self):
        supervisor = RuntimeSupervisor(max_restarts=0)
        self.addAsyncCleanup(supervisor.close)
        ui = runtime_ui(supervisor)
        self.assertFalse(ui.runtime_snapshot()['ready'])

        async def worker():
            raise OSError('private token')

        await supervisor.start('event_ingest', worker)
        snapshot = ui.runtime_snapshot()
        self.assertFalse(snapshot['ready'])
        self.assertEqual(snapshot['workers']['event_ingest']['state'], 'failed')
        self.assertNotIn('private token', str(snapshot))
        await supervisor.close()
        self.assertFalse(ui.runtime_snapshot()['ready'])

    async def test_persistence_and_task_connection_failures_override_running_workers(self):
        supervisor = RuntimeSupervisor()
        self.addAsyncCleanup(supervisor.close)
        supervisor.start('event_ingest', lambda: asyncio.Event().wait())
        ui = runtime_ui(supervisor)
        self.assertTrue(ui.runtime_snapshot()['ready'])
        ui.persistence_status = lambda: {'memory': {'last_error': 'OSError'}}
        self.assertFalse(ui.runtime_snapshot()['ready'])
        ui.persistence_status = lambda: {}
        ui.agent_tasks = SimpleNamespace(status=lambda: {'enabled': True, 'connected': False})
        self.assertFalse(ui.runtime_snapshot()['ready'])
        ui.agent_tasks = SimpleNamespace(status=lambda: {'enabled': True, 'connected': True})
        self.assertTrue(ui.runtime_snapshot()['ready'])
        ui.runtime_started = False
        self.assertFalse(ui.runtime_snapshot()['ready'])

    async def test_main_protection_preserves_current_queued_and_waiting_synthesis(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = time.time()
            paths = [root / f'1700000000_{i}_index.wav' for i in range(1, 5)]
            import os
            for path in paths:
                path.write_bytes(b'fixture')
                os.utime(path, (now - 8 * 86400,) * 2)
            retention = AudioRetention(root, clock=lambda: now)
            playback = SimpleNamespace(current=SimpleNamespace(url='/audio/' + paths[0].name),
                                       queue=[SimpleNamespace(url='/audio/' + paths[1].name)])
            pipeline = SimpleNamespace(pending_audio_path=str(paths[2]),
                                       pending_audio_paths=lambda: {str(paths[2])})
            protect = main_function('protect_audio', dict(playback=playback,
                                    speech_pipeline=pipeline, audio_retention=retention))
            protect()
            await retention.cleanup()
            self.assertTrue(all(path.exists() for path in paths[:3]))
            self.assertFalse(paths[3].exists())

    async def test_main_cleanup_refreshes_protection_even_when_maintenance_is_down(self):
        operations = []

        async def sleep(_):
            operations.append('wait')

        async def cleanup():
            operations.append('cleanup')
            raise asyncio.CancelledError()

        maintain = main_function('maintain_audio', dict(
            asyncio=SimpleNamespace(sleep=sleep),
            settings=SimpleNamespace(tts=SimpleNamespace(audio_retention_interval_sec=30)),
            protect_audio=lambda: operations.append('protect'),
            audio_retention=SimpleNamespace(cleanup=cleanup)))
        with self.assertRaises(asyncio.CancelledError):
            await maintain()
        self.assertEqual(operations, ['wait', 'protect', 'cleanup'])

    async def test_pipeline_failure_surfaces_and_settles_pending_jobs(self):
        entered = asyncio.Event()

        async def broadcast(payload):
            if payload['type'] == 'speech_stage':
                entered.set()
                raise RuntimeError('broken transport')

        voice = SimpleNamespace(speak=lambda *_: asyncio.sleep(0))
        pipeline = SpeechPipeline(voice, SpeechController(PlaybackConfig()), broadcast)
        supervisor = RuntimeSupervisor()
        self.addAsyncCleanup(supervisor.close)
        self.addAsyncCleanup(pipeline.close)
        worker = supervisor.start('speech', pipeline.run, restart=False)
        futures = [pipeline.enqueue(turn_id=index, segment_index=0, text='hello', emotion='平静',
                                   actions=[], event=Event('companion', 'chat', 'hi'))
                   for index in (1, 2)]
        await entered.wait()
        await worker
        ui = runtime_ui(supervisor)
        self.assertEqual(ui.runtime_snapshot()['workers']['speech']['state'], 'failed')
        self.assertFalse(ui.runtime_snapshot()['ready'])
        self.assertTrue(all(future.done() for future in futures))
        self.assertIsNone(pipeline.pending_audio_path)
        self.assertFalse(pipeline.busy)

    async def test_shutdown_cancels_active_synthesis_and_does_not_restart_it(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def speak(*_):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async def broadcast(_):
            return

        pipeline = SpeechPipeline(SimpleNamespace(speak=speak),
                                  SpeechController(PlaybackConfig()), broadcast)
        supervisor = RuntimeSupervisor()
        self.addAsyncCleanup(supervisor.close)
        self.addAsyncCleanup(pipeline.close)
        supervisor.start('speech', pipeline.run, restart=False)
        future = pipeline.enqueue(turn_id=1, segment_index=0, text='hello', emotion='平静',
                                  actions=[], event=Event('companion', 'chat', 'hi'))
        await entered.wait()
        await supervisor.close()
        self.assertTrue(cancelled.is_set())
        self.assertTrue(future.done())
        self.assertIsNone(pipeline.pending_audio_path)
        self.assertEqual(supervisor.summary()['workers']['speech']['state'], 'stopped')
