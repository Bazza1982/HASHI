"""Preserve verified pre-forget Memory+ archives for the new Session.

Only archives from the exact pending Agent's workspaces may be copied.  A
content digest and conservative time bound follow each copied file so a later
forget cannot mistake a fresh copy for fresh knowledge.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any, Iterator

_ARCHIVE_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})_memory_plus_state\.json$")
_MAX_ARCHIVE_BYTES = 1_048_576


def _manifest(workspace: Path) -> dict[str, tuple[str, float]]:
    database = workspace / "bridge_memory.sqlite"
    if not database.exists():
        return {}
    if database.is_symlink():
        raise RuntimeError("memory archive manifest database is a link")
    try:
        with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as conn:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pcm_safe_memory_archives'"
            ).fetchone()
            if not exists:
                return {}
            return {str(row[0]): (str(row[1]), float(row[2])) for row in conn.execute(
                "SELECT relative_path, sha256, verified_before FROM pcm_safe_memory_archives"
            )}
    except (sqlite3.Error, ValueError, TypeError) as exc:
        raise RuntimeError("cannot verify memory archive manifest") from exc


def _no_links(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    current = root
    if current.is_symlink():
        return False
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return False
    return True


def verified_memory_plus_archives(
    workspace: Path, cutoff: float,
) -> Iterator[tuple[Path, dict[str, Any], bytes, float]]:
    """Read only immutable, provenance-verified archives predating cutoff."""
    from orchestrator.pcm_forget_artifacts import _unchanged_before
    from orchestrator.memory_plus_mode import MEMORY_PLUS_SCHEMA_VERSION, _memory_paths

    workspace = Path(workspace)
    archive_dir = _memory_paths(workspace)[3]
    if not archive_dir.is_dir() or not _no_links(archive_dir, workspace):
        return
    manifest = _manifest(workspace)
    for path in sorted(archive_dir.rglob("*_memory_plus_state.json")):
        match = _ARCHIVE_NAME.fullmatch(path.name)
        if not match or not _no_links(path, workspace):
            continue
        try:
            date.fromisoformat(match.group(1))
            if path.stat().st_size > _MAX_ARCHIVE_BYTES:
                continue
            old_file = _unchanged_before(path, cutoff)
            relative = path.relative_to(workspace).as_posix()
            recorded = manifest.get(relative)
            if not old_file and (recorded is None or recorded[1] > cutoff):
                continue
            raw = path.read_bytes()
            if len(raw) > _MAX_ARCHIVE_BYTES:
                continue
            digest = hashlib.sha256(raw).hexdigest()
            if old_file:
                if not _unchanged_before(path, cutoff):
                    continue
                verified_before = cutoff
            else:
                if digest != recorded[0] or not _no_links(path, workspace):
                    continue
                if path.read_bytes() != raw:
                    continue
                verified_before = recorded[1]
            state = json.loads(raw)
            if (not isinstance(state, dict)
                    or state.get("schema_version") != MEMORY_PLUS_SCHEMA_VERSION
                    or state.get("date") != match.group(1)
                    or not isinstance(state.get("today"), dict)
                    or not isinstance(state.get("carryover"), dict)):
                continue
            yield path, state, raw, verified_before
        except (OSError, UnicodeError, ValueError, TypeError):
            continue


def is_verified_archive_copy(workspace: Path, path: Path, cutoff: float) -> bool:
    """Recognize only an unchanged, previously verified local archive copy."""
    workspace, path = Path(workspace), Path(path)
    if not _no_links(path, workspace):
        return False
    try:
        relative = path.relative_to(workspace).as_posix()
        if not relative.startswith("memory/memory_plus_wiki/"):
            return False
        recorded = _manifest(workspace).get(relative)
        if recorded is None or recorded[1] > cutoff or path.stat().st_size > _MAX_ARCHIVE_BYTES:
            return False
        raw = path.read_bytes()
        return (len(raw) <= _MAX_ARCHIVE_BYTES
                and hashlib.sha256(raw).hexdigest() == recorded[0]
                and _no_links(path, workspace) and path.read_bytes() == raw)
    except (OSError, ValueError):
        return False


def carry_safe_memory_plus_archives(
    store: Any, *, operation_id: str, owner_id: str, agent_id: str,
    agent_home: Path, new_session_id: str,
) -> int:
    """Copy proven pre-forget archives into this Agent's fresh Session."""
    from orchestrator.bridge_memory import BridgeMemoryStore
    from orchestrator.pcm_forget_memory import inherit_forget_fences, oldest_forget_start
    from orchestrator.session_store import SessionConflict

    pending = store.pending_pcm_forget(owner_id=owner_id, agent_id=agent_id)
    if pending is None or pending["operation_id"] != operation_id:
        raise SessionConflict("forget operation unavailable")
    session = store.get_session(new_session_id, owner_id=owner_id, agent_id=agent_id, include_deleted=False)
    workspace = store.session_workspace(new_session_id, int(session["context_generation"]))
    home = Path(agent_home)
    if not _no_links(home, home) or not _no_links(workspace, store.workspaces_root):
        raise SessionConflict("memory archive workspace is a link")
    cutoff = oldest_forget_start(home)
    if cutoff is None:
        raise SessionConflict("forget boundary unavailable")
    destination_store = BridgeMemoryStore(workspace)
    inherit_forget_fences(home, destination_store)

    sources = [home]
    for session_id in pending["result"].get("all_session_ids", pending["result"]["session_ids"]):
        if not session_id or any(not (char.isalnum() or char in "_-") for char in session_id):
            raise SessionConflict("invalid forget session id")
        root = store.workspaces_root / session_id
        if not _no_links(root, store.workspaces_root):
            raise SessionConflict("session archive workspace is a link")
        if root.exists():
            for generation in sorted(root.glob("generation_*")):
                if not _no_links(generation, root):
                    raise SessionConflict("session archive generation is a link")
                if generation.is_dir() and generation != workspace:
                    sources.append(generation)

    copied = 0
    for source in sources:
        for old_path, _state, raw, verified_before in verified_memory_plus_archives(source, cutoff):
            digest = hashlib.sha256(raw).hexdigest()
            relative = Path("memory") / "memory_plus_wiki" / "_forget_restored" / digest / old_path.name
            destination = workspace / relative
            if not _no_links(destination, workspace):
                raise SessionConflict("memory archive destination is a link")
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not _no_links(destination, workspace):
                raise SessionConflict("memory archive destination changed")
            try:
                with destination.open("xb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
            except FileExistsError:
                if destination.is_symlink() or destination.read_bytes() != raw:
                    raise SessionConflict("memory archive destination conflicts")
            with destination_store._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS pcm_safe_memory_archives (
                       relative_path TEXT PRIMARY KEY, sha256 TEXT NOT NULL,
                       verified_before REAL NOT NULL)"""
                )
                existing = conn.execute(
                    "SELECT sha256 FROM pcm_safe_memory_archives WHERE relative_path=?",
                    (relative.as_posix(),),
                ).fetchone()
                if existing is not None and str(existing[0]) != digest:
                    raise SessionConflict("memory archive manifest conflicts")
                conn.execute(
                    """INSERT INTO pcm_safe_memory_archives(relative_path, sha256, verified_before)
                       VALUES (?, ?, ?) ON CONFLICT(relative_path) DO UPDATE SET
                       verified_before=MIN(verified_before, excluded.verified_before)""",
                    (relative.as_posix(), digest, verified_before),
                )
            copied += 1
    return copied
