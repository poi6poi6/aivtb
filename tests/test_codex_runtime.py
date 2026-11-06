"""Codex discovery with isolated paths; no real login, CLI or subprocess runs."""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from brain.agent_rpc import codex_client
from brain.codex_runtime import CodexNotFoundError, resolve_codex_command


class CodexDiscoveryFixture:
    def create_fixture(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.local = self.root / 'Local'
        self.desktop = self.local / 'OpenAI' / 'Codex' / 'bin'
        self.environment = {'LOCALAPPDATA': str(self.local), 'PATH': ''}
        # Patch this module's os binding, not global os.name/pathlib behavior,
        # so the Windows discovery rules are testable on every test host.
        fake_os = SimpleNamespace(name='nt', environ=self.environment)
        module_os = patch('brain.codex_runtime.os', fake_os)
        module_os.start()
        self.addCleanup(module_os.stop)
        which = patch('brain.codex_runtime.shutil.which', return_value=None)
        self.which = which.start()
        self.addCleanup(which.stop)

    def executable(self, relative='codex.exe', *, stamp=1_800_000_000_000_000_000):
        path = self.desktop / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'inert test fixture, not executable')
        os.utime(path, ns=(stamp, stamp))
        return path


class CodexRuntimeTests(CodexDiscoveryFixture, unittest.TestCase):
    def setUp(self):
        self.create_fixture()

    def test_default_names_find_desktop_executable_without_path(self):
        path = self.executable()
        for command in ('codex', 'codex.exe', 'CODEX.EXE'):
            with self.subTest(command=command):
                self.assertEqual(resolve_codex_command(command), str(path))
        self.assertEqual(self.environment, {'LOCALAPPDATA': str(self.local), 'PATH': ''})

    def test_newest_version_wins_and_updates_are_not_cached(self):
        direct = self.executable(stamp=1_000_000_000)
        old = self.executable('version-99/codex.exe', stamp=2_000_000_000)
        self.assertEqual(resolve_codex_command(), str(old))
        new = self.executable('version-1/codex.exe', stamp=3_000_000_000)
        self.assertEqual(resolve_codex_command(), str(new), 'mtime matters, not folder lexical order')
        new.unlink()
        self.assertEqual(resolve_codex_command(), str(old), 'removed app versions are not cached')
        old.unlink()
        self.assertEqual(resolve_codex_command(), str(direct))

    def test_path_has_priority_and_result_is_absolute_for_temp_working_directory(self):
        self.executable()
        self.which.return_value = 'fixture-path/codex.exe'
        self.assertEqual(resolve_codex_command(), str(Path('fixture-path/codex.exe').absolute()))
        self.which.assert_called_once_with('codex')

    def test_explicit_paths_and_aliases_never_fall_back_to_desktop_installation(self):
        self.executable()
        explicit = str(self.root / 'configured-missing' / 'codex.exe')
        for command in (explicit, './codex.exe', 'other-codex', 'codex.cmd', ' codex '):
            with self.subTest(command=command):
                self.assertIsNone(resolve_codex_command(command))
        self.which.return_value = explicit
        self.assertEqual(resolve_codex_command(explicit), explicit)
        self.which.return_value = str(self.root / 'alias.exe')
        self.assertEqual(resolve_codex_command('approved-alias'), str(self.root / 'alias.exe'))

    def test_missing_localappdata_uses_only_the_current_home_appdata_location(self):
        self.environment.pop('LOCALAPPDATA')
        home = self.root / 'fixture-home'
        self.desktop = home / 'AppData' / 'Local' / 'OpenAI' / 'Codex' / 'bin'
        executable = self.executable()
        with patch('brain.codex_runtime.Path.home', return_value=home):
            self.assertEqual(resolve_codex_command(), str(executable))

    def test_invalid_localappdata_is_rejected_without_home_fallback(self):
        self.executable()
        for location in ('relative-folder', '  ', str(self.local / '\x00invalid')):
            with self.subTest(location=repr(location)), patch('brain.codex_runtime.Path.home') as home:
                self.environment['LOCALAPPDATA'] = location
                self.assertIsNone(resolve_codex_command())
                home.assert_not_called()

    def test_missing_installation_and_invalid_commands_do_not_run_anything(self):
        self.assertIsNone(resolve_codex_command())
        self.which.reset_mock()
        for command in (None, False, 7, [], '', '   '):
            with self.subTest(command=command):
                self.assertIsNone(resolve_codex_command(command))
        self.which.assert_not_called()

    def test_discovery_is_bounded_to_native_executables_in_known_installation(self):
        nested = self.executable('nested/deeper/codex.exe')
        (self.desktop / 'codex.cmd').write_text('inert', encoding='utf-8')
        (self.local / 'codex.exe').write_text('inert', encoding='utf-8')
        self.assertIsNone(resolve_codex_command(), 'no arbitrary or recursive executable search')
        self.assertTrue(nested.is_file())
        with patch('brain.codex_runtime.os', SimpleNamespace(name='posix', environ=self.environment)):
            self.executable()
            self.assertIsNone(resolve_codex_command(), 'non-Windows hosts use PATH only')

    def test_unreadable_desktop_directory_fails_without_exposing_error(self):
        with patch('brain.codex_runtime.Path.glob', side_effect=PermissionError('fixture-private-path')):
            self.assertIsNone(resolve_codex_command())


class CodexClientDiscoveryTests(CodexDiscoveryFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.create_fixture()
        self.rpc = SimpleNamespace(start=AsyncMock(), call=AsyncMock(return_value={}),
                                   send=AsyncMock(), close=AsyncMock())

    async def test_rpc_starts_with_discovered_absolute_executable(self):
        executable = self.executable('desktop-update/codex.exe')
        cwd = self.root / 'separate-image-workdir'
        with patch('brain.agent_rpc.JsonProcess', return_value=self.rpc) as process, patch(
                'brain.agent_rpc.subscription_env', return_value={'PATH': 'fixture'}) as environment:
            result = await codex_client('codex', cwd, config_overrides={'features.shell_tool': False})
        self.assertIs(result, self.rpc)
        command, actual_cwd, env = process.call_args.args
        self.assertEqual(command[0], str(executable))
        self.assertTrue(Path(command[0]).is_absolute())
        self.assertEqual(command[1:4], ['app-server', '--listen', 'stdio://'])
        self.assertIn('features.shell_tool=false', command)
        self.assertEqual(actual_cwd, cwd)
        self.assertEqual(env, {'PATH': 'fixture'})
        environment.assert_called_once()
        self.rpc.start.assert_awaited_once()
        self.assertEqual(self.rpc.call.await_args.args[0], 'initialize')
        self.rpc.send.assert_awaited_once_with({'method': 'initialized'})
        self.rpc.close.assert_not_awaited()

    async def test_missing_client_raises_before_environment_or_process_creation(self):
        with patch('brain.agent_rpc.JsonProcess') as process, patch('brain.agent_rpc.subscription_env') as environment:
            with self.assertRaises(CodexNotFoundError):
                await codex_client('codex', self.root)
        process.assert_not_called()
        environment.assert_not_called()

    async def test_failed_initialize_closes_only_the_owned_mock_process(self):
        self.executable()
        self.rpc.call.side_effect = RuntimeError('fixture protocol failure')
        with patch('brain.agent_rpc.JsonProcess', return_value=self.rpc), patch(
                'brain.agent_rpc.subscription_env', return_value={}):
            with self.assertRaisesRegex(RuntimeError, 'fixture protocol failure'):
                await codex_client('codex', self.root)
        self.rpc.close.assert_awaited_once()
        self.rpc.send.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
