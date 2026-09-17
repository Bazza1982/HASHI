"""Process-local memoization for the HASHI runtime fingerprint.

The runtime fingerprint is deterministic for a given ``(policy, code_root)``
and process environment.  Recomputing it is dominated by
``dependency_digest``, which re-enumerates every installed distribution and
reads each distribution's ``Name``/``Version`` metadata.  On the reboot path
this exact computation is repeated several times within one process even
though the inputs cannot have changed.

The fingerprint's only variable inputs are:

* the runtime policy plus its exact dependency lock file
  (``runtime_policy_digest``),
* the protected Core source files (``core_source_digest``), and
* the installed distribution set (``dependency_digest``).

The interpreter/ABI fields are constant for the lifetime of a process.  We
therefore snapshot the fingerprint once per ``code_root`` and reuse it as long
as those three inputs are unchanged.  Any change invalidates the snapshot and
forces a full recomputation.  This only removes redundant work; it never
relaxes any check, and every cached value is still produced by
:func:`orchestrator.runtime_contract.current_runtime_fingerprint`.

This module is intentionally **not** part of the protected Core manifest.  It
only memoizes; the fail-closed runtime-contract comparisons that consume the
fingerprint are unchanged.
"""

from __future__ import annotations

import hashlib
import os
import sys
import threading
from pathlib import Path

from orchestrator.runtime_contract import (
    RuntimeFingerprint,
    core_source_digest,
    current_runtime_fingerprint,
    runtime_policy_digest,
)

_lock = threading.Lock()
_cache: dict[str, tuple[str, RuntimeFingerprint]] = {}
_stats: dict[str, int] = {"hits": 0, "misses": 0}


def _distribution_metadata_bytes(entry: str, name: str, dirent: os.DirEntry) -> bytes:
    """Return the exact metadata bytes ``dependency_digest`` derives from."""
    base = Path(entry) / name
    try:
        if dirent.is_dir(follow_symlinks=False):
            for meta in ("METADATA", "PKG-INFO"):
                candidate = base / meta
                if candidate.is_file():
                    return candidate.read_bytes()
            return b"<no-metadata>"
        return base.read_bytes()
    except OSError:
        return b"<unreadable>"


def _distribution_set_signature() -> str:
    """Cheap, deterministic signature of the installed distribution inputs.

    ``dependency_digest`` hashes every installed distribution's ``Name`` and
    ``Version`` (read from ``METADATA``/``PKG-INFO``).  Recomputing that map is
    the dominant cost of the fingerprint.  Hashing the same metadata files
    directly is far cheaper than constructing
    ``importlib.metadata.Distribution`` objects, and still detects add/remove
    and in-place version changes of installed distributions.
    """
    digest = hashlib.sha256()
    entries: list[tuple[str, str, bytes]] = []
    for entry in sys.path:
        if not entry or not isinstance(entry, str):
            continue
        try:
            dirents = list(os.scandir(entry))
        except OSError:
            continue
        for dirent in dirents:
            name = dirent.name
            if not (name.endswith(".dist-info") or name.endswith(".egg-info")):
                continue
            entries.append((name, entry, _distribution_metadata_bytes(entry, name, dirent)))
    for name, entry, payload in sorted(entries, key=lambda item: (item[0], item[1])):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(payload)
        digest.update(b"\n")
    return digest.hexdigest()


def _fingerprint_signature(policy, code_root: Path) -> str:
    parts = (
        runtime_policy_digest(code_root, policy),
        core_source_digest(code_root),
        _distribution_set_signature(),
    )
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def cached_current_runtime_fingerprint(
    policy,
    *,
    code_root: Path,
) -> RuntimeFingerprint:
    """Return the process snapshot of ``current_runtime_fingerprint``.

    The snapshot is recomputed whenever the policy/lock, the protected Core
    source, or the installed distribution set changes.  Thread-safe.
    """
    key = str(Path(code_root).resolve())
    with _lock:
        signature = _fingerprint_signature(policy, code_root)
        cached = _cache.get(key)
        if cached is not None and cached[0] == signature:
            _stats["hits"] += 1
            return cached[1]
        fingerprint = current_runtime_fingerprint(policy, code_root=code_root)
        _cache[key] = (signature, fingerprint)
        _stats["misses"] += 1
        return fingerprint


def invalidate_runtime_fingerprint_cache() -> None:
    """Observable invalidation entry: drop every snapshot and reset counters."""
    with _lock:
        _cache.clear()
        _stats["hits"] = 0
        _stats["misses"] = 0


def runtime_fingerprint_cache_stats() -> dict[str, int]:
    """Observable cache statistics for diagnostics and tests."""
    with _lock:
        return {"hits": _stats["hits"], "misses": _stats["misses"], "entries": len(_cache)}
