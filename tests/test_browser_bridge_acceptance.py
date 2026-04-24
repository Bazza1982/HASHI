from __future__ import annotations

import json
from pathlib import Path

from tools.browser_bridge_acceptance import (
    load_smoke_results,
    summarize_smoke_results,
    summarize_stub_trace,
    write_acceptance_summary,
)


def _write_smoke_results(root: Path, *, status: str = "manual_required") -> None:
    state = root / "state"
    state.mkdir(parents=True, exist_ok=True)
    payload = {
        "root_dir": str(root),
        "generated_at": "2026-04-25T00:00:00+00:00",
        "status": status,
        "results": [
            {"id": "launch_chrome", "status": "manual_required"},
            {"id": "healthcheck", "status": "passed"},
            {"id": "ping", "status": "passed"},
            {"id": "active_tab", "status": "passed"},
            {"id": "get_text", "status": "passed"},
            {"id": "screenshot", "status": "passed"},
        ],
    }
    (state / "smoke_results.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _write_stub_trace(root: Path) -> None:
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    events = [
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
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )


def test_load_smoke_results(tmp_path: Path) -> None:
    _write_smoke_results(tmp_path)
    loaded = load_smoke_results(tmp_path)
    assert loaded["status"] == "manual_required"


def test_summarize_smoke_results_promotable(tmp_path: Path) -> None:
    _write_smoke_results(tmp_path)
    _write_stub_trace(tmp_path)
    summary = summarize_smoke_results(tmp_path)
    assert summary["counts"]["passed"] == 5
    assert summary["counts"]["manual_required"] == 1
    assert summary["promotable_to_live_acceptance"] is True
    assert summary["failed_steps"] == []
    assert summary["trace_summary"]["trace_ok"] is True


def test_summarize_smoke_results_failed_blocker(tmp_path: Path) -> None:
    _write_smoke_results(tmp_path)
    _write_stub_trace(tmp_path)
    path = tmp_path / "state" / "smoke_results.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["results"][2]["status"] = "failed"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    summary = summarize_smoke_results(tmp_path)
    assert summary["promotable_to_live_acceptance"] is False
    assert summary["failed_steps"] == ["ping"]
    assert summary["blockers"] == ["ping"]


def test_write_acceptance_summary(tmp_path: Path) -> None:
    _write_smoke_results(tmp_path)
    _write_stub_trace(tmp_path)
    summary = write_acceptance_summary(tmp_path)
    saved = json.loads((tmp_path / "state" / "acceptance_summary.json").read_text(encoding="utf-8"))
    assert saved == summary


def test_summarize_stub_trace_bad_order(tmp_path: Path) -> None:
    _write_smoke_results(tmp_path)
    _write_stub_trace(tmp_path)
    path = tmp_path / "logs" / "stub_bridge_trace.jsonl"
    bad_events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    bad_events[5]["action"] = "get_text"
    path.write_text("\n".join(json.dumps(event) for event in bad_events) + "\n", encoding="utf-8")

    trace_summary = summarize_stub_trace(tmp_path)
    assert trace_summary is not None
    assert trace_summary["trace_ok"] is False
