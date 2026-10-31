"""Explicit managed registry; it is never selected by legacy L0.

The installed execution channel supplies the authoritative gate and supervisor,
which independently enforce resource binding, start, revoke and cleanup.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time
from typing import Any

from orchestrator.permissions.execution_gate import GateResult, PermissionExecutionGate
from orchestrator.permissions.models import ExecutionContext, Scope
from orchestrator.permissions.waiting import PermissionWaiter
from tools.registry import ToolRegistry, ToolResult, resolve_tiers
from tools.request_access import PermissionAgentTools, PermissionToolError, permission_tool_definitions


_PERMISSION_NAMES = frozenset({"request_access", "get_access_status", "release_access"})
_ORDINARY_QUESTION_NAMES = frozenset({"ask_user", "get_user_answer"})


class ManagedToolRegistry(ToolRegistry):
    """A testable integration seam, not a claim of L1/L2 completeness."""

    def __init__(
        self,
        *,
        allowed_tools: list[str],
        access_root: Path,
        workspace_dir: Path,
        secrets: dict,
        permission_authority: Any,
        permission_context: ExecutionContext,
        permission_supervisor: Any,
        requestable_scopes: tuple[Scope, ...],
        baseline_scopes: tuple[Scope, ...] = (),
        durable_wait: bool = False,
        ordinary_questions: Any = None,
        credential_names: tuple[str, ...] = (),
        script_enabled: bool = False,
        **legacy_options: Any,
    ) -> None:
        if secrets:
            raise ValueError("managed tool registry cannot hold shared credentials")
        if type(durable_wait) is not bool:
            raise ValueError("durable wait must be explicit")
        self.permission_durable_wait = durable_wait
        self._ordinary_questions = ordinary_questions
        self._credential_names = tuple(credential_names)
        self._script_enabled = False
        super().__init__(
            allowed_tools=allowed_tools, access_root=access_root,
            workspace_dir=workspace_dir, secrets={}, **legacy_options,
        )
        self._script_enabled = bool(script_enabled) and any(ToolRegistry.is_allowed(self, name) for name in (
            "run_script", "shell", "bash", "verification_run",
        ))
        self._permission_context = permission_context
        self._permission_cwd = Path(workspace_dir).resolve(strict=False)
        self._permission_roots = tuple(Path(root).resolve(strict=False) for root in self.read_access_roots)
        self._permission_waiter = PermissionWaiter(permission_authority)
        self._permission_tools = PermissionAgentTools(
            permission_authority, permission_context, cwd=self._permission_cwd,
            requestable_scopes=requestable_scopes, durable_wait=durable_wait,
        )
        self._permission_gate = PermissionExecutionGate(
            permission_authority, self._permission_waiter, permission_supervisor,
            context=permission_context, cwd=self._permission_cwd,
            read_roots=self._permission_roots, requestable_scopes=requestable_scopes,
            baseline_scopes=baseline_scopes, durable_control=durable_wait,
        )

    def is_allowed(self, tool_name: str) -> bool:
        if tool_name == "run_script":
            return self._script_enabled
        return tool_name in _PERMISSION_NAMES or super().is_allowed(tool_name)

    def allowed_tool_names(self) -> tuple[str, ...]:
        names = set(super().allowed_tool_names()) | _PERMISSION_NAMES
        names.discard("run_script")
        if self._script_enabled:
            names.add("run_script")
        return tuple(sorted(names))

    def get_tool_definitions(self, tiers: list[str] | None = None) -> list[dict]:
        definitions = super().get_tool_definitions(tiers)
        if self._script_enabled:
            from orchestrator.permissions.script_protocol import definition as script_definition

            definitions = [item for item in definitions if item.get("function", {}).get("name") not in {
                "shell", "bash", "verification_run", "run_script",
            }]
            definitions.append(script_definition())
        if self._credential_names:
            definitions = json.loads(json.dumps(definitions))
            for definition in definitions:
                if definition.get("function", {}).get("name") == "http_request":
                    definition["function"]["parameters"].setdefault("properties", {})["credential"] = {
                        "type": "string", "enum": list(self._credential_names),
                        "description": "Use an installed credential for its approved target operation. No key is returned.",
                    }
        if tiers is not None and "ask_user" not in resolve_tiers(tiers):
            return definitions
        request_allowlist = self._effective_audit_context().get("request_tool_allowlist")
        allowed = (
            {str(name) for name in request_allowlist}
            if isinstance(request_allowlist, list)
            else None
        )
        definitions.extend(
            definition for definition in permission_tool_definitions()
            if allowed is None or definition["function"]["name"] in allowed
        )
        return definitions

    def _operation_id(self, tool_call_id: str) -> str:
        raw = json.dumps(
            [self._permission_context.instance_id, self._permission_context.task_id,
             self._permission_context.run_id, tool_call_id],
            ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")
        return "op_" + hashlib.sha256(raw).hexdigest()

    def _finish_result(
        self, name: str, arguments: dict, result: ToolResult, started: float
    ) -> ToolResult:
        if name in {"file_write", "apply_patch", "run_script"} and (result.details or {}).get("control_disposition") == "allowed":
            from orchestrator.fingerprint.runtime import context_policy
            try:
                enabled = context_policy(self._effective_audit_context()).enabled
                policy_status = "frozen"
            except ValueError:
                enabled, policy_status = True, "unverifiable"
            if enabled:
                # Retain verified permission facts without reopening a write-only
                # handle or granting an evidence-directory write.
                result.details = {**(result.details or {}), "fingerprint": {
                    "business_status": "failed" if result.is_error else "completed",
                    "evidence_status": "permission_result_only", "policy_status": policy_status,
                    "marker_status": "not_qualified", "coverage_complete": False,
                    "before_state": "not_observed", "after_state": "not_observed",
                    "permission_operation": (result.details or {}).get("permission_operation"),
                    "permission_replayed": (result.details or {}).get("permission_replayed", False),
                    "gap": "artifact hash/marker adapter is not qualified for this supervised handle",
                }}
        finalized = self._finalize_tool_result(name, arguments, result, started)
        self._record_tool_audit(name, arguments, finalized, started)
        return finalized

    async def execute(
        self, tool_name: str, arguments: dict, tool_call_id: str = ""
    ) -> ToolResult | GateResult:
        # Ordinary questions remain ordinary questions and never grant access.
        if tool_name in _ORDINARY_QUESTION_NAMES:
            if self._ordinary_questions is None:
                if not self.permission_durable_wait:
                    return await super().execute(tool_name, arguments, tool_call_id)
                return ToolResult(tool_call_id=tool_call_id, output="Error: question_scope_unavailable", is_error=True)
            started = time.monotonic()
            admission = self.evaluate_admission(tool_name, arguments, str(tool_call_id or ""))
            if admission is not None:
                return self._finish_result(tool_name, arguments, admission, started)
            output = await self._ordinary_questions.execute(tool_name, arguments, str(tool_call_id or ""))
            return self._finish_result(tool_name, arguments, ToolResult(
                tool_call_id=tool_call_id, output=output, is_error=output.startswith("Error:"),
                details={"grants_permissions": False},
            ), started)
        started = time.monotonic()
        call_id = str(tool_call_id or "")
        admission = self.evaluate_admission(tool_name, arguments, call_id)
        if admission is not None:
            return self._finish_result(tool_name, arguments, admission, started)
        if tool_name in _PERMISSION_NAMES:
            try:
                response = await self._permission_tools.execute(tool_name, arguments)
                if self.permission_durable_wait and response.get("status") == "pending":
                    from orchestrator.permissions.continuation import PermissionPark

                    raise PermissionPark(GateResult("waiting", "", response["request_id"]))
                result = ToolResult(
                    tool_call_id=call_id, output=json.dumps(response, ensure_ascii=False),
                    details={"control_disposition": "allowed"},
                )
            except (PermissionToolError, ValueError):
                result = ToolResult(
                    tool_call_id=call_id, output="Permission request unavailable or invalid.",
                    is_error=True, details={"control_disposition": "denied"},
                )
            return self._finish_result(tool_name, arguments, result, started)
        if not call_id:
            return GateResult("unavailable", "")
        roots, _, cwd = self._read_scope()
        if (Path(cwd).resolve(strict=False) != self._permission_cwd
                or tuple(Path(root).resolve(strict=False) for root in roots) != self._permission_roots):
            return GateResult("unavailable", self._operation_id(call_id))
        operation_id = self._operation_id(call_id)
        outcome = await self._permission_gate.execute(
            operation_id, tool_name, arguments, reason=f"Use {tool_name}",
            wait_for_approval=not self.permission_durable_wait,
        )
        if self.permission_durable_wait and outcome.state == "unsupported":
            result = ToolResult(
                tool_call_id=call_id, output="This tool or resource scope is unavailable under the installed policy. Choose a supported alternative.",
                is_error=False, details={"control_disposition": "denied", "permission_operation": operation_id},
            )
            return self._finish_result(tool_name, arguments, result, started)
        if self.permission_durable_wait and outcome.state in {"denied", "withdrawn"}:
            result = ToolResult(
                tool_call_id=call_id, output="Access was denied or withdrawn. Keep completed work and choose an allowed alternative.",
                is_error=False, details={"control_disposition": "denied",
                                         "permission_operation": operation_id},
            )
            return self._finish_result(tool_name, arguments, result, started)
        if self.permission_durable_wait and outcome.state not in {"succeeded", "failed"}:
            from orchestrator.permissions.continuation import PermissionPark

            raise PermissionPark(outcome)
        if outcome.state not in {"succeeded", "failed"}:
            # This typed control state must be handled by HERV3, not converted
            # to a provider failure or a model-retryable ordinary tool error.
            return outcome
        if not isinstance(outcome.payload, str):
            return GateResult("unknown", operation_id, outcome.request_id)
        result = ToolResult(
            tool_call_id=call_id, output=outcome.payload,
            is_error=outcome.state == "failed",
            details={
                "control_disposition": "allowed", "permission_operation": operation_id,
                "permission_replayed": outcome.replayed,
            },
        )
        return self._finish_result(tool_name, arguments, result, started)



class ManagedCatalogueRegistry(ToolRegistry):
    """Derive menus/schema before a Run without retaining a legacy executor."""

    def __init__(self, *args, **kwargs):
        if kwargs.get("secrets"):
            raise ValueError("a managed catalogue cannot retain credentials")
        super().__init__(*args, **kwargs)

    def get_tool_definitions(self, tiers=None):
        definitions = super().get_tool_definitions(tiers)
        definitions.extend(permission_tool_definitions())
        return definitions

    async def execute(self, tool_name, arguments, tool_call_id=""):
        return ToolResult(
            tool_call_id=tool_call_id,
            output="Managed tools require a current canonical PAO Run and its assigned execution channel.",
            is_error=False, details={"control_disposition": "denied"},
        )
