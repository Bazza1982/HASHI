from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from orchestrator.superloop_store import SuperloopStore, system_actor


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def delivery_contract_gaps(task: dict[str, Any]) -> list[str]:
    """Validate the user scenario before treating any delivery evidence as coverage."""
    if task.get("delivery_required") is not True:
        return []
    checks = task.get("acceptance_checks")
    if not _text(task.get("user_outcome")) or not isinstance(checks, list) or not checks:
        return ["acceptance_contract"]
    ids, kinds = set(), set()
    for check in checks:
        if not isinstance(check, dict) or not all(_text(check.get(k)) for k in (
                "id", "kind", "scope", "subject_version", "scenario", "expected")):
            return ["acceptance_contract"]
        prerequisites = check.get("prerequisites", [])
        if (check["id"] in ids or not isinstance(prerequisites, list)
                or any(not _text(p) for p in prerequisites)
                or len(prerequisites) != len(set(prerequisites))):
            return ["acceptance_contract"]
        ids.add(check["id"])
        kinds.add(check["kind"])
    required = {"runtime_adoption", "user_acceptance"}
    if task.get("terminal_delivery_required") is True:
        required.add("terminal_delivery")
    return [kind for kind in sorted(required - kinds)]


def _local_evidence(store: SuperloopStore, loop_id: str, ref: Any) -> Path | None:
    if not _text(ref):
        return None
    try:
        root = store.loop_dir(loop_id).resolve()
        path = (root / ref).resolve()
        return path if path.is_relative_to(root) and path.is_file() else None
    except (OSError, ValueError):
        return None


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def acceptance_gap(store: SuperloopStore, loop_id: str, task: dict, check: dict) -> str | None:
    """Check scoped, independently reviewed observation provenance, not semantic truth.

    The complete check is bound into its observation so changing a target,
    expected behavior or prerequisite invalidates the old review. Legacy
    verified flags are historical notes, never alternate acceptance paths.
    """
    results = task.get("acceptance_results", {})
    record = results.get(check["id"]) if isinstance(results, dict) else None
    if not isinstance(record, dict) or not _text(record.get("reviewed_by")):
        return "unreviewed"
    path = _local_evidence(store, loop_id, record.get("evidence_ref"))
    try:
        if path is None or path.stat().st_size > 1024 * 1024 or _digest(path) != record.get("sha256"):
            return "evidence_changed_or_missing"
        proof = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(proof, dict) or proof.get("task_id") != task.get("task_id") or proof.get("check") != check:
            return "scope_mismatch"
        if proof.get("subject_version") != check["subject_version"]:
            return "version_mismatch"
        if not all(_text(proof.get(k)) for k in ("observer", "subject_version", "observed", "observed_at")):
            return "observation_incomplete"
        observed_at = datetime.fromisoformat(proof["observed_at"])
        if observed_at.utcoffset() is None:
            return "observation_incomplete"
        if proof["observer"].strip().casefold() == record["reviewed_by"].strip().casefold():
            return "independent_review_missing"
        artifacts = proof.get("artifacts")
        if not isinstance(artifacts, list) or not artifacts:
            return "source_evidence_missing"
        for artifact in artifacts:
            source = _local_evidence(store, loop_id, artifact.get("ref")) if isinstance(artifact, dict) else None
            if source is None or _digest(source) != artifact.get("sha256"):
                return "source_evidence_changed_or_missing"
        if proof.get("result") != "passed":
            return "failed" if proof.get("result") == "failed" else "unverified"
    except (OSError, ValueError, TypeError):
        return "invalid_evidence"
    return None


def task_delivery_gaps(store: SuperloopStore, loop_id: str, task: dict) -> list[str]:
    if task.get("delivery_required") is not True:
        return []
    gaps = delivery_contract_gaps(task)
    if gaps:
        return gaps
    return [f"{check['id']}:{gap}" for check in task["acceptance_checks"]
            if (gap := acceptance_gap(store, loop_id, task, check))]


class SuperloopTaskboardService:
    def __init__(self, store: SuperloopStore):
        self.store = store

    def list_tasks(self, loop_id: str) -> list[dict[str, Any]]:
        return self.store.load_loop_json_list(self._taskboard_path(loop_id))

    def outcome_report(self, loop_id: str) -> list[dict[str, Any]]:
        """Derive the manager's outcome facts from the same checks used at closeout.

        This is a report projection, never a delivery receipt or another writer
        of task truth. Callers still inspect evidence and explain the next action.
        """
        report = []
        for task in self.list_tasks(loop_id):
            if task.get("delivery_required") is not True:
                continue
            gaps = delivery_contract_gaps(task)
            checks = []
            if not gaps:
                for check in task["acceptance_checks"]:
                    gap = acceptance_gap(self.store, loop_id, task, check)
                    checks.append(dict(check_id=check["id"], kind=check["kind"], scope=check["scope"],
                                       scenario=check["scenario"], expected=check["expected"],
                                       accepted=gap is None, gap=gap))
            report.append(dict(task_id=task["task_id"], outcome=task.get("user_outcome", task.get("title")),
                               owner_agent=task.get("owner_agent"), owner_instance=task.get("owner_instance"),
                               status=task.get("status"), accepted=not gaps and all(c["accepted"] for c in checks),
                               contract_gaps=gaps, checks=checks, next_disposition=task.get("next_disposition")))
        return report

    def add_task(
        self,
        loop_id: str,
        *,
        title: str,
        owner_agent: str,
        owner_instance: str,
        depends_on: list[str] | None = None,
        task_id: str | None = None,
        actor: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        path = self._taskboard_path(loop_id)
        with self.store._lock:
            tasks = self.store.load_loop_json_list(path)
            assigned_task_id = task_id or self.store.generate_record_id("task")
            now = _utc_now()
            task = {
                "task_id": assigned_task_id,
                "title": title,
                "description": title,
                "status": "pending",
                "owner_agent": owner_agent,
                "owner_instance": owner_instance,
                "depends_on": list(depends_on or []),
                "priority": "normal",
                "created_at": now,
                "updated_at": now,
                "artifact_refs": [],
                "notes": [],
            }
            tasks.append(task)
            self.store.save_loop_json_list(path, tasks)
            self.store.refresh_loop_stats(loop_id)
        self.store.append_loop_event(
            loop_id,
            event_type="task.added",
            data={"task_id": assigned_task_id},
            actor=actor or system_actor("superloop_taskboard"),
        )
        return task

    def update_task_status(
        self,
        loop_id: str,
        task_id: str,
        status: str,
        *,
        actor: dict[str, Any] | None = None,
    ) -> bool:
        path = self._taskboard_path(loop_id)
        with self.store._lock:
            tasks = self.store.load_loop_json_list(path)
            updated = False
            for task in tasks:
                if task.get("task_id") != task_id:
                    continue
                if status == "completed":
                    gaps = task_delivery_gaps(self.store, loop_id, task)
                    if gaps:
                        raise ValueError(f"Task {task_id} delivery incomplete: {', '.join(gaps)}")
                task["status"] = status
                task["updated_at"] = _utc_now()
                updated = True
                break
            if not updated:
                return False
            self.store.save_loop_json_list(path, tasks)
            self.store.refresh_loop_stats(loop_id)
        self.store.append_loop_event(
            loop_id,
            event_type="task.status_updated",
            data={"task_id": task_id, "status": status},
            actor=actor or system_actor("superloop_taskboard"),
        )
        return True

    def _taskboard_path(self, loop_id: str) -> Path:
        state = self.store.load_loop_state(loop_id)
        path_rel = state.get("taskboard_path")
        return self.store.resolve_loop_path(loop_id, path_rel, "taskboard.json")
