import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from brain.code_review import CodeReview, digest
from brain import upgrade_validation as validation


class PathPolicyTests(unittest.TestCase):
    def change(self, path):
        return {'path': path, 'operation': 'modify'}

    def test_business_scope_is_explicit_and_does_not_expand_to_siblings(self):
        self.assertEqual(validation.validate_paths([self.change('brain/static/face.js')],
                                                   ['brain/static/']), ['brain/static/face.js'])
        for path in ('brain/face.js', 'brain/static-other/face.js'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                validation.validate_paths([self.change(path)], ['brain/static/'])
        with self.assertRaises(ValueError):
            validation.validate_paths([self.change('brain/static/face.js')], [])

    def test_allowing_broad_directory_cannot_override_protected_files(self):
        for path in ('brain/code_review.py', 'brain/Code_Review.py', 'brain/task_daemon.py',
                     'brain/agent_tasks.py', 'brain/self_upgrade.py', 'brain/upgrade_release.py',
                     'brain/webui.py', 'brain/main.py', 'brain/viewer_identity.py',
                     'brain/safety.py', 'brain/config.py', 'brain/tool_registry.py',
                     'brain/static/tasks.js', 'brain/static/vendor/core.js',
                     'tests/test_business.py', 'tools/launcher/launch.ps1',
                     'tools/run_agent_console.py', 'body/package.json',
                     'brain/requirements-live.txt', 'brain/sitecustomize.py',
                     'requirements.txt', 'config/settings.yaml', 'data/state.json',
                     'vendor/blivedm/blivedm/__init__.py', 'vendor/blivedm/pyproject.toml'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                validation.validate_paths([self.change(path)], ['brain/', 'body/', 'tools/', 'tests/', 'config/', 'data/', 'vendor/'])

    def test_traversal_globs_windows_aliases_and_duplicates_are_rejected(self):
        for path in ('../brain/app.py', '/brain/app.py', 'G:/brain/app.py',
                     'brain\\app.py', 'brain//app.py', 'brain/./app.py',
                     'brain/.hidden/app.py', 'brain/app.py:stream', 'brain/app.py ',
                     'brain/*', 'brain/a[0].py', 'brain/a\x00.py'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                validation.validate_paths([self.change(path)], ['brain/'])
        with self.assertRaises(ValueError):
            validation.validate_paths([self.change('brain/a.py')], ['brain/*'])
        with self.assertRaises(ValueError):
            validation.validate_paths([self.change('brain/a.py'), self.change('brain/A.py')], ['brain/'])


class CandidateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'brain').mkdir()
        (self.root / 'brain/__init__.py').write_text('', encoding='utf-8')
        (self.root / 'brain/a.py').write_text('value = 1\n', encoding='utf-8')
        (self.root / 'tests').mkdir()
        (self.root / 'tests/__init__.py').write_text('', encoding='utf-8')
        (self.root / 'tests/test_candidate.py').write_text(
            'import os\nimport unittest\nfrom pathlib import Path\nfrom brain.a import value\n'
            'class Candidate(unittest.TestCase):\n'
            ' def test_business(self): self.assertEqual(value, 2)\n'
            ' def test_no_live_files(self):\n'
            '  self.assertFalse(Path("config").exists())\n'
            '  self.assertFalse(Path("data").exists())\n'
            '  self.assertNotIn("OPENAI_API_KEY", os.environ)\n', encoding='utf-8')
        # Synthetic sentinels only; the test never reads project/user secrets.
        (self.root / 'config').mkdir()
        (self.root / 'config/settings.yaml').write_text('private: true', encoding='utf-8')
        (self.root / '.env').write_text('SYNTHETIC_TEST_SECRET=hidden', encoding='utf-8')
        task = self.root / 'data/agent-tasks/candidate1'
        task.mkdir(parents=True)
        self.review = CodeReview(self.root, task, 'candidate1')
        self.review.prepare()
        (self.review.copy / 'brain/a.py').write_text('value = 2\n', encoding='utf-8')
        self.inspect()
        for attribute, value in (
                ('PYTHON_TEST_MODULES', ('tests.test_candidate',)), ('NODE_TEST_FILES', ()),
                ('BUSINESS_TESTS', {'brain/a.py': (('tests.test_candidate',), ())})):
            item = patch.object(validation, attribute, value)
            item.start()
            self.addCleanup(item.stop)

    def inspect(self):
        manifest = self.review.version()
        return self.review.inspect([{'command': 'python -m unittest', 'test': True,
            'cwd': str(self.review.copy), 'source_before': manifest, 'source_after': manifest,
            'exit_code': 0, 'output': 'Ran 2 tests in 0.001s\nOK\n'}])

    def saved(self):
        return json.loads((self.review.host / 'independent.json').read_text('utf-8'))

    async def successful(self, argv, *, cwd, timeout, env):
        self.assertTrue(cwd.is_absolute())
        self.assertNotEqual(cwd, self.review.copy)
        self.assertNotEqual(cwd, self.root)
        self.assertFalse((cwd / 'config').exists())
        self.assertFalse((cwd / 'data').exists())
        self.assertNotIn('OPENAI_API_KEY', env)
        self.assertNotIn('PYTHONPATH', env)
        self.assertNotIn('NODE_OPTIONS', env)
        return {'exit_code': 0, 'output': 'Ran 2 tests in 0.01s\nOK\n', 'timeout': False}

    async def test_real_fixed_tests_execute_staged_candidate_not_mutable_workspace(self):
        (self.review.copy / 'brain/a.py').write_text('value = 999\n', encoding='utf-8')
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'synthetic-sentinel'}):
            result = await validation.validate_candidate(self.root, 'candidate1', ['brain/a.py'])
        self.assertTrue(result['passed'], result)
        self.assertTrue(result['commands'][0]['tests_executed'])
        self.assertEqual(result['commands'][0]['exit_code'], 0)
        self.assertEqual(result['source_before'], result['source_after'])
        self.assertEqual(result['source_version'], self.review.read()['source_version'])
        self.assertEqual(result['baseline_version'], self.review.read()['baseline_version'])
        self.assertEqual(result['review_digest'], digest(json.dumps(self.review.read(), sort_keys=True).encode()))
        self.assertFalse(Path(result['commands'][0]['cwd']).exists())
        self.assertEqual((self.root / 'brain/a.py').read_text(), 'value = 1\n')
        self.assertEqual(self.saved(), result)

    async def test_business_mapping_adds_required_tests_not_agent_command(self):
        (self.root / 'tests/test_extra.py').write_text('', encoding='utf-8')
        # Host baseline is fixed: a newly added live test cannot silently join it.
        mapping = {'brain/a.py': (('tests.test_extra',), ())}
        with patch.object(validation, 'BUSINESS_TESTS', mapping):
            result = await validation.validate_candidate(self.root, 'candidate1', ['brain/'], runner=self.successful)
        self.assertFalse(result['passed'])
        self.assertIn('固定测试套件缺少文件', result['errors'][0])
        self.assertEqual(result['commands'], [])

    async def test_fixed_python_and_node_plan_must_all_pass(self):
        # Add a trusted Node fixture before taking a fresh host snapshot.
        (self.review.host / 'baseline/tests/test_fixed.js').write_text("console.log('PASS: fixed assertions');\n", encoding='utf-8')
        (self.review.copy / 'tests/test_fixed.js').write_text("console.log('PASS: fixed assertions');\n", encoding='utf-8')
        self.inspect()
        calls = []
        async def run(argv, **options):
            calls.append(argv)
            return {'exit_code': 0, 'output': 'PASS: node assertion\n' if argv[-1].endswith('.js')
                    else 'Ran 2 tests in 0.01s\nOK\n', 'timeout': False}
        with patch.object(validation, 'NODE_TEST_FILES', ('tests/test_fixed.js',)), patch.object(validation.shutil, 'which', return_value='node'):
            result = await validation.validate_candidate(self.root, 'candidate1', ['brain/a.py'], runner=run)
        self.assertTrue(result['passed'], result)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1], ['node', 'tests/test_fixed.js'])

    async def test_modified_test_is_rejected_before_runner(self):
        (self.review.copy / 'tests/test_candidate.py').write_text('print("PASS: fake")\n', encoding='utf-8')
        self.inspect()
        result = await validation.validate_candidate(self.root, 'candidate1', ['brain/', 'tests/'], runner=self.successful)
        self.assertFalse(result['passed'])
        self.assertEqual(result['commands'], [])

    async def test_unmapped_business_file_cannot_pass_unrelated_smoke(self):
        with patch.object(validation, 'BUSINESS_TESTS', {}):
            result = await validation.validate_candidate(self.root, 'candidate1', ['brain/a.py'], runner=self.successful)
        self.assertFalse(result['passed'])
        self.assertIn('尚无宿主固定验收映射', result['errors'][0])

    async def test_staged_tampering_and_manifest_omission_fail_closed(self):
        staged = self.review.host / 'staged/brain/a.py'
        staged.write_text('value = 3\n', encoding='utf-8')
        result = await validation.validate_candidate(self.root, 'candidate1', ['brain/'], runner=self.successful)
        self.assertFalse(result['passed'])
        self.assertEqual(result['commands'], [])
        staged.write_text('value = 2\n', encoding='utf-8')
        report = self.review.read()
        report['source_version'].pop('tests/test_candidate.py')
        (self.review.host / 'review.json').write_text(json.dumps(report), encoding='utf-8')
        result = await validation.validate_candidate(self.root, 'candidate1', ['brain/'], runner=self.successful)
        self.assertFalse(result['passed'])
        self.assertIn('完整版本', result['errors'][0])

    async def test_running_tests_cannot_mutate_candidate_source(self):
        async def mutate(argv, **options):
            result = await self.successful(argv, **options)
            (options['cwd'] / 'brain/a.py').write_text('value = 4\n', encoding='utf-8')
            return result
        result = await validation.validate_candidate(self.root, 'candidate1', ['brain/'], runner=mutate)
        self.assertFalse(result['passed'])
        self.assertEqual(result['reason'], 'source_changed')
        self.assertNotEqual(result['source_before'], result['source_after'])

    async def test_zero_tests_all_skipped_failed_and_bool_exit_cannot_pass(self):
        for code, output in ((0, 'Ran 0 tests in 0.1s\nOK\n'),
                             (0, 'Ran 2 tests in 0.1s\nOK (skipped=2)\n'),
                             (1, 'Ran 2 tests in 0.1s\nFAILED (failures=1)\n'),
                             (False, 'Ran 2 tests in 0.1s\nOK\n')):
            async def run(*args, **kwargs):
                return {'exit_code': code, 'output': output, 'timeout': False}
            with self.subTest(code=code, output=output):
                result = await validation.validate_candidate(self.root, 'candidate1', ['brain/'], runner=run)
                self.assertFalse(result['passed'])

    async def test_timeout_and_exhausted_budget_never_pass(self):
        async def timeout(*args, **kwargs):
            return {'exit_code': 0, 'output': 'Ran 2 tests in 0.1s\nOK\n', 'timeout': True}
        result = await validation.validate_candidate(self.root, 'candidate1', ['brain/'], runner=timeout)
        self.assertEqual(result['reason'], 'timeout')
        self.assertFalse(result['passed'])
        with patch.object(validation, 'TOTAL_TIMEOUT', 0):
            result = await validation.validate_candidate(self.root, 'candidate1', ['brain/'], runner=self.successful)
        self.assertEqual(result['reason'], 'budget_exceeded')
        self.assertFalse(result['passed'])
        self.assertEqual(result['commands'], [])

    async def test_runner_error_is_safe_and_keeps_failure_receipt(self):
        async def run(*args, **kwargs):
            raise ValueError('synthetic secret must not appear')
        result = await validation.validate_candidate(self.root, 'candidate1', ['brain/'], runner=run)
        self.assertEqual(result['reason'], 'runner_error')
        self.assertFalse(result['passed'])
        self.assertNotIn('synthetic secret', json.dumps(result))

    async def test_review_change_while_testing_invalidates_success(self):
        async def run(argv, **options):
            receipt = await self.successful(argv, **options)
            report = self.review.read()
            report['diff'] = 'changed after starting validation'
            (self.review.host / 'review.json').write_text(json.dumps(report), encoding='utf-8')
            return receipt
        result = await validation.validate_candidate(self.root, 'candidate1', ['brain/'], runner=run)
        self.assertFalse(result['passed'])
        self.assertEqual(result['reason'], 'source_changed')

    async def test_cancel_invalidates_previous_success_and_releases_validation_lock(self):
        good = await validation.validate_candidate(self.root, 'candidate1', ['brain/'], runner=self.successful)
        self.assertTrue(good['passed'])
        entered = asyncio.Event()
        async def wait(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        task = asyncio.create_task(validation.validate_candidate(self.root, 'candidate1', ['brain/'], runner=wait))
        await entered.wait()
        self.assertFalse(self.saved()['passed'])
        with self.assertRaises(ValueError):
            await validation.validate_candidate(self.root, 'candidate1', ['brain/'], runner=self.successful)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.saved()['reason'], 'cancelled')
        self.assertFalse(self.saved()['passed'])
        good = await validation.validate_candidate(self.root, 'candidate1', ['brain/'], runner=self.successful)
        self.assertTrue(good['passed'])

    async def test_job_id_cannot_escape_host_review_directory(self):
        for job_id in ('../candidate1', 'a/b', 'C:drive', '', 'a' * 65, None):
            with self.subTest(job_id=job_id), self.assertRaises(ValueError):
                await validation.validate_candidate(self.root, job_id, ['brain/'], runner=self.successful)
        self.assertFalse((self.review.host / 'independent.json').exists())


class OwnedRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancellation_stops_owned_real_process(self):
        real_popen = validation.subprocess.Popen
        started = asyncio.Event()
        processes = []
        def capture(argv, **options):
            process = real_popen(argv, **options)
            if argv[0] == sys.executable:
                processes.append(process)
                started.set()
            return process
        with tempfile.TemporaryDirectory() as directory, patch.object(validation.subprocess, 'Popen', capture):
            task = asyncio.create_task(validation.run_command(
                [sys.executable, '-c', 'import time; time.sleep(30)'], cwd=Path(directory),
                env=validation._environment(Path(directory)), timeout=30))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertIsNotNone(processes[0].poll())

    async def test_actual_process_timeout_is_not_a_success(self):
        with tempfile.TemporaryDirectory() as directory:
            result = await validation.run_command([sys.executable, '-c', 'import time; time.sleep(30)'],
                cwd=Path(directory), env=validation._environment(Path(directory)), timeout=.15)
        self.assertTrue(result['timeout'])
        self.assertIsNone(result['exit_code'])

    async def test_actual_process_output_budget_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(validation, 'MAX_OUTPUT_BYTES', 1024):
            result = await validation.run_command([sys.executable, '-c', 'print("x" * 50000)'],
                cwd=Path(directory), env=validation._environment(Path(directory)), timeout=10)
        self.assertEqual(result['error'], 'output_limit')
        self.assertIsNone(result['exit_code'])
        self.assertEqual(result['output'], '')


if __name__ == '__main__':
    unittest.main()
