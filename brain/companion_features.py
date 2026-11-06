"""Local companion capabilities, sharing the persona and delivery lifecycle."""
from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from collections import OrderedDict
from dataclasses import asdict
from pathlib import Path

from .events import Event, PRIO_IDLE
from .followups import FollowupService, local_owner
from .image_understanding import ImageUnderstandingError
from .astra_vision import AstraVision, AstraVisionConfig
from .meme_library import MemeLibrary
from .persistence import atomic_json_write


class CompanionFeatures:
    def __init__(self, root, *, personality=None, clock=time.time, codex_command='codex'):
        from .companion_journal import CompanionJournal
        self.folder = Path(root) / 'data' / 'companion'
        self.path = self.folder / 'settings.json'
        self.clock, self.personality = clock, personality
        self.codex_command = codex_command
        self.settings = {'memes_enabled': True, 'auto_memes': False, 'journal_enabled': True,
                         'vision': asdict(AstraVisionConfig())}
        if self.path.exists():
            if self.path.stat().st_size > 16384:
                raise ValueError('陪伴设置文件过大')
            saved = json.loads(self.path.read_text('utf-8'))
            self._validate_settings(saved)
            self.settings.update(saved)
        self.memes = MemeLibrary(self.folder / 'memes')
        self.vision = AstraVision(AstraVisionConfig(**self.settings['vision']), command=self.codex_command)
        self.followups = FollowupService(self.folder / 'followups.json', clock=clock)
        self.journal = CompanionJournal(self.folder / 'journal.json', clock=clock)
        self._events = OrderedDict()
        self._deliveries = {}
        self._delivery_lock = asyncio.Lock()
        self._claims = {}
        self._activity_revision = 0
        self._voice_pending = OrderedDict()
        self._task = None
        self._closed = False
        self.last_activity = self.clock()
        self._last_meme = 0.0
        self.last_error = ''

    @staticmethod
    def _validate_settings(value):
        if not isinstance(value, dict) or set(value) - {'memes_enabled', 'auto_memes', 'journal_enabled', 'vision'}:
            raise ValueError('未知的陪伴设置')
        for name in ('memes_enabled', 'auto_memes', 'journal_enabled'):
            if name in value and type(value[name]) is not bool:
                raise ValueError('开关需要布尔值')
        if 'vision' in value:
            cfg = value['vision']
            if not isinstance(cfg, dict) or set(cfg) - set(asdict(AstraVisionConfig())):
                raise ValueError('视觉使用本地 GPT Astra 套餐，配置无效')
            AstraVision(AstraVisionConfig(**cfg))

    async def configure(self, changes):
        if self._closed:
            raise ValueError('陪伴能力已关闭')
        self._validate_settings(changes)
        if 'vision' in changes and set(changes['vision']) - {'enabled'}:
            raise ValueError('不能通过页面修改图片资源上限')
        settings = {**self.settings, **changes}
        settings['vision'] = {**self.settings['vision'], **changes.get('vision', {})}
        replacement = (AstraVision(AstraVisionConfig(**settings['vision']), command=self.codex_command)
                       if settings['vision'] != self.settings['vision'] else None)
        try:
            atomic_json_write(self.path, settings)
        except BaseException:
            if replacement is not None:
                await replacement.close()
            raise
        previous = self.vision
        if replacement is not None:
            self.vision = replacement
        self.settings = settings
        if replacement is not None:
            await previous.close()
        return await self.snapshot()

    async def snapshot(self):
        mood = getattr(self.personality, 'mood_snapshot', None)
        return {'settings': self.settings, 'vision': self.vision.snapshot(),
                'memes': self.memes.list_items(), 'followups': self.followups.summary(),
                'calendar': await self.followups.list_items(),
                'journal': await self.journal.list_entries(),
                'mood': mood() if callable(mood) else {}, 'error': self.last_error}

    async def describe_qq(self, attachments, text):
        if not attachments:
            return ''
        try:
            if not isinstance(attachments, list) or len(attachments) != 1:
                raise ValueError('只接受一张图片')
            item = attachments[0]
            encoded = item.get('data_base64') if isinstance(item, dict) else None
            if not isinstance(encoded, str) or len(encoded) > 7_000_000:
                raise ValueError('图片过大')
            data = base64.b64decode(encoded, validate=True)
            result = await self.vision.describe(data, question=text[:800], mime_type=item.get('content_type', ''))
            return result.context()
        except ImageUnderstandingError as error:
            return f'宿主图片读取结果：{error} 未获得视觉内容，不得描述或猜测图片。'
        except (ValueError, TypeError):
            return '宿主图片读取结果：图片无效或超过大小限制。没有获得视觉内容，不得猜测。'

    def pick_meme(self, text, emotion, reply):
        if not self.settings['memes_enabled']:
            return None
        if re.search(r'(?:别|不要|不用|不想|不需要|停止|取消)[^，。！？,!?;；]{0,12}(?:表情|斗图|配图)|'
                     r'(?:表情包|斗图)[^，。！？,!?;；]{0,6}(?:不要|不用|算了|停)', text):
            return None
        requested = (text.strip() in {'表情包', '斗图'} or bool(re.search(
            r'(?:发|来|丢|整|换|找|想看)[^，。！？,!?;；]{0,12}表情|(?:来|一起|开始).*斗图', text)))
        if not requested and (not self.settings['auto_memes'] or self.clock() - self._last_meme < 180):
            return None
        if not requested and (emotion in {'平静', '伤心', '委屈'} or re.search(r'难过|害怕|救命|出事|别发.*图', text)):
            return None
        matches = self.memes.search(text + ' ' + reply[:160], emotion=emotion, limit=1)
        if not matches and requested:
            matches = self.memes.search('', emotion=emotion, limit=1)
        if not matches:
            return None
        selected = self.memes.resolve_selection(matches[0]['id'])
        if selected:
            self._last_meme = self.clock()
        return selected

    def foreground(self):
        self.last_activity = self.clock()
        self._activity_revision += 1

    async def registered(self, event):
        if self._closed:
            return
        turn = getattr(event, 'ui_id', None)
        if turn is not None:
            self._events[turn] = event
            while len(self._events) > 128:
                old, old_event = self._events.popitem(last=False)
                await self.abandon(old_event, 'unknown')
                self._deliveries.pop(old, None)
        if event.kind != 'chat' or not local_owner(event):
            return
        if event.source == 'voice':
            if event.retract:
                return
            # VoiceRevisionWindow owns the transcript acceptance boundary. No
            # calendar action or diary survives until that host boundary fires.
            self._voice_pending.setdefault(turn, {'event': event, 'confirmed': False,
                                                  'discarded': False, 'diary_written': False})
            while len(self._voice_pending) > 128:
                self._voice_pending.popitem(last=False)
            if re.search(r'提醒我|叫我|喊我|跟我聊|和我聊|晚点.*聊|稍后.*聊|取消.*(?:提醒|日程|跟进)', event.text):
                event.companion_receipt = ('语音转写仍可纠错：相关日程操作尚未保存。'
                                           '请自然说明确认转写或进入下一轮后才会安排，不得声称已经创建或取消。')
            return
        if event.corrects is not None:
            return
        try:
            receipt = await self.followups.observe_event(event)
            if receipt:
                event.companion_receipt = ('本机日程操作回执（只能依据此结果说明是否已安排）：'
                                         + json.dumps(receipt, ensure_ascii=False)[:2400])
        except (ValueError, OSError):
            event.companion_receipt = '本机日程操作未完成，不得声称已安排提醒。请在陪伴能力页检查。'

    async def confirm_voice(self, event):
        """Called only by the host's voice-confirmation/advance boundary."""
        turn = getattr(event, 'ui_id', None)
        async with self._delivery_lock:
            pending = self._voice_pending.get(turn)
            if (self._closed or event.source != 'voice' or not local_owner(event)
                    or pending is None or pending.get('discarded')
                    or local_owner(pending['event']) != local_owner(event)):
                return None
            pending['confirmed'] = True
            if 'calendar_receipt' not in pending:
                try:
                    receipt = await self.followups.observe_event(event, reference_time=event.ts)
                except (ValueError, OSError):
                    receipt = {'status': 'failed', 'reply': '这条语音的日程操作未完成；请检查时间或在陪伴能力页重新安排。'}
                if not receipt or receipt.get('status') != 'failed':
                    pending['calendar_receipt'] = receipt
                pending['last_receipt'] = receipt
            if (pending.get('delivery') and not pending['diary_written']
                    and not pending.get('journal_blocked') and self.settings['journal_enabled']):
                text, emotion = pending['delivery']
                try:
                    await self.journal.record(pending['event'], text, emotion)
                    pending['diary_written'] = True
                except (ValueError, OSError):
                    # Calendar and diary are separate operations. A diary write
                    # failure must not turn a real schedule receipt into a lie.
                    self.last_error = '语音已确认，但私人日记保存未完成，请检查陪伴能力页。'
            receipt = pending.get('calendar_receipt', pending.get('last_receipt'))
            if receipt:
                event.companion_receipt = ('已确认语音的本机日程回执（只能依据此结果说明是否已安排）：'
                                           + json.dumps(receipt, ensure_ascii=False)[:2400])
            return receipt

    async def discard_voice(self, turn):
        """A correction/retraction invalidates all late receipts for that transcript."""
        async with self._delivery_lock:
            pending = self._voice_pending.get(turn)
            if pending is not None and not pending.get('confirmed'):
                pending.update(discarded=True, delivery=None)
            record = self._deliveries.get(turn)
            if record is not None:
                record['failed'] = True

    async def _record_journal_delivery(self, event, text, emotion):
        if event.source != 'voice':
            await self.journal.record(event, text, emotion)
            return
        pending = self._voice_pending.get(getattr(event, 'ui_id', None))
        if pending is None or pending.get('discarded') or pending.get('journal_blocked'):
            return
        pending['delivery'] = (text, emotion)
        if pending.get('confirmed') and not pending['diary_written']:
            await self.journal.record(pending['event'], text, emotion)
            pending['diary_written'] = True

    def context_for(self, event):
        pieces = [str(getattr(event, 'image_context', ''))[:6000],
                  str(getattr(event, 'companion_receipt', ''))[:2600],
                  str(getattr(event, 'companion_prior_receipt', ''))[:2600]]
        if local_owner(event):
            pieces.append(self.followups.context_for(event))
            if self.settings['journal_enabled']:
                pieces.append(self.journal.context_for(event))
        if re.search(r'表情包|斗图|发.*表情', event.text):
            pieces.append('表情包通道已启用，宿主会依据本轮文字和情绪选取图库图片；正文自然回应，不输出路径、编号或声称图片已经送达。'
                          if self.settings['memes_enabled'] else '表情包通道已关闭，不要声称发出了图片。')
        return '\n'.join(piece for piece in pieces if piece)

    async def generated(self, event, text, emotion, *, segments=0, successful=True):
        turn = getattr(event, 'ui_id', None)
        if (self._closed or type(turn) is not int or turn < 0
                or type(segments) is not int or not 0 <= segments <= 256):
            return
        async with self._delivery_lock:
            if self._closed:
                return
            record = self._deliveries.setdefault(turn, {'played': set(), 'failed': False})
            if record.get('finished'):
                return
            record.update(event=event, text=text, emotion=emotion, expected=segments,
                          failed=record['failed'] or not successful)
            token = getattr(event, 'companion_followup_token', '')
            if token in self._claims:
                self._claims[token].update(state='playing', changed_at=self.clock())
            await self._finish_delivery(turn)

    async def played(self, turn, segment, completed):
        if (self._closed or type(turn) is not int or turn < 0 or type(segment) is not int
                or not 0 <= segment < 256 or type(completed) is not bool):
            return
        async with self._delivery_lock:
            if self._closed:
                return
            record = self._deliveries.setdefault(turn, {'played': set(), 'failed': False})
            if record.get('finished'):
                return
            if turn in self._events:
                record.setdefault('event', self._events[turn])
            if completed:
                record['played'].add(segment)
            else:
                record['failed'] = True
            await self._finish_delivery(turn)

    async def _finish_delivery(self, turn):
        record = self._deliveries.get(turn)
        if not record or 'expected' not in record or record.get('finished'):
            return
        if not record['failed'] and not set(range(record['expected'])).issubset(record['played']):
            return
        event = record['event']
        token = getattr(event, 'companion_followup_token', '')
        if token:
            await self.followups.resolve(token, 'failed' if record['failed'] else 'delivered')
            self._claims.pop(token, None)
        if (not record['failed'] and not record.get('journal_blocked') and event.kind == 'chat' and local_owner(event)
                and self.settings['journal_enabled']):
            await self._record_journal_delivery(event, record['text'], record['emotion'])
        # Durable state must succeed before duplicate receipts are suppressed.
        record['finished'] = True
        while len(self._deliveries) > 128:
            self._deliveries.pop(next(iter(self._deliveries)))

    def followup_current(self, event):
        """Read-only validity gate shared by generation, synthesis and playback."""
        field = event.get if isinstance(event, dict) else lambda name, default=None: getattr(event, name, default)
        token = field('companion_followup_token', '')
        if not token:
            return field('kind') != 'followup'
        version = field('companion_activity_revision')
        return (not self._closed and self.followups.data['settings']['enabled']
                and self.followups.is_claim_active(token)
                and (version == self._activity_revision if version is not None
                     else self.last_activity <= field('ts', 0)))

    def is_followup_turn(self, turn):
        event = self._events.get(turn) if type(turn) is int else None
        return bool(event is not None and getattr(event, 'companion_followup_token', ''))

    def pending_followup_events(self):
        """Snapshot only; callers may invalidate these events while iterating."""
        candidates = [claim['event'] for claim in self._claims.values()]
        candidates += [event for turn, event in self._events.items()
                       if getattr(event, 'companion_followup_token', '')
                       and not self._deliveries.get(turn, {}).get('finished')]
        return tuple({id(event): event for event in candidates}.values())

    async def valid_followup(self, event):
        token = getattr(event, 'companion_followup_token', '')
        if not token:
            return True
        valid = self.followup_current(event)
        if not valid:
            await self.followups.resolve(token, 'cancelled')
            self._claims.pop(token, None)
        elif token in self._claims:
            self._claims[token].update(state='generating', changed_at=self.clock())
        return valid

    async def abandon(self, event, outcome='cancelled'):
        """Host hook for cancellation, failure or a known queue drop, even before generated()."""
        if outcome not in {'cancelled', 'failed', 'unknown'}:
            raise ValueError('未知的跟进中止结果')
        async with self._delivery_lock:
            turn = getattr(event, 'ui_id', None)
            if type(turn) is int:
                record = self._deliveries.setdefault(turn, {'played': set(), 'failed': True})
                if not record.get('finished'):
                    record.update(event=event, failed=True, finished=True)
            token = getattr(event, 'companion_followup_token', '')
            if token:
                await self.followups.resolve(token, outcome)
                self._claims.pop(token, None)

    async def forget_owner(self, owner):
        if owner:
            async with self._delivery_lock:
                await self.journal.clear_owner(owner)
                # Forgetting a memory is not a request to cancel appointments.
                for record in self._deliveries.values():
                    if local_owner(record.get('event')) == owner:
                        record['failed'] = True
                for pending in self._voice_pending.values():
                    if local_owner(pending.get('event')) == owner:
                        pending.update(discarded=True, delivery=None)

    async def apply_memory_change(self, memory):
        """Invalidate only related diary receipts; calendar requests stay valid."""
        async with self._delivery_lock:
            await self.journal.apply_memory_change(memory)
            for turn, record in self._deliveries.items():
                event = record.get('event') or self._events.get(turn)
                if self.journal.memory_change_matches(memory, event, record.get('text', '')):
                    record['journal_blocked'] = True
            for pending in self._voice_pending.values():
                reply = pending.get('delivery', ('', ''))
                reply = reply[0] if reply else ''
                if self.journal.memory_change_matches(memory, pending.get('event'), reply):
                    pending.update(journal_blocked=True, delivery=None)

    def start(self, queue, *, idle, connected):
        if not self._closed and self._task is None:
            self._task = asyncio.create_task(self._run(queue, idle, connected), name='companion-followups')

    async def _settle_stale_claims(self):
        for token, claim in list(self._claims.items()):
            event = claim['event']
            if not self.followups.is_claim_active(token):
                self._claims.pop(token, None)
                continue
            age = self.clock() - claim['changed_at']
            if getattr(event, 'companion_activity_revision', None) != self._activity_revision:
                await self.abandon(event, 'cancelled')
            elif claim['state'] == 'queued' and age >= 30:
                await self.abandon(event, 'cancelled')
            elif claim['state'] != 'queued' and age >= 600:
                await self.abandon(event, 'unknown')

    async def _run(self, queue, idle, connected):
        while not self._closed:
            try:
                await self._settle_stale_claims()
                revision = self._activity_revision
                claim = await self.followups.claim_due(idle=bool(connected() and idle()),
                                                        last_activity_at=self.last_activity)
                if claim:
                    if (self._closed or revision != self._activity_revision
                            or not connected() or not idle()):
                        await self.followups.resolve(claim['token'], 'cancelled')
                        continue
                    event = Event('companion', 'followup', claim['text'], user=claim['user'],
                                  priority=PRIO_IDLE, ts=self.clock())
                    event.owner_control = True
                    event.companion_followup_token = claim['token']
                    event.companion_receipt = claim['context']
                    event.companion_activity_revision = revision
                    self._claims[claim['token']] = dict(event=event, state='queued', changed_at=self.clock())
                    try:
                        await asyncio.wait_for(queue.put(event), 1)
                    except asyncio.TimeoutError:
                        await self.abandon(event, 'cancelled')
                self.last_error = ''
            except asyncio.CancelledError:
                raise
            except Exception:
                self.last_error = '本机跟进暂不可用，请检查日程状态。'
            await asyncio.sleep(1)

    async def close(self):
        if self._closed:
            return
        self._closed = True
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        async with self._delivery_lock:
            results = await asyncio.gather(self.vision.close(), self.followups.close(), self.journal.close(),
                                           return_exceptions=True)
        if any(isinstance(result, BaseException) for result in results):
            raise RuntimeError('陪伴能力关闭时未能完成状态保存')
