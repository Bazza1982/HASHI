from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchestrator.superloop_store import SuperloopStore


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json_list(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, list):
        raise ValueError(f"Expected list JSON in {path}")
    return [item for item in payload if isinstance(item, dict)]


def _dump_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    tmp_path.replace(path)


class SuperloopIssuesService:
    def __init__(self, store: SuperloopStore):
        self.store = store

    def list_issues(self, loop_id: str) -> list[dict[str, Any]]:
        return _load_json_list(self._issues_path(loop_id))

    def open_issue(
        self,
        loop_id: str,
        *,
        title: str,
        severity: str,
        opened_by_agent: str,
        opened_by_instance: str,
        related_task_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        path = self._issues_path(loop_id)
        issues = _load_json_list(path)
        issue_id = f"sli-{len(issues) + 1:03d}"
        issue = {
            "issue_id": issue_id,
            "title": title,
            "status": "open",
            "severity": severity,
            "opened_by": {"agent": opened_by_agent, "instance": opened_by_instance},
            "opened_at": _utc_now(),
            "assigned_to": {"agent": opened_by_agent, "instance": opened_by_instance},
            "related_task_ids": list(related_task_ids or []),
            "resolution": None,
        }
        issues.append(issue)
        _dump_json(path, issues)
        self.store.append_loop_event(loop_id, event_type="issue.opened", data={"issue_id": issue_id, "title": title})
        return issue

    def resolve_issue(self, loop_id: str, issue_id: str, resolution: str) -> bool:
        path = self._issues_path(loop_id)
        issues = _load_json_list(path)
        updated = False
        for issue in issues:
            if issue.get("issue_id") != issue_id:
                continue
            issue["status"] = "resolved"
            issue["resolved_at"] = _utc_now()
            issue["resolution"] = resolution
            updated = True
            break
        if not updated:
            return False
        _dump_json(path, issues)
        self.store.append_loop_event(loop_id, event_type="issue.resolved", data={"issue_id": issue_id})
        return True

    def _issues_path(self, loop_id: str) -> Path:
        state = self.store.load_loop_state(loop_id)
        path_rel = state.get("issues_path")
        if isinstance(path_rel, str) and path_rel.strip():
            return (self.store.root_dir.parent / path_rel).resolve()
        return self.store.loop_dir(loop_id) / "issues.json"
