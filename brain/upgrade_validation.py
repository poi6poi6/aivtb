"""Host-selected regression checks over immutable reviewed source bytes.

This isolates files/environment, not an OS security boundary for hostile code.
The caller must keep the owner approval and business-path policy in place.
"""
from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path, PurePosixPath

from .code_review import CodeReview, EXTS, digest, safe, sources, tests_executed


# Matching is case-insensitive, including on case-sensitive test hosts. An
# allowed directory never overrides these exclusions.
PROTECTED = (
    'tests/*', 'brain/__init__.py', 'brain/agent_*.py', 'brain/task_*.py',
    'brain/code_*.py', 'brain/self_upgrade*', 'brain/upgrade_*',
    'brain/runtime_*', 'brain/main.py', 'brain/webui.py', 'brain/config.py',
    'brain/safety.py', 'brain/viewer_identity.py', 'brain/tool_registry.py',
    'brain/*permission*', 'brain/*policy*', 'brain/*auth*',
    'brain/static/tasks.*', 'brain/static/runtime.*',
    'brain/static/*upgrade*', 'tools/launcher/*', 'tools/run_agent_console.py',
    'brain/static/vendor/*', 'body/node_modules/*',
    'tools/*upgrade*', 'tools/*release*', 'tools/*agent*',
    '*/requirements*.txt', '*/package*.json', '*/pyproject.toml',
    '*/poetry.lock', '*/uv.lock', '*/pipfile*', '*/setup.py', '*/setup.cfg',
    '*/conftest.py', '*/sitecustomize.py', '*/usercustomize.py',
)
SUITE = 'host-smoke-v1'
PYTHON_TEST_MODULES = (
    'tests.test_code_review', 'tests.test_code_verification',
    'tests.test_runtime_supervisor', 'tests.test_presence', 'tests.test_interaction',
)
NODE_TEST_FILES = (
    'tests/test_presence_ui.js', 'tests/test_motion_ui.js',
    'tests/test_face_ui.js', 'tests/test_gaze_ui.js', 'tests/test_interaction_ui.js',
)
# Extending this mapping is a host change, not a candidate-selected command.
# Unmapped business modules fail closed instead of passing unrelated smoke tests.
BUSINESS_TESTS = {
    'brain/conversation.py': (('tests.test_conversation', 'tests.test_interaction'), ()),
    'brain/playback.py': (('tests.test_playback', 'tests.test_presence', 'tests.test_interaction'), ()),
    'brain/tts.py': (('tests.test_tts', 'tests.test_speech_audio'), ()),
    'brain/speech_audio.py': (('tests.test_speech_audio',), ()),
    'brain/speech_pipeline.py': (('tests.test_speech_pipeline', 'tests.test_speech_pipeline_deadlines'), ()),
    'brain/songs.py': (('tests.test_songs',), ()),
    'brain/director.py': (('tests.test_director',), ()),
    'brain/memory.py': (('tests.test_memory', 'tests.test_memory_accuracy', 'tests.test_memory_lifecycle'), ()),
    'brain/static/presence.js': ((), ('tests/test_presence_ui.js',)),
    'brain/static/motion.js': ((), ('tests/test_motion_ui.js',)),
    'brain/static/face.js': ((), ('tests/test_face_ui.js',)),
    'brain/static/gaze.js': ((), ('tests/test_gaze_ui.js',)),
    'brain/static/interaction.js': ((), ('tests/test_interaction_ui.js',)),
    'brain/static/console.js': ((), ('tests/test_console_ui.js',)),
    'brain/static/console.css': ((), ('tests/test_console_ui.js',)),
    'brain/static/companion.css': ((), ('tests/test_page_interaction_ui.js',)),
    'brain/static/index.html': ((), ('tests/test_page_interaction_ui.js',)),
    'brain/static/bilibili-player.js': ((), ('tests/test_bilibili_player_ui.js',)),
    'brain/static/bilibili-browser.js': ((), ('tests/test_bilibili_browser_ui.js',)),
}
TOTAL_TIMEOUT = 180.0
COMMAND_TIMEOUT = 60.0
MAX_OUTPUT_BYTES = 1_000_000
OUTPUT_TAIL = 16000
_RUNNING = set()


def _relative(value, *, directory=False):
    if not isinstance(value, str) or not value or len(value) > 240:
        raise ValueError('非法升级路径')
    if directory and value.endswith('/'):
        value = value[:-1]
    if (not value or any(c in value for c in '\\:*?[]\x00') or
            any(ord(c) < 32 for c in value)):
        raise ValueError('非法升级路径')
    parts = value.split('/')
    if any(not p or p in {'.', '..'} or p.startswith('.') or p.endswith((' ', '.')) for p in parts):
        raise ValueError('非法升级路径')
    if PurePosixPath(value).is_absolute():
        raise ValueError('非法升级路径')
    return value


def validate_paths(changes, allowed_paths):
    """Return allowed changed files; directories must explicitly end with '/'."""
    if (not isinstance(changes, list) or not 1 <= len(changes) <= 100 or
            not isinstance(allowed_paths, (list, tuple)) or not 1 <= len(allowed_paths) <= 100):
        raise ValueError('升级范围不能为空或超过上限')
    allowed = []
    for value in allowed_paths:
        is_dir = isinstance(value, str) and value.endswith('/')
        allowed.append((_relative(value, directory=is_dir).casefold(), is_dir))
    result, seen = [], set()
    for change in changes:
        if not isinstance(change, dict) or change.get('operation') not in {'add', 'modify', 'delete'}:
            raise ValueError('升级变更格式错误')
        path = _relative(change.get('path'))
        folded = path.casefold()
        if folded in seen:
            raise ValueError('升级变更包含重复或大小写别名')
        seen.add(folded)
        parts = PurePosixPath(path).parts
        if (len(parts) < 2 or parts[0] not in {'brain', 'body', 'docs', 'tools'} or
                PurePosixPath(path).suffix.lower() not in EXTS):
            raise ValueError('该路径不属于允许升级的业务源码：' + path)
        if any(fnmatch.fnmatchcase(folded, pattern) for pattern in PROTECTED):
            raise ValueError('禁止升级保护文件：' + path)
        if not any(folded == entry or (is_dir and folded.startswith(entry + '/'))
                   for entry, is_dir in allowed):
            raise ValueError('改动超出本次授权范围：' + path)
        result.append(path)
    return result


def _manifest(data):
    return {name: digest(content) for name, content in data.items()}


def _fixed_candidate(review, allowed_paths):
    report = review.read()
    if not isinstance(report, dict):
        raise ValueError('审核记录格式无效')
    paths = validate_paths(report.get('changes'), allowed_paths)
    baseline = sources(review.host / 'baseline')
    if report.get('baseline_version') != _manifest(baseline):
        raise ValueError('原始源码备份版本不一致')
    candidate = dict(baseline)
    for change, name in zip(report['changes'], paths):
        old = baseline.get(name)
        if change.get('before') != (digest(old) if old is not None else None):
            raise ValueError('改动与原始源码不一致：' + name)
        operation = change['operation']
        if ((operation == 'add' and old is not None) or
                (operation in {'modify', 'delete'} and old is None)):
            raise ValueError('变更操作与原始源码不一致：' + name)
        if operation == 'delete':
            if change.get('after') is not None:
                raise ValueError('删除记录格式错误')
            candidate.pop(name)
        else:
            data = safe(review.host / 'staged', name).read_bytes()
            if digest(data) != change.get('after'):
                raise ValueError('待验证文件校验失败：' + name)
            if len(data) > 2_000_000:
                raise ValueError('待验证文件超过大小上限')
            data.decode('utf-8-sig')
            if name.endswith('.py'):
                compile(data, name, 'exec')
            candidate[name] = data
    if len({name.casefold() for name in candidate}) != len(candidate):
        raise ValueError('源码清单包含大小写别名')
    if report.get('source_version') != _manifest(candidate):
        raise ValueError('候选完整版本与审核清单不一致')
    if sum(map(len, candidate.values())) > 30_000_000:
        raise ValueError('候选源码超过大小上限')
    # Tests come exclusively from the host snapshot, never executor artifacts.
    if {n: b for n, b in candidate.items() if n.startswith('tests/')} != {
            n: b for n, b in baseline.items() if n.startswith('tests/')}:
        raise ValueError('宿主测试被修改')
    return report, candidate


def _test_plan(root, baseline, changed_paths):
    if not PYTHON_TEST_MODULES:
        raise ValueError('宿主固定测试套件为空')
    modules, nodes = list(PYTHON_TEST_MODULES), list(NODE_TEST_FILES)
    for name in changed_paths:
        if name.startswith('docs/') and name.endswith('.md'):
            continue
        extra = BUSINESS_TESTS.get(name)
        if extra is None:
            raise ValueError('业务模块尚无宿主固定验收映射：' + name)
        modules.extend(extra[0])
        nodes.extend(extra[1])
    modules, nodes = list(dict.fromkeys(modules)), list(dict.fromkeys(nodes))
    required = [name.replace('.', '/') + '.py' for name in modules]
    required.extend(nodes)
    if any(name not in baseline for name in required):
        raise ValueError('宿主固定测试套件缺少文件')
    python = root / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    executable = str(python) if python.is_file() else sys.executable
    plan = [('unittest', [executable, '-B', '-s', '-X', 'utf8', '-m', 'unittest',
                          '-q', *modules])]
    if nodes:
        node = shutil.which('node')
        if not node:
            raise ValueError('宿主缺少 Node.js，无法执行固定测试')
        plan.extend(('node', [node, name]) for name in nodes)
    return plan


def _environment(work):
    # Do not inherit API keys, PYTHONPATH, Node preload flags or user settings.
    env = {key: value for key, value in os.environ.items()
           if key.upper() in {'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT', 'PATH'}}
    private = work / '.test-runtime'
    private.mkdir()
    env.update(TEMP=str(private), TMP=str(private), TMPDIR=str(private),
               HOME=str(private), USERPROFILE=str(private),
               PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1',
               PYTHONUTF8='1', PYTHONIOENCODING='utf-8', NO_COLOR='1')
    return env


async def _terminate(process):
    if process.poll() is not None:
        return
    if os.name == 'nt':
        utility = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/taskkill.exe'
        try:
            killer = subprocess.Popen(
                [str(utility), '/PID', str(process.pid), '/T', '/F'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW)
            limit = time.monotonic() + 5
            while killer.poll() is None and time.monotonic() < limit:
                await asyncio.sleep(.025)
            if killer.poll() is None:
                killer.kill()
                killer.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()
    # Popen's wait after terminating our own process does not create a Windows
    # overlapped pipe (which restricted Windows accounts may not support).
    await asyncio.to_thread(process.wait, timeout=5)


async def run_command(argv, *, cwd, timeout, env):
    """Execute fixed argv without a shell; stop the owned tree on timeout/cancel."""
    options = ({'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
               if os.name == 'nt' else {'start_new_session': True})
    # File capture works under restricted Windows accounts without named pipes.
    # It is private/temporary and its size is checked while the process runs.
    with tempfile.TemporaryFile(mode='w+b') as output:
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                   stdout=output, stderr=subprocess.STDOUT, **options)
        deadline = time.monotonic() + timeout
        try:
            failure = None
            while process.poll() is None:
                if os.fstat(output.fileno()).st_size > MAX_OUTPUT_BYTES:
                    failure = 'output_limit'
                    break
                if time.monotonic() >= deadline:
                    failure = 'timeout'
                    break
                await asyncio.sleep(.025)
            if os.fstat(output.fileno()).st_size > MAX_OUTPUT_BYTES:
                failure = 'output_limit'
            if failure:
                await _terminate(process)
            if failure == 'output_limit':
                return {'exit_code': None, 'output': '', 'timeout': False, 'error': 'output_limit'}
            output.seek(0)
            text = output.read(MAX_OUTPUT_BYTES).decode('utf-8', errors='replace')
            return {'exit_code': None if failure else process.returncode,
                    'output': text, 'timeout': failure == 'timeout'}
        except BaseException:
            await _terminate(process)
            raise


def _save(host, report):
    target = safe(host, 'independent.json')
    pending = safe(host, 'independent-' + uuid.uuid4().hex + '.tmp')
    try:
        pending.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(pending, target)
    finally:
        pending.unlink(missing_ok=True)


async def validate_candidate(root, job_id, allowed_paths, *, runner=None):
    """Validate fixed reviewed bytes, persisting a fail-closed independent receipt.

    Injected runner: async (argv, *, cwd: Path, timeout: float, env: dict)
    -> {exit_code: int | None, output: str, timeout: bool, error?: str}.
    It is a host/test dependency; the candidate cannot select it or the suite.
    """
    if not isinstance(job_id, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', job_id):
        raise ValueError('非法升级任务编号')
    root = Path(root).resolve()
    task = safe(root, 'data/agent-tasks/' + job_id)
    host = safe(root, 'data/code-reviews/' + job_id)
    if not host.is_dir():
        raise ValueError('缺少宿主审核目录')
    key = (str(root), job_id)
    if key in _RUNNING:
        raise ValueError('该候选正在独立验证')
    _RUNNING.add(key)
    result = {'version': 1, 'job_id': job_id, 'suite': SUITE, 'passed': False,
              'reason': 'running', 'review_digest': None, 'source_version': {},
              'baseline_version': {}, 'source_before': {}, 'source_after': {},
              'commands': [], 'errors': [], 'started_at': time.time(), 'finished_at': None}
    start = time.monotonic()
    try:
        _save(host, result)  # A cancelled rerun must never leave an old passed receipt.
        review, candidate = _fixed_candidate(CodeReview(root, task, job_id), allowed_paths)
        result.update(review_digest=digest(json.dumps(review, sort_keys=True).encode()),
                      source_version=review['source_version'], baseline_version=review['baseline_version'])
        plan = _test_plan(root, candidate, [c['path'] for c in review['changes']])
        with tempfile.TemporaryDirectory(prefix='vtuber-upgrade-' + job_id + '-') as directory:
            work = Path(directory).resolve()
            for name, content in candidate.items():
                path = safe(work, name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            env = _environment(work)
            result['source_before'] = _manifest(sources(work))
            if result['source_before'] != result['source_version']:
                raise ValueError('独立测试目录版本不一致')
            execute = runner or run_command
            for kind, argv in plan:
                remaining = TOTAL_TIMEOUT - (time.monotonic() - start)
                if remaining <= 0:
                    result['reason'] = 'budget_exceeded'
                    break
                before = _manifest(sources(work))
                timeout = min(COMMAND_TIMEOUT, remaining)
                record = {'argv': argv, 'command': subprocess.list2cmdline(argv), 'cwd': str(work),
                          'exit_code': None, 'output': '', 'timeout': False, 'tests_executed': False,
                          'source_before': before, 'source_after': {}}
                result['commands'].append(record)
                try:
                    receipt = await asyncio.wait_for(execute(argv, cwd=work, timeout=timeout, env=env), timeout + 6)
                except asyncio.TimeoutError:
                    receipt = {'exit_code': None, 'output': '', 'timeout': True}
                except asyncio.CancelledError:
                    raise
                except Exception:
                    result['reason'] = 'runner_error'
                    result['errors'].append('测试进程未能执行，请检查固定运行环境和依赖')
                    break
                if not isinstance(receipt, dict) or not isinstance(receipt.get('output'), str):
                    raise ValueError('宿主测试回执格式无效')
                output = receipt['output']
                code = receipt.get('exit_code')
                after = _manifest(sources(work))
                proof = tests_executed(output, kind)
                record.update(exit_code=code, output=output[-OUTPUT_TAIL:],
                              timeout=receipt.get('timeout') is True,
                              tests_executed=proof, source_after=after)
                result['source_after'] = after
                if before != result['source_version'] or after != result['source_version']:
                    result['reason'] = 'source_changed'
                    break
                if record['timeout'] or time.monotonic() - start > TOTAL_TIMEOUT:
                    result['reason'] = 'timeout' if record['timeout'] else 'budget_exceeded'
                    break
                if receipt.get('error') or len(output.encode('utf-8')) > MAX_OUTPUT_BYTES:
                    result['reason'] = 'output_limit' if receipt.get('error') == 'output_limit' else 'invalid_receipt'
                    break
                if type(code) is not int or code != 0 or not proof:
                    result['reason'] = 'tests_failed' if type(code) is int and code != 0 else 'no_test_evidence'
                    break
            else:
                result['source_after'] = _manifest(sources(work))
                # Fixed report/staged bytes are checked again after execution.
                final_review, final_candidate = _fixed_candidate(CodeReview(root, task, job_id), allowed_paths)
                if (digest(json.dumps(final_review, sort_keys=True).encode()) != result['review_digest'] or
                        _manifest(final_candidate) != result['source_version'] or
                        result['source_after'] != result['source_version']):
                    result['reason'] = 'source_changed'
                elif time.monotonic() - start > TOTAL_TIMEOUT:
                    result['reason'] = 'budget_exceeded'
                else:
                    result.update(passed=True, reason='passed')
    except asyncio.CancelledError:
        result['reason'] = 'cancelled'
        raise
    except (OSError, ValueError, TypeError, KeyError, SyntaxError, UnicodeError) as exc:
        result.update(passed=False, reason='validation_error')
        # Paths/review policy messages are fixed host diagnostics, not arbitrary
        # process exception strings or inherited credentials.
        result['errors'].append(str(exc)[:500] if isinstance(exc, ValueError) and not isinstance(exc, UnicodeError)
                                else '独立验证未完成，请检查候选、依赖及固定测试回执')
    finally:
        result['finished_at'] = time.time()
        try:
            _save(host, result)
        finally:
            _RUNNING.discard(key)
    return result
