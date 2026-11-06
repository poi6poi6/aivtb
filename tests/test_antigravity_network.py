from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from brain.antigravity_chat import _failure, _subscription_env
from brain.antigravity_network import with_windows_proxy
from tools import login_argon


class AntigravityNetworkTests(unittest.TestCase):
    def apply_proxy(self, env, *, enabled=True, server='127.0.0.1:7890'):
        with patch('brain.antigravity_network._windows_proxy_settings',
                   return_value=(enabled, server)) as settings:
            result = with_windows_proxy(env)
        settings.assert_called_once_with()
        return result

    def test_single_address_sets_both_protocols_without_mutating_input_or_environment(self):
        input_env = {'PATH': 'example-path', 'NO_PROXY': 'localhost,127.0.0.1'}
        original = input_env.copy()
        with patch.dict(os.environ, {'HTTP_PROXY': 'http://environment-only:9000'}, clear=True):
            process_environment = dict(os.environ)
            result = self.apply_proxy(input_env)
            self.assertEqual(dict(os.environ), process_environment)
        self.assertIsNot(result, input_env)
        self.assertEqual(input_env, original)
        self.assertEqual(result, {
            **original,
            'HTTP_PROXY': 'http://127.0.0.1:7890',
            'HTTPS_PROXY': 'http://127.0.0.1:7890',
        })

    def test_explicit_proxy_always_wins_and_does_not_read_windows_settings(self):
        for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY',
                    'http_proxy', 'https_proxy', 'all_proxy', 'HtTpS_PrOxY'):
            with self.subTest(key=key):
                original = {key: 'http://127.0.0.1:9', 'PATH': 'example-path'}
                with patch('brain.antigravity_network._windows_proxy_settings') as settings:
                    result = with_windows_proxy(original)
                settings.assert_not_called()
                self.assertEqual(result, original)
                self.assertIsNot(result, original)

    def test_explicit_proxy_is_preserved_even_when_value_has_credentials_or_other_scheme(self):
        # User-supplied environment is authoritative; validation applies only to
        # values newly copied from Windows settings, never to an existing proxy.
        for value in ('socks5://127.0.0.1:1080', 'http://test-user:placeholder@localhost:7890'):
            with self.subTest(value=value):
                original = {'HTTPS_PROXY': value}
                with patch('brain.antigravity_network._windows_proxy_settings') as settings:
                    self.assertEqual(with_windows_proxy(original), original)
                settings.assert_not_called()

    def test_explicit_empty_proxy_values_also_prevent_windows_proxy(self):
        for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'https_proxy'):
            with self.subTest(key=key):
                original = {key: '', 'PATH': 'example-path'}
                with patch('brain.antigravity_network._windows_proxy_settings') as settings:
                    result = with_windows_proxy(original)
                settings.assert_not_called()
                self.assertEqual(result, original)
                self.assertIsNot(result, original)

    def test_protocol_mapping_uses_only_explicit_protocols(self):
        cases = (
            ('http=localhost:7890;https=localhost:7891', {
                'HTTP_PROXY': 'http://localhost:7890',
                'HTTPS_PROXY': 'http://localhost:7891',
            }),
            ('http=localhost:7890', {'HTTP_PROXY': 'http://localhost:7890'}),
            ('https=localhost:7891', {'HTTPS_PROXY': 'http://localhost:7891'}),
            ('socks=localhost:1080', {}),
            ('ftp=localhost:2121;socks=localhost:1080', {}),
            ('http=localhost:7890;socks=localhost:1080', {
                'HTTP_PROXY': 'http://localhost:7890',
            }),
            ('https=https://proxy.example:8443', {
                'HTTPS_PROXY': 'https://proxy.example:8443',
            }),
        )
        for server, expected in cases:
            with self.subTest(server=server):
                self.assertEqual(self.apply_proxy({}, server=server), expected)

    def test_http_https_ipv4_and_bracketed_ipv6_addresses_are_valid(self):
        cases = (
            ('localhost:7890', 'http://localhost:7890'),
            ('http://127.0.0.1:7890', 'http://127.0.0.1:7890'),
            ('https://proxy.example:8443', 'https://proxy.example:8443'),
            ('[::1]:7890', 'http://[::1]:7890'),
        )
        for server, expected in cases:
            with self.subTest(server=server):
                self.assertEqual(self.apply_proxy({}, server=server), {
                    'HTTP_PROXY': expected, 'HTTPS_PROXY': expected,
                })

    def test_disabled_empty_or_invalid_proxy_does_not_change_environment(self):
        original = {'PATH': 'example-path', 'NO_PROXY': 'localhost'}
        self.assertEqual(self.apply_proxy(original, enabled=False), original)
        invalid_servers = (
            '', ' ', 'localhost', 'http://localhost', ':7890',
            'http://:7890', 'http://localhost:0', 'http://localhost:65536',
            'http://localhost:-1', 'http://localhost:not-a-port',
            'http://local host:7890', 'http://localhost:7890\n.evil',
            'socks5://localhost:1080', 'file://localhost:7890',
            'http://test-user:placeholder@localhost:7890',
            'http://test-user@localhost:7890',
            'http://localhost:7890/', 'http://localhost:7890/path',
            'http://localhost:7890?private=value', 'http://localhost:7890#private',
            'http://[invalid-ipv6]:7890', '::1:7890',
        )
        for server in invalid_servers:
            with self.subTest(server=server):
                self.assertEqual(self.apply_proxy(original, server=server), original)
        self.assertEqual(original, {'PATH': 'example-path', 'NO_PROXY': 'localhost'})

    def test_invalid_protocol_entry_does_not_leak_credentials_or_discard_valid_other_protocol(self):
        result = self.apply_proxy({}, server=(
            'http=http://test-user:placeholder@localhost:7890;https=localhost:7891'))
        self.assertEqual(result, {'HTTPS_PROXY': 'http://localhost:7891'})

    def test_subscription_adapter_preserves_sandbox_port_9_proxy(self):
        original = {
            'HTTP_PROXY': 'http://127.0.0.1:9',
            'HTTPS_PROXY': 'http://127.0.0.1:9',
            'ALL_PROXY': 'http://127.0.0.1:9',
            'NO_PROXY': 'localhost,127.0.0.1',
            'GEMINI_API_KEY': 'test-only-placeholder',
        }
        with patch.dict(os.environ, original, clear=True):
            with patch('brain.antigravity_network._windows_proxy_settings') as settings:
                result = _subscription_env()
            settings.assert_not_called()
            self.assertEqual(dict(os.environ), original)
        for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY'):
            self.assertEqual(result[key], original[key])
        self.assertNotIn('GEMINI_API_KEY', result)
        self.assertEqual(result['AGY_CLI_DISABLE_AUTO_UPDATE'], 'true')

    def test_subscription_adapter_uses_the_shared_windows_proxy_settings(self):
        with patch.dict(os.environ, {'NO_PROXY': 'localhost'}, clear=True):
            with patch('brain.antigravity_network._windows_proxy_settings',
                       return_value=(True, 'localhost:7890')) as settings:
                result = _subscription_env()
        settings.assert_called_once_with()
        self.assertEqual(result['HTTP_PROXY'], 'http://localhost:7890')
        self.assertEqual(result['HTTPS_PROXY'], 'http://localhost:7890')
        self.assertEqual(result['NO_PROXY'], 'localhost')

    def test_oauth_connection_timeout_reports_network_without_exposing_upstream_details(self):
        detail = ('token exchange failed: Post "https://oauth2.googleapis.com/token": '
                  'dial tcp [2001:db8::1]:443: connectex: A connection attempt failed '
                  'because the connected party did not properly respond after a period '
                  'of time; authorization_code=TEST_ONLY_PRIVATE_VALUE')
        message = str(_failure(detail))
        self.assertIn('无法连接 Google', message)
        for private_detail in ('oauth2.googleapis.com', '2001:db8', 'authorization_code',
                               'TEST_ONLY_PRIVATE_VALUE'):
            self.assertNotIn(private_detail, message)

    def test_interactive_login_inherits_terminal_and_uses_shared_environment(self):
        shared_env = {'HTTPS_PROXY': 'http://127.0.0.1:9',
                      'AGY_CLI_DISABLE_AUTO_UPDATE': 'true'}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / 'data/argon-runtime/bin/agy.exe'
            executable.parent.mkdir(parents=True)
            executable.touch()
            with patch.object(login_argon, 'ROOT', root):
                with patch.object(login_argon, '_subscription_env', return_value=shared_env) as env:
                    with patch.object(login_argon.subprocess, 'call', return_value=7) as call:
                        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                            result = login_argon.main()
            self.assertEqual(result, 7)
            env.assert_called_once_with()
            call.assert_called_once_with([str(executable)],
                                         cwd=root / 'data/argon-runtime/chat', env=shared_env)
            self.assertIs(call.call_args.kwargs['env'], shared_env)
            self.assertTrue((root / 'data/argon-runtime/chat').is_dir())
            for option in ('shell', 'stdin', 'stdout', 'stderr', 'input', 'capture_output'):
                self.assertNotIn(option, call.call_args.kwargs)


if __name__ == '__main__':
    unittest.main()
