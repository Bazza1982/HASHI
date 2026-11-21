"""Pytest must preserve unittest async lifecycle and native async functions."""
import asyncio
import unittest


class AsyncUnittestLifecycle(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.expected = object()

    async def test_unittest_setup_runs(self):
        await asyncio.sleep(0)
        self.assertIsNotNone(self.expected)


async def test_plain_async_pytest_test_runs():
    event = asyncio.Event()
    asyncio.get_running_loop().call_soon(event.set)
    await asyncio.wait_for(event.wait(), timeout=1)
    assert event.is_set()
