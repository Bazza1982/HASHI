"""Bounded private service endpoints. Calling start is an operational action."""
from __future__ import annotations

import asyncio
from contextlib import suppress

from orchestrator.permissions.ipc_rpc import serve_request, welcome
from orchestrator.permissions.windows_ipc import WindowsPipeListener
from orchestrator.permissions.wire import PermissionServiceRouter


class PermissionEndpoint:
    def __init__(self, listener, router, *, signing_key, service_generation, max_connections=8):
        if (not isinstance(listener, WindowsPipeListener) or not isinstance(router, PermissionServiceRouter)
                or type(max_connections) is not int or not 1 <= max_connections <= 8):
            raise ValueError("a fixed native permission endpoint is required")
        self._listener, self._router = listener, router
        self._key, self.generation = signing_key, service_generation
        self._slots = asyncio.Semaphore(max_connections)
        self._closing = False
        self._accept_task = None
        self._connections, self._tasks = set(), set()

    async def start(self):
        if self._closing or self._accept_task is not None:
            raise RuntimeError("permission endpoint cannot be started twice")
        self._accept_task = asyncio.create_task(self._accept())

    async def _accept(self):
        while not self._closing:
            await self._slots.acquire()
            try:
                accepted = await asyncio.to_thread(self._listener.accept, timeout_s=.25)
            except (OSError, PermissionError, ValueError, TimeoutError):
                self._slots.release()
                if self._closing:
                    break
                continue
            if accepted is None:
                self._slots.release()
                continue
            pipe, _, hello = accepted
            self._connections.add(pipe)
            task = asyncio.create_task(self._connection(pipe, hello))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _connection(self, pipe, hello):
        dispatch = monitor = None
        try:
            await asyncio.to_thread(pipe.send, welcome(
                hello, signing_key=self._key, service_generation=self.generation,
                endpoint=self._listener.name,
            ))
            frame = await asyncio.to_thread(pipe.receive, timeout_s=5)

            async def disconnected():
                while True:
                    try:
                        await asyncio.to_thread(pipe.receive, timeout_s=.25)
                    except TimeoutError:
                        continue
                    # One request per authenticated connection. A second frame
                    # is not a role change, continuation capability or retry.
                    return

            dispatch = asyncio.create_task(serve_request(pipe, self._router, frame))
            monitor = asyncio.create_task(disconnected())
            done, _ = await asyncio.wait((dispatch, monitor), return_when=asyncio.FIRST_COMPLETED)
            if monitor in done and not dispatch.done():
                dispatch.cancel()
            await dispatch
        except (asyncio.CancelledError, OSError, EOFError, ValueError, PermissionError, TimeoutError):
            pass
        finally:
            pipe.close()
            for task in (dispatch, monitor):
                if task is not None and not task.done():
                    task.cancel()
            for task in (dispatch, monitor):
                if task is not None:
                    with suppress(BaseException):
                        await task
            self._connections.discard(pipe)
            self._slots.release()

    async def close(self):
        self._closing = True
        self._listener.close()
        for pipe in tuple(self._connections):
            pipe.close()
        tasks = tuple(self._tasks) + ((self._accept_task,) if self._accept_task is not None else ())
        for task in tasks:
            task.cancel()
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=5)
            for task in done:
                with suppress(BaseException):
                    task.result()
            if pending:
                raise OSError("permission endpoint cleanup is unverified")
        self._tasks.clear()
