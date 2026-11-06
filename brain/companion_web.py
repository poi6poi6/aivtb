"""Authenticated local routes for companion configuration and image input."""
from pathlib import Path

from aiohttp import web

from .events import Event
from .image_understanding import ImageUnderstandingError
from .meme_library import MemeError
from .viewer_identity import viewer_identity


MAX_UPLOAD = 5 * 1024 * 1024


def install_companion_routes(ui, app):
    def service(request, *, write=False):
        ui._local_access(request, write=write)
        feature = getattr(ui.services, 'companion', None)
        if feature is None:
            raise web.HTTPServiceUnavailable(text='陪伴能力尚未启动，请启动主服务。')
        return feature

    async def page(request):
        ui._local_access(request)
        return web.FileResponse(Path(__file__).parent / 'static' / 'companion.html',
                                headers={'Cache-Control': 'no-store'})

    async def status(request):
        return web.json_response(await service(request).snapshot(), headers={'Cache-Control': 'no-store'})

    def result(payload, *, status=200):
        return web.json_response(payload, status=status, headers={'Cache-Control': 'no-store'})

    async def action(request):
        feature = service(request, write=True)
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError('操作格式无效')
            mode = data.get('action')
            if mode == 'settings':
                await feature.configure(data.get('settings'))
            elif mode == 'followup_settings':
                await feature.followups.configure(data.get('settings'))
            elif mode == 'calendar_create':
                user = data.get('user', '你')
                if not isinstance(user, str) or not 1 <= len(user.strip()) <= 40:
                    raise ValueError('称呼需要 1 至 40 个字')
                owner = viewer_identity(Event('companion', 'chat', '', user=user.strip()))
                await feature.followups.create(data.get('title'), data.get('due_at'), user=user.strip(),
                                               owner_key=owner, respect_quiet_hours=data.get('respect_quiet_hours', True))
            elif mode == 'calendar_update':
                await feature.followups.update(data.get('id'), title=data.get('title'),
                                               due_at=data.get('due_at'),
                                               respect_quiet_hours=data.get('respect_quiet_hours'))
            elif mode == 'calendar_cancel':
                if not await feature.followups.cancel(data.get('id')):
                    raise ValueError('未找到可取消的日程')
            elif mode == 'journal_edit':
                if not await feature.journal.edit(data.get('id'), data.get('text')):
                    raise ValueError('未找到这篇日记')
            elif mode == 'journal_delete':
                if not await feature.journal.delete(data.get('id')):
                    raise ValueError('未找到这篇日记')
            elif mode == 'meme_update':
                feature.memes.update(data.get('id'), **{
                    key: data[key] for key in ('caption', 'tags', 'enabled') if key in data})
            elif mode == 'meme_remove':
                feature.memes.remove(data.get('id'))
            else:
                raise ValueError('未知操作')
            return result({'ok': True})
        except (ValueError, TypeError, MemeError) as error:
            return result({'error': str(error)[:180]}, status=400)
        except OSError:
            return result({'error': '保存未完成，请检查本机存储。'}, status=503)

    async def read_image(request):
        if request.content_length and request.content_length > MAX_UPLOAD:
            raise web.HTTPRequestEntityTooLarge(max_size=MAX_UPLOAD, actual_size=request.content_length)
        data = bytearray()
        async for chunk in request.content.iter_chunked(65536):
            data.extend(chunk)
            if len(data) > MAX_UPLOAD:
                raise web.HTTPRequestEntityTooLarge(max_size=MAX_UPLOAD, actual_size=len(data))
        return bytes(data)

    async def image_input(request):
        feature = service(request, write=True)
        question = request.query.get('text', '').strip() or '看看这张图片'
        user = request.query.get('user', '你').strip()
        if len(question) > 1000 or not 1 <= len(user) <= 40:
            raise web.HTTPBadRequest(text='问题或称呼过长')
        try:
            data = await read_image(request)
            ui.services.foreground('web')
            observation = await feature.vision.describe(data, question=question,
                                                       mime_type=request.content_type)
        except ImageUnderstandingError as error:
            return result({'error': str(error), 'code': error.code}, status=400)
        event = Event('companion', 'chat', question, user=user, forced=True)
        event.owner_control = True
        event.image_context = observation.context()
        await ui.event_queue.put(event)
        return result({'ok': True, 'message': '图片已交给主人格，请在聊天区查看回复。'})

    async def meme_upload(request):
        feature = service(request, write=True)
        try:
            item = feature.memes.add_image(await read_image(request),
                caption=request.query.get('caption', ''), tags=request.query.get('tags', ''),
                filename=request.query.get('filename', ''))
            return result({'ok': True, 'item': item})
        except (ValueError, MemeError) as error:
            return result({'error': str(error)[:180]}, status=400)
        except OSError:
            return result({'error': '保存未完成，请检查本机存储。'}, status=503)

    async def meme_asset(request):
        feature = service(request)
        try:
            data, mime = feature.memes.asset(request.match_info['id'])
        except (ValueError, OSError, MemeError):
            raise web.HTTPNotFound() from None
        return web.Response(body=data, content_type=mime, headers={
            'Cache-Control': 'private, max-age=60', 'X-Content-Type-Options': 'nosniff'})

    app.router.add_get('/companion', page)
    app.router.add_get('/api/companion', status)
    app.router.add_post('/api/companion/action', action)
    app.router.add_post('/api/companion/image', image_input)
    app.router.add_post('/api/companion/memes', meme_upload)
    app.router.add_get('/api/companion/memes/{id}', meme_asset)
