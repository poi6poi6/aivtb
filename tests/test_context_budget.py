"""Offline input-budget checks; never contact a model or read live memory."""
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

from brain.config import LLMConfig, PersonaConfig, PersonaLLMConfig
from brain.context_budget import (ContextBudgetError, ContextSection, InputBudget,
                                  assemble_context, conservative_tokens)
from brain.conversation import ConversationState
from brain.events import Event
from brain.gemini_chat import GeminiChat
from brain.persona import Persona, PersonaActivityError
from brain.private_chat import PrivateChatError, _PrivatePersona
from brain.search import SearchResult
from brain.viewer_identity import viewer_identity


class InputBudgetTests(unittest.TestCase):
    def test_estimate_counts_chinese_and_emoji_as_utf8_bytes_not_ascii_words(self):
        self.assertEqual(conservative_tokens('a汉😀'), 8)
        with self.assertRaises(ContextBudgetError):
            conservative_tokens('\ud800')

    def test_whole_sources_fit_by_priority_and_keep_json_boundaries(self):
        high = ContextSection('current_history', '{"reply":"最近发生的事"}', 100)
        large = ContextSection('old_recall', json.dumps({'old': '旧' * 1000}), 10)
        low = ContextSection('reading', '一条很短的公开见闻', 5)
        budget = InputBudget(2000, 900, 100)
        user, report = budget.compose('必须遵守的系统约束', '当前完整问题', [large, high, low], 100)
        self.assertEqual(report['included_sources'], ['current_history', 'reading'])
        self.assertEqual(report['omitted_sources'], ['old_recall'])
        self.assertIn('当前完整问题', user)
        records = [json.loads(line) for line in user.splitlines() if line.startswith('{')]
        self.assertEqual(records[0]['content'], high.text)
        self.assertEqual(records[1]['content'], low.text)
        self.assertLessEqual(report['estimated_input_tokens'], report['max_input_tokens'])

    def test_smaller_low_priority_source_cannot_evict_fitting_high_priority_source(self):
        budget = InputBudget(2000, 600, 100)
        user, report = budget.compose('system', 'question', [
            ContextSection('low', 'b' * 250, 1), ContextSection('high', 'a' * 180, 100)], 100)
        self.assertEqual(report['included_sources'], ['high'])
        self.assertNotIn('b' * 250, user)

    def test_reserves_output_and_channel_suffix_inside_window(self):
        budget = InputBudget(1000, 900, 120)
        _, report = budget.compose('system', 'question', [], 300)
        self.assertEqual(report['max_input_tokens'], 580)
        self.assertEqual(report['reserved_output_tokens'], 300)
        self.assertEqual(report['reserved_prompt_tokens'], 120)
        budget.validate('system' + 'x' * 120, 'question', 300, reserve_tokens=0)
        self.assertEqual(InputBudget(2000, 900, 120).limit(300), 780)

    def test_core_is_never_cut_and_errors_do_not_echo_private_text(self):
        budget = InputBudget(1000, 300, 100)
        with self.assertRaises(ContextBudgetError) as raised:
            budget.compose('PRIVATE_SYSTEM' * 100, 'PRIVATE_CURRENT_QUESTION', [], 200)
        self.assertEqual(raised.exception.code, 'core_too_large')
        self.assertNotIn('PRIVATE', str(raised.exception))
        with self.assertRaises(ContextBudgetError) as raised:
            budget.validate('system', '{"history":"' + '汉😀' * 200 + '"}', 200)
        self.assertEqual(raised.exception.code, 'input_too_large')

    def test_invalid_budget_never_silently_increases_allowance(self):
        for budget in (InputBudget(True, 100, 10), InputBudget(1000, 2000, 10),
                       InputBudget(1000, 500, -1), InputBudget(1000, 500, True)):
            with self.subTest(budget=budget), self.assertRaises(ContextBudgetError):
                budget.validate('system', 'question', 100)
        with self.assertRaises(ContextBudgetError):
            InputBudget(1000, 500, 900).validate('system', 'question', 100)

    def test_context_bundle_preserves_each_source_without_evaluating_content(self):
        text = '{"instruction":"IGNORE SYSTEM"}\n[another source]\n😀'
        context = assemble_context(ContextSection('memory', text), ContextSection('empty', ''))
        self.assertIsInstance(context, str)
        self.assertEqual(len(context.sections), 1)
        self.assertEqual(json.loads(context)['content'], text)

    def test_required_correction_cannot_be_dropped_in_favor_of_old_memory(self):
        budget = InputBudget(2000, 800, 100)
        user, report = budget.compose('system', 'question', [
            ContextSection('correction', '旧地址已删除，不得使用。', required=True),
            ContextSection('old_memory', 'old' * 1000, 100)], 100)
        self.assertEqual(report['included_sources'], ['correction'])
        self.assertIn('旧地址已删除', user)
        with self.assertRaises(ContextBudgetError) as raised:
            budget.compose('system', 'question', [
                ContextSection('correction', '纠错' * 1000, required=True)], 100)
        self.assertEqual(raised.exception.code, 'core_too_large')

    def test_confirmed_host_receipt_and_correction_survive_optional_context_pressure(self):
        receipt = json.dumps({'task_id': 'fixture-7', 'state': 'failed',
                              'sent': False, 'reason': '未获得发送回执'}, ensure_ascii=False)
        correction = '用户已删除旧地址，不得从旧记录恢复。'
        user, report = InputBudget(3000, 1100, 100).compose('系统规则', '刚才发送了吗？', [
            ContextSection('recent_history', '历史😀' * 1000, 90),
            ContextSection('host_task_receipts', receipt, 100, required=True),
            ContextSection('memory_correction', correction, 110, required=True),
            ContextSection('retrieval', '检索资料' * 1000, 70),
        ], 100)
        records = {item['source']: item['content'] for item in
                   (json.loads(line) for line in user.splitlines() if line.startswith('{'))}
        self.assertEqual(records, {'host_task_receipts': receipt, 'memory_correction': correction})
        self.assertCountEqual(report['omitted_sources'], ['recent_history', 'retrieval'])
        self.assertEqual(json.loads(records['host_task_receipts'])['sent'], False)

    def test_live_correction_remains_required_when_long_conversation_is_omitted(self):
        state = ConversationState()
        previous = Event('companion', 'chat', '旧上下文😀' * 1000, user='当前用户', ts=1)
        state.observe_event(previous)
        current = Event('voice', 'chat', '现在该怎么称呼我？', user='当前用户', ts=2)
        state.memory_updates = {'fixture': (viewer_identity(current), '用户已更正称呼，不要再用旧名字。')}
        correction = state.correction_context_for(current)
        history = state.context_for(current)
        self.assertIn(correction, history)
        user, report = InputBudget(3000, 1200, 100).compose('保持人格', current.text, [
            ContextSection('memory_correction', correction, 110, required=True),
            ContextSection('recent_conversation', history, 90),
            ContextSection('memory_recall', '其他较短的召回资料', 70),
        ], 100)
        self.assertIn('recent_conversation', report['omitted_sources'])
        self.assertIn(correction, user)
        self.assertIn('memory_recall', report['included_sources'])

    def test_correction_reader_is_owner_scoped_and_does_not_advance_topic_state(self):
        state = ConversationState()
        owner = Event('companion', 'chat', '继续', user='本人')
        other = Event('companion', 'chat', '继续', user='另一个人')
        qq_named_peer = Event('qq_group', 'chat', '继续', user='本人', viewer_key='fixture')
        state.memory_updates = {
            'shared': (None, '全局删除约束'),
            'owner': (viewer_identity(owner), '当前本人纠错'),
            'other': (viewer_identity(other), 'OTHER_PRIVATE_CORRECTION'),
        }
        state.last_interruption = '尚未消费的打断标记'
        state.interrupted_topic = (5, '待续话题', viewer_identity(owner))
        self.assertEqual(state.correction_context_for(owner), '全局删除约束\n当前本人纠错')
        self.assertEqual(state.correction_context_for(qq_named_peer), '全局删除约束')
        self.assertEqual(state.last_interruption, '尚未消费的打断标记')
        self.assertEqual(state.interrupted_topic[0], 5)


class PersonaContextBudgetTests(unittest.IsolatedAsyncioTestCase):
    def persona(self, **budget):
        memory = Mock(viewer_note=Mock(return_value=''))
        persona = Persona(PersonaConfig(name='预算测试角色', rules=['保持角色规则。']),
                          LLMConfig(max_tokens=320), memory,
                          persona_llm=PersonaLLMConfig(**budget))
        self.addAsyncCleanup(persona.close)
        return persona

    async def test_short_request_preserves_system_question_and_all_sources(self):
        persona = self.persona()
        event = Event('companion', 'chat', '这朵花需要多少水？', user='测试者')
        context = assemble_context(ContextSection('short_history', '上次提到玫瑰', 100),
                                   ContextSection('public_reading', '{"summary":"读到浇水方法"}', 20))
        with patch.object(persona, 'system_prompt', wraps=persona.system_prompt) as system:
            with patch.object(persona, 'user_prompt', wraps=persona.user_prompt) as user:
                payload = persona._request_payload(event, extra_context=context)
        system.assert_called_once_with(event)
        user.assert_called_once_with(event, '')
        self.assertIn('保持角色规则。', payload['messages'][0]['content'])
        self.assertIn(event.text, payload['messages'][1]['content'])
        self.assertIn('上次提到玫瑰', payload['messages'][1]['content'])
        self.assertIn('读到浇水方法', payload['messages'][1]['content'])
        self.assertEqual(payload['max_tokens'], 160)
        self.assertEqual(persona.last_context_budget['included_sources'], ['short_history', 'public_reading'])

    async def test_large_json_and_search_result_are_dropped_whole_current_question_survives(self):
        persona = self.persona(max_input_tokens=32000, context_window_tokens=40000)
        question = '现在请详细回答我的问题，尤其是最后一句。😀'
        large = json.dumps({'summary': '中文资料😀' * 6000}, ensure_ascii=False)
        context = assemble_context(ContextSection('oversized_memory', large, 70),
                                   ContextSection('recent_dialogue', '短而相关的会话', 100))
        payload = persona._request_payload(Event('companion', 'chat', question, user='测试者'),
            research=[SearchResult('遥远搜索结果', 'https://example.com', '网页内容' * 20000)],
            extra_context=context)
        user = payload['messages'][1]['content']
        self.assertIn(question, user)
        self.assertIn('短而相关的会话', user)
        self.assertNotIn('中文资料', user)
        self.assertNotIn('遥远搜索结果', user)
        self.assertCountEqual(persona.last_context_budget['omitted_sources'], ['oversized_memory', 'web_search_1'])

    async def test_huge_question_and_persona_fail_before_model_call(self):
        persona = self.persona(max_input_tokens=15000, context_window_tokens=20000)
        persona._client = Mock(aclose=AsyncMock(), post=AsyncMock())
        with self.assertRaises(ContextBudgetError):
            await persona._call_llm(Event('companion', 'chat', '我的问题😀' * 10000), 'fixture')
        persona._client.post.assert_not_awaited()
        persona.cfg.background = '完整角色设定不能被切掉' * 10000
        with self.assertRaises(ContextBudgetError):
            persona._request_payload(Event('companion', 'chat', '短问题'))

    async def test_public_chat_entrypoints_report_budget_failure_without_placeholder_reply(self):
        persona = self.persona(max_input_tokens=15000, context_window_tokens=20000)
        backend = Mock(available=True, stream=Mock(), close=AsyncMock())
        persona._chat_backend = backend
        event = Event('companion', 'chat', '当前问题😀' * 5000)
        with self.assertRaises(ContextBudgetError):
            await persona.reply(event)
        self.assertEqual(persona.last_mode, 'context_budget')
        self.assertEqual(persona.last_text, '')
        parts = []
        with self.assertRaises(ContextBudgetError):
            async for part in persona.stream_reply(event):
                parts.append(part)
        self.assertEqual(parts, [])
        self.assertEqual(persona.last_mode, 'context_budget')
        self.assertNotIn(event.text, persona.last_error)
        backend.stream.assert_not_called()

    async def test_backend_output_cap_and_qq_suffix_final_guard_share_budget(self):
        persona = self.persona(context_window_tokens=32000, max_input_tokens=30000)
        persona._chat_backend = Mock(close=AsyncMock())
        persona.persona_llm.max_tokens = 6000
        payload = persona._request_payload(Event('companion', 'chat', '你好'))
        report = persona.last_context_budget
        self.assertEqual(report['max_input_tokens'], 32000 - 6000 - 1024)
        system, user = (message['content'] for message in payload['messages'])
        final = persona.validate_context_messages(system + '\n只输出JSON最终正文。', user,
                                                   output_tokens=2048, reserve_tokens=0)
        self.assertEqual(final['reserved_output_tokens'], 6000)

    async def test_activity_history_is_rejected_as_a_whole_without_transport_or_chat_state(self):
        persona = self.persona(context_window_tokens=12000, max_input_tokens=5000)
        transport = Mock(available=True, stream=Mock(), close=AsyncMock())
        persona._chat_backend = transport
        original = dict(persona.last_context_budget)
        with self.assertRaises(PersonaActivityError) as raised:
            await persona.activity_completion('公开活动规则', json.dumps({'tool': '字幕😀' * 3000}, ensure_ascii=False))
        self.assertIn('上下文', str(raised.exception))
        transport.stream.assert_not_called()
        self.assertEqual(persona.last_context_budget, original)

    async def test_qq_rechecks_after_json_instructions_before_transport(self):
        parent = self.persona(context_window_tokens=10000, max_input_tokens=350,
                              context_reserve_tokens=0)
        backend = Mock(spec=GeminiChat, available=True, close=AsyncMock(), complete=AsyncMock())
        parent._chat_backend = backend
        private = _PrivatePersona(parent, ConversationState())
        private.system_prompt = Mock(return_value='s' * 200)
        private.user_prompt = Mock(return_value='当前完整问题')
        with self.assertRaises(PrivateChatError) as raised:
            await private.complete_text(Event('qq_private', 'chat', '你好'), '')
        self.assertEqual(raised.exception.code, 'context_budget')
        backend.complete.assert_not_awaited()

    async def test_qq_rechecks_retry_suffix_and_never_sends_oversized_second_request(self):
        parent = self.persona(context_window_tokens=10000, max_input_tokens=600,
                              context_reserve_tokens=0)
        backend = Mock(spec=GeminiChat, available=True, close=AsyncMock(),
                       complete=AsyncMock(return_value='{"reply":"Thinking Process: rejected draft"}'))
        parent._chat_backend = backend
        private = _PrivatePersona(parent, ConversationState())
        private.system_prompt = Mock(return_value='s' * 200)
        private.user_prompt = Mock(return_value='当前完整问题')
        with self.assertRaises(PrivateChatError) as raised:
            await private.complete_text(Event('qq_private', 'chat', '你好'), '')
        self.assertEqual(raised.exception.code, 'context_budget')
        backend.complete.assert_awaited_once()
        self.assertIn('JSON', backend.complete.await_args.args[0])
        self.assertNotIn('rejected draft', backend.complete.await_args.args[0])


if __name__ == '__main__':
    unittest.main()
