"""Agent permission-request tools, not an approval or execution authority.

The runtime must inject a trusted context, policy ceiling and authority. This
module is not registered on the legacy L0 path. A tool result is a presentation
of ledger state, never a bearer capability or a substitute for execution checks.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping, Protocol

from orchestrator.permissions.authority import (
    AccessRequest,
    OperationConflict,
    PermissionDenied,
    PermissionLease,
)
from orchestrator.permissions.models import (
    ExecutionContext,
    Scope,
    canonical_scope_set,
    normalize_scope,
    scope_covers,
)


def permission_tool_definitions() -> list[dict[str, Any]]:
    """Return fresh schemas only for an explicitly managed execution."""
    resources = [
        {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": [kind]},
                field: {"type": "string"},
                "actions": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 16},
            },
            "required": ["kind", field, "actions"],
            "additionalProperties": False,
        }
        for kind, field in (("filesystem", "path"), ("network", "origin"), ("capability", "name"))
    ]
    return [
        {
            "type": "function",
            "function": {
                "name": "request_access",
                "description": "Request task-scoped access; only a user can approve it.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reason": {"type": "string", "minLength": 1, "maxLength": 2000},
                        "resources": {"type": "array", "items": {"oneOf": resources},
                                      "minItems": 1, "maxItems": 32},
                        "duration": {
                            "type": "object",
                            "properties": {
                                "mode": {"type": "string", "enum": ["run", "task", "ttl"]},
                                "max_seconds": {"type": "integer", "minimum": 1},
                            },
                            "required": ["mode", "max_seconds"],
                            "additionalProperties": False,
                        },
                        "request_key": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"},
                    },
                    "required": ["reason", "resources", "duration", "request_key"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_access_status",
                "description": "Check a permission request or lease for the current task.",
                "parameters": {
                    "type": "object",
                    "properties": {"request_id": {"type": "string"}, "lease_id": {"type": "string"}},
                    "oneOf": [{"required": ["request_id"]}, {"required": ["lease_id"]}],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "release_access",
                "description": "Release a permission lease owned by the current task.",
                "parameters": {
                    "type": "object",
                    "properties": {"lease_id": {"type": "string"}},
                    "required": ["lease_id"],
                    "additionalProperties": False,
                },
            },
        },
    ]


class PermissionAuthorityPort(Protocol):
    """Trusted PAO port; a transport must authenticate its own caller."""

    def request_access(self, context: ExecutionContext, scopes: tuple[Scope, ...], **kwargs: Any) -> AccessRequest: ...

    def get_request(self, context: ExecutionContext, request_id: str) -> AccessRequest: ...

    def get_lease_for_task(self, context: ExecutionContext, lease_id: str) -> PermissionLease: ...

    def revoke(self, context: ExecutionContext, lease_id: str) -> None: ...


class PermissionToolError(ValueError):
    """Malformed or conflicting agent request."""


def _exact_mapping(value: object, keys: set[str]) -> Mapping[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise PermissionToolError("invalid permission tool fields")
    return value


def _request_view(request: AccessRequest) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": request.state,
        "resources": [asdict(scope) for scope in request.scopes],
        "reason": request.purpose,
        "suggested_duration": {
            "mode": request.suggested_lifetime,
            "max_seconds": request.suggested_seconds,
        },
        "expires_at": request.expires_at,
    }
    if request.request_id:
        result.update(request_id=request.request_id, revision=request.revision)
    return result


class PermissionAgentTools:
    """A task-bound tool port; transport identity is supplied by trusted PAO."""

    def __init__(
        self,
        authority: PermissionAuthorityPort,
        context: ExecutionContext,
        *,
        cwd: Path,
        requestable_scopes: tuple[Scope, ...],
        durable_wait: bool = False,
    ) -> None:
        methods = ("request_access", "get_request", "get_lease_for_task", "revoke")
        if not isinstance(context, ExecutionContext) or not all(
            callable(getattr(authority, name, None)) for name in methods
        ):
            raise ValueError("trusted authority and execution context are required")
        directory = Path(cwd)
        if not directory.is_absolute():
            raise ValueError("trusted working directory must be absolute")
        if type(durable_wait) is not bool or (durable_wait and not callable(getattr(authority, "evaluate", None))):
            raise ValueError("durable requests require the authoritative evaluation port")
        self._durable_wait = durable_wait
        self._authority = authority
        self._context = context
        self._cwd = directory.resolve(strict=False)
        self._requestable = canonical_scope_set(requestable_scopes)

    def _parse_resources(self, value: object) -> tuple[Scope, ...]:
        if not isinstance(value, list) or not 1 <= len(value) <= 32:
            raise PermissionToolError("resources must contain 1 to 32 entries")
        scopes = []
        for resource in value:
            if not isinstance(resource, dict):
                raise PermissionToolError("resource must be an object")
            kind = resource.get("kind")
            if kind == "filesystem":
                item = _exact_mapping(resource, {"kind", "path", "actions"})
                scope_kind, identifier = "file", item["path"]
            elif kind == "network":
                item = _exact_mapping(resource, {"kind", "origin", "actions"})
                scope_kind, identifier = "network", item["origin"]
            elif kind == "capability":
                item = _exact_mapping(resource, {"kind", "name", "actions"})
                scope_kind, identifier = "capability", item["name"]
            else:
                raise PermissionToolError("resource kind is unavailable")
            actions = item["actions"]
            if not isinstance(actions, list) or not 1 <= len(actions) <= 16:
                raise PermissionToolError("actions must contain 1 to 16 entries")
            try:
                scopes.append(normalize_scope(scope_kind, identifier, actions, cwd=self._cwd))
            except (TypeError, ValueError) as exc:
                raise PermissionToolError("invalid resource scope") from exc
        return canonical_scope_set(scopes)

    async def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            raise PermissionToolError("tool arguments must be an object")
        if name == "request_access":
            item = _exact_mapping(arguments, {"reason", "resources", "duration", "request_key"})
            scopes = self._parse_resources(item["resources"])
            if not all(any(scope_covers(parent, required) for parent in self._requestable)
                       for required in scopes):
                return {"status": "unavailable"}
            duration = _exact_mapping(item["duration"], {"mode", "max_seconds"})
            if not isinstance(duration["mode"], str) or duration["mode"] not in {"run", "task", "ttl"}:
                raise PermissionToolError("proactive requests cannot suggest this duration")
            if not isinstance(item["request_key"], str) or not item["request_key"]:
                raise PermissionToolError("request_key is required")
            try:
                request = await asyncio.to_thread(
                    self._authority.request_access,
                    self._context,
                    scopes,
                    purpose=item["reason"],
                    suggested_lifetime=duration["mode"],
                    suggested_seconds=duration["max_seconds"],
                    request_key=item["request_key"],
                )
            except PermissionDenied:
                raise
            except (ValueError, OperationConflict) as exc:
                raise PermissionToolError("invalid or conflicting access request") from exc
            if self._durable_wait and request.state not in {"denied", "withdrawn"}:
                covered = await asyncio.to_thread(self._authority.evaluate, self._context, scopes)
                if covered:
                    view = _request_view(request)
                    view["status"] = "granted_existing"
                    return view
                if request.state in {"decided", "expired", "granted_existing"}:
                    request = await asyncio.to_thread(
                        self._authority.request_access, self._context, scopes,
                        purpose=item["reason"], suggested_lifetime=duration["mode"],
                        suggested_seconds=duration["max_seconds"],
                    )
            return _request_view(request)

        if name == "get_access_status":
            if set(arguments) == {"request_id"} and isinstance(arguments["request_id"], str):
                request = await asyncio.to_thread(
                    self._authority.get_request, self._context, arguments["request_id"]
                )
                return _request_view(request)
            if set(arguments) == {"lease_id"} and isinstance(arguments["lease_id"], str):
                lease = await asyncio.to_thread(
                    self._authority.get_lease_for_task, self._context, arguments["lease_id"]
                )
                return {
                    "status": lease.status,
                    "lease_id": lease.lease_id,
                    "resources": [asdict(scope) for scope in lease.scopes],
                    "expires_at": lease.expires_at,
                }
            raise PermissionToolError("specify one request_id or lease_id")

        if name == "release_access":
            item = _exact_mapping(arguments, {"lease_id"})
            if not isinstance(item["lease_id"], str) or not item["lease_id"]:
                raise PermissionToolError("lease_id is required")
            await asyncio.to_thread(self._authority.revoke, self._context, item["lease_id"])
            return {"status": "revoked", "lease_id": item["lease_id"]}

        raise PermissionToolError("permission tool is unavailable")
