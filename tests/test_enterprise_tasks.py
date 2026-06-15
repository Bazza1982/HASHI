from __future__ import annotations

import pytest

from orchestrator.enterprise import (
    ArtifactRegistry,
    EnterpriseAuditLedger,
    EvidenceBundleRegistry,
    ExecutionScope,
    IdentityService,
    TaskRegistry,
    TaskStatus,
    complete_task_with_artifact_verification,
    fail_task_if_promised_artifacts_missing,
    verify_promised_artifacts,
)


def _init_services(tmp_path):
    db_path = tmp_path / "enterprise.sqlite"
    identity = IdentityService.from_path(db_path)
    identity.create_organization(org_id="ORG-001", name="Acme")
    user = identity.create_user(
        org_id="ORG-001",
        email="user@example.com",
        display_name="User",
        password="secret-password",
        user_id="usr-1",
    )
    project = identity.create_project(
        org_id="ORG-001",
        name="Research",
        workspace_root=str(tmp_path / "workspaces" / "research"),
        project_id="prj-research",
    )
    return {
        "db_path": db_path,
        "user": user,
        "project": project,
        "tasks": TaskRegistry.from_path(db_path),
        "artifacts": ArtifactRegistry.from_path(db_path),
        "evidence": EvidenceBundleRegistry.from_path(db_path),
        "ledger": EnterpriseAuditLedger.from_path(db_path, org_id="ORG-001"),
    }


def test_task_registry_creates_lists_and_transitions_tasks(tmp_path):
    svc = _init_services(tmp_path)

    task = svc["tasks"].create_task(
        org_id="ORG-001",
        project_id=svc["project"].id,
        user_id=svc["user"].id,
        agent_id="nana",
        prompt_summary="Prepare monthly close report",
        task_id="task-close",
        metadata={"priority": "high"},
    )

    assert task.status == TaskStatus.DELEGATED.value
    assert task.metadata == {"priority": "high"}
    assert svc["tasks"].list_tasks(org_id="ORG-001", project_id="prj-research") == [task]

    in_progress = svc["tasks"].transition_task("task-close", TaskStatus.IN_PROGRESS)
    completed = svc["tasks"].transition_task("task-close", TaskStatus.COMPLETED)

    assert in_progress.status == "in_progress"
    assert completed.status == "completed"
    assert completed.completed_at is not None
    assert completed.failed_reason is None


def test_task_registry_rejects_unknown_status_and_missing_task(tmp_path):
    svc = _init_services(tmp_path)

    with pytest.raises(ValueError, match="unsupported task status"):
        svc["tasks"].transition_task("task-missing", "paused")

    with pytest.raises(ValueError, match="unknown task"):
        svc["tasks"].transition_task("task-missing", TaskStatus.FAILED, failed_reason="not found")


def test_artifact_registry_hashes_and_lists_task_artifacts(tmp_path):
    svc = _init_services(tmp_path)
    task = svc["tasks"].create_task(
        org_id="ORG-001",
        project_id="prj-research",
        prompt_summary="Write report",
        task_id="task-report",
    )
    report = tmp_path / "report.md"
    report.write_text("hello", encoding="utf-8")

    artifact = svc["artifacts"].register_artifact(
        org_id="ORG-001",
        project_id="prj-research",
        task_id=task.id,
        artifact_type="report",
        path=report,
        artifact_id="art-report",
        metadata={"format": "markdown"},
    )

    assert artifact.hash.startswith("sha256:")
    assert artifact.metadata == {"format": "markdown"}
    assert svc["artifacts"].list_artifacts(org_id="ORG-001", task_id=task.id) == [artifact]


def test_evidence_bundle_builds_from_task_audit_and_artifacts(tmp_path):
    svc = _init_services(tmp_path)
    task = svc["tasks"].create_task(
        org_id="ORG-001",
        project_id="prj-research",
        user_id="usr-1",
        agent_id="zelda",
        prompt_summary="Produce evidence bundle",
        task_id="task-evidence",
    )
    artifact_path = tmp_path / "evidence.pdf"
    artifact_path.write_text("pdf-ish", encoding="utf-8")
    artifact = svc["artifacts"].register_artifact(
        org_id="ORG-001",
        project_id="prj-research",
        task_id=task.id,
        artifact_type="file",
        path=artifact_path,
        artifact_id="art-evidence",
    )
    event = svc["ledger"].append(
        event_type="tool",
        action="tool.file_write",
        status="success",
        actor_id="zelda",
        task_id=task.id,
        context={"path": str(artifact_path)},
    )

    bundle = svc["evidence"].build_for_task(
        ledger=svc["ledger"],
        artifacts=svc["artifacts"],
        org_id="ORG-001",
        task_id=task.id,
    )

    assert bundle.task_id == "task-evidence"
    assert bundle.audit_event_ids == (event.id,)
    assert bundle.artifact_ids == (artifact.id,)
    assert bundle.metadata == {"source": "build_for_task"}


def test_verify_promised_artifacts_accepts_registered_output(tmp_path):
    svc = _init_services(tmp_path)
    task = svc["tasks"].create_task(
        org_id="ORG-001",
        project_id="prj-research",
        prompt_summary="Write governed report",
        task_id="task-verify-ok",
        metadata={"promised_artifacts": ["final-report.md"]},
    )
    artifact = svc["artifacts"].register_artifact(
        org_id="ORG-001",
        project_id="prj-research",
        task_id=task.id,
        artifact_type="file",
        path=tmp_path / "final-report.md",
        artifact_id="art-final-report",
    )

    result = verify_promised_artifacts(task, [artifact])

    assert result.ok is True
    assert result.matched == ("final-report.md",)
    assert result.missing == ()


def test_fail_task_if_promised_artifacts_missing_records_clear_failure(tmp_path):
    svc = _init_services(tmp_path)
    task = svc["tasks"].create_task(
        org_id="ORG-001",
        project_id="prj-research",
        prompt_summary="Write governed report",
        task_id="task-verify-missing",
        metadata={"promised_artifacts": ["final-report.md", {"path": "appendix.csv"}]},
    )
    artifact = svc["artifacts"].register_artifact(
        org_id="ORG-001",
        project_id="prj-research",
        task_id=task.id,
        artifact_type="file",
        path=tmp_path / "final-report.md",
        artifact_id="art-final-report",
    )

    result = fail_task_if_promised_artifacts_missing(svc["tasks"], task, [artifact])

    assert result.ok is False
    assert result.missing == ("appendix.csv",)
    failed = svc["tasks"].get_task(task.id)
    assert failed is not None
    assert failed.status == "failed"
    assert failed.failed_reason == "missing promised artifacts: appendix.csv"


def test_complete_task_with_artifact_verification_completes_when_outputs_exist(tmp_path):
    svc = _init_services(tmp_path)
    task = svc["tasks"].create_task(
        org_id="ORG-001",
        project_id="prj-research",
        prompt_summary="Write governed report",
        task_id="task-complete-ok",
        metadata={"promised_artifacts": ["final-report.md"]},
    )
    svc["artifacts"].register_artifact(
        org_id="ORG-001",
        project_id="prj-research",
        task_id=task.id,
        artifact_type="file",
        path=tmp_path / "final-report.md",
        artifact_id="art-final-report",
    )

    result = complete_task_with_artifact_verification(svc["tasks"], svc["artifacts"], task)

    assert result.ok is True
    completed = svc["tasks"].get_task(task.id)
    assert completed is not None
    assert completed.status == "completed"
    assert completed.failed_reason is None


def test_complete_task_with_artifact_verification_fails_when_outputs_missing(tmp_path):
    svc = _init_services(tmp_path)
    task = svc["tasks"].create_task(
        org_id="ORG-001",
        project_id="prj-research",
        prompt_summary="Write governed report",
        task_id="task-complete-missing",
        metadata={"promised_artifacts": ["final-report.md"]},
    )

    result = complete_task_with_artifact_verification(svc["tasks"], svc["artifacts"], task)

    assert result.ok is False
    failed = svc["tasks"].get_task(task.id)
    assert failed is not None
    assert failed.status == "failed"
    assert failed.failed_reason == "missing promised artifacts: final-report.md"


def test_execution_scope_allows_paths_inside_project_workspace(tmp_path):
    svc = _init_services(tmp_path)
    project = svc["project"]

    scope = ExecutionScope.from_project(project)
    resolved = scope.require_path("reports/final.md")

    assert resolved == (tmp_path / "workspaces" / "research" / "reports" / "final.md").resolve()
    assert scope.check_path(resolved).allowed is True


def test_execution_scope_blocks_workspace_escape(tmp_path):
    svc = _init_services(tmp_path)
    scope = ExecutionScope.from_project(svc["project"])

    decision = scope.check_path("../outside.txt")

    assert decision.allowed is False
    assert decision.reason == "workspace_escape"
    with pytest.raises(PermissionError, match="workspace_escape"):
        scope.require_path(tmp_path / "outside.txt")


def test_execution_scope_blocks_symlink_escape(tmp_path):
    svc = _init_services(tmp_path)
    workspace = tmp_path / "workspaces" / "research"
    workspace.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "escape").symlink_to(outside, target_is_directory=True)
    scope = ExecutionScope.from_project(svc["project"])

    decision = scope.check_path("escape/leak.txt")

    assert decision.allowed is False
    assert decision.reason == "workspace_escape"
