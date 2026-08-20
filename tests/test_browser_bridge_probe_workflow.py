from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tools.browser_bridge_live_bundle import write_live_bundle
from tools.browser_bridge_live_executor import execute_live_probe_plan
from tools.browser_bridge_live_probe import write_live_probe_plan


ROOT = Path(__file__).resolve().parents[1]


def _write_acceptance_inputs(root: Path, *, promotable: bool = True) -> None:
    state = root / "state"
    logs = root / "logs"
    state.mkdir(parents=True)
    logs.mkdir(parents=True)
    results = [
        {"id": "launch_chrome", "status": "manual_required"},
        {"id": "healthcheck", "status": "passed"},
        {"id": "ping", "status": "passed" if promotable else "failed"},
        {"id": "active_tab", "status": "passed"},
        {"id": "get_text", "status": "passed"},
        {"id": "screenshot", "status": "passed"},
    ]
    (state / "smoke_results.json").write_text(
        json.dumps({"status": "manual_required", "results": results}),
        encoding="utf-8",
    )
    trace = [
        {"event": "server_started", "socket_path": "/tmp/harness.sock"},
        *[
            event
            for action in ("ping", "ping", "active_tab", "get_text", "screenshot")
            for event in (
                {"event": "request", "action": action},
                {"event": "response", "action": action, "ok": True},
            )
        ],
        {"event": "server_stopped", "socket_path": "/tmp/harness.sock"},
    ]
    (logs / "stub_bridge_trace.jsonl").write_text(
        "\n".join(json.dumps(event) for event in trace) + "\n",
        encoding="utf-8",
    )


def test_probe_bundle_builds_one_coherent_dry_run_workflow(tmp_path: Path) -> None:
    _write_acceptance_inputs(tmp_path)

    bundle = write_live_bundle(tmp_path, repo_root=ROOT, rollback_commit="HEAD")

    assert bundle["ready_for_live_probe"] is True
    assert bundle["probe_status"] == "dry_run"
    assert bundle["probe_step_ids"] == [
        "healthcheck",
        "ping",
        "active_tab",
        "get_text",
        "screenshot",
    ]
    assert all(Path(path).is_file() for path in bundle["artifacts"].values())
    assert json.loads(
        (tmp_path / "state" / "live_bundle.json").read_text(encoding="utf-8")
    ) == bundle


def test_probe_bundle_rejects_failed_isolated_acceptance(tmp_path: Path) -> None:
    _write_acceptance_inputs(tmp_path, promotable=False)

    with pytest.raises(ValueError, match="not promotable"):
        write_live_bundle(tmp_path, repo_root=ROOT, rollback_commit="HEAD")


def test_probe_execution_requires_confirmation_and_stops_on_first_failure(
    tmp_path: Path,
) -> None:
    _write_acceptance_inputs(tmp_path)
    write_live_probe_plan(tmp_path, rollback_commit="HEAD")

    with pytest.raises(ValueError, match="confirm_live=True"):
        execute_live_probe_plan(tmp_path, dry_run=False)

    calls = 0

    def runner(argv, **_kwargs):
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(
            argv,
            0 if calls == 1 else 1,
            stdout="ok" if calls == 1 else "",
            stderr="" if calls == 1 else "injected failure",
        )

    report = execute_live_probe_plan(
        tmp_path,
        dry_run=False,
        confirm_live=True,
        runner=runner,
    )

    assert report["status"] == "failed"
    assert [item["status"] for item in report["results"]] == ["passed", "failed"]
    assert calls == 2
