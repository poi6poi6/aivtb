from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from brain.config import WebUIConfig
from brain.events import Event
from brain.question_cards import QuestionCards
from brain.webui import WebUI


class QuestionCardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'questions.db'
        self.now = 1000.0
        self.cards = QuestionCards(self.path, clock=lambda: self.now)

    def event(self, key='bili:123', user='同名', ui_id=1):
        event = Event('danmaku', 'chat', '今天如何照顾这盆花？', user=user, viewer_key=key)
        event.ui_id = ui_id
        return event

    def test_unselected_events_are_volatile_and_pins_use_trusted_original_identity(self):
        first = self.event()
        token = self.cards.remember_event(first)['question_pin_token']
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM question_cards').fetchone()[0], 0)
        self.assertNotIn(first.text.encode(), self.path.read_bytes())
        card = self.cards.pin(token)
        self.assertEqual(card['origin']['viewer_key'], 'bili:123')
        self.assertEqual(self.cards.pin(token)['id'], card['id'])
        renamed = self.cards.remember_event(self.event(user='改名'))['question_pin_token']
        self.assertEqual(renamed, token)
        other = self.cards.remember_event(self.event(key='bili:456'))['question_pin_token']
        self.assertNotEqual(self.cards.pin(other)['id'], card['id'])
        public = json.dumps(self.cards.status(), ensure_ascii=False)
        self.assertNotIn('viewer_key', public)
        self.assertNotIn('bili:123', public)
        self.assertNotIn('identity', public)

    def test_recent_cache_is_bounded_expires_and_does_not_reuse_ids_after_restart(self):
        token = self.cards.remember_event(self.event())['question_pin_token']
        self.now += 1801
        with self.assertRaises(ValueError):
            self.cards.pin(token)
        for index in range(600):
            self.cards.remember_event(self.event(ui_id=index + 1))
        self.assertEqual(len(self.cards.recent), 500)
        restarted = QuestionCards(self.path)
        with self.assertRaises(ValueError):
            restarted.pin(token)
        self.assertEqual(restarted.status()['cards'], [])

    def test_manual_idempotency_versions_limits_and_withdrawal(self):
        card = self.cards.manual('手填问题', 'a' * 32)
        self.assertEqual(self.cards.manual('手填问题', 'a' * 32)['id'], card['id'])
        with self.assertRaises(ValueError):
            self.cards.manual('其他问题', 'a' * 32)
        self.cards.MAX_ACTIVE = 1
        with self.assertRaises(ValueError):
            self.cards.manual('第二个问题', 'b' * 32)
        parked = self.cards.move(card['id'], card['version'], 'parked')
        with self.assertRaises(ValueError):
            self.cards.move(card['id'], card['version'], 'waiting')
        with self.assertRaises(ValueError):
            self.cards.move(card['id'], parked['version'], 'answered')
        withdrawn = self.cards.move(card['id'], parked['version'], 'withdrawn')
        self.cards.manual('第二个问题', 'b' * 32)
        with self.assertRaises(ValueError):
            self.cards.move(card['id'], withdrawn['version'], 'waiting')
        self.cards.delete(card['id'], withdrawn['version'])
        self.assertEqual(len(self.cards.status()['cards']), 1)
        for text in ('', 'x' * 1001, '非法\x00问题'):
            with self.assertRaises(ValueError):
                self.cards.manual(text, 'c' * 32)

    def test_only_matching_completed_playback_can_finish_and_restart_recovers(self):
        card = self.cards.manual('先保存', 'a' * 32)
        answering = self.cards.begin_answer(card['id'], card['version'])
        self.assertEqual(answering['state'], 'answering')
        with self.assertRaises(ValueError):
            self.cards.finish_answer(card['id'], answering['attempt'], completed=True)
        bound = self.cards.bind_turn(card['id'], answering['attempt'], 12)
        failed = self.cards.finish_answer(card['id'], bound['attempt'], reason='播放被中断')
        self.assertEqual(failed['state'], 'waiting')
        new = self.cards.begin_answer(card['id'], failed['version'])
        self.cards.bind_turn(card['id'], new['attempt'], 13)
        stale = self.cards.finish_answer(card['id'], bound['attempt'], completed=True)
        self.assertEqual(stale['state'], 'answering')
        done = self.cards.finish_answer(card['id'], new['attempt'], completed=True)
        self.assertEqual(done['state'], 'answered')
        self.assertEqual(done['answered_at'], self.now)
        again = self.cards.move(done['id'], done['version'], 'waiting')
        self.cards.begin_answer(again['id'], again['version'])
        restarted = QuestionCards(self.path)
        recovered = restarted.get(card['id'])
        self.assertEqual(recovered['state'], 'waiting')
        self.assertIn('未确认播完', recovered['reason'])

    def test_late_bind_does_not_resurrect_withdrawn_or_new_attempt(self):
        card = self.cards.manual('卡片', 'a' * 32)
        first = self.cards.begin_answer(card['id'], card['version'])
        failed = self.cards.finish_answer(card['id'], first['attempt'], reason='未调度')
        withdrawn = self.cards.move(card['id'], failed['version'], 'withdrawn')
        late = self.cards.bind_turn(card['id'], first['attempt'], 44)
        self.assertEqual(late['state'], 'withdrawn')
        self.assertIsNone(late['turn_id'])
        waiting = self.cards.move(card['id'], withdrawn['version'], 'waiting')
        second = self.cards.begin_answer(card['id'], waiting['version'])
        late = self.cards.bind_turn(card['id'], first['attempt'], 44)
        self.assertEqual(late['attempt'], second['attempt'])
        self.assertIsNone(late['turn_id'])
        self.cards.bind_turn(card['id'], second['attempt'], 45)
        with self.assertRaises(ValueError):
            self.cards.bind_turn(card['id'], second['attempt'], 46)

    def test_total_limit_only_allows_explicit_deletion_of_withdrawn_cards(self):
        self.cards.MAX_TOTAL = 1
        first = self.cards.manual('要保留', 'a' * 32)
        with self.assertRaises(ValueError):
            self.cards.delete(first['id'], first['version'])
        with self.assertRaises(ValueError):
            self.cards.manual('不能静默挤掉旧卡', 'b' * 32)
        withdrawn = self.cards.move(first['id'], first['version'], 'withdrawn')
        with self.assertRaises(ValueError):
            self.cards.delete(first['id'], first['version'])
        self.cards.delete(withdrawn['id'], withdrawn['version'])
        self.assertEqual(self.cards.manual('新卡', 'b' * 32)['state'], 'waiting')


class QuestionHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ui = WebUI(WebUIConfig(), asyncio.Queue(), self.temp.name, {})
        self.ui.question_cards = QuestionCards(Path(self.temp.name) / 'questions.db')
        self.asked, self.cancelled = [], []
        async def ask(card):
            self.asked.append(card)
            return 77
        async def cancel(card):
            self.cancelled.append(card)
        self.ui.on_question_ask, self.ui.on_question_cancel = ask, cancel
        app = web.Application()
        app.router.add_get('/questions', self.ui._questions_page)
        app.router.add_get('/questions-status', self.ui._questions_status)
        app.router.add_post('/questions-action', self.ui._questions_action)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.addAsyncCleanup(self.ui.close)

    async def action(self, **data):
        return await self.client.post('/questions-action', json=data,
            headers={'Origin': str(self.client.make_url('/')).rstrip('/')})

    async def test_pin_ignores_client_text_source_and_account_fields(self):
        event = Event('danmaku', 'chat', '真实问题', user='真实昵称', viewer_key='bili:real')
        event.ui_id = 2
        token = self.ui.question_event_payload(event)['question_pin_token']
        response = await self.action(action='pin', token=token, text='伪造问题',
                                     source='companion', user='伪造昵称', viewer_key='bili:fake')
        self.assertEqual(response.status, 200)
        card = await response.json()
        self.assertEqual(card['text'], '真实问题')
        self.assertEqual(card['origin']['source'], 'danmaku')
        self.assertNotIn('viewer_key', json.dumps(card))
        answer = await self.action(action='ask', id=card['id'], version=card['version'])
        queued = await answer.json()
        self.assertEqual(queued['state'], 'answering')
        self.assertEqual(queued['turn_id'], 77)
        self.assertEqual(self.asked[0]['origin']['viewer_key'], 'bili:real')
        self.assertIsNone(queued['answered_at'])
        self.assertEqual((await self.action(action='ask', id=card['id'], version=card['version'])).status, 400)
        parked = await (await self.action(action='move', id=queued['id'], version=queued['version'], state='parked')).json()
        self.assertEqual(parked['state'], 'parked')
        self.assertEqual(len(self.cancelled), 1)
        self.assertEqual(self.ui.question_cards.finish_answer(card['id'], self.asked[0]['attempt'], completed=True)['state'], 'parked')

    async def test_schedule_failure_returns_to_waiting_without_leaking_error(self):
        card = await (await self.action(action='manual', text='主播问题', request_key='f' * 32,
                                       source='danmaku', viewer_key='spoof')).json()
        self.assertEqual(card['origin']['kind'], 'manual')
        async def fail(card):
            raise RuntimeError('private backend error')
        self.ui.on_question_ask = fail
        response = await self.action(action='ask', id=card['id'], version=card['version'])
        self.assertEqual(response.status, 503)
        self.assertNotIn('private backend', await response.text())
        state = await (await self.client.get('/questions-status')).json()
        self.assertEqual(state['cards'][0]['state'], 'waiting')
        self.assertIsNone(state['cards'][0]['answered_at'])

    async def test_owner_only_routes_and_no_remote_completion_endpoint(self):
        self.assertEqual((await self.client.post('/questions-action', json={'action':'manual'})).status, 403)
        self.assertEqual((await self.client.get('/questions-status', headers={'Host':'evil.example'})).status, 403)
        self.assertEqual((await self.action(action='pin', token='a' * 32)).status, 400)
        self.assertEqual((await self.action(action='finish', completed=True)).status, 400)
        self.ui.cfg.public_test = True
        for route in ('/questions', '/questions-status'):
            self.assertEqual((await self.client.get(route)).status, 403)
        self.assertEqual((await self.action(action='manual', text='x', request_key='a' * 32)).status, 403)
        event = Event('companion', 'chat', '不能固定公众会话')
        event.ui_id = 1
        self.assertEqual(self.ui.question_event_payload(event), {})

    async def test_fast_completion_before_ask_callback_returns_stays_answered(self):
        card = await (await self.action(action='manual', text='快速回答', request_key='a' * 32)).json()
        async def fast(asked):
            self.ui.question_cards.bind_turn(asked['id'], asked['attempt'], 81)
            self.ui.question_cards.finish_answer(asked['id'], asked['attempt'], completed=True)
            return 81
        self.ui.on_question_ask = fast
        response = await self.action(action='ask', id=card['id'], version=card['version'])
        result = await response.json()
        self.assertEqual(result['state'], 'answered')
        self.assertEqual(result['turn_id'], 81)

    async def test_callback_failure_after_binding_requires_settlement_before_retry(self):
        card = await (await self.action(action='manual', text='回执可能丢失', request_key='a' * 32)).json()
        async def uncertain(asked):
            self.ui.question_cards.bind_turn(asked['id'], asked['attempt'], 82)
            raise RuntimeError('response lost')
        self.ui.on_question_ask = uncertain
        self.assertEqual((await self.action(action='ask', id=card['id'], version=card['version'])).status, 503)
        current = self.ui.question_cards.get(card['id'])
        self.assertEqual(current['state'], 'answering')
        self.assertIn('未确认', current['reason'])
        self.assertEqual((await self.action(action='ask', id=card['id'], version=current['version'])).status, 400)
        stopped = await (await self.action(action='move', id=current['id'], version=current['version'], state='waiting')).json()
        self.assertEqual(stopped['state'], 'waiting')
        self.assertEqual(len(self.cancelled), 1)

    async def test_parallel_clicks_only_dispatch_one_turn(self):
        card = await (await self.action(action='manual', text='只回答一次', request_key='a' * 32)).json()
        entered, release = asyncio.Event(), asyncio.Event()
        async def ask(asked):
            self.asked.append(asked)
            entered.set()
            await release.wait()
            return 83
        self.ui.on_question_ask = ask
        first = asyncio.create_task(self.action(action='ask', id=card['id'], version=card['version']))
        await asyncio.wait_for(entered.wait(), 1)
        second = asyncio.create_task(self.action(action='ask', id=card['id'], version=card['version']))
        release.set()
        results = await asyncio.gather(first, second)
        self.assertEqual(sorted(result.status for result in results), [200, 400])
        self.assertEqual(len(self.asked), 1)

    async def test_stale_move_and_failed_cancel_do_not_unlock_running_question(self):
        card = await (await self.action(action='manual', text='正在播放', request_key='a' * 32)).json()
        current = await (await self.action(action='ask', id=card['id'], version=card['version'])).json()
        self.assertEqual((await self.action(action='move', id=card['id'], version=card['version'], state='withdrawn')).status, 400)
        self.assertEqual(self.cancelled, [])
        async def failed_cancel(asked):
            raise RuntimeError('cancel not confirmed')
        self.ui.on_question_cancel = failed_cancel
        self.assertEqual((await self.action(action='move', id=current['id'], version=current['version'], state='withdrawn')).status, 503)
        self.assertEqual(self.ui.question_cards.get(card['id'])['state'], 'answering')
        self.assertEqual((await self.action(action='delete', id=current['id'], version=current['version'])).status, 400)

    async def test_completed_receipt_racing_with_cancel_does_not_reopen_withdrawn_card(self):
        card = await (await self.action(action='manual', text='结束与撤回相遇', request_key='a' * 32)).json()
        current = await (await self.action(action='ask', id=card['id'], version=card['version'])).json()
        async def finish_during_cancel(asked):
            self.ui.question_cards.finish_answer(asked['id'], asked['attempt'], completed=True)
        self.ui.on_question_cancel = finish_during_cancel
        response = await self.action(action='move', id=current['id'], version=current['version'], state='withdrawn')
        self.assertEqual((await response.json())['state'], 'withdrawn')
        old = self.asked[0]
        late = self.ui.question_cards.finish_answer(old['id'], old['attempt'], completed=True)
        self.assertEqual(late['state'], 'withdrawn')

    async def test_foreign_origin_and_non_loopback_peers_cannot_manage_cards(self):
        response = await self.client.post('/questions-action', json={'action':'manual','text':'x','request_key':'a' * 32},
                                         headers={'Origin':'https://foreign.example'})
        self.assertEqual(response.status, 403)
        from types import SimpleNamespace
        request = SimpleNamespace(remote='192.0.2.20', host='localhost', headers={})
        with self.assertRaises(web.HTTPForbidden):
            await self.ui._questions_status(request)
        with self.assertRaises(web.HTTPForbidden):
            await self.ui._questions_page(request)
        self.assertEqual(self.ui.question_cards.status()['cards'], [])

    async def test_unavailable_optional_card_storage_does_not_drop_chat_events(self):
        event = Event('danmaku', 'chat', '主流程应继续', user='观众')
        event.ui_id = 9
        with patch.object(self.ui, '_question_store', side_effect=sqlite3.OperationalError('locked')):
            self.assertEqual(self.ui.question_event_payload(event), {})
            response = await self.client.get('/questions-status')
            self.assertEqual(response.status, 503)


if __name__ == '__main__':
    unittest.main()
