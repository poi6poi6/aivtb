import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from brain.task_service import TaskService
from brain.task_store import TaskStore, TaskStoreConflict


class TaskStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.path = self.folder / 'jobs.sqlite3'
        self.legacy = self.folder / 'jobs.json'

    def store(self):
        store = TaskStore(self.path, legacy_path=self.legacy)
        self.addCleanup(store.close)
        store.load()
        return store

    def test_legacy_migration_preserves_file_fields_order_and_only_runs_once(self):
        data = [{'id': 'b', 'state': 'running', 'prompt': '庭院与玫瑰', 'revision': 7,
                 'artifacts': ['drawing.json'], 'delivery': {'revision': 7, 'state': 'played'}},
                {'id': 'a', 'state': 'review', 'amend_preview': {'token': 'existing'}}]
        original = json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8')
        self.legacy.write_bytes(original)
        store = self.store()
        loaded = store.load()
        self.assertEqual([j['id'] for j in loaded], ['b', 'a'])
        self.assertEqual(loaded[0]['revision'], 7)
        self.assertEqual(loaded[0]['delivery'], data[0]['delivery'])
        self.assertEqual(loaded[1]['amend_preview'], data[1]['amend_preview'])
        self.assertEqual(self.legacy.read_bytes(), original)
        loaded[0]['detail'] = '已保存在数据库'
        store.save_many([loaded[0]])
        store.close()
        self.legacy.write_text('[{"id":"later","state":"queued"}]', 'utf-8')
        restarted = self.store()
        self.assertEqual([j['id'] for j in restarted.load()], ['b', 'a'])
        self.assertEqual(restarted.snapshot('b')['detail'], '已保存在数据库')

    def test_invalid_migration_rolls_back_all_rows_and_can_retry(self):
        self.legacy.write_text('[{"id":"same","state":"queued"},{"id":"same","state":"running"}]', 'utf-8')
        with self.assertRaisesRegex(ValueError, '重复编号'):
            self.store()
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 0)
            self.assertIsNone(db.execute("SELECT value FROM task_meta WHERE key='legacy_import'").fetchone())
        self.legacy.write_text('[{"id":"fixed","state":"queued"}]', 'utf-8')
        self.assertEqual(self.store().load()[0]['id'], 'fixed')

    def test_only_changed_rows_are_written_and_subset_does_not_delete_history(self):
        store = self.store()
        jobs = [{'id': str(i), 'state': 'running'} for i in range(40)]
        store.save_many(jobs)
        trace = []
        store._connection.set_trace_callback(trace.append)
        jobs[9]['progress'] = {'stage': 'testing'}
        self.assertEqual(store.save_many(jobs), ['9'])
        writes = [sql for sql in trace if sql.startswith(('UPDATE tasks', 'INSERT INTO tasks'))]
        self.assertEqual(len(writes), 1)
        self.assertEqual(store.save_many([jobs[9]]), [])
        self.assertEqual(len(store.load()), 40)
        self.assertFalse(self.legacy.exists())

    def test_sql_failure_rolls_back_whole_batch_and_snapshot_cache(self):
        store = self.store()
        a, b = {'id': 'a', 'state': 'queued'}, {'id': 'b', 'state': 'queued'}
        store.save_many([a, b])
        store._connection.execute("""CREATE TRIGGER fail_second BEFORE UPDATE ON tasks
            WHEN NEW.id='b' BEGIN SELECT RAISE(ABORT, 'simulated disk/write failure'); END""")
        a['state'] = b['state'] = 'running'
        with self.assertRaises(sqlite3.IntegrityError):
            store.save_many([a, b])
        self.assertEqual(store.snapshot('a')['state'], 'queued')
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT state, row_version FROM tasks ORDER BY id').fetchall(),
                             [('queued', 1), ('queued', 1)])
        store._connection.execute('DROP TRIGGER fail_second')
        self.assertEqual(store.save_many([a, b]), ['a', 'b'])

    def test_stale_writer_conflict_rolls_back_earlier_updates_in_batch(self):
        first = self.store()
        first.save_many([{'id': 'a', 'state': 'queued'}, {'id': 'b', 'state': 'queued'}])
        second = self.store()
        newer = first.snapshot('b')
        newer['state'] = 'running'
        first.save_many([newer])
        stale = second.load()  # Capture current state then make it stale again.
        newer['detail'] = 'new receipt'
        first.save_many([newer])
        stale[0]['detail'] = 'must roll back'
        stale[1]['detail'] = 'outdated overwrite'
        with self.assertRaises(TaskStoreConflict):
            second.save_many(stale)
        self.assertNotIn('detail', first.load()[0])
        self.assertEqual(first.snapshot('b')['detail'], 'new receipt')


class TaskServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = TaskStore(Path(self.tmp.name) / 'jobs.sqlite3')
        self.addCleanup(self.store.close)
        self.service = TaskService(self.store)
        self.service.load()

    def job(self, job_id='a', state='queued', requester='owner'):
        job = {'id': job_id, 'state': state, 'requester': requester,
               'kind': 'research', 'prompt': '研究庭院植物', 'artifacts': []}
        self.service.save([job])
        return job

    def test_transition_commands_reject_illegal_or_terminal_restart_without_mutation(self):
        job = self.job(state='pending')
        with self.assertRaisesRegex(ValueError, '不允许'):
            self.service.update(job, state='running')
        self.assertEqual(job['state'], 'pending')
        for state in ['queued', 'running', 'waiting_approval', 'running', 'succeeded']:
            self.service.update(job, state=state)
        before = dict(job)
        with self.assertRaisesRegex(ValueError, '不允许'):
            self.service.update(job, state='running')
        self.assertEqual(job, before)
        self.assertEqual(self.store.snapshot(job['id']), before)

    def test_code_lifecycle_accepts_only_review_apply_verify_rollback(self):
        job = self.job(state='running')
        for state in ['review', 'applied', 'verified', 'rolled_back']:
            self.service.update(job, state=state)
        with self.assertRaises(ValueError):
            self.service.update(job, state='applied')

    def test_progress_and_delivery_have_independent_revisions(self):
        job = self.job(state='running')
        original_revision = job['revision']
        for step in range(25):
            self.service.update(job, progress={'last_event_at': step, 'stage': 'testing'})
            self.service.update(job, delivery={'state': 'playing', 'updated': step, 'revision': original_revision})
            self.service.update(job, detail='执行中', budget={'tool_calls': step})
        self.assertEqual(job['revision'], original_revision)
        self.assertEqual(job['progress_revision'], 25)
        self.assertEqual(job['delivery_revision'], 25)
        self.service.update(job, state='succeeded', revision=999)
        self.assertEqual(job['revision'], original_revision + 1)
        self.assertEqual(job['progress_revision'], 25)
        self.assertEqual(job['delivery_revision'], 25)

    def test_requester_and_expected_revision_are_checked_before_commit(self):
        job = self.job(requester='danmaku:1')
        with self.assertRaisesRegex(ValueError, '自己的'):
            self.service.update(job, requester='danmaku:2', state='cancelled')
        self.service.update(job, requester='danmaku:1', expected_revision=job['revision'], state='running')
        with self.assertRaisesRegex(ValueError, '版本已变化'):
            self.service.update(job, requester='owner', expected_revision=1, state='cancelled')
        self.assertEqual(job['state'], 'running')

    def test_execution_route_changes_invalidate_old_confirmation_revision(self):
        job = self.job()
        revision = job['revision']
        self.service.update(job, execution_mode='omp')
        self.assertEqual(job['revision'], revision + 1)
        self.assertEqual(job['progress_revision'], 0)

    def test_parent_and_child_are_committed_together_only_after_success(self):
        parent = self.job(state='running')
        child = {'id': 'child', 'state': 'queued', 'requester': 'owner', 'parent_id': 'a'}
        candidate = {**parent, 'state': 'cancelled', 'superseded_by': 'child'}
        self.store._connection.execute("""CREATE TRIGGER fail_child BEFORE INSERT ON tasks
            WHEN NEW.id='child' BEGIN SELECT RAISE(ABORT, 'simulated failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.service.replace_many([(parent, candidate), (child, child)])
        self.assertEqual(parent['state'], 'running')
        self.assertNotIn('superseded_by', parent)
        self.assertNotIn('revision', child)
        self.assertIsNone(self.store.snapshot('child'))
        self.store._connection.execute('DROP TRIGGER fail_child')
        self.service.replace_many([(parent, candidate), (child, child)])
        self.assertEqual(parent['superseded_by'], 'child')
        self.assertEqual(child['parent_id'], 'a')
        self.assertEqual([j['id'] for j in self.store.load()], ['a', 'child'])

    def test_compatibility_flush_reverts_dicts_on_validation_failure(self):
        a = self.job('a', 'running')
        b = self.job('b', 'succeeded')
        a['progress'] = {'stage': 'testing'}
        b['state'] = 'running'
        with self.assertRaises(ValueError):
            self.service.save([a, b])
        self.assertNotIn('progress', a)
        self.assertEqual(b['state'], 'succeeded')
        self.assertEqual(self.store.snapshot('a'), a)

    def test_create_rejects_already_completed_or_duplicate_requests(self):
        with self.assertRaisesRegex(ValueError, '新请求'):
            self.service.create({'id': 'a', 'state': 'succeeded'})
        self.service.create({'id': 'a', 'state': 'pending'})
        with self.assertRaisesRegex(ValueError, '已存在'):
            self.service.create({'id': 'a', 'state': 'pending'})


if __name__ == '__main__':
    unittest.main()
