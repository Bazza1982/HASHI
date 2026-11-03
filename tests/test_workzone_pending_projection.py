from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from orchestrator.agent_overview import build_agent_overview


@pytest.mark.parametrize(
    "case,expected",
    [
        ("empty_after_revert", False),
        ("same_configuration_new_timestamps", False),
        ("path_changed", True),
        ("label_changed", True),
        ("disabled", True),
        ("removed", True),
        ("unknown_active", True),
        ("metadata_active", False),
    ],
)
def test_pending_means_an_effective_unadopted_configuration(tmp_path, case, expected):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    active_slot = {
        "slot_id": "main", "path": str(one), "enabled": True,
        "label": "Project", "created_at": "old", "updated_at": "old",
    }
    active = {"revision": 0, "slots": [active_slot]}
    desired = {"revision": 2, "slots": [dict(active_slot, updated_at="new")]}
    if case in {"empty_after_revert", "unknown_active"}:
        active["slots"] = []
        desired["slots"] = []
    elif case == "path_changed":
        desired["slots"][0]["path"] = str(two)
    elif case == "label_changed":
        desired["slots"][0]["label"] = "Renamed"
    elif case == "disabled":
        desired["slots"][0]["enabled"] = False
    elif case == "removed":
        desired["slots"] = []
    runtime = SimpleNamespace(name="test", workspace_dir=workspace)
    metadata = {"id": "test", "online": True}
    if case == "metadata_active":
        metadata["workzone_state"] = active
    elif case != "unknown_active":
        runtime._workzone_state = active
    before = deepcopy((active, desired, metadata, vars(runtime)))

    workzone = build_agent_overview(
        metadata=metadata, workspace_dir=workspace, runtime=runtime,
        desired_workzone_state=desired,
    )["workzone"]

    assert workzone["revision"] == 2
    assert workzone["active_revision"] == 0
    assert workzone["pending_activation"] is expected
    assert (active, desired, metadata, vars(runtime)) == before
    assert one.is_dir() and two.is_dir()
