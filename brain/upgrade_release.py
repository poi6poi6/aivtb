"""Host-owned release activation. This process never force-kills another process.

The AI produces candidates; this controller consumes host review/validation
records, owns the stop/apply/start transaction, and survives the main process.
"""
from __future__ import annotations

import contextlib
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from .code_review import digest, safe, sources

TERMINAL = {'verified', 'rolled_back', 'cancelled', 'failed', 'recovery_blocked'}
ID = re.compile(r'^[A-Za-z0-9_-]{1,80}$')


class ReleaseCancelled(ValueError):
    pass


def workspace_id(root):
    return hashlib.sha256(os.path.normcase(str(Path(root).resolve())).encode('utf-8')).hexdigest()


def process_identity(pid):
    """Read an OS creation identifier; None means absent, errors mean unknown."""
    if type(pid) is not int or pid <= 0:
        raise ValueError('Invalid process identity')
    if os.name == 'nt':
        from ctypes import wintypes
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = api.OpenProcess(0x1000, False, pid)
        if not handle:
            if ctypes.get_last_error() == 87:
                return None
            raise OSError('Cannot inspect process')
        try:
            code = wintypes.DWORD()
            if not api.GetExitCodeProcess(handle, ctypes.byref(code)):
                raise OSError('Cannot inspect process')
            if code.value != 259:
                return None
            times = [wintypes.FILETIME() for _ in range(4)]
            if not api.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
                raise OSError('Cannot inspect process creation time')
            return 'win:' + str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
        finally:
            api.CloseHandle(handle)
    try:
        raw = Path(f'/proc/{pid}/stat').read_text()
        fields = raw[raw.rfind(')') + 2:].split()
        if fields[0] == 'Z':
            return None
        return 'proc:' + fields[19]
    except FileNotFoundError:
        return None


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + secrets.token_hex(8) + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path):
    path = Path(path)
    if path.stat().st_size > 16_000_000:
        raise ValueError('Release record too large')
    return json.loads(path.read_text(encoding='utf-8'))


def local_health_url(value):
    parsed = urlsplit(value)
    if (parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost', '::1'}
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path != '/runtime-health' or not parsed.port):
        raise ValueError('Only the configured local main health endpoint is allowed')
    return value


def release_policy(root, *, manual=False):
    try:
        state = read_json(safe(Path(root), 'data/self-upgrade/state.json'))
    except FileNotFoundError:
        state = {}
    policy = state.get('policy', {})
    if policy.get('mode') not in ({'develop', 'auto'} if manual else {'auto'}):
        raise ValueError('Release deployment is disabled by policy')
    if not manual and state.get('paused'):
        raise ValueError('Automatic upgrade is paused')
    paths = policy.get('allowed_paths')
    if not isinstance(paths, list) or not paths:
        raise ValueError('No release paths are authorized')
    return paths


def configured_health_url(root):
    # Read endpoint configuration only; do not load dotenv or log settings.
    import yaml
    path = safe(Path(root), 'config/settings.yaml')
    settings = yaml.safe_load(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
    web = (settings or {}).get('webui', {})
    host, port = web.get('host', '127.0.0.1'), web.get('port', 8080)
    if host in {'0.0.0.0', '::'}:
        host = '127.0.0.1'
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('Invalid main service port')
    return local_health_url(f'http://{"[::1]" if host == "::1" else host}:{port}/runtime-health')


@contextlib.contextmanager
def release_lock(root):
    path = safe(Path(root), 'data/upgrades/release.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open('a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            stream.seek(0)
            if not stream.read(1):
                stream.write(b'0'); stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        stream.close()
        raise ValueError('Another release controller is running') from None
    try:
        yield
    finally:
        stream.close()


class MainReleaseGate:
    """Small host-only gate polled by main; a request is bound to this boot."""
    def __init__(self, root, *, boot_id=None):
        self.root = Path(root).resolve()
        self.boot_id = boot_id or secrets.token_hex(8)
        self.instance = {'workspace_id': workspace_id(self.root), 'boot_id': self.boot_id,
                         'process': {'pid': os.getpid(), 'identity': process_identity(os.getpid())}}
        for name in ('launcher', 'cleanup'):
            prefix = 'AI_VTUBER_' + name.upper()
            raw = os.environ.get(prefix + '_PID', '')
            identity = os.environ.get(prefix + '_IDENTITY', '')
            if raw.isdigit() and identity:
                self.instance[name] = {'pid': int(raw), 'identity': identity}
        release_id = os.environ.get('AI_VTUBER_RELEASE_ID', '')
        if ID.fullmatch(release_id):
            self.instance['release_id'] = release_id
        atomic_json(safe(self.root, 'data/upgrades/main-instance.json'), self.instance)

    def health_fields(self):
        return dict(self.instance)

    def stop_requested(self, *, idle=True):
        if not idle:
            return False
        path = safe(self.root, 'data/upgrades/stop-main.json')
        try:
            request = read_json(path)
        except (OSError, ValueError):
            return False
        def matches(value):
            return (isinstance(value, dict) and value.get('version') == 1 and ID.fullmatch(str(value.get('request_id', '')))
                and value.get('workspace_id') == self.instance['workspace_id']
                and value.get('expected_boot') == self.boot_id
                and value.get('process') == self.instance['process'])
        if not matches(request):
            return False
        # Claim versus withdraw is decided by an atomic rename. A controller
        # cannot retract a request that main has already accepted for shutdown.
        claim = safe(self.root, 'data/upgrades/stop-claimed.json')
        try:
            os.replace(path, claim)
            request = read_json(claim)
        except (OSError, ValueError):
            return False
        if not matches(request):
            return False
        atomic_json(safe(self.root, 'data/upgrades/stop-ack.json'), request)
        return True


class LocalRuntime:
    """Only loopback health and the fixed project launcher are executable here."""
    def __init__(self, root, health_url):
        self.root = Path(root).resolve()
        self.health_url = local_health_url(health_url)
        self.now, self.sleep = time.monotonic, time.sleep

    def health(self):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        try:
            with opener.open(self.health_url, timeout=2) as response:
                raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise ValueError('Health response too large')
                return json.loads(raw)
        except (urllib.error.URLError, TimeoutError):
            return None

    identity = staticmethod(process_identity)

    def request_stop(self, instance, request_id):
        atomic_json(safe(self.root, 'data/upgrades/stop-main.json'), {
            'version': 1, 'request_id': request_id, 'expected_boot': instance['boot_id'],
            'workspace_id': instance['workspace_id'], 'process': instance['process']})

    def stop_acknowledged(self, instance, request_id):
        for name in ('stop-claimed.json', 'stop-ack.json'):
            try:
                value = read_json(safe(self.root, 'data/upgrades/' + name))
                if (isinstance(value, dict) and value.get('request_id') == request_id and value.get('expected_boot') == instance['boot_id']
                        and value.get('process') == instance['process']):
                    return True
            except (OSError, ValueError):
                pass
        return False

    def withdraw_stop(self, instance, request_id):
        path = safe(self.root, 'data/upgrades/stop-main.json')
        try:
            value = read_json(path)
            if (isinstance(value, dict) and value.get('request_id') == request_id and value.get('expected_boot') == instance['boot_id']
                    and value.get('process') == instance['process']):
                withdrawn = path.with_name('withdrawn-' + secrets.token_hex(8) + '.json')
                os.replace(path, withdrawn)
                withdrawn.unlink(missing_ok=True)
        except FileNotFoundError:
            pass

    def launch(self, release_id):
        script = safe(self.root, 'tools/launcher/start-all.ps1')
        if not script.is_file():
            raise ValueError('Launcher is missing')
        folder = safe(self.root, 'data/upgrades')
        env = dict(os.environ, AI_VTUBER_RELEASE_ID=release_id)
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        with (folder / 'launcher.log').open('ab') as log:
            child = subprocess.Popen(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                '-File', str(script), '-NoBrowser'], cwd=self.root, env=env,
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, creationflags=flags)
        return {'pid': child.pid, 'identity': process_identity(child.pid) or 'exited'}


class ReleaseController:
    def __init__(self, root, *, allowed_paths, runtime, stop_timeout=45, start_timeout=300):
        self.root = Path(root).resolve()
        self.allowed_paths = allowed_paths
        self.runtime = runtime
        self.stop_timeout, self.start_timeout = stop_timeout, start_timeout
        self.path = safe(self.root, 'data/upgrades/release.json')
        self.journal = None

    def save(self, state, **fields):
        self.journal.update(fields, state=state, updated_at=time.time())
        atomic_json(self.path, self.journal)

    def manifest(self):
        return {name: digest(data) for name, data in sources(self.root).items()}

    def validated_review(self, job_id):
        from .upgrade_validation import validate_paths, SUITE
        if not isinstance(job_id, str) or not ID.fullmatch(job_id):
            raise ValueError('Invalid code review ID')
        host = safe(self.root, 'data/code-reviews/' + job_id)
        report = read_json(safe(host, 'review.json'))
        independent = read_json(safe(host, 'independent.json'))
        if (not report.get('can_apply') or not report.get('changes') or independent.get('version') != 1
                or independent.get('job_id') != job_id or independent.get('passed') is not True
                or independent.get('suite') != SUITE
                or independent.get('review_digest') != digest(json.dumps(report, sort_keys=True).encode())
                or any(independent.get(key) != report.get(key) for key in ('source_version', 'baseline_version'))
                or independent.get('source_before') != report.get('source_version')
                or independent.get('source_after') != report.get('source_version')):
            raise ValueError('Independent validation does not match the reviewed candidate')
        validate_paths(report['changes'], self.allowed_paths)
        baseline, candidate = report.get('baseline_version'), report.get('source_version')
        if not isinstance(baseline, dict) or not isinstance(candidate, dict) or not baseline or not candidate:
            raise ValueError('Complete source manifests are required')
        changes = {}
        for change in report['changes']:
            name = change['path']
            if name in changes or change.get('before') != baseline.get(name) or change.get('after') != candidate.get(name):
                raise ValueError('Inconsistent source change record')
            changes[name] = change
            for kind, expected in (('baseline', change['before']), ('staged', change['after'])):
                if expected is not None:
                    data = safe(host / kind, name).read_bytes()
                    if digest(data) != expected:
                        raise ValueError('Reviewed source bytes have changed')
        expected_changes = {name for name in baseline.keys() | candidate.keys() if baseline.get(name) != candidate.get(name)}
        if set(changes) != expected_changes:
            raise ValueError('Manifest changes differ from reviewed files')
        return report, host

    def owns(self, instance, *, expected_boot=None):
        if (not isinstance(instance, dict) or instance.get('workspace_id') != workspace_id(self.root)
                or not re.fullmatch(r'[a-f0-9]{16}', str(instance.get('boot_id', '')))
                or (expected_boot is not None and instance.get('boot_id') != expected_boot)):
            return False
        process = instance.get('process', {})
        return (type(process.get('pid')) is int and isinstance(process.get('identity'), str)
                and self.runtime.identity(process['pid']) == process['identity'])

    def gone(self, process):
        if not isinstance(process, dict) or type(process.get('pid')) is not int or not process.get('identity'):
            return False
        # PID reuse is not the same instance and must never be signalled.
        return self.runtime.identity(process['pid']) != process['identity']

    def cancelled(self):
        try:
            return read_json(safe(self.root, 'data/upgrades/cancel.json')).get('release_id') == self.journal['release_id']
        except (OSError, ValueError):
            return False

    def stop(self, instance):
        if instance.get('workspace_id') != workspace_id(self.root):
            raise ValueError('Cannot stop another workspace')
        if self.owns(instance):
            self.runtime.request_stop(instance, self.journal['release_id'])
        elif not self.gone(instance.get('process')):
            raise ValueError('Cannot stop an unidentified main instance')
        deadline = self.runtime.now() + self.stop_timeout
        while self.runtime.now() < deadline:
            if (self.gone(instance['process']) and all(self.gone(instance[name])
                    for name in ('launcher', 'cleanup') if name in instance)):
                return
            if (self.cancelled() and not self.gone(instance['process'])
                    and not getattr(self.runtime, 'stop_acknowledged', lambda *_: False)(instance, self.journal['release_id'])):
                getattr(self.runtime, 'withdraw_stop', lambda *_: None)(instance, self.journal['release_id'])
                raise ReleaseCancelled('Release cancelled before shutdown was accepted')
            self.runtime.sleep(.25)
        getattr(self.runtime, 'withdraw_stop', lambda *_: None)(instance, self.journal['release_id'])
        raise TimeoutError('The known main instance has not completed graceful shutdown')

    def rewrite(self, report, host, *, previous=False):
        baseline, candidate = report['baseline_version'], report['source_version']
        # After a crash, each reviewed path may contain either exact version;
        # an unrelated edit must never be overwritten by recovery.
        current = self.manifest()
        changed = {change['path'] for change in report['changes']}
        for name in baseline.keys() | candidate.keys() | current.keys():
            allowed = {baseline.get(name), candidate.get(name)} if name in changed else {baseline.get(name)}
            if current.get(name) not in allowed:
                raise ValueError('Unrelated source change blocks release recovery')
        for change in report['changes']:
            name = change['path']
            wanted = change['before' if previous else 'after']
            destination = safe(self.root, name)
            if wanted is None:
                destination.unlink(missing_ok=True)
                continue
            data = safe(host / ('baseline' if previous else 'staged'), name).read_bytes()
            if digest(data) != wanted:
                raise ValueError('Recovery source bytes have changed')
            destination.parent.mkdir(parents=True, exist_ok=True)
            temp = destination.with_name(destination.name + '.release-' + self.journal['release_id'] + '.tmp')
            try:
                with temp.open('wb') as stream:
                    stream.write(data); stream.flush(); os.fsync(stream.fileno())
                os.replace(temp, destination)
            finally:
                temp.unlink(missing_ok=True)
        if self.manifest() != (baseline if previous else candidate):
            raise ValueError('Applied source manifest mismatch')

    def await_started(self, manifest, prior_boot, *, allow_cancel=True):
        deadline = self.runtime.now() + self.start_timeout
        while self.runtime.now() < deadline:
            if allow_cancel and self.cancelled():
                raise ValueError('Release cancelled during startup')
            health = self.runtime.health()
            if (self.owns(health) and health.get('scope') == 'main' and health.get('ready') is True
                    and health.get('boot_id') != prior_boot and health.get('code') == manifest
                    and health.get('release_id') == self.journal['release_id'] and self.manifest() == manifest):
                return health
            self.runtime.sleep(.5)
        raise TimeoutError('New main instance did not verify before the deadline')

    def current_candidate(self):
        health = self.runtime.health()
        if self.owns(health) and health.get('release_id') == self.journal['release_id']:
            return health
        try:
            instance = read_json(safe(self.root, 'data/upgrades/main-instance.json'))
        except (OSError, ValueError):
            instance = None
        if self.owns(instance) and instance.get('release_id') == self.journal['release_id']:
            return instance
        return None

    def restore(self, report, host):
        candidate = self.current_candidate()
        if candidate:
            self.stop(candidate)
        elif self.journal.get('new_launcher') and not self.gone(self.journal['new_launcher']):
            raise ValueError('Candidate launcher is alive without a verifiable main identity')
        elif (self.journal.get('launch_pending') or self.journal['state'] in {'starting', 'restarting_previous'}) and not self.journal.get('new_launcher'):
            # Preserve uncertainty across repeated --recover invocations; the
            # recovery_blocked label must not erase the unrecorded-spawn guard.
            self.journal['launch_pending'] = True
            raise ValueError('Launcher dispatch outcome is unknown; source files were retained')
        old = self.journal.get('old_instance', {})
        self.stop(old)
        self.save('rolling_back')
        self.rewrite(report, host, previous=True)
        self.save('restarting_previous', new_launcher=None, launch_pending=True)
        launched = self.runtime.launch(self.journal['release_id'])
        self.save('restarting_previous', new_launcher=launched, launch_pending=False)
        health = self.await_started(report['baseline_version'], old.get('boot_id'), allow_cancel=False)
        atomic_json(host / 'rolled-back.json', {'files': len(report['changes']), 'release_id': self.journal['release_id']})
        self.save('rolled_back', runtime_boot=health['boot_id'], detail='旧版本已恢复并通过健康检查')

    def run(self, release_id=None, *, recover=False):
        with release_lock(self.root):
            self.journal = read_json(self.path)
            if release_id is not None and self.journal.get('release_id') != release_id:
                raise ValueError('Release request is stale')
            if self.journal.get('state') in TERMINAL and not (recover and self.journal['state'] == 'recovery_blocked'):
                return dict(self.journal)
            if self.journal.get('workspace_id') != workspace_id(self.root):
                raise ValueError('Release belongs to another workspace')
            try:
                # Recovery remains possible after an owner disables automatic mode.
                if not recover or self.journal['state'] in {'queued', 'prepared'}:
                    self.allowed_paths = release_policy(self.root, manual=self.journal.get('manual') is True)
                report, host = self.validated_review(self.journal['job_id'])
                if self.journal.get('review_digest') != digest(json.dumps(report, sort_keys=True).encode()):
                    raise ValueError('Release review changed')
            except Exception as error:
                state = 'failed' if self.journal['state'] in {'queued', 'prepared'} else 'recovery_blocked'
                self.save(state, error_type=type(error).__name__, detail='授权或审核版本检查未通过，未继续部署')
                return dict(self.journal)
            try:
                if recover and self.journal['state'] not in {'queued', 'prepared'}:
                    health = self.runtime.health()
                    if (self.owns(health) and health.get('release_id') == self.journal['release_id']
                            and health.get('ready') is True and health.get('code') == report['source_version']
                            and self.manifest() == report['source_version']):
                        self.save('verified', runtime_boot=health['boot_id'], launch_pending=False)
                    elif (self.owns(health) and health.get('release_id') == self.journal['release_id']
                            and health.get('ready') is True and health.get('code') == report['baseline_version']
                            and self.manifest() == report['baseline_version']
                            and health.get('boot_id') != self.journal['expected_boot']):
                        self.save('rolled_back', runtime_boot=health['boot_id'], launch_pending=False, detail='旧版本恢复已确认')
                    else:
                        self.restore(report, host)
                    return dict(self.journal)
                if self.journal['state'] not in {'queued', 'prepared'}:
                    raise ValueError('Interrupted release requires explicit recovery')
                if self.cancelled():
                    self.save('cancelled'); return dict(self.journal)
                if self.manifest() != report['baseline_version']:
                    raise ValueError('The tested baseline is no longer current')
                health = self.runtime.health()
                if (not self.owns(health, expected_boot=self.journal['expected_boot'])
                        or health.get('scope') != 'main' or health.get('code') != report['baseline_version']
                        or health.get('upgrade_idle') is not True
                        or not all(isinstance(health.get(name), dict) and not self.gone(health[name])
                                   for name in ('launcher', 'cleanup'))):
                    raise ValueError('Main must be identified, idle, and owned by the managed launcher')
                self.save('prepared', old_instance={key: health[key] for key in
                    ('workspace_id', 'boot_id', 'process', 'launcher', 'cleanup')})
                if self.cancelled():
                    self.save('cancelled'); return dict(self.journal)
                self.save('stopping')
                self.stop(health)
                self.save('stopped')
                # Do not trust artifacts that changed while waiting for shutdown.
                self.validated_review(self.journal['job_id'])
                if self.cancelled():
                    self.restore(report, host); return dict(self.journal)
                self.save('applying')
                self.rewrite(report, host)
                atomic_json(host / 'applied.json', {'files': len(report['changes']), 'release_id': self.journal['release_id']})
                self.save('starting', new_launcher=None, launch_pending=True)
                launched = self.runtime.launch(self.journal['release_id'])
                self.save('verifying', new_launcher=launched, launch_pending=False)
                current = self.await_started(report['source_version'], health['boot_id'])
                if self.cancelled():
                    self.restore(report, host)
                else:
                    self.save('verified', runtime_boot=current['boot_id'], detail='新版本源码与健康检查已确认')
            except Exception as error:
                failed_state = self.journal['state']
                if failed_state in {'stopped', 'applying', 'starting', 'verifying'}:
                    try:
                        self.restore(report, host)
                    except Exception as recovery_error:
                        self.save('recovery_blocked', error_type=type(error).__name__,
                                  recovery_error_type=type(recovery_error).__name__, detail='恢复未完成；未知进程或文件未被强制覆盖')
                elif failed_state in {'rolling_back', 'restarting_previous', 'recovery_blocked'}:
                    self.save('recovery_blocked', error_type=type(error).__name__, detail='旧版本恢复未确认；请使用恢复操作继续检查')
                elif failed_state == 'stopping' and (self.gone(self.journal['old_instance']['process'])
                        or getattr(self.runtime, 'stop_acknowledged', lambda *_: False)(
                            self.journal['old_instance'], self.journal['release_id'])):
                    self.save('recovery_blocked', error_type=type(error).__name__, detail='旧实例已接受停止，清理尚未确认；请恢复后再部署')
                elif isinstance(error, ReleaseCancelled):
                    self.save('cancelled', detail='停机尚未接受，升级请求已撤回')
                else:
                    self.save('failed', error_type=type(error).__name__, detail='升级未执行；原运行实例或文件需确认')
            return dict(self.journal)


def launch_release(root, job_id, *, expected_boot, manual=False):
    """Called by the host manager after authorization, never from model arguments."""
    root = Path(root).resolve()
    allowed_paths = release_policy(root, manual=manual)
    health_url = configured_health_url(root)
    controller = ReleaseController(root, allowed_paths=allowed_paths or [], runtime=LocalRuntime(root, health_url))
    report, host = controller.validated_review(job_id)
    if (host / 'applied.json').exists():
        raise ValueError('This candidate has already been applied; create a new reviewed revision')
    with release_lock(root):
        if controller.path.exists() and read_json(controller.path).get('state') not in TERMINAL - {'recovery_blocked'}:
            raise ValueError('A release is already pending')
        release_id = secrets.token_hex(16)
        journal = {'version': 1, 'release_id': release_id, 'job_id': job_id, 'state': 'queued',
                   'expected_boot': expected_boot, 'manual': bool(manual), 'health_url': local_health_url(health_url),
                   'allowed_paths': list(allowed_paths or []), 'created_at': time.time(),
                   'workspace_id': workspace_id(root), 'review_digest': digest(json.dumps(report, sort_keys=True).encode())}
        atomic_json(controller.path, journal)
    # The queued journal reserves the request. Release the lock before starting
    # its child so a fast child cannot race the parent's still-held process lock.
    flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS if os.name == 'nt' else 0
    with (controller.path.parent / 'controller.log').open('ab') as log:
        try:
            subprocess.Popen([sys.executable, str(safe(root, 'tools/run_upgrade_release.py')),
                '--release-id', release_id], cwd=root, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                creationflags=flags, start_new_session=os.name != 'nt')
        except OSError:
            with release_lock(root):
                current = read_json(controller.path)
                if current.get('release_id') == release_id and current.get('state') == 'queued':
                    journal.update(state='failed', detail='外置升级控制器未能启动')
                    atomic_json(controller.path, journal)
            raise
    return {'ok': True, 'queued': True, 'release_id': release_id}


def release_status(root):
    try:
        journal = read_json(safe(Path(root).resolve(), 'data/upgrades/release.json'))
    except FileNotFoundError:
        return {'state': 'idle'}
    keys = ('state', 'release_id', 'job_id', 'detail', 'created_at', 'updated_at', 'runtime_boot', 'error_type', 'recovery_error_type')
    return {key: journal[key] for key in keys if key in journal}


def cancel_release(root, release_id=None):
    root = Path(root).resolve()
    if release_id is None:
        release_id = release_status(root).get('release_id')
    if release_id is None:
        return {'ok': True, 'state': 'idle'}
    if not isinstance(release_id, str) or not ID.fullmatch(release_id):
        raise ValueError('Invalid release identifier')
    atomic_json(safe(Path(root).resolve(), 'data/upgrades/cancel.json'), {'release_id': release_id})
    return {'ok': True}
