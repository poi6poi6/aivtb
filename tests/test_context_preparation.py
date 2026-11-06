"""Offline checks of bounded recall and actual main/playback integration."""
import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from brain.context_budget import ContextSection, assemble_context, context_sections
from brain.context_preparation import context_budget, prepare_context
from brain.config import PlaybackConfig, SelectiveMemoryConfig
from brain.events import Event
from brain.metrics import RuntimeMetrics
from brain.playback import SpeechController, SpeechItem
from brain.speech_pipeline import SynthesisResult
from tests.test_runtime_integration import main_function


class ContextPreparationTests(unittest.IsolatedAsyncioTestCase):
    async def test_sources_start_together_and_one_timeout_keeps_other_context(self):
        first_started, second_started = asyncio.Event(), asyncio.Event()
        cancelled = asyncio.Event()

        async def first(event):
            first_started.set()
            await second_started.wait()
            return 'confirmed fixture memory'

        async def second(event):
            second_started.set()
            await first_started.wait()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        context, sources = await prepare_context(None, SimpleNamespace(context_for=first),
                                                 SimpleNamespace(context_for=second), timeout=.05)
        self.assertIsInstance(context, str)
        self.assertEqual(context.sections, (ContextSection('memory_recall', 'confirmed fixture memory', 70),))
        self.assertEqual(sources['memory_recall']['status'], 'ready')
        self.assertEqual(sources['cognition_recall']['status'], 'timeout')
        self.assertTrue(cancelled.is_set())

    async def test_unavailable_source_does_not_leak_exception_or_lose_context(self):
        bad = SimpleNamespace(context_for=AsyncMock(side_effect=OSError('PRIVATE_FACT_AND_PATH')))
        good = SimpleNamespace(context_for=AsyncMock(return_value='usable'))
        text, sources = await prepare_context(None, bad, good)
        self.assertEqual(text.sections, (ContextSection('cognition_recall', 'usable', 50),))
        self.assertEqual(sources['memory_recall']['status'], 'unavailable')
        self.assertNotIn('PRIVATE_FACT', str(sources))

    async def test_cancellation_stops_both_reads(self):
        started = [asyncio.Event(), asyncio.Event()]
        stopped = []

        def reader(index):
            async def read(event):
                started[index].set()
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped.append(index)
            return SimpleNamespace(context_for=read)

        task = asyncio.create_task(prepare_context(None, reader(0), reader(1)))
        await asyncio.gather(*(event.wait() for event in started))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertCountEqual(stopped, [0, 1])

    async def test_fail_open_source_preserves_unavailable_diagnostic(self):
        source = SimpleNamespace(context_for=AsyncMock(return_value=''), last_context_status='unavailable')
        text, states = await prepare_context(None, source, source)
        self.assertEqual(text, '')
        self.assertEqual(states['cognition_recall']['status'], 'unavailable')

    async def test_both_sources_retain_their_boundaries_and_distinct_priorities(self):
        memory = SimpleNamespace(context_for=AsyncMock(return_value='本人记忆\n{"item":"保留原文"}'))
        cognition = SimpleNamespace(context_for=AsyncMock(return_value='外部认知资料😀'))
        context, _ = await prepare_context(None, memory, cognition)
        self.assertEqual(context_sections(context), (
            ContextSection('memory_recall', '本人记忆\n{"item":"保留原文"}', 70),
            ContextSection('cognition_recall', '外部认知资料😀', 50),
        ))

    async def test_main_counts_memory_preparation_and_publishes_only_private_diagnostics(self):
        event = Event('companion', 'chat', '如何处理这个复杂问题？', user='fixture')
        event.ui_id = 70
        future = asyncio.get_running_loop().create_future()
        future.set_result(SynthesisResult('fixture.wav', []))
        elapsed = [100.0]
        clock = SimpleNamespace(perf_counter=lambda: elapsed[0], time=time.time)

        async def recall(_):
            await asyncio.sleep(0)
            elapsed[0] += .025
            return 'CONFIRMED_MEMORY'

        async def stream(_event, *, extra_context):
            self.assertIn('CONFIRMED_MEMORY', extra_context)
            self.assertEqual([section.name for section in extra_context.sections],
                             ['memory_correction', 'recent_conversation', 'memory_recall'])
            self.assertTrue(extra_context.sections[0].required)
            yield SimpleNamespace(text='离线测试回复。', index=0, emotion='平静', actions=[])

        finalizers = []
        namespace = dict(asyncio=asyncio, time=clock, context_budget=context_budget,
                         prepare_context=prepare_context, SynthesisResult=SynthesisResult,
                         ContextSection=ContextSection, assemble_context=assemble_context,
                         context_sections=context_sections,
                         services=SimpleNamespace(voice_available=lambda: True, bilibili_activity=None,
                                                  topic_context=lambda event: ''),
                         director=Mock(), push_event=AsyncMock(), conversation=Mock(),
                         autobiography=SimpleNamespace(context_for=recall),
                         cognition=SimpleNamespace(context_for=AsyncMock(return_value=''),
                                                   begin_turn=AsyncMock(return_value=None)),
                         settings=SimpleNamespace(selective_memory=SelectiveMemoryConfig(),
                                                  tts=SimpleNamespace(backend='index')),
                         metrics=RuntimeMetrics(), agent_tasks=None, failed_turns=set(),
                         cancelled_questions=set(), question_delivery=Mock(), recovery=Mock(),
                         speech_pipeline=SimpleNamespace(enqueue=Mock(return_value=future)),
                         persona=SimpleNamespace(stream_reply=stream, last_emotion='平静',
                             last_text='离线测试回复。', last_actions=[], last_mode='llm',
                             last_search_seconds=0, last_ttft=.01, last_error='',
                             plan_for=lambda _: {'mode': 'structured', 'intent':'steps', 'guidance':[]}),
                         avatar=SimpleNamespace(show=AsyncMock()), memory=Mock(), learning=Mock(),
                         personality=Mock(), webui=SimpleNamespace(),
                         finalize_cognition=AsyncMock(), track_finalizer=finalizers.append)
        namespace['conversation'].context_for.return_value = 'short context'
        namespace['conversation'].correction_context_for.return_value = '旧资料已删除，不得从历史恢复'
        namespace['director'].summary.return_value = {'phase_label': 'fixture'}
        namespace['learning'].summary.return_value = {}
        namespace['personality'].summary.return_value = {}
        generate = main_function('generate_event', namespace)
        await generate(event)
        await asyncio.gather(*finalizers)
        summary = namespace['metrics'].summary()['latency']
        self.assertEqual(summary['context_prepare']['latest'], .025)
        self.assertEqual(summary['reply']['latest'], .03)
        self.assertEqual(namespace['webui'].reply_diagnostics['turn_id'], 70)
        self.assertNotIn('CONFIRMED_MEMORY', str(namespace['push_event'].call_args_list))

    async def test_main_finalizer_reports_partial_synthesis_and_segment_count(self):
        event = Event('companion', 'chat', 'fixture')
        event.ui_id = 9
        cognition = SimpleNamespace(complete_turn=AsyncMock())
        namespace = dict(asyncio=asyncio, SynthesisResult=SynthesisResult,
                         cognition=cognition, question_delivery=Mock(), metrics=RuntimeMetrics(),
                         settings=SimpleNamespace(tts=SimpleNamespace(backend='index')))
        finalize = main_function('finalize_cognition', namespace)
        futures = [asyncio.get_running_loop().create_future() for _ in range(2)]
        futures[0].set_result(SynthesisResult('fixture.wav', []))
        futures[1].set_result(SynthesisResult(None, [], cancelled=True))
        await finalize(None, event, emotion='平静', text='fixture', actions=[], response_mode='llm',
                       response_latency=.1, synthesis=futures, learning_episode_id=None)
        kwargs = cognition.complete_turn.await_args.kwargs
        self.assertEqual(kwargs['expected_segments'], 2)
        self.assertTrue(kwargs['synthesis_failed'])

    async def test_main_state_hook_catches_unstarted_failure_and_keeps_task_feedback(self):
        player = SpeechController(PlaybackConfig(max_queue=1, max_age_sec=5, start_timeout_sec=1), clock=lambda: 100)
        audit, tasks = Mock(), Mock()
        state = main_function('speech_state', dict(agent_tasks=tasks, playback=player, audit_delivery=audit))
        player.on_state = state
        item = SpeechItem(1, '/audio/1.wav', 'fixture', '平静', [], {}, 10, created_at=100, turn_id=20)
        player.enqueue(item)
        player.audio_finished(1)  # No audio_started: cannot become delivered.
        audit.assert_called_once_with(item, False, 'missing_playback_start')
        self.assertEqual(tasks.speech_state.call_args.args[1], 'played')


class PlaybackFailureReceiptTests(unittest.TestCase):
    def test_dropped_queued_preempted_and_stale_segments_report_failure(self):
        now = [100.0]
        player = SpeechController(PlaybackConfig(max_queue=1, max_age_sec=5), clock=lambda: now[0])
        states = []
        player.on_state = lambda item, state, reason: states.append((item.id, state))

        def item(number, priority=10, age=100):
            return SpeechItem(number, '/audio/test.wav', 'fixture', '平静', [], {}, priority,
                              created_at=age, turn_id=number)

        player.enqueue(item(0, age=10))
        player.enqueue(item(1))
        player.enqueue(item(2))
        player.enqueue(item(3, priority=5))
        player.enqueue(item(4, priority=100))
        now[0] = 106
        player.tick()
        self.assertIn((0, 'failed'), states)
        self.assertIn((1, 'cancelled'), states)
        self.assertIn((2, 'failed'), states)
        self.assertIn((3, 'failed'), states)

    def test_invalid_context_budget_is_finite_and_bounded(self):
        for value in ('invalid', None, float('nan'), float('inf')):
            self.assertEqual(context_budget(value), .75)
        self.assertEqual(context_budget(-1), .05)
        self.assertEqual(context_budget(1000), 5)
