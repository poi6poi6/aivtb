import asyncio
import unittest

from brain.runtime_supervisor import RuntimeSupervisor


class RuntimeSupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.supervisor = RuntimeSupervisor(base_delay=0, max_delay=0)
        self.addAsyncCleanup(self.supervisor.close)

    async def until(self, predicate):
        for _ in range(100):
            if predicate():
                return
            await asyncio.sleep(0)
        self.fail('worker did not reach the expected state')

    async def test_empty_is_not_ready_and_running_worker_is_visible(self):
        self.assertFalse(self.supervisor.summary()['ready'])
        entered = asyncio.Event()

        async def worker():
            entered.set()
            await asyncio.Event().wait()

        self.supervisor.start('ingestion', worker)
        await entered.wait()
        result = self.supervisor.summary()
        self.assertTrue(result['ready'])
        self.assertEqual(result['workers']['ingestion']['state'], 'running')

    async def test_failure_recreates_coroutine_and_stops_at_budget(self):
        calls = []

        async def worker():
            calls.append(1)
            raise RuntimeError('https://secret.invalid/?token=DO_NOT_PUBLISH')

        await self.supervisor.start('source', worker, max_restarts=2)
        state = self.supervisor.summary()
        self.assertEqual(len(calls), 3)
        self.assertEqual(state['workers']['source']['state'], 'failed')
        self.assertEqual(state['workers']['source']['restarts'], 2)
        self.assertFalse(state['ready'])
        self.assertNotIn('DO_NOT_PUBLISH', str(state))
        self.assertNotIn('secret.invalid', str(state))

    async def test_unexpected_normal_exit_is_recovered(self):
        calls = []

        async def worker():
            calls.append(1)

        await self.supervisor.start('source', worker, max_restarts=1)
        self.assertEqual(len(calls), 2)
        self.assertIn('UnexpectedExit', self.supervisor.summary()['workers']['source']['last_error'])

    async def test_recovered_worker_becomes_ready(self):
        calls = []
        entered = asyncio.Event()

        async def worker():
            calls.append(1)
            if len(calls) == 1:
                raise OSError('private path')
            entered.set()
            await asyncio.Event().wait()

        self.supervisor.start('source', worker)
        await entered.wait()
        self.assertTrue(self.supervisor.summary()['ready'])
        self.assertEqual(self.supervisor.summary()['workers']['source']['restarts'], 1)

    async def test_cancel_during_backoff_does_not_restart(self):
        supervisor = RuntimeSupervisor(base_delay=60)
        self.addAsyncCleanup(supervisor.close)
        calls = []

        async def worker():
            calls.append(1)
            raise RuntimeError()

        supervisor.start('source', worker)
        await self.until(lambda: supervisor.summary()['workers']['source']['state'] == 'retrying')
        await supervisor.stop('source')
        await asyncio.sleep(0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(supervisor.summary()['workers']['source']['state'], 'stopped')

    async def test_external_cancellation_before_start_never_restarts(self):
        calls = []

        async def worker():
            calls.append(1)

        task = self.supervisor.start('source', worker)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)
        self.assertEqual(calls, [])
        self.assertEqual(self.supervisor.summary()['workers']['source']['state'], 'stopped')

    async def test_optional_failure_warns_without_hiding_critical_health(self):
        async def critical():
            await asyncio.Event().wait()

        async def optional():
            raise ValueError('secret')

        self.supervisor.start('ingestion', critical)
        await self.supervisor.start('cleanup', optional, restart=False, critical=False)
        state = self.supervisor.summary()
        self.assertTrue(state['ready'])
        self.assertEqual(state['warnings'], ['cleanup'])

    async def test_duplicate_start_and_start_after_close_are_rejected(self):
        async def worker():
            await asyncio.Event().wait()

        self.supervisor.start('source', worker)
        with self.assertRaises(ValueError):
            self.supervisor.start('source', worker)
        await self.supervisor.close()
        self.assertFalse(self.supervisor.summary()['ready'])
        with self.assertRaises(RuntimeError):
            self.supervisor.start('source', worker)

    async def test_one_shot_completes_without_false_failure(self):
        async def worker():
            return

        await self.supervisor.start('one-shot', worker, restart=False, critical=False)
        state = self.supervisor.summary()['workers']['one-shot']
        self.assertEqual(state['state'], 'stopped')
        self.assertIsNone(state['last_error'])
