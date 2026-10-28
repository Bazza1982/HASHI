"""Quarantine continuity artifacts which have no per-item source lineage.

The caller must already hold a durable, owner-scoped pending forget operation.
All contents are copied to the private audit table before removing them from
active PCM.  Interrupted work can be retried without replacing a newer edit.
"""
from __future__ import annotations

import os
from pathlib import Path

from orchestrator.memory_plus_mode import _memory_paths, _memory_plus_store_lock, load_memory_plus_state
from orchestrator.pcm import pcm_block_body_span, update_pcm_text
from orchestrator.session_store import SessionConflict, SessionStore


def _audit(store: SessionStore, *, owner: str, agent: str, operation: str,
           identity: str, content: str) -> None:
    store.audit_pcm_forget_artifact(
        owner_id=owner, agent_id=agent, operation_id=operation,
        identity=identity, content=content,
    )


def _unchanged_before(path: Path, cutoff: float) -> bool:
    """Prove an artifact was neither written nor metadata-changed in the interval."""
    if path.is_symlink():
        return False
    try:
        if os.name != "nt":
            stat = path.stat()
            return stat.st_mtime < cutoff and stat.st_ctime < cutoff
        # Windows st_ctime is creation time. Query NTFS ChangeTime as well,
        # which also changes when an old mtime is restored after a rewrite.
        import ctypes
        import msvcrt
        from ctypes import wintypes

        class FileBasicInfo(ctypes.Structure):
            _fields_ = [("creation", ctypes.c_longlong), ("access", ctypes.c_longlong),
                        ("write", ctypes.c_longlong), ("change", ctypes.c_longlong),
                        ("attributes", wintypes.DWORD)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_info = kernel32.GetFileInformationByHandleEx
        get_info.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        get_info.restype = wintypes.BOOL
        info = FileBasicInfo()
        with path.open("rb") as handle:
            if not get_info(msvcrt.get_osfhandle(handle.fileno()), 0,
                            ctypes.byref(info), ctypes.sizeof(info)):
                return False
        epochs = [value / 10_000_000 - 11_644_473_600
                  for value in (info.creation, info.write, info.change)]
        return all(value > 0 and value < cutoff for value in epochs)
    except (OSError, ValueError, AttributeError):
        return False


def _clear_pcm_blocks(store: SessionStore, home: Path, *, owner: str, agent: str,
                      operation: str, cutoff: float) -> int:
    target = home / "agent.md"
    if not target.exists():
        return 0
    if target.is_symlink():
        raise SessionConflict("PCM file is a link")
    count = 0

    def mutate(text: str) -> str:
        nonlocal count
        if _unchanged_before(target, cutoff):
            return text
        replacements = []
        for name in ("memory", "hcc"):
            span = pcm_block_body_span(text, name)
            if span is None:
                continue
            start, end = span
            old = text[start:end]
            if not old.strip():
                continue
            _audit(store, owner=owner, agent=agent, operation=operation,
                   identity=f"agent:agent.md#{name}", content=old)
            if name == "memory":
                # The optional memory block must either contain text or be
                # absent; unlike HCC, an empty memory block is invalid.
                opening = text.rfind("\n", 0, start - 1) + 1
                closing_newline = text.find("\n", end)
                closing = len(text) if closing_newline < 0 else closing_newline + 1
                replacements.append((opening, closing))
            else:
                replacements.append((start, end))
            count += 1
        for start, end in sorted(replacements, reverse=True):
            text = text[:start] + text[end:]
        return text

    update_pcm_text(home, mutate)
    return count


def _remove_file(store: SessionStore, path: Path, *, base: Path, label: str,
                 owner: str, agent: str, operation: str, cutoff: float) -> int:
    if path.is_symlink():
        raise SessionConflict("continuity artifact is a link")
    if not path.is_file():
        return 0
    if not path.resolve().is_relative_to(base.resolve()):
        raise SessionConflict("continuity artifact escaped its workspace")
    if _unchanged_before(path, cutoff):
        return 0
    original = path.read_bytes()
    content = original.decode("utf-8")
    _audit(store, owner=owner, agent=agent, operation=operation,
           identity=label, content=content)
    if path.is_symlink() or path.read_bytes() != original:
        raise SessionConflict("forget artifact changed after its audit copy")
    path.unlink()
    return 1


def _clear_workspace(store: SessionStore, workspace: Path, *, label: str,
                     owner: str, agent: str, operation: str, cutoff: float) -> int:
    if workspace.is_symlink():
        raise SessionConflict("session workspace is a link")
    if not workspace.exists():
        return 0
    if (workspace / "memory").is_symlink():
        raise SessionConflict("continuity memory directory is a link")
    count = 0
    from orchestrator.context_compaction import CompactionStore

    compaction = CompactionStore(workspace)
    if (workspace / "backend_state").is_symlink() or compaction.root.is_symlink() or compaction.state_path.is_symlink():
        raise SessionConflict("context compaction state is a link")
    if compaction.state_path.exists():
        with compaction._lock:
            state_before = compaction.read_state()
            active = state_before.get("active_capsule")
            proven_old = False
            if active is not None and _unchanged_before(compaction.state_path, cutoff):
                try:
                    # The old pointer authenticates immutable capsule and archive
                    # bytes, so later replacement cannot change its contents.
                    compaction.load_active(state_before)
                    proven_old = _unchanged_before(compaction.state_path, cutoff)
                except Exception:
                    proven_old = False
            if active is not None and not proven_old:
                raw = compaction.state_path.read_bytes()
                _audit(store, owner=owner, agent=agent, operation=operation,
                       identity=f"{label}:context_compaction_pointer", content=raw.decode("utf-8"))
                if compaction.state_path.read_bytes() != raw:
                    raise SessionConflict("context compaction changed after its audit copy")
                compaction.reset_active_pointer()
                count += 1
    with _memory_plus_store_lock(workspace):
        state, notepad, index, archive = _memory_paths(workspace)
        if state.exists():
            # Never treat an unreadable or unsupported configuration as writable.
            load_memory_plus_state(workspace)
        memory_dir = workspace / "memory"
        paths = [
            state, notepad, index, memory_dir / "left_brain_continuity.jsonl",
            workspace / "recent_context.jsonl", workspace / "handoff.md",
        ]
        for directory in (archive, memory_dir / "left_brain_artifacts"):
            if directory.is_symlink():
                raise SessionConflict("continuity archive is a link")
            if directory.exists():
                for item in directory.rglob("*"):
                    if item.is_symlink():
                        raise SessionConflict("continuity archive item is a link")
                    if item.is_file():
                        paths.append(item)
        for path in paths:
            count += _remove_file(
                store, path, base=workspace,
                label=f"{label}:{path.relative_to(workspace).as_posix()}",
                owner=owner, agent=agent, operation=operation, cutoff=cutoff,
            )
    return count


def quarantine_unproven_continuity(
    store: SessionStore, *, operation_id: str, owner_id: str, agent_id: str,
    agent_home: Path, session_ids: list[str],
) -> int:
    """Quarantine summaries that cannot be safely split by time or origin."""
    pending = store.pending_pcm_forget(owner_id=owner_id, agent_id=agent_id)
    if pending is None or pending["operation_id"] != operation_id:
        raise SessionConflict("forget operation unavailable")
    allowed = set(pending["result"].get("all_session_ids", pending["result"]["session_ids"]))
    if set(session_ids) != allowed:
        raise SessionConflict("forget workspace scope changed")
    home = Path(agent_home)
    if home.is_symlink():
        raise SessionConflict("Agent workspace is a link")
    cutoff = float(pending["start_epoch"])
    count = _clear_pcm_blocks(store, home, owner=owner_id, agent=agent_id,
                              operation=operation_id, cutoff=cutoff)
    count += _clear_workspace(store, home, label="agent", owner=owner_id,
                              agent=agent_id, operation=operation_id, cutoff=cutoff)
    for session_id in sorted(allowed):
        if not session_id or any(not (char.isalnum() or char in "_-") for char in session_id):
            raise SessionConflict("invalid forget session id")
        root = store.workspaces_root / session_id
        if root.is_symlink():
            raise SessionConflict("session workspace is a link")
        if not root.exists():
            continue
        for workspace in sorted(root.glob("generation_*")):
            if workspace.is_symlink():
                raise SessionConflict("session generation is a link")
            if workspace.is_dir():
                count += _clear_workspace(
                    store, workspace, label=f"session:{session_id}:{workspace.name}",
                    owner=owner_id, agent=agent_id, operation=operation_id, cutoff=cutoff,
                )
    return count


def forget_local_memories(
    store: SessionStore, *, operation_id: str, owner_id: str, agent_id: str,
    agent_home: Path,
) -> dict[str, int]:
    """Remove the selected interval from the Agent and each old Session store."""
    from datetime import datetime, timezone

    from orchestrator.bridge_memory import BridgeMemoryStore

    pending = store.pending_pcm_forget(owner_id=owner_id, agent_id=agent_id)
    if pending is None or pending["operation_id"] != operation_id:
        raise SessionConflict("forget operation unavailable")
    result = pending["result"]
    start = datetime.fromtimestamp(pending["start_epoch"], timezone.utc)
    end = datetime.fromtimestamp(pending["end_epoch"], timezone.utc)
    owner, agent = str(owner_id), str(agent_id).lower()
    references = []
    with store._lock, store._connection() as connection:
        for run_id in result["run_ids"]:
            row = connection.execute(
                """SELECT r.session_id FROM runs r JOIN sessions s ON s.session_id=r.session_id
                   WHERE r.run_id=? AND s.instance_id=? AND s.agent_id=?""",
                (run_id, store.instance_id, agent),
            ).fetchone()
            if row is None:
                raise SessionConflict("forget run scope changed")
            references.append(f"session:{row['session_id']}:run:{run_id}")

    home = Path(agent_home)
    if home.is_symlink() or not home.is_dir():
        raise SessionConflict("Agent workspace unavailable or linked")
    workspaces = [home]
    for session_id in result.get("all_session_ids", result["session_ids"]):
        if not session_id or any(not (char.isalnum() or char in "_-") for char in session_id):
            raise SessionConflict("invalid forget session id")
        root = store.workspaces_root / session_id
        if root.is_symlink():
            raise SessionConflict("session workspace is a link")
        if not root.exists():
            continue
        for workspace in sorted(root.glob("generation_*")):
            if workspace.is_symlink():
                raise SessionConflict("session generation is a link")
            if workspace.is_dir() and any(
                (workspace / name).exists()
                for name in ("bridge_memory.sqlite", "transcript.jsonl", "recent_context.jsonl")
            ):
                workspaces.append(workspace)
    totals = {"stores": 0, "exchanges": 0, "turns": 0, "memories": 0, "quarantined": 0}
    for workspace in workspaces:
        memory = BridgeMemoryStore(workspace)
        report = memory.forget_window(
            start=start, end=end, naive_timezone=result.get("naive_timezone"),
            origin_refs=tuple(references),
        )
        totals["stores"] += 1
        for key in ("exchanges", "turns", "memories", "quarantined"):
            totals[key] += report[key]
    store.record_pcm_forget_step(
        owner_id=owner, agent_id=agent, operation_id=operation_id, step="local_memory",
    )
    return totals
