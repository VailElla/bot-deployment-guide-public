"""Serialize complete SQLite operations without blocking the event loop."""
from __future__ import annotations

import asyncio
import functools
from concurrent.futures import ThreadPoolExecutor


async def finish_submitted(future):
    """Observe a submitted operation before propagating repeated cancellation."""
    cancelled = False
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            cancelled = True
        except BaseException:
            break
    # Retrieve the result even after cancellation so errors are never orphaned.
    try:
        result = future.result()
    except BaseException:
        if cancelled:
            raise asyncio.CancelledError() from None
        raise
    if cancelled:
        raise asyncio.CancelledError()
    return result


class SQLiteWorker:
    """One thread per live store; each call owns its complete transaction."""
    def __init__(self, name):
        self.name = name
        self.executor = None
        self.closed = False
        self.pending = set()

    async def run(self, function, *args, **kwargs):
        if self.closed:
            raise RuntimeError('SQLite worker is closed')
        if self.executor is None:
            self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=self.name)
        future = asyncio.get_running_loop().run_in_executor(
            self.executor, functools.partial(function, *args, **kwargs))
        self.pending.add(future)
        future.add_done_callback(self.pending.discard)
        return await finish_submitted(future)

    async def close(self):
        self.closed = True
        if self.pending:
            await finish_submitted(asyncio.gather(*tuple(self.pending), return_exceptions=True))
        if self.executor is not None:
            self.executor.shutdown(wait=False)
            self.executor = None
