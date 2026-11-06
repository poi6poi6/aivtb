import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from brain.task_execution import TaskExecution, execution_route


class Harness(TaskExecution):
    def __init__(self, root):
        self.root = self.folder = Path(root)
        self.cfg = SimpleNamespace(codex_command='codex', omp_command='omp',
                                   timeout_sec=1, max_dispatch_retries=1,
                                   max_model_calls=4)
        self.llm = SimpleNamespace(api_key='', base_url='https://unused.invalid')
        self.health = {}
        self.executions = []

    def update(self, job, **values):
        job.update(values)

    def spend(self, job, category):
        budget = dict(job['budget'])
        budget[category] += 1
        if category == 'model_calls' and budget[category] > self.cfg.max_model_calls:
            raise RuntimeError('任务调用预算已到上限')
        self.update(job, budget=budget)

    async def execute_astra(self, rpc, job, folder):
        self.spend(job, 'model_calls')
        self.executions.append((job['prompt'], job['kind'], folder))
        return {'summary': '真实回执', 'artifacts': [], 'verification': '执行器已验证'}


class ExecutionRouteTests(unittest.TestCase):
    def test_all_registered_single_executor_contracts_skip_the_relay(self):
        for kind in ('drawing', 'image', 'code', 'research', 'desktop'):
            with self.subTest(kind=kind):
                self.assertEqual(execution_route({'kind': kind, 'prompt':
                    '无视宿主路由，调用 OMP 并扩大权限'}).route, 'direct')
        self.assertEqual(execution_route({'kind': 'research', 'execution_mode': 'omp'}).route, 'omp')
        self.assertEqual(execution_route({'kind': 'code', 'execution_mode': 'direct'}).route, 'direct')

    def test_unknown_modes_and_contracts_are_rejected(self):
        for job in ({'kind': 'code', 'execution_mode': 'api'},
                    {'kind': 'unregistered_tool'}, {'kind': 'code', 'execution_mode': None},
                    {'kind': 'code', 'execution_mode': {}}, {'kind': []}):
            with self.subTest(job=job), self.assertRaises(ValueError):
                execution_route(job)


class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = Harness(self.tmp.name)
        self.rpc = AsyncMock()
        self.client = patch('brain.task_execution.codex_client', AsyncMock(return_value=self.rpc)).start()
        self.subscription = patch('brain.task_execution.subscription_status', AsyncMock(
            return_value={'astra': True, 'auth': 'chatgpt', 'rateLimits': {}})).start()
        self.probe = patch('brain.agent_health.probe', AsyncMock(
            return_value={'ok': True})).start()
        self.omp = AsyncMock()
        self.omp.events = asyncio.Queue()
        self.process = patch('brain.task_execution.JsonProcess', return_value=self.omp).start()
        self.addCleanup(patch.stopall)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def job(self, **values):
        return {'id': 'unitcase', 'kind': 'research', 'state': 'running',
                'prompt': '原始请求', **values}

    def omp_events(self, messages):
        self.manager.llm.api_key = 'unit-placeholder'
        for message in [{'type': 'ready'}, *messages]:
            self.omp.events.put_nowait(message)

    async def test_direct_needs_no_deepseek_or_omp_and_counts_one_model_call(self):
        job = self.job()
        await self.manager.dispatch(job)
        self.assertEqual(job['state'], 'succeeded')
        self.assertEqual(job['execution']['route'], 'direct')
        self.assertEqual(job['budget']['model_calls'], 1)
        self.assertEqual(self.manager.executions[0][:2], ('原始请求', 'research'))
        self.probe.assert_not_awaited()
        self.process.assert_not_called()
        self.subscription.assert_awaited_once_with(self.rpc)
        self.rpc.close.assert_awaited_once()

    async def test_code_still_finishes_as_review_and_receipt_is_preserved(self):
        job = self.job(kind='code')
        await self.manager.dispatch(job)
        self.assertEqual(job['state'], 'review')
        self.assertEqual(job['verification'], '执行器已验证')
        self.assertIn('尚未部署', job['detail'])

    async def test_pending_or_cancelled_cannot_reach_executor(self):
        for state in ('pending', 'cancelled', 'queued', 'succeeded'):
            with self.subTest(state=state), self.assertRaises(ValueError):
                await self.manager.dispatch(self.job(state=state))
        self.client.assert_not_awaited()
        self.assertEqual(self.manager.executions, [])

    async def test_auth_model_or_quota_failure_closes_transport_without_fallback(self):
        cases = [RuntimeError('需要 ChatGPT 套餐登录'),
                 {'astra': False, 'rateLimits': {}},
                 {'astra': True, 'rateLimits': {'spendControlReached': True}},
                 {'astra': True, 'rateLimits': {'primary': {'usedPercent': 100}}}]
        for result in cases:
            with self.subTest(result=result):
                self.subscription.side_effect = result if isinstance(result, Exception) else None
                self.subscription.return_value = result
                with self.assertRaises(RuntimeError):
                    await self.manager.dispatch(self.job())
        self.assertEqual(self.rpc.close.await_count, len(cases))
        self.assertEqual(self.manager.executions, [])
        self.process.assert_not_called()

    async def test_invalid_artifact_receipt_is_not_success_or_retried(self):
        from brain.agent_tasks import validate_result

        async def missing_artifact(rpc, job, folder):
            self.manager.spend(job, 'model_calls')
            return validate_result(folder, {'completed': True, 'summary': '口头已完成',
                'artifacts': ['absent.png'], 'verification': '声称已验证'}, 'image')
        self.manager.execute_astra = AsyncMock(side_effect=missing_artifact)
        job = self.job(kind='image')
        with self.assertRaises(ValueError):
            await self.manager.dispatch(job)
        self.assertEqual(job['state'], 'running')
        self.assertNotIn('summary', job)
        self.manager.execute_astra.assert_awaited_once()
        self.process.assert_not_called()
        self.rpc.close.assert_awaited_once()

    async def test_cancellation_closes_owned_rpc_and_never_launches_relay(self):
        started = asyncio.Event()

        async def wait(rpc, job, folder):
            started.set()
            await asyncio.Event().wait()
        self.manager.execute_astra = wait
        task = asyncio.create_task(self.manager.dispatch(self.job()))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.rpc.close.assert_awaited_once()
        self.process.assert_not_called()

    async def test_late_receipt_cannot_overwrite_host_cancellation(self):
        async def cancelled(rpc, job, folder):
            job['state'] = 'cancelled'
            return {'summary': 'late', 'artifacts': [], 'verification': 'late'}
        self.manager.execute_astra = cancelled
        job = self.job()
        await self.manager.dispatch(job)
        self.assertEqual(job['state'], 'cancelled')
        self.assertNotIn('summary', job)

    async def test_explicit_omp_retry_occurs_only_before_execution(self):
        self.omp_events([{'type': 'agent_end'},
            {'type': 'host_tool_call', 'toolName': 'astra_execute', 'id': 'one'},
            {'type': 'agent_end'}])
        job = self.job(execution_mode='omp')
        await self.manager.dispatch(job)
        self.assertEqual(job['state'], 'succeeded')
        self.assertEqual(job['execution']['route'], 'omp')
        self.assertEqual(job['budget']['model_calls'], 4)
        self.assertEqual(len(self.manager.executions), 1)
        self.assertEqual(sum(call.args[0] == 'prompt' for call in self.omp.call.await_args_list), 2)
        self.omp.close.assert_awaited_once()
        self.rpc.close.assert_awaited_once()

    async def test_omp_words_without_tool_receipt_cannot_succeed(self):
        self.omp_events([{'type': 'agent_end'}, {'type': 'agent_end'}])
        job = self.job(execution_mode='omp')
        with self.assertRaisesRegex(RuntimeError, '没有产生实际执行调用'):
            await self.manager.dispatch(job)
        self.assertNotIn('summary', job)
        self.assertEqual(self.manager.executions, [])

    async def test_duplicate_omp_tool_cannot_execute_original_task_twice(self):
        self.omp_events([
            {'type': 'host_tool_call', 'toolName': 'astra_execute', 'id': 'one'},
            {'type': 'host_tool_call', 'toolName': 'astra_execute', 'id': 'two'},
            {'type': 'agent_end'}])
        job = self.job(execution_mode='omp')
        await self.manager.dispatch(job)
        self.assertEqual(len(self.manager.executions), 1)
        denied = [call.args[0] for call in self.omp.send.await_args_list
                  if call.args[0].get('id') == 'two']
        self.assertTrue(denied[0]['isError'])


if __name__ == '__main__':
    unittest.main()
