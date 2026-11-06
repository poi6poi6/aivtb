"""Host-selected questions with durable state and playback-confirmed completion."""
from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import unicodedata
import uuid

from .events import Event
from .viewer_identity import viewer_identity


STATES = {'waiting', 'answering', 'answered', 'parked', 'withdrawn'}


def question_text(value):
    if not isinstance(value, str):
        raise ValueError('问题须为文本')
    text = value.strip()
    if not 1 <= len(text) <= 1000:
        raise ValueError('问题须为 1–1000 个字')
    if any(unicodedata.category(c).startswith('C') and c not in '\n\r\t' for c in text):
        raise ValueError('问题包含不支持的字符')
    return text


class QuestionCards:
    MAX_ACTIVE = 100
    MAX_TOTAL = 500

    def __init__(self, path, *, clock=time.time):
        self.path, self.clock = Path(path), clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.recent = OrderedDict()
        self.boot = uuid.uuid4().hex
        with self.database() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS question_cards (
                id TEXT PRIMARY KEY, origin_key TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL, version INTEGER NOT NULL, created REAL NOT NULL,
                payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS question_card_state ON question_cards(state);''')
            for key, payload in db.execute("SELECT id,payload FROM question_cards WHERE state='answering'").fetchall():
                card = json.loads(payload)
                card.update(state='waiting', version=card['version'] + 1, updated=self.clock(),
                            reason='服务已重启，上次回答未确认播完', turn_id=None)
                db.execute('UPDATE question_cards SET state=?,version=?,payload=? WHERE id=?',
                           ('waiting', card['version'], json.dumps(card, ensure_ascii=False), key))

    @contextmanager
    def database(self):
        db = sqlite3.connect(self.path, timeout=2)
        try:
            with db:
                yield db
        finally:
            db.close()

    def remember_event(self, event: Event):
        """Only a bounded volatile cache; no unselected audience messages on disk."""
        if event.kind != 'chat' or event.source not in {'danmaku', 'voice', 'companion'}:
            return {}
        if getattr(event, 'host_question_id', None):
            return {}
        event_id = getattr(event, 'ui_id', None)
        if type(event_id) is not int or event_id < 1:
            return {}
        try:
            text = question_text(event.text)
        except ValueError:
            return {}
        identity = viewer_identity(event)
        origin_key = hashlib.sha256(f'{self.boot}:{event_id}:{identity}'.encode()).hexdigest()
        token = self.recent.get(origin_key, {}).get('token') or uuid.uuid4().hex
        self.recent[origin_key] = {
            'token': token, 'cached_at': self.clock(), 'text': text,
            'origin': {'kind': 'event', 'event_id': event_id, 'source': event.source,
                       'user': event.user or '', 'viewer_key': event.viewer_key,
                       'identity': identity, 'ts': event.ts},
        }
        self.recent.move_to_end(origin_key)
        self._prune_recent()
        return {'question_pin_token': token}

    def _prune_recent(self):
        cutoff = self.clock() - 1800
        while self.recent and (len(self.recent) > 500 or next(iter(self.recent.values()))['cached_at'] < cutoff):
            self.recent.popitem(last=False)

    def pin(self, token):
        if not isinstance(token, str) or len(token) != 32:
            raise ValueError('这条消息无法固定，请在待答页手动填写')
        self._prune_recent()
        for origin_key, event in self.recent.items():
            if event['token'] == token:
                return self._create(origin_key, event['text'], event['origin'])
        raise ValueError('消息已过期，请在待答页手动填写；未保存该弹幕')

    def manual(self, text, request_key):
        if (not isinstance(request_key, str) or len(request_key) != 32
                or any(c not in '0123456789abcdef' for c in request_key)):
            raise ValueError('新增问题编号无效，请刷新重试')
        return self._create('manual:' + request_key, question_text(text),
                            {'kind': 'manual', 'source': 'companion', 'user': '主播手填',
                             'event_id': None, 'viewer_key': '', 'identity': '', 'ts': self.clock()})

    def _create(self, origin_key, text, origin):
        now = self.clock()
        card = {'id': uuid.uuid4().hex[:16], 'text': text, 'origin': deepcopy(origin),
                'state': 'waiting', 'version': 1, 'created': now, 'updated': now,
                'attempt': None, 'turn_id': None, 'reason': '', 'answered_at': None}
        with self.database() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT payload FROM question_cards WHERE origin_key=?', (origin_key,)).fetchone()
            if previous:
                old = json.loads(previous[0])
                if old['text'] != text:
                    raise ValueError('该新增编号已用于其他问题，请重新新增')
                return old
            count = db.execute('SELECT COUNT(*) FROM question_cards').fetchone()[0]
            active = db.execute("SELECT COUNT(*) FROM question_cards WHERE state IN ('waiting','answering','parked')").fetchone()[0]
            if active >= self.MAX_ACTIVE:
                raise ValueError('待处理问题已达 100 张，请先回答或撤回一些卡片')
            if count >= self.MAX_TOTAL:
                raise ValueError('问题卡已达 500 张，请先删除已撤回的卡片')
            db.execute('INSERT INTO question_cards VALUES (?,?,?,?,?,?)',
                       (card['id'], origin_key, card['state'], 1, now, json.dumps(card, ensure_ascii=False)))
        return card

    def get(self, key):
        if not isinstance(key, str):
            raise ValueError('请选择问题卡')
        with self.database() as db:
            row = db.execute('SELECT payload FROM question_cards WHERE id=?', (key,)).fetchone()
        if not row:
            raise ValueError('问题卡不存在或已删除')
        return json.loads(row[0])

    def checked(self, key, version):
        card = self.get(key)
        if type(version) is not int or version != card['version']:
            raise ValueError('问题卡已由其他页面或播放回执更新，请刷新后重试')
        return card

    def _save(self, card):
        previous = card['version']
        card.update(version=previous + 1, updated=self.clock())
        with self.database() as db:
            changed = db.execute('UPDATE question_cards SET state=?,version=?,payload=? WHERE id=? AND version=?',
                                (card['state'], card['version'], json.dumps(card, ensure_ascii=False), card['id'], previous))
            if changed.rowcount != 1:
                raise ValueError('问题卡已更新，请刷新后重试')
        return card

    def begin_answer(self, key, version):
        card = self.checked(key, version)
        if card['state'] not in {'waiting', 'parked'}:
            raise ValueError('只有待答或暂放的问题可以开始回答')
        card.update(state='answering', attempt=uuid.uuid4().hex, turn_id=None,
                    reason='等待实际回答并播完', answered_at=None)
        return self._save(card)

    def bind_turn(self, key, attempt, turn_id):
        if type(turn_id) is not int or turn_id < 1:
            raise ValueError('主控未确认问题调度')
        card = self.get(key)
        if card['state'] != 'answering' or card['attempt'] != attempt:
            return card
        if card['turn_id'] is not None and card['turn_id'] != turn_id:
            raise ValueError('问题已关联其他回答')
        card['turn_id'] = turn_id
        return self._save(card)

    def finish_answer(self, key, attempt, *, completed=False, reason=''):
        """Host receipt only. A stale/cancelled attempt cannot complete a new one."""
        card = self.get(key)
        if card['state'] != 'answering' or not attempt or card['attempt'] != attempt:
            return card
        if completed and card['turn_id'] is None:
            raise ValueError('未关联主控回答，不能标记已答')
        card.update(state='answered' if completed else 'waiting',
                    answered_at=self.clock() if completed else None,
                    reason='已确认完整播完' if completed else str(reason or '上次回答未完成，仍待回答')[:160])
        return self._save(card)

    def dispatch_failed(self, key, attempt):
        card = self.get(key)
        if card['state'] != 'answering' or card['attempt'] != attempt:
            return card
        if card['turn_id'] is None:
            return self.finish_answer(key, attempt, reason='调度未完成，请核对主控后重试')
        # Once a turn is bound it may already be queued/running. A lost callback
        # must not unlock another attempt before the host cancels or settles it.
        card['reason'] = '调度回执未确认，请停止本次回答后重试'
        return self._save(card)

    def move(self, key, version, state):
        card = self.checked(key, version)
        if state not in {'waiting', 'parked', 'withdrawn'}:
            raise ValueError('不支持该问题状态；已答只能由播放完成回执确认')
        if card['state'] == 'answering':
            raise ValueError('请先停止这次回答，再修改问题状态')
        if state in {'waiting', 'parked'} and card['state'] in {'answered', 'withdrawn'}:
            with self.database() as db:
                active = db.execute("SELECT COUNT(*) FROM question_cards WHERE state IN ('waiting','answering','parked')").fetchone()[0]
            if active >= self.MAX_ACTIVE:
                raise ValueError('待处理问题已达 100 张')
        card.update(state=state, attempt=None, turn_id=None,
                    reason='主播撤回' if state == 'withdrawn' else '', answered_at=None)
        return self._save(card)

    def delete(self, key, version):
        card = self.checked(key, version)
        if card['state'] != 'withdrawn':
            raise ValueError('只能删除已撤回的问题卡')
        with self.database() as db:
            result = db.execute("DELETE FROM question_cards WHERE id=? AND version=? AND state='withdrawn'", (key, version))
            if result.rowcount != 1:
                raise ValueError('问题卡已更新，请刷新后重试')
        return {'ok': True}

    @staticmethod
    def public(card):
        result = {key: deepcopy(value) for key, value in card.items() if key not in {'origin', 'attempt'}}
        result['origin'] = {key: card['origin'].get(key) for key in ('kind', 'source', 'user', 'event_id', 'ts')}
        return result

    def status(self):
        with self.database() as db:
            rows = db.execute('SELECT payload FROM question_cards ORDER BY created DESC,rowid DESC').fetchall()
        return {'cards': [self.public(json.loads(row[0])) for row in rows],
                'max_active': self.MAX_ACTIVE, 'max_total': self.MAX_TOTAL}
