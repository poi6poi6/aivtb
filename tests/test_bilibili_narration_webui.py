"""Real socket coverage for the controlled Bilibili narration bridge."""
import asyncio
import itertools
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from brain.config import PlaybackConfig, WebUIConfig
from brain.media_narration import MediaNarrator
from brain.playback import SpeechController
from brain.songs import catalog
from brain.speech_pipeline import SpeechPipeline
from brain.webui import WebUI


class BilibiliNarrationWebUITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.ui = WebUI(WebUIConfig(), asyncio.Queue(), temporary.name, {},
                        workspace_root=Path(temporary.name))
        self.ui._media_search.search = AsyncMock(side_effect=AssertionError('No upstream lookup is allowed'))
        self.rows = [{'bvid': 'BV1B7411m7LV', 'title': '<em>真实歌名</em>'}]
        self.ui.on_bilibili_narrate = AsyncMock(return_value={'completed': True})
        app = web.Application()
        app.router.add_get('/ws', self.ui._ws)
        app.router.add_get('/songs', self.ui._songs)
        app.router.add_get('/songs/{song_id}', self.ui._song)
        app.router.add_get('/bilibili-search', self.ui._search_media)
        app.router.add_post('/bilibili-browser/request', self.ui._browser_media_request)
        app.router.add_post('/bilibili-browser/result', self.ui._browser_media_result)
        app.router.add_post('/bilibili-browser/cancel', self.ui._browser_media_cancel)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.addAsyncCleanup(self.ui.close)
        self.socket = await self.client.ws_connect('/ws', origin=str(self.client.make_url('/')).rstrip('/'))
        hello = await self.socket.receive_json()
        self.assertTrue(hello['bilibili_narration'])
        self.assertEqual(hello['music_protocol'], 6)
        self.assertTrue(hello['song_narration'])
        self.assertTrue(hello['bilibili_direct'])

    async def receive_type(self, kind, socket=None):
        socket = socket or self.socket
        async def read():
            while True:
                message = await socket.receive_json()
                if message.get('type') == kind:
                    return message
        return await asyncio.wait_for(read(), 2)

    async def receive_done(self, socket=None):
        return await self.receive_type('bilibili_narration_done', socket)

    async def barrier(self, socket=None):
        await (socket or self.socket).send_json({'type': 'conversation_interrupt'})
        await asyncio.wait_for(self.ui.event_queue.get(), 2)

    def local_song(self):
        folder = self.root / 'data' / 'songs'
        folder.mkdir(parents=True, exist_ok=True)
        (folder / '真实本地歌名.mp3').write_bytes(b'fixture')
        return catalog(folder)[0]

    async def request(self, request_id='fixture', stage='ack', **extra):
        if isinstance(stage, str) and stage in {'found', 'results'} and extra.get('type') != 'song_narrate' and 'result_id' not in extra:
            query = extra.get('query', '测试歌')
            pending = self.ui._bilibili_browser.request(query)
            rows = self.rows
            if pending['kind'] == 'video':
                from brain.bilibili_media import extract_bvid
                rows = [row for row in rows if row['bvid'] == extract_bvid(query)]
            snapshot = self.ui._bilibili_browser.submit(pending['request_id'], pending['url'], rows)
            extra['result_id'] = snapshot['result_id']
        await self.socket.send_json({'type': 'bilibili_narrate', 'request_id': request_id,
                                     'stage': stage, 'query': '测试歌', **extra})

    async def test_found_verifies_bv_and_ignores_browser_title_or_speech(self):
        await self.request(stage='found', bvid='BV1B7411m7LV', title='虚假歌名', text='随意朗读')
        self.assertTrue((await self.receive_done())['completed'])
        self.ui._media_search.search.assert_not_awaited()
        payload = self.ui.on_bilibili_narrate.call_args.args[0]
        self.assertEqual(payload['text'], '找到了，是《真实歌名》。来，一起听吧。')
        self.assertEqual(payload['emotion'], '开心')

    async def test_direct_bv_does_not_require_search_snapshot_title_or_play_count(self):
        self.ui._bilibili_browser.snapshot = Mock(side_effect=AssertionError('No snapshot required for explicit BV'))
        self.ui._bilibili_browser.request = Mock(side_effect=AssertionError('No helper required for explicit BV'))
        for query in ['BV1B7411m7LV', 'https://www.bilibili.com/video/BV1B7411m7LV?p=3',
                      'https://www.bilibili.com/video/BV1B7411m7LV?p=10000']:
            await self.request(stage='direct', query=query, bvid='BV1B7411m7LV', result_id='unverified',
                               title='虚构标题', text='客户端台词', play_count=0, selection='highest_play')
            self.assertTrue((await self.receive_done())['completed'])
            payload = self.ui.on_bilibili_narrate.call_args.args[0]
            self.assertEqual(payload['text'], '就放你指定的这个视频，来一起听吧。')
            self.assertEqual((payload['stage'], payload['query'], payload['emotion']), ('direct', query, '开心'))
            self.assertNotIn('找到', payload['text'])
            self.assertNotIn('播放量', payload['text'])
        self.ui._bilibili_browser.snapshot.assert_not_called()
        self.ui._bilibili_browser.request.assert_not_called()
        self.ui._media_search.search.assert_not_called()

    async def test_direct_rejects_keywords_mismatched_bv_invalid_parts_and_local_commands(self):
        for query, extra in [('海色', {}), ('https://www.bilibili.com/video/BV1B7411m7LV?p=10001', {}),
                             ('https://www.bilibili.com/video/BV1B7411m7LV?p=2&p=3', {}),
                             ('https://evil.test/video/BV1B7411m7LV', {}),
                             ('BV1B7411m7LV', {'bvid': 'BV1xx411c7mD'}),
                             ('BV1B7411m7LV', {'type': 'song_narrate'})]:
            await self.request(stage='direct', query=query, **extra)
            self.assertFalse((await self.receive_done())['completed'])
        self.ui.on_bilibili_narrate.assert_not_awaited()
        self.ui._media_search.search.assert_not_called()

    async def test_explicit_ack_does_not_claim_lookup_or_verified_availability(self):
        await self.request(stage='ack', query='BV1B7411m7LV')
        self.assertTrue((await self.receive_done())['completed'])
        self.assertEqual(self.ui.on_bilibili_narrate.call_args.args[0]['text'], '好呀，收到这个视频了。')
        await self.request(stage='ack', query='海色')
        self.assertTrue((await self.receive_done())['completed'])
        self.assertEqual(self.ui.on_bilibili_narrate.call_args.args[0]['text'], '好呀，我去B站找找，挑播放量最高的版本给你听。')

    async def test_direct_resource_rejection_is_returned_unchanged(self):
        self.ui.on_bilibili_narrate.return_value = {
            'completed': False, 'error_code': 'voice_resource_unavailable', 'reason': '请在启动中心恢复语音授权。'}
        await self.request(stage='direct', query='BV1B7411m7LV')
        result = await self.receive_done()
        self.assertFalse(result['completed'])
        self.assertEqual(result['error_code'], 'voice_resource_unavailable')
        self.assertEqual(result['reason'], '请在启动中心恢复语音授权。')

    async def test_missing_result_never_claims_found_or_completes(self):
        self.rows = []
        await self.request(stage='found', bvid='BV1B7411m7LV')
        result = await self.receive_done()
        self.assertFalse(result['completed'])
        self.assertIn('确认', result['reason'])
        self.ui.on_bilibili_narrate.assert_not_awaited()

    async def test_highest_play_narration_uses_actual_ranking_and_title(self):
        self.rows = [
            {'bvid': 'BV1xx411c7mD', 'title': '低播放量', 'play_text': '12'},
            {'bvid': 'BV1B7411m7LV', 'title': '<em>真实热门歌名</em>', 'play_text': '200,000'},
        ]
        await self.request(stage='found', bvid='BV1B7411m7LV', selection='highest_play',
                           title='虚假标题', play_count=1, text='随意台词')
        self.assertTrue((await self.receive_done())['completed'])
        payload = self.ui.on_bilibili_narrate.call_args.args[0]
        self.assertEqual(payload['text'], '这次搜到的结果里，播放量最高的是《真实热门歌名》。来，一起听吧。')

    async def test_false_highest_claims_and_missing_counts_do_not_reach_voice(self):
        for counts, bvid, query in [
            ([12, 200000], 'BV1xx411c7mD', '测试歌'),
            ([None, None], 'BV1xx411c7mD', '测试歌'),
            ([200000, 200000], 'BV1B7411m7LV', '测试歌'),
            (['unknown', '80000'], 'BV1xx411c7mD', '测试歌'),
            ([12, 200000], 'BV1B7411m7LV', 'BV1B7411m7LV'),
        ]:
            self.rows = [
                {'bvid': 'BV1xx411c7mD', 'title': '歌曲甲', 'play_text': str(counts[0] or '')},
                {'bvid': 'BV1B7411m7LV', 'title': '歌曲乙', 'play_text': str(counts[1] or '')},
            ]
            await self.request(stage='found', bvid=bvid, query=query, selection='highest_play',
                               play_count=999999999)
            self.assertFalse((await self.receive_done())['completed'])
        self.ui.on_bilibili_narrate.assert_not_awaited()

    async def test_below_threshold_never_auto_narrates_even_with_forged_browser_count(self):
        for count in (0, 199999):
            self.rows = [
                {'bvid': 'BV1B7411m7LV', 'title': '新歌', 'play_text': str(count)}]
            await self.request(stage='found', bvid='BV1B7411m7LV', selection='highest_play',
                               play_count=999999999)
            result = await self.receive_done()
            self.assertFalse(result['completed'])
            self.assertNotIn('二十万', result['reason'])
            self.assertIn('手动选择', result['reason'])
        self.ui.on_bilibili_narrate.assert_not_awaited()

    async def test_manual_and_exact_bv_choice_can_play_below_automatic_threshold(self):
        self.rows = [{'bvid': 'BV1B7411m7LV', 'title': '新歌', 'play_text': '0'}]
        for query in ('测试歌', 'BV1B7411m7LV'):
            await self.request(stage='found', bvid='BV1B7411m7LV', query=query)
            self.assertTrue((await self.receive_done())['completed'])
            self.assertNotIn('播放量最高', self.ui.on_bilibili_narrate.call_args.args[0]['text'])

    async def test_low_play_count_results_offer_selection_without_speaking_threshold(self):
        self.rows = [
            {'bvid': 'BV1B7411m7LV', 'title': '新歌', 'play_text': '199999'}]
        await self.request(stage='results', error='missing_play_count')
        self.assertTrue((await self.receive_done())['completed'])
        text = self.ui.on_bilibili_narrate.call_args.args[0]['text']
        self.assertEqual(text, '这次还没找到合适的版本，你可以换个关键词，或者从结果里挑一个。')
        self.assertNotIn('没拿到可比较的播放量', text)

    async def test_missing_metrics_and_failed_search_have_truthful_spoken_feedback(self):
        await self.request(stage='results')
        self.assertTrue((await self.receive_done())['completed'])
        self.assertIn('没拿到可比较的播放量', self.ui.on_bilibili_narrate.call_args.args[0]['text'])
        await self.request(stage='error', error='service')
        self.assertTrue((await self.receive_done())['completed'])
        self.assertIn('查询没成功', self.ui.on_bilibili_narrate.call_args.args[0]['text'])
        await self.request(stage='error', error='empty')
        self.assertTrue((await self.receive_done())['completed'])
        self.assertIn('没有搜到匹配的视频', self.ui.on_bilibili_narrate.call_args.args[0]['text'])

    async def test_classified_search_errors_are_spoken_without_another_lookup(self):
        expected = {'network': '网络出了问题', 'timeout': '响应超时',
                    'rate_limited': '限制了查询频率', 'upstream_rejected': '拒绝了自动查询',
                    'invalid_response': '返回的数据没法确认'}
        for code, phrase in expected.items():
            await self.request(stage='error', error=code, text='客户端任意台词')
            self.assertTrue((await self.receive_done())['completed'])
            text = self.ui.on_bilibili_narrate.call_args.args[0]['text']
            self.assertIn(phrase, text)
            self.assertNotIn('没有搜到匹配', text)
            self.assertNotIn('客户端任意台词', text)
        self.ui._media_search.search.assert_not_awaited()
        await self.request(stage='error', error=['invalid'])
        self.assertTrue((await self.receive_done())['completed'])
        self.assertIn('查询没成功', self.ui.on_bilibili_narrate.call_args.args[0]['text'])

    async def test_webui_close_closes_persistent_search_client(self):
        self.ui._media_search.close = AsyncMock()
        await self.ui.close()
        self.ui._media_search.close.assert_awaited_once()

    async def test_legacy_search_requires_browser_without_network(self):
        response = await self.client.get('/bilibili-search', params={'q': "it's happening"})
        payload = await response.json()
        self.assertEqual(response.status, 409)
        self.assertEqual(payload['error_code'], 'browser_required')
        self.assertNotIn('results', payload)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.ui._media_search.search.assert_not_awaited()

    async def test_browser_error_messages_are_controlled_and_truthful(self):
        expected = {'browser_required': '安装', 'browser_disconnected': '还没连接',
                    'verification_required': '完成验证', 'login_required': '正常登录',
                    'page_unavailable': '没有读到'}
        for code, phrase in expected.items():
            await self.request(stage='error', error=code, text='不要读这句话')
            self.assertTrue((await self.receive_done())['completed'])
            text = self.ui.on_bilibili_narrate.call_args.args[0]['text']
            self.assertIn(phrase, text)
            self.assertNotIn('不要读这句话', text)
        self.ui._media_search.search.assert_not_awaited()

    async def test_found_and_results_reject_missing_cancelled_or_wrong_query_snapshots(self):
        pending = self.ui._bilibili_browser.request('测试歌')
        snapshot = self.ui._bilibili_browser.submit(pending['request_id'], pending['url'], self.rows)
        for stage in ('found', 'results'):
            for result_id, query in [(None, '测试歌'), ('unknown', '测试歌'), (snapshot['result_id'], '别的歌')]:
                await self.request(stage=stage, bvid='BV1B7411m7LV', result_id=result_id, query=query)
                self.assertFalse((await self.receive_done())['completed'])
        self.ui._bilibili_browser.cancel(request_id=pending['request_id'])
        await self.request(stage='found', bvid='BV1B7411m7LV', result_id=snapshot['result_id'])
        self.assertFalse((await self.receive_done())['completed'])
        self.ui.on_bilibili_narrate.assert_not_awaited()
        self.ui._media_search.search.assert_not_awaited()

    async def test_overlapping_visible_counts_do_not_claim_highest(self):
        self.rows = [{'bvid': 'BV1B7411m7LV', 'title': '甲', 'play_text': '32.1万'},
                     {'bvid': 'BV1xx411c7mD', 'title': '乙', 'play_text': '32.0万'}]
        await self.request(stage='found', bvid='BV1B7411m7LV', selection='highest_play')
        self.assertFalse((await self.receive_done())['completed'])
        self.ui.on_bilibili_narrate.assert_not_awaited()
        await self.request(stage='results')
        self.assertTrue((await self.receive_done())['completed'])
        self.assertIn('还不能确定', self.ui.on_bilibili_narrate.call_args.args[0]['text'])

    async def test_http_snapshot_then_websocket_narration_uses_saved_title(self):
        headers = {'Origin': str(self.client.make_url('/')).rstrip('/')}
        response = await self.client.post('/bilibili-browser/request', headers=headers, json={'query': '测试歌'})
        pending = await response.json()
        response = await self.client.post('/bilibili-browser/result', headers=headers, json={
            'request_id': pending['request_id'], 'page_url': pending['url'],
            'results': [{'bvid': 'BV1B7411m7LV', 'title': '官方页面标题', 'play_text': '500,000'}]})
        snapshot = await response.json()
        self.assertEqual(response.status, 200)
        await self.request(stage='found', result_id=snapshot['result_id'], bvid=snapshot['auto_bvid'],
                           selection='highest_play', title='客户端改名')
        self.assertTrue((await self.receive_done())['completed'])
        self.assertIn('官方页面标题', self.ui.on_bilibili_narrate.call_args.args[0]['text'])
        self.assertNotIn('客户端改名', self.ui.on_bilibili_narrate.call_args.args[0]['text'])
        self.ui._media_search.search.assert_not_awaited()

    async def test_search_client_still_closes_when_runner_cleanup_fails(self):
        runner = SimpleNamespace(cleanup=AsyncMock(side_effect=RuntimeError('fixture cleanup')))
        self.ui._runner = runner
        self.ui._media_search.close = AsyncMock()
        try:
            with self.assertRaises(RuntimeError):
                await self.ui.close()
            self.ui._media_search.close.assert_awaited_once()
        finally:
            self.ui._runner = None

    async def test_socket_reader_remains_free_for_audio_receipts(self):
        receipt = asyncio.Event()
        started = asyncio.Event()
        async def narrate(payload):
            started.set()
            await receipt.wait()
            return {'completed': True}
        async def finish_audio(*args):
            receipt.set()
        self.ui.on_bilibili_narrate = narrate
        self.ui._audio_finished = finish_audio
        await self.request()
        await asyncio.wait_for(started.wait(), 2)
        await self.socket.send_json({'type': 'audio_finished', 'id': 123, 'reason': 'ended'})
        self.assertTrue((await self.receive_done())['completed'])

    async def test_new_request_and_explicit_cancel_never_send_stale_done(self):
        began = asyncio.Event()
        cancelled = asyncio.Event()
        async def narrate(payload):
            if payload['request_id'] == 'old':
                began.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            return {'completed': True}
        self.ui.on_bilibili_narrate = narrate
        await self.request('old')
        await asyncio.wait_for(began.wait(), 2)
        await self.socket.send_json({'type': 'bilibili_narration_cancel', 'request_id': 'old'})
        await asyncio.wait_for(cancelled.wait(), 2)
        await self.request('new')
        self.assertEqual((await self.receive_done())['request_id'], 'new')

    async def test_disconnect_cancels_pending_voice(self):
        began, cancelled = asyncio.Event(), asyncio.Event()
        async def narrate(payload):
            began.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        self.ui.on_bilibili_narrate = narrate
        await self.request()
        await asyncio.wait_for(began.wait(), 2)
        await self.socket.close()
        await asyncio.wait_for(cancelled.wait(), 2)
        self.assertFalse(self.ui._bilibili_narrations)

    async def test_invalid_payload_does_not_break_socket_or_run_voice(self):
        await self.request(stage=[])
        await self.request('valid')
        self.assertEqual((await self.receive_done())['request_id'], 'valid')
        self.ui.on_bilibili_narrate.assert_awaited_once()

    async def test_public_test_does_not_allow_narration(self):
        self.ui.cfg.public_test = True
        await self.request()
        await self.request(stage='direct', query='BV1B7411m7LV')
        await self.socket.send_json({'type': 'conversation_interrupt'})
        await asyncio.wait_for(self.ui.event_queue.get(), 2)
        self.ui.on_bilibili_narrate.assert_not_awaited()
        self.assertFalse(self.ui._bilibili_narrations)

    async def test_media_ownership_reaches_other_audio_pages_and_disconnect_releases_it(self):
        await self.socket.send_json({'type': 'bilibili_playback_state', 'active': True})
        message = await asyncio.wait_for(self.socket.receive_json(), 2)
        self.assertEqual(message, {'type': 'bilibili_playback_state', 'active': True})
        self.assertTrue(self.ui.bilibili_active)
        display = await self.client.ws_connect('/ws?display=1', origin=str(self.client.make_url('/')).rstrip('/'))
        hello = await display.receive_json()
        self.assertTrue(hello['bilibili_active'])
        # A playback-only page cannot acquire or release the console's flow.
        await display.send_json({'type': 'bilibili_playback_state', 'active': False})
        await self.socket.close()
        async def released():
            while True:
                message = await display.receive_json()
                if message.get('type') == 'bilibili_playback_state':
                    return message
        self.assertFalse((await asyncio.wait_for(released(), 2))['active'])
        self.assertFalse(self.ui.bilibili_active)
        await display.close()

    async def test_local_server_song_uses_actual_catalog_title_and_no_network_search(self):
        song = self.local_song()
        songs = await (await self.client.get('/songs')).json()
        self.assertEqual(songs, [song])
        await self.request(type='song_narrate', stage='found',
                           song={'url': song['url'], 'title': '随便念这个假标题'}, text='客户端自由台词')
        self.assertTrue((await self.receive_done())['completed'])
        payload = self.ui.on_bilibili_narrate.call_args.args[0]
        self.assertEqual(payload['media'], 'local')
        self.assertEqual(payload['text'], '找到了，是《真实本地歌名》。来，一起听吧。')
        self.assertEqual(payload['emotion'], '开心')
        self.ui._media_search.search.assert_not_awaited()

    async def test_local_missing_and_unsafe_server_urls_never_claim_found(self):
        song = self.local_song()
        (self.root / 'data' / 'songs' / '真实本地歌名.mp3').unlink()
        for url in (song['url'], '/songs/../../private', 'https://example.test/song.mp3',
                    'blob:http://localhost/fake', '/songs/' + 'a' * 24 + '?path=private'):
            await self.request(type='song_narrate', stage='found', song={'url': url, 'title': '假标题'})
            self.assertFalse((await self.receive_done())['completed'], url)
        self.ui.on_bilibili_narrate.assert_not_awaited()
        self.ui._media_search.search.assert_not_awaited()

    async def test_browser_local_file_accepts_only_bounded_filename_metadata(self):
        with patch('brain.songs.catalog', side_effect=AssertionError('browser file is not server catalog')):
            await self.request(type='song_narrate', stage='found', song={'local_file': True, 'title': '我选的歌'})
            self.assertTrue((await self.receive_done())['completed'])
        payload = self.ui.on_bilibili_narrate.call_args.args[0]
        self.assertEqual(payload['text'], '就是这首，《我选的歌》。来，一起听吧。')
        self.assertNotIn('song', payload)
        self.assertNotIn('url', payload)
        self.ui._media_search.search.assert_not_awaited()
        self.ui.on_bilibili_narrate.reset_mock()
        for song in ({'local_file': True, 'title': 'x' * 121}, {'local_file': True, 'title': '\n坏名称'},
                     {'local_file': True, 'title': 'x', 'url': 'blob:http://localhost/fake'},
                     {'local_file': True, 'title': 'x', 'audio': 'not-accepted'},
                     {'local_file': 1, 'title': 'x'}, {'local_file': True, 'title': '  '}):
            await self.request(type='song_narrate', stage='found', song=song)
            self.assertFalse((await self.receive_done())['completed'], song)
        self.ui.on_bilibili_narrate.assert_not_awaited()

    async def test_local_ack_results_and_error_use_fixed_local_wording(self):
        for stage in ('ack', 'results', 'error'):
            await self.request(type='song_narrate', stage=stage, text='不能由浏览器任意指定')
            self.assertTrue((await self.receive_done())['completed'])
            payload = self.ui.on_bilibili_narrate.call_args.args[0]
            self.assertEqual(payload['media'], 'local')
            self.assertNotIn('BV', payload['text'])
            self.assertNotIn('不能由浏览器任意指定', payload['text'])
            if stage == 'ack':
                self.assertEqual(payload['text'], '好呀，我找找这首。')
        self.ui._media_search.search.assert_not_awaited()

    async def test_only_control_or_current_audio_owner_can_request_local_narration(self):
        origin = str(self.client.make_url('/')).rstrip('/')
        display = await self.client.ws_connect('/ws?display=1', origin=origin)
        await display.receive_json()
        obs = await self.client.ws_connect('/ws?output=live', origin=origin)
        self.assertTrue((await obs.receive_json())['audio_owner'])
        try:
            command = {'type': 'song_narrate', 'request_id': 'local', 'stage': 'ack', 'query': '歌名'}
            await display.send_json(command)
            await self.barrier(display)
            await obs.send_json({**command, 'type': 'bilibili_narrate'})
            await obs.send_json({'type': 'bilibili_playback_state', 'active': True})
            await display.send_json({'type': 'song_playback_state', 'active': True})
            await self.barrier(obs)
            await self.barrier(display)
            self.ui.on_bilibili_narrate.assert_not_awaited()
            self.assertFalse(self.ui.bilibili_active)
            await obs.send_json(command)
            self.assertTrue((await self.receive_done(obs))['completed'])
            self.assertEqual(self.ui.on_bilibili_narrate.call_args.args[0]['media'], 'local')
            await self.request(type='song_narrate')
            self.assertTrue((await self.receive_done())['completed'])
        finally:
            await obs.close()
            await display.close()

    async def test_public_test_blocks_local_metadata_catalog_and_media_lease(self):
        self.ui.cfg.public_test = True
        with patch('brain.songs.catalog') as read_catalog, patch.object(self.ui, '_stop_media') as stop_media:
            await self.request(type='song_narrate', stage='found', song={'url': '/songs/' + 'a' * 24})
            await self.request(type='song_narrate', stage='found', song={'local_file': True, 'title': '私有歌'})
            await self.socket.send_json({'type': 'song_playback_state', 'active': True})
            await self.socket.send_json({'type': 'media_stop'})
            await self.barrier()
            read_catalog.assert_not_called()
            stop_media.assert_not_awaited()
        self.ui.on_bilibili_narrate.assert_not_awaited()
        self.assertFalse(self.ui.bilibili_active)
        self.assertFalse(self.ui._bilibili_narrations)

    async def test_local_and_bilibili_leases_on_one_socket_are_independent(self):
        await self.socket.send_json({'type': 'bilibili_playback_state', 'active': True})
        self.assertTrue((await self.receive_type('bilibili_playback_state'))['active'])
        await self.socket.send_json({'type': 'song_playback_state', 'active': True})
        await self.socket.send_json({'type': 'bilibili_playback_state', 'active': False})
        await self.barrier()
        self.assertTrue(self.ui.bilibili_active, 'Bili.stop must not release a local song lease')
        self.assertTrue(self.ui._song_flows)
        self.assertFalse(self.ui._bilibili_flows)
        await self.socket.send_json({'type': 'song_playback_state', 'active': False})
        self.assertFalse((await self.receive_type('bilibili_playback_state'))['active'])

    async def test_latest_cross_page_holder_keeps_focus_and_repeated_lease_is_quiet(self):
        obs = await self.client.ws_connect('/ws?output=live', origin=str(self.client.make_url('/')).rstrip('/'))
        await obs.receive_json()
        try:
            control_server = next(ws for ws in self.ui._sockets if self.ui._socket_priority[ws] == 0)
            # Both acquisitions finish before either page reads its outbox.
            # The later OBS acquisition must focus only the earlier holder;
            # focusing the idle OBS on the first request would cancel both.
            with patch.object(self.ui, '_send_socket', wraps=self.ui._send_socket) as send:
                await self.socket.send_json({'type': 'bilibili_playback_state', 'active': True})
                await self.barrier()
                await obs.send_json({'type': 'song_playback_state', 'active': True})
                await self.barrier(obs)
                focuses = [call.args[0] for call in send.call_args_list if call.args[1].get('type') == 'media_focus']
                self.assertEqual(focuses, [control_server])
            self.assertTrue((await self.receive_type('bilibili_playback_state'))['active'])
            await self.receive_type('media_focus')
            first_to_initiator = await obs.receive_json()
            self.assertEqual(first_to_initiator, {'type': 'bilibili_playback_state', 'active': True})
            await self.socket.send_json({'type': 'bilibili_playback_state', 'active': False})
            await self.barrier()
            self.assertTrue(self.ui._song_flows)
            self.assertFalse(self.ui._bilibili_flows)
            with patch.object(self.ui, '_send_socket', wraps=self.ui._send_socket) as send:
                await obs.send_json({'type': 'song_playback_state', 'active': True})
                await self.barrier(obs)
                self.assertFalse(any(call.args[1].get('type') == 'media_focus' for call in send.call_args_list))
            # The console now selects B站: OBS receives focus and can release
            # only its local lease, without switching the aggregate state off.
            await self.socket.send_json({'type': 'bilibili_playback_state', 'active': True})
            await self.receive_type('media_focus', obs)
            await obs.send_json({'type': 'song_playback_state', 'active': False})
            await self.barrier(obs)
            self.assertTrue(self.ui.bilibili_active)
            self.assertFalse(self.ui._song_flows)
            await self.socket.close()
            self.assertFalse((await self.receive_type('bilibili_playback_state', obs))['active'])
        finally:
            await obs.close()

    async def test_control_media_stop_cancels_other_page_voice_and_releases_both_leases(self):
        obs = await self.client.ws_connect('/ws?output=live', origin=str(self.client.make_url('/')).rstrip('/'))
        await obs.receive_json()
        began, cancelled = asyncio.Event(), asyncio.Event()
        async def narrate(payload):
            began.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        self.ui.on_bilibili_narrate = narrate
        try:
            await self.socket.send_json({'type': 'bilibili_playback_state', 'active': True})
            await self.barrier()
            await obs.send_json({'type': 'song_playback_state', 'active': True})
            await obs.send_json({'type': 'song_narrate', 'request_id': 'remote', 'query': '歌曲', 'stage': 'ack'})
            await asyncio.wait_for(began.wait(), 2)
            self.assertTrue(self.ui._bilibili_flows and self.ui._song_flows)
            requester = next(ws for ws in self.ui._sockets if self.ui._socket_priority[ws] == 0)
            with patch.object(self.ui, '_send_socket', wraps=self.ui._send_socket) as send:
                await self.socket.send_json({'type': 'media_stop'})
                await self.barrier()
                stopped = [call.args[0] for call in send.call_args_list
                           if call.args[1] == {'type': 'song_control', 'action': 'stop'}]
                self.assertNotIn(requester, stopped, 'requester must not receive a stale stop echo')
                self.assertEqual(stopped, [next(iter(self.ui._sockets))])
            self.assertTrue(cancelled.is_set(), 'server must cancel voice before notifying pages')
            self.assertFalse(self.ui.bilibili_active)
            self.assertFalse(self.ui._bilibili_narrations)
            while (await self.receive_type('bilibili_playback_state'))['active']:
                pass  # Discard the earlier acquisition message still in the client outbox.
            self.assertEqual((await self.receive_type('song_control', obs))['action'], 'stop')
        finally:
            await obs.close()

    async def test_non_owner_display_cannot_stop_global_media_but_audio_owner_can(self):
        origin = str(self.client.make_url('/')).rstrip('/')
        display = await self.client.ws_connect('/ws?display=1', origin=origin)
        await display.receive_json()
        obs = await self.client.ws_connect('/ws?output=live', origin=origin)
        await obs.receive_json()
        try:
            await self.socket.send_json({'type': 'bilibili_playback_state', 'active': True})
            await self.barrier()
            await display.send_json({'type': 'media_stop'})
            await self.barrier(display)
            self.assertTrue(self.ui.bilibili_active)
            requester = next(iter(self.ui._sockets))
            with patch.object(self.ui, '_send_socket', wraps=self.ui._send_socket) as send:
                await obs.send_json({'type': 'media_stop'})
                await self.barrier(obs)
                stopped = [call.args[0] for call in send.call_args_list
                           if call.args[1] == {'type': 'song_control', 'action': 'stop'}]
                self.assertNotIn(requester, stopped)
                self.assertEqual(set(stopped), set(self.ui._sockets) - {requester})
            while (await self.receive_type('bilibili_playback_state', obs))['active']:
                pass
            self.assertFalse(self.ui.bilibili_active)
        finally:
            await obs.close()
            await display.close()

    async def test_local_voice_callback_waits_for_real_socket_audio_receipts(self):
        song = self.local_song()
        playback = self.ui.playback = SpeechController(PlaybackConfig())
        voice = SimpleNamespace(speak=AsyncMock(return_value='C:/fixture/local-narration.wav'))
        pipeline = SpeechPipeline(voice, playback, self.ui.broadcast)
        ids = itertools.count(50)
        async def interrupt(event):
            await pipeline.interrupt_before(event.ui_id)
        narrator = MediaNarrator(allocate_id=lambda: next(ids), pipeline=pipeline, push=self.ui.broadcast,
                                 interrupt=interrupt, timeout_sec=1)
        playback.on_state, playback.on_delivery = narrator.state, narrator.delivered
        self.ui.on_bilibili_narrate = narrator.narrate
        asyncio.create_task(pipeline.run())
        self.addAsyncCleanup(pipeline.close)
        self.addAsyncCleanup(narrator.close)
        await self.request(type='song_narrate', stage='found', song=song)
        audio = await self.receive_type('audio')
        self.assertEqual(audio['event']['media_narration_id'], 'fixture')
        self.assertTrue(narrator.active)
        self.assertTrue(self.ui._bilibili_narrations)
        await self.socket.send_json({'type': 'audio_started', 'id': audio['id']})
        await self.socket.send_json({'type': 'audio_finished', 'id': audio['id'], 'reason': 'ended'})
        self.assertTrue((await self.receive_done())['completed'])
        self.assertIsNone(playback.current)
        self.assertFalse(narrator.active)

    async def test_direct_voice_callback_waits_for_actual_socket_audio_completion(self):
        playback = self.ui.playback = SpeechController(PlaybackConfig())
        voice = SimpleNamespace(speak=AsyncMock(return_value='C:/fixture/direct-narration.wav'))
        pipeline = SpeechPipeline(voice, playback, self.ui.broadcast)
        ids = itertools.count(60)
        async def interrupt(event):
            await pipeline.interrupt_before(event.ui_id)
        narrator = MediaNarrator(allocate_id=lambda: next(ids), pipeline=pipeline, push=self.ui.broadcast,
                                 interrupt=interrupt, timeout_sec=1)
        playback.on_state, playback.on_delivery = narrator.state, narrator.delivered
        self.ui.on_bilibili_narrate = narrator.narrate
        asyncio.create_task(pipeline.run())
        self.addAsyncCleanup(pipeline.close)
        self.addAsyncCleanup(narrator.close)
        playback.set_paused(True)
        await self.request(stage='direct', query='https://www.bilibili.com/video/BV1B7411m7LV?p=3')
        audio = await self.receive_type('audio')
        self.assertEqual(audio['event']['media_narration_stage'], 'direct')
        self.assertEqual(audio['text'], '就放你指定的这个视频，来一起听吧。')
        self.assertFalse(playback.paused)
        self.assertTrue(narrator.active)
        self.assertTrue(self.ui._bilibili_narrations, 'audio synthesis alone cannot authorize playback')
        await self.socket.send_json({'type': 'audio_started', 'id': audio['id']})
        await self.socket.send_json({'type': 'audio_finished', 'id': audio['id'], 'reason': 'ended'})
        self.assertTrue((await self.receive_done())['completed'])
        self.assertIsNone(playback.current)
        self.assertFalse(narrator.active)
        self.ui._media_search.search.assert_not_called()


if __name__ == '__main__':
    unittest.main()
