"""Trusted music-request narration using the normal voice and playback receipts."""
from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass

from .events import Event
from .speech_text import DEFAULT_EMOTIONS


@dataclass
class _Narration:
    request_id: str
    event: Event
    done: asyncio.Future
    playback_generation: int = 0
    cancelled: str = ''
    work: asyncio.Task | None = None


class MediaNarrator:
    """Trusted speech; only an actual started/ended receipt succeeds.

    Payload text is supplied by the trusted WebUI music workflow, never directly
    by a WebSocket field. The owner must wire state/delivered to SpeechController.
    """
    def __init__(self, *, allocate_id, pipeline, push, interrupt, timeout_sec=95, resource_check=None,
                 reviewer=None):
        if not math.isfinite(timeout_sec) or timeout_sec <= 0:
            raise ValueError('旁白期限必须是正数')
        self.allocate_id = allocate_id
        self.pipeline = pipeline
        self.push = push
        self.interrupt = interrupt
        self.timeout_sec = timeout_sec
        # Optional synchronous preflight: None means available; a fixed reason
        # rejects before announcing an acknowledgement or queuing synthesis.
        self.resource_check = resource_check
        # A review is generated inside the same cancellable turn as its speech.
        # The callback gets validated public evidence, never chat memory.
        self.reviewer = reviewer
        self._active: _Narration | None = None
        self._revision = 0
        self._closed = False

    @property
    def active(self):
        return self._active is not None

    @staticmethod
    def _failure(reason):
        return {'completed': False, 'reason': reason}

    def _resource_failure(self):
        if self.resource_check is None:
            return None
        fallback = '语音资源状态暂时无法确认。请在启动中心检查后台任务服务，等授权恢复后重试。'
        try:
            reason = self.resource_check()
        except Exception:
            reason = fallback
        if reason is None:
            return None
        if not isinstance(reason, str) or not reason.strip():
            reason = fallback
        return {**self._failure(reason[:500]), 'error_code': 'voice_resource_unavailable'}

    def _current(self, turn):
        return (self._active is turn and not turn.cancelled
                and turn.playback_generation == self.pipeline.playback.generation)

    def _invalid_result(self, turn):
        reason = turn.cancelled or ('synthesis_cancelled'
            if turn.playback_generation != self.pipeline.playback.generation else 'interrupted')
        return self._failure(reason)

    def _settle(self, turn, result):
        if not turn.done.done():
            turn.done.set_result(result)

    async def _cancel_active(self, reason):
        turn = self._active
        if turn is not None:
            self._active = None
            turn.cancelled = reason
            self._settle(turn, self._failure(reason))
            if turn.work is not None:
                turn.work.cancel()
            await self.pipeline.cancel_turn(turn.event.ui_id)

    async def cancel(self, reason='interrupted'):
        self._revision += 1
        await self._cancel_active(reason)

    async def close(self):
        self._closed = True
        await self.cancel('shutdown')

    def _turn_for(self, item):
        turn = self._active
        if (turn is not None and item.turn_id == turn.event.ui_id
                and item.event.get('media_narration_id') == turn.request_id):
            return turn
        return None

    def _started(self, item):
        playback = self.pipeline.playback
        return playback.current is item and (playback.state == 'playing'
            or playback.paused and playback._resume_state == 'playing')

    def state(self, item, state, reason=''):
        turn = self._turn_for(item)
        if turn is None:
            return
        if state in {'failed', 'cancelled', 'expired', 'dropped'}:
            self._settle(turn, self._failure(reason or state))
        elif state == 'played' and not self._started(item):
            self._settle(turn, self._failure('missing_playback_start'))

    def delivered(self, item, completed):
        turn = self._turn_for(item)
        if turn is not None:
            success = completed is True and self._started(item)
            self._settle(turn, {'completed': True} if success else self._failure('interrupted'))

    async def _perform(self, turn, payload):
        if not self._current(turn):
            return self._invalid_result(turn)
        control = Event('companion', 'interrupt', '点歌旁白', forced=True)
        control.ui_id = turn.event.ui_id
        control.media_narration_id = turn.request_id
        await self.interrupt(control)
        if not self._current(turn):
            return self._invalid_result(turn)
        blocked = self._resource_failure()
        if blocked is not None:
            return blocked
        if payload['stage'] in {'ack', 'found', 'direct', 'review'}:
            # Microphone capture can pause the previous voice. A new explicit
            # music request resumes that timeline before queuing its response.
            for message in self.pipeline.playback.set_paused(False):
                await self.push(message)
            if not self._current(turn):
                return self._invalid_result(turn)
        if payload['stage'] == 'ack':
            await self.push({'type': 'event', 'id': turn.event.ui_id, 'source': 'companion',
                             'kind': 'chat', 'user': '你',
                             'text': ('点歌 ' if payload.get('media') == 'local' else 'B站点歌 ') + payload.get('query', ''),
                             'priority': turn.event.priority, 'media_narration_id': turn.request_id})
        if not self._current(turn):
            return self._invalid_result(turn)
        emotion = payload.get('emotion')
        if payload['stage'] == 'review':
            if not callable(self.reviewer):
                return self._failure('字幕评价尚未启用，请重启主服务后重试。')
            await self.push({'type': 'thinking', 'id': turn.event.ui_id,
                             'media_narration_id': turn.request_id})
            review_task = asyncio.create_task(self.reviewer(payload['evidence']))
            review_task.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
            try:
                await asyncio.wait({review_task, turn.done}, return_when=asyncio.FIRST_COMPLETED)
                if turn.done.done():
                    return turn.done.result()
                review = review_task.result()
            except asyncio.CancelledError:
                raise
            except Exception:
                return self._failure('这次字幕评价没有完整生成，请稍后重试。')
            finally:
                if not review_task.done():
                    review_task.cancel()
            if not self._current(turn):
                return self._invalid_result(turn)
            if (not isinstance(review, dict) or not isinstance(review.get('text'), str)
                    or not 1 <= len(review['text'].strip()) <= 350
                    or review.get('emotion') not in DEFAULT_EMOTIONS):
                return self._failure('评价没有返回可直接播出的正文，请重试。')
            turn.event.text, emotion = review['text'].strip(), review['emotion']
            blocked = self._resource_failure()
            if blocked is not None:
                return blocked
        if not isinstance(emotion, str) or emotion not in DEFAULT_EMOTIONS:
            emotion = '开心' if payload['stage'] in {'found', 'direct'} else '平静'
        actions = ['弯眼'] if emotion == '开心' else []
        await self.push({'type': 'reply', 'id': turn.event.ui_id, 'text': turn.event.text,
                         'emotion': emotion, 'actions': actions, 'mode': 'media', 'segments': 1,
                         'latency': 0, 'ttft': 0, 'media_narration_id': turn.request_id,
                         'media_narration_stage': payload['stage']})
        if not self._current(turn):
            return self._invalid_result(turn)
        synthesized = self.pipeline.enqueue(turn_id=turn.event.ui_id, segment_index=0,
            text=turn.event.text, emotion=emotion, actions=actions, event=turn.event)
        # Cancellation or a playback failure must wake the caller even if the
        # synthesis worker is still winding down a slow/non-cooperative voice.
        await asyncio.wait({synthesized, turn.done}, return_when=asyncio.FIRST_COMPLETED)
        if turn.done.done():
            return turn.done.result()
        if synthesized.cancelled():
            return self._failure('synthesis_cancelled')
        result = synthesized.result()
        if result.cancelled:
            return self._failure('synthesis_cancelled')
        if not result.audio_path:
            return self._failure('no_audio')
        return await turn.done

    async def narrate(self, payload):
        if self._closed:
            return self._failure('shutdown')
        if (not isinstance(payload, dict) or not isinstance(payload.get('stage'), str)
                or payload['stage'] not in {'ack', 'found', 'direct', 'results', 'error', 'review'}
                or not isinstance(payload.get('request_id'), str) or not 1 <= len(payload['request_id']) <= 128
                or not isinstance(payload.get('text'), str) or not payload['text'].strip()
                or len(payload['text']) > 500 or not isinstance(payload.get('query', ''), str)):
            return self._failure('invalid_payload')
        if payload['stage'] == 'review' and (not callable(self.reviewer)
                or not isinstance(payload.get('evidence'), dict)):
            return self._failure('字幕评价尚未准备好，请重新读取字幕。')
        self._revision += 1
        revision = self._revision
        await self._cancel_active('superseded')
        if revision != self._revision or self._closed:
            return self._failure('superseded')
        blocked = self._resource_failure()
        if blocked is not None:
            return blocked
        event = Event('system', 'ambient', payload['text'], forced=True)
        event.ui_id = self.allocate_id()
        generation = self.pipeline.begin_turn(event, event.ui_id)
        event.media_narration_id = payload['request_id']
        event.media_narration_stage = payload['stage']
        turn = _Narration(payload['request_id'], event, asyncio.get_running_loop().create_future(),
                          playback_generation=generation)
        self._active = turn
        result = self._failure('cancelled')
        try:
            turn.work = asyncio.create_task(self._perform(turn, payload))
            result = await asyncio.wait_for(turn.work, self.timeout_sec + (90 if payload['stage'] == 'review' else 0))
            if turn.cancelled:
                result = self._failure(turn.cancelled)
            return {**result, 'turn_id': event.ui_id}
        except asyncio.TimeoutError:
            return {**self._failure('timeout'), 'turn_id': event.ui_id}
        except asyncio.CancelledError:
            if turn.cancelled:
                return {**self._failure(turn.cancelled), 'turn_id': event.ui_id}
            turn.cancelled = 'cancelled'
            raise
        except Exception:
            return {**self._failure('narration_failed'), 'turn_id': event.ui_id}
        finally:
            if self._active is turn:
                self._active = None
            try:
                if not result.get('completed') or turn.cancelled:
                    await self.pipeline.cancel_turn(event.ui_id)
            finally:
                await self.push({'type': 'bilibili_narration_end', 'request_id': turn.request_id,
                                 'turn_id': event.ui_id})
