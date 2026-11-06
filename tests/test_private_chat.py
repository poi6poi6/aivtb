"""QQ persona isolation, complete generation, durable delivery and erasure."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx

from brain.config import LLMConfig, PersonaConfig, PersonaLLMConfig
from brain.events import Event
from brain.gemini_chat import GeminiChat, GeminiChatError, MODEL_ERROR_CODES
from brain.persona import Persona
from brain.private_chat import MAX_REPLY, PrivateChatError, PrivateChatSession, _has_reply_scaffolding
from brain.viewer_identity import viewer_identity


SCREENSHOT_DRAFT = (
    '생각이\nThinking Process:\n'
    '- User requests a short greeting. PRIVATE_DRAFT\n'
    '- Keep the response concise.\n'
    '- Draft: 愿你今天顺顺利利。另一个候选版本。'
)


class ReplyScaffoldingTests(unittest.TestCase):
    def test_process_headings_markdown_wrappers_and_control_fragments(self):
        for text in (
            SCREENSHOT_DRAFT, 'Thinking Process: PRIVATE_DRAFT', '- Draft: PRIVATE_DRAFT',
            '> **Thinking Process:**\nPRIVATE_DRAFT', '> - **Draft**: PRIVATE_DRAFT',
            '1. _Reply draft_: PRIVATE_DRAFT', '### **思考过程**：PRIVATE_DRAFT',
            '- **回答草稿：**PRIVATE_DRAFT', '> 2. `Internal analysis`: PRIVATE_DRAFT',
            '```text\nThinking Process:\nPRIVATE_DRAFT\n```',
            '\ufeffThinking\u200b Process: PRIVATE_DRAFT', '[analysis] PRIVATE_DRAFT',
            '<|im_start|>assistant<|analysis|>PRIVATE_DRAFT', '<draft>PRIVATE_DRAFT</draft>',
            '最终正文。［：］', '最终正文。\x00',
        ):
            with self.subTest(text=text):
                self.assertTrue(_has_reply_scaffolding(text))

    def test_natural_thinking_and_quoted_words_are_not_scaffolding(self):
        for text in (
            '我在思考该怎么回答你。', '思考了一下，我觉得可以。',
            '这份草稿写得不错。', 'I was thinking about your question.',
            'Thinking about it, we could go tomorrow.', 'Draft a short note for tomorrow.',
            'The phrase "Thinking Process" is an English heading.',
            '- 思考之后再行动。\n- 愿你一切顺利。', '> 今天过得怎么样？',
            '参考[来源]，也可以看看【附录】。',
        ):
            with self.subTest(text=text):
                self.assertFalse(_has_reply_scaffolding(text))


class Stream:
    def __init__(self, parts=("[开心]这段话只在我们的 QQ 私聊里。",), error=None, block=False):
        self.parts, self.error, self.block = iter(parts), error, block
        self.waiting = asyncio.Event()
        self.aclose = AsyncMock()

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self.parts)
        except StopIteration:
            if self.block:
                self.waiting.set()
                await asyncio.Event().wait()
            if self.error:
                raise self.error
            raise StopAsyncIteration


class PrivateChatTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "qq-private.json"
        self.memory = Mock(viewer_note=Mock(return_value="本地身份已有记录"))
        self.backend = Mock(available=True, complete=None, stream=Mock(side_effect=lambda *_: Stream()), close=AsyncMock())
        with patch("brain.gemini_chat.GeminiChat", return_value=self.backend):
            self.parent = Persona(PersonaConfig(name="助手"), LLMConfig(), self.memory,
                                  learning=Mock(), persona_llm=PersonaLLMConfig(backend="gemini_api"))
        self.parent._recent_replies.append("公开直播聊过的话")
        self.parent.activity_completion = AsyncMock(side_effect=AssertionError("wrong decision API"))
        self.parent.topics.observe(Event("companion", "chat", "公开聊旅行", user="小明"))

    def session(self, **kwargs):
        session = PrivateChatSession(self.parent, self.path, "小明", **kwargs)
        self.addAsyncCleanup(session.close)
        return session

    def read(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    async def test_same_persona_and_transport_but_fully_private_state(self):
        autobiography = Mock(context_for=AsyncMock(return_value="本人旧记忆：喜欢蓝色"))
        session = self.session(autobiography=autobiography)
        reply = await session.prepare("m1", "私聊计划明天散步")
        system, user = self.backend.stream.call_args.args
        self.assertIn("助手", system)
        self.assertIn("QQ 一对一文字私聊", system)
        self.assertIn("本轮是文字聊天", system)
        for marker in ("[动作:", "[情绪]", "本轮最多输出", "口播句数限制", "本轮口语节奏"):
            self.assertNotIn(marker, system + user)
        self.assertIn("本人旧记忆：喜欢蓝色", user)
        self.assertNotIn("公开直播聊过的话", system + user)
        self.assertNotIn("公开聊旅行", system + user)
        event = autobiography.context_for.call_args.args[0]
        self.assertEqual(viewer_identity(event), viewer_identity("小明"))
        self.assertIs(session.persona._chat_backend, self.parent._chat_backend)
        self.assertIs(session.persona.cfg, self.parent.cfg)
        self.assertIsNot(session.persona.behavior, self.parent.behavior)
        self.assertIsNot(session.persona.dialogue, self.parent.dialogue)
        self.assertIsNot(session.persona.topics, self.parent.topics)
        self.assertIs(session.persona.topics, session.conversation.topics)
        self.assertEqual(reply.text, "这段话只在我们的 QQ 私聊里。")
        self.assertNotIn("reply", self.read()["history"][0])
        self.assertFalse(any(turn.role == "assistant" for turn in session.conversation.turns))
        self.assertTrue(await session.commit(reply.receipt_id, True))
        self.assertEqual(self.read()["history"][0]["reply"], reply.text)
        self.assertEqual(list(self.parent._recent_replies), ["公开直播聊过的话"])
        self.parent.activity_completion.assert_not_called()
        self.memory.record.assert_not_called()
        self.memory.touch_viewer.assert_not_called()
        self.assertFalse(self.parent.learning.mock_calls)

    async def test_unknown_or_failed_receipt_does_not_become_assistant_history(self):
        session = self.session()
        first = await session.prepare("m1", "私密的第一个问题")
        self.assertFalse(await session.commit("invented", True))
        with self.assertRaises(PrivateChatError):
            await session.prepare("m2", "第二个问题")
        self.assertFalse(await session.commit(first.receipt_id, False))
        await session.prepare("m2", "第二个问题")
        system, user = self.backend.stream.call_args.args
        self.assertNotIn(first.text, system + user)
        self.assertTrue(all(not item.get("reply") for item in self.read()["history"]))

    async def test_voice_changes_delivery_guidance_without_reintroducing_performance_tags(self):
        session = self.session()
        reply = await session.prepare('voice', '你好', reply_mode='voice')
        system, user = self.backend.stream.call_args.args
        self.assertIn('作为 QQ 语音读给对方听', system)
        self.assertIn('不输出思维过程', system)
        self.assertNotIn('[动作:', system + user)
        self.assertNotIn('口播句数限制', system + user)
        await session.commit(reply.receipt_id, True)
        await session.prepare('text', '继续聊', reply_mode='text')
        system, _ = self.backend.stream.call_args.args
        self.assertNotIn('作为 QQ 语音读给对方听', system)

    async def test_restart_restores_delivered_context_and_topic_closure(self):
        session = self.session()
        first = await session.prepare("m1", "聊聊明天出门穿什么")
        await session.commit(first.receipt_id, True)
        second = await session.prepare("m2", "这个话题先到这里")
        await session.commit(second.receipt_id, True)
        await session.close()
        restored = self.session()
        self.assertEqual(len(restored.conversation.turns), 4)
        await restored.prepare("m3", "我想聊一下今天的午饭")
        system, user = self.backend.stream.call_args.args
        self.assertIn(first.text, user)
        self.assertIn("这个话题先到这里", user)
        self.assertIn("话题", system)
        self.assertNotIn("公开历史", user)

    async def test_duplicate_commit_and_duplicate_id_are_not_replayed(self):
        session = self.session()
        reply = await session.prepare("m1", "你好")
        self.assertTrue(await session.commit(reply.receipt_id, True))
        self.assertFalse(await session.commit(reply.receipt_id, True))
        with self.assertRaises(PrivateChatError):
            await session.prepare("m1", "你好")
        with self.assertRaises(PrivateChatError):
            await session.prepare("m1", "伪造不同内容")
        self.assertEqual(len(self.read()["history"]), 1)

    async def test_incomplete_stream_is_never_a_ready_reply(self):
        for error in (RuntimeError("PRIVATE upstream credential"), asyncio.TimeoutError()):
            stream = Stream(error=error)
            self.backend.stream.return_value = stream
            self.backend.stream.side_effect = None
            session = self.session()
            with self.assertRaises(PrivateChatError) as caught:
                await session.prepare("m1", "帮我解释一下")
            self.assertNotIn("PRIVATE", str(caught.exception))
            self.assertIsNone(session._pending)
            self.assertNotIn("reply", self.read()["history"][0])
            stream.aclose.assert_awaited_once()
            self.backend.close.assert_not_awaited()

    async def test_cancel_and_close_release_only_this_stream(self):
        stream = Stream(block=True)
        self.backend.stream.side_effect = None
        self.backend.stream.return_value = stream
        session = self.session()
        task = asyncio.create_task(session.prepare("m1", "帮我解释一下"))
        await stream.waiting.wait()
        await session.close()
        with self.assertRaises(asyncio.CancelledError):
            await task
        stream.aclose.assert_awaited_once()
        self.backend.close.assert_not_awaited()
        self.assertIsNone(session._pending)
        self.assertNotIn("reply", self.read()["history"][0])

    async def test_timeout_and_oversized_reply_are_not_truncated_to_success(self):
        session = self.session()
        self.parent.persona_llm.qq_timeout_sec = .02
        self.backend.stream.side_effect = lambda *_: Stream(block=True)
        with self.assertRaises(PrivateChatError) as caught:
            await session.prepare("timeout", "帮我解释一下")
        self.assertEqual(caught.exception.code, 'model_timeout')
        self.backend.stream.side_effect = lambda *_: Stream(["字" * (MAX_REPLY + 1)])
        with self.assertRaises(PrivateChatError):
            await session.prepare("long", "帮我解释一下")
        self.assertIsNone(session._pending)

    async def test_text_is_complete_even_when_speech_would_limit_to_one_sentence(self):
        text = "第一句内容完整。第二句给出了理由。第三句补充例子。第四句说明条件。第五句到这里。"
        self.backend.stream.side_effect = lambda *_: Stream([text])
        session = self.session()
        reply = await session.prepare("m1", "你好")
        self.assertEqual(reply.text, text)

    async def test_final_completion_is_used_without_opening_intermediate_stream(self):
        self.backend.complete = AsyncMock(return_value='[平静]你好。今天过得怎么样？')
        session = self.session()
        reply = await session.prepare('final', '你好')
        self.assertEqual(reply.text, '你好。今天过得怎么样？')
        self.backend.stream.assert_not_called()
        self.backend.complete.assert_awaited_once()
        self.backend.close.assert_not_awaited()
        self.assertNotIn('reply', self.read()['history'][0])

    async def test_rejects_screenshot_style_draft_and_retries_once_before_sending(self):
        draft = SCREENSHOT_DRAFT
        self.backend.complete = AsyncMock(side_effect=[draft, '你好，今天过得怎么样？'])
        session = self.session()
        reply = await session.prepare('retry', '你好')
        self.assertEqual(reply.text, '你好，今天过得怎么样？')
        self.assertEqual(self.backend.complete.await_count, 2)
        self.assertNotIn('PRIVATE_DRAFT', str(self.backend.complete.call_args_list))
        self.assertNotIn('PRIVATE_DRAFT', self.path.read_text('utf-8'))
        self.assertFalse(any(turn.role == 'assistant' for turn in session.conversation.turns))
        await session.commit(reply.receipt_id, True)
        self.assertEqual(self.read()['history'][0]['reply'], reply.text)

    async def test_unsafe_final_output_never_becomes_pending_or_history(self):
        for index, draft in enumerate((
            '思维过程：PRIVATE_DRAFT', '[平静]思考过程：PRIVATE_DRAFT',
            '<think>PRIVATE_DRAFT</think>你好', '你好。[:]', '**Analysis**\nPRIVATE_DRAFT',
            'Thought process: PRIVATE_DRAFT', SCREENSHOT_DRAFT, '> - **Draft:** PRIVATE_DRAFT',
        )):
            with self.subTest(index=index):
                self.backend.complete = AsyncMock(return_value=draft)
                session = self.session()
                with self.assertRaises(PrivateChatError):
                    await session.prepare('invalid-' + str(index), '你好')
                self.assertEqual(self.backend.complete.await_count, 2)
                self.assertIsNone(session._pending)
                self.assertNotIn('PRIVATE_DRAFT', self.path.read_text('utf-8'))

    async def test_final_transport_failure_is_not_retried_as_format_repair(self):
        self.backend.complete = AsyncMock(side_effect=RuntimeError('PRIVATE error'))
        session = self.session()
        with self.assertRaises(PrivateChatError) as caught:
            await session.prepare('failure', '你好')
        self.assertNotIn('PRIVATE', str(caught.exception))
        self.backend.complete.assert_awaited_once()
        self.backend.stream.assert_not_called()

    async def test_known_provider_error_codes_cross_without_exception_details(self):
        session = self.session()
        for code in MODEL_ERROR_CODES:
            with self.subTest(code=code):
                self.backend.complete = AsyncMock(side_effect=GeminiChatError(
                    'PRIVATE credentials and upstream input', code=code))
                with self.assertRaises(PrivateChatError) as caught:
                    await session.prepare(code, '你好')
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn('PRIVATE', str(caught.exception))
                self.assertIsNone(session._pending)
                self.backend.complete.assert_awaited_once()
        self.assertNotIn('PRIVATE', self.path.read_text('utf-8'))

    async def test_unknown_exception_cannot_spoof_a_safe_provider_code(self):
        error = RuntimeError('PRIVATE upstream credentials')
        error.code = 'model_access'
        self.backend.complete = AsyncMock(side_effect=error)
        with self.assertRaises(PrivateChatError) as caught:
            await self.session().prepare('unknown', '你好')
        self.assertEqual(caught.exception.code, 'reply_failed')
        self.assertNotIn('PRIVATE', str(caught.exception))
        self.assertEqual(PrivateChatError('fixed', code='PRIVATE').code, 'reply_failed')
        self.assertEqual(PrivateChatError('fixed', code=[]).code, 'reply_failed')

    async def test_qq_gemini_timeout_override_preserves_shared_stream_budget(self):
        requests = []
        async def response(request):
            requests.append(request)
            await asyncio.sleep(.04)
            return httpx.Response(200, json={'choices': [{
                'index': 0, 'finish_reason': 'stop',
                'message': {'role': 'assistant', 'content': json.dumps({'reply': '你好，我在。'})}}]})
        adapter = GeminiChat(api_key_env='PRIVATE_CHAT_TEST_KEY', model='gemini-4-argon', timeout_sec=.02)
        adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(response), trust_env=False)
        self.addAsyncCleanup(adapter.close)
        self.parent._chat_backend = adapter
        self.parent.persona_llm.timeout_sec = .02
        self.parent.persona_llm.qq_timeout_sec = .3
        with patch.dict(os.environ, {'PRIVATE_CHAT_TEST_KEY': 'offline-fixture'}):
            reply = await self.session().prepare('slow', '你好')
        self.assertEqual(reply.text, '你好，我在。')
        self.assertGreater(requests[0].extensions['timeout']['read'], .2)
        self.assertEqual(json.loads(requests[0].content)['response_format']['type'], 'json_schema')
        self.assertEqual(adapter.timeout_sec, .02)
        self.assertEqual(self.parent.persona_llm.timeout_sec, .02)

    def json_backend(self, outputs):
        requests = []
        outputs = iter(outputs)

        async def response(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={'choices': [{
                'index': 0, 'finish_reason': 'stop',
                'message': {'role': 'assistant', 'content': next(outputs)}}]})

        adapter = GeminiChat(api_key_env='PRIVATE_CHAT_TEST_KEY', model='gemini-4-argon')
        adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(response), trust_env=False)
        self.addAsyncCleanup(adapter.close)
        self.parent._chat_backend = adapter
        return requests

    async def test_json_reply_extracts_only_final_text_and_keeps_escaped_chinese(self):
        # JSON escaping must not reduce the allowed final body length.
        body = '愿你今天顺利。' * 90
        requests = self.json_backend([json.dumps({'reply': body})])
        with patch.dict(os.environ, {'PRIVATE_CHAT_TEST_KEY': 'offline-fixture'}):
            session = self.session()
            reply = await session.prepare('json-final', '说句吉祥话')
        self.assertEqual(reply.text, body)
        self.assertEqual(len(requests), 1)
        self.assertIn('唯一字段为 reply', requests[0]['messages'][0]['content'])
        self.assertNotIn('reply', self.read()['history'][0])
        await session.commit(reply.receipt_id, True)
        self.assertEqual(self.read()['history'][0]['reply'], body)

    async def test_bad_json_or_draft_is_regenerated_once_without_echoing_the_bad_content(self):
        malformed_outputs = (
            'PRIVATE_DRAFT plain text', '```json\n{"reply":"PRIVATE_DRAFT"}\n```',
            '{"reply":"PRIVATE_DRAFT"} trailing', '{"reply":',
            '{"reply":"PRIVATE_DRAFT","analysis":"PRIVATE_DRAFT"}',
            '{"reply":"PRIVATE_DRAFT","reply":"PRIVATE_DRAFT"}',
            '[{"reply":"PRIVATE_DRAFT"}]', '{"reply":null}', '{"reply":false}',
            '{"reply":[]}', '{"reply":{}}', '{}', '{"reply":"  "}',
            json.dumps({'reply': SCREENSHOT_DRAFT}),
        )
        for index, malformed in enumerate(malformed_outputs):
            with self.subTest(index=index):
                requests = self.json_backend([malformed, json.dumps({'reply': '愿你今天顺利。'})])
                with patch.dict(os.environ, {'PRIVATE_CHAT_TEST_KEY': 'offline-fixture'}):
                    session = self.session()
                    reply = await session.prepare('json-retry-' + str(index), '说句吉祥话')
                self.assertEqual(reply.text, '愿你今天顺利。')
                self.assertEqual(len(requests), 2)
                self.assertNotIn('PRIVATE_DRAFT', str(requests))
                self.assertNotIn('PRIVATE_DRAFT', self.path.read_text('utf-8'))
                self.assertFalse(any(turn.role == 'assistant' for turn in session.conversation.turns))

    async def test_second_bad_json_never_becomes_a_pending_reply(self):
        requests = self.json_backend([json.dumps({'reply': SCREENSHOT_DRAFT}), '{"reply":true}'])
        with patch.dict(os.environ, {'PRIVATE_CHAT_TEST_KEY': 'offline-fixture'}):
            session = self.session()
            with self.assertRaises(PrivateChatError) as caught:
                await session.prepare('json-failed', '说句吉祥话')
        self.assertEqual(caught.exception.code, 'model_response')
        self.assertEqual(len(requests), 2)
        self.assertIsNone(session._pending)
        self.assertNotIn('PRIVATE_DRAFT', str(requests))
        self.assertNotIn('PRIVATE_DRAFT', self.path.read_text('utf-8'))

    async def test_json_format_retry_shares_one_qq_deadline(self):
        self.parent.persona_llm.qq_timeout_sec = .15
        requests = []
        completed = []

        async def response(request):
            requests.append(request)
            await asyncio.sleep(.1)
            completed.append(True)
            return httpx.Response(200, json={'choices': [{
                'index': 0, 'finish_reason': 'stop',
                'message': {'role': 'assistant', 'content': 'PRIVATE_DRAFT'}}]})

        adapter = GeminiChat(api_key_env='PRIVATE_CHAT_TEST_KEY', model='gemini-4-argon')
        adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(response), trust_env=False)
        self.addAsyncCleanup(adapter.close)
        self.parent._chat_backend = adapter
        with patch.dict(os.environ, {'PRIVATE_CHAT_TEST_KEY': 'offline-fixture'}):
            session = self.session()
            with self.assertRaises(PrivateChatError) as caught:
                await session.prepare('json-budget', '你好')
        self.assertEqual(caught.exception.code, 'model_timeout')
        self.assertEqual(len(requests), 2)
        self.assertEqual(len(completed), 1)
        self.assertLess(requests[1].extensions['timeout']['read'], .1)
        self.assertIsNone(session._pending)
        self.assertNotIn('PRIVATE_DRAFT', self.path.read_text('utf-8'))

    async def test_format_retry_shares_one_qq_deadline(self):
        self.parent.persona_llm.qq_timeout_sec = .15
        completed = []
        async def complete(system, user):
            await asyncio.sleep(.1)
            completed.append(True)
            return '思维过程：PRIVATE DRAFT' if len(completed) == 1 else '最终正文。'
        self.backend.complete = AsyncMock(side_effect=complete)
        session = self.session()
        with self.assertRaises(PrivateChatError) as caught:
            await session.prepare('budget', '你好')
        self.assertEqual(caught.exception.code, 'model_timeout')
        self.assertEqual(self.backend.complete.await_count, 2)
        self.assertEqual(len(completed), 1)
        self.assertIsNone(session._pending)
        self.assertNotIn('PRIVATE', self.path.read_text('utf-8'))

    async def test_invalid_qq_timeout_never_calls_model(self):
        session = self.session()
        for index, budget in enumerate((True, False, 0, -1, '90', None, float('inf'), float('nan'), 90.01)):
            with self.subTest(budget=budget):
                self.parent.persona_llm.qq_timeout_sec = budget
                with self.assertRaises(PrivateChatError) as caught:
                    await session.prepare('invalid-budget-' + str(index), '你好')
                self.assertEqual(caught.exception.code, 'model_response')
        self.backend.stream.assert_not_called()

    async def test_natural_mentions_of_thinking_and_unknown_brackets_are_preserved(self):
        text = '我思考了一下，你说得对。可以参考[来源]再做决定。'
        self.backend.complete = AsyncMock(return_value=text)
        self.assertEqual((await self.session().prepare('natural', '你觉得呢')).text, text)
        self.backend.complete.assert_awaited_once()

    async def test_old_bad_reply_remains_archived_but_is_not_replayed_to_persona(self):
        session = self.session()
        first = await session.prepare('old', '你好')
        await session.commit(first.receipt_id, True)
        stored = self.read()
        stored['history'][0]['reply'] = SCREENSHOT_DRAFT.replace('PRIVATE_DRAFT', 'PRIVATE_OLD_DRAFT')
        self.path.write_text(json.dumps(stored), encoding='utf-8')
        restored = self.session()
        await restored.prepare('new', '刚才那句不太对')
        system, user = self.backend.stream.call_args.args
        self.assertNotIn('PRIVATE_OLD_DRAFT', system + user)
        self.assertNotIn('PRIVATE_OLD_DRAFT', str(restored.conversation.turns))
        self.assertIn('PRIVATE_OLD_DRAFT', self.path.read_text('utf-8'))

    async def test_owner_memory_delete_erases_pending_history_and_old_corrections(self):
        session = self.session()
        memory = {"slot": "preference", "owner_key": viewer_identity("小明")}
        await session.apply_memory_change(memory, "私密旧更正")
        prepared = await session.prepare("m1", "私密旧用户消息")
        await session.apply_memory_change(memory)
        self.assertFalse(await session.commit(prepared.receipt_id, True))
        disk = self.path.read_text(encoding="utf-8")
        self.assertNotIn("私密旧", disk)
        restored = self.session()
        await restored.prepare("m2", "现在来聊天")
        system, user = self.backend.stream.call_args.args
        self.assertNotIn("私密旧", system + user)
        self.assertIn("不要猜测", user)

    async def test_other_owner_delete_does_not_clear_this_private_context(self):
        session = self.session()
        prepared = await session.prepare("m1", "我自己的信息")
        await session.apply_memory_change({"slot": "other", "owner_key": viewer_identity("其他人")})
        self.assertTrue(await session.commit(prepared.receipt_id, True))

    async def test_forget_write_failure_removes_stale_archive_and_closes_session(self):
        session = self.session()
        prepared = await session.prepare("m1", "私密旧信息")
        await session.commit(prepared.receipt_id, True)
        with patch("brain.private_chat.atomic_json_write", side_effect=OSError("disk full")):
            with self.assertRaises(PrivateChatError):
                await session.apply_memory_change({"slot": "global"})
        self.assertFalse(self.path.exists())
        with self.assertRaises(PrivateChatError):
            await session.prepare("m2", "继续")
        restored = self.session()
        await restored.prepare("m3", "现在聊天")
        self.assertNotIn("私密旧信息", "".join(self.backend.stream.call_args.args))

    async def test_restart_does_not_treat_yesterdays_topic_as_current(self):
        session = self.session()
        prepared = await session.prepare("m1", "聊一下明天怎么安排")
        await session.commit(prepared.receipt_id, True)
        stored = self.read()
        stored["history"][0]["ts"] -= 86400
        stored["history"][0]["delivered_at"] -= 86400
        self.path.write_text(json.dumps(stored), encoding="utf-8")
        restored = self.session()
        prepared = await restored.prepare("m2", "我想谈谈午饭")
        frame = restored.persona.topics.inspect(restored._pending[1])
        self.assertEqual(frame.action, "start")

    async def test_forget_during_generation_cancels_stale_reply(self):
        stream = Stream(block=True)
        self.backend.stream.side_effect = lambda *_: stream
        session = self.session()
        task = asyncio.create_task(session.prepare("m1", "私密旧用户消息"))
        await stream.waiting.wait()
        await session.apply_memory_change({"slot": "global"})
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNone(session._pending)
        self.assertEqual(self.read()["history"], [])

    async def test_write_failure_never_commits_or_sends_ready_reply(self):
        session = self.session()
        with patch("brain.private_chat.atomic_json_write", side_effect=OSError("disk full")):
            with self.assertRaises(PrivateChatError):
                await session.prepare("m1", "你好")
        self.backend.stream.assert_not_called()
        prepared = await session.prepare("m1", "你好")
        with patch("brain.private_chat.atomic_json_write", side_effect=OSError("disk full")):
            with self.assertRaises(PrivateChatError):
                await session.commit(prepared.receipt_id, True)
        self.assertNotIn("reply", self.read()["history"][0])
        self.assertFalse(any(turn.role == "assistant" for turn in session.conversation.turns))
        self.assertTrue(await session.commit(prepared.receipt_id, True))

    async def test_corrupt_or_other_owner_history_is_never_overwritten(self):
        session = self.session()
        await session.prepare("m1", "本人私密内容")
        original = self.path.read_bytes()
        with self.assertRaises(PrivateChatError):
            PrivateChatSession(self.parent, self.path, "其他人")
        self.assertEqual(self.path.read_bytes(), original)
        self.path.write_text("broken", encoding="utf-8")
        with self.assertRaises(PrivateChatError):
            self.session()
        self.assertEqual(self.path.read_text(encoding="utf-8"), "broken")

    async def test_invalid_input_or_ambiguous_receipt_rejected(self):
        session = self.session()
        for message_id, text in (("", "hello"), ("x", ""), ("x", "x" * 4001), (12, "hello")):
            with self.assertRaises(PrivateChatError):
                await session.prepare(message_id, text)
        prepared = await session.prepare("valid", "你好")
        with self.assertRaises(ValueError):
            await session.commit(prepared.receipt_id, "unknown")

    async def test_missing_model_does_not_fallback_to_auxiliary(self):
        self.backend.available = False
        session = self.session()
        with self.assertRaises(PrivateChatError):
            await session.prepare("m1", "你好")
        self.backend.stream.assert_not_called()

    async def test_safety_path_reuses_persona_rules_without_model(self):
        session = self.session()
        reply = await session.prepare("m1", "不想活了")
        self.assertIn("危险", reply.text)
        self.backend.stream.assert_not_called()

    async def test_inherited_backend_reuses_http_and_requires_finish_stop(self):
        responses = ["length", "stop"]
        requests = []
        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={"choices": [{"finish_reason": responses.pop(0),
                "message": {"role": "assistant", "content": "完整的文字回复。"}}]})
        with patch.dict(os.environ, {"PRIVATE_CHAT_TEST_KEY": "offline-fixture"}):
            parent = Persona(PersonaConfig(), LLMConfig(api_key_env="PRIVATE_CHAT_TEST_KEY"), self.memory)
            parent._client = httpx.AsyncClient(transport=httpx.MockTransport(respond), trust_env=False)
            self.addAsyncCleanup(parent.close)
            session = PrivateChatSession(parent, self.path, "小明")
            with self.assertRaises(PrivateChatError):
                await session.prepare("m1", "为什么这样")
            reply = await session.prepare("m1", "为什么这样")
            self.assertEqual(reply.text, "完整的文字回复。")
            await session.close()
            self.assertFalse(parent._client.is_closed)
            self.assertEqual(json.loads(requests[-1].content)["max_tokens"], 2048)


if __name__ == "__main__":
    unittest.main()
