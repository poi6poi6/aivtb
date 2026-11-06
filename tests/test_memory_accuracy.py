"""Offline regressions for factual recall, source identity and corrections."""
import tempfile
import unittest
from pathlib import Path

from brain.autobiographical_memory import AutobiographicalMemory
from brain.config import LLMConfig, SelectiveMemoryConfig
from brain.events import Event
from brain.viewer_identity import viewer_identity


class MemoryAccuracyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 1800000000.
        self.cfg = SelectiveMemoryConfig(state_path=str(Path(self.temp.name) / 'memory.json'),
                                        core_memories_path=str(Path(self.temp.name) / 'none.yaml'),
                                        llm_extraction=False)
        self.memory = self.open_memory()

    def open_memory(self):
        return AutobiographicalMemory(self.cfg, LLMConfig(api_key_env='NO_MEMORY_ACCURACY_KEY'),
                                      '测试角色', clock=lambda: self.now)

    def event(self, text, user='小明', source='danmaku', key=''):
        return Event(source, 'chat', text, user=user, viewer_key=key, ts=self.now)

    async def asyncTearDown(self):
        await self.memory.close()
        self.temp.cleanup()

    async def store(self, text, **kwargs):
        return await self.memory.consolidate(self.event(text, **kwargs), '', '平静')

    async def test_first_and_later_unrelated_recall_never_injects_personal_fact(self):
        await self.store('我最喜欢草莓蛋糕')
        query = self.event('如何求解一元二次方程？')
        query.ui_id = 101
        self.assertEqual(await self.memory.context_for(query), '')
        snapshot = self.memory.recall_snapshot()
        self.assertEqual(snapshot['turn_id'], 101)
        self.assertEqual(snapshot['rejected']['unrelated'], 1)
        self.now += 3600
        self.assertEqual(await self.memory.context_for(query), '')
        self.assertIn('草莓蛋糕', await self.memory.context_for(self.event('你还记得我喜欢什么吗？')))

    async def test_related_synonym_and_original_negation_remain(self):
        await self.store('我不喜欢拿铁')
        self.assertIn('不喜欢拿铁', await self.memory.context_for(self.event('喝什么咖啡？')))

    async def test_mentions_and_unkeyed_cross_source_cannot_read_memory(self):
        await self.store('我最喜欢草莓蛋糕')
        for query in [self.event('小明最喜欢草莓蛋糕吗？', user='小红'),
                      self.event('草莓蛋糕', source='game'),
                      self.event('草莓蛋糕', key='new-account')]:
            self.assertEqual(await self.memory.context_for(query), '')
            self.assertNotIn('草莓蛋糕', str(self.memory.recall_snapshot()))

    async def test_legacy_keyed_and_source_scoped_records_read_without_rebinding(self):
        record = (await self.store('我喜欢咖啡', key='account-one'))[0]
        record['owner_key'] = 'danmaku:account-one'
        record['slot'] = 'old-generated-slot'
        self.assertIn('咖啡', await self.memory.context_for(self.event('咖啡', key='account-one')))
        self.assertEqual(await self.memory.context_for(self.event('咖啡', key='account-two')), '')
        self.now += 1801
        self.assertEqual(await self.memory.context_for(self.event('咖啡')), '')
        self.assertEqual(record['owner_key'], 'danmaku:account-one')

    async def test_unknown_legacy_provenance_stays_quarantined(self):
        record = (await self.store('我喜欢咖啡'))[0]
        record['owner_key'] = ''
        record['source'].pop('source')
        self.assertEqual(await self.memory.context_for(self.event('咖啡')), '')

    async def test_legacy_personal_episode_is_private_even_with_room_subject(self):
        record = (await self.store('我第一次参加漫展'))[0]
        record['subject'] = '直播间'
        record['owner_key'] = ''
        self.assertEqual(await self.memory.context_for(self.event('漫展', user='小红')), '')
        self.assertIn('漫展', await self.memory.context_for(self.event('漫展')))

    async def test_reported_or_retracted_preference_is_not_saved_as_assertion(self):
        for text in ['你说我喜欢咖啡，其实不是', '我喜欢咖啡，其实不是这个意思', '如果我喜欢咖啡会怎样？']:
            self.assertEqual(await self.store(text), [])

    async def test_favorite_categories_coexist_and_same_category_updates(self):
        cake = (await self.store('我最喜欢草莓蛋糕'))[0]
        await self.store('我最喜欢周杰伦的歌曲')
        self.assertEqual(cake['status'], 'active')
        self.now += 1
        await self.store('我最喜欢巧克力蛋糕')
        self.assertEqual(cake['status'], 'superseded')
        self.assertEqual({r['value'] for r in self.memory.state['memories'] if r['status'] == 'active'},
                         {'巧克力蛋糕', '周杰伦的歌曲'})

    def extract(self, source, value, predicate='经历', **extra):
        return self.memory._validate_llm_memories([dict(kind='viewer_fact', predicate=predicate,
            value=value, confidence=.8, importance=.8) | extra], self.event(source))

    async def test_llm_rejects_negation_flip_hypothesis_and_invalid_numbers(self):
        self.assertEqual(self.extract('这是我第一次参加漫展，但我一点也不开心', '第一次参加漫展很开心'), [])
        self.assertEqual(self.extract('我不开心', '开心'), [])
        self.assertEqual(self.extract('如果我参加漫展，我会开心', '参加漫展'), [])
        self.assertEqual(self.extract('如果通过考试，我会参加漫展', '参加漫展'), [])
        self.assertEqual(self.extract('我参加漫展吗？', '参加漫展'), [])
        self.assertEqual(self.extract('我参加漫展', '参加漫展', evidence='我在漫展很开心'), [])
        self.assertEqual(self.extract('我参加漫展', '参加漫展', confidence='nan'), [])

    async def test_llm_quote_preserves_subject_negation_and_does_not_assert_predicate(self):
        result = self.extract('我参加漫展并不开心', '我参加漫展并不开心', predicate='非常高兴')
        self.assertEqual(len(result), 1)
        self.assertNotIn('非常高兴', result[0].summary)
        self.assertIn('并不开心', result[0].summary)
        result = self.extract('小红说她喜欢咖啡', '喜欢咖啡', predicate='喜欢')
        self.assertIn('小红说她喜欢咖啡', result[0].summary)

    async def test_verified_attribute_replaces_old_slot_and_old_record_stays_historical(self):
        first = self.event('我在北京工作')
        a = self.extract(first.text, '北京', '工作城市')[0]
        old = self.memory._upsert(a, first)
        old['slot'] = 'llm:viewer_fact:小明:old-value-fingerprint'
        self.now += 1
        second = self.event('更正，我现在在上海工作')
        b = self.extract(second.text, '上海', '工作城市')[0]
        new = self.memory._upsert(b, second)
        self.assertEqual(old['status'], 'corrected')
        self.assertEqual(new['value'], '上海')
        repeated = self.memory._upsert(a, first)
        self.assertEqual(repeated['status'], 'corrected')
        self.assertEqual(len(self.memory.state['memories']), 2)

    async def test_stable_timeline_branches_separate_accounts_and_survive_reload(self):
        a = (await self.store('我喜欢咖啡', key='one'))[0]
        b = (await self.store('我喜欢绿茶', key='two'))[0]
        branch = 'viewer:' + viewer_identity(self.event('', key='one'))
        self.assertEqual(self.memory.state['branch_heads'][branch], a['timeline_event_id'])
        self.assertNotIn(branch, self.memory.state['timeline_events'][-1]['branches'])
        await self.memory.close()
        self.memory = self.open_memory()
        self.assertEqual(self.memory.state['branch_heads'][branch], a['timeline_event_id'])
        self.assertNotEqual(a['timeline_event_id'], b['timeline_event_id'])

    async def test_legacy_expired_event_without_evidence_ids_cannot_be_revived(self):
        event = self.event('我今天不喜欢咖啡')
        record = (await self.memory.consolidate(event, '', '平静'))[0]
        record.pop('evidence_ids')
        record['slot'] = 'viewer:小明:preference:legacy:temporary'
        self.now += 86401
        await self.memory.close()
        self.memory = self.open_memory()
        await self.memory.consolidate(event, '', '平静')
        self.assertEqual(len(self.memory.state['memories']), 1)
        self.assertEqual(self.memory.state['memories'][0]['status'], 'expired')

    async def test_snapshot_drops_forgotten_edited_and_expired_text(self):
        record = (await self.store('我喜欢咖啡'))[0]
        await self.memory.context_for(self.event('咖啡'))
        self.assertTrue(self.memory.recall_snapshot()['selected'])
        await self.memory.edit_memory(record['id'], '观众小明喜欢茶')
        self.assertNotIn('咖啡', str(self.memory.recall_snapshot()))
        self.now += 1801
        await self.memory.context_for(self.event('茶'))
        active = next(r for r in self.memory.state['memories'] if r['status'] == 'active')
        await self.memory.forget(active['id'])
        self.assertEqual(self.memory.recall_snapshot()['selected'], [])
        self.now += 1
        await self.store('我今天喜欢草莓')
        await self.memory.context_for(self.event('草莓'))
        self.assertTrue(self.memory.recall_snapshot()['selected'])
        self.now += 86401
        self.assertEqual(self.memory.recall_snapshot()['selected'], [])


if __name__ == '__main__':
    unittest.main()
