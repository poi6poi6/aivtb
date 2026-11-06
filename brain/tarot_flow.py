"""Persisted guided tarot conversation; public input and actual delivery drive it.

No timers, network calls or generation occur while reading status. The runtime
owns polling and speech; this store only atomically claims the next stage.
"""
from __future__ import annotations

import json
import math
import re
import uuid

from .events import Event, PRIO_AMBIENT


_READY = {'welcome': 'welcoming', 'reading': 'revealed', 'closing': 'closing'}
_WAITING = {'awaiting_confirmation', 'selecting', 'awaiting_reply'}
_CONFIRM = {'对', '对的', '是', '是的', '没错', '没错的', '确认', '可以', '好', '好的', '嗯', '嗯嗯', 'ok'}
_END = {'结束', '结束占卜', '取消占卜', '取消', '退出', '先不占了', '不占了', '结束塔罗'}


class TarotFlow:
    WAIT_SECONDS = 60.0  # Includes a reasonable live-stream response allowance.
    MAX_SEGMENTS = 64
    MAX_SPOKEN_TEXT = 4000

    @staticmethod
    def _latest(db, session_id):
        row = db.execute('SELECT payload FROM tarot_readings WHERE session_id=? ORDER BY created DESC,rowid DESC LIMIT 1',
                         (session_id,)).fetchone()
        return json.loads(row['payload']) if row else None

    @staticmethod
    def _simple(value):
        return re.sub(r'[\s，。！？,.!?]+', '', value).casefold()

    def _recover_guided(self, db):
        # Reopening the store is a process restart, never permission to resume
        # a show or to consume a viewer's response while nobody is listening.
        for row in db.execute("SELECT payload FROM tarot_readings WHERE state!='finished'").fetchall():
            reading = json.loads(row['payload'])
            if reading.get('hosting') != 'guided' or reading.get('paused'):
                continue
            self._pause_reading(db, reading)
            reading['error'] = '服务已重启，本轮已暂停。确认现场后继续，牌面保持不变。'
            self._save_reading(db, reading)

    def _waiting(self, reading, state, *, duration=None):
        reading.update(state=state, attempt_id='', wait_started_at=self.clock(),
                       wait_deadline=self.clock() + (self.WAIT_SECONDS if duration is None else duration))

    def _complete(self, db, reading):
        reading.update(state='finished', attempt_id='', paused=False, wait_deadline=None)
        self._save_reading(db, reading)
        db.execute("UPDATE tarot_entries SET state='done' WHERE id=?", (reading['entry_id'],))

    def _timeout(self, db, reading):
        names = {'awaiting_confirmation': 'confirmation_timeout', 'selecting': 'selection_timeout',
                 'awaiting_reply': 'reply_timeout'}
        reading.update(finish_reason=names.get(reading['state'], 'timeout'), state='closing',
                       stage='closing', wait_deadline=None, attempt_id='')
        self._save_reading(db, reading)

    def _window_accepts(self, db, reading, timestamp):
        deadline = reading.get('wait_deadline')
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            return False
        if self.clock() >= deadline:
            self._timeout(db, reading)
            return False
        return timestamp >= reading.get('wait_started_at', reading['created']) and timestamp < deadline

    def _guided_input(self, event):
        """Only this round's verified participant can advance its dialogue."""
        from .safety import is_blocked, is_crisis
        from .tarot import _text

        if not isinstance(event.text, str):
            return None
        with self.database() as db:
            db.execute('BEGIN IMMEDIATE')
            session = db.execute("SELECT * FROM tarot_sessions WHERE state='open'").fetchone()
            if session is None:
                return None
            reading = self._latest(db, session['id'])
            if not reading or reading.get('hosting') != 'guided' or reading['state'] == 'finished':
                return None
            try:
                identity = self._identity(session, event)
            except ValueError:
                return None
            selected = db.execute('SELECT viewer FROM tarot_entries WHERE id=?', (reading['entry_id'],)).fetchone()
            if selected['viewer'] != identity:
                return None
            if getattr(event, '_tarot_test_session', session['id']) != session['id']:
                return self._receipt(False, '这条回应不属于当前场次。')
            timestamp = event.ts
            try:
                valid_time = (type(timestamp) in (int, float) and math.isfinite(timestamp)
                              and reading['created'] <= timestamp <= self.clock() + 5)
            except (OverflowError, TypeError, ValueError):
                valid_time = False
            if not valid_time:
                return self._receipt(False, '这条回应不属于当前轮次。')
            if is_crisis(event.text):
                self._complete(db, reading)
                return None  # The runtime routes this to its existing crisis support.
            if is_blocked(event.text) or is_blocked(str(event.user or '')):
                return self._receipt(False, '这个话题不进入茶会。可以换一个日常角度，或发送“结束”。')
            try:
                text = _text(event.text, 160, '回应')
            except ValueError:
                return self._receipt(False, '请用一句不超过 160 字的话回应。')
            if self._simple(text) in _END:
                reading['finish_reason'] = 'viewer_ended'
                self._complete(db, reading)
                return self._receipt(True, '已收起这一轮，谢谢你来坐一会儿。', changed=True)
            if reading.get('paused'):
                return self._receipt(False, '这一轮已暂停，请等主播继续。')
            state = reading['state']
            if state not in _WAITING:
                return self._receipt(False, '已经看到你的消息，请等她说完这一段再回应。')
            if not self._window_accepts(db, reading, timestamp):
                return self._receipt(False, '这条回应已错过本阶段，牌桌会保留当前结果。')
            if state == 'selecting':
                # Bare numbers are useful in a live conversation, but only the
                # selected viewer in this exact phase can use them as a draw.
                import unicodedata
                choice = unicodedata.normalize('NFKC', text)
                if len(choice) > 80 or not re.fullmatch(r'[0-9]+(?:[\s,，]+[0-9]+)*', choice):
                    return self._receipt(False, '请发送牌号，或“抽牌 7”；也可以发送“结束”。')
                try:
                    positions = [int(item) for item in re.split(r'[\s,，]+', choice)]
                    result = self._draw(db, reading, positions)
                except ValueError as error:
                    return self._receipt(False, str(error))
                return self._receipt(True, '已收到选牌，准备听她解读。', changed=True, reading=result)
            if state == 'awaiting_confirmation':
                reading['clarification'] = '' if self._simple(text) in _CONFIRM else text
                self._waiting(reading, 'selecting')
                message = '已确认想聊的方向，请选牌。'
            else:
                reading.update(response=text, stage='closing', state='closing', wait_deadline=None,
                               attempt_id='', finish_reason='viewer_replied')
                message = '已听到你的回应，她会沿着这组牌收个尾。'
            self._save_reading(db, reading)
            return self._receipt(True, message, changed=True, reading_id=reading['id'])

    def _end_crisis_reading(self, event):
        # Reuse identity and timestamp checks. Prefix removal is unnecessary:
        # the crisis detector recognizes the message with its command intact.
        self._guided_input(event)

    def test_reply(self, session_id, user, text):
        from .tarot import _text
        receipt = self._test_event(session_id, user, _text(text, 160, '回应'))
        return receipt if receipt is not None else self._receipt(False, '当前没有等待这位观众的塔罗回应。')

    def _claim_guided(self, db, session, reading, *, retry=False):
        stage = reading.get('stage', 'reading')
        ready = _READY.get(stage)
        if reading.get('paused') or ready is None:
            raise ValueError('本轮已暂停，请先继续。')
        allowed = {ready} | ({'failed', 'interrupted'} if retry else set())
        if reading['state'] not in allowed:
            raise ValueError('当前阶段还不能重复发起解读。')
        if stage == 'reading' and not reading['cards']:
            raise ValueError('请先选牌，再开始解读。')
        reading.update(state='queued', attempt_id=uuid.uuid4().hex, error='', wait_deadline=None,
                       generated_segments=None, played_segments=[], started_segments=[], display_text='')
        self._save_reading(db, reading)
        facts = {key: reading[key] for key in ('user', 'question', 'spread', 'cards', 'positions')}
        facts.update(mode=session['mode'], hosting='guided', stage=stage,
                     clarification=reading.get('clarification', ''), response=reading.get('response', ''),
                     prior_reading=reading.get('prior_reading', '')[:self.MAX_SPOKEN_TEXT],
                     reason=reading.get('finish_reason', ''))
        event = Event('system', 'ambient', reading['question'], user=reading['user'],
                      priority=PRIO_AMBIENT, forced=True, ts=self.clock())
        event.tarot_session_id, event.tarot_reading_id = session['id'], reading['id']
        event.tarot_attempt_id, event.tarot_stage = reading['attempt_id'], stage
        event.tarot_context = json.dumps(facts, ensure_ascii=False)
        return event

    def next_event(self):
        with self.database() as db:
            db.execute('BEGIN IMMEDIATE')
            session = db.execute("SELECT * FROM tarot_sessions WHERE state='open'").fetchone()
            if session is None:
                return None
            reading = self._latest(db, session['id'])
            if not reading or reading.get('hosting') != 'guided' or reading.get('paused'):
                return None
            if reading['state'] in _WAITING:
                deadline = reading.get('wait_deadline')
                if type(deadline) in (int, float) and math.isfinite(deadline) and self.clock() >= deadline:
                    self._timeout(db, reading)
            if reading['state'] != _READY.get(reading.get('stage')):
                return None
            return self._claim_guided(db, session, reading)

    def holds_chat(self):
        with self.database() as db:
            session = db.execute("SELECT * FROM tarot_sessions WHERE state='open' AND mode='live'").fetchone()
            reading = self._latest(db, session['id']) if session else None
            return bool(reading and reading.get('hosting') == 'guided' and reading['state'] != 'finished')

    def _advance_delivered(self, db, reading):
        stage = reading.get('stage')
        if stage == 'welcome':
            self._waiting(reading, 'awaiting_confirmation')
        elif stage == 'reading':
            reading['revealed_count'] = len(reading['cards'])
            self._waiting(reading, 'awaiting_reply')
        elif stage == 'closing':
            reading['farewell'] = reading.get('closing_text', '')[:200]
            self._complete(db, reading)
            return
        self._save_reading(db, reading)

    def _all_delivered(self, db, reading):
        total = reading.get('generated_segments')
        if type(total) is int and total > 0 and set(range(total)).issubset(reading.get('played_segments', [])):
            self._advance_delivered(db, reading)

    def mark_generated(self, event, text, segments, text_only=False):
        if (not isinstance(text, str) or not text.strip() or len(text) > self.MAX_SPOKEN_TEXT
                or type(segments) is not int or not 0 <= segments <= self.MAX_SEGMENTS
                or type(text_only) is not bool):
            raise ValueError('本轮生成结果无效。')
        from .safety import is_blocked
        with self.database() as db:
            db.execute('BEGIN IMMEDIATE')
            reading = self._matching_attempt(db, event)
            if reading is None:
                return False
            if is_blocked(text):
                reading.update(state='failed', error='本轮内容未通过检查，请换一个日常问题。')
                self._save_reading(db, reading)
                return False
            # Older manual callers still see exactly the existing generated
            # status, and do not acquire a hidden automatic response window.
            reading.update(state='generated', display_text=text, generated_segments=segments)
            if reading.get('hosting') == 'guided':
                if reading.get('stage') == 'reading':
                    reading['prior_reading'] = text
                elif reading.get('stage') == 'closing':
                    reading['closing_text'] = text
                if text_only or not segments:
                    reading.update(state='awaiting_host', text_only=True)
                else:
                    self._all_delivered(db, reading)
            self._save_reading(db, reading)
            return self._reading_view(reading)

    def played(self, event, index, completed):
        if type(index) is not int or not 0 <= index < self.MAX_SEGMENTS or type(completed) is not bool:
            return False
        with self.database() as db:
            db.execute('BEGIN IMMEDIATE')
            reading = self._matching_attempt(db, event)
            if reading is None or reading.get('hosting') != 'guided':
                return False
            if not completed:
                reading.update(state='interrupted', error='本段播报未完成，等待主播决定是否用原内容重试。')
            else:
                seen = set(reading.get('played_segments', []))
                seen.add(index)
                reading['played_segments'] = sorted(seen)
                self._all_delivered(db, reading)
            self._save_reading(db, reading)
            return self._reading_view(reading)

    def note_playback_started(self, event, text, index):
        if not isinstance(text, str) or type(index) is not int or not 0 <= index < self.MAX_SEGMENTS:
            return False
        with self.database() as db:
            db.execute('BEGIN IMMEDIATE')
            reading = self._matching_attempt(db, event)
            if reading is None or reading.get('hosting') != 'guided':
                return False
            seen = set(reading.get('started_segments', []))
            if index in seen:
                return False
            seen.add(index)
            reading['started_segments'] = sorted(seen)
            if reading.get('stage') == 'reading':
                matched = [i + 1 for i, card in enumerate(reading['cards']) if card['name'] in text]
                reading['revealed_count'] = max([reading.get('revealed_count', 0), 1, *matched])
            self._save_reading(db, reading)
            return self._reading_view(reading)

    def confirm_delivered(self, session_id, reading_id):
        with self.database() as db:
            db.execute('BEGIN IMMEDIATE')
            self._session(db, session_id)
            reading = self._reading(db, session_id, reading_id)
            if reading.get('hosting') != 'guided' or reading['state'] != 'awaiting_host' or reading.get('paused'):
                raise ValueError('只有等待主播确认展示的阶段才能继续。')
            self._advance_delivered(db, reading)
            return self._reading_view(reading)

    def _pause_reading(self, db, reading):
        state = reading['state']
        deadline = reading.get('wait_deadline')
        remaining = max(0.0, deadline - self.clock()) if type(deadline) in (int, float) else None
        if state in {'queued', 'generated', 'failed', 'interrupted'}:
            state = _READY.get(reading.get('stage'), 'revealed')
        reading.update(paused=True, state='paused', resume_state=state, pause_remaining=remaining,
                       attempt_id='', wait_deadline=None)
        self._save_reading(db, reading)

    def pause(self, session_id, reading_id):
        with self.database() as db:
            db.execute('BEGIN IMMEDIATE')
            self._session(db, session_id)
            reading = self._reading(db, session_id, reading_id)
            if reading.get('hosting') != 'guided' or reading['state'] == 'finished':
                raise ValueError('当前没有正在主持的轮次。')
            if not reading.get('paused'):
                self._pause_reading(db, reading)
            return self._reading_view(reading)

    def pause_current(self):
        with self.database() as db:
            db.execute('BEGIN IMMEDIATE')
            session = db.execute("SELECT * FROM tarot_sessions WHERE state='open'").fetchone()
            reading = self._latest(db, session['id']) if session else None
            if (not reading or reading.get('hosting') != 'guided' or reading.get('paused')
                    or reading['state'] == 'finished'):
                return False
            self._pause_reading(db, reading)
            return True

    def resume(self, session_id, reading_id):
        with self.database() as db:
            db.execute('BEGIN IMMEDIATE')
            self._session(db, session_id)
            reading = self._reading(db, session_id, reading_id)
            if reading.get('hosting') != 'guided' or not reading.get('paused'):
                raise ValueError('这一轮没有暂停。')
            state = reading.pop('resume_state', _READY.get(reading.get('stage'), 'revealed'))
            remaining = reading.pop('pause_remaining', None)
            reading.update(paused=False, state=state, error='')
            if state in _WAITING:
                self._waiting(reading, state, duration=remaining if remaining is not None else self.WAIT_SECONDS)
            self._save_reading(db, reading)
            return self._reading_view(reading)

    def takeover(self, session_id, reading_id):
        with self.database() as db:
            db.execute('BEGIN IMMEDIATE')
            self._session(db, session_id)
            reading = self._reading(db, session_id, reading_id)
            if reading['state'] == 'finished':
                raise ValueError('这一轮已经结束。')
            reading.update(hosting='manual', paused=False, attempt_id='', wait_deadline=None,
                           stage='reading', state='revealed' if reading['cards'] else 'selecting',
                           revealed_count=len(reading['cards']), error='')
            self._save_reading(db, reading)
            return self._reading_view(reading)
