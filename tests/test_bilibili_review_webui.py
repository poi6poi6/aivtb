"""Public subtitle evidence → real HTTP/socket admission, no external account."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from brain.config import WebUIConfig
from brain.webui import WebUI


BV = 'BV1B7411m7LV'
URL = 'https://www.bilibili.com/video/' + BV + '?p=2'


def evidence(lines=None, *, status='ready'):
    return dict(url=URL, bvid=BV, video_part=2, video_cid=222, title='测试公开视频',
                part_title='第二段', description='公开简介', subtitles=lines or ['字幕讲解整理书桌的方法。'] if status == 'ready' else [],
                subtitle_status=status, evidence_kind='full_subtitles' if status == 'ready' else 'page_metadata',
                subtitle_truncated=False, subtitle_language='zh-CN')


class VideoReviewWebUITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.ui = WebUI(WebUIConfig(), asyncio.Queue(), folder.name, {}, workspace_root=Path(folder.name))
        self.ui.bilibili_review_enabled = True
        self.ui.on_bilibili_narrate = AsyncMock(return_value={'completed': True})
        app = web.Application(client_max_size=32768)
        app.router.add_get('/ws', self.ui._ws)
        for action in ('request', 'result', 'cancel'):
            app.router.add_post('/bilibili-video/' + action, getattr(self.ui, '_browser_video_' + action))
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.addAsyncCleanup(self.ui.close)
        self.origin = str(self.client.make_url('/')).rstrip('/')
        self.socket = await self.client.ws_connect('/ws', origin=self.origin)
        hello = await self.socket.receive_json()
        self.assertTrue(hello['bilibili_review'])

    async def post(self, action, data, *, origin=None):
        return await self.client.post('/bilibili-video/' + action, json=data,
            headers={'Origin': origin or self.origin})

    async def saved(self, lines=None, *, status='ready'):
        pending = await (await self.post('request', {'query': URL})).json()
        response = await self.post('result', {'request_id': pending['request_id'], 'evidence': evidence(lines, status=status)})
        self.assertEqual(response.status, 200)
        return await response.json()

    async def review(self, receipt, **extra):
        await self.socket.send_json(dict(type='bilibili_narrate', request_id='review-1', stage='review',
                                        query=URL, evidence_id=receipt['evidence_id'], **extra))
        return await self.done()

    async def done(self):
        async def read():
            while True:
                result = await self.socket.receive_json()
                if result['type'] == 'bilibili_narration_done':
                    return result
        return await asyncio.wait_for(read(), 2)

    async def test_large_chinese_transcript_uses_bounded_route_and_receipt_excludes_raw_text(self):
        lines = ['字' * 11999]
        receipt = await self.saved(lines)
        self.assertNotIn('subtitles', receipt)
        self.assertNotIn('description', receipt)
        self.assertTrue((await self.review(receipt, text='UNTRUSTED_SPEECH', subtitles='UNTRUSTED_SUBTITLE'))['completed'])
        payload = self.ui.on_bilibili_narrate.call_args.args[0]
        self.assertEqual(payload['evidence']['subtitles'], lines)
        self.assertEqual(payload['evidence']['part'], 2)
        self.assertNotIn('UNTRUSTED', str(payload))

    async def test_wrong_part_revoked_and_no_subtitle_cannot_call_persona(self):
        receipt = await self.saved()
        await self.socket.send_json(dict(type='bilibili_narrate', request_id='wrong', stage='review',
                                        query=BV, evidence_id=receipt['evidence_id']))
        self.assertFalse((await self.done())['completed'])
        self.ui.on_bilibili_narrate.assert_not_awaited()
        await self.post('cancel', {'evidence_id': receipt['evidence_id']})
        self.assertFalse((await self.review(receipt))['completed'])
        receipt = await self.saved(status='none')
        result = await self.review(receipt)
        self.assertFalse(result['completed'])
        self.assertEqual(result['error_code'], 'review_failed')
        self.ui.on_bilibili_narrate.assert_not_awaited()

    async def test_cross_origin_public_and_oversized_requests_rejected(self):
        response = await self.post('request', {'query': URL}, origin='https://example.org')
        self.assertEqual(response.status, 403)
        response = await self.post('result', {'padding': 'a' * 128001})
        self.assertEqual(response.status, 413)
        response = await self.post('request', {'query': URL, 'subtitle_url': 'https://example.org'})
        self.assertEqual(response.status, 400)
        self.ui.cfg.public_test = True
        response = await self.post('request', {'query': URL})
        self.assertEqual(response.status, 403)

    async def duplicate_then_cancel(self, action):
        receipt = await self.saved()
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def narrate(payload):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        self.ui.on_bilibili_narrate.side_effect = narrate
        payload = dict(type='bilibili_narrate', request_id='same', stage='review', query=URL,
                       evidence_id=receipt['evidence_id'])
        await self.socket.send_json(payload)
        await asyncio.wait_for(started.wait(), 1)
        await self.socket.send_json(payload)
        await self.socket.send_json({'type': action})
        await asyncio.wait_for(cancelled.wait(), 1)
        self.ui.on_bilibili_narrate.assert_awaited_once()

    async def test_duplicate_request_does_not_start_second_generation_and_stop_cancels_first(self):
        await self.duplicate_then_cancel('media_stop')

    async def test_clear_voice_cancels_pending_review_before_it_can_broadcast(self):
        await self.duplicate_then_cancel('playback_clear')

    async def test_local_song_and_disabled_runtime_never_accept_review(self):
        receipt = await self.saved()
        await self.socket.send_json(dict(type='song_narrate', request_id='local', stage='review',
                                        query=URL, evidence_id=receipt['evidence_id']))
        self.assertFalse((await self.done())['completed'])
        self.ui.bilibili_review_enabled = False
        self.assertFalse((await self.review(receipt))['completed'])
        self.ui.on_bilibili_narrate.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
