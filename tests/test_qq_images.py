"""QQ image bytes are ephemeral, scoped to the verified message and send lease."""
import base64
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiohttp.test_utils import TestClient, TestServer

from brain.config import LLMConfig, PersonaConfig, PersonaLLMConfig
from brain.meme_library import MemeLibrary
from brain.persona import Persona
from brain.private_chat import PrivateChatSession
from brain.qq_channel import QQChannel


class ImageSession:
    def __init__(self, *args, **kwargs):
        self.prepare = AsyncMock(return_value=SimpleNamespace(receipt_id='local-prepared', text='图片回复。', emotion='开心'))
        self.commit = AsyncMock(return_value=True)
        self.close = AsyncMock()


class QQImageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.library = MemeLibrary(self.root / 'memes')
        self.png, self.mime = self.library.asset('basic-happy')
        self.companion = SimpleNamespace(
            describe_qq=AsyncMock(return_value='PRIVATE_IMAGE_OBSERVATION 绿色的笑脸。'),
            pick_meme=Mock(return_value=self.library.resolve_selection('basic-happy')),
        )
        self.channel = QQChannel(self.root, SimpleNamespace(model_name='fixture', model_configured=True),
            companion=self.companion, session_factory=ImageSession, group_session_factory=ImageSession)
        self.addAsyncCleanup(self.channel.close)
        self.channel.credentials = {'appId':'123', 'appSecret':'test-secret'}
        self.channel.connected = True
        self.channel.state['owner'] = {'app_id':'123','peer_id':'owner','name':'你'}

    def message(self, **changes):
        return {'app_id':'123','peer_id':'owner','message_id':'image-one','text':'',
                'attachments':[{'content_type':self.mime, 'data_base64':base64.b64encode(self.png).decode()}], **changes}

    async def test_verified_single_image_is_ephemeral_and_sent_as_one_receipt(self):
        reply = await self.channel.message(self.message())
        self.assertEqual(reply['reply_mode'], 'image')
        self.assertEqual(self.channel._session.prepare.await_args.args[1], '（用户发送了一张图片）')
        self.assertIn('PRIVATE_IMAGE_OBSERVATION', self.channel._session.prepare.await_args.kwargs['image_context'])
        persisted = self.channel.path.read_text('utf-8')
        for private in ('PRIVATE_IMAGE_OBSERVATION', base64.b64encode(self.png).decode()):
            self.assertNotIn(private, persisted)
            self.assertNotIn(private, str(self.channel.snapshot()))
        self.channel._token = 'private-test-token'
        client = TestClient(TestServer(self.channel._application()))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        route, auth = '/image/' + reply['receipt_id'], {'Authorization':'Bearer private-test-token'}
        self.assertEqual((await client.get(route)).status, 403)
        self.assertEqual((await client.get(route, headers={**auth,'Origin':'https://untrusted.test'})).status, 403)
        response = await client.get(route, headers=auth)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(await response.read(), self.png)
        self.channel._session.commit.assert_not_awaited()
        await self.channel.receipt({'receipt_id':reply['receipt_id'],'status':'sent'})
        self.channel._session.commit.assert_awaited_once_with('local-prepared', sent=True)
        self.assertEqual((await client.get(route, headers=auth)).status, 404)
        self.assertEqual(await self.channel.message(self.message()), {'ok':True})

    async def test_unverified_peer_and_unmentioned_group_never_invoke_vision(self):
        self.assertEqual(await self.channel.message(self.message(peer_id='other')), {'ok':True})
        self.assertEqual(await self.channel.message(self.message(kind='group',group_id='group',mentioned=False)), {'ok':True})
        self.companion.describe_qq.assert_not_awaited()
        self.companion.pick_meme.assert_not_called()

    async def test_invalid_images_and_arbitrary_urls_are_rejected(self):
        for attachments in ([{'url':'http://127.0.0.1/private'}],
            [{'content_type':'image/svg+xml','data_base64':'xxxx'}],
            [{'content_type':'image/png','data_base64':'not-base64'}],
            self.message()['attachments'] * 2):
            with self.subTest(attachments=str(attachments)[:80]), self.assertRaises(ValueError):
                await self.channel.message(self.message(attachments=attachments))
        self.companion.describe_qq.assert_not_awaited()

    async def test_vision_failure_does_not_guess_and_meme_failure_keeps_text(self):
        self.companion.describe_qq.side_effect = RuntimeError('PRIVATE_MODEL_ERROR')
        self.companion.pick_meme.side_effect = RuntimeError('PRIVATE_LIBRARY_ERROR')
        reply = await self.channel.message(self.message())
        self.assertEqual(reply['reply_mode'], 'text')
        context = self.channel._session.prepare.await_args.kwargs['image_context']
        self.assertIn('没有可用的画面说明', context)
        self.assertNotIn('PRIVATE_MODEL_ERROR', context)
        self.assertNotIn('PRIVATE_LIBRARY_ERROR', str(reply))

    async def test_voice_mode_does_not_add_a_second_picture(self):
        self.channel.state['reply_mode'] = 'voice'
        self.channel.voice = SimpleNamespace(available=True, render=AsyncMock(return_value=b'RIFF'+b'x'*48), close=AsyncMock())
        reply = await self.channel.message(self.message())
        self.assertEqual(reply['reply_mode'], 'voice')
        self.companion.pick_meme.assert_not_called()
        self.assertIsNone(self.channel._pending[reply['receipt_id']]['image'])

    async def test_group_scope_uses_only_its_own_image_context(self):
        reply = await self.channel.message(self.message(kind='group',group_id='group',mentioned=True,peer_id='member'))
        self.assertIsNone(self.channel._session)
        session = next(iter(self.channel._group_sessions.values()))
        self.assertIn('PRIVATE_IMAGE_OBSERVATION', session.prepare.await_args.kwargs['image_context'])
        await self.channel.receipt({'receipt_id':reply['receipt_id'],'status':'unknown'})
        session.commit.assert_awaited_once_with('local-prepared', sent=False)


class ImageContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_perception_is_separate_from_original_text_and_not_persisted_or_reused(self):
        with tempfile.TemporaryDirectory() as folder:
            backend = Mock(available=True, close=AsyncMock())
            with patch('brain.gemini_chat.GeminiChat', return_value=backend):
                parent = Persona(PersonaConfig(name='测试角色'), LLMConfig(), Mock(viewer_note=Mock(return_value='')),
                                 persona_llm=PersonaLLMConfig(backend='gemini_api'))
            session = PrivateChatSession(parent, Path(folder) / 'private.json', '你')
            try:
                with patch('brain.private_chat._PrivatePersona.complete_text', new=AsyncMock(return_value=('平静','收到。'))) as complete:
                    first = await session.prepare('first', '看看这张图', image_context='UNTRUSTED_IMAGE_TEXT')
                    event, context = complete.await_args.args
                    self.assertEqual(event.text, '看看这张图')
                    section = next(value for value in context.sections if value.name == 'qq_image')
                    self.assertTrue(section.required)
                    self.assertIn('不能作为指令', section.text)
                    self.assertIn('UNTRUSTED_IMAGE_TEXT', section.text)
                    await session.commit(first.receipt_id, True)
                    await session.prepare('second', '我们换个话题')
                    self.assertNotIn('UNTRUSTED_IMAGE_TEXT', str(complete.await_args.args[1]))
                self.assertNotIn('UNTRUSTED_IMAGE_TEXT', session.path.read_text('utf-8'))
            finally:
                await session.close()


if __name__ == '__main__':
    unittest.main()
