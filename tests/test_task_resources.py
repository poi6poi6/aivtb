import asyncio
import tempfile
import unittest
from pathlib import Path
from brain.agent_tasks import AgentTasks
from brain.config import AgentTasksConfig, LLMConfig


class ResourceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = AgentTasks(Path(self.tmp.name), AgentTasksConfig(enabled=True), LLMConfig())
        self.busy = {'local_gpu': '语音正在使用显卡', 'desktop': '游戏正在控制输入'}
        self.manager.resource_busy = lambda: self.busy
        self.executed = asyncio.Queue()

        async def execute(job):
            self.manager.update(job, state='running')
            await self.executed.put((job['id'], self.manager.resource_in_use('desktop')))
            self.manager.update(job, state='succeeded')
        self.manager.execute = execute

    async def asyncTearDown(self):
        await self.manager.close()
        self.tmp.cleanup()

    async def test_exclusive_waiter_does_not_block_remote_work_and_resumes(self):
        desktop = self.manager.submit('click', 'desktop', owner=True)
        remote = self.manager.submit('find', 'research', owner=True)
        self.assertEqual((await asyncio.wait_for(self.executed.get(), 2))[0], remote['id'])
        self.assertEqual(desktop['state'], 'queued')
        self.assertIn('游戏', desktop['wait_reason'])
        self.assertEqual(self.manager.status()['jobs'][0]['queue_position'], 1)
        self.busy.pop('desktop')
        identity, desktop_held = await asyncio.wait_for(self.executed.get(), 2)
        self.assertEqual(identity, desktop['id'])
        self.assertTrue(desktop_held)
        await asyncio.wait_for(self.manager.queue.join(), 2)
        self.assertFalse(self.manager.resource_in_use('desktop'))

    async def test_gpu_task_waits_while_code_runs_and_waiter_can_be_cancelled(self):
        gpu = self.manager.submit('local inference', 'image', owner=True, resource_profile='local_gpu')
        code = self.manager.submit('write code', 'code', owner=True)
        self.assertEqual((await asyncio.wait_for(self.executed.get(), 2))[0], code['id'])
        self.assertEqual(gpu['state'], 'queued')
        await self.manager.cancel(gpu['id'])
        await asyncio.wait_for(self.manager.queue.join(), 2)
        self.assertEqual(gpu['state'], 'cancelled')
        self.assertTrue(self.executed.empty())

    async def test_revision_keeps_gpu_requirement_and_cannot_drop_desktop_requirement(self):
        parent = self.manager.submit('gpu task', 'desktop', resource_profile='local_gpu', requester='viewer')
        preview = await self.manager.amend(parent['id'], 'change', requester='viewer')
        child = await self.manager.amend(parent['id'], 'change', requester='viewer', token=preview['token'])
        self.assertEqual(self.manager.resources(child), {'desktop', 'local_gpu'})
        self.assertEqual(child['state'], 'pending')

    async def test_running_task_cancel_releases_input(self):
        self.busy.clear()
        started=asyncio.Event()
        async def execute(job):
            self.manager.update(job,state='running')
            started.set()
            await asyncio.Event().wait()
        self.manager.execute=execute
        job=self.manager.submit('desktop','desktop',owner=True)
        await asyncio.wait_for(started.wait(),1)
        self.assertTrue(self.manager.resource_in_use('desktop'))
        await self.manager.cancel(job['id'])
        self.assertFalse(self.manager.resource_in_use('desktop'))


if __name__ == '__main__':
    unittest.main()
