import asyncio
import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from brain.code_review import digest
from brain.code_verification import verify_code


class CodeVerificationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'brain').mkdir()
        (self.root / 'brain/app.py').write_text('version = 2\n', encoding='utf-8')
        (self.root / 'brain/dependency.py').write_text('dependency = 1\n', encoding='utf-8')
        self.version = {name: digest((self.root / name).read_bytes())
                        for name in ('brain/app.py', 'brain/dependency.py')}
        self.job = {'id': 'test', 'state': 'applied', 'revision': 3}
        self.report = {'source_version': self.version}
        self.manager = SimpleNamespace(root=self.root, verification_url='http://127.0.0.1:8080/runtime-health',
                                       get=lambda job_id: self.job,
                                       code_review=lambda job_id: SimpleNamespace(read=lambda: self.report))

        def update(job, **fields):
            if 'state' in fields and fields['state'] != job['state']:
                job['revision'] += 1
            job.update(fields)
        self.manager.update = update
        self.client = AsyncMock()
        self.client.__aenter__.return_value = self.client
        self.client_patch = patch('brain.code_verification.httpx.AsyncClient', return_value=self.client)
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)
        self.health = {'scope': 'main', 'ready': True, 'boot_id': 'current-boot',
                       'code': copy.deepcopy(self.version)}
        self.respond(self.health)

    def respond(self, health):
        self.client.get.side_effect = None
        self.client.get.return_value = SimpleNamespace(raise_for_status=lambda: None, json=lambda: health)

    def assert_preserved(self):
        self.assertEqual(self.job['state'], 'applied')
        self.assertEqual((self.root / 'brain/app.py').read_text(encoding='utf-8'), 'version = 2\n')
        self.assertNotIn('runtime_boot', self.job)

    async def test_transient_network_failure_is_retryable_and_next_attempt_can_verify(self):
        for error, reason in ((httpx.ReadTimeout('timeout'), 'timeout'),
                              (httpx.ConnectError('refused'), 'unavailable')):
            with self.subTest(reason=reason):
                self.client.get.side_effect = error
                result = await verify_code(self.manager, 'test')
                self.assertFalse(result['verified'])
                self.assertTrue(result['retryable'])
                self.assertEqual(result['reason'], reason)
                self.assertFalse(result['restart_required'])
                self.assert_preserved()
        self.respond(self.health)
        result = await verify_code(self.manager, 'test')
        self.assertTrue(result['verified'])
        self.assertEqual(self.job['state'], 'verified')
        self.assertEqual(self.job['runtime_boot'], 'current-boot')

    async def test_non_ready_and_malformed_health_cannot_claim_success_or_remove_files(self):
        cases = [(None, 'malformed_health'), ([], 'malformed_health'),
                 ({**self.health, 'scope': 'console'}, 'wrong_scope'),
                 ({**self.health, 'ready': False}, 'not_ready'),
                 ({**self.health, 'ready': 'true'}, 'not_ready'),
                 ({**self.health, 'boot_id': None}, 'invalid_boot'),
                 ({**self.health, 'boot_id': ''}, 'invalid_boot'),
                 ({**self.health, 'boot_id': 12}, 'invalid_boot'),
                 ({**self.health, 'code': []}, 'malformed_manifest'),
                 ({**self.health, 'code': {'brain/app.py': 'unhashed'}}, 'malformed_manifest')]
        for health, reason in cases:
            with self.subTest(reason=reason, health=health):
                self.respond(health)
                result = await verify_code(self.manager, 'test')
                self.assertFalse(result['verified'])
                self.assertEqual(result['reason'], reason)
                self.assert_preserved()

    async def test_http_startup_failure_and_invalid_json_keep_files(self):
        request = httpx.Request('GET', self.manager.verification_url)
        response = httpx.Response(503, request=request)
        self.client.get.side_effect = httpx.HTTPStatusError('startup failure', request=request, response=response)
        result = await verify_code(self.manager, 'test')
        self.assertEqual(result['reason'], 'http_error')
        self.assertEqual(result['http_status'], 503)
        self.assert_preserved()
        self.client.get.side_effect = None
        self.client.get.return_value = SimpleNamespace(raise_for_status=lambda: None,
                                                       json=lambda: (_ for _ in ()).throw(ValueError('not JSON')))
        result = await verify_code(self.manager, 'test')
        self.assertEqual(result['reason'], 'malformed_health')
        self.assert_preserved()

    async def test_runtime_version_difference_names_unchanged_dependencies(self):
        self.health['code']['brain/dependency.py'] = digest(b'old dependency')
        self.health['code']['brain/extra.py'] = digest(b'extra')
        result = await verify_code(self.manager, 'test')
        self.assertFalse(result['verified'])
        self.assertEqual(result['runtime_diff']['changed'], ['brain/dependency.py'])
        self.assertEqual(result['runtime_diff']['extra'], ['brain/extra.py'])
        self.assertEqual(result['disk_diff']['total'], 0)
        self.assertTrue(result['restart_required'])
        self.assertTrue(result['retryable'])
        self.assert_preserved()

    async def test_disk_and_runtime_drift_are_reported_independently(self):
        (self.root / 'brain/dependency.py').write_text('dependency = 99\n', encoding='utf-8')
        self.health['code'].pop('brain/app.py')
        result = await verify_code(self.manager, 'test')
        self.assertEqual(result['reason'], 'version_mismatch')
        self.assertEqual(result['runtime_diff']['missing'], ['brain/app.py'])
        self.assertEqual(result['disk_diff']['changed'], ['brain/dependency.py'])
        self.assertFalse(result['retryable'])
        self.assertFalse(result['restart_required'])
        self.assertEqual((self.root / 'brain/dependency.py').read_text(encoding='utf-8'), 'dependency = 99\n')
        self.assert_preserved()

    async def test_review_without_full_source_binding_is_not_verified(self):
        self.report.clear()
        result = await verify_code(self.manager, 'test')
        self.assertEqual(result['reason'], 'review_invalid')
        self.assertFalse(result['retryable'])
        self.client.get.assert_not_awaited()
        self.assert_preserved()

    async def test_state_or_revision_change_during_request_does_not_get_overwritten(self):
        for state, revision in (('rolled_back', 4), ('applied', 4)):
            with self.subTest(state=state):
                self.job.update(state='applied', revision=3)
                entered, release = asyncio.Event(), asyncio.Event()

                async def wait_for_response(url):
                    entered.set()
                    await release.wait()
                    return SimpleNamespace(raise_for_status=lambda: None, json=lambda: self.health)

                self.client.get.side_effect = wait_for_response
                task = asyncio.create_task(verify_code(self.manager, 'test'))
                await entered.wait()
                self.job.update(state=state, revision=revision, detail='newer change')
                release.set()
                with self.assertRaisesRegex(ValueError, '状态或检查批次发生变化'):
                    await task
                self.assertEqual(self.job['state'], state)
                self.assertEqual(self.job['detail'], 'newer change')
                self.assertNotIn('runtime_boot', self.job)

    async def test_late_failed_check_cannot_overwrite_a_newer_success(self):
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def responses(url):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                await release.wait()
                raise httpx.ReadTimeout('late timeout')
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: self.health)

        self.client.get.side_effect = responses
        old = asyncio.create_task(verify_code(self.manager, 'test'))
        await entered.wait()
        self.assertTrue((await verify_code(self.manager, 'test'))['verified'])
        release.set()
        with self.assertRaisesRegex(ValueError, '状态或检查批次发生变化'):
            await old
        self.assertEqual(self.job['state'], 'verified')
        self.assertEqual(self.job['verification_check']['status'], 'verified')


if __name__ == '__main__':
    unittest.main()
