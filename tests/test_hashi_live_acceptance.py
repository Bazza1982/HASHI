from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
from pathlib import Path

import pytest

from orchestrator.runtime_contract import CORE_SOURCE_PATHS
from scripts import hashi_live_acceptance as live


def minimal_suite(*, evidence: dict[str, int] | None = None) -> dict:
    return {
        "schema_version": 1,
        "suite_id": "focused-live",
        "title": "Focused live suite",
        "description": "Test fixture",
        "policy": {
            "interactive": True,
            "automatic_lifecycle_actions": False,
            "core_source_must_remain_unchanged": True,
            "git_head_must_remain_unchanged": True,
            "working_tree_must_remain_unchanged": True,
            "required_status": "pass",
        },
        "evidence_types": ["screenshot", "comparison", "observation"],
        "items": [
            {
                "id": "one",
                "title": "One",
                "required": True,
                "depends_on": [],
                "operator_steps": ["Perform one live observation."],
                "pass_criteria": ["The observation passes."],
                "evidence_minimum": evidence or {"observation": 1},
            }
        ],
    }


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def initialize_repo(path: Path) -> None:
    path.mkdir()
    for relative in CORE_SOURCE_PATHS:
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# {relative}\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "live@test.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Live Test"],
        check=True,
    )
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-q", "-m", "fixture"], check=True
    )


def test_shipped_suite_is_valid_and_never_automates_lifecycle() -> None:
    suite = live.load_suite(live.DEFAULT_SUITE)

    assert suite["suite_id"] == "hashi-frontend-essential"
    assert suite["policy"]["automatic_lifecycle_actions"] is False
    assert {"reboot_min", "reboot_max", "restart_with_draft"} <= {
        item["id"] for item in suite["items"]
    }


def test_suite_validation_rejects_lifecycle_automation_and_cycles() -> None:
    suite = minimal_suite()
    suite["policy"]["automatic_lifecycle_actions"] = True
    suite["items"][0]["depends_on"] = ["one"]

    with pytest.raises(live.AcceptanceError) as error:
        live.validate_suite(suite)

    assert "automatic_lifecycle_actions must be false" in str(error.value)
    assert "cannot depend on itself" in str(error.value)


def test_pid_liveness_uses_shared_non_signalling_probe(monkeypatch) -> None:
    observed: list[int] = []
    monkeypatch.setattr(
        live,
        "process_is_alive_without_signal",
        lambda pid: observed.append(pid) or True,
        raising=False,
    )
    monkeypatch.setattr(
        live.os,
        "kill",
        lambda *_args: pytest.fail("PID liveness checks must not signal processes"),
    )

    assert live.pid_is_alive(None) is None
    assert live.pid_is_alive(4242) is True
    assert observed == [4242]


def test_snapshot_comparison_enforces_core_pid_source_and_tree() -> None:
    before = {
        "label": "before",
        "source": {
            "core_source_digest": "sha256:core",
            "protected_files": {"main.py": {"sha256": "sha256:file"}},
            "git_head": "abc",
            "git_status": [],
        },
        "runtime": {"core_pid": 100},
    }
    after = copy.deepcopy(before)
    after["label"] = "after"
    after["runtime"]["core_pid"] = 101

    restart = live.compare_snapshots(
        before,
        after,
        core_pid="changed",
        core_digest="same",
        git_head="same",
        git_status="same",
    )
    reboot = live.compare_snapshots(
        before,
        after,
        core_pid="same",
        core_digest="same",
        git_head="same",
        git_status="same",
    )

    assert restart["passed"] is True
    assert reboot["passed"] is False
    assert reboot["checks"]["core_pid"]["passed"] is False


def test_record_requires_evidence_and_copies_it_into_run(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    suite = live.validate_suite(
        minimal_suite(evidence={"screenshot": 1, "observation": 1})
    )
    write_json(run_dir / "suite.json", suite)
    write_json(
        run_dir / "run.json",
        {
            "schema_version": 1,
            "run_id": "run-one",
            "suite_id": suite["suite_id"],
            "status": "running",
            "context": {},
            "items": {
                "one": {
                    "status": "not_run",
                    "evidence": [],
                    "notes": [],
                    "updated_at": None,
                }
            },
        },
    )
    screenshot = tmp_path / "screen.png"
    screenshot.write_bytes(b"not-a-real-png")
    args = argparse.Namespace(
        run=str(run_dir),
        item="one",
        status="pass",
        file=[f"screenshot={screenshot}"],
        observation=["Observed in the real frontend."],
        note=None,
    )

    live.record_item(args)

    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert run["items"]["one"]["status"] == "pass"
    evidence = run["items"]["one"]["evidence"]
    assert {entry["type"] for entry in evidence} == {"screenshot", "observation"}
    copied = run_dir / next(
        entry["path"] for entry in evidence if entry["type"] == "screenshot"
    )
    assert copied.read_bytes() == b"not-a-real-png"


def test_record_refuses_pass_when_attached_comparison_failed(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    suite = live.validate_suite(minimal_suite(evidence={"comparison": 1}))
    write_json(run_dir / "suite.json", suite)
    write_json(
        run_dir / "run.json",
        {
            "schema_version": 1,
            "run_id": "run-failed-comparison",
            "suite_id": suite["suite_id"],
            "status": "running",
            "context": {},
            "items": {
                "one": {
                    "status": "not_run",
                    "evidence": [],
                    "notes": [],
                    "updated_at": None,
                }
            },
        },
    )
    comparison = tmp_path / "comparison.json"
    write_json(comparison, {"passed": False})
    args = argparse.Namespace(
        run=str(run_dir),
        item="one",
        status="pass",
        file=[f"comparison={comparison}"],
        observation=[],
        note=None,
    )

    with pytest.raises(live.AcceptanceError, match="comparison did not pass"):
        live.record_item(args)

    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert run["items"]["one"]["status"] == "not_run"
    assert run["items"]["one"]["evidence"] == []


def test_start_and_finish_preserve_source_invariants(tmp_path: Path) -> None:
    code_root = tmp_path / "repo"
    initialize_repo(code_root)
    runtime_dir = code_root / "state" / "instance"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "process.pid").write_text(str(os.getpid()), encoding="utf-8")
    suite = live.validate_suite(minimal_suite())
    suite_path = tmp_path / "suite.json"
    write_json(suite_path, suite)
    args = argparse.Namespace(
        code_root=str(code_root),
        bridge_home=str(code_root),
        output_root=str(tmp_path / "runs"),
        instance_id="HASHI2",
        agent="test-agent",
        client_label="Workbench",
        client_url="http://127.0.0.1/",
    )

    run_dir = live.start_run(args, suite)
    run, _ = live.load_run(run_dir)
    live.attach_observation(run, "one", "Interactive observation passed.")
    run["items"]["one"]["status"] = "pass"
    live.save_run(run_dir, run)

    assert live.finish_run(run_dir) is True
    finished, _ = live.load_run(run_dir)
    assert finished["status"] == "passed"
    assert (run_dir / "report.md").is_file()
    invariant = json.loads(
        (run_dir / "comparisons" / "baseline--final.json").read_text(
            encoding="utf-8"
        )
    )
    assert invariant["passed"] is True
