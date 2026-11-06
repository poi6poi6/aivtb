import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from brain.code_review import CodeReview, is_test_command, test_scope

class CodeReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root/'brain').mkdir()
        (self.root/'brain/a.py').write_text('value = 1\n')
        (self.root/'.env').write_text('SECRET=hidden')
        (self.root/'data/task').mkdir(parents=True)
        self.review = CodeReview(self.root, self.root/'data/task', 'sample')
        self.copy = self.review.prepare()

    def inspect(self):
        return self.review.inspect([self.receipt()])

    def receipt(self, command='python -m unittest', *, output='Ran 1 test in 0.001s\n\nOK\n', exit_code=0):
        return {'command':command, 'test':True, 'exit_code':exit_code, 'output':output,
                'cwd':str(self.copy),
                'source_before':self.review.version(), 'source_after':self.review.version()}

    def run_unittest(self, *arguments):
        before = self.review.version()
        log = self.root/'data/test-run.log'
        with log.open('wb') as output:
            result = subprocess.run([sys.executable, '-B', '-m', 'unittest', *arguments],
                                    cwd=self.copy, stdin=subprocess.DEVNULL, stdout=output,
                                    stderr=subprocess.STDOUT, timeout=20, check=False,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        return {'command': 'python -m unittest ' + ' '.join(arguments), 'test':True,
                'cwd':str(self.copy),
                'exit_code':result.returncode, 'output':log.read_text('utf-8', errors='replace'),
                'source_before':before, 'source_after':self.review.version()}

    def test_snapshot_diff_apply_and_no_runtime_claim(self):
        self.assertFalse((self.copy/'.env').exists())
        (self.copy/'brain/a.py').write_text('value = 2\n')
        (self.copy/'brain/new.py').write_text('answer = 42\n')
        r = self.inspect()
        self.assertTrue(r['can_apply'])
        self.assertIn('+value = 2', r['diff'])
        self.assertEqual((self.root/'brain/a.py').read_text(), 'value = 1\n')
        # Mutating the executor copy after review must not change the approved bytes.
        (self.copy/'brain/a.py').write_text('value = 999\n')
        self.assertEqual(self.review.apply(self.review.public()['token']), 2)
        self.assertEqual((self.root/'brain/a.py').read_text(), 'value = 2\n')
        with self.assertRaises(ValueError):
            self.review.apply(self.review.public()['token'])

    def test_conflict_preflight_prevents_partial_application(self):
        (self.copy/'brain/a.py').write_text('value = 2\n')
        (self.copy/'brain/new.py').write_text('x = 1\n')
        self.inspect()
        (self.root/'brain/a.py').write_text('value = 3\n')
        with self.assertRaisesRegex(ValueError, '项目已变化'):
            self.review.apply(self.review.public()['token'])
        self.assertFalse((self.root/'brain/new.py').exists())

    def test_snapshot_keeps_vendored_sources_and_provenance(self):
        vendor = self.root / 'vendor/blivedm'
        vendor.mkdir(parents=True)
        contents = {'blivedm/__init__.py': 'VERSION = "fixture"\n',
                    'pyproject.toml': '[project]\nname = "fixture"\n',
                    'upstream.json': '{"commit": "fixture"}',
                    'LICENSE': 'MIT test fixture',
                    '.env': 'SYNTHETIC_SECRET=hidden'}
        for name, content in contents.items():
            path = vendor / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf-8')
        task = self.root / 'data/vendor-task'
        task.mkdir()
        review = CodeReview(self.root, task, 'vendor-task')
        copy = review.prepare()
        for name, content in contents.items():
            path = copy / 'vendor/blivedm' / name
            if name == '.env':
                self.assertFalse(path.exists())
            else:
                self.assertEqual(path.read_text('utf-8'), content)
                self.assertIn('vendor/blivedm/' + name, review.version())

    def test_failed_missing_tests_and_syntax_block_apply(self):
        (self.copy/'brain/a.py').write_text('value = 2\n')
        self.assertFalse(self.review.inspect([])['can_apply'])
        self.assertFalse(self.review.inspect([{'test':True,'exit_code':1}])['can_apply'])
        (self.copy/'brain/a.py').write_text('value = (\n')
        self.assertFalse(self.inspect()['can_apply'])
        with self.assertRaises(ValueError):
            self.review.apply(self.review.public()['token'])

    def test_deletion_and_stale_token(self):
        (self.copy/'brain/a.py').unlink()
        self.inspect()
        with self.assertRaises(ValueError):
            self.review.apply('wrong')
        self.review.apply(self.review.public()['token'])
        self.assertFalse((self.root/'brain/a.py').exists())
        self.assertTrue((self.review.host/'baseline/brain/a.py').exists())

    def test_test_receipt_must_match_final_source_and_dependencies(self):
        (self.copy/'brain/a.py').write_text('value = 2\n')
        version = self.review.version()
        receipt = {'command':'python -m unittest', 'test':True,'exit_code':0,
                   'cwd':str(self.copy),
                   'source_before':version,'source_after':version,'output':'Ran 1 test in 0.001s\nOK\n'}
        self.assertTrue(self.review.inspect([receipt])['can_apply'])
        (self.copy/'brain/a.py').write_text('value = 3\n')
        self.assertFalse(self.review.inspect([receipt])['can_apply'])
        receipt['source_after'] = self.review.version()
        self.assertFalse(self.review.inspect([receipt])['can_apply'])
        receipt['source_before'] = self.review.version()
        self.assertTrue(self.review.inspect([receipt])['can_apply'])
        (self.root/'brain/dependency.py').write_text('changed = True\n')
        with self.assertRaisesRegex(ValueError,'测试依赖版本失效'):
            self.review.apply(self.review.public()['token'])

    def test_legacy_receipt_cannot_authorize_application(self):
        (self.copy/'brain/a.py').write_text('value = 2\n')
        self.assertFalse(self.review.inspect([{'command':'python -m unittest', 'test':True,'exit_code':0}])['can_apply'])

    def test_tests_must_run_in_candidate_not_original_or_unknown_directory(self):
        (self.copy/'brain/a.py').write_text('value = 2\n')
        receipt = self.receipt()
        for cwd in (None, '', 'workspace', str(self.root), str(self.copy/'tests')):
            with self.subTest(cwd=cwd):
                receipt['cwd'] = cwd
                self.assertFalse(self.review.inspect([receipt])['can_apply'])
        receipt.pop('cwd')
        self.assertFalse(self.review.inspect([receipt])['can_apply'])
        receipt['cwd'] = str(self.copy)
        self.assertTrue(self.review.inspect([receipt])['can_apply'])

    def test_real_unittest_subset_cannot_clear_failure_and_fixed_full_rerun_can(self):
        (self.copy/'brain/a.py').write_text('value = 2\n')
        (self.copy/'tests').mkdir()
        (self.copy/'tests/__init__.py').write_text('')
        (self.copy/'tests/test_a.py').write_text(
            'import unittest\nclass A(unittest.TestCase):\n'
            '    def test_a(self): self.assertTrue(True)\n')
        (self.copy/'tests/test_b.py').write_text(
            'import unittest\nfrom brain.a import value\nclass B(unittest.TestCase):\n'
            '    def test_b(self): self.assertEqual(value, 3)\n')
        failed = self.run_unittest('tests.test_a', 'tests.test_b')
        partial = self.run_unittest('tests.test_a')
        self.assertEqual(failed['exit_code'], 1, failed['output'])
        self.assertEqual(partial['exit_code'], 0, partial['output'])
        self.assertFalse(self.review.inspect([failed, partial])['tests_passed'])
        self.assertFalse(self.review.inspect([failed, partial])['can_apply'])
        (self.copy/'brain/a.py').write_text('value = 3\n')
        fixed = self.run_unittest('-q', 'tests.test_a', 'tests.test_b')
        self.assertEqual(fixed['exit_code'], 0, fixed['output'])
        self.assertTrue(self.review.inspect([failed, fixed])['can_apply'])

    def test_real_empty_unittest_discovery_is_not_validation(self):
        (self.copy/'brain/a.py').write_text('value = 2\n')
        (self.copy/'tests').mkdir()
        empty = self.run_unittest('discover', '-s', 'tests')
        self.assertEqual(empty['exit_code'], 0, empty['output'])
        self.assertIn('Ran 0 tests', empty['output'])
        self.assertFalse(self.review.inspect([empty])['can_apply'])

    def test_scope_preserves_multiple_targets_filters_and_quoted_paths(self):
        (self.copy/'brain/a.py').write_text('value = 2\n')
        cases = [
            ('python -m unittest -q tests.test_a tests.test_b', 'python -m unittest -q tests.test_a'),
            ('python -m unittest discover -s tests', 'python -m unittest discover -s tests -p test_a.py'),
            ('python -m unittest tests.test_a', 'python -m unittest tests.test_a -k fast'),
            ('python -m pytest "tests/test a.py" tests/test_b.py', 'python -m pytest "tests/test a.py"'),
            ('python -m pytest tests', 'python -m pytest tests -k "fast or smoke"'),
            ('python -m pytest tests', 'python -m pytest tests --deselect tests/test_b.py::test_b'),
            ('python -m pytest tests', 'python -m pytest tests --ignore tests/test_b.py'),
            ('python -m pytest -- -q tests/test_a.py', 'python -m pytest -- tests/test_a.py'),
            ('python -m pytest -- -v tests/test_a.py', 'python -m pytest -- tests/test_a.py'),
            ('python -m pytest tests -k "-v"', 'python -m pytest tests -k "-q"'),
            ('python -m unittest tests.test_a -k "-v"', 'python -m unittest tests.test_a -k "-q"'),
        ]
        for broad, subset in cases:
            output = '2 passed in 0.02s\n' if 'pytest' in broad else 'Ran 2 tests in 0.02s\nOK\n'
            with self.subTest(command=broad, subset=subset):
                failed = self.receipt(broad, output=output, exit_code=1)
                passed = self.receipt(subset, output=output)
                self.assertFalse(self.review.inspect([failed, passed])['tests_passed'])
                same = self.receipt(broad, output=output)
                self.assertTrue(self.review.inspect([failed, same])['tests_passed'])

    def test_same_scope_under_quoted_windows_python_and_verbosity_reruns(self):
        failed = self.receipt('python -m unittest -q "tests/test a.py" tests.test_b', exit_code=1)
        passed = self.receipt('& "G:\\folder with spaces\\python.exe" -X utf8 -m unittest "tests/test a.py" tests.test_b -v')
        self.assertTrue(self.review.inspect([failed, passed])['tests_passed'])

    def test_runner_evidence_required_and_all_skipped_is_not_execution(self):
        cases = [
            ('python -m unittest', None),
            ('python -m unittest', 'saved report'),
            ('python -m unittest', 'Ran 0 tests in 0.01s\nOK\n'),
            ('python -m unittest', 'Ran 2 tests in 0.01s\nOK (skipped=2)\n'),
            ('python -m pytest', 'no tests ran in 0.01s\n'),
            ('python -m pytest', '2 skipped in 0.01s\n'),
            ('python -m pytest', '1 failed, 2 passed in 0.01s\n'),
            ('python -m unittest', 'Ran 1 test in 0.01s\nFAILED (failures=1)\n'),
        ]
        for command, output in cases:
            with self.subTest(command=command, output=output):
                self.assertFalse(self.review.inspect([self.receipt(command, output=output)])['tests_passed'])
        for command, output in [
            ('python -m unittest', 'Ran 2 tests in 0.01s\nOK (skipped=1)\n'),
            ('python -m unittest', 'Ran 3 tests in 0.01s\nOK (skipped=1, expected failures=1)\n'),
            ('python -m unittest', 'Ran 1 test in 0.01s\r\n\r\nOK\r\n'),
            ('python -m pytest tests', '=== 2 passed, 1 skipped, 1 warning in 0.02s ===\n'),
            ('python -m pytest tests', '=== 2 passed in 65.01s (0:01:05) ===\n'),
            ('node tests/test_example.js', 'PASS: actual assertions completed\n'),
            ('npm run test', 'PASS: actual assertions completed\n'),
        ]:
            with self.subTest(command=command, output=output):
                self.assertTrue(self.review.inspect([self.receipt(command, output=output)])['tests_passed'])

    def test_quoted_test_reports_cannot_supply_or_replace_receipts(self):
        reports = [
            'Write-Output "python -m unittest tests.test_a"',
            'python -c "print(\'python -m unittest tests.test_a\')"',
            'write a report quoting python -m unittest tests.test_a',
            'echo PASS: test; python -m unittest tests.test_a',
        ]
        failed = self.receipt('python -m unittest tests.test_a', exit_code=1)
        for command in reports:
            with self.subTest(command=command):
                fake = self.receipt(command)
                self.assertFalse(self.review.inspect([fake])['tests_passed'])
                self.assertFalse(self.review.inspect([failed, fake])['tests_passed'])

    def test_failed_rerun_without_summary_does_not_leave_old_success_valid(self):
        passed = self.receipt()
        failed = self.receipt(output='ImportError before test discovery', exit_code=1)
        self.assertFalse(self.review.inspect([passed, failed])['tests_passed'])
        self.assertTrue(self.review.inspect([failed, passed])['tests_passed'])

    def test_powershell_exit_suffix_preserves_the_full_test_scope(self):
        (self.copy/'brain/a.py').write_text('value = 2\n')
        failed = self.receipt(
            '& "G:\\tools\\python.exe" -m unittest tests.test_a tests.test_b; exit $LASTEXITCODE',
            output='Ran 2 tests in 0.01s\nFAILED (failures=1)\n', exit_code=1)
        partial = self.receipt('python -m unittest tests.test_a')
        self.assertFalse(self.review.inspect([failed, partial])['can_apply'])
        fixed = self.receipt('python -m unittest tests.test_a tests.test_b')
        self.assertTrue(self.review.inspect([failed, fixed])['can_apply'])

    def test_unknown_failed_test_wrapper_is_not_dropped(self):
        (self.copy/'brain/a.py').write_text('value = 2\n')
        passed = self.receipt('python -m unittest tests.test_a')
        for command in (
            'Set-Location workspace; python -m unittest tests.test_a tests.test_b',
            'python -m unittest tests.test_a tests.test_b && echo done',
        ):
            with self.subTest(command=command):
                failed = self.receipt(command, exit_code=1,
                    output='Ran 2 tests in 0.01s\nFAILED (failures=1)\n')
                self.assertFalse(self.review.inspect([failed, passed])['can_apply'])
                self.assertFalse(self.review.inspect([passed, failed])['can_apply'])
                # Even a matching opaque shell success cannot prove test scope.
                self.assertFalse(self.review.inspect([failed, self.receipt(command), passed])['can_apply'])

    def test_literal_status_suffix_cannot_mask_failure_or_narrow_its_scope(self):
        (self.copy/'brain/a.py').write_text('value = 2\n')
        for suffix in ('Write-Output Done', 'echo "all done"', "Write-Output 'finished'"):
            with self.subTest(suffix=suffix):
                broad = 'python -m unittest tests.test_a tests.test_b'
                failed = self.receipt(broad + '; ' + suffix, exit_code=0,
                    output='Ran 2 tests in 0.01s\nFAILED (failures=1)\nDone\n')
                partial = self.receipt('python -m unittest tests.test_a')
                self.assertFalse(self.review.inspect([failed, partial])['can_apply'])
                self.assertFalse(self.review.inspect([partial, failed])['can_apply'])
                recovered = self.receipt(broad, output='Ran 2 tests in 0.01s\nOK\n')
                self.assertTrue(self.review.inspect([failed, recovered])['can_apply'])

    def test_opaque_test_commands_block_even_when_shell_reports_success(self):
        (self.copy/'brain/a.py').write_text('value = 2\n')
        broad = 'python -m unittest tests.test_a tests.test_b'
        for command in (
            'Set-Location workspace; ' + broad,
            broad + '; Write-Output "$LASTEXITCODE"',
            broad + "; Write-Output '$(Get-Content report)'",
            broad + '; Write-Output Done | Out-Null',
            broad + '; Write-Output Done; Write-Output Done',
            broad + '; Write-Output `nOK',
            broad + '; Write-Output Done > report',
        ):
            with self.subTest(command=command):
                self.assertIsNone(test_scope(command))
                for output in ('Ran 2 tests in 0.01s\nFAILED (failures=1)\n',
                               'Ran 2 tests in 0.01s\nOK\n', ''):
                    opaque = self.receipt(command, exit_code=0, output=output)
                    self.assertFalse(self.review.inspect([opaque, self.receipt(broad)])['can_apply'])

    def test_literal_reports_are_not_tests_but_interpolated_and_shell_wrapped_commands_are(self):
        (self.copy/'brain/a.py').write_text('value = 2\n')
        invocation = 'python -m unittest tests.test_a tests.test_b'
        for command in (
            f"Set-Content -LiteralPath 'report.txt' -Value '{invocation}'",
            f'Add-Content report.txt "{invocation}"',
            f"Out-File -FilePath report.txt -InputObject '{invocation}'",
            f"Write-Output '{invocation}'",
            f'echo "{invocation}"',
            f'write a report quoting {invocation}',
        ):
            with self.subTest(report=command):
                self.assertFalse(is_test_command(command))
                # Old host receipts may still carry the former broad regex flag.
                legacy = self.receipt(command, output='saved report')
                self.assertTrue(self.review.inspect([self.receipt(invocation), legacy])['can_apply'])
        for command in (
            f'Set-Content report.txt $({invocation})',
            f'Set-Content report.txt "$({invocation})"',
            f'Set-Content report.txt ({invocation})',
            f'Set-Content report.txt {{ {invocation} }}',
            f'Set-Content report.txt done; {invocation}',
            f'Write-Output done | {invocation}',
            f'pwsh -Command "{invocation}"',
            f'cmd /c "{invocation}"',
            f'python -c "print(\'{invocation}\')"',
        ):
            with self.subTest(opaque=command):
                self.assertTrue(is_test_command(command))
                self.assertIsNone(test_scope(command))
                receipt = self.receipt(command, output='saved report', exit_code=0)
                receipt['test'] = is_test_command(command)
                self.assertFalse(self.review.inspect([receipt, self.receipt(invocation)])['can_apply'])

    def test_node_and_npm_status_suffix_cannot_supply_test_evidence(self):
        (self.copy/'brain/a.py').write_text('value = 2\n')
        for runner in ('node tests/foo.js', 'npm test', 'npm run test'):
            for suffix in ('Write-Output Done', 'echo "finished"'):
                with self.subTest(runner=runner, suffix=suffix):
                    command = runner + '; ' + suffix
                    self.assertIsNone(test_scope(command))
                    masked = self.receipt(command, output='PASS: first assertion\n'
                        'AssertionError: second assertion failed\nDone\n', exit_code=0)
                    self.assertFalse(self.review.inspect([masked, self.receipt()])['can_apply'])

    @unittest.skipUnless(os.name == 'nt' and shutil.which('pwsh') and shutil.which('node'),
                         'requires PowerShell and Node')
    def test_real_node_pass_then_throw_cannot_be_masked_by_powershell_suffix(self):
        tests = self.copy/'tests'
        tests.mkdir()
        (tests/'fail_after_pass.js').write_text(
            "console.log('PASS: first assertion');\n"
            "require('node:assert/strict').equal(1, 2);\n")
        (self.copy/'brain/a.py').write_text('value = 2\n')
        direct_command = 'node tests/fail_after_pass.js'
        command = direct_command + '; Write-Output Done'
        version = self.review.version()
        masked_result = subprocess.run(['pwsh', '-NoProfile', '-Command', command],
            cwd=self.copy, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, timeout=20, check=False,
            creationflags=subprocess.CREATE_NO_WINDOW)
        masked = self.receipt(command, output=masked_result.stdout.decode('utf-8'),
                              exit_code=masked_result.returncode)
        masked['source_before'] = version
        self.assertEqual(masked['exit_code'], 0)
        self.assertIn('PASS: first assertion', masked['output'])
        self.assertIn('AssertionError', masked['output'])
        self.assertFalse(self.review.inspect([masked, self.receipt()])['can_apply'])
        direct_result = subprocess.run([shutil.which('node'), 'tests/fail_after_pass.js'],
            cwd=self.copy, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, timeout=20, check=False,
            creationflags=subprocess.CREATE_NO_WINDOW)
        direct = self.receipt(direct_command, output=direct_result.stdout.decode('utf-8'),
                              exit_code=direct_result.returncode)
        direct['source_before'] = version
        self.assertNotEqual(direct['exit_code'], 0)
        self.assertIsNotNone(test_scope(direct_command))
        self.assertFalse(self.review.inspect([direct])['can_apply'])

    @unittest.skipUnless(os.name == 'nt' and shutil.which('pwsh'), 'requires PowerShell')
    def test_real_powershell_masked_failure_then_full_recovery(self):
        tests = self.copy/'tests'
        tests.mkdir()
        (tests/'__init__.py').write_text('')
        passing = 'import unittest\nclass T(unittest.TestCase):\n def test_value(self): self.assertEqual(1,1)\n'
        (tests/'test_a.py').write_text(passing)
        (tests/'test_b.py').write_text(passing.replace('1,1', '1,2'))
        (self.copy/'brain/a.py').write_text('value = 2\n')
        command = "& '" + sys.executable.replace("'", "''") + (
            "' -B -m unittest tests.test_a tests.test_b; Write-Output Done")

        def run():
            before = self.review.version()
            result = subprocess.run(['pwsh', '-NoProfile', '-Command', command],
                cwd=self.copy, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, timeout=20, check=False,
                creationflags=subprocess.CREATE_NO_WINDOW)
            receipt = self.receipt(command, output=result.stdout.decode('utf-8'),
                                   exit_code=result.returncode)
            receipt['source_before'] = before
            return receipt

        failed = run()
        self.assertEqual(failed['exit_code'], 0)
        self.assertIn('FAILED (failures=1)', failed['output'])
        partial = self.run_unittest('tests.test_a')
        self.assertEqual(partial['exit_code'], 0)
        self.assertFalse(self.review.inspect([failed, partial])['can_apply'])
        (tests/'test_b.py').write_text(passing)
        recovered = run()
        self.assertTrue(self.review.inspect([failed, recovered])['can_apply'])
        (self.copy/'data').mkdir()
        report_command = ("Set-Content -LiteralPath 'data/report.txt' -Value "
                          "'Tests: python -m unittest tests.test_a tests.test_b'")
        before = self.review.version()
        report_result = subprocess.run(['pwsh', '-NoProfile', '-Command', report_command],
            cwd=self.copy, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, timeout=20, check=False,
            creationflags=subprocess.CREATE_NO_WINDOW)
        report_receipt = self.receipt(report_command, output=report_result.stdout.decode('utf-8'),
                                      exit_code=report_result.returncode)
        report_receipt.update(test=is_test_command(report_command), source_before=before)
        self.assertEqual(report_result.returncode, 0)
        self.assertIn('python -m unittest', (self.copy/'data/report.txt').read_text('utf-8'))
        self.assertFalse(report_receipt['test'])
        self.assertTrue(self.review.inspect([failed, recovered, report_receipt])['can_apply'])

if __name__ == '__main__':
    unittest.main()
