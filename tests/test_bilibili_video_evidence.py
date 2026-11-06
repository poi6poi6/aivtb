import copy
import unittest

from brain.bilibili_browser import BrowserResultError
from brain.bilibili_video_evidence import BilibiliVideoEvidence


BV = 'BV1B7411m7LV'
URL = f'https://www.bilibili.com/video/{BV}?p=2'


def evidence():
    return {'url': URL, 'bvid': BV, 'video_part': 2, 'video_cid': 202, 'title': '视频标题',
            'part_title': '第二部分', 'description': '真实简介', 'subtitles': ['第一句', '第二句'],
            'subtitle_status': 'ready', 'evidence_kind': 'full_subtitles',
            'subtitle_truncated': False, 'subtitle_language': 'zh-CN'}


class VideoEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.now = 100
        self.store = BilibiliVideoEvidence(clock=lambda: self.now)

    def submit(self, value=None):
        request = self.store.request(URL)
        return self.store.submit(request['request_id'], evidence() if value is None else value)

    def test_exact_part_bound_receipt_hides_text_and_keeps_full_host_evidence(self):
        receipt = self.submit()
        self.assertEqual(receipt['part'], 2)
        self.assertNotIn('subtitles', receipt)
        self.assertNotIn('description', receipt)
        saved = self.store.snapshot(receipt['evidence_id'], URL)
        self.assertEqual(saved['subtitles'], evidence()['subtitles'])
        saved['subtitles'].clear()
        self.assertEqual(len(self.store.snapshot(receipt['evidence_id'], URL)['subtitles']), 2)
        with self.assertRaises(BrowserResultError):
            self.store.snapshot(receipt['evidence_id'], BV)

    def test_wrong_bv_part_url_and_boolean_part_are_rejected(self):
        for field, value in [('bvid', 'BV9B7411m7LV'), ('video_part', 1), ('video_part', True),
                             ('url', f'https://www.bilibili.com/video/{BV}')]:
            with self.subTest(field=field, value=value), self.assertRaises(BrowserResultError):
                self.submit({**evidence(), field: value})

    def test_missing_subtitles_cannot_smuggle_comments_or_visible_cues(self):
        for value in [dict(subtitle_status='none'), dict(evidence_kind='visible_subtitles'),
                      dict(subtitle_truncated=True), dict(video_cid=None), dict(subtitles=[]),
                      dict(subtitle_status='unavailable', evidence_kind='page_metadata')]:
            with self.subTest(value=value), self.assertRaises(BrowserResultError):
                self.submit({**evidence(), **value})
        no_text = {**evidence(), 'subtitles': [], 'subtitle_status': 'none', 'evidence_kind': 'page_metadata'}
        receipt = self.submit(no_text)
        self.assertEqual(receipt['subtitle_count'], 0)
        self.assertIn('没有可用字幕', receipt['subtitle_reason'])

    def test_bounded_subtitle_text_and_fixed_reason_never_export_arbitrary_fields(self):
        for lines in [['x' * 12001], ['x'] * 2001, [''], ['bad\x00'], [123]]:
            with self.subTest(lines=len(lines)), self.assertRaises(BrowserResultError):
                self.submit({**evidence(), 'subtitles': lines})
        receipt = self.submit({**evidence(), 'subtitles': ['x' * 12000], 'subtitle_truncated': True,
                               'evidence_kind': 'subtitle_excerpt', 'cookie': 'SECRET', 'subtitle_reason': 'SECRET'})
        self.assertNotIn('SECRET', repr(receipt))
        self.assertIn('前段', receipt['subtitle_reason'])

    def test_request_and_evidence_expire_and_each_receipt_is_one_use(self):
        request = self.store.request(URL)
        self.now += 91
        with self.assertRaises(BrowserResultError):
            self.store.submit(request['request_id'], evidence())
        request = self.store.request(URL)
        saved = self.store.submit(request['request_id'], evidence())
        with self.assertRaises(BrowserResultError):
            self.store.submit(request['request_id'], evidence())
        self.now += 601
        with self.assertRaises(BrowserResultError):
            self.store.snapshot(saved['evidence_id'], URL)

    def test_cancellation_by_request_revokes_already_issued_receipt(self):
        request = self.store.request(URL)
        saved = self.store.submit(request['request_id'], evidence())
        self.store.cancel(request_id=request['request_id'])
        with self.assertRaises(BrowserResultError):
            self.store.snapshot(saved['evidence_id'], URL)
        saved = self.submit()
        self.store.cancel(evidence_id=saved['evidence_id'])
        with self.assertRaises(BrowserResultError):
            self.store.snapshot(saved['evidence_id'], URL)

    def test_arbitrary_urls_and_search_terms_never_create_read_requests(self):
        for value in ['搜索猫咪', 'https://evil.invalid/', URL+'&token=x', URL+'#fragment',
                      f'https://www.bilibili.com/video/{BV}?p=0']:
            with self.subTest(value=value), self.assertRaises(BrowserResultError):
                self.store.request(value)

    def test_capacity_is_bounded_and_close_removes_all_tokens(self):
        self.store = BilibiliVideoEvidence(capacity=2)
        first = self.submit()
        self.submit()
        self.submit()
        with self.assertRaises(BrowserResultError):
            self.store.snapshot(first['evidence_id'], URL)
        self.assertEqual(len(self.store.snapshots), 2)
        self.store.close()
        self.assertFalse(self.store.snapshots)


if __name__ == '__main__':
    unittest.main()
