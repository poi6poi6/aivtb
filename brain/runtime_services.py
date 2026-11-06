"""Application-owned services; HTTP views borrow them without owning their lifetime."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from pathlib import Path


class RuntimeServices:
    UI_BINDINGS = ('music_requests', 'suno_manual', 'minecraft', 'bilibili_activity', 'qq_channel', 'companion')

    def __init__(self, root, event_queue, *, public_test=False):
        from .bilibili_media import BilibiliSearch
        from .bilibili_browser import BilibiliBrowser
        from .music_requests import MusicRequests
        from .suno_manual import SunoManual
        from .minecraft import MinecraftBridge

        self.root = Path(root)
        self.event_queue = event_queue
        self.ui = None
        self.companion = None
        self.public_test = public_test
        self.media_search = BilibiliSearch()
        self.bilibili_browser = BilibiliBrowser()
        folder = self.root / 'data' / ('friend-test' if public_test else 'music')
        self.music_requests = MusicRequests(folder / 'requests.json')
        self.suno_manual = SunoManual(self.root, self.music_requests)
        self.minecraft = MinecraftBridge(self.root, event_queue)
        self.bilibili_activity = self.qq_channel = None
        self.voice_readiness = None
        self.trends = None
        self.idle = lambda: False
        self._started = self._closed = False

    def configure_persona(self, persona, autobiography, tts_cfg, *, synthesis_lock=None,
                          resource_available=lambda: True, codex_command='codex'):
        if self.public_test or self._started or self._closed or self.qq_channel is not None:
            raise ValueError('Channels can only be configured once in a local runtime')
        from .bilibili_activity import BilibiliActivity
        from .deepseek_harness import HarnessRunner
        from .qq_channel import QQChannel
        from .qq_voice import QQVoiceRenderer
        from .tts_readiness import TTSReadiness
        from .companion_features import CompanionFeatures
        from .astra_trends import AstraTrends
        from .trending_topics import TrendingTopics

        self.companion = CompanionFeatures(self.root, personality=getattr(persona, 'personality', None),
                                          codex_command=codex_command)
        self.voice_readiness = TTSReadiness(tts_cfg)
        self.trends = TrendingTopics(self.root / 'data' / 'trending-topics.json',
                                     AstraTrends(command=codex_command), idle=lambda: self.idle())
        self.bilibili_activity = BilibiliActivity(
            self.root / 'data' / 'bilibili-activity.json', HarnessRunner.from_persona(self.root, persona),
            asdict(persona.cfg), idle=lambda: self.idle())
        voice = QQVoiceRenderer(self.root, tts_cfg, synthesis_lock=synthesis_lock,
                                resource_available=resource_available, backend_ready=self.voice_available)
        self.qq_channel = QQChannel(self.root, persona, autobiography=autobiography, voice=voice,
                                    experience_context=self.public_context,
                                    on_foreground=self.foreground, companion=self.companion)

    def bind(self, ui):
        self.ui = ui
        for name in self.UI_BINDINGS:
            setattr(ui, name, getattr(self, name))
        ui._media_search = self.media_search
        ui._bilibili_browser = self.bilibili_browser

    def foreground(self, source):
        """All accepted conversational channels preempt the same background activity."""
        if source not in {'web', 'voice', 'qq_private', 'qq_group'}:
            raise ValueError('Unknown foreground channel')
        if not self._closed and self.bilibili_activity is not None:
            self.bilibili_activity.foreground()
        if not self._closed and self.companion is not None:
            self.companion.foreground()

    def voice_available(self):
        return self.voice_readiness is None or self.voice_readiness.snapshot()['ready']

    def topic_context(self, event):
        """Only public, fresh topics relevant to this turn; no searches from chat."""
        if (self.trends is None or self._closed or self.public_test
                or getattr(event, 'tarot_reading_id', '')
                or event.kind not in {'chat', 'idle'}):
            return ''
        context = self.trends.context_for(event.text)
        if context:
            event.trending_topic_context = True
        return context

    def public_context(self, event):
        pieces = [self.topic_context(event)]
        if self.bilibili_activity is not None:
            pieces.append(self.bilibili_activity.context_for(event))
        return '\n\n'.join(piece for piece in pieces if piece)

    def private_followups_available(self):
        allowed = getattr(self.ui, 'private_followups_available', None)
        return bool(not self._closed and not self.public_test and callable(allowed) and allowed())

    async def start(self):
        if self._closed:
            raise RuntimeError('Services have closed')
        if self._started:
            return
        self._started = True
        if self.voice_readiness is not None:
            self.voice_readiness.start()
        if self.trends is not None and not self.public_test:
            self.trends.start()
        if self.bilibili_activity is not None and not self.public_test:
            self.bilibili_activity.start()
        if self.companion is not None:
            self.companion.start(self.event_queue, idle=lambda: self.idle(),
                                 connected=self.private_followups_available)

    async def close(self):
        if self._closed:
            return
        self._closed = True
        # Each closer gets a chance even if another service cannot flush.
        services = [self.qq_channel, self.bilibili_activity, self.minecraft, self.media_search,
                    self.voice_readiness, self.companion, self.trends]
        results = await asyncio.gather(*(service.close() for service in services if service is not None),
                                       return_exceptions=True)
        self.bilibili_browser.close()
        if any(isinstance(result, BaseException) for result in results):
            raise RuntimeError('One or more application services failed to close')
