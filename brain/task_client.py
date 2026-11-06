"""Foreground task client. Closing the conversation never closes the executor."""
import asyncio
from dataclasses import asdict
from pathlib import Path
import secrets
import time

import httpx
from .events import Event
from .task_coordination import TaskCoordination


class TaskClient(TaskCoordination):
    remote = True

    def __init__(self, root, cfg, event_queue=None, url='http://127.0.0.1:8085'):
        self.root, self.cfg = Path(root), cfg
        self.folder = self.root / 'data/agent-tasks'
        self.event_queue = event_queue
        self.url = url
        self.client_id = secrets.token_hex(16)
        self.http = httpx.AsyncClient(trust_env=False, timeout=8)
        self.jobs, self.intent_prompts = [], {}
        self.foreground = {}
        self.health = {'auth': 'unchecked', 'astra': False}
        self.connected = False
        self.last_sync = 0
        self.boot, self.sequence, self.cursor = None, -1, 0
        self.retired_boots = set()
        self.granted = set()
        self.resource_busy = lambda: {}
        self.feedback = asyncio.Queue()
        self.poller = self.sender = None
        self.closing = False

    async def start(self, autostart=True):
        if autostart:
            from .task_runtime import ensure_task_service
            try:
                await ensure_task_service(self.root, self.url)
            except OSError:
                pass
        try:
            await self.attach()
        except (RuntimeError, OSError):
            pass  # Chat can run while tasks show an honest disconnected state.
        self.poller = asyncio.create_task(self.poll_loop())
        self.sender = asyncio.create_task(self.feedback_loop())

    async def rpc(self, method, args=None, *, timeout=8):
        request_id = secrets.token_hex(16)
        try:
            token = (self.folder / 'service.token').read_text('utf-8').strip()
            response = await self.http.post(self.url + '/task-rpc',
                headers={'X-Task-Service': token}, timeout=timeout,
                json={'client': self.client_id, 'request_id': request_id,
                      'method': method, 'args': args or {}})
            payload = response.json()
            if response.status_code >= 500 or response.status_code in {403, 404}:
                raise ValueError('任务服务协议或连接不可用')
            self.adopt(payload)
            if response.is_error or 'error' in payload:
                raise RuntimeError(payload.get('error', '任务操作未完成'))
            return payload['result']
        except asyncio.CancelledError:
            if method == 'resolve_intent' and not self.closing:
                # Cancellation is a separate authenticated command; never repeat the
                # original mutation if a response was lost.
                try:
                    await asyncio.shield(self.rpc('cancel_request', {'request_id': request_id}, timeout=2))
                except (RuntimeError, OSError):
                    pass
            raise
        except (httpx.HTTPError, ValueError, OSError, KeyError, TypeError) as exc:
            self.connected = False
            self.granted.clear()
            raise RuntimeError('后台任务服务未连接；请查看任务台，操作不会自动重试') from exc

    def adopt(self, payload):
        if (not isinstance(payload, dict) or not isinstance(payload.get('status'), dict)
                or type(payload.get('sequence')) is not int or not isinstance(payload.get('boot'), str)
                or not isinstance(payload['status'].get('jobs'), list)
                or not isinstance(payload['status'].get('subscription'), dict)):
            raise ValueError('任务快照格式错误')
        if payload['boot'] != self.boot:
            if payload['boot'] in self.retired_boots:
                raise ValueError('已退出服务实例的迟到回执')
            if self.boot is not None:
                self.retired_boots.add(self.boot)
            self.boot, self.sequence = payload['boot'], -1
            self.intent_prompts.clear()
        if payload['sequence'] >= self.sequence:
            self.sequence = payload['sequence']
            self.jobs = payload['status']['jobs']
            self.health = payload['status']['subscription']
            self.self_upgrade_status = payload['status'].get('self_upgrade')
            self.foreground = payload['status'].get('foreground', {})
        self.connected = True

    async def attach(self):
        result = await self.rpc('attach', {'resources': self.resource_busy()})
        self.validate_poll(result)
        self.cursor = result['cursor']  # A new foreground session skips old speech.
        self.granted = set(result['granted'])
        self.last_sync = time.monotonic()

    @staticmethod
    def validate_poll(result):
        if (not isinstance(result, dict) or type(result.get('cursor')) is not int
                or not isinstance(result.get('granted'), list)
                or any(v not in {'desktop', 'local_gpu'} for v in result['granted'])
                or not isinstance(result.get('notices'), list)
                or any(not isinstance(n, dict) or type(n.get('seq')) is not int
                       or not isinstance(n.get('event'), dict) for n in result['notices'])):
            raise RuntimeError('任务服务同步格式错误')

    async def poll_loop(self):
        while True:
            try:
                if not self.connected:
                    await self.attach()
                else:
                    old_boot = self.boot
                    result = await self.rpc('poll', {'cursor': self.cursor, 'resources': self.resource_busy()})
                    self.validate_poll(result)
                    self.granted = set(result['granted'])
                    self.last_sync = time.monotonic()
                    if old_boot == self.boot and self.event_queue is not None:
                        for notice in result['notices']:
                            if notice['seq'] > self.cursor:
                                self.event_queue.put_nowait(Event(**notice['event']))
                    self.cursor = result['cursor']
            except (RuntimeError, OSError, ValueError, TypeError, KeyError):
                self.connected = False
                self.granted.clear()
            await asyncio.sleep(.5)

    def status(self):
        return {'enabled': self.cfg.enabled, 'connected': self.connected,
                'subscription': self.health, 'jobs': self.jobs, 'concurrency': 1,
                'service': 'independent', 'foreground': self.foreground, 'storage': 'sqlite',
                'self_upgrade': getattr(self, 'self_upgrade_status', None)}

    def get(self, job_id):
        return next(j for j in self.jobs if j['id'] == job_id)

    async def fetch_job(self, job_id):
        return await self.rpc('get', {'id': job_id})

    def resource_in_use(self, resource):
        # Foreground input needs a fresh, explicit grant. Cache age never grants it.
        if resource in self.resource_busy():
            return not (self.connected and time.monotonic() - self.last_sync < 2 and resource in self.granted)
        return any(j['state'] in {'running', 'waiting_approval'} and
                   (resource == 'desktop' and j['kind'] == 'desktop' or
                    resource == 'local_gpu' and j.get('resource_profile') == 'local_gpu') for j in self.jobs)

    def resource_block_reason(self, resource):
        """Explain the existing gate without granting or releasing any resource."""
        if not self.resource_in_use(resource):
            return None
        label = '显卡' if resource == 'local_gpu' else '桌面控制'
        if resource in self.resource_busy():
            if not self.connected:
                return f'任务协调服务未连接，尚不能取得{label}授权。请在启动中心启动后台任务服务，等连接恢复后重试。'
            if time.monotonic() - self.last_sync >= 2:
                return f'{label}授权已过期。请在启动中心检查后台任务服务，等待授权恢复后重试。'
        if any(job.get('state') in {'running', 'waiting_approval'} and
               (resource == 'local_gpu' and job.get('resource_profile') == 'local_gpu'
                or resource == 'desktop' and job.get('kind') == 'desktop') for job in self.jobs):
            return f'{label}正被后台任务占用。请等待任务完成，或在任务台停止该任务后重试；后台任务服务可在启动中心管理。'
        return f'尚未取得{label}授权。请在启动中心检查后台任务服务及任务台的资源占用，等授权恢复后重试。'

    async def action(self, data):
        return await self.rpc('action', data, timeout=90)

    async def submit(self, prompt, kind='research', **kwargs):
        if not kwargs.pop('owner', False) or set(kwargs) - {'resource_profile', 'execution_mode'}:
            raise ValueError('聊天任务必须通过主机事件入口提交')
        return await self.action({'action': 'submit', 'prompt': prompt, 'kind': kind, **kwargs})

    async def amend(self, job_id, instruction, token=None):
        return await self.action({'action': 'amend', 'id': job_id, 'instruction': instruction, 'token': token})

    async def route_event(self, event, method):
        if event.kind != 'chat':
            return
        try:
            result = await self.rpc(method, {'event': asdict(event),
                                    'owner_control': getattr(event, 'owner_control', False),
                                    'ui_id': getattr(event, 'ui_id', None)})
            for key, value in result['event'].items():
                setattr(event, key, value)
            key = self.requester(event)
            if result['pending']:
                self.intent_prompts[key] = True
            else:
                self.intent_prompts.pop(key, None)
        except RuntimeError:
            from .task_intent import should_classify
            if event.task_intent_pending or should_classify(event.text, self.jobs) or event.text.startswith(('/任务', '任务：', '任务:')):
                event.agent_reply = '后台任务服务暂时未连接，尚未确认执行；可以到任务台查看。'
                event.task_intent_pending = False

    async def handle_event(self, event):
        await self.route_event(event, 'handle_event')

    async def resolve_task_intent(self, event):
        if event.task_intent_pending:
            await self.route_event(event, 'resolve_intent')

    def save(self, *jobs):
        pass  # Read cache only; durable changes go through authenticated commands.

    def notification_tick(self):
        pass  # The independent service owns retries and notification state.

    def context_for(self, event):
        if not self.connected:
            return '后台任务服务暂时断开，当前执行状态未确认，不能沿用旧进度声称正在执行或已完成。'
        return super().context_for(event)

    def refresh_receipt(self, event):
        return (not event.task_id or self.connected) and super().refresh_receipt(event)

    def receipt_valid(self, event):
        return (not event.task_id or self.connected) and super().receipt_valid(event)

    def enqueue_feedback(self, item, state, reason=''):
        if item.event.get('task_notice') and item.event.get('task_id'):
            self.feedback.put_nowait({'event': dict(item.event), 'id': item.id,
                'created_at': item.created_at, 'state': state, 'reason': reason})

    def speech_dispatched(self, item):
        super().speech_dispatched(item)
        self.enqueue_feedback(item, 'dispatched')

    def speech_state(self, item, state, reason=''):
        self.enqueue_feedback(item, state, reason)

    def synthesis_failed(self, event, reason):
        if event.task_notice and event.task_id:
            self.feedback.put_nowait({'event': {'task_notice': True, 'task_id': event.task_id,
                'task_revision': event.task_revision, 'task_ts': event.ts},
                'id': None, 'created_at': event.ts, 'state': 'failed', 'reason': reason})

    async def feedback_loop(self):
        while True:
            data = await self.feedback.get()
            try:
                await self.rpc('feedback', data)
            except (RuntimeError, OSError):
                pass  # Lost delivery acknowledgement must not cause automatic replay.
            finally:
                self.feedback.task_done()

    async def close(self):
        if self.closing:
            return
        self.closing = True
        for task in (self.poller, self.sender):
            if task:
                task.cancel()
        await asyncio.gather(*(t for t in (self.poller, self.sender) if t), return_exceptions=True)
        try:
            await self.rpc('detach', timeout=2)
        except (RuntimeError, OSError):
            pass
        await self.http.aclose()
