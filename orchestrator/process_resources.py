"""Process-local Function synchronization (never an interprocess file lock).

Each immutable shared/Agent process has its own registry. Durable stores that
need cross-process exclusion must use their store transaction or OS file lock.
There is no overlapping in-process module reload and no lock object crosses IPC.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

_REGISTRY_GUARD = threading.RLock()
_NAMED_LOCKS: dict[str, threading.RLock] = {}
_PATH_LOCKS: dict[str, threading.RLock] = {}
_ASYNC_NAMED_LOCKS: dict[str, asyncio.Lock] = {}
_ASYNC_PATH_LOCKS: dict[str, asyncio.Lock] = {}


def named_lock(name: str) -> threading.RLock:
    key = str(name).strip()
    if not key:
        raise ValueError("process lock name is required")
    with _REGISTRY_GUARD:
        return _NAMED_LOCKS.setdefault(key, threading.RLock())


def path_lock(path: str | Path) -> threading.RLock:
    key = str(Path(path).resolve())
    with _REGISTRY_GUARD:
        return _PATH_LOCKS.setdefault(key, threading.RLock())


def async_named_lock(name: str) -> asyncio.Lock:
    key = str(name).strip()
    if not key:
        raise ValueError("process lock name is required")
    with _REGISTRY_GUARD:
        return _ASYNC_NAMED_LOCKS.setdefault(key, asyncio.Lock())


def async_path_lock(path: str | Path) -> asyncio.Lock:
    key = str(Path(path).resolve())
    with _REGISTRY_GUARD:
        return _ASYNC_PATH_LOCKS.setdefault(key, asyncio.Lock())
