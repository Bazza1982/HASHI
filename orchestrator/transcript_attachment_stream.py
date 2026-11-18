"""Verified, bounded-memory delivery of an already-authorized transcript attachment."""
from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
import threading

from aiohttp import web

from orchestrator.session_store import SessionConflict


def _snapshot(path, *, observed_size, expected_digest, cancelled):
    snapshot = tempfile.TemporaryFile(mode="w+b")
    try:
        digest = hashlib.sha256()
        count = 0
        with path.open("rb") as source:
            before = os.fstat(source.fileno())
            if before.st_size != observed_size:
                raise SessionConflict("visible attachment size changed")
            while True:
                if cancelled.is_set():
                    raise RuntimeError("attachment snapshot cancelled")
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                count += len(chunk)
                snapshot.write(chunk)
                digest.update(chunk)
            after = os.fstat(source.fileno())
        if count != observed_size or (
            before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns
        ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise SessionConflict("visible attachment changed during download")
        actual_digest = digest.hexdigest()
        if expected_digest and actual_digest != expected_digest:
            raise SessionConflict("visible attachment content changed")
        snapshot.seek(0)
        return snapshot, actual_digest
    except BaseException:
        snapshot.close()
        raise


async def _prepare_snapshot(path, *, observed_size, expected_digest):
    cancelled = threading.Event()
    task = asyncio.create_task(asyncio.to_thread(
        _snapshot, path, observed_size=observed_size,
        expected_digest=expected_digest, cancelled=cancelled,
    ))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        cancelled.set()

        def release(completed):
            try:
                snapshot, _digest = completed.result()
            except BaseException:
                return
            snapshot.close()

        task.add_done_callback(release)
        raise


async def stream_verified_attachment(
    request, path, *, observed_size, expected_digest, content_type, headers,
):
    """Verify one frozen copy before exposing any bytes to the caller."""
    snapshot, digest = await _prepare_snapshot(
        path, observed_size=observed_size, expected_digest=expected_digest,
    )
    response_headers = {
        **headers,
        "Content-Type": content_type,
        "Content-Length": str(observed_size),
        "X-Hashi-Attachment-SHA256": digest,
        "ETag": f'"{digest}"',
    }
    response = web.StreamResponse(status=200, headers=response_headers)
    try:
        await response.prepare(request)
        while chunk := await asyncio.to_thread(snapshot.read, 1024 * 1024):
            await response.write(chunk)
        await response.write_eof()
    except (BrokenPipeError, ConnectionResetError):
        # The client no longer receives bytes; do not issue a second response.
        pass
    finally:
        snapshot.close()
    return response
