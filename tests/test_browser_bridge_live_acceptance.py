from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.browser_bridge_live_acceptance import (
    build_live_acceptance_runbook,
    write_live_acceptance_runbook,
)


def _write_acceptance_inputs(root: Path, *, promotable: bool = True) -> None:
    state = root / "state"
    logs = root / "logs"
    state.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)

    smoke_results = {
        "root_dir": str(root),
        "generated_at": "2026-04-25T00:00:00+00:00",
        "status": "manual_required",
        "results": [
            {"id": "launch_chrome", "status": "manual_required"},
            {"id": "healthcheck", "status": "passed"},
            {"id": "ping", "status": "passed"},
            {"id": "active_tab", "status": "passed"},
            {"id": "get_text", "status": "passed"},
            {"id": "screenshot", "status": "passed"},
        ],
    }
    if not promotable:
        smoke_results["results"][2]["status"] = "failed"
    (state / "smoke_results.json").write_text(json.dumps(smoke_results, indent=2) + "\n", encoding="utf-8")

    trace_events = [
        {"event": "server_started", "socket_path": "/tmp/harness.sock"},
        {"event": "request", "action": "ping"},
        {"event": "response", "action": "ping", "ok": True},
        {"event": "request", "action": "ping"},
        {"event": "response", "action": "ping", "ok": True},
        {"event": "request", "action": "active_tab"},
        {"event": "response", "action": "active_tab", "ok": True},
        {"event": "request", "action": "get_text"},
        {"event": "response", "action": "get_text", "ok": True},
        {"event": "request", "action": "screenshot"},
        {"event": "response", "action": "screenshot", "ok": True},
        {"event": "server_stopped", "socket_path": "/tmp/harness.sock"},
    ]
    (logs / "stub_bridge_trace.jsonl").write_text(
        "\n".join(json.dumps(event) for event in trace_events) + "\n",
        encoding="utf-8",
    )


def test_build_live_acceptance_runbook(tmp_path: Path) -> None:
    _write_acceptance_inputs(tmp_path, promotable=True)
    runbook = build_live_acceptance_runbook(tmp_path, rollback_commit="deadbeef")
    assert runbook["rollback_commit"] == "deadbeef"
    assert runbook["mode"] == "non_destructive_live_acceptance"
    assert "healthcheck passes" in runbook["success_criteria"]


def test_build_live_acceptance_runbook_rejects_unpromotable(tmp_path: Path) -> None:
    _write_acceptance_inputs(tmp_path, promotable=False)
    with pytest.raises(ValueError):
        build_live_acceptance_runbook(tmp_path, rollback_commit="deadbeef")


def test_write_live_acceptance_runbook(tmp_path: Path) -> None:
    _write_acceptance_inputs(tmp_path, promotable=True)
    runbook = write_live_acceptance_runbook(tmp_path, rollback_commit="deadbeef")
    saved = json.loads((tmp_path / "state" / "live_acceptance_runbook.json").read_text(encoding="utf-8"))
    assert saved == runbook
