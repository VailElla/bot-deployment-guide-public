import asyncio
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from local_embedding.main import LocalEmbedding


class FakeVector:
    def tolist(self):
        return [0.0] * 512


class LocalEmbeddingTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_only_output_and_cancellation_keeps_gate(self):
        started, release = threading.Event(), threading.Event()
        calls, settings = [], {}

        class FakeModel:
            def __init__(self, **kwargs):
                settings.update(kwargs)

            def embed(self, texts, batch_size):
                calls.append(list(texts))
                started.set()
                release.wait(3)
                return [FakeVector() for _ in texts]

        with tempfile.TemporaryDirectory() as folder, \
             patch.dict(sys.modules, {'fastembed': types.SimpleNamespace(TextEmbedding=FakeModel)}), \
             patch('local_embedding.main.StarTools.get_data_dir', return_value=Path(folder)):
            provider = LocalEmbedding({'id': 'test', 'type': 'local_fastembed_zh'}, {})
            first = asyncio.create_task(provider.get_embedding('first'))
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(.01)
            self.assertTrue(started.is_set())
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            second = asyncio.create_task(provider.get_embedding('second'))
            await asyncio.sleep(.05)
            self.assertEqual(calls, [['first']])
            release.set()
            vector = await asyncio.wait_for(second, 3)
            await provider.terminate()
            self.assertEqual(len(vector), 512)
            self.assertEqual(calls, [['first'], ['second']])
            self.assertTrue(settings['local_files_only'])
            self.assertEqual(settings['providers'], ['CPUExecutionProvider'])


if __name__ == '__main__':
    unittest.main()
