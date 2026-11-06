"""Private loopback protocol for the single task executor. No public chat authority."""
import asyncio
from collections import deque
from dataclasses import asdict
import secrets
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

from aiohttp import web
from .events import Event
from .task_actions import task_action

EVENT_INPUT = {'source', 'kind', 'text', 'user', 'priority', 'ts', 'forced',
               'viewer_key', 'task_id', 'task_revision', 'task_notice',
               'agent_reply', 'task_intent_pending'}
EVENT_OUTPUT = {'task_id', 'task_revision', 'task_notice', 'agent_reply',
                'task_intent_pending', 'text', 'forced'}


class TaskDaemon:
    def __init__(self, manager):
        self.manager = manager
        self.token = secrets.token_urlsafe(48)
        self.boot = secrets.token_hex(16)
        self.sequence = 0
        self.notice_sequence = 0
        self.notices = deque(maxlen=256)
        self.lease = None
        self.requests = {}
        self.finished = {}
        self.manager.event_queue = asyncio.Queue()
        self.manager.resource_busy = self.busy_resources

    def busy_resources(self):
        if not self.lease:
            return {}
        stale = time.monotonic() - self.lease['seen'] > 10
        return {key: ('主界面连接中断，资源保留；重连或在任务台释放后继续' if stale else value)
                for key, value in self.lease['resources'].items()}

    def resources(self, client, requested):
        if not isinstance(requested, dict) or set(requested) - {'desktop', 'local_gpu'}:
            raise ValueError('资源请求格式错误')
        if any(not isinstance(v, str) or len(v) > 200 for v in requested.values()):
            raise ValueError('资源说明格式错误')
        # All foreground instances share this reservation. A lost client never silently
        # releases physical input while an action may still be running.
        if self.lease and self.lease['client'] != client:
            raise ValueError('已有主界面持有资源；请先关闭它或释放失联会话')
        granted = {k: v for k, v in requested.items() if v and not self.manager.resource_in_use(k)}
        self.lease = {'client': client, 'resources': granted, 'seen': time.monotonic()}
        return sorted(granted)

    def drain(self):
        while not self.manager.event_queue.empty():
            event = self.manager.event_queue.get_nowait()
            self.notice_sequence += 1
            self.notices.append({'seq': self.notice_sequence, 'event': asdict(event)})

    def snapshot(self):
        self.drain()
        self.sequence += 1
        return {'boot': self.boot, 'sequence': self.sequence, 'status': self.status()}

    def status(self):
        return {**self.manager.status(), 'service': 'independent', 'storage': 'sqlite',
                'foreground': {'connected': bool(self.lease and time.monotonic() - self.lease['seen'] <= 10),
                    'stale': bool(self.lease and time.monotonic() - self.lease['seen'] > 10),
                    'resources': sorted(self.lease['resources']) if self.lease else []}}

    async def health(self, request):
        self.access(request, token=False)
        return web.json_response({'service': 'avatar-tasks', 'protocol': 1,
                                  'boot': self.boot, 'ready': True})

    def access(self, request, token=True):
        if request.remote not in {'127.0.0.1', '::1'} or urlsplit('//'+request.host).hostname not in {'localhost', '127.0.0.1', '::1'}:
            raise web.HTTPForbidden()
        if request.headers.get('Origin') or (token and not secrets.compare_digest(
                request.headers.get('X-Task-Service', ''), self.token)):
            raise web.HTTPForbidden()

    async def rpc(self, request):
        self.access(request)
        data = await request.json()
        if not isinstance(data, dict):
            raise web.HTTPBadRequest()
        client, request_id = data.get('client'), data.get('request_id')
        if not isinstance(client, str) or not 1 <= len(client) <= 64 or not isinstance(request_id, str) or not 1 <= len(request_id) <= 64:
            raise web.HTTPBadRequest()
        key = (client, request_id)
        now = time.monotonic()
        self.finished = {key: at for key, at in self.finished.items() if now - at < 600}
        if key in self.requests or key in self.finished:
            return web.json_response({'error': '重复请求，不重复执行'}, status=409)
        if len(self.finished) + len(self.requests) >= 10000:
            return web.json_response({'error': '请求过于频繁，请稍后重试'}, status=429)
        self.requests[key] = asyncio.current_task()
        try:
            result = await self.command(client, data)
            return web.json_response({'result': result, **self.snapshot()}, headers={'Cache-Control': 'no-store'})
        except (ValueError, RuntimeError, OSError, StopIteration, KeyError, TypeError) as exc:
            return web.json_response({'error': str(exc)[:1000], **self.snapshot()}, status=400)
        finally:
            self.requests.pop(key, None)
            self.finished[key] = time.monotonic()

    async def command(self, client, data):
        method = data.get('method')
        args = data.get('args') or {}
        if not isinstance(args, dict):
            raise ValueError('参数格式错误')
        if method in {'attach', 'poll'}:
            granted = self.resources(client, args.get('resources', {}))
            self.drain()
            cursor = self.notice_sequence if method == 'attach' else args.get('cursor', self.notice_sequence)
            if type(cursor) is not int or cursor < 0:
                raise ValueError('通知游标错误')
            return {'granted': granted, 'cursor': self.notice_sequence,
                    'notices': [n for n in self.notices if n['seq'] > cursor]}
        if method == 'detach':
            if self.lease and self.lease['client'] == client:
                self.lease = None
            return {'ok': True}
        if method == 'status':
            return self.status()
        if method == 'get':
            return self.manager.get(args['id'])
        if method == 'action':
            if args.get('action') == 'release_stale':
                return await self.release_stale()
            return await task_action(self.manager, args)
        if method == 'cancel_request':
            target = args.get('request_id')
            if not isinstance(target, str) or not 1 <= len(target) <= 64:
                raise ValueError('请求编号错误')
            self.finished[(client, target)] = time.monotonic()
            pending = self.requests.get((client, target))
            if pending and pending is not asyncio.current_task():
                pending.cancel()
            return {'ok': True}
        if method in {'handle_event', 'resolve_intent'}:
            raw = args.get('event')
            if not isinstance(raw, dict) or len(str(raw.get('text', ''))) > 4000:
                raise ValueError('事件格式错误')
            event = Event(**{k: v for k, v in raw.items() if k in EVENT_INPUT})
            if type(args.get('ui_id')) is int:
                event.ui_id = args['ui_id']
            # Only this authenticated internal endpoint may carry host authority.
            event.owner_control = args.get('owner_control') is True
            if method == 'handle_event':
                await self.manager.handle_event(event)
            else:
                await self.manager.resolve_task_intent(event)
            return {'event': {k: getattr(event, k) for k in EVENT_OUTPUT},
                    'pending': bool(self.manager.intent_prompts.get(self.manager.requester(event)))}
        if method == 'feedback':
            ref = args.get('event') or {}
            item = SimpleNamespace(event=ref, id=args.get('id'), created_at=args.get('created_at', time.time()))
            if not ref.get('task_id') or not self.manager.speech_valid(item):
                return {'ignored': True}
            state = args.get('state')
            if state == 'dispatched':
                self.manager.speech_dispatched(item)
            elif state in {'playing', 'played', 'failed', 'cancelled'}:
                self.manager.speech_state(item, state, str(args.get('reason', ''))[:240])
            else:
                raise ValueError('播放状态错误')
            return {'ok': True}
        raise ValueError('未知内部命令')

    async def release_stale(self):
        if self.lease and time.monotonic() - self.lease['seen'] <= 10:
            raise ValueError('主界面仍在线，请先关闭游戏控制与主服务')
        self.lease = None
        return {'ok': True}

    async def tick(self):
        while True:
            self.manager.notification_tick()
            self.drain()
            self.manager.self_upgrade.kick()
            await asyncio.sleep(.25)
