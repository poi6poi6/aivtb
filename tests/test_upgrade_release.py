"""Release transactions use fixture files and fake lifecycles only."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from brain.code_review import digest, sources
from brain.upgrade_release import (LocalRuntime, MainReleaseGate, ReleaseController, atomic_json, cancel_release,
    launch_release, local_health_url, process_identity, read_json, release_status, workspace_id)
from brain.upgrade_validation import SUITE


class FakeRuntime:
    def __init__(self, root, manifest):
        self.root, self.time = root, 0
        self.live, self.events = {}, []
        self.launches = 0
        self.fail_first = False
        self.refuse_stop = False
        self.on_sleep = None
        self.stale = None
        self.current = self.instance(100, 'a' * 16, manifest)

    def instance(self, pid, boot, manifest, release_id=None, ready=True):
        data = {'scope': 'main', 'workspace_id': workspace_id(self.root), 'boot_id': boot,
                'ready': ready, 'upgrade_idle': True, 'code': manifest}
        for offset, name in enumerate(('process', 'launcher', 'cleanup')):
            self.live[pid + offset] = 'fake:' + str(pid + offset)
            data[name] = {'pid': pid + offset, 'identity': self.live[pid + offset]}
        if release_id:
            data['release_id'] = release_id
        return data

    def now(self):
        return self.time

    def sleep(self, delay):
        self.time += delay
        if self.on_sleep:
            self.on_sleep()

    def identity(self, pid):
        return self.live.get(pid)

    def health(self):
        if self.stale:
            value, self.stale = self.stale, None
            return value
        return self.current

    def request_stop(self, instance, request_id):
        self.events.append(('stop', instance['boot_id']))
        if self.refuse_stop:
            return
        for name in ('process', 'launcher', 'cleanup'):
            if name in instance:
                self.live.pop(instance[name]['pid'], None)
        if self.current and self.current['boot_id'] == instance['boot_id']:
            self.current = None

    def launch(self, release_id):
        self.launches += 1
        manifest = {name: digest(data) for name, data in sources(self.root).items()}
        self.events.append(('launch', manifest))
        self.current = self.instance(200 + 10 * self.launches, f'{self.launches:016x}', manifest,
            release_id, ready=not(self.fail_first and self.launches == 1))
        atomic_json(self.root / 'data/upgrades/main-instance.json', self.current)
        return self.current['launcher']


class UpgradeReleaseTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        (self.root / 'brain').mkdir()
        self.before = {'brain/app.py': b'value = 1\n', 'brain/other.py': b'other = 1\n'}
        self.after = {'brain/app.py': b'value = 2\n', 'brain/other.py': b'other = 2\n'}
        for name, data in self.before.items():
            (self.root / name).write_bytes(data)
        self.baseline = {name: digest(data) for name, data in self.before.items()}
        self.candidate = {name: digest(data) for name, data in self.after.items()}
        self.host = self.root / 'data/code-reviews/job'
        for kind, files in (('baseline', self.before), ('staged', self.after)):
            for name, data in files.items():
                target = self.host / kind / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
        self.report = {'can_apply': True, 'source_version': self.candidate, 'baseline_version': self.baseline,
            'changes': [{'path': name, 'before': self.baseline[name], 'after': self.candidate[name],
                         'operation': 'modify'} for name in self.before]}
        self.save_report()
        self.policy = {'paused': False, 'policy': {'mode': 'auto', 'allowed_paths': ['brain/app.py', 'brain/other.py']}}
        atomic_json(self.root / 'data/self-upgrade/state.json', self.policy)
        self.runtime = FakeRuntime(self.root, self.baseline)
        self.journal = {'version': 1, 'release_id': 'fixture-release', 'job_id': 'job', 'state': 'queued',
            'expected_boot': self.runtime.current['boot_id'], 'manual': False,
            'workspace_id': workspace_id(self.root), 'allowed_paths': self.policy['policy']['allowed_paths'],
            'review_digest': digest(json.dumps(self.report, sort_keys=True).encode())}
        atomic_json(self.root / 'data/upgrades/release.json', self.journal)

    def save_report(self):
        atomic_json(self.host / 'review.json', self.report)
        atomic_json(self.host / 'independent.json', {'version': 1, 'suite': SUITE, 'job_id': 'job', 'passed': True,
            'review_digest': digest(json.dumps(self.report, sort_keys=True).encode()),
            'baseline_version': self.baseline, 'source_version': self.candidate,
            'source_before': self.candidate, 'source_after': self.candidate})

    def controller(self):
        return ReleaseController(self.root, allowed_paths=['brain/'], runtime=self.runtime, stop_timeout=1, start_timeout=2)

    def test_success_is_a_new_owned_boot_with_the_exact_tested_manifest(self):
        result = self.controller().run('fixture-release')
        self.assertEqual(result['state'], 'verified')
        self.assertEqual([event[0] for event in self.runtime.events], ['stop', 'launch'])
        self.assertNotEqual(result['runtime_boot'], self.journal['expected_boot'])
        self.assertEqual(self.runtime.current['code'], self.candidate)
        self.assertTrue((self.host / 'applied.json').exists())
        self.assertNotIn('process', release_status(self.root))

    def test_failed_new_health_restores_files_and_restarts_previous_version(self):
        self.runtime.fail_first = True
        result = self.controller().run('fixture-release')
        self.assertEqual(result['state'], 'rolled_back')
        self.assertEqual([event[0] for event in self.runtime.events], ['stop', 'launch', 'stop', 'launch'])
        self.assertEqual(self.runtime.current['code'], self.baseline)
        self.assertTrue((self.host / 'rolled-back.json').exists())

    def test_wrong_owner_boot_pid_busy_or_unguarded_launch_never_stops(self):
        original = copy.deepcopy(self.runtime.current)
        for change in ({'workspace_id': 'foreign'}, {'boot_id': 'b' * 16}, {'upgrade_idle': False}, {'cleanup': None}):
            with self.subTest(change=change):
                self.runtime.current = {**copy.deepcopy(original), **change}
                if change.get('cleanup', 1) is None:
                    del self.runtime.current['cleanup']
                atomic_json(self.root / 'data/upgrades/release.json', self.journal)
                self.assertEqual(self.controller().run()['state'], 'failed')
                self.assertEqual(self.runtime.events, [])
        self.runtime.current = original
        self.runtime.live[100] = 'reused-pid'
        atomic_json(self.root / 'data/upgrades/release.json', self.journal)
        self.assertEqual(self.controller().run()['state'], 'failed')
        self.assertEqual(self.runtime.events, [])

    def test_cancel_before_stop_does_nothing_and_cancel_during_start_recovers(self):
        cancel_release(self.root)
        self.assertEqual(self.controller().run()['state'], 'cancelled')
        self.assertEqual(self.runtime.events, [])
        (self.root / 'data/upgrades/cancel.json').unlink()
        atomic_json(self.root / 'data/upgrades/release.json', self.journal)
        self.runtime.fail_first = True
        self.runtime.on_sleep = lambda: cancel_release(self.root)
        self.assertEqual(self.controller().run()['state'], 'rolled_back')
        self.assertEqual(self.runtime.current['code'], self.baseline)

    def test_stop_timeout_preserves_files_and_does_not_force_kill(self):
        self.runtime.refuse_stop = True
        result = self.controller().run()
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(self.runtime.launches, 0)
        self.assertEqual((self.root / 'brain/app.py').read_bytes(), self.before['brain/app.py'])
        self.assertEqual(len(self.runtime.live), 3)

    def test_crash_mid_apply_has_durable_journal_and_restores_both_files(self):
        controller = self.controller()
        original_replace = os.replace

        def crash_replace(source, target):
            if Path(target) == self.root / 'brain/other.py':
                raise SystemExit('fixture crash')
            return original_replace(source, target)

        with patch('brain.upgrade_release.os.replace', side_effect=crash_replace):
            with self.assertRaises(SystemExit):
                controller.run()
        self.assertEqual(read_json(controller.path)['state'], 'applying')
        self.assertEqual((self.root / 'brain/app.py').read_bytes(), self.after['brain/app.py'])
        self.assertEqual((self.root / 'brain/other.py').read_bytes(), self.before['brain/other.py'])
        self.assertEqual(self.controller().run(recover=True)['state'], 'rolled_back')
        self.assertEqual(self.runtime.current['code'], self.baseline)

    def test_recovery_refuses_to_overwrite_an_unrelated_edit(self):
        controller = self.controller()
        controller.journal = dict(self.journal, old_instance=copy.deepcopy(self.runtime.current))
        self.runtime.request_stop(self.runtime.current, 'fixture')
        controller.save('applying')
        (self.root / 'brain/app.py').write_text('owner_change = True\n')
        result = self.controller().run(recover=True)
        self.assertEqual(result['state'], 'recovery_blocked')
        self.assertEqual((self.root / 'brain/app.py').read_text(), 'owner_change = True\n')
        self.assertEqual(self.runtime.launches, 0)

    def test_foreign_health_during_failed_start_never_becomes_a_stop_target(self):
        original_launch = self.runtime.launch

        def launch(release_id):
            handle = original_launch(release_id)
            if self.runtime.launches == 1:
                self.runtime.current = {'scope': 'main', 'workspace_id': 'foreign', 'ready': True,
                    'boot_id': 'f' * 16, 'process': {'pid': 999, 'identity': 'foreign'}}
                self.runtime.live[999] = 'foreign'
            return handle

        self.runtime.launch = launch
        result = self.controller().run()
        self.assertEqual(result['state'], 'rolled_back')
        self.assertEqual(self.runtime.live[999], 'foreign')
        self.assertNotIn(('stop', 'f' * 16), self.runtime.events)

    def test_invalid_independent_report_and_changed_staged_bytes_fail_before_stop(self):
        proof = read_json(self.host / 'independent.json')
        for changes in ({'passed': False}, {'suite': 'candidate-suite'}, {'source_after': self.baseline}, {'review_digest': 'bad'}):
            atomic_json(self.host / 'independent.json', {**proof, **changes})
            atomic_json(self.root / 'data/upgrades/release.json', self.journal)
            self.assertEqual(self.controller().run()['state'], 'failed')
            self.assertEqual(self.runtime.events, [])
        atomic_json(self.host / 'independent.json', proof)
        (self.host / 'staged/brain/app.py').write_text('tampered = 1')
        atomic_json(self.root / 'data/upgrades/release.json', self.journal)
        self.assertEqual(self.controller().run()['state'], 'failed')
        self.assertEqual(self.runtime.events, [])

    def test_protected_paths_and_policy_cannot_be_bypassed_by_a_wide_request(self):
        self.report['changes'][0]['path'] = 'brain/upgrade_release.py'
        self.save_report()
        with self.assertRaises(ValueError):
            self.controller().validated_review('job')
        self.policy['policy']['mode'] = 'off'
        atomic_json(self.root / 'data/self-upgrade/state.json', self.policy)
        with patch('brain.upgrade_release.subprocess.Popen') as spawn:
            with self.assertRaises(ValueError):
                launch_release(self.root, 'job', expected_boot='a' * 16)
            spawn.assert_not_called()

    def test_manual_develop_mode_and_auto_pause_are_distinct(self):
        self.policy['policy']['mode'] = 'develop'
        atomic_json(self.root / 'data/self-upgrade/state.json', self.policy)
        self.assertEqual(self.controller().run()['state'], 'failed')
        self.journal['manual'] = True
        atomic_json(self.root / 'data/upgrades/release.json', self.journal)
        self.assertEqual(self.controller().run()['state'], 'verified')

    def test_loopback_only_and_no_redirect_or_credentials_in_target(self):
        for url in ('https://127.0.0.1:8080/runtime-health', 'http://example.org:8080/runtime-health',
                    'http://127.0.0.1:8080/other', 'http://user:pass@127.0.0.1:8080/runtime-health',
                    'http://127.0.0.1:8080/runtime-health?q=secret'):
            with self.assertRaises(ValueError):
                local_health_url(url)
        self.assertEqual(local_health_url('http://[::1]:8080/runtime-health'), 'http://[::1]:8080/runtime-health')

    def test_main_gate_binds_root_boot_process_and_defers_until_idle(self):
        gate = MainReleaseGate(self.root, boot_id='c' * 16)
        self.assertEqual(gate.health_fields()['process']['identity'], process_identity(os.getpid()))
        request = {'version': 1, 'request_id': 'release', 'workspace_id': workspace_id(self.root),
                   'expected_boot': gate.boot_id, 'process': gate.instance['process']}
        path = self.root / 'data/upgrades/stop-main.json'
        for malformed in ([], 42, 'invalid', None):
            atomic_json(path, malformed)
            self.assertFalse(gate.stop_requested(), 'invalid control JSON cannot crash or stop main')
        for changes in ({'expected_boot': 'd' * 16}, {'workspace_id': 'foreign'}, {'process': {'pid': 1, 'identity': 'no'}}):
            atomic_json(path, {**request, **changes}); self.assertFalse(gate.stop_requested())
        atomic_json(path, request)
        self.assertFalse(gate.stop_requested(idle=False)); self.assertTrue(path.exists())
        self.assertTrue(gate.stop_requested(idle=True)); self.assertFalse(path.exists())

    def test_withdrawn_stop_cannot_fire_when_main_becomes_idle_later(self):
        gate = MainReleaseGate(self.root, boot_id='c' * 16)
        runtime = LocalRuntime(self.root, 'http://127.0.0.1:8080/runtime-health')
        runtime.request_stop(gate.instance, 'first')
        self.assertFalse(gate.stop_requested(idle=False))
        runtime.withdraw_stop(gate.instance, 'first')
        self.assertFalse(gate.stop_requested(idle=True))
        self.assertFalse(runtime.stop_acknowledged(gate.instance, 'first'))
        runtime.request_stop(gate.instance, 'second')
        os.replace(self.root / 'data/upgrades/stop-main.json', self.root / 'data/upgrades/stop-claimed.json')
        runtime.withdraw_stop(gate.instance, 'second')
        self.assertTrue(runtime.stop_acknowledged(gate.instance, 'second'), 'claimed request is already accepted even before ack publication')

    def test_unrecorded_forward_or_rollback_launch_is_not_dispatched_twice(self):
        for state in ('starting', 'restarting_previous'):
            with self.subTest(state=state):
                old = copy.deepcopy(self.runtime.current)
                self.runtime.request_stop(old, 'fixture')
                journal = dict(self.journal, state=state, old_instance=old, new_launcher=None)
                atomic_json(self.root / 'data/upgrades/release.json', journal)
                self.assertEqual(self.controller().run(recover=True)['state'], 'recovery_blocked')
                self.assertEqual(self.runtime.launches, 0)
                self.assertEqual(self.controller().run(recover=True)['state'], 'recovery_blocked')
                self.assertEqual(self.runtime.launches, 0, 'repeated recovery cannot forget an uncertain dispatch')
                self.runtime.current = self.runtime.instance(100, 'a' * 16, self.baseline)

    def test_request_dispatch_releases_lock_before_child_and_preserves_scope(self):
        self.journal['state'] = 'failed'
        atomic_json(self.root / 'data/upgrades/release.json', self.journal)
        def spawned(argv, **kwargs):
            from brain.upgrade_release import release_lock
            with release_lock(self.root):
                current = read_json(self.root / 'data/upgrades/release.json')
            self.assertEqual(current['state'], 'queued')
            self.assertEqual(current['allowed_paths'], self.policy['policy']['allowed_paths'])
            self.assertEqual(argv[-2], '--release-id')
            self.assertEqual(kwargs['cwd'], self.root)
        with patch('brain.upgrade_release.subprocess.Popen', side_effect=spawned) as spawn:
            result = launch_release(self.root, 'job', expected_boot='a' * 16)
        self.assertTrue(result['queued']); self.assertEqual(spawn.call_count, 1)


if __name__ == '__main__':
    unittest.main()
