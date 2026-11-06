import asyncio
from copy import deepcopy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from brain.self_upgrade import SelfUpgrade, validate_policy, DEFAULT_POLICY
from brain.agent_rpc import subscription_remaining


HEALTH = {'auth': 'chatgpt', 'astra': True, 'rateLimits': {'primary': {'usedPercent': 12}}}


class Manager:
    def __init__(self, root):
        self.root = self.folder = Path(root)
        self.jobs, self.health = [], deepcopy(HEALTH)
        self.cfg = SimpleNamespace(codex_command='codex')
        self.closing = False
        self.verification_url = 'http://127.0.0.1:8080/runtime-health'
        self.cancel = AsyncMock(side_effect=self._cancel)
        self.enqueued = []

    def prepare_upgrade(self, prompt, issue_id, scope):
        return self.submit(prompt, 'code', owner=True, user='系统改进', execution_mode='direct',
                           self_upgrade_id=issue_id, self_upgrade_scope=list(scope))

    def _enqueue_submission(self, job):
        # Publication must observe the persisted authorization, not just memory.
        from brain.self_upgrade import read_state
        disk = read_state(self.root)
        assert disk['active']['job_id'] == job['id']
        assert job['self_upgrade_id'] == disk['active']['issue_id']
        assert job['self_upgrade_scope'] == disk['policy']['allowed_paths']
        self.enqueued.append(job['id'])

    def submit(self, prompt, kind, **kw):
        job = {'id': f'job{len(self.jobs)}', 'state': 'queued', 'prompt': prompt, 'kind': kind, **kw}
        self.jobs.append(job)
        return job

    def update(self, job, **values):
        job.update(values)

    def get(self, key):
        return next(j for j in self.jobs if j['id'] == key)

    def _cancel(self, key):
        self.get(key)['state'] = 'cancelled'


class PolicyTests(unittest.TestCase):
    def test_default_and_scopes(self):
        self.assertEqual(validate_policy({}), DEFAULT_POLICY)
        for path in ('brain/static/face.js', 'brain/static/'):
            self.assertEqual(validate_policy({'mode': 'auto', 'allowed_paths': [path]})['allowed_paths'], [path])
        for raw in ({'mode': 'auto'}, {'mode': []}, {'daily_limit': True}, {'extra': 1},
                    {'allowed_paths': ['../data']}, {'allowed_paths': ['brain/main.py']},
                    {'allowed_paths': ['tests/']}, {'allowed_paths': ['G:/x.py']}):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                validate_policy(raw)

    def test_multi_bucket_quota_unknown_and_no_credit_fallback(self):
        self.assertEqual(subscription_remaining(HEALTH), 88)
        self.assertIsNone(subscription_remaining({'rateLimits': {'credits': {'unlimited': True}}}))
        state = {**HEALTH, 'rateLimitsByLimitId': {'codex': {'primary': {'usedPercent': 15}},
                                                  'astra': {'primary': {'usedPercent': 98}}}}
        self.assertEqual(subscription_remaining(state), 2)
        for used in (True, '3', float('nan'), -1):
            self.assertIsNone(subscription_remaining({'rateLimits': {'primary': {'usedPercent': used}}}))


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.manager = Manager(self.tmp.name)
        self.loop = SelfUpgrade(self.manager)
        for index in range(100):
            self.loop.submit(f'Improvement {index}')
            self.loop.state['issues'][-1].update(
                status='completed', job_id=f'completed-job-{index}',
                detail=f'Verified deployment {index}', attempts=1)
        self.loop.save()

    def test_completed_history_is_archived_before_admitting_new_goal_and_survives_restart(self):
        original = deepcopy(self.loop.state['issues'][0])
        new = self.loop.submit('New improvement')
        self.assertEqual(new['status'], 'proposed')
        self.assertEqual(len(self.loop.state['issues']), 100)
        self.assertNotIn(original['id'], [issue['id'] for issue in self.loop.state['issues']])
        archive = self.loop.path.parent/'archive'/f"{original['id']}.json"
        archived = json.loads(archive.read_text('utf-8'))
        self.assertEqual(archived['issue'], original)

        restarted = SelfUpgrade(self.manager)
        self.assertEqual(len(restarted.state['issues']), 100)
        self.assertEqual(restarted.state['issues'][-1]['id'], new['id'])
        duplicate = restarted.submit(original['prompt'])
        self.assertEqual(duplicate['id'], original['id'])
        self.assertEqual(duplicate['job_id'], original['job_id'])
        self.assertEqual(duplicate['detail'], original['detail'])
        self.assertEqual(duplicate['status'], 'completed')
        self.assertEqual(duplicate['count'], original['count'] + 1)
        self.assertEqual(len(restarted.state['issues']), 100)
        self.assertEqual(json.loads(archive.read_text('utf-8'))['issue'], duplicate)
        self.assertEqual(json.loads(archive.read_text('utf-8'))['archived_at'], archived['archived_at'])
        self.assertEqual(self.manager.jobs, [])

    def test_active_reference_is_retained_even_if_marked_completed(self):
        protected, next_old = self.loop.state['issues'][:2]
        # Defend either reference independently during a transitional update.
        for active in ({'issue_id': protected['id']}, {'job_id': protected['job_id']}):
            with self.subTest(active=active):
                self.loop.state['active'] = active
                self.loop.save()
                self.loop.submit(f'New improvement {active}')
                self.assertIn(protected, self.loop.state['issues'])
                self.assertFalse((self.loop.path.parent/'archive'/f"{protected['id']}.json").exists())
        self.assertNotIn(next_old, self.loop.state['issues'])

    def test_unfinished_goals_are_not_discarded_to_make_room(self):
        for index, issue in enumerate(self.loop.state['issues']):
            issue['status'] = ('proposed', 'retry', 'developing', 'validating',
                               'validated', 'deploying', 'blocked')[index % 7]
        self.loop.save()
        before = deepcopy(self.loop.state)
        with self.assertRaisesRegex(ValueError, '未完成改进目标已达上限'):
            self.loop.submit('Another goal')
        self.assertEqual(self.loop.state, before)
        self.assertEqual(SelfUpgrade(self.manager).state, before)
        self.assertFalse((self.loop.path.parent/'archive').exists())

    def test_failed_archive_publication_preserves_every_goal_in_memory_and_on_disk(self):
        before = deepcopy(self.loop.state)
        replace = Path.replace

        def fail_archive(path, target):
            if path.parent.name == 'archive':
                raise OSError('archive disk unavailable')
            return replace(path, target)

        with patch.object(Path, 'replace', fail_archive):
            with self.assertRaisesRegex(OSError, 'archive disk unavailable'):
                self.loop.submit('New improvement')
        self.assertEqual(self.loop.state, before)
        self.assertEqual(SelfUpgrade(self.manager).state, before)

    def test_failed_working_list_publication_keeps_archived_target_recoverable(self):
        before = deepcopy(self.loop.state)
        with patch.object(self.loop, 'save', side_effect=OSError('state disk unavailable')):
            with self.assertRaisesRegex(OSError, 'state disk unavailable'):
                self.loop.submit('New improvement')
        self.assertEqual(self.loop.state, before)
        self.assertEqual(SelfUpgrade(self.manager).state, before)
        original = before['issues'][0]
        archive = self.loop.path.parent/'archive'/f"{original['id']}.json"
        self.assertEqual(json.loads(archive.read_text('utf-8'))['issue'], original)
        # Retrying completes the same archive transition without losing a goal.
        self.loop.submit('New improvement')
        self.assertEqual(len(self.loop.state['issues']), 100)
        self.assertEqual(self.loop.state['issues'][-1]['prompt'], 'New improvement')


class FlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = Manager(self.tmp.name)
        self.loop = SelfUpgrade(self.manager)
        self.loop.next_scan = float('inf')
        self.loop.subscription = AsyncMock(return_value=deepcopy(HEALTH))

    async def asyncTearDown(self):
        await self.loop.close()
        self.tmp.cleanup()

    async def enable(self, mode='develop', **options):
        await self.loop.action('self_upgrade_configure', {'policy': {
            'mode': mode, 'allowed_paths': ['brain/static/face.js'], **options}})

    def issue(self):
        self.loop.submit('修复说话时表情冻结')
        return self.loop.state['issues'][0]

    async def test_default_proposal_never_spends_or_edits(self):
        issue = self.issue()
        self.loop.submit('修复说话时表情冻结')
        await self.loop.step()
        self.assertEqual(issue['count'], 2)
        self.assertEqual(self.manager.jobs, [])
        self.loop.subscription.assert_not_awaited()

    async def test_develop_validates_then_waits_for_apply(self):
        await self.enable()
        issue = self.issue()
        await self.loop.step()
        job = self.manager.jobs[0]
        self.assertEqual(job['execution_mode'], 'direct')
        self.loop.authorize_dispatch(job, HEALTH)
        job['state'] = 'review'
        with patch('brain.upgrade_validation.validate_candidate', AsyncMock(return_value={'passed': True})), \
             patch('brain.upgrade_release.launch_release') as launch:
            await self.loop.step()
            await self.loop.step()
        self.assertEqual(issue['status'], 'validated')
        self.assertIsNone(self.loop.state['active'])
        launch.assert_not_called()

    async def test_auto_waits_for_idle_and_tracks_verified_release(self):
        await self.enable('auto')
        issue = self.issue()
        await self.loop.step()
        self.manager.jobs[0]['state'] = 'review'
        with patch('brain.upgrade_validation.validate_candidate', AsyncMock(return_value={'passed': True})):
            await self.loop.step()
        self.loop.scan = AsyncMock()
        self.loop.runtime = {'upgrade_idle': False, 'boot_id': 'old'}
        with patch('brain.upgrade_release.launch_release', return_value={'ok': True}) as launch:
            await self.loop.step()
            launch.assert_not_called()
            self.loop.runtime['upgrade_idle'] = True
            await self.loop.step()
            launch.assert_called_once_with(self.loop.root, issue['job_id'], expected_boot='old', manual=False)
        with patch('brain.upgrade_release.release_status', return_value={
                'job_id': issue['job_id'], 'state': 'verified', 'detail': '新版本已核验'}):
            await self.loop.step()
        self.assertEqual(issue['status'], 'completed')
        self.assertIsNone(self.loop.state['active'])

    async def test_validation_retries_are_bounded(self):
        await self.enable(max_attempts=2)
        issue = self.issue()
        with patch('brain.upgrade_validation.validate_candidate', AsyncMock(return_value={
                'passed': False, 'reason': '目标模块测试失败'})):
            for _ in range(2):
                await self.loop.step()
                self.manager.jobs[-1]['state'] = 'review'
                await self.loop.step()
            await self.loop.step()
        self.assertEqual(len(self.manager.jobs), 2)
        self.assertEqual(issue['status'], 'blocked')

    async def test_daily_budget_does_not_query_or_spend(self):
        import time
        await self.enable(daily_limit=1)
        self.loop.state['starts'] = [time.time()]
        self.issue()
        await self.loop.step()
        self.loop.subscription.assert_not_awaited()
        self.assertEqual(self.manager.jobs, [])

    async def test_quota_failure_and_dispatch_recheck_fail_closed(self):
        await self.enable()
        self.issue()
        await self.loop.step()
        for health in ({}, {'auth': 'api', 'astra': True},
                       {**HEALTH, 'rateLimits': {'primary': {'usedPercent': 81}}}):
            with self.assertRaises(RuntimeError):
                self.loop.authorize_dispatch(self.manager.jobs[0], health)
        await self.loop.stop()
        with self.assertRaises(RuntimeError):
            self.loop.authorize_dispatch(self.manager.jobs[0], HEALTH)
        self.assertEqual(self.loop.state['policy']['mode'], 'proposal')

    async def test_restart_does_not_replay_active_job(self):
        await self.enable()
        self.issue()
        await self.loop.step()
        again = SelfUpgrade(self.manager)
        again.next_scan = float('inf')
        await again.step()
        self.assertTrue(again.state['paused'])
        self.assertIsNone(again.state['active'])
        self.assertEqual(len(self.manager.jobs), 1)

    async def test_recovery_blocked_is_not_success(self):
        await self.enable('auto')
        issue = self.issue()
        await self.loop.step()
        self.loop.state['active']['stage'] = 'deploying'
        with patch('brain.upgrade_release.release_status', return_value={
                'job_id': issue['job_id'], 'state': 'recovery_blocked', 'detail': '需要人工恢复'}):
            await self.loop.step()
        self.assertEqual(issue['status'], 'blocked')
        self.assertIsNone(self.loop.state['active'])

    async def test_metadata_is_persisted_before_enqueue(self):
        await self.enable()
        issue = self.issue()
        await self.loop.step()
        job = self.manager.jobs[0]
        self.assertEqual(job['self_upgrade_id'], issue['id'])
        self.assertEqual(job['self_upgrade_scope'], ['brain/static/face.js'])
        self.assertEqual(self.manager.enqueued, [job['id']])

    async def test_prepare_failure_never_publishes_an_unrestricted_job(self):
        await self.enable()
        self.issue()
        with patch.object(self.manager, 'prepare_upgrade', side_effect=OSError('database unavailable')):
            await self.loop.safe_step()
        self.assertEqual(self.manager.enqueued, [])
        self.assertEqual(self.manager.jobs, [])
        self.assertTrue(self.loop.state['paused'])

    async def test_state_write_failure_cancels_candidate_before_enqueue(self):
        await self.enable()
        issue = self.issue()
        save = self.loop.save
        calls = 0

        def fail_once():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError('disk unavailable')
            save()

        with patch.object(self.loop, 'save', side_effect=fail_once):
            await self.loop.safe_step()
        self.assertEqual(self.manager.enqueued, [])
        self.assertEqual(len(self.manager.jobs), 1)
        job = self.manager.jobs[0]
        self.assertEqual(job['self_upgrade_id'], issue['id'])
        self.assertEqual(job['state'], 'cancelled')
        self.assertTrue(self.loop.state['paused'])
        self.assertIsNone(self.loop.state['active'])

    async def test_superseded_candidate_does_not_validate_or_deploy(self):
        await self.enable('auto')
        issue = self.issue()
        await self.loop.step()
        self.manager.jobs[0].update(state='review', superseded_by='new-revision')
        with patch('brain.upgrade_validation.validate_candidate', AsyncMock()) as validate, \
             patch('brain.upgrade_release.launch_release') as launch:
            await self.loop.step()
        validate.assert_not_awaited()
        launch.assert_not_called()
        self.assertEqual(issue['status'], 'blocked')
        self.assertIsNone(self.loop.state['active'])

    async def test_unexpected_applied_state_does_not_stick_as_developing(self):
        await self.enable()
        issue = self.issue()
        await self.loop.step()
        self.manager.jobs[0]['state'] = 'applied'
        await self.loop.step()
        self.assertEqual(issue['status'], 'blocked')
        self.assertIsNone(self.loop.state['active'])

    async def test_stop_revokes_deploy_waiting_for_health_even_after_reenable(self):
        await self.enable('auto')
        issue = self.issue()
        await self.loop.step()
        self.manager.jobs[0]['state'] = 'review'
        issue['status'] = 'validated'
        self.loop.state['active'] = None
        entered, finish = asyncio.Event(), asyncio.Event()

        async def delayed_scan():
            entered.set()
            await finish.wait()
            self.loop.runtime = {'upgrade_idle': True, 'boot_id': 'old'}

        self.loop.scan = delayed_scan
        with patch('brain.upgrade_release.launch_release') as launch:
            pending = asyncio.create_task(self.loop.action('self_upgrade_deploy', {'id': issue['job_id']}))
            await asyncio.wait_for(entered.wait(), 1)
            self.assertEqual(self.loop.state['active']['stage'], 'preparing_release')
            with self.assertRaises(ValueError):
                await self.loop.action('self_upgrade_deploy', {'id': issue['job_id']})
            await self.loop.stop()
            await self.enable('auto')
            finish.set()
            with self.assertRaises(ValueError):
                await asyncio.wait_for(pending, 1)
        launch.assert_not_called()
        self.assertIsNone(self.loop.state['active'])


if __name__ == '__main__':
    unittest.main()
