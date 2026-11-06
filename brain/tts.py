"""TTS backends write WAV files for the shared playback and avatar pipeline.

真实开播时让播放器 / 虚拟声卡播放 out_dir 里的音频，OBS 采集即可。
"""
from __future__ import annotations

import asyncio
import base64
import io
import itertools
import math
import os
import time
import uuid
import wave
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .config import TTSConfig
from .speech_text import sanitize_for_tts
from .speech_audio import trim_edge_silence
from .fish_expression import DeliveryPlan, FISH_EMOTION_TAGS, plan_fish_speech
from .index_delivery import plan_index_delivery


def emotion_strength(emotion: str, text: str, base: float, previous: float | None = None) -> float:
    scale = {"平静": 0.68, "委屈": 0.76, "吐槽": 0.85, "开心": 0.92,
             "得意": 0.9, "惊讶": 1.0}.get(emotion, 0.68)
    target = max(0.0, min(1.0, base * scale))
    if emotion in {"开心", "惊讶"} and any(mark in text for mark in "！!"):
        target = min(1.0, target + 0.04)
    if previous is not None:
        target = max(previous - 0.12, min(previous + 0.12, target))
    return round(target, 3)


def index_voice_profile(cfg: TTSConfig, text: str) -> tuple[str, int]:
    reference = cfg.indextts2_ref_audio_path or cfg.ref_audio_path
    if cfg.indextts2_short_ref_audio_path:
        length = sum(char.isalnum() for char in text)
        if length < max(1, cfg.indextts2_long_min_chars):
            return cfg.indextts2_short_ref_audio_path, cfg.indextts2_num_beams
        return reference, cfg.indextts2_long_num_beams
    return reference, cfg.indextts2_num_beams


class MockTTS:
    def __init__(self, cfg: TTSConfig):
        self.cfg = cfg
        self._speak_lock = asyncio.Lock()

    async def speak(self, text: str, emotion: str = "平静") -> str | None:
        text = sanitize_for_tts(text)
        if not text:
            return None
        async with self._speak_lock:
            print(f"[TTS] 播报[{emotion}] {text}")
            await asyncio.sleep(min(0.06 * len(text), 2.0))  # 模拟播报耗时
            return None

    async def close(self) -> None:
        return None


class GptSovitsTTS:
    def __init__(self, cfg: TTSConfig):
        self.cfg = cfg
        self.out_dir = Path(cfg.out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._counter = itertools.count(1)
        # 即使未来有多个事件消费者，也只允许一个合成请求进入 TTS 服务。
        self._speak_lock = asyncio.Lock()
        self._client: httpx.AsyncClient | None = None

    async def speak(self, text: str, emotion: str = "平静") -> str | None:
        text = sanitize_for_tts(text)
        if not text:
            return None
        async with self._speak_lock:
            payload = {
                "text": text,
                "text_lang": "zh",
                "ref_audio_path": self.cfg.ref_audio_path,
                "prompt_text": self.cfg.prompt_text,
                "prompt_lang": self.cfg.prompt_lang,
            }
            client = self._http_client()
            resp = await client.post(
                f"{self.cfg.gpt_sovits_url.rstrip('/')}/tts", json=payload
            )
            resp.raise_for_status()
            path = self.out_dir / f"{int(time.time())}_{next(self._counter)}.wav"
            path.write_bytes(trim_edge_silence(resp.content) if self.cfg.trim_edge_silence else resp.content)
            print(f"[TTS] GPT-SoVITS 已合成[{emotion}] {path}")
            return str(path)

    def _http_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=60)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


class IndexTTS2TTS:
    def __init__(self, cfg: TTSConfig):
        self.cfg = cfg
        if type(cfg.indextts2_natural_delivery) is not bool:
            raise ValueError('indextts2_natural_delivery 必须为 true 或 false')
        if cfg.indextts2_delivery_preset not in ('reference', 'expressive'):
            raise ValueError('indextts2_delivery_preset 必须为 reference 或 expressive')
        if type(cfg.indextts2_expressive_teasing) is not bool:
            raise ValueError('indextts2_expressive_teasing 必须为 true 或 false')
        self.out_dir = Path(cfg.out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._counter = itertools.count(1)
        self._speak_lock = asyncio.Lock()
        self._client: httpx.AsyncClient | None = None
        self._last_alpha: float | None = None
        self._last_synthesis = 0.0
        self._delivery_turn = None
        self._delivery_next_segment = 0
        self._delivery_reference: str | None = None
        self._delivery_mode: str | None = None

    async def speak(self, text: str, emotion: str = "平静") -> str | None:
        return await self._speak(text, emotion)

    async def speak_segment(self, text: str, emotion: str = "平静", *,
                            turn_id, segment_index: int) -> str | None:
        return await self._speak(text, emotion, context=(turn_id, segment_index))

    def _reset_delivery(self) -> None:
        self._delivery_turn = None
        self._delivery_next_segment = 0
        self._delivery_reference = None
        self._delivery_mode = None
        self._last_alpha = None
        self._last_synthesis = 0.0

    async def _speak(self, text: str, emotion: str, *, context=None) -> str | None:
        text = sanitize_for_tts(text)
        async with self._speak_lock:
            consecutive = (context is not None and context[1] > 0
                           and context[0] == self._delivery_turn
                           and context[1] == self._delivery_next_segment
                           and 0 <= time.monotonic() - self._last_synthesis < 30)
            previous = self._last_alpha if consecutive else None
            previous_reference = self._delivery_reference if consecutive else None
            previous_mode = self._delivery_mode if consecutive else None
            # A failed request, a standalone preview, or a skipped segment must
            # break continuity. Only a successfully saved segment commits it.
            self._reset_delivery()
            if not text:
                return None
            reference, beams = index_voice_profile(self.cfg, text)
            if self.cfg.indextts2_natural_delivery and previous_reference:
                reference = previous_reference
            request_id = uuid.uuid4().hex
            delivery = plan_index_delivery(
                text, emotion, emotion_strength(emotion, text, self.cfg.indextts2_emo_alpha),
                base=self.cfg.indextts2_emo_alpha,
                enabled=(self.cfg.indextts2_delivery_preset == 'expressive'
                         and self.cfg.indextts2_natural_delivery
                         and self.cfg.indextts2_reference_style),
                previous_mode=previous_mode,
                expressive_teasing=self.cfg.indextts2_expressive_teasing,
            )
            alpha = delivery.alpha
            if previous is not None:
                alpha = round(max(previous - .12, min(previous + .12, alpha)), 3)
            payload = {
                "request_id": request_id,
                "text": text,
                "emotion": delivery.emotion,
                "ref_audio_path": reference,
                "reference_style": self.cfg.indextts2_reference_style,
                "natural_delivery": self.cfg.indextts2_natural_delivery,
                "emo_alpha": alpha,
                "num_beams": beams,
            }
            if delivery.temperature is not None:
                payload['temperature'] = delivery.temperature
            client = self._http_client()
            try:
                resp = await client.post(
                    f"{self.cfg.indextts2_url.rstrip('/')}/tts", json=payload
                )
            except asyncio.CancelledError:
                # A disconnected HTTP request alone does not stop server inference.
                try:
                    await client.post(
                        f"{self.cfg.indextts2_url.rstrip('/')}/cancel",
                        json={"request_id": request_id}, timeout=2.0,
                    )
                except httpx.HTTPError:
                    pass
                raise
            resp.raise_for_status()
            path = self.out_dir / f"{int(time.time())}_{next(self._counter)}_index.wav"
            audio = resp.content
            if self.cfg.trim_edge_silence:
                # Keep a little more of the model's existing quiet onset. This
                # never inserts breathing or changes pauses inside the speech.
                audio = trim_edge_silence(
                    audio, start_padding_sec=0.18 if self.cfg.indextts2_natural_delivery else 0.10)
            path.write_bytes(audio)
            if context is not None:
                self._delivery_turn, segment_index = context
                self._delivery_next_segment = segment_index + 1
                self._delivery_reference = reference
                self._delivery_mode = delivery.mode
                self._last_alpha = alpha
                self._last_synthesis = time.monotonic()
            print(f"[TTS] IndexTTS2 已合成[{emotion}] {path}")
            return str(path)

    def _http_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=240)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


class FishSpeechTTS:
    """Official self-hosted /v1/tts API (Drama 3 and S2-Pro).

    JSON is supported by tools/server/api_utils.py; reference bytes are base64
    decoded by ServeReferenceAudio. The loaded checkpoint selects the model.
    Keep non-streaming PCM WAV for the existing playback/trim contract.
    """
    EMOTION_TAGS = FISH_EMOTION_TAGS
    MAX_REFERENCE_BYTES = 20 * 1024 * 1024
    MAX_AUDIO_BYTES = 64 * 1024 * 1024

    def __init__(self, cfg: TTSConfig):
        self.cfg = cfg
        url = urlsplit(cfg.fish_speech_url)
        if (url.scheme not in {'http', 'https'} or not url.hostname or url.username
                or url.password or url.query or url.fragment):
            raise ValueError('Fish Speech 地址必须是 HTTP(S) 服务根地址，不包含凭据或查询参数')
        if url.path.rstrip('/'):
            raise ValueError('Fish Speech 请填写服务根地址，不要附加 /v1/tts')
        timeout = cfg.fish_speech_timeout_sec
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('Fish Speech 请求超时必须为正数')
        if type(cfg.fish_speech_emotion_tags) is not bool:
            raise ValueError('fish_speech_emotion_tags 必须为 true 或 false')
        if type(cfg.fish_speech_natural_delivery) is not bool:
            raise ValueError('fish_speech_natural_delivery 必须为 true 或 false')
        if type(cfg.fish_speech_breathing) is not bool:
            raise ValueError('fish_speech_breathing 必须为 true 或 false')
        self.out_dir = Path(cfg.out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._speak_lock = asyncio.Lock()
        self._client: httpx.AsyncClient | None = None
        # Only one consecutive spoken turn is retained; previews are independent.
        self._delivery_turn = None
        self._delivery_next_segment = 0
        self._delivery_units = 0
        self._delivery_breathed = False
        self._delivery_emotion = None
        self._delivery_completed_at = 0.0

    def _plan_delivery(self, text, emotion, *, prior=0, breathed=False, continuation=False):
        if not self.cfg.fish_speech_emotion_tags:
            return DeliveryPlan(text, None, 0)
        return plan_fish_speech(text, emotion, natural=self.cfg.fish_speech_natural_delivery,
                                breathing=self.cfg.fish_speech_breathing and not breathed,
                                spoken_since_breath=prior, continuation=continuation)

    def _payload(self, text, emotion, *, delivery=None):
        delivery = delivery if delivery is not None else self._plan_delivery(text, emotion)
        payload = {'text': delivery.text, 'format': 'wav', 'streaming': False}
        reference_id = self.cfg.fish_speech_reference_id.strip()
        if reference_id:
            payload['reference_id'] = reference_id
            return payload
        reference = self.cfg.fish_speech_ref_audio_path.strip()
        transcript = self.cfg.fish_speech_prompt_text.strip()
        if bool(reference) != bool(transcript):
            raise ValueError('Fish Speech 参考音频和逐字转写必须成对配置，或使用 reference_id')
        if reference:
            path = Path(reference)
            if not path.is_file():
                raise ValueError('Fish Speech 参考音频不存在，请检查 fish_speech_ref_audio_path')
            # Bound the actual read too, in case the file grows after stat().
            with path.open('rb') as stream:
                audio = stream.read(self.MAX_REFERENCE_BYTES + 1)
            if not 192 <= len(audio) <= self.MAX_REFERENCE_BYTES:
                raise ValueError('Fish Speech 参考音频为空、过短或超过 20 MiB')
            payload['references'] = [{'audio': base64.b64encode(audio).decode('ascii'), 'text': transcript}]
            # Official server keys file-reference codec caching by audio hash.
            # Keep reference_id on its default path so server-side edits stay visible.
            payload['use_memory_cache'] = 'on'
        return payload

    @staticmethod
    def _check_wav(audio):
        try:
            with wave.open(io.BytesIO(audio), 'rb') as wav:
                frames = wav.getnframes()
                if frames <= 0 or wav.getframerate() <= 0:
                    raise ValueError('empty wave')
                expected = frames * wav.getnchannels() * wav.getsampwidth()
                if expected > len(audio) or len(wav.readframes(frames)) != expected:
                    raise ValueError('truncated wave')
        except (wave.Error, EOFError, ValueError, OverflowError) as exc:
            raise ValueError('Fish Speech 未返回完整的 PCM WAV 音频，请检查服务地址及日志') from exc

    async def speak(self, text: str, emotion: str = '平静') -> str | None:
        return await self._speak(text, emotion)

    async def speak_segment(self, text: str, emotion: str = '平静', *,
                            turn_id, segment_index: int) -> str | None:
        return await self._speak(text, emotion, context=(turn_id, segment_index))

    async def _speak(self, text, emotion, *, context=None):
        text = sanitize_for_tts(text)
        async with self._speak_lock:
            if not text:
                if context is not None:
                    self._delivery_next_segment = 0
                return None
            same_turn = (context is not None and context[1] > 0
                         and context[0] == self._delivery_turn)
            consecutive = (same_turn
                           and context[1] == self._delivery_next_segment
                           and time.monotonic() - self._delivery_completed_at < 30)
            prior = self._delivery_units if consecutive else 0
            # A pause resets phrasing, but cannot replenish this turn's breath budget.
            breathed = self._delivery_breathed if same_turn else False
            delivery = self._plan_delivery(text, emotion, prior=prior, breathed=breathed,
                                          continuation=consecutive and emotion == self._delivery_emotion)
            try:
                path = await self._synthesize(text, emotion, delivery)
            except (Exception, asyncio.CancelledError):
                if context is not None:
                    # Drop phrasing continuity, keeping breaths already delivered
                    # successfully in this turn. The failed plan is never committed.
                    self._delivery_next_segment = 0
                raise
            if context is not None:
                self._delivery_turn, segment_index = context
                self._delivery_next_segment = segment_index + 1
                self._delivery_units = delivery.remaining_units
                self._delivery_breathed = breathed or delivery.breath_at is not None
                self._delivery_emotion = emotion
                self._delivery_completed_at = time.monotonic()
            return path

    async def _synthesize(self, text, emotion, delivery):
        payload = self._payload(text, emotion, delivery=delivery)
        headers = {'Accept': 'audio/wav'}
        token = os.environ.get(self.cfg.fish_speech_api_key_env, '').strip()
        if token:
            headers['Authorization'] = 'Bearer ' + token
        client = self._http_client()
        # Cancellation closes the HTTP request and propagates to SpeechPipeline.
        # Fish has no /cancel endpoint: do not claim server inference stopped.
        async with client.stream('POST', self.cfg.fish_speech_url.rstrip('/') + '/v1/tts',
                                 json=payload, headers=headers) as resp:
            resp.raise_for_status()
            content_type = resp.headers.get('Content-Type', '').split(';')[0].strip().lower()
            if content_type and content_type not in {'audio/wav', 'audio/x-wav', 'audio/wave', 'application/octet-stream'}:
                raise ValueError('Fish Speech 返回的内容不是 WAV 音频，请检查服务配置')
            audio = bytearray()
            async for chunk in resp.aiter_bytes():
                if len(audio) + len(chunk) > self.MAX_AUDIO_BYTES:
                    raise ValueError('Fish Speech 音频超过 64 MiB，已停止接收')
                audio.extend(chunk)
        audio = bytes(audio)
        self._check_wav(audio)
        if self.cfg.trim_edge_silence:
            # Keep the onset of a deliberately quiet inhale on a continuation.
            audio = trim_edge_silence(audio, start_padding_sec=0.35 if delivery.breath_at == 0 else 0.10)
        path = self.out_dir / f'{int(time.time())}_{uuid.uuid4().hex}_fish.wav'
        try:
            path.write_bytes(audio)
        except OSError:
            path.unlink(missing_ok=True)
            raise
        return str(path)

    def _http_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.cfg.fish_speech_timeout_sec,
                                            trust_env=False, follow_redirects=False)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def make_tts(cfg: TTSConfig):
    if cfg.backend == "fish_speech":
        return FishSpeechTTS(cfg)
    if cfg.backend == "indextts2":
        return IndexTTS2TTS(cfg)
    if cfg.backend == "gpt_sovits":
        return GptSovitsTTS(cfg)
    return MockTTS(cfg)
