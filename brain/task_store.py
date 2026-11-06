"""Transactional, per-task persistence for the single background task writer.

The legacy JSON file is imported once and kept untouched. JSON remains the row
payload so existing task dictionaries and old optional fields retain their
meaning; it is no longer rewritten as a file on every executor receipt.
"""
from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from pathlib import Path


class TaskStoreConflict(ValueError):
    """A different writer changed a row after this store loaded it."""


def _encode(job):
    if not isinstance(job, dict) or not isinstance(job.get('id'), str) or not job['id']:
        raise ValueError('任务记录缺少有效编号')
    if not isinstance(job.get('state'), str) or not job['state']:
        raise ValueError('任务记录缺少状态')
    return json.dumps(job, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


class TaskStore:
    """One row per task, atomic batches, and optimistic writer conflict checks.

The owning service still holds its process lock. Row versions are additional
protection against a stale accidental writer, not a distributed task lease.
Call ``load`` after opening before modifying records. ``save_many`` writes only
changed rows and never deletes history merely because a caller supplies a
subset. SQLite transactions cover these rows, not generated files or audio.
"""

    def __init__(self, path, *, legacy_path=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.legacy_path = Path(legacy_path) if legacy_path is not None else self.path.with_suffix('.json')
        self._connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        self._closed = False
        self._snapshots = {}
        self._encoded = {}
        self._versions = {}
        try:
            self._connection.execute('PRAGMA journal_mode=WAL')
            self._connection.execute('PRAGMA synchronous=FULL')
            self._connection.execute('PRAGMA busy_timeout=5000')
            self._schema()
            self._migrate()
        except BaseException:
            self.close()
            raise

    def _schema(self):
        version = self._connection.execute('PRAGMA user_version').fetchone()[0]
        if version not in {0, 1}:
            raise ValueError('后台任务数据库版本不受当前程序支持')
        self._connection.execute('BEGIN IMMEDIATE')
        try:
            self._connection.execute('CREATE TABLE IF NOT EXISTS task_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            self._connection.execute('''CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, position INTEGER NOT NULL, state TEXT NOT NULL,
                revision INTEGER NOT NULL, progress_revision INTEGER NOT NULL,
                delivery_revision INTEGER NOT NULL, row_version INTEGER NOT NULL,
                payload TEXT NOT NULL)''')
            self._connection.execute('CREATE INDEX IF NOT EXISTS tasks_position ON tasks(position)')
            self._connection.execute('PRAGMA user_version=1')
            self._connection.execute('COMMIT')
        except BaseException:
            self._connection.execute('ROLLBACK')
            raise

    def _migrate(self):
        self._connection.execute('BEGIN IMMEDIATE')
        try:
            done = self._connection.execute("SELECT value FROM task_meta WHERE key='legacy_import'").fetchone()
            if not done:
                has_rows = self._connection.execute('SELECT 1 FROM tasks LIMIT 1').fetchone()
                if has_rows:
                    outcome = 'existing_database'
                elif self.legacy_path.exists():
                    jobs = json.loads(self.legacy_path.read_text('utf-8-sig'))
                    if not isinstance(jobs, list):
                        raise ValueError('旧任务记录必须为任务列表，未迁移')
                    ids = set()
                    for position, original in enumerate(jobs):
                        job = deepcopy(original)
                        _encode(job)
                        if job['id'] in ids:
                            raise ValueError('旧任务记录包含重复编号，未迁移')
                        ids.add(job['id'])
                        job.setdefault('revision', 1)
                        job.setdefault('progress_revision', 0)
                        job.setdefault('delivery_revision', 0)
                        encoded = _encode(job)
                        self._connection.execute('INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                            (job['id'], position, job['state'], job['revision'], job['progress_revision'],
                             job['delivery_revision'], 1, encoded))
                    outcome = 'imported'
                else:
                    outcome = 'no_legacy_file'
                self._connection.execute("INSERT INTO task_meta(key, value) VALUES ('legacy_import', ?)", (outcome,))
            self._connection.execute('COMMIT')
        except BaseException:
            self._connection.execute('ROLLBACK')
            raise

    def load(self):
        """Return detached mutable dictionaries in their original creation order."""
        rows = self._connection.execute('SELECT id, row_version, payload FROM tasks ORDER BY position, id').fetchall()
        self._snapshots = {job_id: json.loads(payload) for job_id, _, payload in rows}
        self._encoded = {job_id: payload for job_id, _, payload in rows}
        self._versions = {job_id: version for job_id, version, _ in rows}
        return [deepcopy(self._snapshots[job_id]) for job_id, _, _ in rows]

    def snapshot(self, job_id):
        """The last successfully loaded/committed version, never a shared dict."""
        return deepcopy(self._snapshots.get(job_id))

    def save_many(self, jobs):
        """Commit a subset or full list atomically; return IDs actually written."""
        pending = []
        seen = set()
        for job in jobs:
            encoded = _encode(job)
            job_id = job['id']
            if job_id in seen:
                raise ValueError('同一批提交不能重复包含任务编号')
            seen.add(job_id)
            if self._encoded.get(job_id) != encoded:
                pending.append((deepcopy(job), encoded))
        if not pending:
            return []
        committed_versions = {}
        self._connection.execute('BEGIN IMMEDIATE')
        try:
            next_position = self._connection.execute('SELECT COALESCE(MAX(position), -1)+1 FROM tasks').fetchone()[0]
            for job, encoded in pending:
                job_id = job['id']
                previous = self._versions.get(job_id)
                if previous is None:
                    if self._connection.execute('SELECT 1 FROM tasks WHERE id=?', (job_id,)).fetchone():
                        raise TaskStoreConflict('任务已由其他写入端创建，请重新加载状态')
                    self._connection.execute('INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                        (job_id, next_position, job['state'], job.get('revision', 1),
                         job.get('progress_revision', 0), job.get('delivery_revision', 0), 1, encoded))
                    committed_versions[job_id] = 1
                    next_position += 1
                else:
                    cursor = self._connection.execute('''UPDATE tasks SET state=?, revision=?, progress_revision=?,
                        delivery_revision=?, row_version=row_version+1, payload=? WHERE id=? AND row_version=?''',
                        (job['state'], job.get('revision', 1), job.get('progress_revision', 0),
                         job.get('delivery_revision', 0), encoded, job_id, previous))
                    if cursor.rowcount != 1:
                        raise TaskStoreConflict('任务已由其他写入端更新，请重新加载状态')
                    committed_versions[job_id] = previous + 1
            self._connection.execute('COMMIT')
        except BaseException:
            self._connection.execute('ROLLBACK')
            raise
        # Cache changes only after COMMIT; failed batches remain retryable.
        for job, encoded in pending:
            job_id = job['id']
            self._snapshots[job_id] = job
            self._encoded[job_id] = encoded
            self._versions[job_id] = committed_versions[job_id]
        return [job['id'] for job, _ in pending]

    def close(self):
        if not self._closed:
            self._closed = True
            self._connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
