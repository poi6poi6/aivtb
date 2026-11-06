"""No-network official-page bridge: source binding, counts, leases and HTTP access."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from multidict import CIMultiDict

from brain.bilibili_browser import BilibiliBrowser, BrowserResultError, direct_video_reference, parse_visible_count
from brain.config import WebUIConfig
from brain.webui import WebUI


BV = 'BV1B7411m7LV'
OTHER = 'BV1xx411c7mD'


def row(bvid=BV, count='200,000', **extra):
    return {'bvid': bvid, 'title': '实际页面歌名', 'play_text': count, **extra}


class VisibleCountTests(unittest.TestCase):
    def test_precise_digits_commas_and_fullwidth(self):
        for raw, count in [('0', 0), ('200,000', 200000), ('１２３４５６７', 1234567)]:
            with self.subTest(raw=raw):
                actual = parse_visible_count(raw)
                self.assertEqual(actual, {'play_count': count, 'play_count_exact': True,
                                          'play_count_lower': count, 'play_count_upper': count})

    def test_abbreviations_preserve_uncertainty_at_actual_display_resolution(self):
        for raw, center, lower, upper in [('20万', 200000, 190000, 210000),
                                          ('20.0万', 200000, 199000, 201000),
                                          ('20.01万', 200100, 200000, 200200),
                                          ('1.2亿', 120000000, 110000000, 130000000),
                                          ('0.0万', 0, 0, 1000)]:
            with self.subTest(raw=raw):
                actual = parse_visible_count(raw)
                self.assertEqual(actual['play_count'], center)
                self.assertFalse(actual['play_count_exact'])
                self.assertEqual((actual['play_count_lower'], actual['play_count_upper']), (lower, upper))

    def test_invalid_or_overflow_values_never_become_zero(self):
        for raw in [True, 200000, None, '', '-1', '1e6', 'NaN', 'Infinity', '1,23',
                    '播放 200000', '20万+', '1.5', '9007199254740992', '999999999999999亿']:
            with self.subTest(raw=raw):
                self.assertIsNone(parse_visible_count(raw)['play_count'])
                self.assertIsNone(parse_visible_count(raw)['play_count_upper'])


class BrowserSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.
        self.bridge = BilibiliBrowser(clock=lambda: self.now)

    def submit(self, rows, query='海色'):
        pending = self.bridge.request(query)
        return self.bridge.submit(pending['request_id'], pending['url'], rows)

    def test_direct_reference_accepts_only_canonical_bv_and_bounded_parts(self):
        for query, part in [(BV, 1), ('https://www.bilibili.com/video/' + BV, 1),
                            ('https://www.bilibili.com/video/' + BV + '?p=1', 1),
                            ('https://www.bilibili.com/video/' + BV + '?p=10000', 10000)]:
            target = direct_video_reference(query)
            self.assertEqual((target['bvid'], target['part']), (BV, part))
        for query in ['海色', '播放 ' + BV, 'https://evil.test/video/' + BV,
                      'http://www.bilibili.com/video/' + BV, 'https://m.bilibili.com/video/' + BV,
                      'https://user@www.bilibili.com/video/' + BV,
                      'https://www.bilibili.com:443/video/' + BV,
                      'https://www.bilibili.com/video/' + BV + '?p=10001',
                      'https://www.bilibili.com/video/' + BV + '?p=0',
                      'https://www.bilibili.com/video/' + BV + '?p=1&p=2',
                      'https://www.bilibili.com/video/' + BV + '?other=1',
                      'https://www.bilibili.com/video/' + BV + '#fragment',
                      BV + '/other', '', None, []]:
            with self.subTest(query=query), self.assertRaises(BrowserResultError):
                direct_video_reference(query)

    def test_request_generates_only_fixed_official_sources_and_unique_tokens(self):
        pending = self.bridge.request('海色 & cover')
        parsed = urlsplit(pending['url'])
        self.assertEqual(parsed.netloc, 'search.bilibili.com')
        self.assertEqual(parse_qs(parsed.query), {'keyword': ['海色 & cover'], 'order': ['click']})
        self.assertGreaterEqual(len(pending['request_id']), 40)
        self.assertNotEqual(pending['request_id'], self.bridge.request('海色')['request_id'])
        for bad in ['https://evil.example/video/' + BV, 'https://user@www.bilibili.com/video/' + BV,
                    'https://www.bilibili.com:445/video/' + BV, 'https://www.bilibili.com/live/123',
                    'https://www.bilibili.com/video/' + BV + '?p=0', '', 'a' * 101, [], None]:
            with self.subTest(query=bad), self.assertRaises(BrowserResultError):
                self.bridge.request(bad)

    def test_bv_part_is_bound_and_exempt_from_automatic_threshold(self):
        query = 'https://www.bilibili.com/video/' + BV + '?p=3&spm_id_from=333'
        pending = self.bridge.request(query)
        self.assertEqual(pending['kind'], 'video')
        self.assertEqual(pending['url'], 'https://www.bilibili.com/video/' + BV + '?p=3')
        snapshot = self.bridge.submit(pending['request_id'], pending['url'], [row(count='')])
        self.assertEqual((snapshot['auto_bvid'], snapshot['selection_reason']), (BV, 'explicit_video'))
        self.assertEqual(snapshot['results'][0]['url'], pending['url'])
        pending = self.bridge.request(query)
        with self.assertRaises(BrowserResultError):
            self.bridge.submit(pending['request_id'], 'https://www.bilibili.com/video/' + BV, [row()])
        pending = self.bridge.request(BV)
        with self.assertRaises(BrowserResultError):
            self.bridge.submit(pending['request_id'], pending['url'], [row(OTHER)])
        maximum = self.bridge.request('https://www.bilibili.com/video/' + BV + '?p=10000')
        self.assertTrue(maximum['url'].endswith('?p=10000'))
        for part in ('10001', '99999', '-1', '1&p=2'):
            with self.subTest(part=part), self.assertRaises(BrowserResultError):
                self.bridge.request('https://www.bilibili.com/video/' + BV + '?p=' + part)

    def test_source_query_order_host_and_duplicate_query_must_match(self):
        for url in ['https://search.bilibili.com/all?keyword=另一首&order=click',
                    'https://search.bilibili.com/all?keyword=海色&order=totalrank',
                    'https://search.bilibili.com.evil.example/all?keyword=海色&order=click',
                    'https://evil@search.bilibili.com/all?keyword=海色&order=click',
                    'http://search.bilibili.com/all?keyword=海色&order=click',
                    'https://search.bilibili.com/all?keyword=海色&keyword=海色&order=click']:
            pending = self.bridge.request('海色')
            with self.subTest(url=url), self.assertRaises(BrowserResultError):
                self.bridge.submit(pending['request_id'], url, [row()])
        pending = self.bridge.request('海色')
        snapshot = self.bridge.submit(pending['request_id'], pending['url'].replace('/all?', '/video?'), [row()])
        self.assertEqual(snapshot['auto_bvid'], BV)

    def test_sort_deduplicate_exact_ties_and_uncertain_winners(self):
        snapshot = self.submit([row(count='12'), row(OTHER, '200000'), row(OTHER, '999999')])
        self.assertEqual([x['bvid'] for x in snapshot['results']], [OTHER, BV])
        self.assertEqual(snapshot['auto_bvid'], OTHER)
        tie = self.submit([row(OTHER, '200000'), row(count='200000')])
        self.assertEqual(tie['auto_bvid'], OTHER)
        for counts, reason in [(('199999', '1'), 'below_play_threshold'),
                               (('20万', '1'), 'below_play_threshold'),
                               (('32.1万', '32.0万'), 'uncertain_play_count'),
                               (('1000000', ''), 'uncertain_play_count'),
                               (('', ''), 'missing_play_count')]:
            snapshot = self.submit([row(count=counts[0]), row(OTHER, counts[1])])
            self.assertIsNone(snapshot['auto_bvid'])
            self.assertEqual(snapshot['selection_reason'], reason)
        proven = self.submit([row(count='32.1万'), row(OTHER, '31.9万')])
        self.assertEqual(proven['auto_bvid'], BV)  # Intervals meet at 320000.

    def test_cancel_submission_once_query_binding_and_snapshot_copy(self):
        pending = self.bridge.request('海色')
        snapshot = self.bridge.submit(pending['request_id'], pending['url'], [row()])
        with self.assertRaises(BrowserResultError):
            self.bridge.submit(pending['request_id'], pending['url'], [row()])
        with self.assertRaises(BrowserResultError):
            self.bridge.snapshot(snapshot['result_id'], '别的歌')
        snapshot['results'][0]['title'] = '篡改'
        self.assertEqual(self.bridge.snapshot(snapshot['result_id'], '海色')['results'][0]['title'], '实际页面歌名')
        self.bridge.cancel(request_id=pending['request_id'])
        with self.assertRaises(BrowserResultError):
            self.bridge.snapshot(snapshot['result_id'], '海色')
        self.assertEqual(self.bridge.cancel(request_id=pending['request_id']), {'ok': True})

    def test_ttl_capacity_cancel_and_close_do_not_revive_results(self):
        pending = self.bridge.request('海色')
        self.now += 180
        with self.assertRaises(BrowserResultError):
            self.bridge.submit(pending['request_id'], pending['url'], [row()])
        snapshot = self.submit([row()])
        self.now += 600
        with self.assertRaises(BrowserResultError):
            self.bridge.snapshot(snapshot['result_id'], '海色')
        self.bridge = BilibiliBrowser(clock=lambda: self.now, capacity=2)
        evicted = self.bridge.request('旧歌')
        self.bridge.request('第二首')
        self.bridge.request('第三首')
        with self.assertRaises(BrowserResultError):
            self.bridge.submit(evicted['request_id'], evicted['url'], [row()])
        old = self.submit([row()])
        self.submit([row()])
        current = self.submit([row()])
        with self.assertRaises(BrowserResultError):
            self.bridge.snapshot(old['result_id'], '海色')
        self.bridge.cancel(result_id=current['result_id'])
        with self.assertRaises(BrowserResultError):
            self.bridge.snapshot(current['result_id'], '海色')
        pending = self.bridge.request('最后')
        self.bridge.close()
        with self.assertRaises(BrowserResultError):
            self.bridge.submit(pending['request_id'], pending['url'], [row()])

    def test_strict_bounded_rows_empty_and_untrusted_extra_metrics(self):
        for rows in [None, {}, [row()] * 31, [True], [row(title='')], [row(title='x' * 251)],
                     [row(play_text=200000)], [row(author='x\n')], [row(bvid='invalid')]]:
            with self.subTest(rows=repr(rows)[:100]), self.assertRaises(BrowserResultError):
                self.submit(rows)
        empty = self.submit([])
        self.assertEqual((empty['results'], empty['auto_bvid'], empty['selection_reason']), ([], None, 'empty'))
        result = self.submit([row(count='', play_count=9999999, title='<em>真实标题</em>',
                                  cover='https://evil.example/private')])
        self.assertIsNone(result['auto_bvid'])
        self.assertEqual(result['results'][0]['cover'], '')
        self.assertEqual(result['results'][0]['title'], '真实标题')


class BrowserHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.ui = WebUI(WebUIConfig(), asyncio.Queue(), temp.name, {}, workspace_root=Path(temp.name))
        self.ui._media_search.search = AsyncMock(side_effect=AssertionError('no upstream'))
        app = web.Application(client_max_size=32768)
        for action in ('request', 'result', 'cancel'):
            app.router.add_post('/bilibili-browser/' + action, getattr(self.ui, '_browser_media_' + action))
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.headers = {'Origin': str(self.client.make_url('/')).rstrip('/')}
        self.addAsyncCleanup(self.client.close)
        self.addAsyncCleanup(self.ui.close)

    async def test_real_http_snapshot_cancel_and_replay(self):
        response = await self.client.post('/bilibili-browser/request', headers=self.headers, json={'query': '海色'})
        pending = await response.json()
        self.assertEqual(response.status, 200)
        body = {'request_id': pending['request_id'], 'page_url': pending['url'], 'results': [row()]}
        response = await self.client.post('/bilibili-browser/result', headers=self.headers, json=body)
        snapshot = await response.json()
        self.assertEqual(snapshot['auto_bvid'], BV)
        response = await self.client.post('/bilibili-browser/result', headers=self.headers, json=body)
        self.assertEqual(response.status, 410)
        self.assertEqual((await response.json())['error_code'], 'expired_request')
        response = await self.client.post('/bilibili-browser/cancel', headers=self.headers,
                                          json={'request_id': pending['request_id'], 'result_id': snapshot['result_id']})
        self.assertEqual(await response.json(), {'ok': True})
        with self.assertRaises(BrowserResultError):
            self.ui._bilibili_browser.snapshot(snapshot['result_id'], '海色')
        self.ui._media_search.search.assert_not_awaited()

    async def test_all_write_routes_reject_missing_foreign_repeated_origin_and_public(self):
        origin = self.headers['Origin']
        for action in ('request', 'result', 'cancel'):
            for headers in [{}, {'Origin': 'https://evil.example'}, [('Origin', origin), ('Origin', origin)],
                            {'Origin': origin + '/'}, {'Origin': origin, 'Host': 'evil.example'}]:
                response = await self.client.post('/bilibili-browser/' + action, headers=headers, json={'query': '海色'})
                self.assertEqual(response.status, 403)
            self.ui.cfg.public_test = True
            response = await self.client.post('/bilibili-browser/' + action, headers=self.headers, json={'query': '海色'})
            self.assertEqual(response.status, 403)
            self.ui.cfg.public_test = False
        self.assertFalse(self.ui._bilibili_browser._pending)

    async def test_remote_host_rejected_even_with_matching_origin(self):
        request = SimpleNamespace(remote='192.168.1.5', host='127.0.0.1:8080', scheme='http',
                                  headers=CIMultiDict({'Origin': 'http://127.0.0.1:8080'}),
                                  json=AsyncMock(return_value={'query': '海色'}))
        response = await self.ui._browser_media_request(request)
        self.assertEqual(response.status, 403)
        request.json.assert_not_awaited()

    async def test_bad_payloads_are_json_errors_and_oversized_body_rejected(self):
        for payload in [[], {'query': []}, {'query': '海色', 'text': '任意朗读'}, {'query': 'x' * 501}]:
            response = await self.client.post('/bilibili-browser/request', headers=self.headers, json=payload)
            self.assertEqual(response.status, 400)
            self.assertIn('error_code', await response.json())
        response = await self.client.post('/bilibili-browser/request', headers=self.headers, data='not json')
        self.assertEqual(response.status, 400)
        response = await self.client.post('/bilibili-browser/request', headers=self.headers, json={'query': 'x' * 40000})
        self.assertEqual(response.status, 413)


if __name__ == '__main__':
    unittest.main()
