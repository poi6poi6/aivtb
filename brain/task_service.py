"""Host task-state commands independent from HTTP and executor transports.

Existing managers may keep mutable dictionaries, but all persistence crosses
this kernel: state transitions are validated against the committed snapshot,
and task, progress, and playback delivery revisions are assigned separately.
"""
from __future__ import annotations

import time
from copy import deepcopy


ACTIVE = frozenset({'queued', 'running', 'waiting_approval'})
TERMINAL = frozenset({'succeeded', 'review', 'failed', 'cancelled', 'blocked', 'applied', 'verified', 'rolled_back'})
TRANSITIONS = {
    'pending': frozenset({'queued', 'cancelled'}),
    'queued': frozenset({'running', 'cancelled', 'blocked', 'failed'}),
    'running': frozenset({'waiting_approval', 'succeeded', 'review', 'failed', 'cancelled', 'blocked'}),
    'waiting_approval': frozenset({'running', 'failed', 'cancelled', 'blocked'}),
    'review': frozenset({'applied'}),
    'applied': frozenset({'verified', 'rolled_back'}),
    'verified': frozenset({'rolled_back'}),
    'succeeded': frozenset(), 'failed': frozenset(), 'cancelled': frozenset(),
    'blocked': frozenset(), 'rolled_back': frozenset(),
}
# These changes invalidate task confirmations and old conversational receipts.
# Progress, budgets, descriptive text, and delivery callbacks do not.
SEMANTIC_FIELDS = frozenset({'state', 'prompt', 'kind', 'requester', 'resource_profile',
                             'execution_mode', 'parent_id', 'superseded_by', 'artifacts', 'approvals',
                             'drawing_stage', 'drawing_root', 'self_upgrade_id', 'self_upgrade_scope'})
DELIVERY_FIELDS = frozenset({'delivery', 'announced_revision', 'announced_audio'})


def validate_transition(before, after):
    if before not in TRANSITIONS or after not in TRANSITIONS:
        raise ValueError('未知的任务状态')
    if before != after and after not in TRANSITIONS[before]:
        raise ValueError(f'不允许的任务状态转换：{before} → {after}')


def require_requester(job, requester):
    """Callers supply identity derived by the host, never a model/tool payload."""
    if requester != 'owner' and job.get('requester') != requester:
        raise ValueError('只能操作自己的任务')


def _revision(value, default=0):
    if value is None:
        return default
    if type(value) is not int or value < 0:
        raise ValueError('任务版本必须为非负整数')
    return value


class TaskService:
    """Command kernel for a single writer backed by a transactional TaskStore.

``save`` is the compatibility entry for dictionaries already modified by a
trusted host mixin. Prefer ``update``/``replace`` for new commands: they stage a
copy and publish it to the original dict only after a successful transaction.
Multi-job ``replace_many`` makes parent/child links one database operation; it
does not make a subprocess cancellation or a filesystem write transactional.
"""

    def __init__(self, store):
        self.store = store

    def load(self):
        return self.store.load()

    @staticmethod
    def _prepare(previous, candidate):
        candidate = deepcopy(candidate)
        if candidate.get('state') not in TRANSITIONS:
            raise ValueError('未知的任务状态')
        if previous is None:
            # Existing host fixtures/imports can contain any known state. New
            # user requests should enter through create(), which is stricter.
            candidate['revision'] = _revision(candidate.get('revision'), 1)
            candidate['progress_revision'] = _revision(candidate.get('progress_revision'))
            candidate['delivery_revision'] = _revision(candidate.get('delivery_revision'))
            return candidate
        if candidate.get('id') != previous['id']:
            raise ValueError('任务编号不能修改')
        validate_transition(previous['state'], candidate['state'])
        semantic = any(previous.get(key) != candidate.get(key) for key in SEMANTIC_FIELDS)
        progress = previous.get('progress') != candidate.get('progress')
        delivery = any(previous.get(key) != candidate.get(key) for key in DELIVERY_FIELDS)
        candidate['revision'] = _revision(previous.get('revision')) + int(semantic)
        candidate['progress_revision'] = _revision(previous.get('progress_revision')) + int(progress)
        candidate['delivery_revision'] = _revision(previous.get('delivery_revision')) + int(delivery)
        return candidate

    def create(self, job):
        if self.store.snapshot(job.get('id')) is not None:
            raise ValueError('任务编号已存在')
        if job.get('state') not in {'pending', 'queued'}:
            raise ValueError('新请求必须等待批准或排队')
        return self.replace(job, job)

    def save(self, jobs):
        """Flush a trusted subset; revert persisted dicts if validation/write fails."""
        jobs = list(jobs)
        previous = [(job, self.store.snapshot(job.get('id'))) for job in jobs]
        try:
            return self.replace_many([(job, job) for job in jobs])
        except BaseException:
            # This compatibility API receives objects mutated before save().
            # Restore committed content so a failed command isn't advertised.
            for job, old in previous:
                if old is not None:
                    job.clear()
                    job.update(old)
            raise

    def update(self, job, *, requester=None, expected_revision=None, **values):
        candidate = deepcopy(job)
        candidate.update(values, updated=time.time())
        self.replace(job, candidate, requester=requester, expected_revision=expected_revision)
        return job

    def replace(self, job, candidate, *, requester=None, expected_revision=None):
        expected = None if expected_revision is None else {job['id']: expected_revision}
        self.replace_many([(job, candidate)], requester=requester, expected_revisions=expected)
        return job

    def replace_many(self, replacements, *, requester=None, expected_revisions=None):
        """Atomically commit (existing mutable dict, proposed dict) pairs."""
        staged = []
        for job, candidate in replacements:
            job_id = job.get('id')
            if candidate.get('id') != job_id:
                raise ValueError('任务编号不能修改')
            previous = self.store.snapshot(job_id)
            if requester is not None:
                require_requester(previous or candidate, requester)
            if expected_revisions is not None and job_id in expected_revisions:
                revision = (previous or job).get('revision', 0)
                if expected_revisions[job_id] != revision:
                    raise ValueError('任务版本已变化，请重新读取状态')
            staged.append((job, self._prepare(previous, candidate)))
        written = self.store.save_many(candidate for _, candidate in staged)
        for job, candidate in staged:
            job.clear()
            job.update(candidate)
        return written

    def close(self):
        self.store.close()
