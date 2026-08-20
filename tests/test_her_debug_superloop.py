from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _controller_module():
    path = ROOT / "scripts" / "her_debug_superloop.py"
    spec = importlib.util.spec_from_file_location("her_debug_superloop", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_in_progress_packet_continuation_cannot_require_new_start_authority() -> None:
    controller = _controller_module()
    tasks = [{"task_id": "HD-003", "status": "in_progress"}]
    state = {
        "current_step": "HD-003",
        "active_dispatch_id": None,
        "active_wait_id": None,
        "selected_next_packet": {
            "work_item_id": "HER-LIVE-DS-FLASH-FIXED-LOW",
            "scenario": "C00",
            "started": False,
            "pending_non_nudge_start_authority": True,
        },
        "next_action": {
            "kind": "await_non_nudge_start_authority",
            "pending_non_nudge_start_authority": True,
        },
    }

    assert controller._in_progress_packet_start_authority_conflict(state, tasks) is True

    state["operator_execution_authority"] = {
        "status": "active",
        "scope": "campaign_until_terminal",
    }
    state["selected_next_packet"]["pending_non_nudge_start_authority"] = False
    state["next_action"] = {"kind": "dispatch_selected_packet"}
    assert controller._in_progress_packet_start_authority_conflict(state, tasks) is False

    tasks[0]["status"] = "pending"
    state["selected_next_packet"]["pending_non_nudge_start_authority"] = True
    state["next_action"] = {"kind": "await_non_nudge_start_authority"}
    assert controller._in_progress_packet_start_authority_conflict(state, tasks) is False
