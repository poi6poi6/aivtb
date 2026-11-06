import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
import yaml

from tools.configure_tts import check_service, configure, fish_url, save_settings


SOURCE = '''# keep header
room_id: 123  # keep number
tts:
  backend: indextts2  # keep selected backend comment
  out_dir: data/audio
  indextts2_ref_audio_path: 'V:/existing.wav' # keep index voice
  fish_speech_url: http://127.0.0.1:8081 # keep url comment
  fish_speech_prompt_text: |- # keep text block
    old transcript
# next section comment
webui:
  port: 8080
'''


class TtsSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'config/settings.yaml'
        self.path.parent.mkdir()
        self.path.write_bytes(b'\xef\xbb\xbf' + SOURCE.replace('\n', '\r\n').encode('utf-8'))

    def test_atomic_edit_preserves_other_values_comments_bom_and_backup(self):
        original = self.path.read_bytes()
        before = yaml.safe_load(original)
        updates = {'backend': 'fish_speech', 'fish_speech_reference_id': '中文音色 #1',
                   'fish_speech_prompt_text': '', 'fish_speech_ref_audio_path': ''}
        backup = save_settings(self.path, updates)
        content = self.path.read_bytes()
        self.assertTrue(content.startswith(b'\xef\xbb\xbf'))
        self.assertEqual(backup.read_bytes(), original)
        expected = {**before, 'tts': {**before['tts'], **updates}}
        self.assertEqual(yaml.safe_load(content), expected)
        for comment in ('# keep header', '# keep number', '# keep selected backend comment',
                        '# keep index voice', '# keep url comment', '# keep text block', '# next section comment'):
            self.assertIn(comment, content.decode('utf-8-sig'))
        self.assertNotIn(b'\n', content.replace(b'\r\n', b''))

    def test_only_backend_changes_for_existing_providers(self):
        before = yaml.safe_load(self.path.read_bytes())
        save_settings(self.path, {'backend': 'gpt_sovits'})
        after = yaml.safe_load(self.path.read_bytes())
        before['tts']['backend'] = 'gpt_sovits'
        self.assertEqual(after, before)

    def test_failed_atomic_replace_keeps_original_and_removes_temporary_file(self):
        original = self.path.read_bytes()
        with patch('tools.configure_tts.os.replace', side_effect=OSError('fixture replace failure')):
            with self.assertRaises(OSError):
                save_settings(self.path, {'backend': 'mock'})
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.glob('.tts-*.tmp')), [])
        backups = list(self.path.parent.glob('settings.yaml.tts-*.bak'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)

    def test_rejects_bad_url_and_missing_reference_pair_without_writing(self):
        original = self.path.read_bytes()
        for updates in ({'backend': 'fish_speech', 'fish_speech_url': 'http://localhost:8081/v1/tts'},
                        {'backend': 'fish_speech', 'fish_speech_ref_audio_path': 'missing.wav'},
                        {'backend': 'arbitrary'}, {'backend': 'mock', 'room_id': 888}):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                save_settings(self.path, updates)
            self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.glob('*.bak')), [])

    def test_relative_reference_path_is_resolved_from_project_root(self):
        reference = self.root / 'data/reference.wav'
        reference.parent.mkdir()
        reference.write_bytes(b'fake')
        save_settings(self.path, {'backend': 'fish_speech', 'fish_speech_ref_audio_path': 'data/reference.wav',
                                  'fish_speech_prompt_text': '准确转写'})
        self.assertEqual(yaml.safe_load(self.path.read_bytes())['tts']['fish_speech_ref_audio_path'], 'data/reference.wav')

    def test_duplicate_and_aliased_config_refuses_ambiguous_edit(self):
        for source in ('tts:\n  backend: mock\n  backend: indextts2\n',
                       'other: &voice mock\ntts:\n  backend: *voice\n'):
            self.path.write_text(source, 'utf-8')
            with self.assertRaises(ValueError):
                save_settings(self.path, {'backend': 'gpt_sovits'})
            self.assertEqual(self.path.read_text('utf-8'), source)

    def test_interactive_fish_id_save_and_cancel_have_no_network_effect(self):
        original = self.path.read_bytes()
        answers = iter(['1', '', '2', 'avatar', 'n'])
        self.assertFalse(configure(self.path, ask=lambda _: next(answers), tell=lambda _: None))
        self.assertEqual(self.path.read_bytes(), original)
        answers = iter(['1', '', '2', 'avatar', 'y'])
        with patch('tools.configure_tts.check_service', side_effect=AssertionError('must not connect')):
            self.assertTrue(configure(self.path, ask=lambda _: next(answers), tell=lambda _: None))
        cfg = yaml.safe_load(self.path.read_bytes())['tts']
        self.assertEqual(cfg['backend'], 'fish_speech')
        self.assertEqual(cfg['fish_speech_reference_id'], 'avatar')
        self.assertEqual(cfg['fish_speech_prompt_text'], '')

    def test_url_validation(self):
        self.assertEqual(fish_url(' http://127.0.0.1:8081/ '), 'http://127.0.0.1:8081')
        for url in ('http://localhost:notaport', 'https://user:secret@example.com',
                    'file:///tmp/tts', 'http://localhost/v1/tts', 'https://example.com?a=1'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                fish_url(url)

    def test_health_check_only_reads_health_and_supports_auth_without_synthesis(self):
        seen = []
        def transport(request):
            seen.append(request)
            return httpx.Response(200, json={'status': 'ok'})
        with httpx.Client(transport=httpx.MockTransport(transport)) as client:
            with patch.dict(os.environ, {'FISH_FIXTURE_TOKEN': 'secret-fixture'}):
                self.assertEqual(check_service({'fish_speech_url': 'http://127.0.0.1:8081',
                                                'fish_speech_api_key_env': 'FISH_FIXTURE_TOKEN'}, client=client),
                                 'http://127.0.0.1:8081')
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].method, 'GET')
        self.assertEqual(seen[0].url.path, '/v1/health')
        self.assertEqual(seen[0].headers['Authorization'], 'Bearer secret-fixture')

    def test_health_failures_do_not_echo_response_or_token(self):
        for status, payload in ((401, {'error': 'secret-fixture'}), (503, {'error': 'secret-fixture'}),
                                (200, {'scope': 'main'}), (200, [])):
            with self.subTest(status=status, payload=payload):
                with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(status, json=payload))) as client:
                    with self.assertRaises(ValueError) as caught:
                        check_service({}, client=client)
                self.assertNotIn('secret-fixture', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
