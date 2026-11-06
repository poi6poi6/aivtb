"""Isolated offline console demo; never loads live settings, credentials or models.

Run ``python -m brain.local_test``. Text is scripted and audio is a quiet tone,
while the browser, WebSocket ownership and playback acknowledgements are real.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import io
import itertools
import math
import os
from pathlib import Path
import struct
import tempfile
import time
import wave
import webbrowser

import aiohttp
from aiohttp import web

from .arbiter import Arbiter
from .config import ROOT, PlaybackConfig, WebUIConfig
from .events import Event, PRIO_CHAT
from .metrics import RuntimeMetrics
from .playback import SpeechController, SpeechItem
from .webui import STATIC_DIR, WebUI

APP_ID = 'ai-vtuber-local-test'
DEFAULT_PORT = 18080
WORKSPACE_ID = hashlib.sha256(str(ROOT.resolve()).encode()).hexdigest()[:20]
SCENARIOS = frozenset({'slow', 'reply_error', 'tts_error'})
LOCAL_ACTIONS = frozenset({
    'inject', 'conversation_interrupt', 'audio_started', 'audio_finished',
    'audio_progress', 'playback_skip', 'playback_pause', 'playback_clear',
    'set_manual_mode',
})


def _plain_directory(path: Path) -> Path:
    """Reject existing redirected ancestors before creating any test files."""
    path = path.absolute()
    for parent in (path, *path.parents):
        if parent.exists() or parent.is_symlink():
            stat = parent.lstat()
            if parent.is_symlink() or getattr(stat, 'st_file_attributes', 0) & 0x400:
                raise ValueError('离线测试目录不能是符号链接或目录联接')
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve(strict=True)


def prepare_tone(data_root: Path) -> Path:
    """Write one reusable 1.2s PCM tone, without voice engines or recordings."""
    audio = _plain_directory(data_root / 'audio')
    target = audio / 'test-tone.wav'
    rate, duration = 16000, 1.2
    frames = bytearray()
    for index in range(int(rate * duration)):
        seconds = index / rate
        envelope = min(1.0, seconds / .03, (duration - seconds) / .08)
        # Quiet two-note cue. It is deliberately not presented as character TTS.
        frequency = 440 if seconds < .6 else 554.37
        sample = int(1800 * envelope * math.sin(2 * math.pi * frequency * seconds))
        frames.extend(struct.pack('<h', sample))
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(frames)
    descriptor, temporary = tempfile.mkstemp(prefix='tone-', suffix='.tmp', dir=audio)
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(buffer.getvalue())
        # Atomic replacement does not follow a pre-existing file symlink/hardlink.
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return target


class LocalTestWebUI(WebUI):
    """Reuse production transport/playback, with a closed set of demo routes."""

    def __init__(self, *, port=DEFAULT_PORT, data_root=None, response_delay=.35, slow_delay=4.0):
        self.data_root = _plain_directory(Path(data_root) if data_root is not None
                                          else ROOT / 'data' / 'local-test')
        self.tone = prepare_tone(self.data_root)
        self.ids = itertools.count(1)
        self.workspace_id = WORKSPACE_ID
        self.response_delay = response_delay
        self.slow_delay = slow_delay
        self.active = None
        self.workers = []
        self._closing = False
        self._clearing = False
        self._clear_lock = asyncio.Lock()
        self._generating_turns = set()
        self._synthesizing_turns = set()
        self.arbiter = Arbiter(danmaku_cooldown_sec=0, max_pending=32)
        self.confirmed_playbacks = 0
        manual = asyncio.Event()
        manual.set()
        super().__init__(
            WebUIConfig(host='127.0.0.1', port=port), asyncio.Queue(maxsize=32),
            str(self.tone.parent), {
                'local_test': True, 'llm': '离线脚本回复', 'tts': 'local-tone',
                'focus': '离线控制台测试', 'bilibili': False, 'search': False,
                'local_test_audio': '1.2 秒低音量提示音，不是角色语音',
                'local_test_data': str(self.data_root),
            }, manual_mode=manual,
            playback=SpeechController(PlaybackConfig(max_queue=3, max_age_sec=120)),
            metrics=RuntimeMetrics(), workspace_root=self.data_root,
        )
        self.allowed_ws_actions = LOCAL_ACTIONS
        self.conversation_status = lambda: {
            'generating': bool(self._generating_turns),
            'generating_turn_ids': sorted(self._generating_turns),
            'synthesizing_turn_ids': sorted(self._synthesizing_turns),
        }
        self.runtime_health = {'scope': 'local_test', 'ready': True, 'local_test': True}
        self.playback.on_delivery = self._delivered

    def make_app(self):
        @web.middleware
        async def local_only(request, handler):
            self._tasks_access(request, write=request.method not in {'GET', 'HEAD'})
            response = await handler(request)
            response.headers['Cache-Control'] = 'no-store'
            response.headers['Permissions-Policy'] = 'microphone=(), camera=()'
            return response

        app = web.Application(middlewares=[local_only], client_max_size=16384)
        app.router.add_get('/', self._index)
        app.router.add_get('/ws', self._ws)
        app.router.add_get('/audio/{name}', self._audio)
        app.router.add_get('/local-test-status', self._local_status)
        app.router.add_get('/runtime-health', self._local_status)
        app.router.add_get('/experience-report', self._local_experience_report)
        app.router.add_get('/bilibili-status', self._offline_bilibili)
        app.router.add_post('/local-test-scenario', self._scenario)
        app.router.add_static('/static/', STATIC_DIR, show_index=False)
        if (ROOT / 'skin').is_dir():
            app.router.add_static('/skin/', ROOT / 'skin', show_index=False)
        app.on_startup.append(self._start_workers)
        app.on_cleanup.append(self._stop_workers)
        return app

    async def start(self):
        self._runner = web.AppRunner(self.make_app())
        await self._runner.setup()
        await web.TCPSite(self._runner, '127.0.0.1', self.cfg.port).start()

    async def _start_workers(self, app):
        self.workers = [asyncio.create_task(self._receive_events(), name='local-test-events'),
                        asyncio.create_task(self._schedule(), name='local-test-playback')]

    async def _stop_workers(self, app):
        self._closing = True
        for task in self.workers:
            task.cancel()
        await asyncio.gather(*self.workers, return_exceptions=True)
        self.workers.clear()
        await self._cancel_active()

    async def _local_status(self, request):
        return web.json_response({
            'app': APP_ID, 'local_test': True, 'workspace_id': self.workspace_id,
            'ready': not self._closing and all(not worker.done() for worker in self.workers),
            'data_root': str(self.data_root), 'audio': 'local-tone',
            'playback': self.playback.summary(), 'metrics': self.metrics.summary(),
            'confirmed_playbacks': self.confirmed_playbacks,
            'message': '离线脚本与提示音；重启清空对话，提示音可复用。',
        })

    async def _offline_bilibili(self, request):
        return web.json_response({'state': 'disabled', 'room_id': 0, 'local_test': True,
                                  'message': '离线测试，不连接直播间'})

    async def _local_experience_report(self, request):
        return web.json_response({
            'version': 1, 'local_test': True, 'recorded_at': time.time(),
            'metrics': self.metrics.summary(), 'confirmed_playbacks': self.confirmed_playbacks,
            'measurement': '离线脚本与测试提示音；播放完成依据浏览器回执，未测量扬声器声音。',
            'limitations': '没有调用真实语言模型、语音合成或语音识别；不代表这些服务的性能。',
        }, headers={'Content-Disposition': 'attachment; filename="local-test-experience.json"'})

    async def _scenario(self, request):
        try:
            data = await request.json()
            if not isinstance(data, dict) or data.get('scenario') not in SCENARIOS:
                raise ValueError('请选择 slow、reply_error 或 tts_error 离线场景')
            scenario = data['scenario']
            event = Event('system', 'chat', {
                'slow': '演示等待回复后的恢复流程',
                'reply_error': '演示模型回复失败',
                'tts_error': '演示语音生成失败',
            }[scenario], user='离线场景', priority=PRIO_CHAT, forced=True)
            event.ui_id = next(self.ids)
            event.local_test_scenario = scenario
            self.event_queue.put_nowait(event)
            return web.json_response({'accepted': True, 'turn_id': event.ui_id,
                                      'scenario': scenario, 'local_test': True})
        except (ValueError, TypeError):
            return web.json_response({'error': '请选择有效的离线场景'}, status=400)
        except asyncio.QueueFull:
            return web.json_response({'error': '离线事件队列已满，请先清空或稍后再试'}, status=429)

    async def _inject(self, data, *, owner=False):
        if not isinstance(data.get('text'), str) or len(data['text']) > 600:
            return
        if data.get('confirm_memory'):
            await self.broadcast({'type': 'memory_confirmation', 'ok': False,
                                  'text': '离线测试不保存观众记忆'})
            return
        await super()._inject(data, owner=owner)

    def _delivered(self, item, completed):
        if completed:
            self.confirmed_playbacks += 1

    async def _cancel_active(self):
        if self.active is not None:
            active = self.active
            self.active = None
            active.cancel()
            await asyncio.gather(active, return_exceptions=True)

    async def _clear_pending(self):
        async with self._clear_lock:
            self._clearing = True
            try:
                await self._cancel_active()
                for pending in self.arbiter.pending_turn_ids():
                    self.metrics.record_cancelled(pending)
                    await self.broadcast({'type': 'reply_cancelled', 'id': pending})
                self.arbiter = Arbiter(danmaku_cooldown_sec=0, max_pending=32)
                await self._broadcast_many(self.playback.clear())
            finally:
                self._clearing = False

    async def _playback_clear(self):
        await self._clear_pending()
        while not self.event_queue.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                self.event_queue.get_nowait()
        await self.broadcast({'type': 'arbiter', **self.arbiter.summary()})

    async def _receive_events(self):
        while True:
            event = await self.event_queue.get()
            if event.kind == 'interrupt':
                await self._clear_pending()
                await self.broadcast({'type': 'arbiter', **self.arbiter.summary()})
                continue
            if event.source in {'voice', 'companion'}:
                await self._clear_pending()
            if event.corrects is not None and event.retract:
                await self.broadcast({'type': 'voice_retracted', 'id': event.corrects})
                continue
            if not hasattr(event, 'ui_id'):
                event.ui_id = next(self.ids)
            self.metrics.record_event(event.ui_id)
            await self.broadcast({'type': 'event', 'id': event.ui_id,
                                  'source': event.source, 'kind': event.kind, 'text': event.text,
                                  'user': event.user, 'priority': event.priority,
                                  'corrects': event.corrects, 'local_test': True})
            if event.resume_playback:
                await self._broadcast_many(self.playback.set_paused(False))
            self.arbiter.push(event)
            await self.broadcast({'type': 'arbiter', **self.arbiter.summary()})

    async def _schedule(self):
        while True:
            await self._broadcast_many(self.playback.tick())
            if self.active is not None and self.active.done():
                # Retrieve failures; do not leave an unobserved task exception.
                self.active.result()
                self.active = None
            if self.active is None and not self.playback.paused and not self._clearing:
                event = self.arbiter.pick(allow_idle=False)
                if event is not None:
                    self.active = asyncio.create_task(self._reply(event))
                    await self.broadcast({'type': 'arbiter', **self.arbiter.summary()})
            await asyncio.sleep(.04)

    async def _reply(self, event):
        turn_id = event.ui_id
        self._generating_turns.add(turn_id)
        scenario = getattr(event, 'local_test_scenario', '')
        started = time.perf_counter()
        try:
            await self.broadcast({'type': 'thinking', 'id': turn_id})
            await asyncio.sleep(self.response_delay)
            if scenario == 'slow':
                await self.broadcast({'type': 'broadcast_notice', 'text': '离线模拟：回复仍在等待，稍后继续。'})
                await asyncio.sleep(self.slow_delay)
            if scenario == 'reply_error':
                self.metrics.record_error(turn_id)
                await self.broadcast({'type': 'reply_error', 'id': turn_id,
                                      'error': '离线模拟：模型请求失败。真实模型未被调用。'})
                return
            text = ('【离线脚本】收到这条礼物事件，谢谢你的测试。'
                    if event.kind == 'gift' else
                    '【离线脚本】已收到：' + event.text[:100] + '。接下来播放测试提示音。')
            self.metrics.record_first_sentence(turn_id)
            await self.broadcast({'type': 'reply_partial', 'id': turn_id, 'text': text,
                                  'emotion': '平静', 'actions': [], 'elapsed': 0})
            latency = time.perf_counter() - started
            self.metrics.record_reply(turn_id, latency=latency, llm_ttft=0)
            await self.broadcast({'type': 'reply', 'id': turn_id, 'text': text,
                                  'emotion': '平静', 'actions': [], 'mode': 'local_test',
                                  'latency': latency, 'ttft': 0, 'local_test': True})
            self._generating_turns.discard(turn_id)
            self._synthesizing_turns.add(turn_id)
            await self.broadcast({'type': 'speech_stage', 'turn_id': turn_id, 'stage': 'synthesizing'})
            if scenario == 'tts_error':
                self.metrics.record_error(turn_id)
                await self.broadcast({'type': 'tts_error', 'turn_id': turn_id,
                                      'error': '离线模拟：语音生成失败。真实语音引擎未被调用。'})
                return
            while not self.playback.has_capacity(event.priority):
                await asyncio.sleep(.05)
            await self._broadcast_many(self.playback.enqueue(SpeechItem(
                id=turn_id, turn_id=turn_id, segment_index=0,
                url='/audio/test-tone.wav', text=text, emotion='平静', actions=[],
                event={'source': event.source, 'kind': event.kind, 'text': event.text,
                       'user': event.user, 'local_test': True}, priority=event.priority,
            )))
        except asyncio.CancelledError:
            self.metrics.record_cancelled(turn_id)
            await self.broadcast({'type': 'reply_cancelled', 'id': turn_id})
            raise
        finally:
            self._generating_turns.discard(turn_id)
            self._synthesizing_turns.discard(turn_id)
            await self.broadcast({'type': 'speech_stage', 'turn_id': turn_id, 'stage': 'drained'})
            await self.broadcast({'type': 'metrics', **self.metrics.summary()})


async def existing_instance(port):
    """Only query a loopback URL, ignoring proxy environment and redirects."""
    try:
        async with aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=1)) as client:
            async with client.get(f'http://127.0.0.1:{port}/local-test-status', allow_redirects=False) as response:
                if response.status != 200:
                    return False
                data = await response.json()
                return (isinstance(data, dict) and data.get('app') == APP_ID
                        and data.get('local_test') is True and data.get('ready') is True
                        and data.get('workspace_id') == WORKSPACE_ID)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return False


async def run(port=DEFAULT_PORT, *, open_browser=True):
    url = f'http://127.0.0.1:{port}/?console=1'
    if await existing_instance(port):
        print(f'[offline] Existing isolated test console: {url}', flush=True)
        if open_browser:
            await asyncio.to_thread(webbrowser.open, url)
        return
    ui = LocalTestWebUI(port=port)
    try:
        await ui.start()
    except OSError:
        await ui.close()
        raise RuntimeError(f'Port {port} is occupied. Existing services were not stopped. '
                           'Choose another port with --port 18081.') from None
    print(f'[offline] Console: {url}', flush=True)
    print('[offline] Scripted replies + 1.2 second test tone; no live models, TTS or Bilibili.', flush=True)
    print(f'[offline] Data: {ui.data_root}; conversations reset on restart. Ctrl+C to stop.', flush=True)
    try:
        if open_browser:
            await asyncio.to_thread(webbrowser.open, url)
        await asyncio.Event().wait()
    finally:
        await ui.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Isolated offline AI-VTuber console (no live services).')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--validate-only', action='store_true', help='Check imports/options without writing data or starting services')
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error('--port must be between 1024 and 65535')
    if args.port in {8080, 8085, 9880, 9890}:
        parser.error('Choose a dedicated offline port; 8080/8085/9880/9890 are reserved for live services')
    if args.validate_only:
        print(f'[offline] Validation OK. UI=http://127.0.0.1:{args.port}/?console=1')
        return 0
    try:
        asyncio.run(run(args.port, open_browser=not args.no_browser))
    except KeyboardInterrupt:
        print('\n[offline] Stopped. Live services were not changed.')
    except (RuntimeError, OSError, ValueError) as exc:
        print(f'[offline] Startup failed: {exc}')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
