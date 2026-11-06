"""Guided tea-table lifecycle, with temporary storage and simulated receipts."""
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from brain.events import Event
from brain.tarot import Tarot


class GuidedTarotTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / 'tarot.sqlite3'
        self.now = 1000.0
        self.tarot = Tarot(self.path, clock=lambda: self.now)

    def event(self, text, key='bili:100', user='来客'):
        return Event('danmaku', 'chat', text, user=user, viewer_key=key, ts=self.now)

    def start(self, spread='single', mode='live'):
        self.session = self.tarot.create(hosting='guided', spread=spread, mode=mode)
        if mode == 'live':
            joined = self.tarot.handle_event(self.event('塔罗 怎样开始一张新的画'))
        else:
            joined = self.tarot.test_join(self.session['id'], '来客', '怎样开始一张新的画')
        self.reading = self.tarot.select(self.session['id'], joined['entry_id'])
        return self.tarot.next_event()

    def current(self):
        return self.tarot.status()['reading']

    def reply(self, text):
        if self.session['mode'] == 'live':
            return self.tarot.handle_event(self.event(text))
        return self.tarot.test_reply(self.session['id'], '来客', text)

    def delivered(self, event, text='可以先从一点具体的事开始。', segments=1):
        self.tarot.mark_generated(event, text, segments)
        for index in range(segments):
            self.tarot.note_playback_started(event, text, index)
            self.tarot.played(event, index, True)

    def ready(self, spread='single'):
        welcome = self.start(spread)
        self.delivered(welcome, '你想聊的是如何开始一张新的画，对吗？')
        self.reply('对')
        self.tarot.draw(self.session['id'], self.reading['id'], [7] if spread == 'single' else [3, 12, 18])
        return self.tarot.next_event()

    def test_complete_conversation_preserves_question_cards_and_scoped_context(self):
        welcome = self.start()
        self.assertEqual(welcome.tarot_stage, 'welcome')
        facts = json.loads(welcome.tarot_context)
        self.assertEqual(facts['hosting'], 'guided')
        self.assertEqual(facts['cards'], [])
        self.assertIsNone(self.tarot.next_event())
        self.delivered(welcome, '你想知道怎么开始一张新的画，对吗？')
        self.assertEqual(self.current()['state'], 'awaiting_confirmation')
        self.assertTrue(self.reply('我其实是不敢画人物')['ok'])
        self.assertEqual(self.current()['question'], '怎样开始一张新的画')
        self.assertEqual(self.current()['clarification'], '我其实是不敢画人物')
        self.assertEqual(self.current()['state'], 'selecting')
        self.assertTrue(self.reply('7')['ok'])
        draw = self.current()['cards']
        reading = self.tarot.next_event()
        facts = json.loads(reading.tarot_context)
        self.assertEqual(facts['clarification'], '我其实是不敢画人物')
        self.delivered(reading, '先画一张不公开的草稿。你更担心画坏还是给别人看到？')
        self.assertEqual(self.current()['state'], 'awaiting_reply')
        self.assertTrue(self.reply('我更担心画坏')['ok'])
        closing = self.tarot.next_event()
        facts = json.loads(closing.tarot_context)
        self.assertEqual(facts['response'], '我更担心画坏')
        self.assertIn('先画一张', facts['prior_reading'])
        self.assertEqual(facts['cards'], draw)
        self.tarot.mark_generated(closing, '那今晚就允许它只是一张草稿。我们先聊到这里。', 1)
        self.assertEqual(self.tarot.status(public=True)['reading']['farewell'], '')
        self.delivered(closing, '那今晚就允许它只是一张草稿。我们先聊到这里。')
        self.assertEqual(self.current()['state'], 'finished')
        self.assertIn('先聊到这里', self.tarot.status(public=True)['reading']['farewell'])
        self.assertEqual(self.current()['cards'], draw)
        self.assertFalse(self.tarot.valid_event(closing))
        self.assertIsNone(self.tarot.next_event())
        self.assertFalse(self.tarot.holds_chat())

    def test_generation_alone_never_starts_response_timer(self):
        welcome = self.start()
        self.tarot.mark_generated(welcome, '请确认问题。', 3)
        self.now += 600
        self.assertIsNone(self.current()['wait_deadline'])
        self.assertEqual(self.current()['state'], 'generated')
        self.assertIsNone(self.tarot.next_event())
        for index in (2, 0, 2):
            self.tarot.played(welcome, index, True)
        self.assertEqual(self.current()['state'], 'generated')
        self.tarot.played(welcome, 1, True)
        self.assertEqual(self.current()['state'], 'awaiting_confirmation')
        self.assertEqual(self.current()['wait_deadline'], self.now + 60)

    def test_out_of_order_playback_before_generation_is_counted_once(self):
        welcome = self.start()
        self.tarot.played(welcome, 1, True)
        self.tarot.played(welcome, 0, True)
        self.assertEqual(self.current()['state'], 'queued')
        self.tarot.mark_generated(welcome, '请确认问题。', 2)
        deadline = self.current()['wait_deadline']
        self.assertEqual(self.current()['state'], 'awaiting_confirmation')
        self.now += 5
        self.assertFalse(self.tarot.played(welcome, 0, True))
        self.assertEqual(self.current()['wait_deadline'], deadline)

    def test_foreign_viewer_and_stale_messages_do_not_steal_window(self):
        welcome = self.start()
        old = self.event('对')
        self.now += 2
        self.delivered(welcome)
        self.assertIsNone(self.tarot.handle_event(self.event('我来回答', key='bili:other', user='来客')))
        self.assertFalse(self.tarot.handle_event(old)['ok'])
        self.assertEqual(self.current()['state'], 'awaiting_confirmation')
        changed_name = self.event('对', user='改名后的来客')
        self.assertTrue(self.tarot.handle_event(changed_name)['ok'])
        self.assertEqual(self.current()['state'], 'selecting')

    def test_duplicate_viewer_replies_are_not_generic_chat_or_extra_turns(self):
        reading = self.ready()
        self.assertFalse(self.reply('抢先发一个回答')['ok'])
        self.delivered(reading)
        self.assertTrue(self.reply('我担心画坏')['ok'])
        self.assertFalse(self.reply('这是第二个答案')['ok'])
        self.assertEqual(self.current()['response'], '我担心画坏')
        closing = self.tarot.next_event()
        self.assertIsNone(self.tarot.next_event())
        self.assertEqual(closing.tarot_stage, 'closing')

    def test_all_three_wait_windows_expire_to_one_closing_without_reroll(self):
        for window in ('awaiting_confirmation', 'selecting', 'awaiting_reply'):
            with self.subTest(window=window):
                welcome = self.start()
                self.delivered(welcome)
                if window in {'selecting', 'awaiting_reply'}:
                    self.reply('对')
                if window == 'awaiting_reply':
                    self.reply('抽牌 4')
                    self.delivered(self.tarot.next_event())
                cards = self.current()['cards']
                self.now = self.current()['wait_deadline'] + 1
                # GET stays inert even if the deadline has passed.
                self.assertEqual(self.current()['state'], window)
                close = self.tarot.next_event()
                self.assertEqual(close.tarot_stage, 'closing')
                self.assertTrue(json.loads(close.tarot_context)['reason'].endswith('_timeout'))
                self.assertIsNone(self.tarot.next_event())
                self.delivered(close)
                self.assertEqual(self.current()['cards'], cards)
                self.tarot.close(self.session['id'])
                self.now += 1

    def test_late_reply_after_deadline_cannot_reopen_window(self):
        reading = self.ready()
        self.delivered(reading)
        self.now = self.current()['wait_deadline']
        self.assertFalse(self.reply('迟到的回应')['ok'])
        self.assertEqual(self.current()['state'], 'closing')
        self.assertEqual(self.current()['response'], '')

    def test_paused_window_preserves_remaining_time_and_drops_old_messages(self):
        welcome = self.start()
        self.delivered(welcome)
        self.now += 10
        old = self.event('对')
        self.assertTrue(self.tarot.pause_current())
        self.assertFalse(self.tarot.pause_current())
        self.assertEqual(self.current()['state'], 'paused')
        self.now += 200
        self.assertIsNone(self.tarot.next_event())
        self.assertFalse(self.reply('对')['ok'])
        self.tarot.resume(self.session['id'], self.reading['id'])
        self.assertEqual(self.current()['wait_deadline'], self.now + 50)
        self.assertFalse(self.tarot.handle_event(old)['ok'])
        self.assertTrue(self.reply('对')['ok'])

    def test_pause_in_flight_invalidates_old_attempt_and_resumes_same_stage(self):
        event = self.ready()
        cards = self.current()['cards']
        self.tarot.pause(self.session['id'], self.reading['id'])
        self.assertFalse(self.tarot.valid_event(event))
        self.assertFalse(self.tarot.mark_generated(event, '晚到的结果', 1))
        self.tarot.resume(self.session['id'], self.reading['id'])
        retry = self.tarot.next_event()
        self.assertNotEqual(retry.tarot_attempt_id, event.tarot_attempt_id)
        self.assertEqual(self.current()['cards'], cards)
        self.assertFalse(self.tarot.played(event, 0, True))

    def test_takeover_stops_automatic_hosting_and_preserves_draw(self):
        event = self.ready()
        cards = self.current()['cards']
        self.tarot.takeover(self.session['id'], self.reading['id'])
        self.assertFalse(self.tarot.valid_event(event))
        self.assertFalse(self.tarot.holds_chat())
        self.assertEqual(self.current()['hosting'], 'manual')
        self.assertEqual(self.current()['state'], 'revealed')
        self.assertEqual(self.current()['cards'], cards)
        self.assertIsNone(self.tarot.next_event())
        manual = self.tarot.interpretation_event(self.session['id'], self.reading['id'])
        self.assertTrue(self.tarot.valid_event(manual))

    def test_text_only_needs_host_confirmation_and_cannot_accept_audio_later(self):
        welcome = self.start()
        self.tarot.mark_generated(welcome, '你想聊的是画画，对吗？', 0, text_only=True)
        self.assertEqual(self.current()['state'], 'awaiting_host')
        self.assertIsNone(self.current()['wait_deadline'])
        self.assertFalse(self.tarot.played(welcome, 0, True))
        self.assertIsNone(self.tarot.next_event())
        self.tarot.confirm_delivered(self.session['id'], self.reading['id'])
        self.assertEqual(self.current()['state'], 'awaiting_confirmation')
        with self.assertRaises(ValueError):
            self.tarot.confirm_delivered(self.session['id'], self.reading['id'])

    def test_failure_stops_auto_retry_and_manual_retry_retains_stage(self):
        welcome = self.start()
        self.tarot.played(welcome, 0, False)
        self.assertEqual(self.current()['state'], 'interrupted')
        self.assertIsNone(self.tarot.next_event())
        retry = self.tarot.interpretation_event(self.session['id'], self.reading['id'])
        self.assertEqual(retry.tarot_stage, 'welcome')
        self.assertNotEqual(retry.tarot_attempt_id, welcome.tarot_attempt_id)
        self.tarot.mark_result(retry, 'failed', '模拟失败')
        self.assertIsNone(self.tarot.next_event())

    def test_restart_pauses_ready_waiting_and_in_flight_rounds(self):
        for phase in ('ready', 'in_flight', 'waiting', 'text'):
            with self.subTest(phase=phase):
                event = self.start()
                if phase == 'ready':
                    self.delivered(event)
                    self.reply('对')
                    self.reply('抽牌 7')
                elif phase == 'waiting':
                    self.delivered(event)
                elif phase == 'text':
                    self.tarot.mark_generated(event, '确认问题', 0, True)
                resumed = Tarot(self.path, clock=lambda: self.now)
                state = resumed.status()['reading']
                self.assertTrue(state['paused'])
                self.assertEqual(state['state'], 'paused')
                self.assertIsNone(resumed.next_event())
                self.assertFalse(resumed.valid_event(event))
                resumed.close(self.session['id'])
                self.now += 1

    def test_public_display_only_reveals_cards_when_audio_begins(self):
        event = self.ready(spread='three')
        cards = self.current()['cards']
        self.assertEqual(self.tarot.status(public=True)['reading']['cards'], [])
        self.tarot.note_playback_started(event, '先看第一张。', 0)
        self.assertEqual(self.current()['revealed_count'], 1)
        self.tarot.note_playback_started(event, cards[1]['name'] + '指向一种调整。', 1)
        self.assertEqual(self.current()['revealed_count'], 2)
        public = self.tarot.status(public=True)['reading']
        self.assertEqual(len(public['cards']), 2)
        self.assertEqual(public['card_count'], 3)
        self.tarot.mark_generated(event, '整段原文只给主控', 3)
        for key in ('display_text', 'prior_reading', 'response', 'clarification', 'attempt_id', 'deck', 'viewer'):
            self.assertNotIn(key, self.tarot.status(public=True)['reading'])

    def test_safe_question_and_reply_filters_apply_before_persisting(self):
        self.session = self.tarot.create(hosting='guided', mode='live')
        self.assertFalse(self.tarot.handle_event(self.event('塔罗 聊聊政治'))['ok'])
        self.assertEqual(self.tarot.status()['entries'], [])
        joined = self.tarot.handle_event(self.event('塔罗 怎样继续画画'))
        self.reading = self.tarot.select(self.session['id'], joined['entry_id'])
        self.delivered(self.tarot.next_event())
        self.assertFalse(self.reply('讨论政治')['ok'])
        self.assertEqual(self.current()['clarification'], '')
        self.assertNotIn('讨论政治', self.path.read_bytes().decode('utf-8', errors='ignore'))

    def test_crisis_input_ends_current_round_and_returns_to_existing_support(self):
        self.start()
        self.assertIsNone(self.reply('我现在想伤害自己'))
        self.assertEqual(self.current()['state'], 'finished')
        self.assertFalse(self.tarot.holds_chat())

    def test_current_viewer_can_end_without_waiting_for_generation(self):
        event = self.start()
        self.assertTrue(self.reply('结束')['ok'])
        self.assertEqual(self.current()['state'], 'finished')
        self.assertFalse(self.tarot.valid_event(event))
        self.assertIsNone(self.tarot.next_event())

    def test_concurrent_scheduler_claim_only_produces_one_event(self):
        event = self.start()
        self.delivered(event)
        self.reply('对')
        self.reply('抽牌 7')
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(lambda _: self.tarot.next_event(), range(2)))
        self.assertEqual(sum(item is not None for item in claims), 1)

    def test_test_reply_requires_test_mode_and_current_session(self):
        event = self.start(mode='test')
        self.delivered(event)
        self.assertTrue(self.reply('对')['ok'])
        self.tarot.close(self.session['id'])
        live = self.tarot.create(hosting='guided', mode='live')
        with self.assertRaises(ValueError):
            self.tarot.test_reply(live['id'], '来客', '对')
        with self.assertRaises(ValueError):
            self.tarot.test_reply(self.session['id'], '来客', '对')

    def test_invalid_times_or_segment_indices_cannot_advance(self):
        event = self.start()
        for index in (True, -1, 64, '0', None):
            self.assertFalse(self.tarot.played(event, index, True))
        self.delivered(event)
        for value in (True, None, float('nan'), float('inf'), 10 ** 1000, -1, 'now'):
            malformed = self.event('对')
            malformed.ts = value
            self.assertFalse(self.tarot.handle_event(malformed)['ok'])
        self.assertEqual(self.current()['state'], 'awaiting_confirmation')


if __name__ == '__main__':
    unittest.main()
