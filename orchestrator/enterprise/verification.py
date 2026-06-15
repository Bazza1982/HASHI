from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from orchestrator.enterprise.artifacts import Artifact, ArtifactRegistry
from orchestrator.enterprise.tasks import EnterpriseTask, TaskRegistry, TaskStatus


@dataclass(frozen=True)
class ArtifactVerificationResult:
    task_id: str
    ok: bool
    required: tuple[str, ...]
    matched: tuple[str, ...]
    missing: tuple[str, ...]
    reason: str | None = None


def verify_promised_artifacts(
    task: EnterpriseTask,
    artifacts: Iterable[Artifact],
    *,
    promised_artifacts: Iterable[object] | None = None,
) -> ArtifactVerificationResult:
    required = _normalize_promised_artifacts(promised_artifacts, task=task)
    artifact_list = list(artifacts)
    matched: list[str] = []
    missing: list[str] = []
    for requirement in required:
        if _matches_any_artifact(requirement, artifact_list):
            matched.append(requirement)
        else:
            missing.append(requirement)
    ok = not missing
    return ArtifactVerificationResult(
        task_id=task.id,
        ok=ok,
        required=tuple(required),
        matched=tuple(matched),
        missing=tuple(missing),
        reason=None if ok else f"missing promised artifacts: {', '.join(missing)}",
    )


def fail_task_if_promised_artifacts_missing(
    registry: TaskRegistry,
    task: EnterpriseTask,
    artifacts: Iterable[Artifact],
    *,
    promised_artifacts: Iterable[object] | None = None,
) -> ArtifactVerificationResult:
    result = verify_promised_artifacts(task, artifacts, promised_artifacts=promised_artifacts)
    if not result.ok:
        registry.transition_task(task.id, TaskStatus.FAILED, failed_reason=result.reason)
    return result


def complete_task_with_artifact_verification(
    tasks: TaskRegistry,
    artifacts: ArtifactRegistry,
    task: EnterpriseTask,
    *,
    promised_artifacts: Iterable[object] | None = None,
) -> ArtifactVerificationResult:
    task_artifacts = artifacts.list_artifacts(org_id=task.org_id, task_id=task.id)
    result = verify_promised_artifacts(
        task,
        task_artifacts,
        promised_artifacts=promised_artifacts,
    )
    if result.ok:
        tasks.transition_task(task.id, TaskStatus.COMPLETED)
    else:
        tasks.transition_task(task.id, TaskStatus.FAILED, failed_reason=result.reason)
    return result


def _normalize_promised_artifacts(
    promised_artifacts: Iterable[object] | None,
    *,
    task: EnterpriseTask,
) -> list[str]:
    raw_items = promised_artifacts
    if raw_items is None:
        raw_items = (
            task.metadata.get("promised_artifacts")
            or task.metadata.get("required_artifacts")
            or task.metadata.get("expected_artifacts")
            or []
        )
    normalized: list[str] = []
    for item in raw_items or []:
        value = _requirement_to_text(item)
        if value and value not in normalized:
            normalized.append(value)
    return normalized


def _requirement_to_text(item: object) -> str:
    if isinstance(item, dict):
        for key in ("path", "artifact_path", "id", "artifact_id", "name", "type"):
            value = str(item.get(key) or "").strip()
            if value:
                return value
        return ""
    return str(item or "").strip()


def _matches_any_artifact(requirement: str, artifacts: list[Artifact]) -> bool:
    wanted = _normalize_match_text(requirement)
    if not wanted:
        return True
    for artifact in artifacts:
        candidates = {
            _normalize_match_text(artifact.id),
            _normalize_match_text(artifact.type),
            _normalize_match_text(artifact.path),
            _normalize_match_text(Path(artifact.path).name),
        }
        if wanted in candidates:
            return True
    return False


def _normalize_match_text(value: object) -> str:
    return str(value or "").strip().casefold()
