"""Owned stdio transports. Never print credentials or inherit API fallback keys."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
from contextlib import suppress

from .codex_runtime import CodexNotFoundError, resolve_codex_command


class JsonProcess:
    def __init__(self, command, cwd, env=None):
        self.command, self.cwd, self.env = command, cwd, env
        self.process = None
        self.pending = {}
        self.events = asyncio.Queue()
        self.serial = 0
        self.reader = None
        self.stderr = None

    async def start(self):
        self.process = await asyncio.create_subprocess_exec(
            *self.command, cwd=str(self.cwd), env=self.env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=2**22,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        self.reader = asyncio.create_task(self._read())
        self.stderr = asyncio.create_task(self._discard_stderr())

    async def _discard_stderr(self):
        while await self.process.stderr.read(65536):
            pass

    async def _read(self):
        try:
            async for raw in self.process.stdout:
                try:
                    msg = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    continue
                key = msg.get('id')
                is_response = 'result' in msg or 'error' in msg or msg.get('type') == 'response'
                future = self.pending.get(key) if is_response else None
                if future is not None and not future.done():
                    if 'error' in msg or msg.get('success') is False:
                        future.set_exception(RuntimeError(str(msg.get('error', 'RPC failed'))[:1000]))
                    else:
                        future.set_result(msg.get('result', msg.get('data', {})))
                else:
                    await self.events.put(msg)
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(RuntimeError('任务运行进程已退出'))
            await self.events.put({'type': 'transport_closed'})

    async def send(self, message):
        self.process.stdin.write((json.dumps(message, ensure_ascii=False) + '\n').encode())
        await self.process.stdin.drain()

    async def call(self, method, params=None, *, omp=False, timeout=40):
        self.serial += 1
        key = f'client-{self.serial}'
        future = asyncio.get_running_loop().create_future()
        self.pending[key] = future
        try:
            await self.send({'id': key, 'type': method, **(params or {})} if omp else
                            {'id': key, 'method': method, 'params': params or {}})
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(key, None)

    async def close(self):
        if self.process and self.process.returncode is None:
            with suppress(OSError):
                self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), 8)
            except asyncio.TimeoutError:
                # Only this owned process tree; no name-based process killing.
                if os.name == 'nt':
                    killer = await asyncio.create_subprocess_exec(
                        'taskkill', '/PID', str(self.process.pid), '/T', '/F',
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                        creationflags=subprocess.CREATE_NO_WINDOW)
                    await killer.wait()
                else:
                    self.process.kill()
                await self.process.wait()
        for task in (self.reader, self.stderr):
            if task:
                task.cancel()
        await asyncio.gather(*(t for t in (self.reader, self.stderr) if t), return_exceptions=True)
        # Windows pipes may outlive process.wait(), especially when an inherited
        # handle has not reached EOF. Close only this process' owned transports
        # while the event loop is still alive rather than in __del__ at shutdown.
        transport = getattr(self.process, '_transport', None)
        if transport is not None:
            transport.close()
            await asyncio.sleep(0)


def subscription_env():
    env = dict(os.environ)
    for key in ('OPENAI_API_KEY', 'CODEX_API_KEY', 'OPENAI_BASE_URL'):
        env.pop(key, None)
    return env


async def codex_client(executable, cwd, *, config_overrides=None):
    executable = resolve_codex_command(executable)
    if not executable:
        raise CodexNotFoundError('找不到本机 Codex 客户端，请检查 agent_tasks.codex_command。')
    command = [executable, 'app-server', '--listen', 'stdio://',
               '-c', 'model_provider="openai"', '-c', 'forced_login_method="chatgpt"']
    for key, value in (config_overrides or {}).items():
        command.extend(['-c', key + '=' + json.dumps(value, ensure_ascii=False)])
    rpc = JsonProcess(command, cwd, subscription_env())
    try:
        await rpc.start()
        await rpc.call('initialize', {'clientInfo': {'name': 'avatar', 'version': '1.0.0'},
                                      'capabilities': {'experimentalApi': True}})
        await rpc.send({'method': 'initialized'})
        return rpc
    except BaseException:
        await rpc.close()
        raise


async def subscription_status(rpc):
    account = (await rpc.call('account/read')).get('account') or {}
    if account.get('type') != 'chatgpt':
        raise RuntimeError('需要 Codex 的 ChatGPT 套餐登录；已拒绝 API Key 通道。请运行 codex login。')
    models, cursor = [], None
    for _ in range(10):
        page = await rpc.call('model/list', {'limit': 100, 'includeHidden': False, 'cursor': cursor})
        models.extend(page.get('data', []))
        cursor = page.get('nextCursor')
        if not cursor:
            break
    available = any(m.get('model') == 'gpt-6-astra' for m in models)
    limits = await rpc.call('account/rateLimits/read')
    return {'auth': 'chatgpt', 'plan': account.get('planType'), 'astra': available,
            'rateLimits': limits.get('rateLimits') or {},
            'rateLimitsByLimitId': limits.get('rateLimitsByLimitId') or {}}


def subscription_remaining(status):
    """Conservative remaining percentage; unknown is never an unlimited grant."""
    import math
    buckets = status.get('rateLimitsByLimitId') or {}
    if not isinstance(buckets, dict):
        return None
    selected = list(buckets.values()) if buckets else [status.get('rateLimits')]
    remaining = []
    for bucket in selected:
        if not isinstance(bucket, dict):
            return None
        if bucket.get('spendControlReached') or bucket.get('rateLimitReachedType'):
            return 0
        for name in ('primary', 'secondary', 'individualLimit'):
            window = bucket.get(name)
            if not isinstance(window, dict):
                continue
            used = window.get('usedPercent')
            if type(used) not in (int, float) or not math.isfinite(used) or used < 0:
                return None
            remaining.append(max(0, 100 - used))
    return min(remaining) if remaining else None
