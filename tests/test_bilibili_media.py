import asyncio
import time
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from brain.bilibili_media import (
    BilibiliSearch, BilibiliSearchError, MAX_PLAY_COUNT, extract_bvid, normalize_play_count,
    normalize_results, normalize_video,
)
from brain.config import WebUIConfig
from brain.webui import WebUI


BV = 'BV1PV411G7Cu'


def detail(title='Fixture'):
    return {'code': 0, 'data': {'bvid': BV, 'title': title, 'stat': {'view': 700000}}}


class ScriptedHTTP:
    """Stage-aware HTTP double; no upstream requests, credentials, or sleeps."""

    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []
        self.sessions = []
        self.cancelled = 0

    def session(self, **options):
        owner = self

        class Session:
            closed = False
            close_count = 0

            def get(self, url, *, params=None):
                if self.closed:
                    raise RuntimeError('fixture use after close')
                owner.calls.append((url, params, time.monotonic()))
                if not owner.steps:
                    raise AssertionError('Unexpected extra upstream request')
                return Response(owner.steps.pop(0))

            async def close(self):
                self.closed = True
                self.close_count += 1

        class Response:
            def __init__(self, step):
                self.step = step
                self.status = step.get('status', 200)
                self.headers = step.get('headers', {})

            async def __aenter__(self):
                if self.step.get('entered'):
                    self.step['entered'].set()
                if self.step.get('release'):
                    try:
                        await self.step['release'].wait()
                    except asyncio.CancelledError:
                        owner.cancelled += 1
                        raise
                if self.step.get('error'):
                    raise self.step['error']
                return self

            async def __aexit__(self, *args):
                return False

            async def read(self):
                raise AssertionError('Anonymous initialization must not download the full homepage')

            async def json(self):
                if self.step.get('json_error'):
                    raise self.step['json_error']
                return self.step.get('payload', {'code': 0, 'data': {'result': []}})

        session = Session()
        self.sessions.append(session)
        return session


class MediaTests(unittest.IsolatedAsyncioTestCase):
    def test_play_counts_are_exact_known_values_or_unknown(self):
        valid = [(0, 0), (123, 123), (123.0, 123), ('0', 0), ('1,234,567', 1234567),
                 (' ２．５万 ', 25000), ('1.25亿', 125000000), ('0.0001万', 1),
                 ('3萬', 30000), ('2億', 200000000), (str(MAX_PLAY_COUNT), MAX_PLAY_COUNT)]
        for value, expected in valid:
            with self.subTest(value=value):
                self.assertEqual(normalize_play_count(value), expected)
        invalid = [None, '', '--', '未知', True, False, [], {}, -1, -0.5, 1.5,
                   float('nan'), float('inf'), '-1', '+1', '12,34', '1,000,',
                   '1e6', '1.5', '1.2万次', '播放1万', '1.00000000000000000000000000001',
                   '0.00001万', MAX_PLAY_COUNT + 1, str(MAX_PLAY_COUNT + 1), '9' * 100]
        for value in invalid:
            with self.subTest(value=value):
                self.assertIsNone(normalize_play_count(value))

    def test_sort_deduplicates_then_ranks_stably_and_truncates(self):
        def video(index, plays):
            return {'bvid': 'BV' + f'{index:010d}', 'title': str(index), 'play': plays}

        items = [video(1, None), video(2, 0), video(3, '1万'), video(4, '10,000')]
        items.extend(video(index, index * 100) for index in range(5, 18))
        items.extend([video(3, 9999999), video(18, '8亿'), video(19, '--')])
        result = normalize_results({'code': 0, 'data': {'result': items}})
        self.assertEqual(len(result), 12)
        # The most viewed candidate came after the first twelve API rows.
        self.assertEqual([item['title'] for item in result],
                         ['18', '3', '4', '17', '16', '15', '14', '13', '12', '11', '10', '9'])
        self.assertEqual(result[1]['play_count'], 10000)  # First duplicate wins.
        smaller = normalize_results({'code': 0, 'data': {'result': items[:4] + [video(19, '--')]}})
        self.assertEqual([item['title'] for item in smaller], ['3', '4', '2', '1', '19'])
        self.assertIsNone(smaller[-1]['play_count'])
        self.assertEqual(smaller[2]['play_count'], 0)

    def test_bvid_and_official_link_extraction(self):
        for value in ('BV1PV411G7Cu', ' bv1PV411G7Cu ', 'https://www.bilibili.com/video/BV1PV411G7Cu/?p=1'):
            self.assertEqual(extract_bvid(value), 'BV1PV411G7Cu')
        for value in ('spiral', 'BV123', 'https://evil.example/video/BV1PV411G7Cu', 'https://www.bilibili.com.evil.example/video/BV1PV411G7Cu'):
            self.assertIsNone(extract_bvid(value))

    def test_exact_video_details_and_missing_video(self):
        payload = {'code': 0, 'data': {'bvid': 'BV1PV411G7Cu', 'title': '星间飞行', 'owner': {'name': 'UP'}, 'duration': 232, 'pic': 'http://i0.hdslb.com/test.jpg', 'stat': {'view': 7654321}}}
        result = normalize_video(payload, 'BV1PV411G7Cu')
        self.assertEqual(result[0]['duration'], '3:52')
        self.assertEqual(result[0]['cover'], 'https://i0.hdslb.com/test.jpg')
        self.assertEqual(result[0]['play_count'], 7654321)
        payload['data']['stat'] = {'view': 'bad'}
        self.assertIsNone(normalize_video(payload, 'BV1PV411G7Cu')[0]['play_count'])
        with self.assertRaises(ValueError):
            normalize_video({'code': -404}, 'BV1PV411G7Cu')
        with self.assertRaises(ValueError):
            normalize_video(payload, 'BV1B7411m7LV')

    def test_only_real_valid_results_and_safe_links(self):
        item = {'bvid': 'BV1B7411m7LV', 'title': '<em>spiral</em> &amp; song',
                'pic': 'javascript:alert(1)', 'author': 'UP'}
        result = normalize_results({'code': 0, 'data': {'result': [item, item, {'bvid': '../bad'}]}})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['title'], 'spiral & song')
        self.assertEqual(result[0]['cover'], '')
        self.assertEqual(result[0]['url'], 'https://www.bilibili.com/video/BV1B7411m7LV')
        with self.assertRaises(ValueError):
            normalize_results({'code': -412})

    def test_malformed_api_rows_and_shapes_cannot_invent_counts(self):
        for payload in (None, [], {'code': False}, {'code': 0.0}, {'code': 0, 'data': []},
                        {'code': 0, 'data': {'result': 'not a list'}}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                normalize_results(payload)
        for payload in (None, [], {'code': False}, {'code': 0, 'data': ['bad']}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                normalize_video(payload, 'BV1PV411G7Cu')
        for cover in ({}, 'https://[invalid', 'https://user@i0.hdslb.com/p.png',
                      'https://i0.hdslb.com:123/p.png', 'https://i0.hdslb.com.evil.test/p.png'):
            result = normalize_results({'code': 0, 'data': {'result': [None, [], {'bvid': []},
                {'bvid': 'BV1PV411G7Cu', 'pic': cover, 'play': -5}]}})
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]['cover'], '')
            self.assertIsNone(result[0]['play_count'])

    async def test_search_orders_keywords_by_clicks_but_bv_uses_exact_details(self):
        http = ScriptedHTTP([{}, {}, {}, {'payload': detail()}])
        with patch('brain.bilibili_media.aiohttp.ClientSession', http.session):
            search = BilibiliSearch(min_interval=0)
            self.addAsyncCleanup(search.close)
            self.assertEqual(await search.search('海色'), [])
            self.assertEqual(http.calls[-1][:2], ('https://api.bilibili.com/x/web-interface/search/type',
                                        {'search_type': 'video', 'keyword': '海色', 'order': 'click'}))
            self.assertEqual(await search.search('海色'), [])
            self.assertEqual(len(http.calls), 2)
            await search.search('another keyword')
            result = await search.search(BV)
            self.assertEqual(sum(url == 'https://www.bilibili.com/' for url, _, _ in http.calls), 1)
            self.assertEqual(len(http.sessions), 1)
            self.assertEqual(http.calls[-1][:2], ('https://api.bilibili.com/x/web-interface/view', {'bvid': BV}))
            self.assertEqual(result[0]['bvid'], BV)
            self.assertEqual(result[0]['play_count'], 700000)

    async def test_legacy_search_endpoint_requires_browser_without_upstream_access(self):
        ui = WebUI(WebUIConfig(), asyncio.Queue(), '.', {})
        ui._media_search.search = AsyncMock(return_value=[])
        app = web.Application()
        app.router.add_get('/bilibili-search', ui._search_media)
        async with TestClient(TestServer(app)) as client:
            self.assertEqual((await client.get('/bilibili-search?q=')).status, 400)
            response = await client.get('/bilibili-search?q=spiral')
            self.assertEqual(response.status, 409)
            data = await response.json()
            self.assertEqual(data['error_code'], 'browser_required')
            self.assertNotIn('results', data)
            ui._media_search.search.assert_not_awaited()


class SearchReliabilityTests(unittest.IsolatedAsyncioTestCase):
    def client(self, **kwargs):
        client = BilibiliSearch(min_interval=kwargs.pop('min_interval', 0), **kwargs)
        self.addAsyncCleanup(client.close)
        return client

    async def test_transient_error_retries_once_without_replacing_its_cause(self):
        cases = [
            ({'error': aiohttp.ClientConnectionError('private cookie/network details')}, 'network', 502),
            ({'error': asyncio.TimeoutError('private timeout detail')}, 'timeout', 504),
            ({'status': 502}, 'service', 503),
        ]
        for step, code, status in cases:
            with self.subTest(code=code):
                http = ScriptedHTTP([step, step, step, step])
                client = self.client()
                with patch('brain.bilibili_media.aiohttp.ClientSession', http.session):
                    for expected_calls in (2, 4):
                        with self.assertRaises(BilibiliSearchError) as caught:
                            await client.search(BV)
                        self.assertEqual(caught.exception.code, code)
                        self.assertEqual(caught.exception.http_status, status)
                        self.assertEqual(caught.exception.stage, 'detail')
                        self.assertNotIn('private', str(caught.exception))
                        self.assertNotIn('搜索太快', str(caught.exception))
                        self.assertEqual(len(http.calls), expected_calls)
                    self.assertFalse(client.cached)
                    self.assertEqual(len(http.sessions), 1)

    async def test_retry_spacing_and_single_retry_budget_across_homepage_and_api(self):
        http = ScriptedHTTP([{'error': aiohttp.ClientConnectionError('fixture')}, {}, {'status': 503}])
        client = self.client(min_interval=.025)
        with patch('brain.bilibili_media.aiohttp.ClientSession', http.session):
            with self.assertRaises(BilibiliSearchError) as caught:
                await client.search('fixture')
            self.assertEqual(caught.exception.code, 'service')
            self.assertEqual(caught.exception.stage, 'search')
            self.assertEqual(len(http.calls), 3)  # Homepage spent the only retry.
            for first, second in zip(http.calls, http.calls[1:]):
                self.assertGreaterEqual(second[2] - first[2], .023)
            self.assertEqual(BilibiliSearch().min_interval, 2)

    async def test_transient_search_success_keeps_same_anonymous_session(self):
        http = ScriptedHTTP([{}, {'status': 503}, {}, {}])
        client = self.client()
        with patch('brain.bilibili_media.aiohttp.ClientSession', http.session):
            self.assertEqual(await client.search('A'), [])
            self.assertEqual(await client.search('B'), [])
        self.assertEqual(len(http.sessions), 1)
        self.assertEqual(sum(url == 'https://www.bilibili.com/' for url, _, _ in http.calls), 1)
        self.assertFalse(http.sessions[0].closed)

    async def test_explicit_rejections_do_not_retry_or_bypass_homepage(self):
        for status, code in ((403, 'upstream_rejected'), (412, 'upstream_rejected'), (429, 'rate_limited')):
            with self.subTest(status=status):
                http = ScriptedHTTP([{'status': status}])
                client = self.client()
                with patch('brain.bilibili_media.aiohttp.ClientSession', http.session):
                    with self.assertRaises(BilibiliSearchError) as caught:
                        await client.search('A')
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(caught.exception.stage, 'homepage')
                self.assertEqual(len(http.calls), 1)
                self.assertEqual(http.calls[0][0], 'https://www.bilibili.com/')
                self.assertFalse(client.cached)

    async def test_non_json_malformed_payload_and_business_rejection_are_safe(self):
        cases = [
            ({'json_error': ValueError('private malformed JSON')}, 'invalid_response'),
            ({'json_error': aiohttp.ContentTypeError(None, (), message='private HTML')}, 'invalid_response'),
            ({'payload': None}, 'invalid_response'),
            ({'payload': {'code': 0}}, 'invalid_response'),
            ({'payload': {'code': 0, 'data': {'bvid': 'wrong'}}}, 'invalid_response'),
            ({'payload': {'code': -412, 'message': 'private upstream policy'}}, 'upstream_rejected'),
            ({'payload': {'code': -509}}, 'rate_limited'),
            ({'error': RuntimeError('private internal detail')}, 'service'),
        ]
        for step, code in cases:
            with self.subTest(code=code, step=step):
                http = ScriptedHTTP([step])
                client = self.client()
                with patch('brain.bilibili_media.aiohttp.ClientSession', http.session):
                    with self.assertRaises(BilibiliSearchError) as caught:
                        await client.search(BV)
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn('private', str(caught.exception))
                self.assertEqual(len(http.calls), 1)
                self.assertFalse(client.cached)

    async def test_retry_after_is_respected_without_changing_session(self):
        http = ScriptedHTTP([{'status': 429, 'headers': {'Retry-After': '7'}}, {'payload': detail()}])
        client = self.client()
        tick = [100]
        client._clock = lambda: tick[0]
        with patch('brain.bilibili_media.aiohttp.ClientSession', http.session):
            for _ in range(2):
                with self.assertRaises(BilibiliSearchError) as caught:
                    await client.search(BV)
                self.assertEqual(caught.exception.code, 'rate_limited')
                self.assertEqual(caught.exception.retry_after, 7)
            self.assertEqual(len(http.calls), 1)
            tick[0] = 108
            self.assertEqual((await client.search(BV))[0]['bvid'], BV)
            self.assertEqual(len(http.sessions), 1)

    async def test_service_retry_after_waits_and_does_not_exceed_total_budget(self):
        http = ScriptedHTTP([{'status': 503, 'headers': {'Retry-After': '1'}}, {'payload': detail()}])
        client = self.client(total_timeout=2)
        with patch('brain.bilibili_media.aiohttp.ClientSession', http.session):
            self.assertEqual((await client.search(BV))[0]['bvid'], BV)
        self.assertGreaterEqual(http.calls[1][2] - http.calls[0][2], 1)

        http = ScriptedHTTP([{'status': 503, 'headers': {'Retry-After': '30'}}])
        client = self.client(total_timeout=.05)
        with patch('brain.bilibili_media.aiohttp.ClientSession', http.session):
            for _ in range(2):
                with self.assertRaises(BilibiliSearchError) as caught:
                    await client.search(BV)
                self.assertEqual(caught.exception.code, 'service')
                self.assertEqual(caught.exception.retry_after, 30)
            self.assertEqual(len(http.calls), 1)

    def test_retry_after_accepts_http_date_and_rejects_untrusted_text(self):
        later = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=30), usegmt=True)
        delay = BilibiliSearch._retry_after({'Retry-After': later})
        self.assertLessEqual(delay, 30)
        self.assertGreaterEqual(delay, 29)
        for value in ('private cookie data', '-1', 'Infinity', '1.1', '9' * 1000, None):
            self.assertIsNone(BilibiliSearch._retry_after({'Retry-After': value}))

    async def test_multiple_queries_cache_ttl_capacity_and_returned_values_are_isolated(self):
        payload = {'code': 0, 'data': {'result': [{'bvid': BV, 'title': 'Original', 'play': 300000}]}}
        http = ScriptedHTTP([{}] + [{'payload': payload} for _ in range(5)])
        client = self.client(cache_size=2)
        tick = [100]
        client._clock = lambda: tick[0]
        with patch('brain.bilibili_media.aiohttp.ClientSession', http.session):
            await client.search('A')
            tick[0] = 103
            await client.search('B')
            tick[0] = 103.1
            results = await client.search('A')
            results[0]['title'] = 'Mutated caller copy'
            self.assertEqual((await client.search('A'))[0]['title'], 'Original')
            self.assertEqual(len(http.calls), 3)  # Homepage, A, B; confirmation A stays local.
            tick[0] = 106
            await client.search('C')
            self.assertEqual(set(client.cached), {'A', 'C'})
            tick[0] = 109
            await client.search('B')
            tick[0] = 170
            await client.search('A')
            self.assertEqual(len(http.calls), 6)
            self.assertLessEqual(len(client.cached), 2)

    async def test_concurrent_same_query_coalesces_and_one_cancel_does_not_kill_other(self):
        entered, release = asyncio.Event(), asyncio.Event()
        http = ScriptedHTTP([{'entered': entered, 'release': release, 'payload': detail()}])
        client = self.client()
        with patch('brain.bilibili_media.aiohttp.ClientSession', http.session):
            first = asyncio.create_task(client.search(BV))
            await entered.wait()
            second = asyncio.create_task(client.search(BV))
            await asyncio.sleep(0)
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            self.assertEqual(http.cancelled, 0)
            release.set()
            self.assertEqual((await second)[0]['bvid'], BV)
            self.assertEqual(len(http.calls), 1)
            self.assertFalse(client._inflight)

    async def test_cancelling_final_waiter_drains_request_and_does_not_revive_it(self):
        entered, release = asyncio.Event(), asyncio.Event()
        http = ScriptedHTTP([{'entered': entered, 'release': release, 'payload': detail('old')},
                             {'payload': detail('new')}])
        client = self.client()
        with patch('brain.bilibili_media.aiohttp.ClientSession', http.session):
            task = asyncio.create_task(client.search(BV))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(http.cancelled, 1)
            self.assertFalse(client._inflight)
            self.assertFalse(client.cached)
            release.set()
            await asyncio.sleep(0)
            self.assertFalse(client.cached)
            self.assertEqual((await client.search(BV))[0]['title'], 'new')
            self.assertEqual(len(http.sessions), 1)

    async def test_finished_inner_request_cannot_cache_after_final_waiter_cancels(self):
        client = self.client()
        pending = None

        async def finished_during_cancel(*args):
            asyncio.get_running_loop().call_soon(pending.cancel)
            return []

        client._execute = finished_during_cancel
        pending = asyncio.create_task(client.search(BV))
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertFalse(client.cached)
        self.assertFalse(client._inflight)

    async def test_cancelled_finished_flight_cannot_overwrite_replacement_query(self):
        client = self.client()
        pending = None
        replacement = []
        calls = []

        def replace():
            replacement.append(asyncio.create_task(client.search(BV)))

        async def finish(*args):
            calls.append(True)
            if len(calls) == 1:
                loop = asyncio.get_running_loop()
                loop.call_soon(pending.cancel)
                loop.call_soon(replace)
                return [{'bvid': BV, 'title': 'abandoned'}]
            return [{'bvid': BV, 'title': 'replacement'}]

        client._execute = finish
        pending = asyncio.create_task(client.search(BV))
        with self.assertRaises(asyncio.CancelledError):
            await pending
        result = await replacement[0]
        self.assertEqual(result[0]['title'], 'replacement')
        self.assertEqual((await client.search(BV))[0]['title'], 'replacement')
        self.assertEqual(len(calls), 2)
        self.assertFalse(client._inflight)

    async def test_total_deadline_covers_lock_wait_and_cancels_slow_http(self):
        client = self.client(total_timeout=.025)
        await client.lock.acquire()
        with self.assertRaises(BilibiliSearchError) as caught:
            await client.search(BV)
        self.assertEqual(caught.exception.code, 'timeout')
        self.assertEqual(caught.exception.stage, 'search_total')
        self.assertIsNone(client._session)
        client.lock.release()
        http = ScriptedHTTP([{'release': asyncio.Event()}])
        with patch('brain.bilibili_media.aiohttp.ClientSession', http.session):
            with self.assertRaises(BilibiliSearchError) as caught:
                await client.search(BV)
        self.assertEqual(caught.exception.code, 'timeout')
        self.assertEqual(http.cancelled, 1)
        self.assertFalse(client.cached)
        self.assertFalse(client.lock.locked())

    async def test_close_cancels_pending_calls_is_idempotent_and_never_reopens(self):
        entered, release = asyncio.Event(), asyncio.Event()
        http = ScriptedHTTP([{'entered': entered, 'release': release, 'payload': detail()}])
        client = self.client()
        with patch('brain.bilibili_media.aiohttp.ClientSession', http.session):
            pending = asyncio.create_task(client.search(BV))
            await entered.wait()
            await asyncio.gather(client.close(), client.close())
            with self.assertRaises(BilibiliSearchError) as caught:
                await pending
            self.assertEqual(caught.exception.code, 'closed')
            release.set()
            with self.assertRaises(BilibiliSearchError) as caught:
                await client.search(BV)
            self.assertEqual(caught.exception.code, 'closed')
            self.assertEqual(len(http.sessions), 1)
            self.assertTrue(http.sessions[0].closed)
            self.assertEqual(http.sessions[0].close_count, 1)
            self.assertFalse(client._inflight)
            self.assertFalse(client.cached)
