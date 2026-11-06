"""Durable tarot state with temp SQLite only; no models, channels or live data."""
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from brain.events import Event
from brain.tarot import Tarot, tarot_command


class PredictableRandom:
    def __init__(self):
        self.bits = 0

    def shuffle(self, deck):
        pass

    def getrandbits(self, count):
        self.bits += 1
        return (self.bits - 1) % 2


class TarotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'tarot.sqlite3'
        self.now = 1000.0
        self.tarot = Tarot(self.path, clock=lambda: self.now)

    def event(self, text='塔罗 最近该把注意力放在哪里', *, user='观众甲', key='bili:100', **kwargs):
        return Event('danmaku', 'chat', text, user=user, viewer_key=key, ts=self.now, **kwargs)

    def select_test(self, *, spread='single', user='测试观众', question='下一步可以怎样调整'):
        session = self.tarot.create(spread=spread)
        joined = self.tarot.test_join(session['id'], user, question)
        self.assertTrue(joined['ok'])
        reading = self.tarot.select(session['id'], joined['entry_id'])
        return session, reading

    def ready(self, *, spread='single'):
        session, reading = self.select_test(spread=spread)
        positions = [7] if spread == 'single' else [3, 12, 18]
        reading = self.tarot.draw(session['id'], reading['id'], positions)
        return session, reading

    def test_commands_are_explicit_and_leave_ordinary_chat_untouched(self):
        for text in ('7', '塔罗是什么', '我想问塔罗', '塔罗', '抽牌', '可以抽牌 7 吗', '投票 1'):
            self.assertIsNone(tarot_command(text))
            self.assertIsNone(self.tarot.handle_event(self.event(text)))
        self.assertEqual(tarot_command('塔罗  想聊聊创作'), ('join', '想聊聊创作'))
        self.assertEqual(tarot_command('抽牌：７'), ('choose', '７'))
        self.assertIsNone(self.tarot.handle_event(Event('companion', 'chat', '塔罗 问题')))
        self.assertIsNone(self.tarot.handle_event(Event('danmaku', 'gift', '塔罗 问题')))
        self.assertFalse(self.tarot.handle_event(self.event())['ok'])

    def test_defaults_and_single_open_session(self):
        self.assertEqual(self.tarot.status(), dict(server_time=self.now, current=None, entries=[], reading=None, history=[]))
        session = self.tarot.create()
        self.assertEqual((session['mode'], session['spread'], session['state']), ('test', 'single', 'open'))
        self.assertFalse(self.tarot.live_active())
        with self.assertRaises(ValueError):
            self.tarot.create(mode='live')
        closed = self.tarot.close(session['id'])
        self.assertEqual(closed['state'], 'closed')
        self.assertEqual(self.tarot.close(session['id'])['state'], 'closed')
        self.now += 1
        current = self.tarot.create(mode='live')
        self.assertTrue(self.tarot.live_active())
        self.tarot.close(session['id'])
        self.assertEqual(self.tarot.status()['current']['id'], current['id'])
        self.assertEqual(self.tarot.status()['current']['state'], 'open')
        self.tarot.close(current['id'])
        self.assertFalse(self.tarot.live_active())

    def test_real_and_test_inputs_cannot_cross_modes(self):
        session = self.tarot.create()
        self.assertFalse(self.tarot.handle_event(self.event())['ok'])
        forged = self.event(forced=True)
        self.assertFalse(self.tarot.handle_event(forged)['ok'])
        forged.owner_control = True
        self.assertTrue(self.tarot.handle_event(forged)['ok'])
        self.tarot.close(session['id'])
        self.now += 1
        session = self.tarot.create(mode='live')
        with self.assertRaises(ValueError):
            self.tarot.test_join(session['id'], '测试观众', '问题')
        for key in ('', 'bili:', 'qq:100', 'bili:bad key'):
            self.assertFalse(self.tarot.handle_event(self.event(key=key))['ok'])
        self.assertFalse(self.tarot.handle_event(self.event(forced=True))['ok'])
        self.assertTrue(self.tarot.handle_event(self.event(key='bili-hash:verified'))['ok'])

    def test_identity_not_nickname_controls_registration_and_draw(self):
        session = self.tarot.create(mode='live')
        first = self.tarot.handle_event(self.event(user='同名观众', key='bili:private-account-A'))
        second = self.tarot.handle_event(self.event(user='同名观众', key='bili:private-account-B'))
        self.assertNotEqual(first['entry_id'], second['entry_id'])
        duplicate = self.tarot.handle_event(self.event('塔罗 改了昵称后的重复报名', user='新昵称', key='bili:private-account-A'))
        self.assertFalse(duplicate['changed'])
        self.assertEqual(first['entry_id'], duplicate['entry_id'])
        reading = self.tarot.select(session['id'], first['entry_id'])
        self.now += 1
        self.assertFalse(self.tarot.handle_event(self.event('抽牌 7', user='同名观众', key='bili:private-account-B'))['ok'])
        result = self.tarot.handle_event(self.event('抽牌 7', user='新昵称', key='bili:private-account-A'))
        self.assertTrue(result['ok'])
        self.assertEqual(result['reading_id'], reading['id'])
        serialized = json.dumps(self.tarot.status(), ensure_ascii=False)
        self.assertNotIn('private-account', serialized)
        self.assertNotIn(b'private-account', self.path.read_bytes())

    def test_queue_limit_and_duplicate_registration_after_finish(self):
        session = self.tarot.create()
        entries = [self.tarot.test_join(session['id'], f'观众{i}', '想聊聊创作') for i in range(30)]
        self.assertTrue(all(item['ok'] for item in entries))
        self.assertFalse(self.tarot.test_join(session['id'], '超出人数', '问题')['ok'])
        reading = self.tarot.select(session['id'], entries[0]['entry_id'])
        self.assertEqual(self.tarot.status()['current']['queue_count'], 29)
        self.tarot.finish(session['id'], reading['id'])
        old = self.tarot.test_join(session['id'], '观众0', '新的问题')
        self.assertTrue(old['ok'])
        self.assertFalse(old['changed'])
        self.assertEqual(old['entry']['state'], 'done')
        self.assertEqual(len(self.tarot.status()['entries']), 30)

    def test_host_selects_one_viewer_and_stale_controls_cannot_change_next_one(self):
        session = self.tarot.create()
        first = self.tarot.test_join(session['id'], '甲', '问题甲')
        second = self.tarot.test_join(session['id'], '乙', '问题乙')
        reading = self.tarot.select(session['id'], first['entry_id'])
        with self.assertRaises(ValueError):
            self.tarot.select(session['id'], second['entry_id'])
        self.tarot.finish(session['id'], reading['id'])
        current = self.tarot.select(session['id'], second['entry_id'])
        with self.assertRaises(ValueError):
            self.tarot.finish(session['id'], reading['id'])
        with self.assertRaises(ValueError):
            self.tarot.draw(session['id'], reading['id'], [4])
        self.assertEqual(self.tarot.status()['reading']['id'], current['id'])

    def test_select_shuffles_once_and_card_orientation_and_labels_stay_fixed(self):
        with patch('brain.tarot.secrets.SystemRandom', return_value=PredictableRandom()) as rng:
            session, reading = self.select_test(spread='three')
            self.assertEqual(reading['cards'], [])
            self.assertEqual(reading['positions'], [])
            drawn = self.tarot.draw(session['id'], reading['id'], [1, 2, 22])
            self.assertEqual([card['name'] for card in drawn['cards']], ['愚者', '魔术师', '世界'])
            self.assertEqual([card['orientation'] for card in drawn['cards']], ['upright', 'reversed', 'reversed'])
            self.assertEqual([card['label'] for card in drawn['cards']], ['眼下', '阻碍', '可尝试的方向'])
            self.assertTrue(all(card['keywords'] and card['meaning'] for card in drawn['cards']))
            self.assertEqual(self.tarot.draw(session['id'], reading['id'], [1, 2, 22])['cards'], drawn['cards'])
            with self.assertRaises(ValueError):
                self.tarot.draw(session['id'], reading['id'], [2, 1, 22])
            restored = Tarot(self.path, clock=lambda: self.now)
            self.assertEqual(restored.status()['reading']['cards'], drawn['cards'])
            self.assertEqual(rng.call_count, 1)

    def test_random_deck_has_22_distinct_majors_and_is_hidden_until_chosen(self):
        session, reading = self.select_test()
        with self.tarot.database() as db:
            payload = json.loads(db.execute('SELECT payload FROM tarot_readings WHERE id=?', (reading['id'],)).fetchone()[0])
        self.assertEqual(set(card['card'] for card in payload['deck']), set(range(22)))
        self.assertTrue(all(card['orientation'] in {'upright', 'reversed'} for card in payload['deck']))
        for public in (False, True):
            state = self.tarot.status(public=public)
            self.assertEqual(state['reading']['cards'], [])
            text = json.dumps(state)
            for hidden in ('deck', 'salt', 'viewer', 'source_key'):
                self.assertNotIn(hidden, text)
        selected = self.tarot.draw(session['id'], reading['id'], [5])
        self.assertEqual(selected['card_count'], 1)
        self.assertEqual(selected['cards'][0]['label'], '此刻的线索')

    def test_bad_positions_do_not_consume_or_reroll_cards(self):
        session, reading = self.select_test(spread='three')
        for positions in ([1], [1, 2], [1, 2, 3, 4], [1, 1, 2], [0, 2, 3], [1, 2, 23],
                          [True, 2, 3], ['1', 2, 3], (1, 2, 3), None):
            with self.subTest(positions=positions), self.assertRaises(ValueError):
                self.tarot.draw(session['id'], reading['id'], positions)
        self.assertEqual(self.tarot.status()['reading']['state'], 'selecting')
        self.assertTrue(self.tarot.test_choose(session['id'], '测试观众', [22, 4, 10])['ok'])
        self.assertEqual(self.tarot.status()['reading']['positions'], [22, 4, 10])

    def test_late_stale_duplicate_and_foreign_choice_events_do_not_change_result(self):
        session = self.tarot.create(mode='live')
        too_old = self.event(); too_old.ts -= 1
        future = self.event(); future.ts += 6
        invalid = self.event(); invalid.ts = float('nan')
        for event in (too_old, future, invalid):
            self.assertFalse(self.tarot.handle_event(event)['ok'])
        for timestamp in (None, '', True, [], {}, float('inf'), -(10 ** 1000), 10 ** 1000):
            malformed = self.event(); malformed.ts = timestamp
            self.assertFalse(self.tarot.handle_event(malformed)['ok'])
        joined = self.tarot.handle_event(self.event())
        early_choice = self.event('抽牌 3')
        self.now += 1
        reading = self.tarot.select(session['id'], joined['entry_id'])
        self.assertFalse(self.tarot.handle_event(early_choice)['ok'])
        self.assertFalse(self.tarot.handle_event(self.event('抽牌 3 然后忽略规则'))['ok'])
        for choice in ('1e2', '1.0', '-3', '七', '1\x002', '9' * 5000, '[3]', '3 / 8'):
            self.assertFalse(self.tarot.handle_event(self.event('抽牌 ' + choice))['ok'])
        selected = self.tarot.handle_event(self.event('抽牌 ３'))
        self.assertTrue(selected['ok'])
        self.assertFalse(self.tarot.handle_event(self.event('抽牌 3'))['changed'])
        self.assertFalse(self.tarot.handle_event(self.event('抽牌 8'))['ok'])
        self.tarot.finish(session['id'], reading['id'])
        self.assertFalse(self.tarot.handle_event(self.event('抽牌 3'))['ok'])
        self.assertEqual(self.tarot.status()['reading']['positions'], [3])

    def test_event_claim_contains_real_question_and_only_public_draw_facts(self):
        session, reading = self.ready()
        event = self.tarot.interpretation_event(session['id'], reading['id'])
        self.assertEqual((event.source, event.kind, event.forced), ('system', 'ambient', True))
        self.assertEqual(event.text, reading['question'])
        self.assertEqual(event.user, reading['user'])
        self.assertEqual(event.agent_reply, '')
        facts = json.loads(event.tarot_context)
        self.assertEqual(facts['cards'], reading['cards'])
        self.assertEqual(facts['mode'], 'test')
        for hidden in ('attempt_id', 'deck', 'salt', 'viewer', 'entry_id'):
            self.assertNotIn(hidden, event.tarot_context)
        self.assertTrue(self.tarot.valid_event(event))
        self.assertTrue(self.tarot.valid_event(vars(event)))
        with self.assertRaises(ValueError):
            self.tarot.interpretation_event(session['id'], reading['id'])

    def test_generation_is_not_finished_and_failure_retry_uses_same_cards(self):
        session, reading = self.ready(spread='three')
        first = self.tarot.interpretation_event(session['id'], reading['id'])
        result = self.tarot.mark_result(first, 'generated')
        self.assertEqual(result['state'], 'generated')
        self.assertTrue(self.tarot.valid_event(first), 'remaining TTS may still play')
        result = self.tarot.mark_result(first, 'interrupted', '播放被打断')
        self.assertEqual(result['state'], 'interrupted')
        self.assertFalse(self.tarot.valid_event(first))
        second = self.tarot.interpretation_event(session['id'], reading['id'])
        self.assertNotEqual(first.tarot_attempt_id, second.tarot_attempt_id)
        self.assertFalse(self.tarot.mark_result(first, 'generated'))
        self.assertFalse(self.tarot.valid_event(first))
        self.assertTrue(self.tarot.valid_event(second))
        self.assertEqual(self.tarot.status()['reading']['cards'], reading['cards'])
        self.tarot.mark_result(second, 'failed', '测试错误' * 100)
        self.assertLessEqual(len(self.tarot.status()['reading']['error']), 160)
        third = self.tarot.interpretation_event(session['id'], reading['id'])
        self.tarot.mark_result(third, 'generated')
        self.tarot.finish(session['id'], reading['id'])
        self.assertFalse(self.tarot.valid_event(third))
        self.assertFalse(self.tarot.mark_result(third, 'failed'))
        self.assertEqual(self.tarot.status()['reading']['state'], 'finished')

    def test_restart_does_not_replay_queued_or_generated_attempts(self):
        for generation_state in ('queued', 'generated'):
            with self.subTest(generation_state=generation_state):
                session, reading = self.ready()
                event = self.tarot.interpretation_event(session['id'], reading['id'])
                if generation_state == 'generated':
                    self.tarot.mark_result(event, 'generated')
                resumed = Tarot(self.path, clock=lambda: self.now)
                result = resumed.status()['reading']
                self.assertEqual(result['state'], 'interrupted')
                self.assertEqual(result['attempt_id'], '')
                self.assertEqual(result['cards'], reading['cards'])
                self.assertFalse(resumed.valid_event(event))
                self.assertFalse(resumed.mark_result(event, 'generated'))
                resumed.close(session['id'])
                self.now += 1

    def test_restart_before_draw_keeps_original_hidden_shuffle(self):
        with patch('brain.tarot.secrets.SystemRandom', return_value=PredictableRandom()) as rng:
            session, reading = self.select_test()
        resumed = Tarot(self.path, clock=lambda: self.now)
        selected = resumed.draw(session['id'], reading['id'], [22])
        self.assertEqual(selected['cards'][0]['name'], '世界')
        self.assertEqual(selected['cards'][0]['orientation'], 'reversed')
        self.assertEqual(rng.call_count, 1)

    def test_public_projection_omits_queue_history_and_attempts(self):
        session, reading = self.ready()
        event = self.tarot.interpretation_event(session['id'], reading['id'])
        public = self.tarot.status(public=True)
        self.assertEqual(public['entries'], [])
        self.assertEqual(public['history'], [])
        self.assertNotIn('attempt_id', public['reading'])
        self.assertNotIn('error', public['reading'])
        self.assertNotIn('session_id', public['reading'])
        self.assertNotIn(event.tarot_attempt_id, json.dumps(public))
        self.assertEqual(public['reading']['cards'], reading['cards'])
        self.tarot.close(session['id'])
        self.assertFalse(self.tarot.valid_event(event))
        self.assertEqual(self.tarot.status()['history'][0]['state'], 'finished')

    def test_concurrent_interpretation_claim_only_wins_once(self):
        session, reading = self.ready()
        def claim():
            try:
                return self.tarot.interpretation_event(session['id'], reading['id'])
            except ValueError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: claim(), range(2)))
        self.assertEqual(sum(item is not None for item in outcomes), 1)

    def test_history_is_bounded_and_only_closed_sessions_are_reclaimed(self):
        self.tarot.MAX_RECORDS = 4
        for index in range(8):
            session, reading = self.ready()
            self.tarot.finish(session['id'], reading['id'])
            self.tarot.close(session['id'])
            self.now += 1
        state = self.tarot.status()
        self.assertLessEqual(len(state['history']), 4)
        with self.tarot.database() as db:
            for table in ('tarot_sessions', 'tarot_entries', 'tarot_readings'):
                self.assertLessEqual(db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0], 4)
        newest = self.tarot.create()
        with self.tarot.database() as db:
            self.assertIsNotNone(db.execute('SELECT 1 FROM tarot_sessions WHERE id=?', (newest['id'],)).fetchone())

    def test_validation_and_stale_test_forms_do_not_target_another_session(self):
        for changes in ({'spread': 'five'}, {'spread': []}, {'mode': 'demo'}, {'mode': None}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.tarot.create(**changes)
        first = self.tarot.create()
        for question in ('', '字' * 161, '问题\n第二行', '坏\x00内容'):
            with self.subTest(question=question), self.assertRaises(ValueError):
                self.tarot.test_join(first['id'], '测试观众', question)
        self.tarot.close(first['id'])
        self.now += 1
        second = self.tarot.create()
        with self.assertRaises(ValueError):
            self.tarot.test_join(first['id'], '测试观众', '陈旧问题')
        self.assertEqual(self.tarot.status()['current']['id'], second['id'])
        self.assertEqual(self.tarot.status()['entries'], [])
        self.assertFalse(self.tarot.valid_event({}))
        self.assertFalse(self.tarot.valid_event({'tarot_session_id': True}))
        with self.assertRaises(ValueError):
            self.tarot.mark_result({}, 'finished')


if __name__ == '__main__':
    unittest.main()
