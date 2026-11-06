"""Local gameplay-only console. No LLM, TTS, or public tunnel."""
import asyncio
from contextlib import suppress
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from aiohttp import web
from brain.minecraft import MinecraftBridge


async def create_app():
    queue = asyncio.Queue()
    bridge = MinecraftBridge(ROOT, queue)

    @web.middleware
    async def local_only(request, handler):
        if request.remote not in {'127.0.0.1', '::1'} or request.host not in {'127.0.0.1:8084', 'localhost:8084'}:
            raise web.HTTPForbidden()
        if request.method == 'POST' and request.headers.get('Origin') != f'http://{request.host}':
            raise web.HTTPForbidden()
        return await handler(request)

    app = web.Application(middlewares=[local_only])

    async def page(request):
        html = (ROOT / 'brain/static/minecraft.html').read_text(encoding='utf-8')
        html = html.replace('角色语音由主界面播放，请保持主界面打开。', '当前是独立游戏测试，语音解说未启动。')
        html = html.replace('<a href="/" target="_blank" rel="noopener">打开角色主界面</a>', '')
        return web.Response(text=html, content_type='text/html', headers={'Cache-Control': 'no-store'})

    async def script(request):
        return web.FileResponse(ROOT / 'brain/static/minecraft.js')

    async def status(request):
        return web.json_response({**bridge.state, 'history': bridge.history}, headers={'Cache-Control': 'no-store'})

    async def action(request):
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError('请求格式错误')
            await bridge.command(str(data.get('action', '')), str(data.get('target', '')))
            return web.json_response({'ok': True, 'message': '指令已提交，请看实际回执'})
        except (ValueError, OSError, RuntimeError) as exc:
            return web.json_response({'error': str(exc)[:240]}, status=400)

    async def drain():
        while True:
            await queue.get()
            queue.task_done()

    async def lifecycle(app):
        consumer = asyncio.create_task(drain())
        try:
            yield
        finally:
            await bridge.close()
            consumer.cancel()
            with suppress(asyncio.CancelledError):
                await consumer

    app.cleanup_ctx.append(lifecycle)
    app.router.add_get('/', page)
    app.router.add_get('/minecraft', page)
    app.router.add_get('/static/minecraft.js', script)
    app.router.add_get('/minecraft-status', status)
    app.router.add_post('/minecraft-action', action)
    return app


if __name__ == '__main__':
    web.run_app(create_app(), host='127.0.0.1', port=8084)
