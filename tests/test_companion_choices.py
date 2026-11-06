import tempfile
import unittest
from brain.companion_features import CompanionFeatures


class CompanionChoicesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.features = CompanionFeatures(folder.name)
        self.addAsyncCleanup(self.features.close)

    async def test_explicit_meme_request_and_opt_out(self):
        self.assertIsNotNone(self.features.pick_meme('发个开心的表情包', '开心', '给你。'))
        self.assertIsNone(self.features.pick_meme('不要给我发表情包', '开心', '好。'))
        self.assertIsNone(self.features.pick_meme('表情包是什么', '开心', '是一种图片表达。'))
        await self.features.configure({'auto_memes': True})
        self.assertIsNone(self.features.pick_meme('别配图', '开心', '好。'))

    async def test_unrelated_settings_do_not_cancel_inflight_vision(self):
        vision = self.features.vision
        vision._state = 'ready'
        await self.features.configure({'journal_enabled': False, 'auto_memes': True})
        self.assertIs(self.features.vision, vision)
        self.assertFalse(vision._closed)
        await self.features.configure({'vision': {'enabled': False}})
        self.assertIsNot(self.features.vision, vision)
        self.assertTrue(vision._closed)


if __name__ == '__main__':
    unittest.main()
