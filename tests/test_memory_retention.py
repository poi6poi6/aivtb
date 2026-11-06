"""Retention follows original evidence, not model guesses or queue latency."""
import tempfile
import unittest
from pathlib import Path

from brain.autobiographical_memory import AutobiographicalMemory, memory_retention_type
from brain.config import LLMConfig, SelectiveMemoryConfig
from brain.events import Event


class MemoryRetentionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 1_800_000_000.0
        self.memory = AutobiographicalMemory(
            SelectiveMemoryConfig(state_path=str(Path(self.temp.name) / 'memory.json'),
                                  core_memories_path=str(Path(self.temp.name) / 'none.yaml'),
                                  llm_extraction=False),
            LLMConfig(api_key_env='NO_MEMORY_RETENTION_KEY'), '测试角色', clock=lambda: self.now,
        )

    async def asyncTearDown(self):
        await self.memory.close()
        self.temp.cleanup()

    def event(self, text, *, ts=None):
        return Event('voice', 'chat', text, user='你', ts=self.now if ts is None else ts)

    def extract(self, source, value, *, kind='viewer_fact', ttl=None):
        return self.memory._validate_llm_memories([
            dict(kind=kind, predicate='自述', value=value, confidence=.8, importance=.8, ttl_days=ttl)
        ], self.event(source))[0]

    async def test_unrelated_time_clause_does_not_expire_stable_preference(self):
        records = await self.memory.consolidate(
            self.event('我最喜欢草莓蛋糕，今天准备出门'), '', '平静')
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['retention_type'], 'stable_fact')
        self.assertEqual(records[0]['scope'], 'lasting')
        self.assertIsNone(records[0]['expires_at'])
        candidate = self.extract('我最喜欢草莓蛋糕，今天准备出门', '草莓蛋糕')
        self.assertEqual(candidate.retention_type, 'stable_fact')
        self.assertIsNone(candidate.ttl_days)

    async def test_multiple_explicit_clauses_keep_independent_lifetimes(self):
        records = await self.memory.consolidate(
            self.event('我喜欢咖啡，我今天不喜欢奶茶'), '', '平静')
        by_type = {record['retention_type']: record for record in records}
        self.assertEqual(set(by_type), {'stable_fact', 'temporary_state'})
        self.assertIsNone(by_type['stable_fact']['expires_at'])
        self.assertEqual(by_type['temporary_state']['expires_at'], self.now + 86400)
        self.assertEqual(by_type['temporary_state']['evidence'], '我今天不喜欢奶茶')

    async def test_ongoing_and_future_plans_have_consistent_bounded_retention(self):
        for text, ttl in [('我最近在学画画', 45), ('我正在做模型', 45),
                          ('我明天要考试', 3), ('我下周要参加漫展', 14),
                          ('我今天准备学画画', 1)]:
            with self.subTest(text=text):
                heuristic = self.memory._viewer_fact_candidates('你', text)
                self.assertEqual(len(heuristic), 1)
                model = self.extract(text, text)
                for candidate in [heuristic[0], model]:
                    self.assertEqual(candidate.retention_type, 'plan')
                    self.assertEqual(candidate.ttl_days, ttl)

    async def test_model_episode_without_ttl_is_bounded_but_stable_fact_is_not(self):
        episode = self.extract('我第一次参加漫展', '第一次参加漫展', kind='shared_episode')
        self.assertEqual(episode.retention_type, 'episode')
        self.assertEqual(episode.ttl_days, self.memory.cfg.episode_retention_days)
        preference = self.extract('我喜欢咖啡', '喜欢咖啡')
        self.assertEqual(preference.retention_type, 'stable_fact')
        self.assertIsNone(preference.ttl_days)

    async def test_temporary_model_ttl_cannot_extend_explicit_today(self):
        candidate = self.extract('我今天喜欢奶茶', '喜欢奶茶', ttl=365)
        self.assertEqual(candidate.ttl_days, 1)
        self.assertEqual(candidate.retention_type, 'temporary_state')

    async def test_model_mislabelled_plan_keeps_its_explicit_short_horizon(self):
        candidate = self.extract('我明天要考试', '我明天要考试', kind='shared_episode')
        self.assertEqual(candidate.kind, 'shared_episode')
        self.assertEqual(candidate.retention_type, 'plan')
        self.assertEqual(candidate.ttl_days, 3)
        self.assertEqual(memory_retention_type(dict(kind='shared_episode', evidence='我明天要考试')), 'plan')

    async def test_model_mislabelled_temporary_preference_is_not_a_long_episode(self):
        candidate = self.extract('我今天不喜欢奶茶', '我今天不喜欢奶茶', kind='shared_episode')
        self.assertEqual(candidate.retention_type, 'temporary_state')
        self.assertEqual(candidate.scope, 'temporary')
        self.assertEqual(candidate.ttl_days, 1)
        for text in ['我今天完成重要的比赛', '我今天完成了模型']:
            with self.subTest(text=text):
                candidate = self.extract(text, text, kind='viewer_fact')
                self.assertEqual(candidate.retention_type, 'episode')
                self.assertEqual(candidate.ttl_days, self.memory.cfg.episode_retention_days)
        preference = self.extract('我今天喜欢完成任务的感觉', '我今天喜欢完成任务的感觉')
        self.assertEqual(preference.retention_type, 'temporary_state')
        self.assertEqual(preference.ttl_days, 1)

    async def test_same_message_conflicting_facts_keep_last_explicit_clause(self):
        event = self.event('我喜欢咖啡；我现在不喜欢咖啡；我喜欢茶')
        records = await self.memory.consolidate(event, '', '平静')
        self.assertEqual(len(records), 2)
        coffee = next(item for item in records if item['predicate'] == '对咖啡的偏好')
        self.assertEqual(coffee['value'], '不喜欢')
        self.assertEqual(coffee['evidence'], '我现在不喜欢咖啡')
        self.assertTrue(all(item['status'] == 'active' for item in records))
        await self.memory.consolidate(event, '第二段', '平静')
        self.assertEqual(len(self.memory.state['memories']), 2)
        self.assertTrue(all(item['reinforcement_count'] == 0 for item in records))

    async def test_same_message_repeated_value_and_candidate_order_preserve_source_order(self):
        event = self.event('我喜欢咖啡；我现在不喜欢咖啡；我喜欢咖啡')
        candidates = self.memory._viewer_fact_candidates('你', event.text)
        selected = self.memory._deduplicate_candidates(list(reversed(candidates)), event)
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].value, '喜欢')
        records = await self.memory.consolidate(event, '', '平静')
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['value'], '喜欢')

    async def test_model_mislabelled_experience_is_bounded_without_expiring_preferences(self):
        for text in ['这是我第一次参加漫展', '我第一次参加漫展', '我已经完成模型', '我昨天完成了模型']:
            with self.subTest(text=text):
                candidate = self.extract(text, text, kind='viewer_fact')
                self.assertEqual(candidate.kind, 'viewer_fact')
                self.assertEqual(candidate.retention_type, 'episode')
                self.assertEqual(candidate.ttl_days, self.memory.cfg.episode_retention_days)
        for text in ['我最喜欢参加漫展', '我最喜欢第一次参加漫展的感觉', '我喜欢完成任务的成就感']:
            with self.subTest(text=text):
                candidate = self.extract(text, text, kind='viewer_fact')
                self.assertEqual(candidate.retention_type, 'stable_fact')
                self.assertIsNone(candidate.ttl_days)

    async def test_delayed_temporary_evidence_is_already_expired(self):
        timestamp = self.now - 2 * 86400
        records = await self.memory.consolidate(self.event('我今天喜欢奶茶', ts=timestamp), '', '平静')
        self.assertEqual(records[0]['expires_at'], timestamp + 86400)
        self.assertEqual(records[0]['last_confirmed_at'], timestamp)
        self.assertEqual(records[0]['status'], 'expired')
        self.assertEqual(await self.memory.context_for(self.event('奶茶')), '')

    async def test_distinct_evidence_refreshes_confirmation_not_original_event(self):
        original = self.event('我最近喜欢咖啡')
        record = (await self.memory.consolidate(original, '', '平静'))[0]
        first_event_time = record['event_time']
        self.now += 86400
        second = self.event('我最近喜欢咖啡')
        await self.memory.consolidate(second, '', '平静')
        self.assertEqual(record['last_confirmed_at'], self.now)
        self.assertEqual(record['expires_at'], self.now + 7 * 86400)
        self.assertEqual(record['event_time'], first_event_time)
        confirmed, expires, count = record['last_confirmed_at'], record['expires_at'], record['reinforcement_count']
        self.now += 86400
        await self.memory.context_for(self.event('咖啡'))
        await self.memory.consolidate(original, '旧消息的另一个片段', '平静')
        await self.memory.consolidate(self.event('我最近喜欢咖啡', ts=second.ts - 1), '', '平静')
        self.assertEqual(record['last_confirmed_at'], confirmed)
        self.assertEqual(record['expires_at'], expires)
        self.assertEqual(record['reinforcement_count'], count)

    async def test_late_old_value_cannot_roll_back_new_current_fact(self):
        older = self.event('我喜欢咖啡')
        self.now += 60
        current = (await self.memory.consolidate(self.event('我不喜欢咖啡'), '', '平静'))[0]
        self.now += 60
        historical = (await self.memory.consolidate(older, '', '平静'))[0]
        self.assertEqual(current['status'], 'active')
        self.assertEqual(historical['status'], 'superseded')
        self.assertEqual(historical['superseded_by'], current['id'])
        self.assertEqual(historical['valid_to'], current['last_confirmed_at'])
        self.assertEqual([r['value'] for r in self.memory.state['memories'] if r['status'] == 'active'], ['不喜欢'])

    async def test_confirmed_time_clamps_future_clock_and_commitment_uses_delivery(self):
        future = (await self.memory.consolidate(self.event('我今天喜欢咖啡', ts=self.now + 86400), '', '平静'))[0]
        self.assertEqual(future['last_confirmed_at'], self.now)
        self.assertEqual(future['expires_at'], self.now + 86400)
        commitment = (await self.memory.consolidate(
            self.event('好呀', ts=self.now - 3 * 86400), '下次我会一起讨论这本书。', '平静'))[0]
        self.assertEqual(commitment['retention_type'], 'commitment')
        self.assertEqual(commitment['last_confirmed_at'], self.now)
        self.assertEqual(commitment['expires_at'], self.now + 30 * 86400)

    async def test_confirmation_and_original_expiry_survive_reload(self):
        event = self.event('我今天喜欢奶茶')
        record = (await self.memory.consolidate(event, '', '平静'))[0]
        expires_at = record['expires_at']
        await self.memory.close()
        self.now += 3600
        self.memory = AutobiographicalMemory(self.memory.cfg, self.memory.llm, '测试角色', clock=lambda: self.now)
        replayed = (await self.memory.consolidate(event, '', '平静'))[0]
        self.assertEqual(replayed['last_confirmed_at'], event.ts)
        self.assertEqual(replayed['expires_at'], expires_at)
        self.assertEqual(replayed['reinforcement_count'], 0)
        self.assertEqual(replayed['retention_type'], 'temporary_state')

    async def test_legacy_classification_does_not_mutate_records(self):
        records = [
            ({'kind': 'viewer_fact', 'predicate': '明天要', 'value': '考试'}, 'plan'),
            ({'kind': 'viewer_fact', 'predicate': '自述', 'evidence': '我最近在学画画'}, 'plan'),
            ({'kind': 'viewer_fact', 'scope': 'temporary'}, 'temporary_state'),
            ({'kind': 'viewer_fact'}, 'stable_fact'),
            ({'kind': 'shared_episode'}, 'episode'),
        ]
        for record, expected in records:
            before = dict(record)
            self.assertEqual(memory_retention_type(record), expected)
            self.assertEqual(record, before)


if __name__ == '__main__':
    unittest.main()
