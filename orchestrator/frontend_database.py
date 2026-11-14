"""Bounded FC database work; SQLite connections stay inside their owning call."""
from __future__ import annotations

import asyncio
import contextvars
import hashlib
import logging
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from uuid import uuid4

logger = logging.getLogger(__name__)
_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="frontend-db")
_SLOTS = threading.BoundedSemaphore(8)


class DatabaseWorkBusy(RuntimeError):
    """Admission or BEGIN failed before any write; no operation was replayed."""


def is_sqlite_busy(error):
    return isinstance(error, sqlite3.OperationalError) and (
        getattr(error, "sqlite_errorcode", 0) & 255
    ) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}


async def run_database_work(function, /, *args, _db_role="frontend", _db_instance=None, _db_operation=None, **kwargs):
    """At most two running/eight admitted calls per process, including cancelled calls.

    Do not cancel/release a running write when its HTTP/IPC caller disappears.
    Settle it first and propagate cancellation without replaying its effect.
    No request/response/Telegram objects are passed by this executor.
    """
    operation = _db_operation or getattr(function, "__qualname__", type(function).__name__)
    bound = getattr(function, "__self__", None)
    instance = _db_instance or getattr(bound, "instance_id", None)
    if instance is None and args:
        instance = getattr(args[0], "instance_id", None)
    instance = instance or "unknown"
    scope = next((str(kwargs[key]) for key in ("request_id", "question_id", "event_id", "session_id")
                  if kwargs.get(key)), uuid4().hex)
    correlation = hashlib.sha256(scope.encode()).hexdigest()[:16]
    queued_at = time.monotonic()
    context = contextvars.copy_context()

    def call():
        started = time.monotonic()
        try:
            return context.run(partial(function, *args, **kwargs))
        finally:
            duration = time.monotonic() - started
            queued = started - queued_at
            if duration + queued >= 0.1:
                logger.info("frontend.database_work operation=%s instance=%s pid=%s role=%s correlation=%s queue_ms=%.1f duration_ms=%.1f",
                            operation, instance, os.getpid(), _db_role, correlation, queued * 1000, duration * 1000)

    if not _SLOTS.acquire(blocking=False):
        raise DatabaseWorkBusy("database work capacity reached")
    try:
        submitted = _EXECUTOR.submit(call)
    except BaseException:
        _SLOTS.release()
        raise
    submitted.add_done_callback(lambda _: _SLOTS.release())
    future = asyncio.wrap_future(submitted)
    cancelled = False
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break
    if cancelled:
        # Retrieve any storage error; the abandoned HTTP request is not a receipt.
        try:
            future.result()
        except Exception:
            logger.warning("frontend.database_work abandoned operation=%s correlation=%s outcome=unconfirmed",
                           operation, correlation)
        raise asyncio.CancelledError
    return future.result()
# HASHI-Fingerprint: hfp_3bcedf8ba47f43a1813c4a921cd50593
