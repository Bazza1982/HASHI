from __future__ import annotations

import json
import importlib

from orchestrator import workspace_state
from orchestrator.workspace_state import WorkspaceStateStore


def test_workspace_state_update_preserves_unowned_blocks(tmp_path):
    store = WorkspaceStateStore(tmp_path)
    store.replace({"active_backend": "codex-cli", "memory_plus": {"enabled": True}})

    store.update(lambda state: state.update({"privacy_level": 1}))

    assert store.read() == {
        "active_backend": "codex-cli",
        "memory_plus": {"enabled": True},
        "privacy_level": 1,
    }


def test_workspace_state_replace_is_valid_json_and_leaves_no_temp_file(tmp_path):
    store = WorkspaceStateStore(tmp_path)

    store.replace({"agent_mode": "dual-brain", "label": "月如"})

    assert json.loads(store.path.read_text(encoding="utf-8"))["label"] == "月如"
    assert list(tmp_path.glob(".state.json.tmp-*")) == []


def test_workspace_state_lock_survives_hot_module_reload(tmp_path):
    before = workspace_state._path_lock(tmp_path / "state.json")

    reloaded = importlib.reload(workspace_state)
    after = reloaded._path_lock(tmp_path / "state.json")

    assert after is before
