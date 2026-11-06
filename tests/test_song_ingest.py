"""Run the production ingestion function without booting services or an LLM."""
import ast
import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from brain.events import Event
from brain.tarot import Tarot


def production_ingest(namespace):
    path = Path(__file__).resolve().parents[1] / 'brain' / 'main.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    function = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.AsyncFunctionDef) and n.name == 'ingest_events')
    namespace.update(__name__='brain.main', __package__='brain')
    exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])),
                 str(path), 'exec'), namespace)
    return namespace['ingest_events']


class SongIngestionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        folder = self.root / 'data' / 'songs'
        folder.mkdir(parents=True)
        for title in ('歌手 - 青鸟', '青鸟 Live', 'Spiral'):
            (folder / (title + '.mp3')).write_bytes(b'test-only-audio')
        self.now = 0.0
        self.queue = asyncio.Queue()
        self.push = AsyncMock()
        self.register = AsyncMock()
        self.requests = SimpleNamespace(items=[], submit=Mock(return_value='制作请求已记录'))
        self.webui = SimpleNamespace(music_requests=self.requests, broadcast_danmaku=AsyncMock())
        self.namespace = {
            'queue': self.queue, 'live_polls': None, 'tarot': None, 'director': Mock(), 'webui': self.webui,
            'register_event': self.register, 'push_event': self.push, 'sqlite3': sqlite3,
            'time': SimpleNamespace(monotonic=lambda: self.now), 'asyncio': asyncio,
            'settings': SimpleNamespace(webui=SimpleNamespace(public_test=False)),
            'config': SimpleNamespace(ROOT=self.root),
        }
        self.task = asyncio.create_task(production_ingest(self.namespace)())

    async def asyncTearDown(self):
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)

    async def deliver(self, text, source='companion', kind='chat', **control):
        before = len(self.push.await_args_list)
        await self.queue.put(Event(source, kind, text, viewer_key='test:1', **control))
        await asyncio.wait_for(self.queue.join(), timeout=2)
        if self.task.done():
            self.task.result()
        return [call.args[0] for call in self.push.await_args_list[before:]]

    async def test_companion_voice_and_danmaku_use_real_catalog_without_llm(self):
        for source in ('companion', 'voice', 'danmaku'):
            messages = await self.deliver('点歌：《ＳＰＩＲＡＬ》', source)
            song = next(m['song'] for m in messages if m['type'] == 'song_request')
            self.assertEqual(song['title'], 'Spiral')
            self.assertRegex(song['url'], r'^/songs/[a-f0-9]{24}$')
            self.now += 11
        self.webui.broadcast_danmaku.assert_awaited_once()

    async def test_tarot_registration_and_draw_are_consumed_before_music_and_persona(self):
        tarot = self.namespace['tarot'] = Tarot(self.root / 'tarot.sqlite3')
        session = tarot.create(mode='live', spread='single')
        await self.queue.put(Event('danmaku', 'chat', '塔罗 怎样把休息安排好？',
                                   user='观众甲', viewer_key='bili:1'))
        await asyncio.wait_for(self.queue.join(), timeout=2)
        entries = tarot.status()['entries']
        self.assertEqual(len(entries), 1)
        tarot.select(session['id'], entries[0]['id'])
        await self.queue.put(Event('danmaku', 'chat', '抽牌 7', user='观众甲', viewer_key='bili:1'))
        await asyncio.wait_for(self.queue.join(), timeout=2)
        self.assertEqual(tarot.status()['reading']['state'], 'revealed')
        self.assertEqual(len(tarot.status()['reading']['cards']), 1)
        self.register.assert_not_awaited()
        self.requests.submit.assert_not_called()
        self.assertEqual(self.webui.broadcast_danmaku.await_count, 2)
        self.register.assert_not_awaited()

    async def test_unique_partial_not_found_and_ambiguity_are_consumed(self):
        messages = await self.deliver('播放青鸟')
        self.assertEqual(messages[0]['reason'], 'ambiguous')
        self.assertIn('歌手 - 青鸟', messages[0]['text'])
        messages = await self.deliver('放一首不存在的歌')
        self.assertEqual(messages[0]['reason'], 'not_found')
        self.assertIn('已查询本地曲库', messages[0]['text'])
        messages = await self.deliver('来一首 live')
        self.assertEqual(messages[0]['song']['title'], '青鸟 Live')
        self.register.assert_not_awaited()

    async def test_first_request_at_zero_is_accepted_and_cooldown_is_explicit(self):
        first = await self.deliver('点歌Spiral')
        self.assertEqual(first[0]['type'], 'song_request')
        self.now = 1
        for command in ('点歌Spiral', 'B站点歌青鸟'):
            messages = await self.deliver(command)
            self.assertEqual(messages[0]['reason'], 'cooldown')
        self.now = 10
        self.assertEqual((await self.deliver('点歌Spiral'))[0]['type'], 'song_request')
        self.register.assert_not_awaited()

    async def test_bilibili_routes_original_query_without_catalog_or_network(self):
        with patch('brain.songs.catalog', side_effect=AssertionError('B站 must not read local catalog')):
            for command, expected in (('在B站搜索 青鸟 并播放', '青鸟'),
                                      ('BV1xx411c7mD', 'BV1xx411c7mD'),
                                      ('播放https://www.bilibili.com/video/BV1xx411c7mD/',
                                       'https://www.bilibili.com/video/BV1xx411c7mD/')):
                self.assertEqual(await self.deliver(command), [{'type': 'bilibili_request', 'query': expected}])
                self.now += 11
        self.requests.submit.assert_not_called()
        self.register.assert_not_awaited()

    async def test_empty_bilibili_query_reports_help_without_cooldown(self):
        messages = await self.deliver('B站点歌')
        self.assertEqual(messages[0]['reason'], 'missing_query')
        self.assertEqual((await self.deliver('B站点歌青鸟'))[0]['type'], 'bilibili_request')
        self.register.assert_not_awaited()

    async def test_controls_are_not_blocked_by_play_cooldown(self):
        await self.deliver('点歌Spiral')
        for command, action in (('停止播放', 'stop'), ('下一首', 'next'), ('打开歌单', 'open')):
            self.assertEqual(await self.deliver(command), [{'type': 'song_control', 'action': action}])
        self.register.assert_not_awaited()

    async def test_public_test_never_reads_production_catalog_or_submits_jobs(self):
        self.namespace['settings'].webui.public_test = True
        with patch('brain.songs.catalog', side_effect=AssertionError('private catalog access')):
            for command in ('点歌Spiral', 'B站点歌青鸟', 'BV1xx411c7mD', '停止播放', '唱歌 青鸟', '原创 春天'):
                messages = await self.deliver(command)
                self.assertEqual(messages[0]['reason'], 'public_test', command)
        self.requests.submit.assert_not_called()
        self.register.assert_not_awaited()

    async def test_unavailable_ui_is_consumed_even_for_production_requests(self):
        self.namespace['webui'] = None
        with patch('brain.songs.catalog', side_effect=AssertionError('catalog should not be read')):
            for command in ('点歌Spiral', 'B站点歌青鸟', '唱歌 青鸟'):
                messages = await self.deliver(command)
                self.assertEqual(messages[0]['reason'], 'unavailable')
        self.requests.submit.assert_not_called()
        self.register.assert_not_awaited()

    async def test_catalog_failure_reports_unavailable_not_empty_library(self):
        with patch('brain.songs.catalog', side_effect=OSError('test unreadable')):
            messages = await self.deliver('点歌Spiral')
        self.assertEqual(messages[0]['reason'], 'catalog_unavailable')
        self.assertNotIn('没有匹配', messages[0]['text'])
        self.register.assert_not_awaited()

    async def test_production_commands_keep_their_real_job_path(self):
        messages = await self.deliver('唱歌 青鸟')
        self.requests.submit.assert_called_once_with('cover', '青鸟', 'test:1')
        self.assertEqual(messages, [{'type': 'music_status', 'text': '制作请求已记录'}])
        self.register.assert_not_awaited()

    async def test_cached_cover_can_play_at_zero_and_then_cools_down(self):
        song = {'title': '青鸟翻唱', 'url': '/songs/' + 'a' * 24}
        self.requests.items.append({'mode': 'cover', 'query': '青鸟', 'state': 'ready', 'song': song})
        messages = await self.deliver('翻唱 青鸟')
        self.assertEqual(messages[0], {'type': 'song_request', 'song': song})
        self.assertEqual((await self.deliver('翻唱 青鸟'))[0]['reason'], 'cooldown')
        self.requests.submit.assert_not_called()
        self.register.assert_not_awaited()

    async def test_unsupported_platform_and_unverified_ordinal_do_not_search_catalog(self):
        with patch('brain.songs.catalog', side_effect=AssertionError('unsupported request')):
            for command in ('播放QQ音乐青鸟', '点歌网易云青鸟', '放首QQ音乐青鸟', '来一首网易云青鸟', '播放第二个吧'):
                messages = await self.deliver(command)
                self.assertEqual(messages[0]['reason'], 'unsupported')
        self.register.assert_not_awaited()

    async def test_ordinary_chat_and_non_chat_events_keep_existing_ingestion(self):
        await self.deliver('你喜欢什么音乐')
        await self.deliver('点歌Spiral', kind='interrupt')
        self.assertEqual(self.register.await_count, 2)
        self.push.assert_not_awaited()

    async def test_voice_retraction_and_memory_confirmation_do_not_replay_commands(self):
        with patch('brain.songs.catalog', side_effect=AssertionError('voice control must not read catalog')):
            for text, flag in (('点歌Spiral', 'retract'), ('点歌Spiral', 'confirm_memory'),
                               ('唱歌 青鸟', 'retract'), ('原创 春天', 'confirm_memory')):
                self.assertEqual(await self.deliver(text, source='voice', corrects=42, **{flag: True}), [])
                event = self.register.await_args.args[0]
                self.assertTrue(getattr(event, flag))
                self.assertEqual(event.corrects, 42)
        self.assertEqual(self.register.await_count, 4)
        self.requests.submit.assert_not_called()
        self.push.assert_not_awaited()

    async def test_voice_rewrite_to_song_remains_a_new_executable_request(self):
        messages = await self.deliver('点歌Spiral', source='voice', corrects=42)
        self.assertEqual(messages[0]['type'], 'song_request')
        self.assertEqual(messages[0]['song']['title'], 'Spiral')
        self.register.assert_not_awaited()
