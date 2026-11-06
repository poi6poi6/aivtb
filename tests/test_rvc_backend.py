import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace
import wave
from brain.rvc_backend import RVCBackend
from brain.music_requests import MusicRequests


class RVCTests(unittest.TestCase):
    def test_missing_model_and_config_are_honest(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            backend = RVCBackend(root, MusicRequests(root/'requests.json'), AsyncMock())
            self.assertIn('配置', backend.missing()[0])
            (root/'config').mkdir()
            (root/'config/rvc.json').write_text(json.dumps({'model':''}))
            self.assertTrue(any('RVC' in message and '模型' in message for message in backend.missing()))

    def test_source_matches_exact_title_and_never_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            sources = root/'data/music/sources'
            sources.mkdir(parents=True)
            song = sources/'spiral.wav'
            song.write_bytes(b'RIFF')
            backend = RVCBackend(root, MusicRequests(root/'requests.json'), AsyncMock())
            self.assertEqual(backend.source('SPIRAL'), song)
            self.assertIsNone(backend.source('../spiral'))
            self.assertIsNone(backend.source('spir'))


class RVCRenderTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_successful_valid_audio_is_published(self):
        for succeeds in (False, True):
            with tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                requests = MusicRequests(root/'requests.json')
                requests.submit('cover', 'test', 'viewer')
                job = requests.items[0]
                publish = AsyncMock()
                backend = RVCBackend(root, requests, publish)
                async def launch(*args, **kwargs):
                    if succeeds:
                        output = Path(args[args.index('--output')+1])
                        with wave.open(str(output), 'wb') as audio:
                            audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                            audio.writeframes(b'\x01\x00' * 16000)
                    return SimpleNamespace(returncode=0 if succeeds else 1)
                with patch('brain.rvc_backend.asyncio.create_subprocess_exec', side_effect=launch):
                    await backend.render(job, root/'input.wav')
                dispatched = [c.args[0] for c in publish.call_args_list if c.args[0]['type'] == 'song_request']
                self.assertEqual(bool(dispatched), succeeds)
                self.assertEqual(job['state'], 'ready' if succeeds else 'failed')
