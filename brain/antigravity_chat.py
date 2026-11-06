"""Subscription-only Antigravity CLI transport for a tool-free persona.

The CLI keeps its own OAuth credentials. This adapter neither reads tokens nor
implements a private endpoint. Every turn gets a new workspace and conversation.
Call ``aclose`` on a stream when ending it early (as Persona already does).
"""
from __future__ import annotations

import asyncio
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile

from .antigravity_network import with_windows_proxy


class AntigravityChatError(RuntimeError):
    """Credential-safe failure suitable for a local status panel."""


_MAX_OUTPUT = 256 * 1024
_STDERR_LIMIT = 8192
_END = object()
_API_ENV = {
    'GEMINI_API_KEY', 'GOOGLE_API_KEY', 'GOOGLE_GENAI_USE_VERTEXAI',
    'GOOGLE_APPLICATION_CREDENTIALS', 'GOOGLE_CLOUD_PROJECT',
    'GOOGLE_CLOUD_LOCATION', 'GCLOUD_PROJECT', 'CLOUDSDK_CORE_PROJECT',
    'AGY_ADC_AUTH', 'GOOGLE_CLOUD_QUOTA_PROJECT',
    'CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE', 'CLOUDSDK_AUTH_ACCESS_TOKEN',
    'VERTEX_API_KEY', 'GOOGLE_GENAI_API_KEY', 'GOOGLE_GEMINI_BASE_URL',
    'GEMINI_BASE_URL', 'GOOGLE_VERTEX_BASE_URL', 'ANTHROPIC_API_KEY',
    'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL', 'OPENAI_API_KEY',
    'OPENAI_BASE_URL', 'DEEPSEEK_API_KEY', 'CODEX_API_KEY',
}


def _settings_path() -> Path:
    return Path.home() / '.gemini' / 'antigravity-cli' / 'settings.json'


def _check_subscription_settings() -> None:
    try:
        path = _settings_path()
        if path.stat().st_size > 1024 * 1024:
            raise ValueError('settings too large')
        settings = json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(settings, dict):
            raise ValueError('invalid settings')
    except (OSError, ValueError, UnicodeError):
        raise AntigravityChatError('Antigravity 套餐设置不可用，请先完成官方 CLI 设置。') from None
    if settings.get('modelProvider'):
        raise AntigravityChatError('Antigravity 配置了自定义模型提供方，已停止套餐调用。')
    if settings.get('useG1Credits') is not False:
        raise AntigravityChatError('请先关闭 Antigravity 的 Use G1 Credits，防止套餐外扣费。')


def _subscription_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items()
           if key.upper() not in _API_ENV}
    env['AGY_CLI_DISABLE_AUTO_UPDATE'] = 'true'
    return with_windows_proxy(env)


def _failure(detail: str) -> AntigravityChatError:
    """Classify only; never return upstream output, file paths, or credentials."""
    lowered = detail.lower()
    if any(term in lowered for term in ('dial tcp', 'connectex', 'proxyconnect',
                                        'connection refused', 'network is unreachable',
                                        'no such host', 'tls handshake timeout',
                                        'connection timed out', 'i/o timeout')):
        return AntigravityChatError('Antigravity 无法连接 Google，请确认终端代理可用后重试。')
    if any(term in lowered for term in ('authentication', 'unauthenticated', 'oauth',
                                        'login', 'sign in', 'credential', '401')):
        return AntigravityChatError('Antigravity 登录不可用，请在官方 CLI 重新登录 Google 账号。')
    if any(term in lowered for term in ('quota', 'resource_exhausted', 'rate limit', '429', 'credit')):
        return AntigravityChatError('Antigravity 套餐额度暂不可用，未切换到额外付费通道。')
    if any(term in lowered for term in ('model', 'not available', 'permission_denied', '403')):
        return AntigravityChatError('Antigravity 指定模型当前不可用，请检查账号权限和模型选择。')
    return AntigravityChatError('Antigravity 本次回复未完成，请检查官方 CLI 状态。')


class AntigravityChat:
    def __init__(self, command: str, model: str, effort: str = 'low',
                 timeout_sec: float = 45.0, *, runtime_dir: Path):
        self.command = str(command)
        self.model = model
        self.effort = effort
        self.timeout_sec = float(timeout_sec)
        self.runtime_dir = Path(runtime_dir).absolute()
        if (not math.isfinite(self.timeout_sec) or self.timeout_sec <= 0
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', model)):
            raise ValueError('无效的 Antigravity 模型或超时配置')
        if effort not in {'low', 'medium', 'high', 'xhigh', 'max'}:
            raise ValueError('无效的 Antigravity 推理强度')
        self._workers: set[asyncio.Task] = set()
        self._closed = False

    def _executable(self) -> str:
        executable = shutil.which(self.command)
        if not executable:
            raise AntigravityChatError('未找到官方 Antigravity CLI，请先安装 agy。')
        if os.name == 'nt' and Path(executable).suffix.lower() != '.exe':
            raise AntigravityChatError('Antigravity 需要直接运行官方 agy.exe，不能使用 shell 包装脚本。')
        return executable

    @property
    def available(self) -> bool:
        if self._closed:
            return False
        try:
            self._executable()
            _check_subscription_settings()
        except AntigravityChatError:
            return False
        return True

    def _workspace_root(self) -> Path:
        # TemporaryDirectory recursively cleans up only a verified owned root.
        for ancestor in (self.runtime_dir, *self.runtime_dir.parents):
            if ancestor.exists() or ancestor.is_symlink():
                attrs = getattr(ancestor.lstat(), 'st_file_attributes', 0)
                if ancestor.is_symlink() or attrs & 0x400:
                    raise AntigravityChatError('Antigravity 临时目录不能使用符号链接或目录联接。')
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        return self.runtime_dir.resolve(strict=True)

    async def stream(self, system_prompt: str, user_prompt: str):
        if self._closed:
            raise AntigravityChatError('Antigravity 会话通道已经关闭。')
        executable = self._executable()
        _check_subscription_settings()
        if not isinstance(system_prompt, str) or not isinstance(user_prompt, str):
            raise TypeError('人格提示和用户内容必须是文本')
        queue: asyncio.Queue = asyncio.Queue()
        worker = asyncio.create_task(self._produce(executable, system_prompt, user_prompt, queue))
        self._workers.add(worker)
        worker.add_done_callback(self._workers.discard)
        try:
            while True:
                item = await queue.get()
                if item is _END:
                    return
                if isinstance(item, AntigravityChatError):
                    raise item
                yield item
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def close(self) -> None:
        self._closed = True
        workers = list(self._workers)
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

    async def _produce(self, executable, system_prompt, user_prompt, queue):
        process = None
        stderr_task = None
        stderr = bytearray()
        error = None
        try:
            root = self._workspace_root()
            with tempfile.TemporaryDirectory(prefix='turn-', dir=root) as directory:
                workspace = Path(directory).resolve(strict=True)
                if workspace.parent != root:
                    raise AntigravityChatError('Antigravity 临时目录校验失败。')
                agent = workspace / '.agents' / 'agents' / 'avatar.md'
                agent.parent.mkdir(parents=True)
                agent.write_text(
                    '---\nname: avatar\ndescription: Persona speech only\n'
                    'mainAgent: true\nsubagent: false\ntools: []\nmcpServers: []\n'
                    'skills: []\nplugins: []\ncommandExecutionPolicy: "off"\n---\n'
                    + system_prompt + '\n', encoding='utf-8')
                args = [executable, '--agent', 'avatar', '--model', self.model,
                        '--effort', self.effort, '--disable-slash-commands',
                        '--input-format', 'stream-json', '--output-format', 'stream-json',
                        '--print-timeout', f'{self.timeout_sec:g}s']
                options = ({'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt'
                           else {'start_new_session': True})
                try:
                    spawning = asyncio.create_task(asyncio.create_subprocess_exec(
                        *args, cwd=str(workspace), env=_subscription_env(),
                        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE, limit=_MAX_OUTPUT, **options))
                    try:
                        process = await asyncio.shield(spawning)
                    except asyncio.CancelledError:
                        # Retain ownership if cancellation races process startup.
                        process = await spawning
                        raise
                    stderr_task = asyncio.create_task(self._read_stderr(process, stderr))
                    await asyncio.wait_for(self._exchange(process, user_prompt, queue, stderr),
                                           timeout=self.timeout_sec)
                finally:
                    # Shield cleanup against a second cancellation while closing.
                    cleanup = asyncio.create_task(self._stop_process(process, stderr_task))
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        await cleanup
                        raise
        except asyncio.CancelledError:
            error = AntigravityChatError('Antigravity 本次回复已取消。')
        except asyncio.TimeoutError:
            error = AntigravityChatError('Antigravity 回复等待超时，已停止本次调用。')
        except AntigravityChatError as exc:
            error = exc
        except Exception:
            error = _failure(bytes(stderr).decode('utf-8', errors='replace'))
        finally:
            if error is not None:
                queue.put_nowait(error)
            queue.put_nowait(_END)

    @staticmethod
    async def _read_stderr(process, buffer):
        while True:
            data = await process.stderr.read(4096)
            if not data:
                return
            # Keep a bounded tail for classification; never log it.
            buffer.extend(data)
            del buffer[:-_STDERR_LIMIT]

    async def _exchange(self, process, user_prompt, queue, stderr):
        initialized = False
        emitted = ''
        result_received = False
        async for raw in process.stdout:
            try:
                event = json.loads(raw)
            except (ValueError, UnicodeError):
                raise AntigravityChatError('Antigravity 返回了无效的事件流。') from None
            if not isinstance(event, dict):
                raise AntigravityChatError('Antigravity 返回了无效的事件流。')
            kind = event.get('event')
            if kind == 'init':
                init = event.get('init')
                if initialized or not isinstance(init, dict) or init.get('tools') != []:
                    raise AntigravityChatError('Antigravity 未确认零工具模式，已停止发送聊天内容。')
                initialized = True
                # No user input reaches the process before its capability check.
                payload = {'event': 'user', 'message': {'content': user_prompt}}
                process.stdin.write((json.dumps(payload, ensure_ascii=False) + '\n').encode('utf-8'))
                await process.stdin.drain()
                process.stdin.close()
            elif kind == 'step_update':
                if not initialized:
                    raise AntigravityChatError('Antigravity 未初始化就开始了回复。')
                step = event.get('step_update')
                if not isinstance(step, dict):
                    raise AntigravityChatError('Antigravity 返回了无效的事件流。')
                step_type = step.get('step_type', '')
                if 'tool' in str(step_type).lower():
                    raise AntigravityChatError('Antigravity 意外尝试工具操作，本次回复已停止。')
                if step_type != 'agent_response':
                    continue
                delta = step.get('text_delta', '')
                if not isinstance(delta, str):
                    raise AntigravityChatError('Antigravity 返回了无效的回复正文。')
                emitted += delta
                if len(emitted.encode('utf-8')) > _MAX_OUTPUT:
                    raise AntigravityChatError('Antigravity 回复超过长度限制。')
                if delta:
                    queue.put_nowait(delta)
            elif kind == 'result':
                result = event.get('result')
                if not isinstance(result, dict):
                    raise AntigravityChatError('Antigravity 返回了无效的结果。')
                if result.get('status') != 'SUCCESS':
                    raise _failure(str(result.get('error', ''))[:_STDERR_LIMIT])
                if not initialized:
                    raise AntigravityChatError('Antigravity 未确认零工具模式。')
                response = result.get('response')
                if (not isinstance(response, str) or not response.strip()
                        or len(response.encode('utf-8')) > _MAX_OUTPUT
                        or not response.startswith(emitted)):
                    raise AntigravityChatError('Antigravity 最终回复与事件流不一致。')
                if response[len(emitted):]:
                    queue.put_nowait(response[len(emitted):])
                result_received = True
                break
        code = await process.wait()
        if code != 0 or not result_received:
            # The stderr reader can have its final read pending at process exit.
            await asyncio.sleep(0)
            raise _failure(bytes(stderr).decode('utf-8', errors='replace'))

    @staticmethod
    async def _stop_process(process, stderr_task):
        try:
            if process is not None and process.returncode is None:
                try:
                    process.stdin.close()
                except (OSError, RuntimeError):
                    pass
                if os.name == 'nt':
                    # Kill only the child tree created for this turn.
                    killer = None
                    try:
                        killer = await asyncio.create_subprocess_exec(
                            'taskkill.exe', '/PID', str(process.pid), '/T', '/F',
                            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                            creationflags=subprocess.CREATE_NO_WINDOW)
                        await asyncio.wait_for(killer.wait(), 5)
                    except (OSError, asyncio.TimeoutError):
                        # Still reap the owned CLI if Windows cannot start taskkill.
                        if killer is not None and killer.returncode is None:
                            killer.kill()
                            await killer.wait()
                else:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                await process.wait()
            elif process is not None:
                await process.wait()
        finally:
            if stderr_task:
                stderr_task.cancel()
                await asyncio.gather(stderr_task, return_exceptions=True)
