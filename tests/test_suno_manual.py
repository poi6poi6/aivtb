import asyncio
import tempfile
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import AsyncMock
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from brain.suno_manual import SunoManual
from brain.music_requests import MusicRequests
from brain.webui import WebUI
from brain.config import WebUIConfig, ROOT


class SunoTests(unittest.IsolatedAsyncioTestCase):
    async def test_import_requires_stable_file_and_explicit_job_and_valid_audio(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); requests=MusicRequests(root/'requests.json')
            requests.submit('original','雨天','one'); job=requests.items[0]
            manual=SunoManual(root,requests); manual.ffmpeg=ROOT/'RVC/bin/ffmpeg.exe'
            manual.inbox.mkdir(parents=True)
            path=manual.inbox/'song.wav'
            with wave.open(str(path),'wb') as w:
                w.setparams((1,2,16000,0,'NONE','none'));w.writeframes(b'\x01\x00'*32000)
            self.assertFalse(manual.candidates())
            manual.observed={key:time.monotonic()-4 for key in manual.observed}
            candidate=manual.candidates()[0]
            with self.assertRaises(ValueError):manual.resolve('../song.wav')
            if not manual.ffmpeg.exists():self.skipTest('FFmpeg not installed')
            song=await manual.import_audio(job['id'],candidate['id'])
            self.assertTrue(song['url'].startswith('/songs/'))
            self.assertEqual(job['state'],'ready')
            with self.assertRaises(ValueError):await manual.import_audio(job['id'],candidate['id'])
            self.assertTrue(path.exists())

    def test_file_changes_invalidate_selection_and_drafts_are_bounded(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);manual=SunoManual(root,MusicRequests(root/'requests.json'))
            manual.inbox.mkdir(parents=True); path=manual.inbox/'x.mp3';path.write_bytes(b'a'*100)
            manual.candidates();manual.observed={k:time.monotonic()-4 for k in manual.observed}
            old=manual.candidates()[0]['id'];path.write_bytes(b'b'*200)
            with self.assertRaises(ValueError):manual.resolve(old)
            with self.assertRaises(ValueError):manual.validate_draft({'title':'x','style':'x','lyrics':''})

    async def test_public_and_cross_origin_cannot_import(self):
        for public in (False,True):
            ui=WebUI(WebUIConfig(public_test=public),asyncio.Queue(),'.',{})
            app=web.Application();app.router.add_post('/suno-manual',ui._suno_action)
            async with TestClient(TestServer(app)) as client:
                response=await client.post('/suno-manual',headers={'Origin':'https://evil.example'},json={})
                self.assertEqual(response.status,403)
