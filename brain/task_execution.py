"""Host-owned executor routing; both routes share the same Astra receipt checks.

Every currently registered task kind has one execution contract.  Reading,
editing and testing within that contract are Astra's work, not separate host
tools that need another model to forward the request.  The legacy OMP relay is
still available explicitly; it is not advertised as a multi-tool planner.
"""
from __future__ import annotations

import asyncio
import json
import os
from contextlib import suppress
from dataclasses import dataclass

from .agent_rpc import JsonProcess, codex_client, subscription_status


EXECUTION_MODES = frozenset({'auto', 'direct', 'omp'})
SINGLE_EXECUTOR_KINDS = frozenset({'drawing', 'image', 'code', 'research', 'desktop'})


@dataclass(frozen=True)
class ExecutionRoute:
    route: str
    reason: str


def execution_route(job):
    """Route only using validated host metadata, never instructions in text."""
    mode = job.get('execution_mode', 'auto')
    if not isinstance(mode, str) or mode not in EXECUTION_MODES:
        raise ValueError('未知的任务执行方式')
    if not isinstance(job.get('kind'), str) or job['kind'] not in SINGLE_EXECUTOR_KINDS:
        raise ValueError('任务类型没有已注册的执行契约')
    if mode == 'omp':
        return ExecutionRoute('omp', '已明确选择 OMP 兼容调度')
    return ExecutionRoute('direct', '已确定一个 Astra 执行任务，由宿主直接调度')


class TaskExecution:
    async def dispatch(self, job):
        # Dispatch is reached after the manager's permission and resource gate.
        # Keep the state precondition here so a pending viewer request cannot be
        # dispatched by accidentally calling this method directly.
        if job.get('state') != 'running':
            raise ValueError('只有已批准并开始运行的任务可以进入执行器')
        route = execution_route(job)
        self.update(job, execution={'route': route.route, 'reason': route.reason},
                    budget={'model_calls': 0, 'tool_calls': 0,
                            'seconds_limit': self.cfg.timeout_sec})
        folder = self.folder / job['id']
        folder.mkdir(exist_ok=True)
        rpc = await codex_client(self.cfg.codex_command, folder)
        try:
            prior_deepseek = self.health.get('deepseek')
            self.health = await subscription_status(rpc)
            if prior_deepseek is not None:
                self.health['deepseek'] = prior_deepseek
            if job.get('self_upgrade_id'):
                # Policy may have changed while this task was queued.
                self.self_upgrade.authorize_dispatch(job, self.health)
            if not self.health['astra']:
                raise RuntimeError('当前套餐客户端未提供 gpt-6-astra，不切换模型或 API')
            limit = self.health['rateLimits']
            if limit.get('spendControlReached') or any(
                    (limit.get(window) or {}).get('usedPercent', 0) >= 100
                    for window in ('primary', 'secondary')):
                raise RuntimeError('套餐额度已到限，任务暂停；不会自动购买额度或切换 API')
            if route.route == 'direct':
                # execute_astra owns model/tool budgeting, approvals, cancellation,
                # progress, output validation and code review for both routes.
                receipt = await self.execute_astra(rpc, job, folder)
            else:
                receipt = await self.dispatch_omp(rpc, job, folder)
            # An execution cancellation/state change must not be overwritten by
            # a late receipt.  No route may replace a host decision with prose.
            if job.get('state') != 'running':
                return
            self.update(job, **receipt,
                        state='review' if job['kind'] == 'code' else 'succeeded',
                        detail='已生成草稿，尚未部署' if job['kind'] == 'code'
                        else '执行回执与产物已收集')
        finally:
            await rpc.close()

    async def dispatch_omp(self, rpc, job, folder):
        """Explicit legacy relay with one fixed, host-authorized tool only."""
        from .agent_health import probe, dispatch_env
        connection = await probe(self.llm)
        self.health['deepseek'] = connection
        if not connection['ok']:
            raise RuntimeError(connection['detail'])
        if not self.llm.api_key:
            raise RuntimeError('未配置 DeepSeek 调度会话密钥')
        env = dispatch_env(dict(os.environ), self.llm, connection)
        env['PI_CODING_AGENT_DIR'] = str(self.folder / 'omp-profile')
        env['DEEPSEEK_API_KEY'] = self.llm.api_key
        omp = JsonProcess([str(self.root / self.cfg.omp_command), '--mode', 'rpc',
            '--no-session', '--no-tools', '--no-extensions', '--no-skills', '--no-lsp',
            '--model', 'deepseek/deepseek-flash', '--thinking', 'off',
            '--system-prompt', '你是助手的后台调度会话。对任务调用一次 astra_execute。'
            '只有工具回执才是执行结果，不能假装操作成功。'], folder, env)
        try:
            await omp.start()
            ready = await asyncio.wait_for(omp.events.get(), 40)
            if ready.get('type') != 'ready':
                raise RuntimeError('OMP 未返回就绪协议')
            await omp.call('set_host_tools', {'tools': [{
                'name': 'astra_execute', 'label': 'Astra 套餐执行', 'loadMode': 'essential',
                'description': '执行用户已经提交的原始任务；权限和任务内容由主机固定，不能扩大。',
                'parameters': {'type': 'object', 'properties': {},
                               'additionalProperties': False}}]}, omp=True)
            await omp.call('set_auto_retry', {'enabled': False}, omp=True)
            self.update(job, detail='DeepSeek 正在安排任务')
            self.spend(job, 'model_calls')
            await omp.call('prompt', {'message': job['prompt']}, omp=True)
            receipt = None
            dispatch_retry = False
            dispatch_error = ''
            while True:
                msg = await asyncio.wait_for(omp.events.get(), self.cfg.timeout_sec)
                if msg.get('type') == 'message_end':
                    message = msg.get('message') or {}
                    if message.get('stopReason') == 'error':
                        dispatch_error = str(message.get('errorMessage') or '调度会话模型请求失败').replace(
                            self.llm.api_key, '[redacted]')[:600]
                if msg.get('type') == 'host_tool_call':
                    if msg.get('toolName') != 'astra_execute' or receipt is not None:
                        self.spend(job, 'model_calls')
                        await omp.send({'type': 'host_tool_result', 'id': msg['id'], 'isError': True,
                                        'result': {'content': [{'type': 'text', 'text': '不允许重复执行或未知工具'}]}})
                        continue
                    receipt = await self.execute_astra(rpc, job, folder)
                    self.spend(job, 'model_calls')
                    await omp.send({'type': 'host_tool_result', 'id': msg['id'], 'result': {
                        'content': [{'type': 'text', 'text': json.dumps(receipt, ensure_ascii=False)}]}})
                elif msg.get('type') == 'agent_end':
                    if receipt is None:
                        if not dispatch_retry and self.cfg.max_dispatch_retries > 0:
                            dispatch_retry = True
                            self.spend(job, 'model_calls')
                            self.update(job, detail='调度会话未调用执行器，重试调度一次')
                            await omp.call('prompt', {'message': '任务尚未执行。现在必须调用已注册的 '
                                'astra_execute 工具一次，参数为 {}。不要只用文字回答。'}, omp=True)
                            continue
                        raise RuntimeError('调度会话没有产生实际执行调用，不计为成功' +
                                           (': ' + dispatch_error if dispatch_error else ''))
                    return receipt
                elif msg.get('type') == 'transport_closed' or (
                        msg.get('type') == 'response' and msg.get('success') is False):
                    raise RuntimeError('OMP 调度中断，未报告成功')
        finally:
            with suppress(Exception):
                await omp.call('abort', omp=True, timeout=3)
            await omp.close()
