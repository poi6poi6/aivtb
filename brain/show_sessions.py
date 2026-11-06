"""Host-owned show cards and factual recaps. No model calls or audience identities."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import time
import uuid

from .live_polls import short_text

PROGRAM_STAGES = ('intro', 'vote', 'sketch', 'discussion', 'naming', 'finished')
PROGRAM_LABELS = {'intro': '介绍委托', 'vote': '观众投票', 'sketch': '准备草稿',
                  'discussion': '看画讨论', 'naming': '命名收束', 'finished': '本轮结束'}


def program_context(show, *, armed=False, poll=None, drawing=None, pending_count=None, now=None):
    """Build audience-safe facts separately from private card and executor data."""
    now = time.time() if now is None else now
    program = (show or {}).get('program') or {}
    card = next((c for c in (show or {}).get('cards', []) if c['id'] == program.get('card_id')), None)
    live = bool(show and show['state'] == 'open' and program.get('live_mode') and armed)
    stage = program.get('stage', 'intro') if card else 'waiting'
    context = {'live_mode': live, 'show_id': (show or {}).get('id'), 'card_id': (card or {}).get('id'),
               'stage': stage, 'stage_label': PROGRAM_LABELS.get(stage, '等待选择栏目'),
               'theme': (show or {}).get('theme', ''), 'topic': (card or {}).get('title', ''),
               'next_label': '', 'status': '等待主播选择本轮栏目', 'status_code': 'waiting',
               'pending_count': pending_count, 'updated_at': now, 'fresh': True}
    if not card:
        return context
    index = PROGRAM_STAGES.index(stage)
    context['next_label'] = PROGRAM_LABELS[PROGRAM_STAGES[index + 1]] if index < 5 else '下一张栏目卡片'
    status, code, fresh = '介绍本轮委托与创作主题', 'intro', True
    if stage == 'vote':
        if not program.get('poll_id'):
            status, code = '等待主播发起并关联投票', 'poll_missing'
        elif poll is None:
            status, code, fresh = '投票状态暂未确认', 'poll_unknown', False
        elif poll['state'] == 'open':
            status, code = f"{'测试投票 · ' if poll['mode'] == 'test' else ''}投票进行中 · {poll['total']} 人参与", 'poll_open'
        elif poll['state'] == 'cancelled':
            status, code = '本轮投票已取消，等待主播安排', 'poll_cancelled'
        elif not poll['total']:
            status, code = '本轮没有有效票，等待主播选择', 'poll_empty'
        elif len(poll['winners']) > 1:
            status, code = '本轮出现平票，等待主播选择', 'poll_tied'
        else:
            winner = next(o['label'] for o in poll['options'] if o['number'] in poll['winners'])
            status, code = '最高票：' + winner + ' · 等待主播确认下一步', 'poll_closed'
        if poll is not None and poll['mode'] == 'test' and code != 'poll_open':
            status = '测试投票 · ' + status
        if (poll is not None and card.get('poll_origin_show') not in {None, show['id']}
                and card.get('poll', {}).get('id') == poll['id']):
            status = '沿用往期结果 · ' + status
    elif stage in {'sketch', 'discussion'}:
        if not card.get('drawing_root'):
            if card.get('submission'):
                status, code, fresh = '委托提交状态待确认', 'drawing_unknown', False
            else:
                status, code = '等待主播提交绘画委托', 'drawing_missing'
        elif not drawing:
            status, code, fresh = '作品进度暂未确认', 'drawing_unknown', False
        else:
            state = drawing.get('state')
            ready = drawing.get('ready_stage')
            code = 'drawing_' + str(state)
            if state == 'queued':
                status = '绘画委托排队中'
            elif state == 'running':
                status = '正在准备' + ('草稿' if drawing.get('stage') == 'sketch' else '上色与细节')
            elif state == 'waiting_approval':
                status = '等待主播确认下一步'
            elif state == 'succeeded' and ready:
                status = '草稿已准备，可一起讨论' if ready == 'sketch' else '上色版本已准备，可一起欣赏'
            elif state in {'failed', 'cancelled', 'blocked'}:
                status = '本轮绘画暂未完成，等待主播安排'
            else:
                status, code, fresh = '作品进度暂未确认', 'drawing_unknown', False
            if ready and state != 'succeeded':
                status += ' · 已有' + ('草稿' if ready == 'sketch' else '上一版作品')
    elif stage == 'naming':
        status, code = ('本轮命名：' + card['name'], 'named') if card.get('name') else ('一起为本轮内容想个名字，等待主播记录', 'naming')
    elif stage == 'finished':
        status, code = '本轮栏目已结束，等待下一轮安排', 'finished'
    context.update(status=status, status_code=code, fresh=fresh)
    return context


def public_program(context):
    if not context.get('live_mode'):
        return {'active': False, 'theme': '', 'title': '', 'stage_label': '栏目休息中',
                'status': '等待主播开启直播节目单', 'next_label': '', 'pending_count': None,
                'updated_at': context.get('updated_at'), 'fresh': context.get('fresh', True)}
    return {'active': True, 'title': context['topic'], **{key: context[key] for key in
        ('theme', 'stage_label', 'status', 'next_label', 'pending_count', 'updated_at', 'fresh')}}


def optional_text(value, limit, label):
    if not isinstance(value, str):
        raise ValueError(f'{label}须为文本')
    value = value.strip()
    if not value:
        return ''
    short_text(value.replace('\r', ' ').replace('\n', ' ').replace('\t', ' '), limit, label)
    return value


def poll_commission(poll, choice=None):
    if poll['state'] != 'closed':
        raise ValueError('请先结束投票；取消的投票不能生成委托')
    winners = poll['winners']
    if choice is None and len(winners) == 1:
        choice = winners[0]
    if type(choice) is not int or choice not in [o['number'] for o in poll['options']]:
        raise ValueError('平票或零票时请由主播明确选择一个选项')
    option = next(o for o in poll['options'] if o['number'] == choice)
    decision = ('观众最高票' if len(winners) == 1 and choice == winners[0]
                else '主播选择（零票）' if not poll['total'] else '主播选择')
    return {'poll': deepcopy(poll), 'choice': choice, 'decision': decision,
            'prompt': f"{poll['program']}的绘画委托。主题问题：{poll['question']}。"
                      f"本次采用：{option['label']}。先制作可修改的草稿，待主播确认后再上色。"}


class ShowSessions:
    def __init__(self, path, *, clock=time.time):
        self.path, self.clock = Path(path), clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.database() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS shows (
                id TEXT PRIMARY KEY, state TEXT NOT NULL, created REAL NOT NULL,
                version INTEGER NOT NULL, payload TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_show ON shows(state) WHERE state='open';''')

    @contextmanager
    def database(self):
        db = sqlite3.connect(self.path, timeout=2)
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, key):
        if not isinstance(key, str):
            raise ValueError('请选择一场直播')
        with self.database() as db:
            row = db.execute('SELECT payload FROM shows WHERE id=?', (key,)).fetchone()
        if not row:
            raise ValueError('直播场次不存在')
        return json.loads(row[0])

    def status(self):
        with self.database() as db:
            rows = db.execute('SELECT payload FROM shows ORDER BY created DESC, rowid DESC LIMIT 31').fetchall()
        shows = [json.loads(r[0]) for r in rows]
        return {'current': next((s for s in shows if s['state'] == 'open'), None),
                'history': [s for s in shows if s['state'] == 'closed'][:30]}

    def current(self):
        with self.database() as db:
            row = db.execute("SELECT payload FROM shows WHERE state='open'").fetchone()
        return json.loads(row[0]) if row else None

    def program_command(self, key, version, command, *, card_id=None, poll_id=None):
        """Advance host-selected stages only; this never starts tasks or polls."""
        show = self.editable(key, version)
        program = show.setdefault('program', {'live_mode': False, 'card_id': None,
                                            'stage': 'intro', 'poll_id': None, 'changed': self.clock()})
        if command in {'enable', 'disable'}:
            program['live_mode'] = command == 'enable'
        elif command == 'start_card':
            card = self.card(show, card_id)
            program.update(card_id=card['id'], stage='intro', poll_id=card.get('poll', {}).get('id'))
        elif command == 'attach_poll':
            self.card(show, program['card_id'])
            if poll_id is not None and (not isinstance(poll_id, str) or len(poll_id) != 16):
                raise ValueError('投票编号无效')
            program['poll_id'] = poll_id
        elif command in {'next_stage', 'finish_card'}:
            self.card(show, program['card_id'])
            index = PROGRAM_STAGES.index(program['stage'])
            program['stage'] = ('finished' if command == 'finish_card'
                                else PROGRAM_STAGES[min(index + 1, len(PROGRAM_STAGES) - 1)])
        else:
            raise ValueError('未知节目单操作')
        program['changed'] = self.clock()
        return self.save(show)

    def start(self, theme, previous=None):
        theme = short_text(theme, 100, '本场主题')
        show = {'id': uuid.uuid4().hex[:16], 'theme': theme, 'state': 'open', 'version': 1,
                'created': self.clock(), 'closed': None, 'previous': previous, 'cards': [], 'recap': ''}
        if previous:
            old = self.get(previous)
            if old['state'] != 'closed':
                raise ValueError('请先收播，再接续下一场')
            for card in old['cards']:
                if card['next_todo']:
                    continued = deepcopy(card)
                    continued.update(id=uuid.uuid4().hex[:16], continued_from=card['id'],
                                     continued_from_show=old['id'], next_todo='', note=card['next_todo'])
                    show['cards'].append(continued)
            if not show['cards']:
                raise ValueError('上场没有记录下次待办，请直接创建新场次')
        with self.database() as db:
            if db.execute("SELECT 1 FROM shows WHERE state='open'").fetchone():
                raise ValueError('已有正在进行的场次，请先收播')
            db.execute('INSERT INTO shows VALUES (?,?,?,?,?)',
                       (show['id'], 'open', show['created'], 1, json.dumps(show, ensure_ascii=False)))
        return show

    def editable(self, key, version):
        show = self.get(key)
        if show['state'] != 'open':
            raise ValueError('这场已收播，历史记录不能改写')
        if type(version) is not int or show['version'] != version:
            raise ValueError('栏目已由其他页面更新，请刷新后重试')
        return show

    def save(self, show):
        previous = show['version']
        show['version'] += 1
        with self.database() as db:
            changed = db.execute('UPDATE shows SET state=?, version=?, payload=? WHERE id=? AND version=?',
                (show['state'], show['version'], json.dumps(show, ensure_ascii=False), show['id'], previous))
            if changed.rowcount != 1:
                raise ValueError('栏目已由其他页面更新，请刷新后重试')
        return show

    @staticmethod
    def card(show, key):
        for card in show['cards']:
            if card['id'] == key:
                return card
        raise ValueError('栏目卡片不存在')

    def add(self, key, version, title, prompt='', *, poll=None, choice=None):
        show = self.editable(key, version)
        if len(show['cards']) >= 50:
            raise ValueError('一场最多保留 50 张栏目卡片')
        title = short_text(title, 100, '栏目主题')
        seed = poll_commission(poll, choice) if poll is not None else {}
        if poll and any(c.get('poll', {}).get('id') == poll['id'] for c in show['cards']):
            raise ValueError('这轮投票已有栏目卡片，请编辑原卡片')
        prompt = optional_text(prompt, 4000, '绘画要求') or seed.get('prompt', '')
        card = {'id': uuid.uuid4().hex[:16], 'title': title, 'prompt': prompt,
                'name': '', 'next_todo': '', 'note': '', 'drawing_root': None,
                'drawing': None, 'drawing_checked': None, 'continued_from': None, 'submission': None}
        card.update({k: seed[k] for k in ('poll', 'choice', 'decision') if k in seed})
        if poll is not None:
            card['poll_origin_show'] = show['id']
        show['cards'].append(card)
        return self.save(show)

    def edit(self, key, version, card_id, data):
        show = self.editable(key, version)
        card = self.card(show, card_id)
        for field, limit in (('title', 100), ('name', 100), ('next_todo', 1000), ('note', 1000), ('prompt', 4000)):
            if field in data:
                value = short_text(data[field], limit, '栏目主题') if field == 'title' else optional_text(data[field], limit, field)
                if field == 'prompt' and card['submission'] and value != card['prompt']:
                    raise ValueError('已提交的绘画要求请到画布里修订；提交未确认时请先核对')
                card[field] = value
        return self.save(show)

    def begin_submission(self, key, version, card_id):
        show = self.editable(key, version)
        card = self.card(show, card_id)
        if card['drawing_root']:
            raise ValueError('这张卡片已有绘画，请打开画布继续修改')
        if card['submission'] is None:
            prompt = optional_text(card['prompt'], 4000, '绘画要求')
            if not prompt:
                raise ValueError('请先填写绘画要求')
            card['submission'] = {'key': uuid.uuid4().hex, 'prompt': prompt, 'state': 'pending'}
            self.save(show)
        return deepcopy(card['submission'])

    def submitted(self, key, card_id, submission_key, job):
        # An unrelated card edit during RPC must not overwrite newer show data.
        show = self.get(key)
        card = self.card(show, card_id)
        if not card['submission'] or card['submission']['key'] != submission_key:
            raise ValueError('委托已变化，请刷新后核对')
        if job.get('kind') != 'drawing' or job.get('commission_key') != submission_key:
            raise ValueError('任务服务未确认这份绘画委托')
        if card['submission']['state'] == 'submitted':
            return show
        if show['state'] != 'open':
            raise ValueError('这场已收播，请到任务台核对委托')
        card['drawing_root'] = job.get('drawing_root') or job['id']
        card['submission']['state'] = 'submitted'
        return self.save(show)

    def record_drawings(self, key, version, observations, *, close=False, allow_unconfirmed=False):
        show = self.editable(key, version)
        if close and not allow_unconfirmed and any(c['submission'] and c['submission']['state'] == 'pending' for c in show['cards']):
            raise ValueError('还有提交未确认的委托，请先点击核对提交，再收播')
        for card in show['cards']:
            if card['drawing_root'] in observations:
                observation = observations[card['drawing_root']]
                card['drawing'] = observation
                card['drawing_checked'] = self.clock()
        if close:
            show.update(state='closed', closed=self.clock())
            show['recap'] = self.recap(show)
        return self.save(show)

    @staticmethod
    def recap(show):
        lines = [f"本场主题：{show['theme']}", f"栏目数量：{len(show['cards'])}"]
        for i, card in enumerate(show['cards'], 1):
            lines.append(f"{i}. {card['title']}")
            if 'poll' in card:
                p = card['poll']
                choice = next(o['label'] for o in p['options'] if o['number'] == card['choice'])
                source = card.get('poll_origin_show')
                prefix = f"沿用上场投票结果（来源场次 {source}，不计为本场参与）" if source and source != show['id'] else '投票'
                lines.append(f"{prefix}{'（测试）' if p['mode'] == 'test' else ''}：{p['question']}；原轮次 {p['total']} 人参与；采用“{choice}”（{card['decision']}）。")
            if card['drawing_root']:
                d = card.get('drawing') or {}
                if d.get('error'):
                    lines.append('绘画：收播时状态未确认。')
                else:
                    latest, ready = d.get('latest') or {}, d.get('drawing') or {}
                    labels = {'succeeded': '任务已完成', 'running': '执行中', 'queued': '排队中',
                              'failed': '未完成', 'cancelled': '已取消', 'blocked': '暂不可执行', 'waiting_approval': '等待审批'}
                    lines.append('绘画：' + labels.get(latest.get('state'), '状态未确认') +
                                 ('；已有' + ('草稿' if ready.get('stage') == 'sketch' else '上色版本') if ready else '') + '。')
            elif card['submission'] and card['submission']['state'] == 'pending':
                lines.append('绘画：提交未确认；这不表示后台任务未创建或已取消，请到任务台核对。')
            else:
                lines.append('绘画：未提交。')
            if card['name']:
                lines.append('主播命名：' + card['name'])
            if card['note']:
                lines.append('主播记录：' + card['note'])
            if card['next_todo']:
                lines.append('下次待办：' + card['next_todo'])
        return '\n'.join(lines)
