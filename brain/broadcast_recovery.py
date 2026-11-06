"""Live reply circuit breaker and voice-specific, explicitly prepared standby clip."""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sqlite3
import struct
import time

from .persistence import atomic_json_write


STANDBY_TEXT = '刚才的回应没有顺利完成。我们稍等片刻，你的问题还可以再试一次。'


def valid_standby_wav(data):
    """Accept complete PCM/float WAV chunks, not a header-only failed response."""
    if len(data) < 44 or data[:4] != b'RIFF' or data[8:12] != b'WAVE':
        return False
    end = struct.unpack_from('<I', data, 4)[0] + 8
    if end != len(data):
        return False
    offset, audio_format, alignment, data_size = 12, None, 0, 0
    while offset + 8 <= end:
        kind = data[offset:offset + 4]
        size = struct.unpack_from('<I', data, offset + 4)[0]
        start = offset + 8
        if start + size > end:
            return False
        if kind == b'fmt ':
            if size < 16 or audio_format is not None:
                return False
            audio_format, channels, rate, byte_rate, alignment, bits = struct.unpack_from('<HHIIHH', data, start)
            if audio_format == 0xfffe:
                if size < 40:
                    return False
                audio_format = struct.unpack_from('<H', data, start + 24)[0]
            if (audio_format not in {1, 3} or not 1 <= channels <= 8 or not 1 <= rate <= 384000
                    or bits not in {8, 16, 24, 32, 64} or alignment != channels * (bits // 8)
                    or byte_rate != rate * alignment or audio_format == 3 and bits not in {32, 64}):
                return False
        elif kind == b'data':
            data_size += size
        offset = start + size + size % 2
    return offset == end and audio_format is not None and data_size > 0 and data_size % alignment == 0


class BroadcastRecovery:
    def __init__(self, *, failure_limit=3, cooldown_sec=30, clock=time.monotonic):
        self.limit = max(1, int(failure_limit))
        self.cooldown = max(1.0, float(cooldown_sec))
        self.clock = clock
        self.failures = {'llm': 0, 'tts': 0}
        self.retry_at = 0.0
        self._seen = deque(maxlen=128)
        self._last_notice = -float('inf')
        self.preparing = False
        self.prepare_error = ''

    def failure(self, stage, turn_id):
        if stage not in self.failures or (stage, turn_id) in self._seen:
            return
        self._seen.append((stage, turn_id))
        self.failures[stage] += 1
        if self.failures[stage] >= self.limit:
            self.retry_at = self.clock() + self.cooldown

    def success(self, stage):
        if stage in self.failures:
            self.failures[stage] = 0
        if not any(value >= self.limit for value in self.failures.values()):
            self.retry_at = 0.0

    def allow_automatic(self):
        return self.clock() >= self.retry_at

    def claim_notice(self):
        now = self.clock()
        if now - self._last_notice < 30:
            return False
        self._last_notice = now
        return True

    def summary(self):
        remaining = max(0, self.retry_at - self.clock())
        return {'automatic_paused': remaining > 0, 'retry_in_sec': round(remaining, 1),
                'failures': dict(self.failures), 'failure_limit': self.limit,
                'preparing': self.preparing, 'prepare_error': self.prepare_error,
                'message': ('连续回应失败，自动接话暂缓；冷却后允许新的回复重试。' if remaining > 0
                            else '自动接话可用；手动提问始终可尝试。')}


class StandbyVoice:
    def __init__(self, root, cfg):
        self.root = Path(root).resolve()
        self.cfg = cfg
        self.manifest = self.root / '.standby-voice.json'
        self.name = 'standby_notice.wav'

    def fingerprint(self):
        values = {key: value for key, value in asdict(self.cfg).items()
                  if not key.startswith('audio_retention') and key != 'out_dir'}
        refs = []
        for key in ('ref_audio_path', 'indextts2_ref_audio_path', 'indextts2_short_ref_audio_path',
                    'fish_speech_ref_audio_path'):
            configured = getattr(self.cfg, key, '')
            if not configured:
                refs.append(('', None))
                continue
            path = Path(configured)
            try:
                info = path.stat()
                refs.append((str(path), info.st_size, info.st_mtime_ns))
            except OSError:
                refs.append((str(path), None))
        return hashlib.sha256(json.dumps([values, refs], sort_keys=True).encode()).hexdigest()

    def available(self):
        target = self.root / self.name
        try:
            if target.is_symlink() or self.manifest.is_symlink() or target.resolve() != target:
                return None
            record = json.loads(self.manifest.read_text('utf-8'))
            data = target.read_bytes()
            if (isinstance(record, dict) and record.get('fingerprint') == self.fingerprint()
                    and record.get('text') == STANDBY_TEXT
                    and record.get('sha256') == hashlib.sha256(data).hexdigest()
                    and valid_standby_wav(data)):
                return str(target)
        except (OSError, ValueError):
            pass
        return None

    async def prepare(self, voice):
        fingerprint = self.fingerprint()
        audio = await voice.speak(STANDBY_TEXT, '平静')
        if not audio:
            raise ValueError('当前语音后端没有生成音频')
        def save():
            source = Path(audio).resolve(strict=True)
            if source.parent != self.root or source.suffix.lower() != '.wav':
                raise ValueError('提示音必须来自当前语音输出目录')
            data = source.read_bytes()
            if not valid_standby_wav(data):
                raise ValueError('语音结果不是有效的 WAV')
            target = self.root / self.name
            if target.is_symlink() or self.manifest.is_symlink() or target.resolve() != target:
                raise ValueError('提示音路径已变化')
            # Replace the fixed clip atomically. A manifest mismatch fails closed
            # if the process exits before both files have been committed.
            temporary = self.root / 'standby_notice.preparing'
            if temporary.is_symlink():
                raise ValueError('提示音暂存路径已变化')
            temporary.write_bytes(data)
            temporary.replace(target)
            atomic_json_write(self.manifest, {'fingerprint': fingerprint, 'text': STANDBY_TEXT,
                                              'sha256': hashlib.sha256(data).hexdigest()})
        await asyncio.to_thread(save)
        if not self.available():
            raise ValueError('语音配置已变化，请重新准备提示音')


class TurnDeliveryTracker:
    """A saved question is answered only after generation AND every segment plays."""
    def __init__(self):
        self.turns = {}

    def begin(self, turn_id, finish):
        self.turns[turn_id] = {'finish': finish, 'expected': set(), 'played': set(), 'generated': False,
                              'outcome': None}

    def expect(self, turn_id, index):
        if turn_id in self.turns:
            self.turns[turn_id]['expected'].add(index)

    def played(self, turn_id, index, completed):
        state = self.turns.get(turn_id)
        if not state:
            return
        if not completed:
            self.cancel(turn_id, '回答被打断或没有完整播放')
        else:
            state['played'].add(index)
            self._complete(turn_id)

    def generated(self, turn_id, success):
        state = self.turns.get(turn_id)
        if not state:
            return
        if not success or not state['expected']:
            self.cancel(turn_id, '回答生成未完成')
            return
        state['generated'] = True
        self._complete(turn_id)

    def cancel(self, turn_id, reason='回答未完成'):
        state = self.turns.get(turn_id)
        if state and state['outcome'] is None:
            state['outcome'] = (False, reason)
        self.retry()

    def retry(self):
        for turn_id, state in tuple(self.turns.items()):
            if state['outcome'] is None:
                continue
            try:
                state['finish'](*state['outcome'])
            except (OSError, RuntimeError, sqlite3.Error):
                continue  # Keep proof of playback until the card store recovers.
            self.turns.pop(turn_id, None)

    def _complete(self, turn_id):
        state = self.turns.get(turn_id)
        if state and state['generated'] and state['expected'] <= state['played']:
            if state['outcome'] is None:
                state['outcome'] = (True, '')
            self.retry()
