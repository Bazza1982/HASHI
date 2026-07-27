from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Callable


# importlib.reload() reuses the module dictionary. Preserve live locks so a
# /reboot cannot split concurrent state writers across old and new lock maps.
_LOCKS_GUARD = globals().get("_LOCKS_GUARD") or threading.Lock()
_LOCKS: dict[str, threading.RLock] = globals().get("_LOCKS") or {}


def _path_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


class WorkspaceStateStore:
    """The only persistence boundary for a workspace's shared state.json."""

    def __init__(self, workspace_dir: Path):
        self.path = Path(workspace_dir) / "state.json"
        self._lock = _path_lock(self.path)

    def read(self) -> dict:
        with self._lock:
            if not self.path.exists():
                return {}
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                return {}
            return payload if isinstance(payload, dict) else {}

    def replace(self, payload: dict) -> dict:
        snapshot = dict(payload)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.path.with_name(
                f".{self.path.name}.tmp-{os.getpid()}-{threading.get_ident()}"
            )
            temp_path.write_text(
                json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temp_path.replace(self.path)
        return snapshot

    def update(self, mutator: Callable[[dict], dict | None]) -> dict:
        """Atomically read, mutate, and replace state within this process."""
        with self._lock:
            current = self.read()
            result = mutator(current)
            updated = current if result is None else result
            if not isinstance(updated, dict):
                raise TypeError("Workspace state mutator must return a dict or None")
            return self.replace(updated)
