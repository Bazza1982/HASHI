"""Derive immutable service boundaries from the existing live-runtime owner."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys

from orchestrator.live_runtime_protection import evaluate_live_runtime_request, load_live_runtime_policy
from orchestrator.permissions.authority import PermissionDenied


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
