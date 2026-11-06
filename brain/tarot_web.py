"""Local-only host controls for the persisted tarot column."""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from aiohttp import web


QUEUE_TIMEOUT_SEC = 1.0
_FIELDS = {
    'create': {'spread', 'mode', 'hosting'},
    'select': {'session_id', 'entry_id'},
    'draw': {'session_id', 'reading_id', 'positions'},
    'finish': {'session_id', 'reading_id'},
    'close': {'session_id'},
    'test_join': {'session_id', 'user', 'question'},
    'test_choose': {'session_id', 'user', 'positions'},
    'interpret': {'session_id', 'reading_id'},
    'pause': {'session_id', 'reading_id'},
    'resume': {'session_id', 'reading_id'},
    'takeover': {'session_id', 'reading_id'},
    'confirm_delivered': {'session_id', 'reading_id'},
    'test_reply': {'session_id', 'user', 'text'},
}


def _identifier(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise ValueError('请先选择有效的场次、观众和牌面')
    return value


def _positions(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 3 or any(type(item) is not int for item in value):
        raise ValueError('选牌位置需要一至三个整数')
    return value


def install_tarot_routes(ui, app):
    actions = asyncio.Lock()

    def result(value, *, status=200):
        return web.json_response(value, status=status, headers={'Cache-Control': 'no-store'})

    def service(request, *, write=False):
        ui._local_access(request, write=write)
        tarot = getattr(ui, 'tarot', None)
        if tarot is None:
            raise web.HTTPServiceUnavailable(text='塔罗栏目尚未启动，请启动主服务。',
                                             headers={'Cache-Control': 'no-store'})
        return tarot

    async def page(request):
        ui._local_access(request)
        return web.FileResponse(Path(__file__).parent / 'static' / 'tarot.html',
                                headers={'Cache-Control': 'no-store'})

    async def status(request):
        tarot = service(request)
        try:
            public = request.query.get('clean') == '1'
            state = tarot.status(public=public)
            if public:
                # Clean display responses never carry the host's waiting list
                # or reading history, even when a store returns empty arrays.
                state = {name: state.get(name) for name in ('server_time', 'current', 'reading')}
            return result(state)
        except (OSError, sqlite3.Error):
            return result({'error': '牌桌存储暂不可用，请稍后刷新。'}, status=503)

    def fail_claim(tarot, event):
        try:
            tarot.mark_result(event, 'failed', error='解读未确认加入回复队列，请刷新后重试。')
        except (ValueError, TypeError, OSError, sqlite3.Error):
            # A stale attempt must not overwrite a newer attempt or a closed
            # reading. Storage errors are reported as unconfirmed, not success.
            pass

    async def interpret(tarot, data):
        session = _identifier(data.get('session_id'))
        reading = _identifier(data.get('reading_id'))
        idle = getattr(getattr(ui, 'services', None), 'idle', None)
        if not getattr(ui, 'runtime_started', False) or not callable(idle) or not idle():
            return result({'error': '主服务尚未就绪或正在回应，请等当前内容结束后再解读。'}, status=409)
        event = tarot.interpretation_event(session, reading)
        try:
            await asyncio.wait_for(ui.event_queue.put(event), timeout=QUEUE_TIMEOUT_SEC)
            if not tarot.valid_event(event):
                fail_claim(tarot, event)
                return result({'error': '本轮解读已经结束或失效，请刷新牌桌。'}, status=409)
        except asyncio.CancelledError:
            fail_claim(tarot, event)
            raise
        except Exception:
            fail_claim(tarot, event)
            return result({'error': '解读未确认加入回复队列，请刷新牌桌后重试。'}, status=503)
        return result({'ok': True, 'message': '已加入主人格解读队列，请等待生成；牌面保持不变。'})

    async def action(request):
        tarot = service(request, write=True)
        try:
            data = await request.json()
            if not isinstance(data, dict) or not isinstance(data.get('action'), str):
                raise ValueError('请求格式无效')
            mode = data['action']
            if mode not in _FIELDS or set(data) - _FIELDS[mode] - {'action'}:
                raise ValueError('未知的塔罗操作或字段')
            async with actions:
                if mode == 'interpret':
                    return await interpret(tarot, data)
                if mode == 'create':
                    spread, channel = data.get('spread', 'single'), data.get('mode', 'test')
                    hosting = data.get('hosting', 'manual')
                    if (spread not in ('single', 'three') or channel not in ('test', 'live')
                            or hosting not in ('guided', 'manual')):
                        raise ValueError('请选择有效牌阵、参与方式与主持方式')
                    value = tarot.create(spread=spread, mode=channel, hosting=hosting)
                else:
                    session = _identifier(data.get('session_id'))
                    if mode == 'select':
                        value = tarot.select(session, _identifier(data.get('entry_id')))
                    elif mode == 'draw':
                        value = tarot.draw(session, _identifier(data.get('reading_id')), _positions(data.get('positions')))
                    elif mode == 'finish':
                        value = tarot.finish(session, _identifier(data.get('reading_id')))
                    elif mode == 'close':
                        value = tarot.close(session)
                    elif mode == 'test_join':
                        value = tarot.test_join(session, data.get('user'), data.get('question'))
                    elif mode == 'test_choose':
                        value = tarot.test_choose(session, data.get('user'), _positions(data.get('positions')))
                    elif mode == 'test_reply':
                        value = tarot.test_reply(session, data.get('user'), data.get('text'))
                    else:
                        method = {'pause': tarot.pause, 'resume': tarot.resume,
                                  'takeover': tarot.takeover,
                                  'confirm_delivered': tarot.confirm_delivered}[mode]
                        value = method(session, _identifier(data.get('reading_id')))
                if isinstance(value, dict) and value.get('ok') is False:
                    raise ValueError(value.get('message') or '本次操作未完成，请刷新牌桌')
                if mode == 'create' and channel == 'live':
                    block_private = getattr(ui, '_block_private_followups', None)
                    if callable(block_private):
                        try:
                            await block_private()
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            return result({'error': '正式牌桌已开启，停止已有私人提醒尚未确认，请刷新检查。'}, status=503)
                if mode in {'finish', 'close', 'pause', 'takeover', 'test_reply'}:
                    cancel = getattr(ui, 'on_tarot_cancel', None)
                    if callable(cancel):
                        try:
                            await cancel()
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            return result({'error': '牌桌状态已更新，停止播报尚未确认，请刷新检查。'}, status=503)
                return result({'ok': True, 'result': value})
        except (ValueError, TypeError) as error:
            return result({'error': str(error)[:180]}, status=400)
        except (OSError, sqlite3.Error):
            return result({'error': '牌桌保存未确认，请刷新后检查。'}, status=503)

    app.router.add_get('/tarot', page)
    app.router.add_get('/tarot-status', status)
    app.router.add_post('/tarot-action', action)
