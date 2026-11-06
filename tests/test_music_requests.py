import tempfile
import unittest
from pathlib import Path
from brain.music_requests import MusicRequests, parse_music_request


class MusicRequestTests(unittest.TestCase):
    def test_commands_do_not_capture_regular_chat_or_local_playback(self):
        self.assertEqual(parse_music_request('唱歌 星间飞行'), ('cover', '星间飞行'))
        self.assertEqual(parse_music_request('原创：关于下雨的爵士歌'), ('original', '关于下雨的爵士歌'))
        for text in ('点歌 星间飞行', '你会唱歌吗', '原创音乐很好听'):
            self.assertIsNone(parse_music_request(text))

    def test_pending_setup_is_persisted_deduplicated_and_cancellable(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'requests.json'
            requests = MusicRequests(path)
            requests.submit('cover', '星间飞行', 'one')
            self.assertEqual(requests.items[0]['state'], 'needs_setup')
            self.assertIn('重复', requests.submit('cover', '星间飞行', 'two'))
            self.assertIn('30秒', requests.submit('original', '雨天', 'one'))
            requests.submit('original', '雨天', 'two')
            restored = MusicRequests(path)
            self.assertEqual(len(restored.items), 2)
            self.assertTrue(restored.cancel(restored.items[0]['id']))
            self.assertEqual(MusicRequests(path).items[0]['state'], 'cancelled')
            self.assertFalse(restored.cancel('missing'))

    def test_input_and_queue_limits(self):
        with tempfile.TemporaryDirectory() as folder:
            requests = MusicRequests(Path(folder) / 'requests.json')
            requests.submit('cover', 'x' * 201, 'one')
            self.assertFalse(requests.items)
            for i in range(20):
                requests.submit('original', str(i), str(i))
            self.assertIn('已满', requests.submit('original', 'extra', 'extra'))

    def test_full_history_keeps_old_unfinished_request_after_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'requests.json'
            requests = MusicRequests(path)
            requests.submit('cover', '仍在等待原曲', 'pending-viewer')
            pending = requests.items[0]
            for i in range(99):
                requests.submit('original', f'完成的歌曲{i}', f'viewer-{i}')
                requests.items[-1]['state'] = 'ready'
            oldest_finished_id = requests.items[1]['id']
            self.assertIn('已记录', requests.submit('original', '新歌曲', 'new-viewer'))
            self.assertEqual(len(requests.items), 100)
            self.assertIs(requests.items[0], pending)
            self.assertNotIn(oldest_finished_id, {item['id'] for item in requests.items})
            restored = MusicRequests(path)
            self.assertEqual(restored.items[0], pending)
            self.assertIn('重复', restored.submit('cover', '仍在等待原曲', 'another-viewer'))
            self.assertTrue(restored.cancel(pending['id']))

    def test_history_pruning_keeps_every_inflight_state_and_worker_reference(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'requests.json'
            requests = MusicRequests(path)
            active = []
            states = ('needs_setup', 'waiting_source', 'processing', 'awaiting_draft',
                      'drafting', 'waiting_suno', 'draft_failed')
            for i, state in enumerate(states):
                requests.submit('original', f'进行中的歌曲{i}', f'active-{i}')
                job = requests.items[-1]
                job['state'] = state
                active.append(job)
            for i in range(100):
                requests.submit('original', f'历史歌曲{i}', f'history-{i}')
                requests.items[-1]['state'] = ('ready', 'cancelled', 'failed')[i % 3]
            requests.save()
            self.assertEqual(len(requests.items), 100)
            for job, state in zip(active, states):
                self.assertIs(next(item for item in requests.items if item['id'] == job['id']), job)
                self.assertEqual(job['state'], state)
            # The renderer's original object must still be the persisted job.
            active[2].update(state='ready', song={'id': 'fixture'})
            requests.save()
            restored = MusicRequests(path)
            rendered = next(item for item in restored.items if item['id'] == active[2]['id'])
            self.assertEqual(rendered['song'], {'id': 'fixture'})
