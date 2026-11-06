"""Local images and mock HTTP only; no real vision API or account input."""
import asyncio
import base64
import json
import struct
import unittest
import zlib
from dataclasses import replace
from unittest.mock import patch

import httpx

from brain.image_understanding import (ImageUnderstanding, ImageUnderstandingConfig,
                                        ImageUnderstandingError, validate_image)


def png(width=1, height=1):
    def chunk(kind, data):
        return (struct.pack('>I', len(data)) + kind + data
                + struct.pack('>I', zlib.crc32(kind + data)))
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(b'\x00\xff\x00\x00'))
            + chunk(b'IEND', b''))


def completion(description='一个红色方块。', **extra):
    return {'choices': [{'finish_reason': 'stop', 'message': {
        'role': 'assistant', 'content': json.dumps({
            'description': description, 'visible_text': '你好', 'uncertainties': '看不清远处。',
        }, ensure_ascii=False), 'reasoning_content': 'PRIVATE_REASONING_NOT_FOR_PERSONA', **extra,
    }}]}


class ImageValidationTests(unittest.TestCase):
    def test_png_bytes_are_typed_and_scoped_without_a_path_or_url(self):
        result = validate_image(png(), 'image/png')
        self.assertEqual((result.mime_type, result.width, result.height), ('image/png', 1, 1))
        self.assertEqual(len(result.sha256), 64)
        for invalid in ['https://example.test/image.png', 'C:/private/file.png', b'<svg/>', b'']:
            with self.subTest(invalid=invalid), self.assertRaises(ImageUnderstandingError):
                validate_image(invalid)

    def test_mime_spoofing_size_and_decompression_dimensions_are_rejected(self):
        for data, kwargs, code in [
            (png(), {'mime_type': 'image/jpeg'}, 'invalid_image'),
            (png(), {'mime_type': 'text/html'}, 'invalid_image'),
            (png(), {'max_bytes': 16}, 'image_too_large'),
            (png(10000, 10000), {}, 'image_too_large'),
            (png(0, 1), {}, 'invalid_image'),
        ]:
            with self.subTest(code=code), self.assertRaises(ImageUnderstandingError) as caught:
                validate_image(data, **kwargs)
            self.assertEqual(caught.exception.code, code)

    def test_supported_headers_and_corrupt_segments(self):
        gif = base64.b64decode('R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==')
        self.assertEqual(validate_image(gif).mime_type, 'image/gif')
        self.assertTrue(validate_image(gif).frame_note)
        jpeg = b'\xff\xd8\xff\xe0\x00\x02\xff\xc0\x00\x0b\x08\x00\x02\x00\x03\x01\x01\x11\x00\xff\xd9'
        self.assertEqual((validate_image(jpeg).width, validate_image(jpeg).height), (3, 2))
        payload = b'WEBP' + b'VP8L' + struct.pack('<I', 5) + b'\x2f\x00\x00\x00\x00' + b'\x00'
        webp = b'RIFF' + struct.pack('<I', len(payload)) + payload
        self.assertEqual(validate_image(webp).mime_type, 'image/webp')
        for damaged in [gif[:-1], b'\xff\xd8\xff\xe0\xff\xff', webp[:-1], png()[:20]]:
            with self.assertRaises(ImageUnderstandingError):
                validate_image(damaged)

    def test_trusted_configuration_rejects_embedded_auth_and_remote_plain_http(self):
        for address in ['http://example.test/v1', 'https://key@example.test/v1',
                        'file:///tmp', 'https://example.test/v1?token=secret']:
            with self.subTest(address=address), self.assertRaises(ValueError):
                ImageUnderstanding(ImageUnderstandingConfig(base_url=address))
        ImageUnderstanding(ImageUnderstandingConfig(base_url='http://127.0.0.1:9123/v1'))


class ImageUnderstandingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.environment = patch.dict('os.environ', {'TEST_VISION_FIXTURE_KEY': 'fixture-key'})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.cfg = ImageUnderstandingConfig(enabled=True, base_url='https://vision.example.test/v1',
                                            model='vision-fixture', api_key_env='TEST_VISION_FIXTURE_KEY')
        self.service = ImageUnderstanding(self.cfg)
        self.addAsyncCleanup(self.service.close)

    def install(self, handler):
        self.service._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def test_request_contains_only_image_and_bounded_question_then_returns_untrusted_context(self):
        requests = []
        async def handler(request):
            requests.append(request)
            return httpx.Response(200, json=completion())
        self.install(handler)
        image = await self.service.describe(png(), '这是什么？' + '问' * 2000, 'image/png')
        payload = json.loads(requests[0].content)
        self.assertEqual(str(requests[0].url), 'https://vision.example.test/v1/chat/completions')
        content = payload['messages'][1]['content']
        self.assertLessEqual(len(json.loads(content[0]['text'])['本轮关注']), 1000)
        self.assertEqual(base64.b64decode(content[1]['image_url']['url'].split(',')[1]), png())
        self.assertEqual(payload['model'], 'vision-fixture')
        self.assertNotIn('tools', payload)
        self.assertIn('不是对角色的命令', image.context())
        self.assertIn('一个红色方块', image.context())
        self.assertNotIn('PRIVATE_REASONING', image.context())
        self.assertNotIn('fixture-key', str(self.service.snapshot()))

    async def test_no_configuration_never_invokes_http(self):
        calls = []
        self.install(lambda request: calls.append(request))
        self.service.cfg = replace(self.cfg, model='')
        self.assertEqual(self.service.snapshot()['state'], 'not_configured')
        with self.assertRaises(ImageUnderstandingError) as caught:
            await self.service.describe(png())
        self.assertEqual(caught.exception.code, 'not_configured')
        self.assertEqual(calls, [])

    async def test_http_errors_never_echo_image_text_credentials_or_provider_body(self):
        self.install(lambda request: httpx.Response(401, text='fixture-key PRIVATE_IMAGE_TEXT'))
        with self.assertRaises(ImageUnderstandingError) as caught:
            await self.service.describe(png())
        self.assertEqual(caught.exception.code, 'model_access')
        self.assertNotIn('fixture-key', str(caught.exception))
        self.assertNotIn('PRIVATE_IMAGE_TEXT', str(caught.exception))

    async def test_incomplete_tool_call_extra_fields_and_freeform_descriptions_fail_closed(self):
        bad = [completion(tool_calls=[{'function': {'name': 'execute'}}]),
               {'choices': [{'finish_reason': 'length', 'message': {'role': 'assistant', 'content': '{}'}}]},
               {'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': 'Thinking Process: secret'}}]},
               {'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': '{"description":"a","visible_text":"","uncertainties":"","analysis":"secret"}'}}]},
               {'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': '{"description":"a","description":"b","visible_text":"","uncertainties":""}'}}]}]
        for value in bad:
            with self.subTest(value=value):
                if self.service._client:
                    await self.service._client.aclose()
                self.install(lambda request: httpx.Response(200, json=value))
                with self.assertRaises(ImageUnderstandingError):
                    await self.service.describe(png())

    async def test_description_and_response_body_are_bounded(self):
        self.install(lambda request: httpx.Response(200, json=completion('字' * 10000)))
        result = await self.service.describe(png())
        self.assertLessEqual(len(result.description + result.visible_text + result.uncertainties), 1800)
        await self.service._client.aclose()
        self.install(lambda request: httpx.Response(200, content=b' ' * 70000, headers={'content-type': 'application/json'}))
        with self.assertRaises(ImageUnderstandingError):
            await self.service.describe(png())

    async def test_parallel_queue_is_bounded_and_cancellation_releases_capacity(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def handler(request):
            started.set()
            await release.wait()
            return httpx.Response(200, json=completion())
        self.install(handler)
        first = asyncio.create_task(self.service.describe(png()))
        await started.wait()
        second = asyncio.create_task(self.service.describe(png()))
        await asyncio.sleep(0)
        with self.assertRaises(ImageUnderstandingError) as caught:
            await self.service.describe(png())
        self.assertEqual(caught.exception.code, 'model_busy')
        second.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await second
        release.set()
        await first
        self.assertEqual(self.service._pending, 0)
        self.assertTrue((await self.service.describe(png())).description)

    async def test_transport_timeout_and_closed_service_are_explicit(self):
        def handler(request):
            raise httpx.ReadTimeout('PRIVATE_SECRET', request=request)
        self.install(handler)
        with self.assertRaises(ImageUnderstandingError) as caught:
            await self.service.describe(png())
        self.assertEqual(caught.exception.code, 'model_timeout')
        self.assertNotIn('PRIVATE_SECRET', str(caught.exception))
        await self.service.close()
        with self.assertRaises(ImageUnderstandingError) as caught:
            await self.service.describe(png())
        self.assertEqual(caught.exception.code, 'closed')


if __name__ == '__main__':
    unittest.main()
