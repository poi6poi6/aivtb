import unittest
from datetime import datetime
from unittest.mock import patch
from brain.clock_context import LOCAL_ZONE, clock_prompt, factual_reply


class ClockContextTests(unittest.TestCase):
    def test_date_and_weekday_use_fresh_clock(self):
        with patch('brain.clock_context.local_now', return_value=datetime(2026, 9, 7, 1, 30, tzinfo=LOCAL_ZONE)):
            self.assertEqual(factual_reply('现在是几号？'), '按北京时间，现在是2026年9月7日，星期一。')
            self.assertIn('1点30分', factual_reply('现在几点'))
            self.assertIn('UTC+8', clock_prompt())
        with patch('brain.clock_context.local_now', return_value=datetime(2026, 9, 8, tzinfo=LOCAL_ZONE)):
            self.assertIn('9月8日', factual_reply('今天几号'))

    def test_weather_does_not_invent_location(self):
        self.assertIn('哪个城市', factual_reply('明天的天气？'))
        self.assertIsNone(factual_reply('大阪明天的天气？'))
        self.assertIsNone(factual_reply('现在心情好吗'))
