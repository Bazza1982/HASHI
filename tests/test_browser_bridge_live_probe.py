from __future__ import annotations

import json
from pathlib import Path

from tools.browser_bridge_live_probe import (
    build_live_probe_plan,
    write_live_probe_plan,
)


def _write_acceptance_inputs(root: Path) -> None:
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


def test_build_live_probe_plan(tmp_path: Path) -> None:
    _write_acceptance_inputs(tmp_path)
    plan = build_live_probe_plan(
        tmp_path,
        rollback_commit="deadbeef",
        live_socket_path="/tmp/live.sock",
        benign_url="https://example.com",
    )
    assert plan["rollback_commit"] == "deadbeef"
    assert plan["live_socket_path"] == "/tmp/live.sock"
    assert [step["id"] for step in plan["steps"]] == [
        "healthcheck",
        "ping",
        "active_tab",
        "get_text",
        "screenshot",
    ]


def test_write_live_probe_plan(tmp_path: Path) -> None:
    _write_acceptance_inputs(tmp_path)
    plan = write_live_probe_plan(tmp_path, rollback_commit="deadbeef")
    saved = json.loads((tmp_path / "state" / "live_probe_plan.json").read_text(encoding="utf-8"))
    assert saved == plan
