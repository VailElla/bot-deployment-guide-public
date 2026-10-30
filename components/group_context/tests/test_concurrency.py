import asyncio
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import Mock

path = Path(__file__).resolve().parents[1] / 'reference/concurrency.py'
spec = importlib.util.spec_from_file_location('concurrency_reference', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_four_permits_without_replacing_live_queue(self):
        class Provider:
            def __init__(self):
                self._semaphore = asyncio.Semaphore(1)
        provider = Provider()
        semaphore = provider._semaphore
        release = asyncio.Event()
        active = []
        async def run(n):
            async with semaphore:
                active.append(n)
                await release.wait()
        tasks = [asyncio.create_task(run(n)) for n in range(5)]
        try:
            await asyncio.sleep(.02)
            self.assertEqual(len(active), 1)
            module.enable_four_requests(Provider, [provider], Mock())
            module.enable_four_requests(Provider, [provider], Mock())
            await asyncio.sleep(.02)
            self.assertIs(provider._semaphore, semaphore)
            self.assertEqual(len(active), 4)
        finally:
            release.set()
            await asyncio.gather(*tasks)
        self.assertEqual(len(active), 5)
        self.assertEqual(semaphore._value, 4)
