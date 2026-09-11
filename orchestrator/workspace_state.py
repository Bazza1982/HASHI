from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

from orchestrator.config_json import read_config_json, write_config_json
from orchestrator.process_resources import path_lock as process_path_lock


def _path_lock(path: Path) -> threading.RLock:
    return process_path_lock(path)


class WorkspaceStateStore:
    """The only persistence boundary for a workspace's shared state.json."""

    def __init__(self, workspace_dir: Path):
        self.path = Path(workspace_dir) / "state.json"
        self._lock = _path_lock(self.path)

    def _read_for_update(self) -> tuple[dict, str | None]:
        """Only absence permits an empty writable state; other errors propagate."""
        try:
            document = read_config_json(self.path)
        except FileNotFoundError:
            return {}, None
        return dict(document), document.revision

    def read(self) -> dict:
        """Return a plain view, retaining the existing read-only error fallback."""
        with self._lock:
            try:
                return self._read_for_update()[0]
            except (OSError, ValueError):
                return {}

    def _publish(self, payload: dict, revision: str | None) -> dict:
        snapshot = dict(payload)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        write_config_json(self.path, snapshot, expected_revision=revision)
        return snapshot

    def replace(self, payload: dict) -> dict:
        """Explicit whole-document replacement, checked from this call's read.

        This cannot recover the revision of an earlier plain-dict snapshot.
        Use update() for read/modify/write operations; a read fallback is not
        a replacement source. An unreadable existing file is never overwritten.
        """
        with self._lock:
            _current, revision = self._read_for_update()
            return self._publish(payload, revision)

    def update(self, mutator: Callable[[dict], dict | None]) -> dict:
        """Read strictly, mutate once, and reject stale publication across processes.

        The callback should only mutate its supplied document. A conflict or
        committed durability error propagates without replaying the callback or
        rolling back a published file. The process lock also retains the existing
        within-process ordering; interprocess protection is the file primitive's.
        """
        with self._lock:
            current, revision = self._read_for_update()
            result = mutator(current)
            updated = current if result is None else result
            if not isinstance(updated, dict):
                raise TypeError("Workspace state mutator must return a dict or None")
            return self._publish(updated, revision)
