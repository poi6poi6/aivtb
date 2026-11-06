import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from brain.agent_tasks import AgentTasks
from brain.config import AgentTasksConfig, LLMConfig


class AtomicAmendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = AgentTasks(Path(self.tmp.name), AgentTasksConfig(enabled=True), LLMConfig())
        # Keep this suite fully offline while retaining the production queue.
        self.manager.worker = asyncio.create_task(asyncio.Event().wait())

    async def asyncTearDown(self):
        await self.manager.close()
        self.tmp.cleanup()

    def parent(self, **kwargs):
        job = self.manager.submit('画一座庭院', kind='drawing', owner=True, **kwargs)
        self.manager.queue.get_nowait()
        self.manager.queue.task_done()
        return job

    def persisted(self, job_id):
        with sqlite3.connect(self.manager.task_store.path) as db:
            row = db.execute('SELECT payload FROM tasks WHERE id=?', (job_id,)).fetchone()
        return json.loads(row[0]) if row else None

    async def test_confirmation_is_durably_consumed_before_cancel_and_cannot_run_twice(self):
        parent = self.parent()
        preview = await self.manager.amend(parent['id'], '背景改成夜晚')
        entered, release = asyncio.Event(), asyncio.Event()
        original_cancel = self.manager.cancel

        async def held_cancel(job_id=None):
            entered.set()
            await release.wait()
            await original_cancel(job_id)

        self.manager.cancel = held_cancel
        pending = asyncio.create_task(self.manager.amend(parent['id'], '背景改成夜晚', token=preview['token']))
        await entered.wait()
        self.assertNotIn('amend_preview', self.persisted(parent['id']))
        self.assertEqual(parent['state'], 'queued')
        with self.assertRaisesRegex(ValueError, '正在停止'):
            await self.manager.amend(parent['id'], '背景改成夜晚', token=preview['token'])
        release.set()
        child = await pending
        self.assertEqual(parent['superseded_by'], child['id'])
        self.assertEqual(len(self.manager.jobs), 2)
        self.assertEqual(self.manager.queue.get_nowait(), child['id'])
        self.manager.queue.task_done()

    async def test_child_insert_failure_leaves_no_unlinked_job_or_queued_work(self):
        parent = self.parent()
        preview = await self.manager.amend(parent['id'], '背景改成夜晚')
        self.manager.task_store._connection.execute("""CREATE TRIGGER fail_child BEFORE INSERT ON tasks
            BEGIN SELECT RAISE(ABORT, 'simulated storage failure'); END""")
        with self.assertRaisesRegex(ValueError, '修订未创建'):
            await self.manager.amend(parent['id'], '背景改成夜晚', token=preview['token'])
        self.assertEqual(parent['state'], 'cancelled')
        self.assertNotIn('superseded_by', parent)
        self.assertNotIn('amend_preview', self.persisted(parent['id']))
        self.assertEqual(len(self.manager.jobs), 1)
        self.assertTrue(self.manager.queue.empty())
        with sqlite3.connect(self.manager.task_store.path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 1)

    async def test_consume_failure_does_not_cancel_original_or_publish_child(self):
        parent = self.parent()
        preview = await self.manager.amend(parent['id'], '背景改成夜晚')
        self.manager.task_store._connection.execute("""CREATE TRIGGER fail_consume BEFORE UPDATE ON tasks
            BEGIN SELECT RAISE(ABORT, 'simulated storage failure'); END""")
        original_cancel = self.manager.cancel
        self.manager.cancel = AsyncMock()
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                await self.manager.amend(parent['id'], '背景改成夜晚', token=preview['token'])
            self.manager.cancel.assert_not_awaited()
        finally:
            self.manager.cancel = original_cancel
            self.manager.task_store._connection.execute('DROP TRIGGER fail_consume')
        self.assertEqual(parent['state'], 'queued')
        self.assertEqual(parent['amend_preview']['token'], preview['token'])
        self.assertEqual(len(self.manager.jobs), 1)
        self.assertTrue(self.manager.queue.empty())

    async def test_capacity_is_checked_again_after_process_cancellation(self):
        parent = self.parent()
        self.manager.cfg.max_queue = 1
        preview = await self.manager.amend(parent['id'], '背景改成夜晚')
        original_cancel = self.manager.cancel

        async def concurrent_submit(job_id=None):
            await original_cancel(job_id)
            self.manager.submit('另一个主播请求', owner=True)

        self.manager.cancel = concurrent_submit
        try:
            with self.assertRaisesRegex(ValueError, '队列已满'):
                await self.manager.amend(parent['id'], '背景改成夜晚', token=preview['token'])
        finally:
            self.manager.cancel = original_cancel
        self.assertEqual(len(self.manager.jobs), 2)
        self.assertFalse(any(job.get('parent_id') for job in self.manager.jobs))
        self.assertNotIn('superseded_by', parent)
        self.assertEqual(parent['state'], 'cancelled')

    async def test_invalid_submission_keeps_original_task_and_confirmation(self):
        parent = self.parent()
        preview = await self.manager.amend(parent['id'], '背景改成夜晚')
        self.manager.cfg.enabled = False
        with self.assertRaisesRegex(ValueError, '尚未启用'):
            await self.manager.amend(parent['id'], '背景改成夜晚', token=preview['token'])
        self.assertEqual(parent['state'], 'queued')
        self.assertEqual(parent['amend_preview']['token'], preview['token'])

    async def test_viewer_revision_keeps_identity_approval_and_execution_metadata(self):
        parent = self.manager.submit('画庭院', 'drawing', requester='danmaku:one', user='同名观众',
                                     resource_profile='local_gpu', execution_mode='omp')
        self.manager.submit('画云朵', 'drawing', requester='danmaku:two', user='同名观众')
        preview = await self.manager.amend(parent['id'], '夜晚背景', requester='danmaku:one')
        child = await self.manager.amend(parent['id'], '夜晚背景', requester='danmaku:one', token=preview['token'])
        self.assertEqual(child['state'], 'pending')
        self.assertEqual(child['requester'], 'danmaku:one')
        self.assertEqual(child['execution_mode'], 'omp')
        self.assertEqual(child['resource_profile'], 'local_gpu')
        self.assertEqual(child['parent_id'], parent['id'])
        self.assertTrue(self.manager.queue.empty())

    async def test_worker_only_sees_child_after_both_records_have_committed(self):
        parent = self.parent(execution_mode='direct')
        preview = await self.manager.amend(parent['id'], '背景改成夜晚')
        original_enqueue = self.manager._enqueue_submission
        observed = []

        def inspect_before_enqueue(child):
            self.assertEqual(self.persisted(parent['id'])['superseded_by'], child['id'])
            self.assertEqual(self.persisted(child['id'])['parent_id'], parent['id'])
            self.assertIs(self.manager.get(child['id']), child)
            observed.append(child['id'])
            original_enqueue(child)

        self.manager._enqueue_submission = inspect_before_enqueue
        child = await self.manager.amend(parent['id'], '背景改成夜晚', token=preview['token'])
        self.assertEqual(observed, [child['id']])
        self.assertEqual(child['execution_mode'], 'direct')


if __name__ == '__main__':
    unittest.main()
