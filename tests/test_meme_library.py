"""Local, path-bounded raster assets with deterministic caption/tag recall."""
import json
import tempfile
import unittest
from pathlib import Path

from brain.meme_library import MAX_IMAGE_BYTES, MemeError, MemeLibrary, image_info


class MemeLibraryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.library = MemeLibrary(Path(self.folder.name) / 'memes')

    def png(self):
        return self.library.asset('basic-happy')[0]

    def test_builtin_search_uses_real_caption_tags_and_no_paths(self):
        self.assertEqual(self.library.snapshot()['count'], 6)
        selected = self.library.search('发一个开心的表情', emotion='开心')
        self.assertEqual(selected[0]['id'], 'basic-happy')
        self.assertNotIn('root', selected[0])
        self.assertNotIn('file', selected[0])
        self.assertTrue(selected[0]['url'].startswith('/api/companion/memes/'))
        self.assertEqual(self.library.search('这个库里没有的火星飞船'), [])
        self.assertIn('basic-happy', self.library.prompt_context('开心'))

    def test_uploaded_names_never_become_paths_and_assets_survive_restart(self):
        item = self.library.add_image(self.png(), '../../secret.png', caption='我的开心', tags=['快乐'])
        data, mime = self.library.asset(item['id'])
        self.assertEqual(mime, 'image/png')
        self.assertEqual(data, self.png())
        self.assertEqual(len(list(self.library.folder.rglob('*.png'))), 1)
        reopened = MemeLibrary(self.library.folder)
        self.assertEqual(reopened.get(item['id'])['caption'], '我的开心')
        self.assertEqual(reopened.resolve_selection('../outside'), None)

    def test_toggles_and_reversible_remove_apply_to_selection(self):
        self.library.remove('basic-happy')
        self.assertIsNone(self.library.get('basic-happy'))
        self.library.update('basic-happy', enabled=True)
        self.assertIsNotNone(self.library.get('basic-happy'))
        self.library.configure(enabled=False, auto_send=True)
        self.assertEqual(self.library.search(), [])
        self.assertIsNone(self.library.resolve_selection('basic-happy'))
        reopened = MemeLibrary(self.library.folder)
        self.assertFalse(reopened.snapshot()['enabled'])
        self.assertTrue(reopened.snapshot()['auto_send'])

    def test_unknown_or_executable_images_and_oversize_are_rejected(self):
        for payload in [b'<svg onload="x"/>', b'x' * (MAX_IMAGE_BYTES + 1), b'RIFFxxxxWEBPfake']:
            with self.subTest(size=len(payload)), self.assertRaises(MemeError):
                self.library.add_image(payload, 'safe.png')
        broken = bytearray(self.png())
        broken[-5] ^= 1
        with self.assertRaises(MemeError):
            image_info(bytes(broken))

    def test_manifest_cannot_escape_root_or_alias_duplicate_builtin_ids(self):
        folder = self.library.folder
        folder.mkdir(parents=True)
        outside = folder.parent / 'outside.png'
        outside.write_bytes(self.png())
        items = [dict(id='evil-image', file='../outside.png', caption='秘密', tags=[]),
                 dict(id='evil-absolute', file=str(outside), caption='秘密', tags=[])]
        (folder / 'index.json').write_text(json.dumps(dict(version=1, enabled=True, items=items)), 'utf-8')
        reopened = MemeLibrary(folder)
        self.assertIsNone(reopened.get('evil-image'))
        self.assertIsNone(reopened.get('evil-absolute'))
        self.assertEqual(reopened.snapshot()['count'], 6)

    def test_changed_image_must_be_reindexed_before_serving(self):
        item = self.library.add_image(self.png(), caption='会变化')
        path = next((self.library.folder / 'images').glob('*.png'))
        path.write_bytes(self.library.asset('basic-sad')[0])
        self.assertIsNone(self.library.resolve_selection(item['id']))

    def test_metadata_is_bounded_and_bad_index_does_not_get_overwritten(self):
        with self.assertRaises(MemeError):
            self.library.update('basic-happy', tags=['x'] * 21)
        self.library.folder.mkdir(parents=True)
        self.library.path.write_text('{broken', 'utf-8')
        broken = MemeLibrary(self.library.folder)
        self.assertFalse(broken.snapshot()['enabled'])
        with self.assertRaises(MemeError):
            broken.configure(enabled=True)
        self.assertEqual(self.library.path.read_text('utf-8'), '{broken')


if __name__ == '__main__':
    unittest.main()
