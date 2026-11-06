"""A local tea-party driver claims once and never retries an uncertain turn."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from brain.events import Event
from brain.tarot_runtime import TarotHost, holds_audience_event


class TarotHostTests(unittest.IsolatedAsyncioTestCase):
    def test_guided_holds_ordinary_chat_but_keeps_host_and_support_available(self):
        tarot = SimpleNamespace(holds_chat=lambda: True)
        self.assertTrue(holds_audience_event(tarot, Event('danmaku', 'chat', '来聊别的')))
        self.assertTrue(holds_audience_event(tarot, Event('danmaku', 'gift', '一份礼物')))
        self.assertFalse(holds_audience_event(tarot, Event('voice', 'chat', '先停一下')))
        self.assertFalse(holds_audience_event(tarot, Event('danmaku', 'chat', '我现在想伤害自己')))
        self.assertFalse(holds_audience_event(tarot, Event('danmaku', 'chat', '主播指定的问题', forced=True)))

    def service(self):
        return SimpleNamespace(next_event=Mock(), mark_result=Mock())

    async def test_unavailable_stage_does_not_claim_or_spend_a_turn(self):
        tarot, register = self.service(), AsyncMock()
        host = TarotHost(tarot, register, available=lambda: False)
        self.assertIsNone(await host.tick())
        tarot.next_event.assert_not_called()
        register.assert_not_awaited()

    async def test_successful_claim_is_registered_once_and_next_tick_is_inert(self):
        tarot, register = self.service(), AsyncMock()
        event = Event('system', 'ambient', '公开塔罗问题', forced=True)
        tarot.next_event.side_effect = [event, None]
        host = TarotHost(tarot, register, available=lambda: True)
        self.assertIs(await host.tick(), event)
        self.assertIsNone(await host.tick())
        register.assert_awaited_once_with(event)
        tarot.mark_result.assert_not_called()

    async def test_registration_failure_stops_claim_and_does_not_retry_it(self):
        tarot, register = self.service(), AsyncMock(side_effect=RuntimeError('PRIVATE_PATH'))
        event = Event('system', 'ambient', '公开塔罗问题', forced=True)
        tarot.next_event.side_effect = [event, None]
        host = TarotHost(tarot, register, available=lambda: True)
        self.assertIsNone(await host.tick())
        self.assertNotIn('PRIVATE_PATH', host.last_error)
        tarot.mark_result.assert_called_once()
        self.assertEqual(tarot.mark_result.call_args.args, (event, 'failed'))
        self.assertIsNone(await host.tick())
        register.assert_awaited_once()

    async def test_cancel_during_registration_marks_claim_interrupted(self):
        tarot = self.service()
        event = Event('system', 'ambient', '公开塔罗问题', forced=True)
        tarot.next_event.return_value = event
        entered = asyncio.Event()

        async def register(value):
            self.assertIs(value, event)
            entered.set()
            await asyncio.Event().wait()

        host = TarotHost(tarot, register, available=lambda: True)
        task = asyncio.create_task(host.tick())
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(tarot.mark_result.call_args.args, (event, 'interrupted'))

    async def test_runner_recovers_from_store_read_failure_without_reclaiming(self):
        tarot, register = self.service(), AsyncMock()
        recovered = asyncio.Event()
        count = 0

        def claim():
            nonlocal count
            count += 1
            if count == 1:
                raise OSError('PRIVATE_STORAGE_PATH')
            recovered.set()
            return None

        tarot.next_event.side_effect = claim
        host = TarotHost(tarot, register, available=lambda: True, interval=.001)
        task = asyncio.create_task(host.run())
        try:
            await asyncio.wait_for(recovered.wait(), 1)
            self.assertNotIn('PRIVATE_STORAGE_PATH', host.last_error)
            register.assert_not_awaited()
            tarot.mark_result.assert_not_called()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


if __name__ == '__main__':
    unittest.main()
