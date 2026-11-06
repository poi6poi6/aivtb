import copy
import json
import unittest
from datetime import datetime, timezone

from brain.bilibili_experience import recall_context, validate_diary
from brain.events import Event


URL = 'https://www.bilibili.com/video/BV1B7411m7LV'
URL2 = 'https://www.bilibili.com/video/BV1B7411m7LW'
URL3 = 'https://www.bilibili.com/video/BV1B7411m7LX'
NOW = datetime(2026, 10, 26, 4, tzinfo=timezone.utc).timestamp()


def note(url=URL, **updates):
    return dict(url=url, title='玫瑰花盆养护', summary='视频简介建议花盆底部保留排水孔。',
                reaction='想继续了解玫瑰配土。', evidence_kind='page_metadata',
                visibility='public', at=NOW - 30, **updates)


def event(text='最近在看什么', source='companion', **updates):
    return Event(source, 'chat', text, user='你', ts=NOW, **updates)


def entries(context):
    return json.loads(context.split('\n', 1)[1])


class DiaryValidationTests(unittest.TestCase):
    def test_diary_requires_existing_saved_public_evidence_and_is_json_safe(self):
        sources = {URL: note()}
        original = copy.deepcopy(sources)
        diary = validate_diary('这次想继续了解花盆的排水设计。', [URL + '/', URL], sources, lambda: NOW)
        self.assertEqual(diary['source_urls'], [URL])
        self.assertEqual(diary['visibility'], 'public')
        self.assertEqual(diary['kind'], 'bilibili_diary')
        self.assertEqual(json.loads(json.dumps(diary, allow_nan=False)), diary)
        self.assertEqual(sources, original)

    def test_rejects_search_only_unknown_private_or_unproven_sources(self):
        for sources in ({}, {URL: {'url': URL, 'title': '只有标题'}},
                        {URL: {**note(), 'visibility': 'private'}},
                        {URL: {**note(), 'evidence_kind': 'claimed_watched'}},
                        {URL: {**note(), 'url': URL2}}):
            with self.subTest(sources=sources), self.assertRaises(ValueError):
                validate_diary('感想', [URL], sources, lambda: NOW)

    def test_accepts_subtitle_excerpt_without_claiming_full_video(self):
        result = validate_diary('读到了片段。', [URL],
                                {URL: {**note(), 'evidence_kind': 'subtitle_excerpt'}}, lambda: NOW)
        self.assertEqual(result['text'], '读到了片段。')

    def test_invalid_urls_text_times_and_excess_sources_fail_closed(self):
        for urls in ([URL + '?next=evil'], ['http://www.bilibili.com/video/BV1B7411m7LV'],
                     ['https://www.bilibili.com@evil.example/video/BV1B7411m7LV'], [], [URL] * 11, URL):
            with self.subTest(urls=urls), self.assertRaises(ValueError):
                validate_diary('感想', urls, {URL: note()}, lambda: NOW)
        for text in ('', ' ' * 3, '甲' * 1201, '正文\x00', None, {}):
            with self.subTest(text=text), self.assertRaises(ValueError):
                validate_diary(text, [URL], {URL: note()}, lambda: NOW)
        for ts in (float('nan'), float('inf'), -1, True, 'today', 10 ** 20):
            with self.subTest(ts=ts), self.assertRaises(ValueError):
                validate_diary('感想', [URL], {URL: note()}, lambda: ts)


class ExperienceRecallTests(unittest.TestCase):
    def test_recent_question_has_time_sources_and_reading_limit(self):
        result = recall_context([note()], [], event())
        self.assertIn('不能说看完视频', result)
        self.assertIn('不是用户事实', result)
        self.assertIn(URL, result)
        self.assertIn('2026-10-26 11:59 UTC+8', result)
        self.assertEqual(entries(result)[0]['阅读依据'], 'page_metadata')

    def test_multi_part_reading_scope_is_explicit_and_unknown_metadata_is_not_invented(self):
        public = {**note(), 'evidence_kind': 'full_subtitles', 'video_part': 2,
                  'video_part_count': 5, 'subtitle_language': 'zh-CN'}
        result = recall_context([public], [], event())
        row = entries(result)[0]
        self.assertEqual(row['阅读依据'], 'full_subtitles')
        self.assertIn('已读 P2 / 共 5P', row['分P范围'])
        self.assertEqual(row['字幕语言'], 'zh-CN')
        self.assertIn('不表示多P全看', result)
        for fields in ({}, {'video_part': 1}, {'video_part': True, 'video_part_count': 3},
                       {'video_part': 4, 'video_part_count': 3}, {'video_part': 1, 'video_part_count': 10001}):
            with self.subTest(fields=fields):
                rendered = entries(recall_context([{**note(), **fields}], [], event()))[0]
                self.assertEqual(rendered['分P范围'], '未知，不能推断已读取全部分P')

    def test_related_old_note_wins_over_unrelated_new_notes(self):
        old = {**note(), 'at': NOW - 86400 * 10}
        unrelated = {**note(URL2), 'title': '电脑配置', 'summary': '显卡与主板的搭配。', 'reaction': ''}
        result = recall_context([old, unrelated], [], event('玫瑰花盆怎么保持排水？'))
        self.assertIn(URL, result)
        self.assertNotIn(URL2, result)
        self.assertEqual(recall_context([old], [], event('你知道天体物理吗')), '')
        self.assertEqual(recall_context([old], [], event('你好')), '')

    def test_recent_topic_question_does_not_displace_matching_older_notes(self):
        old = {**note(), 'at': NOW - 86400 * 3}
        newer = [{**note('https://www.bilibili.com/video/BV' + str(i).zfill(10)),
                  'title': '电脑配置', 'summary': '显卡与主板的搭配。', 'reaction': ''} for i in range(4)]
        result = recall_context([old, *newer], [], event('最近看过玫瑰养护的视频吗？'), limit=1)
        self.assertEqual(entries(result)[0]['来源'], [URL])

    def test_group_requires_explicit_public_marker_and_never_serializes_extra_fields(self):
        legacy = note()
        legacy.pop('visibility')
        private = {**note(URL2), 'visibility': 'private', 'summary': 'PRIVATE_CONTENT'}
        public = {**note(URL3), 'user': 'PRIVATE_NAME', 'private_history': 'PRIVATE_SECRET'}
        self.assertIn(URL, recall_context([legacy], [], event()))
        result = recall_context([legacy, private, public], [], event(source='qq_group'))
        self.assertNotIn(URL + '"', result)
        self.assertNotIn(URL2, result)
        self.assertIn(URL3, result)
        self.assertNotIn('PRIVATE_', result)
        self.assertEqual(recall_context([private], [], event()), '')

    def test_diaries_need_public_marker_and_valid_sources(self):
        diary = validate_diary('花盆排水值得留意。', [URL], {URL: note()}, lambda: NOW - 5)
        result = recall_context([note()], [diary], event('看看你的日记'))
        self.assertEqual(len(entries(result)), 1)
        self.assertEqual(entries(result)[0]['类型'], '公开阅读日记')
        self.assertEqual(recall_context([], [{**diary, 'visibility': 'private'}], event('日记')), '')
        self.assertEqual(recall_context([], [{**diary, 'source_urls': ['https://evil.example']}], event('日记')), '')

    def test_total_budget_complete_json_and_limit_are_bounded(self):
        notes = [{**note('https://www.bilibili.com/video/BV' + str(i).zfill(10)),
                  'summary': '花盆' * 500, 'reaction': '玫瑰' * 500} for i in range(210)]
        for budget in (0, 100, 650, 1200, 3500, 10000):
            with self.subTest(budget=budget):
                result = recall_context(notes, [], event(), limit=99, budget_chars=budget)
                self.assertLessEqual(len(result), min(budget, 3500))
                if result:
                    self.assertLessEqual(len(entries(result)), 3)
                    self.assertTrue(all(x['来源'][0].startswith('https://www.bilibili.com/video/BV')
                                        for x in entries(result)))
        for limit in (0, -1, None, True, 1.5):
            self.assertEqual(recall_context([note()], [], event(), limit=limit), '')

    def test_deduplication_and_storage_tail_do_not_mutate_input(self):
        old = {**note(), 'at': NOW - 20, 'summary': 'OLD_SUMMARY'}
        new = {**note(), 'at': NOW - 10, 'summary': 'NEW_SUMMARY'}
        source = [old, new]
        before = copy.deepcopy(source)
        result = recall_context(source, [], event())
        self.assertEqual(len(entries(result)), 1)
        self.assertIn('NEW_SUMMARY', result)
        self.assertNotIn('OLD_SUMMARY', result)
        self.assertEqual(source, before)

    def test_today_yesterday_and_future_records_respect_event_time(self):
        today = note()
        yesterday = {**note(URL2), 'at': NOW - 86400}
        future = {**note(URL3), 'at': NOW + 86400}
        result = recall_context([today, yesterday, future], [], event('今天看了什么'))
        self.assertIn(URL, result)
        self.assertNotIn(URL2, result)
        self.assertNotIn(URL3, result)
        result = recall_context([today, yesterday, future], [], event('昨天看了什么'))
        self.assertIn(URL2, result)
        self.assertNotIn(URL + '"', result)

    def test_unsupported_channels_events_and_malformed_records_return_empty(self):
        for source in ('danmaku', 'game', 'system', 'qq_private', None):
            self.assertEqual(recall_context([note()], [], event(source=source)), '')
        self.assertEqual(recall_context([note()], [], Event('voice', 'idle', '最近在看什么')), '')
        for item in (None, {}, {'visibility': 'public', 'url': URL}, {**note(), 'at': True},
                     {**note(), 'summary': '甲' * 1001}, {**note(), 'evidence_kind': 'unknown'}):
            self.assertEqual(recall_context([item], [], event()), '')


if __name__ == '__main__':
    unittest.main()
