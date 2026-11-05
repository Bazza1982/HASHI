"""Keep blocking service work off its I/O loop without abandoning mutations."""
from __future__ import annotations

import asyncio
from contextvars import ContextVar, copy_context
from functools import partial

_CURRENT_EXECUTOR = ContextVar("permission_service_executor", default=None)


async def settled_thread(function, /, *arguments, executor=None, **keywords):
    """Cancellation retains ownership until the submitted operation has settled.

    A caller disconnect cannot stop SQLite's durable commit. Keep the dispatch
    alive until its thread finishes; the endpoint must not claim clean shutdown
    or release the installation lock before that point.
    """
    loop = asyncio.get_running_loop()
    context = copy_context()
    future = loop.run_in_executor(
        executor if executor is not None else _CURRENT_EXECUTOR.get(),
        context.run, partial(function, *arguments, **keywords),
    )
    cancelled = False
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            cancelled = True
        except BaseException:
            # Retrieve the settled error below, including after cancellation.
            break
    if cancelled:
        if not future.cancelled():
            future.exception()
        raise asyncio.CancelledError
    return future.result()
# HASHI-Fingerprint: hfp_d1e73b26420f44849fd2f99c3a825db4
