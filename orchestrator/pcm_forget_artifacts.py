"""Handle tracked local memories and preserve untraceable continuity artifacts."""
from __future__ import annotations

import os
from pathlib import Path

from orchestrator.session_store import SessionConflict, SessionStore


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


def validate_untracked_continuity_scope(
    store: SessionStore, *, operation_id: str, owner_id: str, agent_id: str,
    agent_home: Path, session_ids: list[str],
) -> int:
    """Validate pending scope without changing summaries of unknown origin.

    An untraceable summary may still contain information from the interval.
    """
    pending = store.pending_pcm_forget(owner_id=owner_id, agent_id=agent_id)
    if pending is None or pending["operation_id"] != operation_id:
        raise SessionConflict("forget operation unavailable")
    allowed = set(pending["result"].get("all_session_ids", pending["result"]["session_ids"]))
    if set(session_ids) != allowed:
        raise SessionConflict("forget workspace scope changed")
    home = Path(agent_home)
    if home.is_symlink() or not home.is_dir():
        raise SessionConflict("Agent workspace unavailable or linked")
    return 0


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
                for name in (
                    "bridge_memory.sqlite", "transcript.jsonl", "recent_context.jsonl",
                    "memory/memory_plus_state.json", "memory/memory_plus_index.json",
                    "memory/memory_plus_wiki",
                )
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
