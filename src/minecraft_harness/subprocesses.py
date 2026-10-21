"""Bounded cleanup for local CLI children with captured subprocess pipes."""
from __future__ import annotations

import asyncio
import os
import signal


async def bounded_reap(process, readers=(), *, timeout_s=2, kill_group=False):
    """Kill, discard captured pipe buffers and reap before returning.

    ``Process.wait`` can remain pending after the OS has killed a child when a
    failed/cancelled reader leaves its pipe above asyncio's high-water mark.
    Closing those transports is intentional: the operation already failed, so
    its remaining output must be discarded, rather than retained or persisted.
    """
    async def finish():
        for reader in readers:
            if not reader.done():
                reader.cancel()
        if kill_group:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await asyncio.gather(*readers, return_exceptions=True)
        for stream in (process.stdout, process.stderr):
            transport = getattr(stream, '_transport', None)
            if transport is not None:
                transport.close()
        try:
            async with asyncio.timeout(timeout_s):
                await process.wait()
        finally:
            transport = getattr(process, '_transport', None)
            if transport is not None:
                transport.close()

    cleanup = asyncio.create_task(finish())
    cancelled = False
    # A repeated caller cancellation must not abandon a killed but unreaped
    # subprocess. finish() itself remains bounded by the reap deadline.
    while True:
        try:
            await asyncio.shield(cleanup)
            break
        except asyncio.CancelledError:
            cancelled = True
            if cleanup.done():
                cleanup.result()
                break
    if cancelled:
        raise asyncio.CancelledError
