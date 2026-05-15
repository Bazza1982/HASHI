from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchestrator.superloop_store import SuperloopStore


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SuperloopIssuesService:
    def __init__(self, store: SuperloopStore):
        self.store = store

    def list_issues(self, loop_id: str) -> list[dict[str, Any]]:
        return self.store.load_loop_json_list(self._issues_path(loop_id))

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
        with self.store._lock:
            issues = self.store.load_loop_json_list(path)
            issue_id = self.store.generate_record_id("sli")
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
            self.store.save_loop_json_list(path, issues)
            self.store.refresh_loop_stats(loop_id)
        self.store.append_loop_event(loop_id, event_type="issue.opened", data={"issue_id": issue_id, "title": title})
        return issue

    def resolve_issue(self, loop_id: str, issue_id: str, resolution: str) -> bool:
        path = self._issues_path(loop_id)
        with self.store._lock:
            issues = self.store.load_loop_json_list(path)
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
            self.store.save_loop_json_list(path, issues)
            self.store.refresh_loop_stats(loop_id)
        self.store.append_loop_event(loop_id, event_type="issue.resolved", data={"issue_id": issue_id})
        return True

    def _issues_path(self, loop_id: str) -> Path:
        state = self.store.load_loop_state(loop_id)
        path_rel = state.get("issues_path")
        return self.store.resolve_loop_path(loop_id, path_rel, "issues.json")
