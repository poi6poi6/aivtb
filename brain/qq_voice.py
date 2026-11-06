"""Generate private QQ voice bytes without entering the public speech pipeline."""
from __future__ import annotations

import asyncio
import io
import math
import tempfile
import wave
from dataclasses import replace
from pathlib import Path

import httpx

from .config import TTSConfig
from .speech_text import DEFAULT_EMOTIONS
from .tts import make_tts


class QQVoiceError(RuntimeError):
    # Only these public reason codes may cross into channel status. Never use
    # provider messages, response bodies, request URLs, or private text here.
    _CODES = frozenset({'voice_text_too_long', 'voice_invalid_text',
                        'voice_timeout', 'voice_unavailable',
                        'voice_invalid_audio', 'voice_failed'})

    def __init__(self, code='voice_failed'):
        self.code = code if isinstance(code, str) and code in self._CODES else 'voice_failed'
        super().__init__('暂时无法生成 QQ 语音，请使用文字回复。')


class _ResourceLock:
    """Share the live backend lock, rechecking its GPU grant after acquisition."""

    def __init__(self, lock, resource_available):
        self.lock = lock
        self.resource_available = resource_available

    async def __aenter__(self):
        await self.lock.acquire()
        try:
            while not self.resource_available():
                await asyncio.sleep(.1)
        except BaseException:
            self.lock.release()
            raise
        return self

    async def __aexit__(self, *args):
        self.lock.release()


class QQVoiceRenderer:
    MAX_TEXT_CHARS = 300
    # QQ file_data is base64; keep the complete encoded request below 5 MB.
    MAX_AUDIO_BYTES = 3_500_000
    MAX_AUDIO_SECONDS = 60
    _BACKENDS = frozenset({'indextts2', 'gpt_sovits', 'fish_speech'})

    def __init__(self, root, tts_cfg: TTSConfig, *, timeout_sec=60,
                 synthesis_lock=None, resource_available=None, backend_ready=None):
        if (type(timeout_sec) not in (int, float) or not math.isfinite(timeout_sec)
                or timeout_sec <= 0):
            raise ValueError('QQ 语音合成期限必须为正数')
        self.root = Path(root).resolve()
        self.cfg = replace(tts_cfg)
        if self.cfg.backend == 'indextts2':
            # QQ synthesizes the whole reply inside one 60-second budget.
            # Keep its configured short/long references and delivery style,
            # but avoid the slower multi-beam long-sentence search here.
            self.cfg = replace(self.cfg, indextts2_num_beams=1,
                               indextts2_long_num_beams=1)
        self.timeout_sec = timeout_sec
        self.resource_available = resource_available or (lambda: True)
        self.backend_ready = backend_ready or (lambda: True)
        self._lock = synthesis_lock if synthesis_lock is not None else asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self._closed = False

    @property
    def available(self):
        # A busy GPU does not change the configured mode or disable its switch.
        # MockTTS prints input instead of producing audio, so never construct it.
        return not self._closed and self.cfg.backend in self._BACKENDS and self.backend_ready()

    async def render(self, text: str, emotion: str = '平静') -> bytes:
        if not self.available:
            raise QQVoiceError('voice_unavailable')
        if not isinstance(text, str) or not text.strip():
            raise QQVoiceError('voice_invalid_text')
        if len(text) > self.MAX_TEXT_CHARS:
            raise QQVoiceError('voice_text_too_long')
        if not isinstance(emotion, str) or emotion not in DEFAULT_EMOTIONS:
            emotion = '平静'
        task = asyncio.create_task(self._render(text, emotion))
        self._tasks.add(task)
        try:
            # The deadline includes resource/lock waits. Cancelling the backend
            # preserves IndexTTS2's /cancel behavior before temporary files vanish.
            return await asyncio.wait_for(task, timeout=self.timeout_sec)
        except asyncio.CancelledError:
            raise
        except QQVoiceError:
            raise
        except (asyncio.TimeoutError, httpx.TimeoutException):
            raise QQVoiceError('voice_timeout') from None
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            code = ('voice_timeout' if status in (408, 504) else
                    'voice_unavailable' if status in (429, 502, 503) else 'voice_failed')
            raise QQVoiceError(code) from None
        except httpx.RequestError:
            raise QQVoiceError('voice_unavailable') from None
        except Exception:
            # Provider failures may contain the private input or server payload.
            raise QQVoiceError('voice_failed') from None
        finally:
            self._tasks.discard(task)

    async def _render(self, text, emotion):
        while not self.resource_available():
            await asyncio.sleep(.1)
        folder = self.root / 'data' / 'qq' / 'voice-temp'
        # Only this internal location is eligible for automatic removal. Never
        # write to the public audio directory, even transiently.
        public_dir = Path(self.cfg.out_dir).resolve()
        if folder.resolve() != folder or folder.is_relative_to(public_dir):
            raise QQVoiceError('voice_invalid_audio')
        folder.mkdir(parents=True, exist_ok=True)
        if folder.resolve() != folder:
            raise QQVoiceError('voice_invalid_audio')
        with tempfile.TemporaryDirectory(prefix='reply-', dir=folder) as temporary:
            private_dir = Path(temporary).resolve()
            voice = make_tts(replace(self.cfg, out_dir=str(private_dir)))
            voice._speak_lock = _ResourceLock(self._lock, self.resource_available)
            try:
                path = await voice.speak(text, emotion)
                if not isinstance(path, (str, Path)) or not str(path).strip():
                    raise QQVoiceError('voice_invalid_audio')
                source = Path(path)
                try:
                    if source.is_symlink() or source.resolve(strict=True).parent != private_dir:
                        raise QQVoiceError('voice_invalid_audio')
                    with source.open('rb') as stream:
                        audio = stream.read(self.MAX_AUDIO_BYTES + 1)
                except FileNotFoundError:
                    raise QQVoiceError('voice_invalid_audio') from None
                self._validate_wav(audio)
                return audio
            finally:
                await voice.close()

    @classmethod
    def _validate_wav(cls, audio):
        if not 44 <= len(audio) <= cls.MAX_AUDIO_BYTES:
            raise QQVoiceError('voice_invalid_audio')
        try:
            with wave.open(io.BytesIO(audio), 'rb') as wav:
                rate, frames = wav.getframerate(), wav.getnframes()
                channels, width = wav.getnchannels(), wav.getsampwidth()
                if (wav.getcomptype() != 'NONE' or rate <= 0 or frames <= 0
                        or channels not in (1, 2) or width not in (1, 2, 3, 4)
                        or frames / rate > cls.MAX_AUDIO_SECONDS):
                    raise QQVoiceError('voice_invalid_audio')
                expected = frames * channels * width
                if expected > cls.MAX_AUDIO_BYTES or len(wav.readframes(frames)) != expected:
                    raise QQVoiceError('voice_invalid_audio')
        except (wave.Error, EOFError, ValueError, OverflowError):
            raise QQVoiceError('voice_invalid_audio') from None

    async def close(self):
        self._closed = True
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.difference_update(tasks)
