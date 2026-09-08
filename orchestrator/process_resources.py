"""Process-local Function synchronization (never an interprocess file lock).

Each immutable shared/Agent process has its own registry. Durable stores that
need cross-process exclusion must use their store transaction or OS file lock.
There is no overlapping in-process module reload and no lock object crosses IPC.
"""

from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path

_REGISTRY_GUARD = threading.RLock()
_NAMED_LOCKS: dict[str, threading.RLock] = {}
_PATH_LOCKS: dict[str, threading.RLock] = {}
_ASYNC_NAMED_LOCKS: dict[str, asyncio.Lock] = {}
_ASYNC_PATH_LOCKS: dict[str, asyncio.Lock] = {}


def _path_key(path: str | Path) -> str:
    candidate = Path(path).expanduser()
    if os.name != "nt":
        return str(candidate.resolve())
    key = os.path.normcase(str(candidate.resolve()))
    if key.startswith("\\\\?\\unc\\"):
        return "\\\\" + key[8:]
    if key.startswith("\\\\?\\"):
        return key[4:]
    return key


def named_lock(name: str) -> threading.RLock:
    key = str(name).strip()
    if not key:
        raise ValueError("process lock name is required")
    with _REGISTRY_GUARD:
        return _NAMED_LOCKS.setdefault(key, threading.RLock())


def path_lock(path: str | Path) -> threading.RLock:
    key = _path_key(path)
    with _REGISTRY_GUARD:
        return _PATH_LOCKS.setdefault(key, threading.RLock())


def async_named_lock(name: str) -> asyncio.Lock:
    key = str(name).strip()
    if not key:
        raise ValueError("process lock name is required")
    with _REGISTRY_GUARD:
        return _ASYNC_NAMED_LOCKS.setdefault(key, asyncio.Lock())


def async_path_lock(path: str | Path) -> asyncio.Lock:
    key = _path_key(path)
    with _REGISTRY_GUARD:
        return _ASYNC_PATH_LOCKS.setdefault(key, asyncio.Lock())
