"""Local-console lifecycle metadata must not migrate or strengthen memories."""
import copy
import tempfile
import unittest
from pathlib import Path

from brain.autobiographical_memory import AutobiographicalMemory
from brain.config import LLMConfig, SelectiveMemoryConfig
from brain.events import Event


class MemoryDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.now = 1800000000.0
        self.memory = AutobiographicalMemory(
            SelectiveMemoryConfig(
                state_path=str(Path(self.folder.name) / 'memory.json'),
                core_memories_path=str(Path(self.folder.name) / 'none.yaml'),
                llm_extraction=False,
            ), LLMConfig(api_key_env='NO_MEMORY_DIAGNOSTICS_KEY'),
            '测试角色', clock=lambda: self.now,
        )
        self.addAsyncCleanup(self.memory.close)

    async def store(self, text):
        event = Event('companion', 'chat', text, user='你', ts=self.now)
        return (await self.memory.consolidate(event, '', '平静'))[0]

    async def test_legacy_list_metadata_is_read_only_and_uses_evidence_time(self):
        record = await self.store('我最喜欢草莓蛋糕')
        record.pop('retention_type', None)
        record.pop('last_confirmed_at', None)
        evidence_time = record['source']['event_ts']
        self.now += 86400
        record['updated_at'] = self.now
        before = copy.deepcopy(self.memory.state)
        item = (await self.memory.list_memories())[0]
        self.assertEqual(item['retention_type'], 'stable_fact')
        self.assertEqual(item['last_confirmed_at'], evidence_time)
        self.assertEqual(item['freshness'], 1.0)
        self.assertEqual(self.memory.state, before)

    async def test_viewing_and_recalling_does_not_refresh_confirmation_or_expiry(self):
        record = await self.store('我今天不喜欢咖啡')
        confirmed, expires = record['last_confirmed_at'], record['expires_at']
        self.now += 3600
        await self.memory.context_for(Event('companion', 'chat', '咖啡', user='你', ts=self.now))
        first = (await self.memory.list_memories())[0]
        self.now += 3600
        second = (await self.memory.list_memories())[0]
        self.assertEqual(second['last_confirmed_at'], confirmed)
        self.assertEqual(second['expires_at'], expires)
        self.assertGreaterEqual(first['freshness'], second['freshness'])
        self.assertEqual(record['reinforcement_count'], 0)

    async def test_manual_plan_correction_preserves_lifecycle_and_deadline(self):
        record = await self.store('我明天要参加考试')
        self.now += 100
        self.assertTrue(await self.memory.edit_memory(record['id'], '观众你明天要参加面试'))
        new = next(item for item in await self.memory.list_memories() if item['status'] == 'active')
        self.assertEqual(new['retention_type'], 'plan')
        self.assertEqual(new['expires_at'], record['expires_at'])
        self.assertEqual(new['owner_key'], record['owner_key'])

    async def test_expired_diagnostics_never_appear_as_fresh_or_recalled(self):
        record = await self.store('我今天不喜欢咖啡')
        await self.memory.context_for(Event('companion', 'chat', '咖啡', user='你', ts=self.now))
        self.now = record['expires_at'] + 1
        item = (await self.memory.list_memories())[0]
        self.assertEqual(item['status'], 'expired')
        self.assertEqual(item['freshness'], 0.0)
        self.assertEqual(self.memory.recall_snapshot()['selected'], [])


if __name__ == '__main__':
    unittest.main()
