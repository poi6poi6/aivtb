"""Group mentions retain persona behavior without sharing any member's history."""
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
from brain.persona import Persona
from brain.private_chat import PrivateChatError, PrivateChatSession
from brain.qq_group_chat import GroupChatSession
from brain.viewer_identity import viewer_identity


class ForbiddenStore:
    def __getattr__(self, name):
        raise AssertionError('group accessed shared memory: ' + name)

    def __bool__(self):
        raise AssertionError('group inspected shared memory')


class GroupChatTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.backend = Mock(available=True, complete=AsyncMock(return_value='[平静]欢迎一起聊。'),
                            close=AsyncMock())
        forbidden = ForbiddenStore()
        with patch('brain.gemini_chat.GeminiChat', return_value=self.backend):
            self.parent = Persona(PersonaConfig(name='助手'), LLMConfig(), forbidden,
                learning=forbidden, web_search=forbidden, personality=forbidden,
                persona_llm=PersonaLLMConfig(backend='gemini_api'))
        self.parent.autobiography = forbidden
        self.parent._recent_replies.append('PRIVATE_PARENT_REPLY_SENTINEL')
        self.parent.topics.observe(Event('companion', 'chat', 'PRIVATE_PARENT_TOPIC_SENTINEL', user='群友'))
        self.parent.activity_completion = AsyncMock(side_effect=AssertionError('not a chat backend'))

    def session(self, *, app='app-one', group='group-one', member='member-one', path=None):
        path = path or self.root / (app + '-' + group + '-' + member + '.json')
        session = GroupChatSession(self.parent, path, app, group, member)
        self.addAsyncCleanup(session.close)
        return session

    def prompts(self):
        return self.backend.complete.call_args.args

    async def test_borrows_only_role_and_model_without_reading_shared_stores(self):
        session = self.session()
        reply = await session.prepare('m1', '我今天看见一朵漂亮的花')
        system, user = self.prompts()
        self.assertIn('助手', system)
        self.assertIn('QQ 群聊', system)
        self.assertIn('回复会公开发到这个群里', system)
        for marker in ('QQ 一对一文字私聊', '已由宿主验证的本人', 'PRIVATE_PARENT_',
                       'app-one', 'group-one', 'member-one', session.scope_key,
                       '[情绪]', '[动作:', '口播句数限制'):
            self.assertNotIn(marker, system + user)
        event = session._events['m1']
        self.assertEqual((event.source, event.user), ('qq_group', '群友'))
        self.assertEqual(viewer_identity(event), session.owner_key)
        self.assertNotEqual(session.owner_key, viewer_identity('群友'))
        self.assertIs(session.persona.cfg, self.parent.cfg)
        self.assertIs(session.persona._chat_backend, self.parent._chat_backend)
        self.assertIsNone(session.autobiography)
        self.assertIsNone(session.persona.personality)
        self.assertIsNone(session.persona.learning)
        self.assertIsNone(session.persona.web_search)
        self.assertIsNot(session.persona.behavior, self.parent.behavior)
        self.assertTrue(await session.commit(reply.receipt_id, True))
        self.parent.activity_completion.assert_not_called()
        self.assertEqual(list(self.parent._recent_replies), ['PRIVATE_PARENT_REPLY_SENTINEL'])

    async def test_group_member_and_app_boundaries_are_separate_with_identical_display_names(self):
        original = self.session()
        reply = await original.prepare('shared-message-id', 'ONLY_ORIGINAL_GROUP_MEMBER')
        self.backend.complete.return_value = 'ORIGINAL_REPLY_SECRET'
        await original.commit(reply.receipt_id, True)
        scopes = [self.session(group='group-two'), self.session(member='member-two'),
                  self.session(app='app-two')]
        self.assertEqual(len({s.owner_key for s in [original, *scopes]}), 4)
        for other in scopes:
            other_reply = await other.prepare('shared-message-id', '我想聊聊另一件事')
            system, user = self.prompts()
            self.assertNotIn('ONLY_ORIGINAL_GROUP_MEMBER', system + user)
            self.assertNotIn(reply.text, system + user)
            self.assertEqual(other.owner_name, original.owner_name)
            self.assertIsNot(other.persona.dialogue, original.persona.dialogue)
            self.assertIsNot(other.conversation, original.conversation)
            await other.commit(other_reply.receipt_id, False)

    async def test_same_named_private_owner_history_never_enters_group(self):
        self.parent.memory = Mock(viewer_note=Mock(return_value='PRIVATE_VISIT_SENTINEL'))
        self.parent.personality = Mock(context_for=Mock(return_value='PRIVATE_RELATION_SENTINEL'))
        self.parent.learning = None
        private = PrivateChatSession(self.parent, self.root / 'private.json', '群友',
            autobiography=Mock(context_for=AsyncMock(return_value='PRIVATE_LONG_TERM_SENTINEL')))
        self.addAsyncCleanup(private.close)
        self.backend.complete.return_value = 'PRIVATE_DELIVERED_REPLY'
        private_reply = await private.prepare('same-id', 'PRIVATE_INPUT_SENTINEL')
        await private.commit(private_reply.receipt_id, True)
        self.parent.memory.viewer_note.reset_mock()
        self.parent.personality.context_for.reset_mock()
        group = self.session()
        await group.prepare('same-id', '群里聊点新的')
        system, user = self.prompts()
        self.assertNotIn('PRIVATE_', system + user)
        self.assertNotEqual(group.owner_key, private.owner_key)
        self.parent.memory.viewer_note.assert_not_called()
        self.parent.personality.context_for.assert_not_called()
        self.assertNotEqual(group.path, private.path)

    async def test_wrong_scope_archive_is_rejected_without_overwrite(self):
        first = self.session()
        reply = await first.prepare('m1', '本群本人的历史')
        await first.commit(reply.receipt_id, True)
        before = first.path.read_bytes()
        for kwargs in ({'group': 'another'}, {'member': 'another'}, {'app': 'another'}):
            with self.assertRaises(PrivateChatError):
                self.session(path=first.path, **kwargs)
            self.assertEqual(first.path.read_bytes(), before)
        with self.assertRaises(PrivateChatError):
            PrivateChatSession(self.parent, first.path, '群友')

    async def test_failed_or_unknown_receipts_do_not_restore_undelivered_reply(self):
        session = self.session()
        self.backend.complete.return_value = 'UNDELIVERED_GROUP_REPLY'
        reply = await session.prepare('m1', '一个待回复的问题')
        self.assertFalse(await session.commit('wrong-receipt', True))
        with self.assertRaises(PrivateChatError):
            await session.prepare('m2', '还在等待')
        self.assertFalse(await session.commit(reply.receipt_id, False))
        await session.close()
        restored = self.session()
        await restored.prepare('m2', '我继续说')
        self.assertNotIn('UNDELIVERED_GROUP_REPLY', ''.join(self.prompts()))
        self.assertFalse(any(turn.role == 'assistant' for turn in restored.conversation.turns))

    async def test_restart_restores_only_confirmed_turns_and_topic_closure(self):
        session = self.session()
        first = await session.prepare('m1', '我想聊聊玫瑰怎么浇水')
        self.assertEqual(session.persona.topics.inspect(session._events['m1']).action, 'start')
        await session.commit(first.receipt_id, True)
        closing = await session.prepare('m2', '这个话题到此为止')
        self.assertEqual(session.persona.topics.inspect(session._events['m2']).action, 'close')
        await session.commit(closing.receipt_id, True)
        await session.close()
        restored = self.session()
        self.assertEqual(len(restored.conversation.turns), 4)
        current = await restored.prepare('m3', '回到刚才的话题')
        self.assertEqual(restored.persona.topics.inspect(restored._events['m3']).action, 'uncertain')
        self.assertIn('仅当前群友在本群的对话历史', ''.join(self.prompts()))
        self.assertNotIn('QQ 私聊历史', ''.join(self.prompts()))
        await restored.commit(current.receipt_id, True)
        await restored.prepare('m4', '聊聊明天的午饭')
        self.assertEqual(restored.persona.topics.inspect(restored._events['m4']).action, 'start')

    async def test_dialogue_preference_and_closure_work_for_group_source(self):
        session = self.session()
        first = await session.prepare('m1', '我很难过，先听我说，别给建议')
        frame = session.persona.dialogue.inspect(session._events['m1'])
        self.assertEqual(frame.mode, 'listening')
        self.assertTrue(frame.care)
        await session.commit(first.receipt_id, True)
        second = await session.prepare('m2', '后来也没有等到答复')
        self.assertEqual(session.persona.dialogue.inspect(session._events['m2']).mode, 'listening')
        await session.commit(second.receipt_id, True)
        await session.prepare('m3', '不用再聊了')
        closed = session.persona.dialogue.inspect(session._events['m3'])
        self.assertTrue(closed.closed)
        self.assertFalse(closed.care)
        other = self.session(member='member-two')
        await other.prepare('m1', '今天的花真漂亮')
        self.assertEqual(other.persona.dialogue.inspect(other._events['m1']).mode, '')

    async def test_voice_guidance_and_draft_guard_are_reused(self):
        session = self.session()
        self.backend.complete.side_effect = ['思维过程：\nPRIVATE_DRAFT[:]', '你好，来聊聊吧。']
        reply = await session.prepare('m1', '你好', reply_mode='voice')
        self.assertEqual(reply.text, '你好，来聊聊吧。')
        system, user = self.prompts()
        self.assertIn('作为 QQ 语音读给对方听', system)
        self.assertNotIn('PRIVATE_DRAFT', system + user)
        self.assertNotIn('[动作:', system + user)
        self.assertNotIn('QQ 一对一', system + user)
        await session.commit(reply.receipt_id, True)
        self.assertNotIn('PRIVATE_DRAFT', session.path.read_text('utf-8'))

    async def test_archived_draft_is_not_restored_into_context(self):
        session = self.session()
        session._write([{'id': 'old', 'text': '旧问题', 'ts': 1.,
                         'reply': '思维过程：\nARCHIVED_DRAFT[:]'}], [])
        await session.close()
        restored = self.session()
        await restored.prepare('new', '新问题')
        self.assertNotIn('ARCHIVED_DRAFT', ''.join(self.prompts()))
        self.assertFalse(any(turn.role == 'assistant' for turn in restored.conversation.turns))

    async def test_shared_memory_correction_cannot_inject_private_fact(self):
        session = self.session()
        await session.apply_memory_change({'slot': 'global-looking'}, 'PRIVATE_CORRECTION_SENTINEL')
        await session.prepare('m1', '你好')
        self.assertNotIn('PRIVATE_CORRECTION_SENTINEL', ''.join(self.prompts()))
        self.assertEqual(session._corrections, [])

    async def test_close_cancels_generation_without_closing_shared_model(self):
        started = asyncio.Event()

        async def wait(*args):
            started.set()
            await asyncio.Event().wait()

        self.backend.complete.side_effect = wait
        session = self.session()
        task = asyncio.create_task(session.prepare('m1', '你好'))
        await asyncio.wait_for(started.wait(), 1)
        await session.close()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.backend.close.assert_not_called()

    async def test_invalid_scope_is_rejected_before_creating_history(self):
        for invalid in ('', None, 'untrusted\nname', 'a' * 257):
            with self.assertRaises(ValueError):
                GroupChatSession(self.parent, self.root / 'invalid.json', 'app', 'group', invalid)
        self.assertFalse((self.root / 'invalid.json').exists())


class GeminiGroupAdapterTests(unittest.IsolatedAsyncioTestCase):
    """Exercise the real HTTP adapter offline, with fictional configuration."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {'QQ_GROUP_OFFLINE_TEST_KEY': 'offline-only'})
        environment.start()
        self.addCleanup(environment.stop)
        self.parent = Persona(
            PersonaConfig(name='测试角色', tone='自然、简洁'), LLMConfig(), ForbiddenStore(),
            persona_llm=PersonaLLMConfig(
                backend='gemini_api', api_key_env='QQ_GROUP_OFFLINE_TEST_KEY',
                model='gemini-test', timeout_sec=.5, qq_timeout_sec=5.,
            ),
        )
        self.addAsyncCleanup(self.parent.close)
        self.requests = []

    def session(self, handler):
        def capture(request):
            self.requests.append(request)
            return handler(request)

        self.parent._chat_backend._client = httpx.AsyncClient(
            transport=httpx.MockTransport(capture), trust_env=False,
        )
        session = GroupChatSession(self.parent, self.root / 'group.json', 'app', 'group', 'member')
        self.addAsyncCleanup(session.close)
        return session

    @staticmethod
    def final_response(text='我在，想聊点什么？', *, structured=True):
        return httpx.Response(200, json={'choices': [{
            'index': 0, 'finish_reason': 'stop',
            'message': {'role': 'assistant',
                        'content': json.dumps({'reply': text}, ensure_ascii=False) if structured else text,
                        'reasoning_content': 'PROVIDER_REASONING_SENTINEL'},
        }]})

    @staticmethod
    def screenshot_draft():
        return ('생각이\nThinking Process:\n'
                '- User wants some auspicious words in a QQ group chat.\n'
                '- Keep it concise and suitable for a voice message.\n'
                '- Draft: UNDELIVERED_DRAFT_SENTINEL，愿你今晚睡得安稳。')

    def assert_final_reply_request(self, request):
        payload = json.loads(request.content)
        response_format = payload['response_format']
        self.assertEqual(response_format['type'], 'json_schema')
        self.assertIs(response_format['json_schema']['strict'], True)
        schema = response_format['json_schema']['schema']
        self.assertEqual(schema['type'], 'object')
        self.assertEqual(set(schema['properties']), {'reply'})
        self.assertEqual(schema['properties']['reply']['type'], 'string')
        self.assertEqual(schema['required'], ['reply'])
        self.assertIs(schema['additionalProperties'], False)
        self.assertIs(payload['stream'], False)
        return payload

    async def test_full_voice_prompt_uses_final_content_and_qq_request_budget(self):
        session = self.session(lambda request: self.final_response())
        reply = await session.prepare('test', '说话', reply_mode='voice')
        self.assertEqual(reply.text, '我在，想聊点什么？')
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        payload = self.assert_final_reply_request(request)
        self.assertEqual(payload['model'], 'gemini-test')
        self.assertEqual([item['role'] for item in payload['messages']], ['system', 'user'])
        self.assertIn('QQ 群聊', payload['messages'][0]['content'])
        self.assertIn('作为 QQ 语音读给对方听', payload['messages'][0]['content'])
        self.assertIn('说话', payload['messages'][1]['content'])
        self.assertIs(payload['extra_body']['google']['thinking_config']['include_thoughts'], False)
        self.assertGreater(request.extensions['timeout']['read'], .5)
        self.assertLessEqual(request.extensions['timeout']['read'], 5.)
        self.assertEqual(self.parent._chat_backend.timeout_sec, .5)
        self.assertTrue(await session.commit(reply.receipt_id, True))
        saved = session.path.read_text('utf-8')
        self.assertNotIn('PROVIDER_REASONING_SENTINEL', saved)
        self.assertNotIn('offline-only', saved)

    async def test_screenshot_plain_draft_is_regenerated_as_final_json(self):
        session = self.session(lambda request: self.final_response(
            self.screenshot_draft(), structured=False) if len(self.requests) == 1
            else self.final_response('愿你今晚睡得安稳，明天事事顺心。'))
        reply = await session.prepare('test', '说点吉祥话', reply_mode='voice')
        self.assertEqual(reply.text, '愿你今晚睡得安稳，明天事事顺心。')
        self.assertEqual(len(self.requests), 2)
        for request in self.requests:
            payload = self.assert_final_reply_request(request)
            self.assertNotIn('UNDELIVERED_DRAFT_SENTINEL', json.dumps(payload))
            self.assertNotIn('PROVIDER_REASONING_SENTINEL', json.dumps(payload))
        first_timeout, second_timeout = [request.extensions['timeout']['read']
                                         for request in self.requests]
        self.assertGreater(second_timeout, 0)
        self.assertLessEqual(second_timeout, first_timeout)
        self.assertLessEqual(first_timeout, 5.)
        self.assertEqual(self.parent._chat_backend.timeout_sec, .5)
        self.assertFalse(any(turn.role == 'assistant' for turn in session.conversation.turns))
        self.assertTrue(await session.commit(reply.receipt_id, True))
        history = json.loads(session.path.read_text('utf-8'))['history']
        self.assertEqual(history[-1]['reply'], reply.text)
        self.assertNotIn('UNDELIVERED_DRAFT_SENTINEL', session.path.read_text('utf-8'))

    async def test_structured_reply_with_markdown_draft_is_regenerated(self):
        draft = '### Thinking Process:\n- Draft: UNDELIVERED_DRAFT_SENTINEL，愿你一切顺利。'
        session = self.session(lambda request: self.final_response(
            draft if len(self.requests) == 1 else '愿你每天都遇到一点好事。'))
        reply = await session.prepare('test', '说点吉祥话', reply_mode='voice')
        self.assertEqual(reply.text, '愿你每天都遇到一点好事。')
        self.assertEqual(len(self.requests), 2)
        for request in self.requests:
            payload = self.assert_final_reply_request(request)
            self.assertNotIn('UNDELIVERED_DRAFT_SENTINEL', json.dumps(payload))
        self.assertTrue(await session.commit(reply.receipt_id, True))
        self.assertNotIn('UNDELIVERED_DRAFT_SENTINEL', session.path.read_text('utf-8'))

    async def test_repeated_structured_draft_fails_without_send_or_history_replay(self):
        session = self.session(lambda request: self.final_response(
            self.screenshot_draft() if len(self.requests) <= 2 else '明天也要照顾好自己。'))
        with self.assertRaises(PrivateChatError) as failure:
            await session.prepare('test', '说点吉祥话', reply_mode='voice')
        self.assertEqual(failure.exception.code, 'model_response')
        self.assertNotIn('UNDELIVERED_DRAFT_SENTINEL', str(failure.exception))
        self.assertEqual(len(self.requests), 2)
        self.assertIsNone(session._pending)
        self.assertFalse(any(turn.role == 'assistant' for turn in session.conversation.turns))
        self.assertNotIn('UNDELIVERED_DRAFT_SENTINEL', session.path.read_text('utf-8'))
        next_reply = await session.prepare('next', '再说一句', reply_mode='voice')
        self.assertEqual(next_reply.text, '明天也要照顾好自己。')
        self.assertEqual(len(self.requests), 3)
        next_payload = self.assert_final_reply_request(self.requests[-1])
        self.assertNotIn('UNDELIVERED_DRAFT_SENTINEL', json.dumps(next_payload))
        self.assertNotIn('PROVIDER_REASONING_SENTINEL', json.dumps(next_payload))

    async def test_malformed_json_gets_only_one_clean_regeneration(self):
        session = self.session(lambda request: self.final_response(
            '{"reply": "BROKEN_JSON_SENTINEL', structured=False)
            if len(self.requests) == 1 else self.final_response('祝你一切顺利。'))
        reply = await session.prepare('test', '说点吉祥话', reply_mode='voice')
        self.assertEqual(reply.text, '祝你一切顺利。')
        self.assertEqual(len(self.requests), 2)
        for request in self.requests:
            payload = self.assert_final_reply_request(request)
            self.assertNotIn('BROKEN_JSON_SENTINEL', json.dumps(payload))
        self.assertNotIn('BROKEN_JSON_SENTINEL', session.path.read_text('utf-8'))

    async def test_archived_screenshot_draft_is_not_replayed_to_real_adapter(self):
        session = self.session(lambda request: self.final_response('愿你今天心情轻松些。'))
        session._write([{'id': 'old', 'text': '过去的吉祥话请求', 'ts': 1.,
                         'reply': self.screenshot_draft()}], [])
        await session.close()
        restored = GroupChatSession(self.parent, session.path, 'app', 'group', 'member')
        self.addAsyncCleanup(restored.close)
        reply = await restored.prepare('new', '再说一句吉祥话', reply_mode='voice')
        self.assertEqual(reply.text, '愿你今天心情轻松些。')
        self.assertEqual(len(self.requests), 1)
        payload = self.assert_final_reply_request(self.requests[0])
        self.assertNotIn('UNDELIVERED_DRAFT_SENTINEL', json.dumps(payload))
        self.assertNotIn('Thinking Process:', json.dumps(payload))
        self.assertFalse(any(turn.role == 'assistant' for turn in restored.conversation.turns))

    async def test_provider_timeout_reaches_group_as_specific_safe_error(self):
        def timeout(request):
            raise httpx.ReadTimeout('PRIVATE_PROVIDER_ERROR_SENTINEL', request=request)

        session = self.session(timeout)
        with self.assertRaises(PrivateChatError) as failure:
            await session.prepare('test', '说话', reply_mode='voice')
        self.assertEqual(failure.exception.code, 'model_timeout')
        self.assertNotIn('PRIVATE_PROVIDER_ERROR_SENTINEL', str(failure.exception))
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(any(turn.role == 'assistant' for turn in session.conversation.turns))

    async def test_provider_quota_reaches_group_as_specific_safe_error_without_retry(self):
        session = self.session(lambda request: httpx.Response(
            429, json={'error': {'message': 'PRIVATE_PROVIDER_ERROR_SENTINEL'}}))
        with self.assertRaises(PrivateChatError) as failure:
            await session.prepare('test', '说话', reply_mode='voice')
        self.assertEqual(failure.exception.code, 'model_quota')
        self.assertNotIn('PRIVATE_PROVIDER_ERROR_SENTINEL', str(failure.exception))
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(any(turn.role == 'assistant' for turn in session.conversation.turns))


if __name__ == '__main__':
    unittest.main()
