"""Host-created, single-choice live polls. Votes bypass the chat attention queue."""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
import unicodedata
import uuid
from contextlib import contextmanager
from pathlib import Path

from .events import PRIO_AMBIENT, Event


def normalized(value):
    return unicodedata.normalize('NFKC', value).strip().casefold()


def vote_command(text):
    """Require an explicit prefix so casual numbers and conversation stay intact."""
    match = re.fullmatch(r'投票\s*[:：]?\s*(.{1,60})', text.strip())
    return normalized(match[1]) if match else None


def short_text(value, limit, label):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise ValueError(f'{label}须为 1–{limit} 个字')
    if any(unicodedata.category(c).startswith('C') for c in value):
        raise ValueError(f'{label}不能包含控制字符')
    return value.strip()


class LivePolls:
    def __init__(self, path, *, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.database() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS polls (
                    id TEXT PRIMARY KEY, program TEXT NOT NULL, question TEXT NOT NULL,
                    options TEXT NOT NULL, mode TEXT NOT NULL, state TEXT NOT NULL,
                    created REAL NOT NULL, deadline REAL NOT NULL, closed REAL,
                    close_reason TEXT, salt TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS one_open_poll ON polls(state) WHERE state='open';
                CREATE TABLE IF NOT EXISTS votes (
                    poll_id TEXT NOT NULL REFERENCES polls(id), voter TEXT NOT NULL,
                    choice INTEGER NOT NULL, received REAL NOT NULL,
                    PRIMARY KEY(poll_id, voter));
            ''')

    @contextmanager
    def database(self):
        db = sqlite3.connect(self.path, timeout=2)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def _expire(self, db):
        db.execute("UPDATE polls SET state='closed', closed=deadline, close_reason='timeout' "
                   "WHERE state='open' AND deadline<=?", (self.clock(),))

    def tick(self):
        with self.database() as db:
            self._expire(db)

    def create(self, *, program, question, options, duration=180, mode='live'):
        program = short_text(program, 30, '栏目名称')
        question = short_text(question, 100, '投票问题')
        if not isinstance(options, list) or not 2 <= len(options) <= 6:
            raise ValueError('请填写 2–6 个选项')
        options = [short_text(item, 40, '选项') for item in options]
        keys = [normalized(item) for item in options]
        if len(set(keys)) != len(keys):
            raise ValueError('选项不能重复')
        if any(re.fullmatch(r'\d+|[a-f]', key) for key in keys):
            raise ValueError('选项请使用名称，避免与投票编号冲突')
        if type(duration) is not int or not 15 <= duration <= 3600:
            raise ValueError('投票时长须为 15–3600 秒的整数')
        if mode not in {'live', 'test'}:
            raise ValueError('请选择正式投票或测试投票')
        now, poll_id = self.clock(), uuid.uuid4().hex[:16]
        with self.database() as db:
            self._expire(db)
            if db.execute("SELECT 1 FROM polls WHERE state='open'").fetchone():
                raise ValueError('请先结束或取消当前投票')
            db.execute('INSERT INTO polls VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       (poll_id, program, question, json.dumps(options, ensure_ascii=False),
                        mode, 'open', now, now + duration, None, None, uuid.uuid4().hex))
            return self._summary(db, db.execute('SELECT * FROM polls WHERE id=?', (poll_id,)).fetchone())

    def finish(self, poll_id, *, cancel=False):
        with self.database() as db:
            self._expire(db)
            row = db.execute('SELECT * FROM polls WHERE id=?', (poll_id,)).fetchone()
            if row is None:
                raise ValueError('投票不存在，请刷新页面')
            if row['state'] == 'open':
                db.execute('UPDATE polls SET state=?, closed=?, close_reason=? WHERE id=?',
                           ('cancelled' if cancel else 'closed', self.clock(),
                            'cancelled' if cancel else 'host', poll_id))
            elif cancel and row['state'] != 'cancelled':
                raise ValueError('这轮投票已结束，结果已保留')
            return self._summary(db, db.execute('SELECT * FROM polls WHERE id=?', (poll_id,)).fetchone())

    def handle_event(self, event):
        """Return a local receipt or None (ordinary chat); never call a model."""
        if event.source != 'danmaku' or event.kind != 'chat':
            return None
        choice = vote_command(event.text)
        if choice is None:
            return None
        with self.database() as db:
            self._expire(db)
            row = db.execute("SELECT * FROM polls WHERE state='open'").fetchone()
            if row is None:
                return {'ok': False, 'message': '当前没有进行中的投票'}
            identity = event.viewer_key
            if row['mode'] == 'test':
                if not (event.forced and getattr(event, 'owner_control', False)):
                    return {'ok': False, 'message': '测试投票不接收直播观众票'}
                identity = 'test:' + (event.user or '测试观众')
            elif (event.forced or not identity or
                  not identity.startswith(('bili:', 'bili-hash:'))):
                return {'ok': False, 'message': '正式投票只接收有稳定账号标识的 B 站弹幕'}
            now = self.clock()
            if not math.isfinite(event.ts) or event.ts < row['created'] or event.ts > now + 5:
                return {'ok': False, 'message': '这条弹幕不属于当前投票时间段'}
            options = json.loads(row['options'])
            aliases = {normalized(label): i for i, label in enumerate(options)}
            aliases.update({str(i + 1): i for i in range(len(options))})
            aliases.update({chr(97 + i): i for i in range(len(options))})
            if choice not in aliases:
                return {'ok': False, 'message': '选项无效，请发送“投票 1”等页面列出的编号'}
            voter = hashlib.sha256((row['salt'] + identity).encode()).hexdigest()
            old = db.execute('SELECT choice, received FROM votes WHERE poll_id=? AND voter=?',
                             (row['id'], voter)).fetchone()
            if old and event.ts <= old['received']:
                return {'ok': True, 'changed': False, 'poll_id': row['id'],
                        'mode': row['mode'], 'message': '重复或较早的投票已忽略'}
            db.execute('INSERT INTO votes VALUES (?,?,?,?) ON CONFLICT(poll_id,voter) '
                       'DO UPDATE SET choice=excluded.choice, received=excluded.received',
                       (row['id'], voter, aliases[choice], event.ts))
            return {'ok': True, 'changed': old is None or old['choice'] != aliases[choice],
                    'poll_id': row['id'], 'mode': row['mode'],
                    'message': '已改票' if old and old['choice'] != aliases[choice] else '已计票'}

    def test_vote(self, poll_id, user, choice):
        # A stale form must never vote in a newer poll.
        current = self.status(include_history=False)['current']
        if not current or current['id'] != poll_id or current['state'] != 'open' or current['mode'] != 'test':
            raise ValueError('当前不是这轮测试投票，请刷新页面')
        event = Event('danmaku', 'chat', '投票 ' + short_text(choice, 40, '投票选项'),
                      user=short_text(user, 40, '测试观众'), forced=True, ts=self.clock())
        event.owner_control = True
        return self.handle_event(event)

    def _summary(self, db, row):
        counts = dict(db.execute('SELECT choice, COUNT(*) FROM votes WHERE poll_id=? GROUP BY choice', (row['id'],)))
        total = sum(counts.values())
        options = [{'number': i + 1, 'label': label, 'votes': counts.get(i, 0),
                    'percent': round(counts.get(i, 0) * 100 / total, 1) if total else 0}
                   for i, label in enumerate(json.loads(row['options']))]
        top = max(item['votes'] for item in options)
        winners = ([item['number'] for item in options if item['votes'] == top]
                   if total and row['state'] == 'closed' else [])
        return {key: row[key] for key in ('id', 'program', 'question', 'mode', 'state', 'created',
                                         'deadline', 'closed', 'close_reason')} | {
            'total': total, 'options': options, 'winners': winners,
            'remaining': max(0, math.ceil(row['deadline'] - self.clock())) if row['state'] == 'open' else 0}

    def status(self, *, include_history=True):
        with self.database() as db:
            self._expire(db)
            rows = db.execute('SELECT * FROM polls ORDER BY created DESC, rowid DESC LIMIT ?',
                              (21 if include_history else 1,)).fetchall()
            return {'server_time': self.clock(), 'current': self._summary(db, rows[0]) if rows else None,
                    'history': [self._summary(db, row) for row in rows[1:]]}

    def announcement(self, poll_id):
        return self._announcement_text(self.get(poll_id))

    def get(self, poll_id):
        """Read any durable round, including one older than the recent sidebar."""
        if not isinstance(poll_id, str) or len(poll_id) != 16:
            raise ValueError('请选择一轮投票')
        with self.database() as db:
            self._expire(db)
            row = db.execute('SELECT * FROM polls WHERE id=?', (poll_id,)).fetchone()
            if row is None:
                raise ValueError('投票不存在')
            poll = self._summary(db, row)
        return poll

    def announcement_event(self, poll_id):
        """Attach server-owned poll identity so votes cannot answer other questions."""
        with self.database() as db:
            self._expire(db)
            row = db.execute('SELECT * FROM polls WHERE id=?', (poll_id,)).fetchone()
            if row is None:
                raise ValueError('投票不存在')
            poll = self._summary(db, row)
        text = self._announcement_text(poll)
        event = Event('system', 'ambient', text, priority=PRIO_AMBIENT,
                      forced=True, agent_reply=text, ts=self.clock())
        event.live_poll = {key: poll[key] for key in
                           ('id', 'question', 'state', 'mode', 'deadline', 'total')}
        return event

    @staticmethod
    def _announcement_text(poll):
        prefix = ('这是一轮测试投票。' if poll['mode'] == 'test' else '') + f"{poll['program']}，{poll['question']}。"
        if poll['state'] == 'open':
            return prefix + '；'.join(f"{o['number']}号，{o['label']}" for o in poll['options']) + (
                '。本轮每个账号一票，结束前可以改票。请在画面倒计时结束前发送投票加编号参与。')
        if poll['state'] == 'cancelled':
            return prefix + '本轮已取消，不选出结果。'
        if not poll['total']:
            return prefix + '本轮投票已结束，没有收到有效票，暂不选出结果。'
        names = '、'.join(o['label'] for o in poll['options'] if o['number'] in poll['winners'])
        tally = '；'.join(f"{o['label']}{o['votes']}票" for o in poll['options'])
        return prefix + f"投票已结束，共{poll['total']}人参与。{tally}。{names}" + (
            '并列最高票，等待主播决定。' if len(poll['winners']) > 1 else '获得最高票。')
