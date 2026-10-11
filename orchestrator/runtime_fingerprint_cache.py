"""Process-local memoization for the HASHI runtime fingerprint.

The runtime fingerprint is deterministic for a policy, code root, and process
environment. Recomputing it is dominated by dependency_digest, which
re-enumerates every installed distribution and reads its metadata.

The cache signature covers the runtime policy and lock, protected Core source,
and installed distribution metadata. Any change invalidates the snapshot and
forces the authoritative fingerprint calculation. This removes redundant work
without relaxing a runtime-contract comparison.
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
    """Return the metadata bytes that identify one installed distribution."""
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
    """Return a cheap deterministic signature of installed distributions."""
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
            entries.append(
                (name, entry, _distribution_metadata_bytes(entry, name, dirent))
            )
    for name, entry, payload in sorted(entries, key=lambda item: (item[0], item[1])):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(entry.encode("utf-8"))
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
    """Return the current fingerprint, reusing it only while all inputs match."""
    resolved_root = Path(code_root).resolve()
    key = str(resolved_root)
    with _lock:
        signature = _fingerprint_signature(policy, resolved_root)
        cached = _cache.get(key)
        if cached is not None and cached[0] == signature:
            _stats["hits"] += 1
            return cached[1]
        fingerprint = current_runtime_fingerprint(policy, code_root=resolved_root)
        _cache[key] = (signature, fingerprint)
        _stats["misses"] += 1
        return fingerprint


def invalidate_runtime_fingerprint_cache() -> None:
    """Drop every process-local snapshot and reset observable counters."""
    with _lock:
        _cache.clear()
        _stats["hits"] = 0
        _stats["misses"] = 0


def runtime_fingerprint_cache_stats() -> dict[str, int]:
    """Return cache diagnostics for tests and runtime observation."""
    with _lock:
        return {
            "hits": _stats["hits"],
            "misses": _stats["misses"],
            "entries": len(_cache),
        }
