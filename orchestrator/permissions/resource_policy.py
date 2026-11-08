"""Derive immutable service boundaries from the existing live-runtime owner."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys

from orchestrator.live_runtime_protection import evaluate_live_runtime_request, load_live_runtime_policy
from orchestrator.permissions.authority import PermissionDenied
from orchestrator.permissions.models import canonical_scope_set, normalize_scope


# Bump this contract when the L2 ceiling changes; old epochs must not inherit it.
WORKZONE_BOUNDARY_VERSION = 1


def constrain_to_workzones(scopes, roots):
    """Intersect installed grants with exact Workzones; never grant new actions.

    This is the service-owned policy ceiling, not an OS sandbox. Native object
    rebinding and the qualified host/Runner still enforce actual execution.
    """
    if not roots or any(not Path(root).is_absolute() for root in roots):
        raise PermissionDenied("an explicit L2 Workzone ceiling is required")
    canonical_roots = tuple(Path(normalize_scope(
        "file", str(root), ("read",), cwd=Path(root),
    ).resource) for root in roots)
    result = []
    for scope in scopes:
        if scope.kind == "file":
            path = Path(scope.resource)
            for root in canonical_roots:
                if path.is_relative_to(root):
                    result.append(scope)
                elif root.is_relative_to(path):
                    result.append(normalize_scope("file", str(root), scope.actions, cwd=root))
        elif scope.kind != "capability" or scope.resource != "process.unrestricted":
            result.append(scope)
    return canonical_scope_set(result)


class ProtectedResourcePolicy:
    def __init__(self, global_config, *, private_paths, immutable_paths, runtime_prefix=None):
        policy = load_live_runtime_policy(global_config, runtime_prefix=runtime_prefix or sys.prefix)
        if policy is None:
            raise PermissionDenied("the canonical live-runtime protection policy is unavailable")
        private = tuple(Path(value).resolve() for value in private_paths)
        immutable = tuple(Path(value).resolve() for value in immutable_paths)
        self.policy = replace(
            policy,
            protected_read_paths=tuple(dict.fromkeys((*policy.protected_read_paths, *private))),
            protected_write_paths=tuple(dict.fromkeys((
                *policy.protected_write_paths, *private, *immutable,
            ))),
        )

    def permits(self, scope):
        if scope.kind != "file":
            return True
        # Every concrete resource is checked again at prepare/start/running.
        # A broader directory grant does not bypass a protected descendant.
        for tool in (("file_read",) if set(scope.actions) <= {"read", "list"}
                     else ("file_read", "file_write")):
            if evaluate_live_runtime_request(
                self.policy, tool_name=tool, arguments={"path": scope.resource},
                workspace_dir=Path(scope.resource).parent,
            ) is not None:
                return False
        return True
# HASHI-Fingerprint: hfp_b0006ef986134101a1db3a1dc0a486b4
