"""Multi-turn regression scenarios across management, delayed jobs and restarts."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from brain.autobiographical_memory import AutobiographicalMemory, MemoryCandidate
from brain.config import LLMConfig, SelectiveMemoryConfig, WebUIConfig
from brain.conversation import ConversationState
from brain.events import Event
from brain.webui import WebUI


class MemoryLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 1800000000.0
        self.cfg = SelectiveMemoryConfig(state_path=str(Path(self.temp.name) / 'memory.json'),
                                         llm_extraction=False, minimum_recall_score=0.1)
        self.memory = self.open_memory()

    def open_memory(self):
        return AutobiographicalMemory(self.cfg, LLMConfig(api_key_env='NO_LIFECYCLE_KEY'),
                                      '助手', clock=lambda: self.now)

    def event(self, text, user='你', source='voice'):
        return Event(source, 'chat', text, user=user, ts=self.now)

    async def asyncTearDown(self):
        await self.memory.close()
        self.temp.cleanup()

    async def test_edit_syncs_current_context_and_survives_restart(self):
        state = ConversationState()
        original = self.event('我喜欢咖啡')
        original.ui_id = 1
        state.observe_event(original)
        state.record_reply(original, '你喜欢咖啡。')
        state.observe_event(self.event('我喜欢苹果', user='朋友'))
        old = (await self.memory.consolidate(original, '', '平静'))[0]
        self.now += 10
        ui = WebUI(WebUIConfig(), asyncio.Queue(), self.temp.name, {}, selective_memory=self.memory)
        async def sync(memory, replacement):
            state.apply_memory_change(memory, replacement, self.now)
        ui.on_memory_change = sync
        class Socket:
            async def send_str(self, message):
                pass
        await ui._manage_memory({'action':'edit', 'id':old['id'], 'text':'观众你喜欢茶'}, Socket())
        # A cancelled generation finishing late cannot restore the original claim.
        state.record_reply(original, '你喜欢咖啡。')
        state.mark_interrupted(original)
        self.now += 1
        context = state.context_for(self.event('我喜欢什么？'))
        self.assertIn('喜欢茶', context)
        self.assertNotIn('咖啡', context)
        self.assertIn('苹果', context)
        await self.memory.close()
        self.memory = self.open_memory()
        recalled = await self.memory.context_for(self.event('茶'))
        self.assertIn('喜欢茶', recalled)
        self.assertNotIn('咖啡', recalled)
        self.assertEqual(await self.memory.consolidate(original, '旧任务', '平静'), [])

    async def test_forget_blocks_delayed_extraction_including_changed_representation(self):
        original = self.event('我喜欢咖啡')
        old = (await self.memory.consolidate(original, '', '平静'))[0]
        self.now += 1
        started, release = asyncio.Event(), asyncio.Event()
        async def delayed(event, reply):
            started.set()
            await release.wait()
            return [MemoryCandidate('shared_episode', '直播间', '回忆', '喝咖啡',
                                    '观众喜欢喝咖啡', 0.8, 0.8, slot='different-summary')]
        self.cfg.llm_extraction = True
        self.memory.llm = SimpleNamespace(api_key='test')
        with patch.object(self.memory, '_heuristic_candidates', return_value=([], None)), \
             patch.object(self.memory, '_should_ask_llm', return_value=True), \
             patch.object(self.memory, '_extract_with_llm', side_effect=delayed):
            task = asyncio.create_task(self.memory.consolidate(original, '', '平静'))
            await asyncio.wait_for(started.wait(), 2)
            self.now += 1
            await self.memory.forget(old['id'])
            self.now += 1
            release.set()
            self.assertEqual(await task, [])
        self.assertFalse(any(item['status'] == 'active' for item in self.memory.state['memories']))

    async def test_barrier_survives_history_pruning_and_allows_new_direct_statement(self):
        original = self.event('我喜欢咖啡')
        old = (await self.memory.consolidate(original, '', '平静'))[0]
        self.now += 10
        await self.memory.forget(old['id'])
        self.memory.state['memories'].clear()
        self.memory._save()
        await self.memory.close()
        self.memory = self.open_memory()
        self.now += 10
        self.assertEqual(await self.memory.consolidate(original, '', '平静'), [])
        summary = self.event('我喜欢咖啡', source='system')
        self.assertEqual(await self.memory.consolidate(summary, '', '平静'), [])
        fresh = await self.memory.consolidate(self.event('我喜欢咖啡'), '', '平静')
        self.assertEqual(len(fresh), 1)
        self.assertEqual(fresh[0]['status'], 'active')

    async def test_temporary_state_expires_after_restart_without_losing_preference(self):
        await self.memory.consolidate(self.event('我喜欢咖啡'), '', '平静')
        self.now += 1
        await self.memory.consolidate(self.event('我今天不喜欢咖啡'), '', '平静')
        await self.memory.close()
        self.now += 86401
        self.memory = self.open_memory()
        memories = await self.memory.list_memories()
        self.assertEqual([(m['value'], m['status']) for m in memories],
                         [('不喜欢', 'expired'), ('喜欢', 'active')])

    async def test_forget_does_not_leave_correction_note_or_late_playback(self):
        state = ConversationState()
        old = (await self.memory.consolidate(self.event('我喜欢咖啡'), '', '平静'))[0]
        self.now += 1
        state.apply_memory_change(old, '观众你喜欢茶', self.now)
        self.now += 1
        state.apply_memory_change(old, None, self.now)
        item = SimpleNamespace(event={'user':'你', 'ts':self.now - 10}, created_at=self.now - 10)
        self.assertIsNone(state.record_playback(item, True))
        context = state.context_for(self.event('你还记得吗'))
        self.assertIn('忘记', context)
        self.assertNotIn('茶', context)
        self.assertNotIn('咖啡', context)
