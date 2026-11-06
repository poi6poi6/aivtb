import asyncio
import unittest

from brain.socket_outbox import SocketOutbox


class SocketOutboxTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_connection_does_not_block_fast_audio_controls(self):
        blocked = asyncio.Event()
        failed = asyncio.Event()
        delivered = asyncio.Event()
        received = []

        async def slow_send(text):
            await blocked.wait()

        async def fast_send(text):
            received.append(text)
            if text == 'audio-2':
                delivered.set()

        async def fail():
            failed.set()

        slow = SocketOutbox(slow_send, fail, timeout=.03)
        fast = SocketOutbox(fast_send, fail)
        try:
            for text in ['audio-1', 'pause', 'clear', 'audio-2']:
                slow.offer(text)
                fast.offer(text)
            await asyncio.wait_for(delivered.wait(), 1.0)
            await asyncio.wait_for(failed.wait(), 1.0)
            self.assertEqual(received, ['audio-1', 'pause', 'clear', 'audio-2'])
            self.assertTrue(slow.failed)
            self.assertFalse(fast.failed)
        finally:
            await slow.close()
            await fast.close()

    async def test_latest_status_is_coalesced_without_losing_controls(self):
        received = []
        delivered = asyncio.Event()
        async def send(text):
            received.append(text)
            if text == 'clear':
                delivered.set()
        async def fail():
            self.fail('status traffic should not disconnect')
        outbox = SocketOutbox(send, fail, capacity=4)
        try:
            outbox.offer('audio')
            for i in range(100):
                outbox.offer(str(i), state_key='metrics')
            outbox.offer('clear')
            await asyncio.wait_for(delivered.wait(), 1.0)
            self.assertEqual(received, ['audio', '99', 'clear'])
            self.assertEqual(outbox.coalesced, 99)
        finally:
            await outbox.close()

    async def test_reliable_queue_overflow_disconnects_explicitly(self):
        failed = asyncio.Event()
        async def send(text):
            await asyncio.Event().wait()
        async def fail():
            failed.set()
        outbox = SocketOutbox(send, fail, capacity=2)
        try:
            self.assertTrue(outbox.offer('audio'))
            self.assertTrue(outbox.offer('clear'))
            self.assertFalse(outbox.offer('next-audio'))
            await asyncio.wait_for(failed.wait(), .5)
            self.assertTrue(outbox.closed)
        finally:
            await outbox.close()

    async def test_explicit_close_does_not_report_failure(self):
        failed = []
        async def send(text):
            await asyncio.Event().wait()
        async def fail():
            failed.append(True)
        outbox = SocketOutbox(send, fail)
        outbox.offer('audio')
        await asyncio.sleep(0)
        await outbox.close()
        self.assertFalse(failed)
