"""Independent, bounded TTS health checks; never synthesize to test readiness."""
from __future__ import annotations

import asyncio
import json
import os
import time
from urllib.parse import urlsplit

import httpx

from .upgrade_release import process_identity


class TTSReadiness:
    def __init__(self, cfg, *, probe=None, clock=time.monotonic, interval_sec=3,
                 startup_timeout=240, env=None, identity=process_identity):
        self.cfg, self.clock = cfg, clock
        self._probe = probe or self._health
        self.interval = max(.05, min(30., float(interval_sec)))
        self.startup_timeout = max(0., min(240., float(startup_timeout)))
        self._env = os.environ if env is None else env
        self._identity = identity
        self._task = self._client = None
        self._closed = False
        self._started = clock()
        self._owned_pid, self._owned_identity = None, None
        launched = self._env.get('AI_VTUBER_TTS_BACKEND') == cfg.backend
        self._loading = launched and self._env.get('AI_VTUBER_TTS_STARTUP_STATE') == 'loading'
        if self._loading:
            value = self._env.get('AI_VTUBER_TTS_OWNED_PID', '')
            identity_value = self._env.get('AI_VTUBER_TTS_OWNED_IDENTITY', '')
            if isinstance(value, str) and value.isdigit() and identity_value:
                self._owned_pid, self._owned_identity = int(value), identity_value
        state = 'ready' if cfg.backend == 'mock' else 'loading' if self._loading else 'checking'
        if cfg.backend != 'mock' and launched and self._env.get('AI_VTUBER_TTS_STARTUP_STATE') == 'unavailable':
            state = 'unavailable'
        self._status = dict(backend=cfg.backend, state=state, ready=state == 'ready',
                            detail=self._detail(state), checked_at=None)

    def _detail(self, state):
        if self.cfg.backend == 'mock' and state != 'stopped':
            return '当前使用模拟语音，不需要语音服务。'
        return {'checking': '正在检查语音服务，文字功能可以使用。',
                'loading': '语音模型正在加载，文字功能可以使用。',
                'ready': '语音服务可连接；合成是否完成以实际回执为准。',
                'unavailable': '语音服务暂不可用，文字功能可以使用；请检查语音服务启动日志。',
                'stopped': '语音状态检查已停止。'}[state]

    def snapshot(self):
        return dict(self._status)

    def start(self):
        if not self._closed and self.cfg.backend != 'mock' and self._task is None:
            self._task = asyncio.create_task(self._run(), name='tts-readiness')

    async def check(self):
        if self._closed or self.cfg.backend == 'mock':
            return self.snapshot()
        try:
            available = await asyncio.wait_for(self._probe(), timeout=2)
        except asyncio.CancelledError:
            raise
        except Exception:
            available = False
        if self._closed:
            return self.snapshot()
        loading = self._loading and self.clock() - self._started < self.startup_timeout
        if loading and self._owned_pid is not None:
            try:
                loading = self._identity(self._owned_pid) == self._owned_identity
            except Exception:
                loading = False
        state = 'ready' if available is True else 'loading' if loading else 'unavailable'
        if state != 'loading':
            self._loading = False
        self._status.update(state=state, ready=state == 'ready', detail=self._detail(state),
                            checked_at=time.time())
        return self.snapshot()

    async def _run(self):
        while not self._closed:
            await self.check()
            await asyncio.sleep(self.interval)

    async def _health(self):
        if self._closed:
            return False
        backend = self.cfg.backend
        endpoints = {'indextts2': ('indextts2_url', '/health'),
                     'fish_speech': ('fish_speech_url', '/v1/health'),
                     'gpt_sovits': ('gpt_sovits_url', '/openapi.json')}
        if backend not in endpoints:
            return False
        name, path = endpoints[backend]
        base = getattr(self.cfg, name, '')
        parsed = urlsplit(base)
        if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            return False
        headers = {'Accept': 'application/json'}
        if backend == 'fish_speech':
            token = self._env.get(self.cfg.fish_speech_api_key_env, '').strip()
            if token:
                headers['Authorization'] = 'Bearer ' + token
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=1.5, trust_env=False, follow_redirects=False)
        chunks, size = [], 0
        async with self._client.stream('GET', base.rstrip('/') + path, headers=headers) as response:
            if response.status_code != 200:
                return False
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > 131072:
                    return False
                chunks.append(chunk)
        data = json.loads(b''.join(chunks))
        if not isinstance(data, dict):
            return False
        if backend == 'gpt_sovits':
            paths = data.get('paths')
            tts = paths.get('/tts') if isinstance(paths, dict) else None
            return isinstance(tts, dict) and isinstance(tts.get('post'), dict)
        return (data.get('status') == 'ok'
                and (backend != 'indextts2' or data.get('backend') == 'indextts2'))

    async def close(self):
        if self._closed:
            return
        self._closed = True
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self._client:
            await self._client.aclose()
            self._client = None
        self._status.update(state='stopped', ready=False, detail=self._detail('stopped'))
