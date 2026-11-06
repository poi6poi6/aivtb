"""Offline regressions for useful recall without unwanted reminders."""
import tempfile
import unittest
from pathlib import Path

from brain.autobiographical_memory import AutobiographicalMemory
from brain.config import LLMConfig, SelectiveMemoryConfig
from brain.events import Event


class MemoryRecallQualityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 1800000000.0
        self.cfg = SelectiveMemoryConfig(
            state_path=str(Path(self.temp.name) / 'memory.json'),
            core_memories_path=str(Path(self.temp.name) / 'missing.yaml'),
            llm_extraction=False,
        )
        self.memory = AutobiographicalMemory(
            self.cfg, LLMConfig(api_key_env='NO_MEMORY_RECALL_TEST_KEY'),
            '测试角色', clock=lambda: self.now,
        )

    async def asyncTearDown(self):
        await self.memory.close()
        self.temp.cleanup()

    def event(self, text, user='小明', source='voice'):
        return Event(source, 'chat', text, user=user, ts=self.now)

    async def store(self, text):
        return await self.memory.consolidate(self.event(text), '', '平静')

    async def test_repeated_explicit_question_keeps_answer_without_reconfirming_fact(self):
        record = (await self.store('我喜欢咖啡'))[0]
        evidence_time = self.memory._last_confirmed_at(record)
        self.now += 60
        for _ in range(3):
            self.assertIn('咖啡', await self.memory.context_for(self.event('你还记得我喜欢什么吗？')))
            self.now += 1
        self.assertEqual(self.memory._last_confirmed_at(record), evidence_time)
        self.assertEqual(record['access_count'], 3)

    async def test_current_question_with_know_me_is_not_a_request_for_all_memories(self):
        await self.store('我喜欢咖啡')
        self.assertEqual(await self.memory.context_for(self.event('你知道我今天该怎么穿搭吗？')), '')
        self.assertEqual(self.memory.recall_snapshot()['rejected']['unrelated'], 1)

    async def test_unrelated_chat_does_not_receive_outstanding_commitment(self):
        await self.memory.consolidate(
            self.event('可以继续讲这个故事吗'), '待会我会接着给你讲这个故事。', '开心',
        )
        self.assertEqual(await self.memory.context_for(self.event('今天天气怎么样？')), '')
        self.assertIn('待办承诺', await self.memory.context_for(self.event('然后呢？')))
        self.assertIn('待办承诺', await self.memory.context_for(self.event('你答应我的事情呢？')))
        self.assertEqual(await self.memory.context_for(self.event('然后呢？', user='小红')), '')

    async def test_commitment_can_still_be_recalled_by_its_topic(self):
        await self.memory.consolidate(
            self.event('可以继续讲这个故事吗'), '待会我会接着给你讲这个故事。', '开心',
        )
        self.assertIn('待办承诺', await self.memory.context_for(self.event('那个故事后来怎么样了？')))

    async def test_personal_preference_question_does_not_pull_unrelated_commitment(self):
        await self.store('我喜欢咖啡')
        await self.memory.consolidate(
            self.event('可以继续讲这个故事吗'), '待会我会接着给你讲这个故事。', '开心',
        )
        context = await self.memory.context_for(self.event('你还记得我喜欢什么吗？'))
        self.assertIn('咖啡', context)
        self.assertNotIn('待办承诺', context)

    async def test_plan_context_preserves_relative_date_as_historical_statement(self):
        await self.store('我明天要参加漫展')
        self.now += 3600
        context = await self.memory.context_for(self.event('我的漫展计划你还记得吗？'))
        self.assertIn('观众事实·近期计划', context)
        self.assertIn('历史计划，未确认完成', context)
        self.assertIn('相对日期以当次原话时间为准', context)
        self.assertNotIn('待办承诺', context)

    async def test_reconfirmation_is_distinguished_from_first_event_time(self):
        await self.store('我喜欢咖啡')
        self.now += 2 * 86400
        await self.store('我喜欢咖啡')
        context = await self.memory.context_for(self.event('咖啡'))
        self.assertIn('2天前·最近确认:一小时内', context)

    def test_relative_time_labels_do_not_claim_calendar_days(self):
        for age, expected in [(3599, '一小时内'), (3600, '24小时内'),
                              (86399, '24小时内'), (86400, '1天前'), (172800, '2天前')]:
            with self.subTest(age=age):
                self.assertEqual(self.memory._time_label(self.now - age, self.now), expected)

    async def test_old_stable_preference_remains_recallable_and_fresh(self):
        record = (await self.store('我喜欢咖啡'))[0]
        self.now += 10 * 365 * 86400
        self.assertIn('咖啡', await self.memory.context_for(self.event('你还记得我喜欢什么吗？')))
        selected = self.memory.recall_snapshot()['selected'][0]
        self.assertEqual(selected['retention_type'], 'stable_fact')
        self.assertEqual(selected['freshness'], 1.0)
        self.assertEqual(selected['last_confirmed_at'], self.memory._last_confirmed_at(record))
        self.assertGreater(selected['score'], 0)

    async def test_old_episode_fades_from_weak_association_but_answers_explicit_recall(self):
        event = Event('game', 'find', '直播中终于吃到了香蕉', ts=self.now)
        record = (await self.memory.consolidate(event, '香蕉补充能量。', '开心'))[0]
        self.assertIn('香蕉', await self.memory.context_for(self.event('我们去挑苹果吧')))
        self.now += 40 * 86400
        self.assertEqual(await self.memory.context_for(self.event('我们去挑苹果吧')), '')
        self.assertEqual(record['status'], 'active')
        self.assertIn('香蕉', await self.memory.context_for(self.event('还记得上次吃香蕉吗？')))

    def test_legacy_confirmation_uses_evidence_never_management_edit_time(self):
        record = {'updated_at': self.now, 'original_source': {'event_ts': self.now - 86400},
                  'created_at': self.now - 3600}
        self.assertEqual(self.memory._last_confirmed_at(record), self.now - 86400)
        self.assertIsNone(self.memory._last_confirmed_at({'updated_at': self.now}))
        self.assertIsNone(self.memory._last_confirmed_at({'last_confirmed_at': float('nan')}))

    async def test_expired_and_forgotten_records_are_not_fresh_or_recalled(self):
        record = (await self.store('我今天喜欢咖啡'))[0]
        self.now += 86401
        self.assertEqual(await self.memory.context_for(self.event('咖啡')), '')
        self.assertEqual(self.memory._freshness(record, self.now), 0)
        permanent = (await self.store('我喜欢猫咪'))[0]
        await self.memory.forget(permanent['id'])
        self.assertEqual(self.memory._freshness(permanent, self.now), 0)


if __name__ == '__main__':
    unittest.main()
