"""Conservative retention for generated speech; all filesystem work runs off-loop."""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


_GENERATED = re.compile(r'[0-9]{10,}_(?:[1-9][0-9]*(?:_index)?|[a-f0-9]{32}_fish)\.wav\Z')


def _linked(info):
    return (stat.S_ISLNK(info.st_mode)
            or bool(getattr(info, 'st_file_attributes', 0) & 0x400))


class AudioRetention:
    def __init__(self, root, *, enabled=True, max_age_days=7,
                 max_bytes=1073741824, grace_seconds=300, reference_paths=(),
                 pins_path=None, clock=time.time):
        if any(not math.isfinite(value) or value < 0
               for value in (max_age_days, max_bytes, grace_seconds)):
            raise ValueError('音频保留期限、容量与宽限期不能小于零')
        self.root = Path(root).absolute()
        self._anchor = self.root.resolve()
        self._root_identity = None
        self.enabled = bool(enabled)
        self.max_age_seconds = float(max_age_days) * 86400
        self.max_bytes, self.grace_seconds = int(max_bytes), float(grace_seconds)
        self.clock = clock
        self.pins_path = Path(pins_path).absolute() if pins_path else self.root / '.audio-pins.json'
        if self.pins_path.parent.resolve() != self._anchor:
            raise ValueError('收藏清单必须位于音频目录顶层')
        if _GENERATED.fullmatch(self.pins_path.name):
            raise ValueError('收藏清单不能使用生成音频文件名')
        self._references = {Path(path).resolve() for path in reference_paths if path}
        self._protected: set[str] = set()
        self._pins: set[str] = set()
        self._cleanup_error = None
        self._operation_error = None
        self._io_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._state = {'total_files': 0, 'total_bytes': 0, 'last_cleanup': None,
                       'last_error': None, 'deleted_files': 0, 'deleted_bytes': 0}

    def _name(self, value):
        value = str(value)
        if _GENERATED.fullmatch(value):
            return value
        path = Path(value)
        if path.is_absolute() and path.parent.resolve() == self._anchor:
            return path.name if _GENERATED.fullmatch(path.name) else None
        return None

    def set_protected(self, paths):
        names = {name for path in paths if (name := self._name(path))}
        with self._state_lock:
            self._protected = names

    def status(self):
        with self._state_lock:
            return dict(self._state, enabled=self.enabled,
                        max_age_days=self.max_age_seconds / 86400,
                        max_bytes=self.max_bytes, grace_seconds=self.grace_seconds,
                        pinned_count=len(self._pins), protected_count=len(self._protected))

    def _root_ok(self):
        info = self.root.lstat()
        if _linked(info) or not stat.S_ISDIR(info.st_mode) or self.root.resolve() != self._anchor:
            raise ValueError('音频目录身份已变化，停止清理')
        identity = (info.st_dev, info.st_ino)
        if self._root_identity is None:
            self._root_identity = identity
        elif identity != self._root_identity:
            raise ValueError('音频目录已被替换，停止清理')

    def _safe_file(self, name):
        if not _GENERATED.fullmatch(name):
            return None
        self._root_ok()
        path = self.root / name
        try:
            info = path.lstat()
            if (_linked(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or path.resolve(strict=True) != self._anchor / name
                    or path.resolve() in self._references):
                return None
            return info
        except FileNotFoundError:
            return None

    @contextmanager
    def _writer(self):
        """An OS lock is released after a crash and serializes multiple instances."""
        self._root_ok()
        path = self.root / '.audio-retention.lock'
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if _linked(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('音频维护锁文件不安全')
        flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
        descriptor = os.open(path, flags, 0o600)
        locked = False
        try:
            info = os.fstat(descriptor)
            current = path.lstat()
            if (_linked(current) or info.st_nlink != 1
                    or not stat.S_ISREG(info.st_mode)
                    or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)):
                raise ValueError('音频维护锁文件身份已变化')
            if os.name == 'nt':
                import msvcrt
                if info.st_size == 0:
                    os.write(descriptor, b'0')
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
            self._root_ok()
            yield
        finally:
            if locked:
                if os.name == 'nt':
                    import msvcrt
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _load_pins(self):
        try:
            info = self.pins_path.lstat()
        except FileNotFoundError:
            pins = set()
        else:
            if (_linked(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_size > 1024 * 1024
                    or self.pins_path.resolve() != self._anchor / self.pins_path.name):
                raise ValueError('收藏清单不安全，停止清理')
            data = json.loads(self.pins_path.read_text(encoding='utf-8'))
            if (not isinstance(data, dict) or data.get('version') != 1
                    or not isinstance(data.get('files'), list)
                    or any(not isinstance(name, str) or not _GENERATED.fullmatch(name)
                           for name in data['files'])):
                raise ValueError('收藏清单损坏，停止清理')
            pins = set(data['files'])
        with self._state_lock:
            self._pins = pins

    def _save_pins(self, pins):
        self._root_ok()
        temporary = self.root / ('.audio-pins-' + uuid.uuid4().hex + '.tmp')
        try:
            with temporary.open('x', encoding='utf-8') as handle:
                json.dump({'version': 1, 'files': sorted(pins)}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.pins_path)
        finally:
            if temporary.exists():
                temporary.unlink()
        with self._state_lock:
            self._pins = pins

    def _scan(self):
        files = []
        for path in self.root.iterdir():
            info = self._safe_file(path.name)
            if info is not None:
                files.append((path.name, info))
        return files

    def _update_totals(self, files):
        with self._state_lock:
            self._state.update(total_files=len(files), total_bytes=sum(info.st_size for _, info in files))

    def _operation(self, function, *args):
        try:
            with self._io_lock, self._writer():
                self._load_pins()
                result = function(*args)
                with self._state_lock:
                    if function == self._cleanup:
                        self._cleanup_error = None
                    self._operation_error = None
                    self._state['last_error'] = self._cleanup_error
                return result
        except Exception as error:
            # Never publish a path, request payload, or arbitrary exception text.
            with self._state_lock:
                message = f'{type(error).__name__}：音频维护未完成，已停止清理'
                if function == self._cleanup:
                    self._cleanup_error = message
                else:
                    self._operation_error = message
                self._state['last_error'] = self._cleanup_error or self._operation_error
            raise ValueError('音频维护未完成，请检查目录权限、收藏清单或其他维护进程') from None

    async def list_files(self, limit=100):
        limit = max(0, min(100, int(limit)))
        return await asyncio.to_thread(self._operation, self._list_files, limit)

    def _list_files(self, limit):
        files = self._scan()
        self._update_totals(files)
        files.sort(key=lambda item: (item[1].st_mtime_ns, item[0]), reverse=True)
        with self._state_lock:
            return [dict(name=name, bytes=info.st_size, modified_at=info.st_mtime,
                         pinned=name in self._pins, protected=name in self._protected)
                    for name, info in files[:limit]]

    async def pin(self, name, pinned=True):
        if not isinstance(name, str) or not _GENERATED.fullmatch(name) or type(pinned) is not bool:
            raise ValueError('收藏操作仅接受生成音频的文件名与布尔状态')
        return await asyncio.to_thread(self._operation, self._pin, name, pinned)

    def _pin(self, name, pinned):
        if self._safe_file(name) is None:
            raise ValueError('音频不存在或不能收藏')
        pins = set(self._pins)
        pins.add(name) if pinned else pins.discard(name)
        self._save_pins(pins)
        return {'name': name, 'pinned': pinned}

    async def cleanup(self, protected_paths=None):
        if protected_paths is not None:
            self.set_protected(protected_paths)
        if not self.enabled:
            return self.status()
        try:
            await asyncio.to_thread(self._operation, self._cleanup)
        except ValueError:
            pass  # Fail closed and surface the safe error through runtime status.
        return self.status()

    def _cleanup(self):
        now = self.clock()
        files = sorted(self._scan(), key=lambda item: (item[1].st_mtime_ns, item[0]))
        total = sum(info.st_size for _, info in files)
        deleted = deleted_bytes = 0
        for name, original in files:
            with self._state_lock:
                if name in self._pins or name in self._protected:
                    continue
                info = self._safe_file(name)
                if info is None or (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size) != (
                        original.st_dev, original.st_ino, original.st_mtime_ns, original.st_size):
                    continue
                age = now - info.st_mtime
                if age < self.grace_seconds:
                    continue
                if age < self.max_age_seconds and total <= self.max_bytes:
                    continue
                (self.root / name).unlink()
                total -= info.st_size
                deleted += 1
                deleted_bytes += info.st_size
        self._update_totals(self._scan())
        with self._state_lock:
            self._state.update(last_cleanup=now, deleted_files=deleted,
                               deleted_bytes=deleted_bytes)
