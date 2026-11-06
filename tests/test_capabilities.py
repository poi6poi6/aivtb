import unittest
from brain.capabilities import CAPABILITY_PROMPT, unexecuted_media_reply, unsupported_media_reply


class CapabilityTests(unittest.TestCase):
    def test_unimplemented_platform_actions_are_not_narrated_as_done(self):
        for request in ('播放第二个吧', '点开第2个视频', '网易云放一首歌', 'QQ音乐点歌青鸟'):
            self.assertIsNotNone(unsupported_media_reply(request), request)

    def test_other_topics_and_local_song_commands_are_not_blocked(self):
        for request in ('点歌 spiral', '在b站搜索 spiral 并播放', 'B站点歌青鸟',
                        'BV1xx411c7mD', '你喜欢什么音乐', '第二个问题是什么意思', '穿上外套'):
            self.assertIsNone(unsupported_media_reply(request), request)

    def test_prompt_describes_request_routing_without_claiming_playback(self):
        self.assertIn('没有查询结果时不能断言曲库没有', CAPABILITY_PROMPT)
        self.assertIn('播放量最高', CAPABILITY_PROMPT)
        self.assertIn('播放回执', CAPABILITY_PROMPT)
        self.assertNotIn('B站点歌需要用户在独立页面操作', CAPABILITY_PROMPT)

    def test_unexecuted_media_guard_is_separate_from_actual_playback_capability(self):
        for text in ('点歌 海色', 'B站点歌 海色', '你好，点歌 海色', '下一首', '停止播放'):
            reply = unexecuted_media_reply(text)
            self.assertIn('还没有执行', reply)
            self.assertNotIn('没有这首歌', reply)
            self.assertIsNone(unsupported_media_reply(text), 'real ingestion must remain executable')
        self.assertIsNone(unexecuted_media_reply('你喜欢什么音乐'))
