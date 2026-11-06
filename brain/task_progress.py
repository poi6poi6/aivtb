"""Small, host-owned task timelines built from executor lifecycle receipts.

Only fixed labels and numeric exit codes are retained. Commands, file paths,
tool arguments/results, thoughts and error payloads never enter this channel.
"""
import hashlib
import re
import time

HISTORY_LIMIT = 24
SEEN_LIMIT = 256
ACTIVE_LIMIT = 128
LIVE_STATES = {'running', 'waiting_approval'}
TERMINAL = {'succeeded', 'review', 'failed', 'cancelled', 'blocked', 'applied', 'verified', 'rolled_back'}
STAGES = {
    'executing': '执行器已开始工作',
    'validating': '正在核对产物与验证记录',
    'searching': '检索资料',
    'editing': '修改文件',
    'image': '生成图像',
    'testing': '运行测试命令',
    'command': '运行命令',
    'tool': '执行已连接工具',
    'awaiting_receipt': '本步已结束，等待后续执行回执',
}
STATE_LABELS = {
    'waiting_approval': '等待本机批准具体操作',
    'succeeded': '产物已核验',
    'review': '开发结果待审核，尚未应用',
    'failed': '执行失败，未完成',
    'cancelled': '任务已停止',
    'blocked': '暂时无法执行，未完成',
    'applied': '文件已应用，等待服务验证',
    'verified': '服务版本检查通过，功能效果需另行验收',
    'rolled_back': '文件已回退，等待重启服务',
}
ERROR_LABELS = {
    'authentication': '账号或身份验证失败', 'quota': '可用额度不足',
    'rate_limit': '服务限流', 'timeout': '执行或连接超时',
    'network': '服务连接失败', 'execution': '执行或产物校验失败',
}
ITEM_STAGES = {'webSearch': 'searching', 'fileChange': 'editing',
               'imageGeneration': 'image', 'mcpToolCall': 'tool'}


def command_is_test(command):
    """Conservatively recognize a direct runner, never quoted report content.

    Wrapper scripts and compound shell commands stay '运行命令'. This is a UI
    description only, not the host's source-bound test acceptance decision.
    """
    if not isinstance(command, str) or len(command) > 8000:
        return False
    text = command.strip()
    if any(token in text for token in ('\n', ';', '&&', '||', '|', '>', '<', '`', '$(')):
        return False
    executable = r'(?:&\s*)?(?:"[^"\r\n]+"|\x27[^\x27\r\n]+\x27|[^\s"\x27]+)'
    match = re.match(r'^(' + executable + r')\s+(.+)$', text)
    if not match:
        return False
    program = re.sub(r'^&\s*', '', match.group(1)).strip('"\x27').replace('\\', '/').rsplit('/', 1)[-1].lower()
    args = match.group(2)
    if program in {'python', 'python3', 'python.exe', 'python3.exe', 'py', 'py.exe'}:
        return bool(re.match(r'^(?:-X\s+utf8\s+)?-m\s+(?:unittest|pytest)(?:\s|$)', args))
    if program in {'node', 'node.exe'}:
        return bool(re.match(r'^["\x27]?(?:\.[/\\])?tests[/\\][^\s"\x27]+', args))
    if program in {'npm', 'npm.cmd'}:
        return bool(re.match(r'^(?:run\s+)?test(?:\s|$)', args))
    return False


def _key(item):
    # Opaque protocol IDs are not needed by the audience or the task UI.
    value = item.get('id')
    if not isinstance(value, str) or not value or len(value) > 2000:
        return None
    return hashlib.sha256(value.encode('utf-8')).hexdigest()[:24]


def _exit_code(item):
    code = item.get('exitCode')
    return code if type(code) is int and -(2**31) <= code < 2**31 else None


class TaskProgress:
    """Mixin; owner provides save(). No task-state or speech revision changes."""

    @staticmethod
    def _progress(job):
        return job.setdefault('progress', {'active': [], 'history': [], 'seen': []})

    @staticmethod
    def _append(progress, stage, event, *, exit_code=None, error_category=None):
        now = time.time()
        entry = {'stage': stage, 'event': event, 'label': STAGES.get(stage, STATE_LABELS.get(stage, '执行状态已更新')), 'at': now}
        if exit_code is not None:
            entry['exit_code'] = exit_code
        if error_category in ERROR_LABELS:
            entry['error'] = ERROR_LABELS[error_category]
        progress['history'] = (progress.get('history', []) + [entry])[-HISTORY_LIMIT:]
        progress['last_event_at'] = now
        return entry

    @staticmethod
    def _current(job, progress):
        if job.get('state') == 'waiting_approval':
            stage = 'waiting_approval'
        elif progress.get('active'):
            stage = progress['active'][-1]['stage']
        else:
            stage = 'awaiting_receipt'
        progress.update(stage=stage, label=STAGES.get(stage, STATE_LABELS.get(stage)))

    def record_progress(self, job, stage, *, event='started'):
        """Call only after a real executor start or when host verification starts."""
        if job.get('state') not in LIVE_STATES or stage not in {'executing', 'validating'} or event != 'started':
            return False
        progress = self._progress(job)
        if progress.get('stage') == stage:
            return False
        self._append(progress, stage, event)
        # An active tool remains the more specific, evidenced current activity.
        if progress['active'] or job.get('state') == 'waiting_approval':
            self._current(job, progress)
        else:
            progress.update(stage=stage, label=STAGES[stage])
        self.save(job)
        return True

    def observe_progress(self, job, method, item):
        """Caller must restrict events to this job's current executor thread."""
        if job.get('state') not in LIVE_STATES or method not in {'item/started', 'item/completed'} or not isinstance(item, dict):
            return False
        stage = ITEM_STAGES.get(item.get('type'))
        if item.get('type') == 'commandExecution':
            stage = 'testing' if command_is_test(item.get('command')) else 'command'
        key = _key(item)
        if not stage or not key:
            return False
        progress = self._progress(job)
        active = next((entry for entry in progress['active'] if entry['key'] == key), None)
        if method == 'item/started':
            if active or key in progress['seen'] or len(progress['active']) >= ACTIVE_LIMIT:
                return False
            progress['active'].append({'key': key, 'stage': stage})
            self._append(progress, stage, 'started')
        else:
            # Unmatched/duplicate completions cannot invent progress or replace
            # the current phase; keep the classification observed at start.
            if active is None:
                return False
            stage = active['stage']
            progress['active'].remove(active)
            progress['seen'] = (progress['seen'] + [key])[-SEEN_LIMIT:]
            code = _exit_code(item)
            failed = (code is not None and code != 0) or item.get('status') in {'failed', 'declined', 'cancelled'} or bool(item.get('error'))
            self._append(progress, stage, 'failed' if failed else 'completed', exit_code=code)
        self._current(job, progress)
        self.save(job)
        return True

    def progress_state(self, job, state, *, error_category=None):
        """Called in the host's update before save(); never saves recursively."""
        if state not in TERMINAL | {'waiting_approval', 'running'}:
            return False
        progress = job.get('progress')
        if state == 'running':
            if progress and progress.get('stage') == 'waiting_approval':
                # The host is resuming after approval, not starting new work.
                active = progress.get('active', [])
                stage = active[-1]['stage'] if active else 'awaiting_receipt'
                self._append(progress, stage, 'resumed')
                progress.update(stage=stage, label=STAGES[stage])
                return True
            return False
        progress = self._progress(job)
        if progress.get('stage') == state:
            return False
        if state in TERMINAL:
            progress['active'] = []
        self._append(progress, state, 'state', error_category=error_category if state in {'failed', 'blocked'} else None)
        progress.update(stage=state, label=STATE_LABELS[state])
        return True
