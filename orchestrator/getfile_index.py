"""PAO's bounded recent-file projection of committed canonical evidence.

This disposable index is never an audit writer or a filesystem/access grant.
No watcher, whole-drive scan, provider inference, or Fingerprint dependency.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
from typing import Mapping

from orchestrator.getfile_recent import select_recent_agent_files

logger = logging.getLogger(__name__)
_EXCLUDED = frozenset({".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", ".hashi"})
_KEEP = 200
_RECOVERED = set()


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def run_scope(runtime, item):
    """Only the admission-time snapshot defines a native Engine's cwd/roots."""
    metadata = dict(getattr(item, "request_metadata", None) or {})
    snapshot = metadata.get("workzone_snapshot")
    roots, primary = [], None
    if isinstance(snapshot, Mapping):
        for slot in snapshot.get("slots") or ():
            if (not isinstance(slot, Mapping) or not slot.get("enabled")
                    or not slot.get("available") or slot.get("host_compatible") is False):
                continue
            path = str(slot.get("path") or "")
            if path and path not in roots:
                roots.append(path)
                if (slot.get("slot_id") or slot.get("slot")) == "main":
                    primary = path
    if not roots:
        roots = [str(runtime.workspace_dir)]
    return {"roots": roots, "cwd": primary or roots[0],
            "owner_id": str(getattr(item, "owner_id", "") or metadata.get("owner_id") or "")}


def _candidate(raw_path, scope):
    if not isinstance(raw_path, str) or not raw_path or len(raw_path) > 4096:
        return None
    if any(ord(c) < 32 for c in raw_path):
        return None
    from tools.device_paths import wsl_to_windows_path, windows_to_wsl_path
    path = raw_path
    try:
        if os.name == "nt" and path.startswith("/"):
            path = wsl_to_windows_path(path)
        elif os.name != "nt" and len(path) > 2 and path[1] == ":":
            path = windows_to_wsl_path(path) or path
        candidate = Path(path)
        if not candidate.is_absolute():
            cwd = scope.get("cwd")
            if not cwd or not Path(cwd).is_absolute():
                return None
            candidate = Path(cwd) / candidate
        candidate = Path(os.path.abspath(candidate))
        roots = [Path(os.path.abspath(p)) for p in scope.get("roots", ()) if Path(p).is_absolute()]
        for root in sorted(roots, key=lambda p: len(p.parts), reverse=True):
            try:
                relative = candidate.relative_to(root).as_posix()
            except ValueError:
                continue
            parts = relative.split("/")
            if any(p.casefold() in _EXCLUDED for p in parts) or relative.endswith((".tmp", ".pyc")):
                return None
            if "canonical_audit" in parts[:-1] or relative.startswith("state/getfile/"):
                return None
            root_id = hashlib.sha256(os.path.normcase(str(root)).encode()).hexdigest()[:32]
            return root, root_id, relative
    except (OSError, ValueError):
        return None
    return None


class RecentFileIndex:
    def __init__(self, audit):
        self.audit = audit
        self.path = audit.root / "recent-files.sqlite3"
        self.dirty = audit.root / "recent-files.needs-rebuild"

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.executescript("""
            CREATE TABLE IF NOT EXISTS recent(
              owner TEXT NOT NULL, path_key TEXT NOT NULL, observed_at TEXT NOT NULL,
              record TEXT NOT NULL, PRIMARY KEY(owner,path_key));
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        return db

    def ingest(self, records):
        """Called after canonical commit; replays are idempotent and time ordered."""
        observations = []
        expanded = []
        for event in records:
            payload = event.get("payload") or {}
            if isinstance(payload, Mapping):
                if event.get("event_type") == "provider_stream_event" and payload.get("kind") == "file_edit":
                    metadata = payload.get("metadata")
                    metadata = metadata if isinstance(metadata, Mapping) else {}
                    paths = metadata.get("file_paths") or [payload.get("file_path")]
                    if isinstance(paths, (tuple, list)):
                        for path in paths[:100]:
                            expanded.append({**event, "payload": {**payload, "file_path": path}})
                        continue
                if event.get("event_type") == "tool_call" and not payload.get("is_error"):
                    details = payload.get("details")
                    details = details if isinstance(details, Mapping) else {}
                    shell_rows = details.get("file_observations")
                    for item in (shell_rows[:8] if isinstance(shell_rows, (tuple, list)) else ()):
                        expanded.append({**event, "event_type": "shell_file_observation", "payload": item})
            expanded.append(event)
        for event in expanded:
            payload, provenance = event.get("payload"), event.get("provenance") or {}
            if not isinstance(payload, Mapping) or not isinstance(provenance, Mapping):
                continue
            kind = event.get("event_type")
            action, grade, raw = "touched", "engine_reported", ""
            if kind == "tool_call" and not payload.get("is_error"):
                if str(payload.get("output") or "").startswith("Error"):
                    continue
                if payload.get("tool_name") not in {"file_write", "apply_patch"}:
                    continue
                scope = {"roots": provenance.get("access_roots") or (),
                         "cwd": provenance.get("workspace_dir"),
                         "owner_id": provenance.get("owner_id")}
                details, arguments = payload.get("details"), payload.get("arguments")
                details = details if isinstance(details, Mapping) else {}
                arguments = arguments if isinstance(arguments, Mapping) else {}
                effect = details.get("file_effect")
                effect = effect if isinstance(effect, Mapping) else {}
                raw = arguments.get("path")
                if effect.get("verified") is True and effect.get("path") == str(raw):
                    action, grade = effect.get("action", "touched"), "verified"
            elif kind == "shell_file_observation":
                scope = {"roots": provenance.get("access_roots") or (),
                         "cwd": provenance.get("workspace_dir"), "owner_id": provenance.get("owner_id")}
                raw, grade = payload.get("path"), "interval_observation"
            elif kind == "provider_stream_event" and payload.get("kind") == "file_edit":
                scope = provenance.get("getfile_scope") or {}
                raw = payload.get("file_path")
            else:
                continue
            if not isinstance(scope, Mapping):
                continue
            owner = scope.get("owner_id")
            if not isinstance(owner, str) or not owner:
                continue
            target = _candidate(raw, scope)
            if target is None:
                continue
            root, root_id, relative = target
            candidate = {"root_id": root_id, "relative_path": relative,
                         "action": action, "evidence": grade,
                         "observed_at": event.get("recorded_at") or utc_now(),
                         "source_event_id": str(event.get("event_id") or "")}
            selected = select_recent_agent_files([candidate])
            if not selected:
                continue
            row = {**selected[0], "root_path": str(root), "request_id": str(event.get("request_id") or "")}
            key = root_id + ":" + (relative.casefold() if os.name == "nt" else relative)
            observations.append((owner, key, row["observed_at"], json.dumps(row)))
        if not observations:
            return
        with closing(self._connect()) as db, db:
            db.executemany("""INSERT INTO recent VALUES(?,?,?,?)
                ON CONFLICT(owner,path_key) DO UPDATE SET observed_at=excluded.observed_at,record=excluded.record
                WHERE excluded.observed_at>=recent.observed_at""", observations)
            for owner in {row[0] for row in observations}:
                db.execute("""DELETE FROM recent WHERE owner=? AND path_key NOT IN
                    (SELECT path_key FROM recent WHERE owner=? ORDER BY observed_at DESC,rowid DESC LIMIT ?)""",
                    (owner, owner, _KEEP))
            db.execute("INSERT OR REPLACE INTO meta VALUES('last_event',?)",
                       (str(records[-1].get("event_id") or ""),))

    def rebuild(self):
        """Explicit bounded recovery. Never use the full audit reader."""
        records = self.audit.recent_projection_events(authorization={
            "allow_raw_audit": True, "actor": "PAO.recent-file-index",
            "purpose": "Rebuild this Agent-owned derived file index"}, max_bytes=4 * 1024 * 1024, limit=2000)
        self.ingest(records)
        # Remember an empty rebuild, so an empty menu doesn't rescan the audit.
        with closing(self._connect()) as db, db:
            db.execute("INSERT OR REPLACE INTO meta VALUES('rebuilt_at',?)", (utc_now(),))
        self.dirty.unlink(missing_ok=True)

    def recent(self, *, owner_id, limit=10):
        recovery_key = (os.getpid(), str(self.path))
        if recovery_key not in _RECOVERED or not self.path.exists() or self.dirty.exists():
            self.rebuild()
            _RECOVERED.add(recovery_key)
        with closing(self._connect()) as db:
            records = [json.loads(r[0]) for r in db.execute(
                "SELECT record FROM recent WHERE owner=? ORDER BY observed_at DESC,rowid DESC LIMIT ?",
                (owner_id, _KEEP))]
        by_key = {(r["root_id"], r["relative_path"]): r for r in records}
        return [by_key[(r["root_id"], r["relative_path"])] for r in select_recent_agent_files(
            records, limit=limit, case_sensitive=os.name != "nt")]
