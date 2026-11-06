"""Host-led tarot segments with durable, immutable draws and no model calls."""
from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import sqlite3
import time
import unicodedata
import uuid
from contextlib import contextmanager
from pathlib import Path

from .events import Event, PRIO_AMBIENT
from .tarot_flow import TarotFlow


# These are reflection prompts, not predictions or claims about the viewer.
_MAJORS = (
    ('愚者', '起点、好奇、试探', '可以留一点空间尝试新事物，也看清脚下的现实条件。', '冒进、犹豫、准备', '先分清是害怕迈步，还是准备不足；小步试探比冲动决定更稳妥。'),
    ('魔术师', '资源、行动、表达', '看看手里已有的工具，把一个想法落实成可执行的小动作。', '分心、空转、聚焦', '检查注意力是否被太多目标分散，先让一件具体的事落地。'),
    ('女祭司', '观察、直觉、留白', '不急着填满未知，区分自己的感受与已经核实的信息。', '困惑、压抑、核实', '被忽略的感受值得倾听，猜测仍需要现实信息来核对。'),
    ('皇后', '滋养、创造、照料', '为成长留出时间，照顾日常需求，也给创作一点舒展的余地。', '耗竭、依赖、界限', '检查看顾他人时是否透支了自己，适当收回精力与界限。'),
    ('皇帝', '结构、责任、边界', '把规则和责任说清楚，稳定的安排可以托住眼前的行动。', '僵化、控制、弹性', '分清必要的秩序与过度控制，给实际情况留一点调整空间。'),
    ('教皇', '学习、传统、指引', '可以参考可靠的经验和规范，再判断哪些适合自己的处境。', '质疑、自主、辨别', '不必因惯例而盲从，也别只为反对而反对；解释自己的理由。'),
    ('恋人', '选择、价值、连接', '留意选择是否符合自己的价值，也把关系中的真实需求说出来。', '分歧、摇摆、沟通', '先看清价值或需求上的差异，不急着把复杂关系裁成对错。'),
    ('战车', '方向、意志、协调', '确定一段可以掌握的方向，让行动和节奏朝同一处用力。', '失衡、急迫、调整', '方向不清时可以暂缓加速，把冲突的目标重新排个次序。'),
    ('力量', '耐心、勇气、温和', '用温和而持续的方式处理困难，力量也可以是不急于证明自己。', '自疑、疲惫、恢复', '疲惫时不必硬撑，先恢复一点空间，再处理让自己不安的事。'),
    ('隐者', '独处、梳理、寻找', '给自己一段安静的整理时间，从具体经验中辨认下一步。', '疏离、封闭、联系', '独处若变成难以求助，可以尝试向可信的人表达一点真实需要。'),
    ('命运之轮', '变化、周期、应变', '有些变化不由个人安排，重点可以放在目前仍能调整的部分。', '反复、阻滞、弹性', '反复出现的情形值得记录规律，先处理可控因素而非归咎命运。'),
    ('正义', '事实、衡量、责任', '把证据、选择和后果摆在一起，尽量作出公平且可解释的判断。', '偏差、失衡、核对', '检查信息是否完整，也看看评判自己或他人的尺度是否一致。'),
    ('倒吊人', '暂停、换位、观察', '暂时停一停也能获得信息，换个角度看当前的取舍。', '拖延、消耗、取舍', '分清等待是否仍有意义，避免只因已经投入而继续无效消耗。'),
    ('死神', '结束、转变、更新', '这张牌谈的是阶段转换；可以辨认哪些旧做法已经不适合继续。', '留恋、过渡、松手', '改变未必要一次完成，先承认留恋，再尝试结束一个小循环。'),
    ('节制', '调和、节奏、整合', '把不同需要放进可持续的节奏，适量和渐进比极端更有帮助。', '过量、失调、缓和', '留意哪里失去平衡，先做一个可以维持的小调整。'),
    ('恶魔', '牵绊、惯性、觉察', '看看哪些习惯或交换让自己失去选择感，辨认可以松动的一环。', '松绑、自主、重建', '重新审视依赖与界限，改变可以从一次更自主的选择开始。'),
    ('高塔', '冲击、拆解、重建', '当原有安排被打乱，先处理眼前可确认的情况，再考虑怎样重建。', '预警、抗拒、修补', '留意被拖延的问题，较早的小修正有时比继续硬撑更合适。'),
    ('星星', '希望、修复、远景', '可以给恢复一点时间，把希望放在温和且实际的下一步上。', '灰心、疏远、续航', '暂时看不到远景时，先寻找一点可验证的支持与进展。'),
    ('月亮', '未知、情绪、想象', '感受可能放大未知，允许不确定，同时用现实信息检验猜测。', '澄清、疑虑、辨认', '把模糊的担忧拆开，一次核实一个问题，不急着下定论。'),
    ('太阳', '明朗、活力、分享', '留意眼前真实的小成果，让清晰的表达和轻松感帮助自己前行。', '低落、遮蔽、耐心', '进展不够显眼不等于没有进展，降低比较，看看已经做到的部分。'),
    ('审判', '回顾、觉醒、回应', '回看经历中的线索，决定哪些经验值得带进下一阶段。', '自责、回避、宽待', '反省可以指向改进，不必把一次失误变成对自己的永久判词。'),
    ('世界', '整合、完成、阶段', '把阶段成果收拢，认清已经完成的部分，再决定是否开启下一步。', '未竟、收尾、连接', '看看还有哪一小处需要收尾，不必为了完美否认已有的积累。'),
)
SPREADS = {'single': ('此刻的线索',), 'three': ('眼下', '阻碍', '可尝试的方向')}


def _text(value, limit, label):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= limit:
        raise ValueError(f'{label}须为 1–{limit} 个字')
    if any(unicodedata.category(char).startswith('C') for char in value):
        raise ValueError(f'{label}不能包含控制字符')
    return value.strip()


def tarot_command(text):
    """Do not swallow casual mentions of tarot, bare numbers or other chat."""
    if not isinstance(text, str):
        return None
    match = re.fullmatch(r'(塔罗|抽牌)(?:\s+|[:：]\s*)(.+)', text.strip())
    return (('join' if match[1] == '塔罗' else 'choose'), match[2].strip()) if match else None


class Tarot(TarotFlow):
    MAX_QUEUE = 30
    MAX_RECORDS = 500

    def __init__(self, path, *, clock=time.time):
        self.path, self.clock = Path(path), clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.database() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS tarot_sessions (
                    id TEXT PRIMARY KEY, mode TEXT NOT NULL, spread TEXT NOT NULL,
                    state TEXT NOT NULL, created REAL NOT NULL, closed REAL, salt TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS one_open_tarot ON tarot_sessions(state) WHERE state='open';
                CREATE TABLE IF NOT EXISTS tarot_entries (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES tarot_sessions(id) ON DELETE CASCADE,
                    viewer TEXT NOT NULL, user TEXT NOT NULL, question TEXT NOT NULL,
                    state TEXT NOT NULL, received REAL NOT NULL, UNIQUE(session_id, viewer));
                CREATE TABLE IF NOT EXISTS tarot_readings (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES tarot_sessions(id) ON DELETE CASCADE,
                    entry_id TEXT NOT NULL REFERENCES tarot_entries(id) ON DELETE CASCADE,
                    state TEXT NOT NULL, created REAL NOT NULL, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS tarot_readings_session ON tarot_readings(session_id,created);
            ''')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(tarot_sessions)')}
            if 'hosting' not in columns:
                db.execute("ALTER TABLE tarot_sessions ADD COLUMN hosting TEXT NOT NULL DEFAULT 'manual'")
            for row in db.execute("SELECT * FROM tarot_readings WHERE state IN ('queued','generated')").fetchall():
                reading = json.loads(row['payload'])
                reading.update(state='interrupted', attempt_id='',
                               error='服务已重启，上次解读未确认结束；可手动重试，牌面保持不变。')
                self._save_reading(db, reading)
            self._recover_guided(db)

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

    @staticmethod
    def _id(value):
        if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{16}', value):
            raise ValueError('塔罗轮次编号无效，请刷新页面')
        return value

    def _session(self, db, key, *, open_required=True):
        row = db.execute('SELECT * FROM tarot_sessions WHERE id=?', (self._id(key),)).fetchone()
        if row is None:
            raise ValueError('塔罗场次不存在')
        if open_required and row['state'] != 'open':
            raise ValueError('这场塔罗已结束，请刷新页面')
        return row

    def _reading(self, db, session_id, reading_id, *, current=True):
        row = db.execute('SELECT * FROM tarot_readings WHERE id=? AND session_id=?',
                         (self._id(reading_id), session_id)).fetchone()
        if row is None:
            raise ValueError('这位观众的塔罗记录不存在')
        if current:
            latest = db.execute('SELECT id FROM tarot_readings WHERE session_id=? ORDER BY created DESC,rowid DESC LIMIT 1',
                                (session_id,)).fetchone()
            if not latest or latest['id'] != reading_id:
                raise ValueError('当前已换到另一位观众，请刷新页面')
        return json.loads(row['payload'])

    @staticmethod
    def _save_reading(db, reading):
        db.execute('UPDATE tarot_readings SET state=?,payload=? WHERE id=?',
                   (reading['state'], json.dumps(reading, ensure_ascii=False), reading['id']))

    def _make_room(self, db, table):
        # Table names are internal constants. Never prune the active program.
        while db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] >= self.MAX_RECORDS:
            oldest = db.execute("SELECT id FROM tarot_sessions WHERE state='closed' ORDER BY created,rowid LIMIT 1").fetchone()
            if not oldest:
                raise ValueError('塔罗记录已满，请先结束当前场次')
            db.execute('DELETE FROM tarot_sessions WHERE id=?', (oldest['id'],))

    @staticmethod
    def _entry_view(row):
        return {key: row[key] for key in ('id', 'user', 'question', 'state')}

    @staticmethod
    def _reading_view(reading, *, public=False):
        fields = ('id', 'user', 'question', 'state', 'spread', 'card_count', 'cards', 'positions')
        result = {key: reading[key] for key in fields}
        if not public:
            result.update(session_id=reading['session_id'], attempt_id=reading['attempt_id'], error=reading['error'])
        result.update({key: reading.get(key, default) for key, default in (
            ('hosting', 'manual'), ('stage', 'reading'), ('paused', False),
            ('wait_deadline', None), ('revealed_count', len(reading['cards'])), ('farewell', ''))})
        if public and reading.get('hosting') == 'guided':
            result['cards'] = result['cards'][:result['revealed_count']]
        if not public:
            result.update({key: reading.get(key, '') for key in (
                'display_text', 'closing_text', 'clarification', 'response', 'finish_reason')})
        return result

    @staticmethod
    def _session_view(db, row):
        return {key: row[key] for key in ('id', 'mode', 'spread', 'state', 'created', 'hosting')} | {
            'queue_count': db.execute("SELECT COUNT(*) FROM tarot_entries WHERE session_id=? AND state='waiting'",
                                      (row['id'],)).fetchone()[0]}

    def create(self, *, spread='single', mode='test', hosting='manual'):
        if (not isinstance(spread, str) or spread not in SPREADS
                or not isinstance(mode, str) or mode not in {'test', 'live'}
                or not isinstance(hosting, str) or hosting not in {'manual', 'guided'}):
            raise ValueError('请选择单牌或三牌，以及测试或正式模式')
        with self.database() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM tarot_sessions WHERE state='open'").fetchone():
                raise ValueError('请先结束当前塔罗场次')
            self._make_room(db, 'tarot_sessions')
            key = uuid.uuid4().hex[:16]
            db.execute('INSERT INTO tarot_sessions VALUES (?,?,?,?,?,?,?,?)',
                       (key, mode, spread, 'open', self.clock(), None, secrets.token_hex(32), hosting))
            return self._session_view(db, self._session(db, key))

    def select(self, session_id, entry_id):
        with self.database() as db:
            db.execute('BEGIN IMMEDIATE')
            session = self._session(db, session_id)
            current = db.execute("SELECT 1 FROM tarot_readings WHERE session_id=? AND state!='finished'",
                                 (session_id,)).fetchone()
            if current:
                raise ValueError('请先结束当前观众的解读，再选择下一位')
            entry = db.execute('SELECT * FROM tarot_entries WHERE id=? AND session_id=?',
                               (self._id(entry_id), session_id)).fetchone()
            if entry is None or entry['state'] != 'waiting':
                raise ValueError('请选择仍在等待的观众')
            self._make_room(db, 'tarot_readings')
            deck = list(range(len(_MAJORS)))
            rng = secrets.SystemRandom()
            rng.shuffle(deck)
            deck = [{'card': card, 'orientation': 'reversed' if rng.getrandbits(1) else 'upright'} for card in deck]
            reading = dict(id=uuid.uuid4().hex[:16], session_id=session_id, entry_id=entry_id,
                           user=entry['user'], question=entry['question'], state='selecting',
                           spread=session['spread'], card_count=len(SPREADS[session['spread']]),
                           cards=[], positions=[], attempt_id='', error='', created=self.clock(), deck=deck)
            reading.update(hosting=session['hosting'], stage='reading', paused=False,
                           wait_deadline=None, revealed_count=0)
            if session['hosting'] == 'guided':
                reading.update(state='welcoming', stage='welcome')
            db.execute('INSERT INTO tarot_readings VALUES (?,?,?,?,?,?)',
                       (reading['id'], session_id, entry_id, reading['state'], reading['created'], json.dumps(reading, ensure_ascii=False)))
            db.execute("UPDATE tarot_entries SET state='selected' WHERE id=?", (entry_id,))
            return self._reading_view(reading)

    @staticmethod
    def _positions(positions, count):
        if (not isinstance(positions, list) or len(positions) != count
                or any(type(position) is not int or not 1 <= position <= 22 for position in positions)
                or len(set(positions)) != count):
            raise ValueError(f'请选择 {count} 个不同的牌位，编号为 1–22')
        return positions[:]

    def _draw(self, db, reading, positions):
        positions = self._positions(positions, reading['card_count'])
        if reading['state'] != 'selecting':
            if reading['positions'] == positions:
                return self._reading_view(reading)
            raise ValueError('牌面已经固定，不能重新抽牌；可重试同一牌面的解读')
        cards = []
        for label, position in zip(SPREADS[reading['spread']], positions):
            chosen = reading['deck'][position - 1]
            number, orientation = chosen['card'], chosen['orientation']
            card = _MAJORS[number]
            keywords, meaning = (card[1], card[2]) if orientation == 'upright' else (card[3], card[4])
            cards.append(dict(id=f'major-{number:02d}', name=card[0], orientation=orientation,
                              position=position, label=label, keywords=keywords.split('、'), meaning=meaning))
        reading.update(state='revealed', positions=positions, cards=cards, stage='reading',
                       wait_deadline=None, revealed_count=0 if reading.get('hosting') == 'guided' else len(cards))
        self._save_reading(db, reading)
        return self._reading_view(reading)

    def draw(self, session_id, reading_id, positions):
        with self.database() as db:
            db.execute('BEGIN IMMEDIATE')
            self._session(db, session_id)
            reading = self._reading(db, session_id, reading_id)
            if reading['state'] == 'finished':
                raise ValueError('这位观众的解读已结束')
            return self._draw(db, reading, positions)

    def finish(self, session_id, reading_id):
        with self.database() as db:
            db.execute('BEGIN IMMEDIATE')
            self._session(db, session_id)
            reading = self._reading(db, session_id, reading_id)
            reading.update(state='finished', attempt_id='', paused=False, wait_deadline=None)
            self._save_reading(db, reading)
            db.execute("UPDATE tarot_entries SET state='done' WHERE id=?", (reading['entry_id'],))
            return self._reading_view(reading)

    def close(self, session_id):
        with self.database() as db:
            db.execute('BEGIN IMMEDIATE')
            session = self._session(db, session_id, open_required=False)
            if session['state'] == 'open':
                for row in db.execute("SELECT * FROM tarot_readings WHERE session_id=? AND state!='finished'", (session_id,)).fetchall():
                    reading = json.loads(row['payload'])
                    reading.update(state='finished', attempt_id='', paused=False, wait_deadline=None)
                    self._save_reading(db, reading)
                db.execute("UPDATE tarot_entries SET state='done' WHERE session_id=?", (session_id,))
                db.execute("UPDATE tarot_sessions SET state='closed',closed=? WHERE id=?", (self.clock(), session_id))
            return self._session_view(db, self._session(db, session_id, open_required=False))

    def _identity(self, session, event):
        if session['mode'] == 'test':
            if not (event.forced is True and getattr(event, 'owner_control', False) is True):
                raise ValueError('测试场次只接收本地主控的测试参与')
            key = 'test:' + _text(event.user, 40, '测试观众')
        else:
            key = event.viewer_key
            if (event.forced or not isinstance(key, str) or len(key) > 256
                    or not re.fullmatch(r'(?:bili:|bili-hash:)\S+', key)
                    or any(unicodedata.category(char).startswith('C') for char in key)):
                raise ValueError('正式塔罗只接收有稳定账号标识的 B 站观众')
        return hashlib.sha256((session['salt'] + key).encode()).hexdigest()

    @staticmethod
    def _receipt(ok, message, *, changed=False, **extra):
        return {'ok': ok, 'changed': changed, 'message': message, **extra}

    def handle_event(self, event):
        if event.source != 'danmaku' or event.kind != 'chat':
            return None
        command = tarot_command(event.text)
        if command is None:
            return self._guided_input(event)
        from .safety import is_blocked, is_crisis
        if is_crisis(event.text):
            self._end_crisis_reading(event)
            return None
        if is_blocked(event.text) or is_blocked(str(event.user or '')):
            return self._receipt(False, '这个问题不适合在茶会里展开，请换一个日常问题。')
        try:
            with self.database() as db:
                db.execute('BEGIN IMMEDIATE')
                session = db.execute("SELECT * FROM tarot_sessions WHERE state='open'").fetchone()
                if session is None:
                    return self._receipt(False, '当前没有进行中的塔罗场次')
                expected_session = getattr(event, '_tarot_test_session', None)
                if expected_session is not None and expected_session != session['id']:
                    return self._receipt(False, '测试页面已过期，请刷新场次')
                identity = self._identity(session, event)
                try:
                    valid_time = (type(event.ts) in (int, float) and math.isfinite(event.ts)
                                  and session['created'] <= event.ts <= self.clock() + 5)
                except (OverflowError, TypeError, ValueError):
                    valid_time = False
                if not valid_time:
                    return self._receipt(False, '这条消息不属于当前塔罗场次')
                session_id = session['id']
                if command[0] == 'join':
                    question = _text(command[1], 160, '塔罗问题')
                    old = db.execute('SELECT * FROM tarot_entries WHERE session_id=? AND viewer=?',
                                     (session_id, identity)).fetchone()
                    if old:
                        return self._receipt(True, '本场已经报名，重复消息已忽略', session_id=session_id,
                                             entry_id=old['id'], entry=self._entry_view(old))
                    if db.execute('SELECT COUNT(*) FROM tarot_entries WHERE session_id=?', (session_id,)).fetchone()[0] >= self.MAX_QUEUE:
                        return self._receipt(False, '本场报名已满 30 人，请等待下一场', session_id=session_id)
                    self._make_room(db, 'tarot_entries')
                    key, user = uuid.uuid4().hex[:16], _text(event.user or '观众', 40, '观众称呼')
                    db.execute('INSERT INTO tarot_entries VALUES (?,?,?,?,?,?,?)',
                               (key, session_id, identity, user, question, 'waiting', event.ts))
                    entry = self._entry_view(db.execute('SELECT * FROM tarot_entries WHERE id=?', (key,)).fetchone())
                    return self._receipt(True, '已加入塔罗报名，等待主播选择', changed=True,
                                         session_id=session_id, entry_id=key, entry=entry)
                row = db.execute("SELECT * FROM tarot_readings WHERE session_id=? ORDER BY created DESC,rowid DESC LIMIT 1", (session_id,)).fetchone()
                if row is None:
                    return self._receipt(False, '请等待主播先选择一位观众')
                reading = json.loads(row['payload'])
                selected = db.execute('SELECT viewer FROM tarot_entries WHERE id=?', (reading['entry_id'],)).fetchone()
                if selected['viewer'] != identity:
                    return self._receipt(False, '现在由被选中的观众选牌，请稍等')
                if event.ts < reading['created']:
                    return self._receipt(False, '这条选牌消息早于本次选人，已忽略')
                if reading.get('hosting') == 'guided':
                    if reading.get('paused'):
                        return self._receipt(False, '这一轮已暂停，请等主播继续。')
                    if reading['state'] == 'selecting' and not self._window_accepts(db, reading, event.ts):
                        return self._receipt(False, '选牌时间已结束，正在收起这一轮。')
                choice = unicodedata.normalize('NFKC', command[1])
                if len(choice) > 80 or not re.fullmatch(r'[0-9]+(?:[\s,，]+[0-9]+)*', choice):
                    raise ValueError('请发送“抽牌 7”或“三牌：抽牌 3 12 18”这样的数字指令')
                positions = [int(item) for item in re.split(r'[\s,，]+', choice)]
                if reading['state'] == 'finished':
                    return self._receipt(False, '这位观众的解读已结束')
                changed = reading['state'] == 'selecting'
                result = self._draw(db, reading, positions)
                return self._receipt(True, '牌面已固定' if changed else '重复选牌已忽略，牌面保持不变',
                                     changed=changed, session_id=session_id, reading_id=reading['id'], reading=result)
        except ValueError as error:
            return self._receipt(False, str(error))

    def _test_event(self, session_id, user, text):
        with self.database() as db:
            session = self._session(db, session_id)
            if session['mode'] != 'test':
                raise ValueError('本地测试不能参与正式场次')
        event = Event('danmaku', 'chat', text, user=_text(user, 40, '测试观众'), forced=True, ts=self.clock())
        event.owner_control, event._tarot_test_session = True, session_id
        return self.handle_event(event)

    def test_join(self, session_id, user, question):
        return self._test_event(session_id, user, '塔罗 ' + _text(question, 160, '塔罗问题'))

    def test_choose(self, session_id, user, positions):
        with self.database() as db:
            session = self._session(db, session_id)
            positions = self._positions(positions, len(SPREADS[session['spread']]))
        return self._test_event(session_id, user, '抽牌 ' + ' '.join(map(str, positions)))

    def status(self, *, public=False):
        with self.database() as db:
            session = db.execute('SELECT * FROM tarot_sessions ORDER BY created DESC,rowid DESC LIMIT 1').fetchone()
            result = dict(server_time=self.clock(), current=None, entries=[], reading=None, history=[])
            if session:
                result['current'] = self._session_view(db, session)
                if not public:
                    result['entries'] = [self._entry_view(row) for row in db.execute(
                        'SELECT * FROM tarot_entries WHERE session_id=? ORDER BY received,rowid', (session['id'],))]
                reading = db.execute('SELECT payload FROM tarot_readings WHERE session_id=? ORDER BY created DESC,rowid DESC LIMIT 1', (session['id'],)).fetchone()
                if reading:
                    result['reading'] = self._reading_view(json.loads(reading['payload']), public=public)
            if not public:
                result['history'] = [self._reading_view(json.loads(row['payload'])) for row in db.execute(
                    "SELECT payload FROM tarot_readings WHERE state='finished' ORDER BY created DESC,rowid DESC LIMIT 10")]
            return result

    def live_active(self):
        """Cheap host arbitration gate; no question, history or hidden deck read."""
        with self.database() as db:
            return db.execute("SELECT 1 FROM tarot_sessions WHERE mode='live' AND state='open' LIMIT 1").fetchone() is not None

    def interpretation_event(self, session_id, reading_id):
        with self.database() as db:
            db.execute('BEGIN IMMEDIATE')
            session = self._session(db, session_id)
            reading = self._reading(db, session_id, reading_id)
            if reading.get('hosting') == 'guided':
                return self._claim_guided(db, session, reading, retry=True)
            if reading['state'] not in {'revealed', 'failed', 'interrupted'} or not reading['cards']:
                raise ValueError('请先完成选牌；同一解读不能重复排队')
            reading.update(state='queued', attempt_id=uuid.uuid4().hex, error='')
            self._save_reading(db, reading)
            facts = {key: reading[key] for key in ('user', 'question', 'spread', 'cards', 'positions')}
            facts['mode'] = session['mode']
        event = Event('system', 'ambient', reading['question'], user=reading['user'],
                      priority=PRIO_AMBIENT, forced=True, ts=self.clock())
        event.tarot_session_id, event.tarot_reading_id = session_id, reading_id
        event.tarot_attempt_id = reading['attempt_id']
        event.tarot_context = json.dumps(facts, ensure_ascii=False)
        return event

    def _matching_attempt(self, db, event):
        field = event.get if isinstance(event, dict) else lambda key, default=None: getattr(event, key, default)
        session_id, reading_id, attempt = (field(key, '') for key in ('tarot_session_id', 'tarot_reading_id', 'tarot_attempt_id'))
        if not all(isinstance(value, str) and value for value in (session_id, reading_id, attempt)):
            return None
        session = db.execute("SELECT 1 FROM tarot_sessions WHERE id=? AND state='open'", (session_id,)).fetchone()
        if not session:
            return None
        row = db.execute('SELECT payload FROM tarot_readings WHERE session_id=? ORDER BY created DESC,rowid DESC LIMIT 1', (session_id,)).fetchone()
        reading = json.loads(row['payload']) if row else None
        if (reading and reading['id'] == reading_id and reading['attempt_id'] == attempt
                and reading['state'] in {'queued', 'generated'}):
            return reading
        return None

    def valid_event(self, event):
        with self.database() as db:
            return self._matching_attempt(db, event) is not None

    def mark_result(self, event, status, error=''):
        if status not in {'generated', 'failed', 'interrupted'}:
            raise ValueError('塔罗解读结果状态无效')
        with self.database() as db:
            db.execute('BEGIN IMMEDIATE')
            reading = self._matching_attempt(db, event)
            if reading is None:
                return False
            reading.update(state=status, error=str(error or '')[:160])
            self._save_reading(db, reading)
            return self._reading_view(reading)
