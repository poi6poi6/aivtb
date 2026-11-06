import json
import unittest
from unittest.mock import patch

from brain.task_progress import HISTORY_LIMIT, SEEN_LIMIT, TaskProgress, command_is_test


class Host(TaskProgress):
    def __init__(self):
        self.saves = 0

    def save(self, *jobs):
        self.saves += 1


class TaskProgressTests(unittest.TestCase):
    def setUp(self):
        self.host = Host()
        self.job = {'state': 'running', 'revision': 3}

    def event(self, id, type='commandExecution', **values):
        return {'id': id, 'type': type, **values}

    def test_concurrent_stale_completion_cannot_replace_active_phase(self):
        old = self.event('old', 'webSearch')
        current = self.event('current', command='python -m unittest tests.test_x')
        self.host.observe_progress(self.job, 'item/started', old)
        self.host.observe_progress(self.job, 'item/started', current)
        self.host.observe_progress(self.job, 'item/completed', old)
        self.assertEqual(self.job['progress']['stage'], 'testing')
        self.host.observe_progress(self.job, 'item/completed', {**current, 'exitCode': 0})
        self.assertEqual(self.job['progress']['stage'], 'awaiting_receipt')
        self.assertEqual(self.job['state'], 'running')
        self.assertEqual(self.job['revision'], 3)

    def test_repeated_or_unmatched_events_do_not_reset_last_progress_time(self):
        item = self.event('first', 'fileChange')
        with patch('brain.task_progress.time.time', return_value=10):
            self.host.observe_progress(self.job, 'item/started', item)
        with patch('brain.task_progress.time.time', return_value=20):
            self.assertFalse(self.host.observe_progress(self.job, 'item/started', item))
            self.assertFalse(self.host.observe_progress(self.job, 'item/completed', self.event('unknown')))
        self.assertEqual(self.job['progress']['last_event_at'], 10)
        self.host.observe_progress(self.job, 'item/completed', item)
        count = self.host.saves
        self.assertFalse(self.host.observe_progress(self.job, 'item/completed', item))
        self.assertFalse(self.host.observe_progress(self.job, 'item/started', item))
        self.assertEqual(self.host.saves, count)

    def test_history_is_bounded_and_raw_tool_data_never_enters_progress(self):
        secret = 'sk-private-bearer-cookie'
        for i in range(SEEN_LIMIT + 4):
            item = self.event(secret + str(i), 'mcpToolCall', arguments={'cookie': secret}, result=secret, error=secret, command=secret)
            self.host.observe_progress(self.job, 'item/started', item)
            self.host.observe_progress(self.job, 'item/completed', item)
        progress = self.job['progress']
        self.assertEqual(len(progress['history']), HISTORY_LIMIT)
        self.assertEqual(len(progress['seen']), SEEN_LIMIT)
        self.assertEqual(progress['active'], [])
        self.assertNotIn(secret, json.dumps(progress))
        self.assertEqual(progress['history'][-1]['event'], 'failed')

    def test_only_real_supported_events_advance_and_error_exit_code_is_preserved(self):
        self.assertFalse(self.host.observe_progress(self.job, 'item/started', self.event('chat', 'agentMessage', text='I finished everything')))
        self.assertFalse(self.host.observe_progress(self.job, 'item/updated', self.event('x')))
        item = self.event('test', command='node tests/test_x.js')
        self.host.observe_progress(self.job, 'item/started', item)
        self.host.observe_progress(self.job, 'item/completed', {**item, 'exitCode': 2, 'aggregatedOutput': 'private traceback'})
        last = self.job['progress']['history'][-1]
        self.assertEqual((last['stage'], last['event'], last['exit_code']), ('testing', 'failed', 2))
        self.assertNotIn('private', json.dumps(last))

    def test_approval_preserves_active_work_and_terminal_drops_it(self):
        item = self.event('work', 'imageGeneration')
        self.host.observe_progress(self.job, 'item/started', item)
        self.host.progress_state(self.job, 'waiting_approval')
        self.job['state'] = 'waiting_approval'
        self.assertEqual(self.job['progress']['stage'], 'waiting_approval')
        self.host.progress_state(self.job, 'running')
        self.job['state'] = 'running'
        self.assertEqual(self.job['progress']['stage'], 'image')
        self.host.progress_state(self.job, 'cancelled')
        self.job['state'] = 'cancelled'
        self.assertEqual(self.job['progress']['active'], [])
        self.assertFalse(self.host.observe_progress(self.job, 'item/completed', item))
        self.assertFalse(self.host.record_progress(self.job, 'validating'))
        self.assertEqual(self.job['progress']['stage'], 'cancelled')

    def test_host_verification_is_not_completion_and_failure_is_categorized(self):
        self.assertTrue(self.host.record_progress(self.job, 'executing'))
        self.assertFalse(self.host.record_progress(self.job, 'executing'))
        self.assertFalse(self.host.record_progress(self.job, 'succeeded'))
        self.assertTrue(self.host.record_progress(self.job, 'validating'))
        self.assertEqual(self.job['state'], 'running')
        self.host.progress_state(self.job, 'failed', error_category='timeout')
        self.assertEqual(self.job['progress']['history'][-1]['error'], '执行或连接超时')
        self.assertEqual(self.job['progress']['active'], [])

    def test_direct_test_command_recognition_ignores_quoted_reports(self):
        for command in ('python -m unittest tests.test_x', '& "G:\\folder with spaces\\python.exe" -m pytest tests/test_x.py', 'node tests/test_x.js', 'npm run test', 'python -X utf8 -m unittest discover'):
            self.assertTrue(command_is_test(command), command)
        for command in ('echo "python -m unittest tests.test_x"', 'Set-Content report.md "node tests/test_x.js"', 'python -c "print(\'python -m unittest tests.x\')"', 'Get-Content x; python -m unittest tests.x', 'python -m unittest tests.x > report.txt', 'node tools/tests_report.js'):
            self.assertFalse(command_is_test(command), command)


if __name__ == '__main__':
    unittest.main()
