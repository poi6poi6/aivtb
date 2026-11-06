"""Bound independent memory reads without adding another model request."""
from __future__ import annotations

import asyncio
import math
import time

from .context_budget import ContextSection, assemble_context


def context_budget(value: object) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return .75
    return min(5.0, max(.05, number)) if math.isfinite(number) else .75


async def prepare_context(event, autobiography, cognition, *, timeout=.75):
    """Each source gets its own budget; timeout cannot discard the other source."""
    if getattr(event, 'tarot_reading_id', ''):
        return assemble_context(), {name: {'status': 'skipped', 'seconds': 0.0}
                                    for name in ('memory_recall', 'cognition_recall')}
    async def read(name, source):
        started = time.perf_counter()
        try:
            text = await asyncio.wait_for(source.context_for(event), context_budget(timeout))
            text = text if isinstance(text, str) else ''
            status = 'ready' if text else 'empty'
            source_status = getattr(source, 'last_context_status', '')
            if not text and source_status in {'empty', 'unavailable', 'disabled'}:
                status = source_status
        except asyncio.TimeoutError:
            text, status = '', 'timeout'
        except Exception:
            # Error details can contain source text, credentials or local paths.
            text, status = '', 'unavailable'
        return name, text, {'status': status, 'seconds': time.perf_counter() - started}

    results = await asyncio.gather(read('memory_recall', autobiography),
                                   read('cognition_recall', cognition))
    priorities = {'memory_recall': 70, 'cognition_recall': 50}
    return (assemble_context(*(ContextSection(name, text, priority=priorities[name])
                               for name, text, _ in results)),
            {name: detail for name, _, detail in results})
