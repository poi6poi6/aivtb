"""Run actual main reply helpers with inert model, UI and synthesis boundaries."""
import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from brain.config import SelectiveMemoryConfig
from brain.context_budget import ContextSection, assemble_context, context_sections
from brain.context_preparation import context_budget, prepare_context
from brain.events import Event
from brain.metrics import RuntimeMetrics
from brain.speech_pipeline import SynthesisResult
from tests.test_runtime_integration import main_function


class TextReadinessTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self, *, become_ready_during_stream=False):
        readiness = SimpleNamespace(ready=False)
        finalizers = []
        segments = ['第一段文字。', '第二段文字。']

        async def stream(event, *, extra_context):
            for index, text in enumerate(segments):
                yield SimpleNamespace(text=text, index=index, emotion='平静', actions=[])
                if become_ready_during_stream:
                    readiness.ready = True

        def enqueue(**kwargs):
            result = asyncio.get_running_loop().create_future()
            result.set_result(SynthesisResult(f'fixture-{kwargs["segment_index"]}.wav', []))
            return result

        namespace = dict(
            asyncio=asyncio, time=time, context_budget=context_budget,
            prepare_context=prepare_context, ContextSection=ContextSection,
            assemble_context=assemble_context, context_sections=context_sections,
            SynthesisResult=SynthesisResult, director=Mock(), conversation=Mock(),
            push_event=AsyncMock(), broadcast_notice=AsyncMock(),
            services=SimpleNamespace(voice_available=lambda: readiness.ready, bilibili_activity=None,
                                     topic_context=lambda event: ''),
            autobiography=SimpleNamespace(context_for=AsyncMock(return_value='')),
            cognition=SimpleNamespace(context_for=AsyncMock(return_value=''),
                begin_turn=AsyncMock(return_value=None), complete_turn=AsyncMock(), abort_turn=AsyncMock()),
            settings=SimpleNamespace(selective_memory=SelectiveMemoryConfig(),
                                     tts=SimpleNamespace(backend='indextts2')),
            metrics=RuntimeMetrics(), agent_tasks=None, failed_turns=set(), cancelled_questions=set(),
            question_delivery=Mock(), recovery=Mock(), reply_failed=AsyncMock(),
            speech_pipeline=SimpleNamespace(enqueue=Mock(side_effect=enqueue)),
            persona=SimpleNamespace(stream_reply=stream, last_emotion='平静',
                last_text=''.join(segments), last_actions=[], last_mode='llm',
                last_search_seconds=0, last_ttft=.01, last_error='', plan_for=lambda _: {}),
            avatar=SimpleNamespace(show=AsyncMock()), memory=Mock(), learning=Mock(),
            personality=Mock(), webui=SimpleNamespace(), remember_spoken=Mock(),
            track_finalizer=finalizers.append,
        )
        namespace['conversation'].context_for.return_value = '近期对话'
        namespace['conversation'].correction_context_for.return_value = ''
        namespace['director'].summary.return_value = {'phase_label': '闲聊'}
        namespace['learning'].summary.return_value = {}
        namespace['personality'].summary.return_value = {}
        main_function('finalize_cognition', namespace)
        generate = main_function('generate_event', namespace)
        return generate, namespace, readiness, finalizers

    async def test_not_ready_delivers_complete_text_and_next_ready_turn_synthesizes(self):
        generate, state, readiness, finalizers = self.fixture()
        first = Event('companion', 'chat', '请说两句话。', user='离线观众')
        first.ui_id = 101
        await generate(first)
        await asyncio.gather(*finalizers)

        self.assertTrue(first.text_only_delivery)
        sent = [call.args[0] for call in state['push_event'].await_args_list]
        self.assertEqual([item['text'] for item in sent if item['type'] == 'reply_partial'],
                         ['第一段文字。', '第一段文字。第二段文字。'])
        replies = [item for item in sent if item['type'] == 'reply']
        self.assertEqual([(item['text'], item['segments']) for item in replies],
                         [('第一段文字。第二段文字。', 0)])
        self.assertFalse(any(item['type'] == 'reply_error' for item in sent))
        state['speech_pipeline'].enqueue.assert_not_called()
        state['question_delivery'].expect.assert_not_called()
        state['question_delivery'].cancel.assert_not_called()
        state['conversation'].record_reply.assert_called_once_with(first, '第一段文字。第二段文字。')
        state['broadcast_notice'].assert_awaited_once()
        audit = state['cognition'].complete_turn.await_args
        self.assertIs(audit.args[1], first)
        self.assertEqual(audit.kwargs['expected_segments'], 0)
        self.assertIsNone(audit.kwargs['audio_path'])
        self.assertFalse(audit.kwargs['synthesis_failed'])

        readiness.ready = True
        second = Event('companion', 'chat', '现在再说一次。', user='离线观众')
        second.ui_id = 102
        await generate(second)
        await asyncio.gather(*finalizers)
        self.assertFalse(second.text_only_delivery)
        self.assertEqual(state['speech_pipeline'].enqueue.call_count, 2)
        self.assertEqual([call.kwargs['text'] for call in state['speech_pipeline'].enqueue.call_args_list],
                         ['第一段文字。', '第二段文字。'])
        self.assertTrue(all(call.kwargs['turn_id'] == second.ui_id
                            for call in state['speech_pipeline'].enqueue.call_args_list))
        audit = state['cognition'].complete_turn.await_args
        self.assertEqual(audit.kwargs['expected_segments'], 2)
        self.assertEqual(audit.kwargs['audio_path'], 'fixture-0.wav; fixture-1.wav')
        self.assertFalse(audit.kwargs['synthesis_failed'])
        state['broadcast_notice'].assert_awaited_once()
        state['cognition'].abort_turn.assert_not_awaited()

    async def test_readiness_mid_reply_keeps_the_whole_turn_text_only(self):
        generate, state, readiness, finalizers = self.fixture(become_ready_during_stream=True)
        event = Event('companion', 'chat', '模型恰好在回复时加载完成。')
        event.ui_id = 103
        await generate(event)
        await asyncio.gather(*finalizers)
        self.assertTrue(readiness.ready)
        self.assertTrue(event.text_only_delivery)
        state['speech_pipeline'].enqueue.assert_not_called()
        state['question_delivery'].expect.assert_not_called()
        audit = state['cognition'].complete_turn.await_args
        self.assertEqual(audit.kwargs['text'], '第一段文字。第二段文字。')
        self.assertEqual(audit.kwargs['expected_segments'], 0)

