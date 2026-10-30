"""Describe known tool effects without executing them or claiming OS isolation.

Only reviewed tools appear here. Unknown tools fail closed until they have a
specific description. A descriptor is not an authorization or a substitute for
rebinding the actual resource immediately before execution.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from orchestrator.permissions.models import Scope, canonical_scope_set, normalize_scope


class UnknownTool(ValueError):
    """No reviewed description exists for the requested tool."""


@dataclass(frozen=True, slots=True)
class ToolDescriptor:
    tool_name: str
    scopes: tuple[Scope, ...]
    arguments_digest: str
    coverage: str
    limitations: tuple[str, ...]


_FILE_ACTIONS = {
    "file_read": ("read",),
    "file_list": ("list",),
    "file_write": ("create", "write"),
    "apply_patch": ("create", "read", "write"),
}
_CONTROL_TOOLS = frozenset({"ask_user", "get_user_answer"})
_UNRESTRICTED_EXECUTORS = frozenset({"shell", "bash"})


def _validate_json(value: Any, *, depth: int = 0, active: set[int] | None = None) -> None:
    if depth > 100:
        raise ValueError("tool arguments are too deeply nested")
    if value is None or type(value) in (bool, int, float, str):
        return
    if type(value) not in (list, dict):
        raise ValueError("tool arguments must contain only JSON types")
    active = active if active is not None else set()
    if id(value) in active:
        raise ValueError("tool arguments cannot contain cycles")
    active.add(id(value))
    try:
        if isinstance(value, dict):
            if any(type(key) is not str for key in value):
                raise ValueError("JSON object keys must be strings")
            children = value.values()
        else:
            children = value
        for child in children:
            _validate_json(child, depth=depth + 1, active=active)
    finally:
        active.remove(id(value))


def _arguments_digest(tool_name: str, arguments: dict[str, Any], cwd: Path) -> str:
    _validate_json(arguments)
    try:
        payload = json.dumps(
            {"version": 1, "tool": tool_name, "arguments": arguments, "cwd": str(cwd)},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("tool arguments must be finite JSON data") from exc
    return sha256(payload).hexdigest()


def _network_scope(url: object, actions: tuple[str, ...], cwd: Path) -> Scope:
    if not isinstance(url, str) or not url:
        raise ValueError("network action needs an explicit URL")
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise ValueError("invalid network URL") from exc
    if not parts.scheme or not parts.netloc:
        raise ValueError("network action needs an absolute URL")
    # Retain credentials in the temporary string so the origin normalizer
    # rejects them; never save this string or include it in a descriptor.
    origin = f"{parts.scheme}://{parts.netloc}"
    return normalize_scope("network", origin, actions, cwd=cwd)


def describe_tool(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    cwd: Path,
    read_roots: tuple[Path, ...],
) -> ToolDescriptor:
    if not isinstance(tool_name, str) or not tool_name:
        raise UnknownTool("tool name is missing")
    if not isinstance(arguments, dict):
        raise ValueError("tool arguments must be an object")
    cwd = Path(cwd)
    if not cwd.is_absolute():
        raise ValueError("trusted execution directory must be absolute")
    cwd = cwd.resolve(strict=False)
    digest = _arguments_digest(tool_name, arguments, cwd)

    if tool_name in _CONTROL_TOOLS:
        return ToolDescriptor(
            tool_name, (), digest, "control",
            ("Ordinary question answers never grant permissions.",),
        )

    if tool_name == "run_script":
        from orchestrator.permissions.script_protocol import scopes as script_scopes

        return ToolDescriptor(tool_name, canonical_scope_set(script_scopes(arguments, cwd=cwd)), digest, "partial", (
            "Offline isolated Python; real inputs are snapshots and outputs remain temporary until saved by a file tool.",
        ))

    if tool_name in _FILE_ACTIONS:
        resource = arguments.get("path")
        scope = normalize_scope("file", resource, _FILE_ACTIONS[tool_name], cwd=cwd)
        return ToolDescriptor(
            tool_name, (scope,), digest, "partial",
            ("Path identity must be rebound at execution; this is not an OS file fence.",),
        )

    if tool_name == "file_search":
        raw_roots = arguments.get("roots")
        if raw_roots is None:
            roots = tuple(read_roots)
        elif isinstance(raw_roots, list) and raw_roots:
            if any(not isinstance(root, str) or not root.strip() for root in raw_roots):
                raise ValueError("search roots must be paths")
            roots = tuple(raw_roots)
        else:
            raise ValueError("search roots must be a nonempty list")
        if not roots:
            raise ValueError("trusted default search roots are unavailable")
        scopes = canonical_scope_set(
            normalize_scope("file", str(root), ("list", "read"), cwd=cwd) for root in roots
        )
        return ToolDescriptor(
            tool_name, scopes, digest, "partial",
            ("Search must rebind each actual root and visited object at execution.",),
        )

    if tool_name in _UNRESTRICTED_EXECUTORS or (
        tool_name == "verification_run" and arguments.get("operation") == "run"
    ):
        scope = normalize_scope("capability", "process.unrestricted", ("execute",), cwd=cwd)
        return ToolDescriptor(
            tool_name, (scope,), digest, "partial",
            ("The local process may access files, networks, credentials and child processes.",),
        )

    if tool_name == "verification_run" and arguments.get("operation") == "list":
        scope = normalize_scope("capability", "verification.recipes", ("read",), cwd=cwd)
        return ToolDescriptor(tool_name, (scope,), digest, "partial", ("Recipe metadata only.",))

    if tool_name in {"http_request", "web_fetch"}:
        raw_url = arguments.get("url")
        method = arguments.get("method", "GET") if tool_name == "http_request" else "GET"
        if not isinstance(method, str) or method.upper() not in {
            "GET", "HEAD", "POST", "PUT", "PATCH", "DELETE",
        }:
            raise ValueError("unknown network method")
        method = method.upper()
        if method in {"GET", "HEAD"}:
            actions = ["fetch", "send"]
            # A caller-controlled URL path can carry read data just as a query or
            # header can. Origin permission alone cannot promise receive-only I/O.
        else:
            actions = ["send", "upload"]
        scope = _network_scope(raw_url, tuple(actions), cwd)
        scopes = [scope]
        if tool_name == "http_request" and arguments.get("credential") is not None:
            name = arguments["credential"]
            if not isinstance(name, str) or not name or len(name) > 64:
                raise ValueError("a named installed credential capability is required")
            scopes.append(normalize_scope("capability", "credential:" + name, ("use",), cwd=cwd))
        return ToolDescriptor(
            tool_name, canonical_scope_set(scopes), digest, "partial",
            ("Redirects, DNS changes and actual connections need execution-time checks.",),
        )

    if tool_name == "process_kill":
        pid = arguments.get("pid")
        if type(pid) is not int or pid <= 0:
            raise ValueError("process identity must be a positive PID")
        scope = normalize_scope("capability", f"process:{pid}", ("control",), cwd=cwd)
        return ToolDescriptor(
            tool_name, (scope,), digest, "partial",
            ("The process identity must be rebound to avoid PID reuse.",),
        )

    raise UnknownTool(f"tool has no reviewed permission descriptor: {tool_name}")
