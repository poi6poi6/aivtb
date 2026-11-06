import json
import tempfile
import unittest
from pathlib import Path

from brain.songs import catalog, match_songs, normalize_song_title, parse_song_command, resolve_song


class SongCatalogTests(unittest.TestCase):
    def test_only_explicit_audio_files_and_opaque_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / '歌手 - 歌曲.MP3'
            audio.write_bytes(b'audio')
            (root / 'private.txt').write_text('private')
            (root / 'nested').mkdir()
            (root / 'nested' / 'hidden.mp3').write_bytes(b'hidden')
            songs = catalog(root)
            self.assertEqual(len(songs), 1)
            self.assertEqual(songs[0]['title'], '歌手 - 歌曲')
            self.assertEqual(resolve_song(root, songs[0]['id']), audio)
            self.assertIsNone(resolve_song(root, '../private.txt'))
            audio.unlink()
            self.assertIsNone(resolve_song(root, songs[0]['id']))

    def test_missing_directory_is_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(catalog(Path(directory) / 'absent'), [])


class SongCommandTests(unittest.TestCase):
    def test_screenshot_greeting_is_a_local_song_request(self):
        self.assertEqual(parse_song_command('你好，点歌 海色'), {'mode': 'local', 'query': '海色'})

    def test_screenshot_platform_request_removes_repeated_play_word(self):
        self.assertEqual(parse_song_command('B站点歌 放个飞我去星吧'),
                         {'mode': 'bilibili', 'query': '飞我去星'})
        self.assertEqual(parse_song_command('B站点歌 放个走吧'),
                         {'mode': 'bilibili', 'query': '走吧'})
        self.assertEqual(parse_song_command('B站点歌 放个《飞我去星吧》'),
                         {'mode': 'bilibili', 'query': '飞我去星吧'})

    def test_shared_browser_and_server_command_contract(self):
        fixture = Path(__file__).with_name('fixtures') / 'song_command_cases.json'
        examples = json.loads(fixture.read_text(encoding='utf-8'))
        self.assertEqual(examples['version'], 1)
        self.assertGreaterEqual(len(examples['cases']), 92)
        for case in examples['cases']:
            with self.subTest(text=case['text']):
                self.assertEqual(parse_song_command(case['text']), case['expected'])

    def test_local_anchored_verbs_with_optional_separator_and_quotes(self):
        for command in ('点歌青鸟', '点歌:青鸟', '点歌： 《青鸟》', '播放“青鸟”',
                        '放一首 青鸟', '放首「青鸟」', '放歌 青鸟', '来一首 青鸟',
                        '请播放青鸟', '帮我点歌青鸟', '请给我放一首 青鸟', '请帮我点歌青鸟', '“点歌青鸟”'):
            self.assertEqual(parse_song_command(command), {'mode': 'local', 'query': '青鸟'}, command)
        for text in ('我喜欢青鸟', '你能播放音乐吗', '不要播放青鸟', 'B站是什么', '', None):
            self.assertIsNone(parse_song_command(text), text)

    def test_catalog_normalization_keeps_case_sensitive_ids_and_original_objects(self):
        songs = [{'id': 'one', 'title': '歌手 — LOVE ＳＴＯＲＹ！', 'url': '/songs/one'},
                 {'id': 'two', 'title': 'Love Story (Live)', 'url': '/songs/two'}]
        self.assertEqual(normalize_song_title(' ＳＴＯＲＹ—love! '), 'storylove')
        self.assertEqual(match_songs(songs, '《歌手love story》'), [songs[0]])
        self.assertIs(match_songs(songs, '歌手lovestory')[0], songs[0])
        self.assertEqual(match_songs(songs, 'LOVE-STORY'), songs)
        self.assertEqual(match_songs(songs, '无此歌曲'), [])
        self.assertEqual(match_songs(songs, '！？'), [])

    def test_exact_wins_and_duplicate_normalized_titles_remain_ambiguous(self):
        songs = [{'title': '青鸟'}, {'title': '青鸟 (Live)'}, {'title': '青 鸟！'}]
        self.assertEqual(match_songs(songs, '青鸟'), [songs[0], songs[2]])
        self.assertEqual(match_songs(songs[:2], '青鸟'), [songs[0]])
        self.assertEqual(match_songs(songs, 'live'), [songs[1]])

    def test_bilibili_commands_preserve_bv_case_and_remove_play_suffix(self):
        bv = 'BV1xx411c7mD'
        for command in (bv, '播放' + bv, '点歌《' + bv + '》', 'B站点歌 ' + bv,
                        '请帮我在 B站搜索 ' + bv + ' 并播放', '点歌B站：' + bv,
                        '在哔哩哔哩播放' + bv, '给我放一首bilibili ' + bv):
            self.assertEqual(parse_song_command(command), {'mode': 'bilibili', 'query': bv}, command)
        self.assertEqual(parse_song_command('B站点歌青鸟'), {'mode': 'bilibili', 'query': '青鸟'})
        self.assertEqual(parse_song_command('在B站搜索 spiral 并播放'), {'mode': 'bilibili', 'query': 'spiral'})
        self.assertEqual(parse_song_command('B站点歌'), {'mode': 'bilibili', 'query': ''})

    def test_only_full_bv_or_official_video_urls_are_automatic_references(self):
        for url in ('https://www.bilibili.com/video/BV1xx411c7mD/?p=2',
                    'https://m.bilibili.com/video/av12345', 'www.bilibili.com/video/BV1xx411c7mD'):
            self.assertEqual(parse_song_command(url), {'mode': 'bilibili', 'query': url})
        for url in ('BV1xx411c7m', 'BV1xx411c7mDextra', 'BV1xx411c7mı', 'https://bilibili.com.evil/video/BV1xx411c7mD',
                    'https://user@bilibili.com/video/BV1xx411c7mD',
                    'https://bilibili.com:9090/video/BV1xx411c7mD', 'https://bilibili.com/read/BV1xx411c7mD'):
            self.assertIsNone(parse_song_command(url), url)

    def test_controls_allow_polite_prefixes_and_do_not_absorb_sentences(self):
        for text, mode in (('点歌', 'open'), ('播放', 'open'), ('请打开歌单', 'open'),
                           ('请停止播放！', 'stop'), ('停止歌曲', 'stop'), ('下一首', 'next'), ('帮我切歌', 'next')):
            self.assertEqual(parse_song_command(text), {'mode': mode, 'query': ''})
        self.assertIsNone(parse_song_command('下一首是什么'))
