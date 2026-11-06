"""Local controls for periodic public-topic research; no chat dispatch."""
import asyncio
import json

from aiohttp import web


def install_trending_routes(ui, app):
    def service(request, *, write=False):
        ui._local_access(request, write=write)
        feature = getattr(ui.services, 'trends', None)
        if feature is None:
            raise web.HTTPServiceUnavailable(text='热点话题服务尚未启动。')
        return feature

    def response(data, status=200):
        return web.json_response(data, status=status, headers={'Cache-Control': 'no-store'})

    async def payload(request):
        async def read():
            body = bytearray()
            async for chunk in request.content.iter_chunked(1024):
                body.extend(chunk)
                if len(body) > 4096:
                    raise web.HTTPRequestEntityTooLarge(max_size=4096, actual_size=len(body))
            return json.loads(body)
        try:
            data = await asyncio.wait_for(read(), 5)
            if not isinstance(data, dict):
                raise ValueError('invalid body')
            return data
        except (ValueError, UnicodeError, asyncio.TimeoutError):
            raise web.HTTPBadRequest(text='热点设置格式无效。') from None

    async def status(request):
        return response(service(request).snapshot())

    async def settings(request):
        feature = service(request, write=True)
        data = await payload(request)
        try:
            await feature.update_settings(data)
            return response(feature.snapshot())
        except (ValueError, TypeError):
            return response({'error': '设置无效；周期需为 2–24 小时，至少选择一类允许的话题。'}, 400)
        except OSError:
            return response({'error': '设置未保存，请检查本机存储。'}, 503)

    async def refresh(request):
        feature = service(request, write=True)
        if await payload(request):
            return response({'error': '刷新不接受搜索词、聊天内容或其他参数。'}, 400)
        try:
            return response(feature.request_refresh(manual=True), 202)
        except (ValueError, RuntimeError) as error:
            known = {
                '今天的热点更新次数已用完，请明天再试',
                '热点刚更新过或正在等待重试，请稍后再试',
                '热点更新正在进行，请稍后查看',
                '请先开启热点话题',
            }
            detail = str(error)
            return response({'error': detail if detail in known else
                             '当前无法刷新，请检查启用状态与今日额度。'}, 409)
        except OSError:
            return response({'error': '刷新未启动，请检查本机存储。'}, 503)

    app.router.add_get('/trends-status', status)
    app.router.add_post('/trends-settings', settings)
    app.router.add_post('/trends-refresh', refresh)
