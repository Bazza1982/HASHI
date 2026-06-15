from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from orchestrator.enterprise.identity import Project


@dataclass(frozen=True)
class ExecutionPathDecision:
    allowed: bool
    root: Path
    requested: str
    resolved: Path | None
    reason: str | None = None


@dataclass(frozen=True)
class ExecutionScope:
    org_id: str
    project_id: str
    workspace_root: Path

    @classmethod
    def from_project(
        cls,
        project: Project,
        *,
        default_workspace_root: Path | str | None = None,
    ) -> "ExecutionScope":
        root = project.workspace_root or default_workspace_root
        if root is None:
            raise ValueError("project workspace_root is required for governed execution")
        workspace_root = Path(root).expanduser().resolve()
        return cls(org_id=project.org_id, project_id=project.id, workspace_root=workspace_root)

    def check_path(self, path: Path | str) -> ExecutionPathDecision:
        requested = str(path)
        if not requested.strip():
            return ExecutionPathDecision(
                allowed=False,
                root=self.workspace_root,
                requested=requested,
                resolved=None,
                reason="path_required",
            )
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace_root / candidate
        resolved = candidate.resolve(strict=False)
        if _is_relative_to(resolved, self.workspace_root):
            return ExecutionPathDecision(
                allowed=True,
                root=self.workspace_root,
                requested=requested,
                resolved=resolved,
            )
        return ExecutionPathDecision(
            allowed=False,
            root=self.workspace_root,
            requested=requested,
            resolved=resolved,
            reason="workspace_escape",
        )

    def require_path(self, path: Path | str) -> Path:
        decision = self.check_path(path)
        if not decision.allowed or decision.resolved is None:
            raise PermissionError(
                f"enterprise execution path denied: {decision.reason}; "
                f"requested={decision.requested!r}; root={decision.root}"
            )
        return decision.resolved


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
