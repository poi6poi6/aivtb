"""Offline delivery receipts, bounded extraction, and private recall boundaries."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from brain.autobiographical_memory import AutobiographicalMemory
from brain.config import LLMConfig, PersonaConfig, PersonaLLMConfig, SelectiveMemoryConfig
from brain.events import Event
from brain.persona import Persona
from brain.private_chat import PrivateChatError, PrivateChatSession
from brain.qq_group_chat import GroupChatSession
from brain.viewer_identity import qq_private_binding


class QQPrivateMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.path = self.folder / 'chat.json'
        cfg = SelectiveMemoryConfig(state_path=str(self.folder / 'memory.json'),
                                    core_memories_path=str(self.folder / 'none.yaml'),
                                    llm_extraction=False)
        self.memory = AutobiographicalMemory(cfg, LLMConfig(api_key_env='NO_QQ_PRIVATE_TEST_KEY'), '测试角色')
        self.addAsyncCleanup(self.memory.close)
        self.backend = Mock(available=True, complete=AsyncMock(return_value='记住了。'), close=AsyncMock())
        with patch('brain.gemini_chat.GeminiChat', return_value=self.backend):
            self.parent = Persona(PersonaConfig(name='测试角色'), LLMConfig(),
                                  Mock(viewer_note=Mock(return_value='')),
                                  persona_llm=PersonaLLMConfig(backend='gemini_api'))

    def session(self, *, path=None, peer='peer-one', memory=None, bind=True):
        session = PrivateChatSession(self.parent, path or self.path, '你',
                                     autobiography=self.memory if memory is None else memory)
        self.addAsyncCleanup(session.close)
        if bind:
            session.bind_memory_identity('app-one', peer)
        return session

    @staticmethod
    async def drain(session):
        task = session._memory_worker
        if task is not None:
            await asyncio.wait_for(asyncio.shield(task), 2)

    async def delivered(self, session, message_id='first', text='我最喜欢草莓蛋糕'):
        prepared = await session.prepare(message_id, text)
        self.assertTrue(await session.commit(prepared.receipt_id, True))
        await self.drain(session)
        return prepared

    def event(self, *, source='companion', owner=False, peer=None, user='你'):
        event = Event(source, 'chat', '你还记得我喜欢什么吗？', user=user)
        event.owner_control = owner
        if peer is not None:
            event._qq_private_binding = qq_private_binding('app-one', peer)
        return event

    async def test_only_confirmed_delivery_writes_once_and_retry_is_safe(self):
        session = self.session()
        prepared = await session.prepare('first', '我最喜欢草莓蛋糕')
        self.assertEqual(await self.memory.list_memories(), [])
        self.assertFalse(await session.commit('unknown-receipt', True))
        self.assertFalse(await session.commit(prepared.receipt_id, False))
        self.assertIsNone(session._memory_worker)
        self.assertEqual(await self.memory.list_memories(), [])
        delivered = await self.delivered(session)
        self.assertFalse(await session.commit(delivered.receipt_id, True))
        records = await self.memory.list_memories()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['visibility'], 'private')
        self.assertEqual(records[0]['private_binding'], qq_private_binding('app-one', 'peer-one'))
        self.assertEqual(records[0]['reinforcement_count'], 0)
        self.assertEqual(self.memory.state['timeline_events'], [])
        self.assertEqual(self.memory.state['concepts'], [])
        self.assertEqual(session._history[0]['memory_state'], 'stored')

    async def test_missing_or_unknown_receipt_and_unbound_session_never_write(self):
        session = self.session()
        prepared = await session.prepare('first', '我最喜欢草莓蛋糕')
        with self.assertRaises(ValueError):
            await session.commit(prepared.receipt_id, None)
        await session.close()
        self.assertEqual(await self.memory.list_memories(), [])
        unbound = self.session(path=self.folder / 'unbound.json', bind=False)
        await self.delivered(unbound)
        self.assertEqual(await self.memory.list_memories(), [])

    async def test_recall_requires_same_binding_or_verified_local_owner(self):
        await self.delivered(self.session())
        record = (await self.memory.list_memories())[0]
        for event in (self.event(), self.event(peer='peer-two'),
                      self.event(source='danmaku', owner=True, peer='peer-one'),
                      self.event(source='qq_group', owner=True, peer='peer-one'),
                      self.event(owner=True, user='别人')):
            with self.subTest(source=event.source, binding=getattr(event, '_qq_private_binding', '')):
                self.assertFalse(self.memory._visible_to_event(record, event))
                self.assertEqual(await self.memory.context_for(event), '')
        for event in (self.event(peer='peer-one'), self.event(owner=True),
                      self.event(source='voice', owner=True)):
            self.memory.state['memories'][0]['last_recalled_at'] = 0
            self.assertIn('草莓蛋糕', await self.memory.context_for(event))
        # A new app binding is distinct even if peer and display name are equal.
        event = self.event(peer='peer-one')
        event._qq_private_binding = qq_private_binding('app-two', 'peer-one')
        self.assertEqual(await self.memory.context_for(event), '')

    async def test_public_and_private_slots_do_not_overwrite_or_reinforce_each_other(self):
        public = Event('companion', 'chat', '我最喜欢巧克力蛋糕', user='你')
        await self.memory.consolidate(public, '', '平静')
        await self.delivered(self.session())
        records = await self.memory.list_memories()
        self.assertEqual(len(records), 2)
        self.assertTrue(all(item['status'] == 'active' for item in records))
        self.assertTrue(all(item['reinforcement_count'] == 0 for item in records))
        public_context = await self.memory.context_for(self.event())
        self.assertIn('巧克力蛋糕', public_context)
        self.assertNotIn('草莓蛋糕', public_context)

    async def test_concurrent_same_name_peers_keep_evidence_and_forget_barriers_separate(self):
        first = self.session(path=self.folder / 'one.json', peer='peer-one')
        second = self.session(path=self.folder / 'two.json', peer='peer-two')
        # Same timestamp and text are still distinct platform evidence.
        with patch('brain.private_chat.time.time', return_value=1800000000.):
            await asyncio.gather(self.delivered(first), self.delivered(second))
        records = await self.memory.list_memories()
        self.assertEqual(len(records), 2)
        self.assertNotEqual(records[0]['slot'], records[1]['slot'])
        self.assertNotEqual(records[0]['evidence_ids'], records[1]['evidence_ids'])
        selected = next(row for row in records if row['private_binding'] == qq_private_binding('app-one', 'peer-one'))
        self.assertTrue(await self.memory.forget(selected['id']))
        await second.apply_memory_change(selected)
        self.assertEqual(second._corrections, [])
        self.assertEqual(await self.memory.context_for(self.event(peer='peer-one')), '')
        self.assertIn('草莓蛋糕', await self.memory.context_for(self.event(peer='peer-two')))

    async def test_manual_edit_preserves_scope_and_foreign_binding_cannot_receive_correction(self):
        session = self.session()
        await self.delivered(session)
        record = (await self.memory.list_memories())[0]
        self.assertTrue(await self.memory.edit_memory(record['id'], '你喜欢巧克力蛋糕'))
        active = [row for row in await self.memory.list_memories() if row['status'] == 'active'][0]
        self.assertEqual(active['visibility'], 'private')
        self.assertEqual(active['private_binding'], record['private_binding'])
        self.assertEqual(active['owner_key'], record['owner_key'])
        self.assertEqual(self.memory.state['timeline_events'], [])
        other = self.session(path=self.folder / 'other.json', peer='peer-two')
        await other.apply_memory_change(record, '你喜欢巧克力蛋糕')
        self.assertEqual(other._corrections, [])
        await session.apply_memory_change(record, '你喜欢巧克力蛋糕')
        self.assertIn('巧克力蛋糕', session._corrections[0])
        self.assertEqual(session._history, [])
        self.assertEqual(await self.memory.context_for(self.event(peer='peer-two')), '')
        self.assertIn('巧克力蛋糕', await self.memory.context_for(self.event(owner=True)))

    async def test_restart_migration_cannot_promote_private_memories_into_shared_graph(self):
        session = self.session()
        await self.delivered(session)
        record = (await self.memory.list_memories())[0]
        self.assertIsNone(self.memory.graph.anchor([record], self.event(peer='peer-one')))
        await session.close()
        await self.memory.close()
        reopened = AutobiographicalMemory(self.memory.cfg, LLMConfig(api_key_env='NO_QQ_PRIVATE_TEST_KEY'), '测试角色')
        self.addAsyncCleanup(reopened.close)
        self.assertEqual(reopened.state['timeline_events'], [])
        self.assertEqual(reopened.state['concepts'], [])
        self.assertEqual(reopened.state['graph_edges'], [])
        self.assertNotIn('草莓蛋糕', str(reopened.summary()))
        self.assertEqual(await reopened.context_for(self.event(source='danmaku')), '')
        self.assertIn('草莓蛋糕', await reopened.context_for(self.event(peer='peer-one')))

    async def test_same_name_history_cannot_be_rebound_to_another_peer(self):
        session = self.session()
        await self.delivered(session)
        session.bind_memory_identity('app-one', 'peer-one')
        with self.assertRaises(PrivateChatError):
            session.bind_memory_identity('app-one', 'peer-two')
        await session.close()
        with self.assertRaises(PrivateChatError):
            self.session(peer='peer-two')
        unbound = self.session(bind=False)
        with self.assertRaises(PrivateChatError):
            await unbound.prepare('unverified', '继续聊吧')

    async def test_background_failure_is_safe_and_retries_after_restart(self):
        writer = AsyncMock(side_effect=RuntimeError('PRIVATE_DETAILS_MUST_NOT_ESCAPE'))
        memory = SimpleNamespace(context_for=AsyncMock(return_value=''), consolidate_private=writer)
        session = self.session(memory=memory)
        await self.delivered(session)
        self.assertEqual(session.memory_error, 'memory_failed')
        self.assertEqual(session._history[0]['memory_state'], 'pending')
        self.assertEqual(session._history[0]['memory_attempts'], 1)
        await session.close()
        writer.side_effect = None
        writer.return_value = []
        recovered = self.session(memory=memory)
        await self.drain(recovered)
        self.assertEqual(writer.await_count, 2)
        self.assertEqual(recovered._history[0]['memory_state'], 'stored')
        self.assertEqual(writer.await_args.args[0]._qq_private_binding, qq_private_binding('app-one', 'peer-one'))

    async def test_extraction_timeout_has_bounded_retries_and_does_not_fail_send(self):
        async def blocked(*args, **kwargs):
            await asyncio.Event().wait()

        writer = AsyncMock(side_effect=blocked)
        memory = SimpleNamespace(context_for=AsyncMock(return_value=''), consolidate_private=writer)
        with patch('brain.private_chat.MEMORY_TIMEOUT', .01):
            session = self.session(memory=memory)
            await self.delivered(session)
            for attempt in range(2):
                await session.close()
                session = self.session(memory=memory)
                await self.drain(session)
            self.assertEqual(writer.await_count, 3)
            self.assertEqual(session._history[0]['memory_state'], 'failed')
            self.assertEqual(session.memory_error, 'memory_timeout')
            await session.close()
            session = self.session(memory=memory)
            self.assertIsNone(session._memory_worker)

    async def test_close_keeps_pending_receipt_and_restart_deduplicates_partial_success(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.memory.consolidate_private

        async def store_then_block(*args, **kwargs):
            result = await original(*args, **kwargs)
            entered.set()
            await release.wait()
            return result

        with patch.object(self.memory, 'consolidate_private', side_effect=store_then_block):
            session = self.session()
            prepared = await session.prepare('first', '我最喜欢草莓蛋糕')
            self.assertTrue(await session.commit(prepared.receipt_id, True))
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.wait_for(session.close(), 2)
        state = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(state['history'][0]['memory_state'], 'pending')
        recovered = self.session()
        await self.drain(recovered)
        records = await self.memory.list_memories()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['reinforcement_count'], 0)
        self.assertEqual(recovered._history[0]['memory_state'], 'stored')

    async def test_completion_receipt_waits_for_long_term_durable_flush(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.memory.flush_persistence
        self.memory.enable_background_persistence(debounce_sec=.01)

        async def delayed_flush():
            entered.set()
            await release.wait()
            await original()

        with patch.object(self.memory, 'flush_persistence', side_effect=delayed_flush):
            session = self.session()
            prepared = await session.prepare('first', '我最喜欢草莓蛋糕')
            self.assertTrue(await session.commit(prepared.receipt_id, True))
            await asyncio.wait_for(entered.wait(), 1)
            self.assertEqual(session._history[0]['memory_state'], 'pending')
            release.set()
            await self.drain(session)
        self.assertEqual(session._history[0]['memory_state'], 'stored')
        disk = json.loads((self.folder / 'memory.json').read_text(encoding='utf-8'))
        self.assertEqual(disk['memories'][0]['visibility'], 'private')

    async def test_close_has_deadline_and_late_extraction_cannot_write(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.memory._input_candidates

        async def delayed(event, emotion):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            return await original(event, emotion)

        with patch.object(self.memory, '_input_candidates', side_effect=delayed), \
                patch('brain.private_chat.MEMORY_CLOSE_TIMEOUT', .01):
            session = self.session()
            prepared = await session.prepare('first', '我最喜欢草莓蛋糕')
            self.assertTrue(await session.commit(prepared.receipt_id, True))
            worker = session._memory_worker
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.wait_for(session.close(), .2)
            release.set()
            await asyncio.gather(worker, return_exceptions=True)
        self.assertEqual(await self.memory.list_memories(), [])
        self.assertEqual(session._history[0]['memory_state'], 'pending')

    async def test_forgetting_invalidates_extraction_even_if_it_ignores_cancellation(self):
        session = self.session()
        await self.delivered(session)
        record = (await self.memory.list_memories())[0]
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.memory._input_candidates

        async def delayed(event, emotion):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            return await original(event, emotion)

        with patch.object(self.memory, '_input_candidates', side_effect=delayed):
            prepared = await session.prepare('second', '我最喜欢草莓蛋糕')
            self.assertTrue(await session.commit(prepared.receipt_id, True))
            worker = session._memory_worker
            await asyncio.wait_for(entered.wait(), 1)
            self.assertTrue(await self.memory.forget(record['id']))
            await session.apply_memory_change(record)
            release.set()
            await asyncio.gather(worker, return_exceptions=True)
        self.assertEqual([row for row in await self.memory.list_memories() if row['status'] == 'active'], [])
        self.assertEqual(session._history, [])

    async def test_one_worker_queues_confirmed_turns_without_blocking_commit(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed(*args, **kwargs):
            entered.set()
            await release.wait()
            return []

        writer = AsyncMock(side_effect=delayed)
        memory = SimpleNamespace(context_for=AsyncMock(return_value=''), consolidate_private=writer)
        session = self.session(memory=memory)
        first = await session.prepare('first', '我喜欢草莓蛋糕')
        self.assertTrue(await asyncio.wait_for(session.commit(first.receipt_id, True), .2))
        await asyncio.wait_for(entered.wait(), 1)
        worker = session._memory_worker
        second = await session.prepare('second', '我喜欢咖啡')
        self.assertTrue(await asyncio.wait_for(session.commit(second.receipt_id, True), .2))
        self.assertIs(session._memory_worker, worker)
        self.assertEqual(writer.await_count, 1)
        release.set()
        await self.drain(session)
        self.assertEqual(writer.await_count, 2)
        self.assertTrue(all(row['memory_state'] == 'stored' for row in session._history))

    async def test_group_cannot_bind_or_write_private_memory(self):
        group = GroupChatSession(self.parent, self.folder / 'group.json', 'app-one', 'group-one', 'peer-one')
        self.addAsyncCleanup(group.close)
        with self.assertRaises(PrivateChatError):
            group.bind_memory_identity('app-one', 'peer-one')
        # Even an accidental shared store injection cannot enable private writes.
        group.autobiography = self.memory
        await self.delivered(group)
        self.assertEqual(await self.memory.list_memories(), [])

    async def test_queue_is_bounded_and_continues_after_oldest_turn_ages_out(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed(*args, **kwargs):
            entered.set()
            await release.wait()
            return []

        writer = AsyncMock(side_effect=delayed)
        memory = SimpleNamespace(context_for=AsyncMock(return_value=''), consolidate_private=writer)
        session = self.session(memory=memory)
        capacity = 4
        with patch('brain.private_chat.MAX_HISTORY', capacity):
            for index in range(capacity + 2):
                prepared = await session.prepare(str(index), '我喜欢草莓蛋糕')
                self.assertTrue(await session.commit(prepared.receipt_id, True))
        self.assertEqual(len(session._history), capacity)
        self.assertEqual(writer.await_count, 1)
        release.set()
        await self.drain(session)
        self.assertEqual(writer.await_count, capacity + 1)
        self.assertTrue(all(row['memory_state'] == 'stored' for row in session._history))

