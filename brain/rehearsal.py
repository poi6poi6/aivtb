"""An isolated host rehearsal; browser receipts never stand in for listening checks."""
from __future__ import annotations

import asyncio
import copy
import time
import uuid


class ProbeReport:
    def __init__(self, service, run_id):
        self._service, self.run_id = service, run_id

    def __call__(self, stage, state, detail=''):
        return self._service.report(self.run_id, stage, state, detail)

    def bind_turn(self, turn_id):
        return self._service.bind_turn(self.run_id, turn_id)

    async def wait_for_playback(self):
        return await self._service.wait_for_playback(self.run_id)


class RehearsalService:
    def __init__(self, run_probe=None, *, can_start=lambda: True, danmaku_status=None,
                 timeout_sec=90, playback_timeout_sec=30, clock=time.time):
        self.run_probe, self.can_start, self.danmaku_status = run_probe, can_start, danmaku_status
        self.timeout_sec, self.playback_timeout_sec = timeout_sec, playback_timeout_sec
        self.clock = clock
        self._task = None
        self._result = None
        self._turn_id = None
        self._used_turns = set()
        self._playback_done = None
        self._started = False
        self._playback_failed = False
        self._closed = False

    @property
    def running(self):
        return self._task is not None and not self._task.done()

    def snapshot(self):
        if self._result is None:
            return {'available': self.run_probe is not None and not self._closed,
                    'running': False, 'run_id': None, 'state': 'idle',
                    'automatic_passed': False, 'full_passed': False, 'steps': {}}
        return dict(copy.deepcopy(self._result), running=self.running,
                    available=self.run_probe is not None and not self._closed)

    async def start(self):
        if self._closed or self.run_probe is None:
            raise ValueError('开播演练尚未就绪')
        if self.running:
            raise ValueError('演练仍在运行或停止中，请等待本轮结束')
        allowed = self.can_start()
        if allowed is not True:
            raise ValueError(allowed if isinstance(allowed, str) else '当前有正式内容正在处理，请稍后再试')
        run_id = uuid.uuid4().hex
        self._result = {'run_id': run_id, 'state': 'running', 'started_at': self.clock(),
                        'finished_at': None, 'automatic_passed': False, 'full_passed': False,
                        'message': '正在检查开播链路',
                        'steps': {name: {'state': 'pending', 'detail': ''} for name in
                                  ('danmaku', 'llm', 'tts', 'playback', 'microphone', 'obs_audio')}}
        self._turn_id = None
        self._started = self._playback_failed = False
        self._playback_done = asyncio.Event()
        self._task = asyncio.create_task(self._run(run_id), name='broadcast-rehearsal')
        return self.snapshot()

    def _active(self, run_id):
        return (self._result is not None and self._result['run_id'] == run_id
                and self._result['state'] == 'running' and not self._closed)

    def report(self, run_id, stage, state, detail=''):
        if not self._active(run_id):
            return False
        if stage not in {'llm', 'tts'} or state not in {'running', 'passed', 'failed'}:
            raise ValueError('演练阶段或状态无效')
        if not isinstance(detail, str) or len(detail) > 240:
            raise ValueError('演练说明须为 240 字以内的安全摘要')
        self._result['steps'][stage] = {'state': state, 'detail': detail}
        return True

    def bind_turn(self, run_id, turn_id):
        if not self._active(run_id):
            raise ValueError('演练已失效')
        if type(turn_id) is not int or turn_id <= 0 or turn_id in self._used_turns:
            raise ValueError('演练语音编号无效或已使用')
        if self._turn_id is not None:
            raise ValueError('本轮演练已经绑定语音')
        self._turn_id = turn_id
        self._used_turns.add(turn_id)
        self._result['steps']['playback'] = {'state': 'running', 'detail': '等待当前主播放页开始与完成回执'}
        return True

    def audio_started(self, turn_id):
        if (not self._result or not self._active(self._result['run_id'])
                or type(turn_id) is not int or turn_id != self._turn_id
                or self._playback_failed or self._playback_done.is_set()):
            return False
        self._started = True
        self._result['steps']['playback'] = {'state': 'running', 'detail': '已收到主播放页开始回执，等待播放完成'}
        return True

    def audio_finished(self, turn_id, *, completed=True, reason=''):
        if (not self._result or not self._active(self._result['run_id'])
                or type(turn_id) is not int or turn_id != self._turn_id
                or self._playback_done.is_set()):
            return False
        passed = completed is True and self._started
        self._playback_failed = not passed
        self._result['steps']['playback'] = {
            'state': 'passed' if passed else 'failed',
            'detail': '主播放页已回报完整播放；实际听感仍需人工确认' if passed else
                      '演练未完整播放，或未收到对应的开始回执'}
        self._playback_done.set()
        return True

    def playback_disconnected(self):
        if not self._result or not self._active(self._result['run_id']):
            return False
        # Losing the authoritative player before/during a run invalidates this attempt,
        # even when another tab reconnects and sends a late finish receipt.
        self._playback_failed = True
        self._result['steps']['playback'] = {'state': 'failed', 'detail': '主播放页已断开，请重新演练'}
        self._playback_done.set()
        return True

    async def wait_for_playback(self, run_id):
        if not self._active(run_id) or self._turn_id is None:
            raise ValueError('演练播放等待已失效')
        await asyncio.wait_for(self._playback_done.wait(), self.playback_timeout_sec)
        if self._playback_failed:
            raise ValueError('演练没有完整播放')
        return True

    def _danmaku(self):
        status = self.danmaku_status() if self.danmaku_status else {'state': 'disabled'}
        if not isinstance(status, dict):
            raise ValueError('弹幕状态不可用')
        state = status.get('state')
        if status.get('enabled') is False or state == 'disabled':
            return {'state': 'skipped', 'detail': '当前未启用直播弹幕，本轮不验证弹幕接入'}
        heartbeat = status.get('last_heartbeat', 0)
        fresh = isinstance(heartbeat, (int, float)) and 0 <= self.clock() - heartbeat <= 90
        if state == 'connected' and fresh:
            return {'state': 'passed', 'detail': '直播弹幕连接在线，已收到近期心跳；未发送测试弹幕'}
        return {'state': 'failed', 'detail': '直播弹幕未连接或心跳已过期，请检查直播间连接'}

    async def _run(self, run_id):
        try:
            self._result['steps']['danmaku'] = self._danmaku()
            await asyncio.wait_for(self.run_probe(ProbeReport(self, run_id)), self.timeout_sec)
            for name in ('llm', 'tts'):
                if self._result['steps'][name]['state'] != 'passed':
                    raise ValueError('模型或语音阶段没有有效成功结果')
            if self._turn_id is None:
                raise ValueError('演练未绑定主播放页语音')
            await asyncio.wait_for(self._playback_done.wait(), self.playback_timeout_sec)
            if self._playback_failed or self._result['steps']['danmaku']['state'] == 'failed':
                self._result.update(state='failed', message='自动检查未全部通过，请修复后重新演练')
            else:
                self._result.update(state='awaiting_confirmation', automatic_passed=True,
                                    message='自动检查已完成，请人工检查麦克风听感与 OBS 声音')
                for name in ('microphone', 'obs_audio'):
                    self._result['steps'][name] = {'state': 'pending', 'detail': '等待主播人工确认，系统未自动检测'}
        except asyncio.CancelledError:
            self._result.update(state='cancelled', message='演练已取消，本轮不能作为开播通过依据',
                                automatic_passed=False, full_passed=False)
            raise
        except asyncio.TimeoutError:
            self._result.update(state='failed', message='演练等待超时，请检查当前未完成的阶段',
                                automatic_passed=False, full_passed=False)
            for name in ('llm', 'tts', 'playback'):
                if self._result['steps'][name]['state'] in {'pending', 'running'}:
                    self._result['steps'][name] = {'state': 'failed', 'detail': '等待超时，未确认成功'}
        except Exception as error:
            self._result.update(state='failed', message=f'{type(error).__name__}：演练未完成，请检查模型、语音和播放页',
                                automatic_passed=False, full_passed=False)
        finally:
            self._result['finished_at'] = self.clock()

    async def cancel(self, run_id):
        self._require_latest(run_id)
        if self.running:
            self._result['state'] = 'cancelling'
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            # A task cancelled before its first instruction never runs its finally.
            self._result.update(state='cancelled', message='演练已取消', finished_at=self.clock(),
                                automatic_passed=False, full_passed=False)
        return self.snapshot()

    def _require_latest(self, run_id):
        if not isinstance(run_id, str) or not self._result or self._result['run_id'] != run_id:
            raise ValueError('演练结果已更新，请刷新后操作最新一轮')

    def confirm(self, run_id, field, passed):
        self._require_latest(run_id)
        if field not in {'microphone', 'obs_audio'} or type(passed) is not bool:
            raise ValueError('人工检查项目或结果无效')
        if self.running or not self._result['automatic_passed']:
            raise ValueError('请先完成本轮自动演练，再进行人工确认')
        self._result['steps'][field] = {'state': 'passed' if passed else 'failed',
                                        'detail': '主播人工确认正常' if passed else '主播人工检查发现问题',
                                        'confirmed_at': self.clock()}
        manual = [self._result['steps'][name]['state'] for name in ('microphone', 'obs_audio')]
        full = all(state == 'passed' for state in manual)
        self._result.update(full_passed=full,
                            state='passed' if full else 'needs_attention' if 'failed' in manual else 'awaiting_confirmation',
                            message='本轮已启用链路演练通过（含主播人工确认）' if full else
                                    '人工检查发现问题，请处理后重新确认' if 'failed' in manual else
                                    '仍有人工检查尚未确认，不能视为完整通过')
        return self.snapshot()

    async def close(self):
        self._closed = True
        if self.running:
            await self.cancel(self._result['run_id'])
