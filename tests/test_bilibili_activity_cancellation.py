"""Broker lifecycle notifications exercised without a browser or model."""
import asyncio
import unittest
import uuid

from brain.bilibili_activity import BrowserBroker


class BrowserCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_control_poll_does_not_lease_a_second_command(self):
        broker = BrowserBroker(timeout=1)
        first = asyncio.create_task(broker.call('read', {'url': 'fixture'}))
        second = asyncio.create_task(broker.call('account', {}))
        await asyncio.sleep(0)
        command = broker.poll()['command']
        try:
            for _ in range(3):
                self.assertEqual(broker.poll(command['id']), {'command': None, 'cancelled': []})
            self.assertEqual(sum(row[2] for row in broker.pending.values()), 1)
            self.assertTrue(broker.snapshot()['connected'])
            broker.cancel()
            self.assertEqual(broker.poll(command['id'])['cancelled'], [command['id']])
            await asyncio.gather(first, second, return_exceptions=True)
            self.assertEqual(broker.poll(command['id'])['cancelled'], [command['id']])
            self.assertIsNone(broker.poll()['command'])
        finally:
            broker.cancel()
            await asyncio.gather(first, second, return_exceptions=True)

    async def test_parent_task_cancellation_invalidates_an_already_claimed_publish(self):
        broker = BrowserBroker(timeout=1)
        task = asyncio.create_task(broker.call('publish', {'text': 'fixture'}))
        await asyncio.sleep(0)
        command = broker.poll()['command']
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(broker.poll(command['id']), {'command': None, 'cancelled': [command['id']]})
        self.assertIsNone(broker.poll()['command'])
        self.assertFalse(broker.result({'id': command['id'], 'ok': True, 'result': {'status': 'published'}}))

    async def test_timeout_and_restart_notify_the_old_lease_without_reissuing_it(self):
        broker = BrowserBroker(timeout=.01)
        task = asyncio.create_task(broker.call('read', {'url': 'fixture'}))
        await asyncio.sleep(0)
        command = broker.poll()['command']
        with self.assertRaisesRegex(ValueError, '没有及时回执'):
            await task
        self.assertEqual(broker.poll(command['id'])['cancelled'], [command['id']])
        restarted = BrowserBroker()
        self.assertEqual(restarted.poll(command['id']), {'command': None, 'cancelled': [command['id']]})
        self.assertIsNone(restarted.poll()['command'])

    def test_invalid_control_id_is_not_a_claim(self):
        broker = BrowserBroker()
        for value in ('', 'not-a-uuid', 3, [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                broker.poll(value)
        missing = str(uuid.uuid4())
        self.assertEqual(broker.poll(missing)['cancelled'], [missing])


if __name__ == '__main__':
    unittest.main()
