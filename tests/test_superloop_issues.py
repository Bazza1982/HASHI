from __future__ import annotations

from pathlib import Path

from orchestrator.superloop_issues import SuperloopIssuesService
from orchestrator.superloop_store import SuperloopStore


def test_issues_open_and_resolve(tmp_path: Path) -> None:
    store = SuperloopStore(tmp_path / "superloops")
    store.create_compiled_loop(
        loop_id="sl-test-001",
        loop_state={"loop_id": "sl-test-001", "status": "running", "issues_path": "superloops/loops/sl-test-001/issues.json"},
        taskboard=[],
        issues=[],
        waits=[],
        operator_summary="# summary\n",
    )
    svc = SuperloopIssuesService(store)
    issue = svc.open_issue(
        "sl-test-001",
        title="Needs review",
        severity="medium",
        opened_by_agent="zelda",
        opened_by_instance="HASHI1",
        related_task_ids=["task-001"],
    )
    assert issue["issue_id"].startswith("sli-")
    assert svc.resolve_issue("sl-test-001", issue["issue_id"], "done") is True
    issues = svc.list_issues("sl-test-001")
    assert issues[0]["status"] == "resolved"


def test_issues_path_must_stay_under_superloops_root(tmp_path: Path) -> None:
    store = SuperloopStore(tmp_path / "superloops")
    store.create_compiled_loop(
        loop_id="sl-test-escape",
        loop_state={"loop_id": "sl-test-escape", "status": "running", "issues_path": "../escape.json"},
        taskboard=[],
        issues=[],
        waits=[],
        operator_summary="# summary\n",
    )
    svc = SuperloopIssuesService(store)
    try:
        svc.list_issues("sl-test-escape")
        assert False, "expected ValueError for escaping issues path"
    except ValueError:
        pass
