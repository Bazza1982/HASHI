from __future__ import annotations

import json
import os
import tempfile
from types import SimpleNamespace
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from tools.registry import ToolRegistry
from tools.schemas import ALL_TOOL_NAMES
from tools.workbench_client import (
    live_workbench_api_base_url as _live_workbench_api_base_url,
)
from orchestrator.file_permissions import tighten_fd_permissions

CONTEXT_SCHEMA_VERSION = 6
_COMPATIBLE_CONTEXT_SCHEMA_VERSIONS = frozenset({3, 4, 5, CONTEXT_SCHEMA_VERSION})

# LEGACY HER V1 ONLY. The subprocess Tool Gateway is not part of HERV3 or any
# direct API backend. Its circuit breakers remain solely to contain a retired
# compatibility protocol and must never be imported as active-runtime policy.
LEGACY_HER_GATEWAY_MAX_CALLS = 100

_TOOL_SECRET_KEYS = {
    "web_search": {"brave_api_key"},
    "xai_imagine": {"xai_api_key", "XAI_API_KEY", "xai_oauth_refresh_token"},
    "telegram_send": {"telegram_bot_token", "_authorized_telegram_id"},
    "telegram_send_file": {
        "telegram_bot_token",
        "_agent_telegram_token",
        "_authorized_telegram_id",
    },
}


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {
            str(key): _json_safe(item)
            for key, item in value.items()
            if str(key) not in {"global_config", "_runtime"}
        }
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return None


def live_workbench_api_base_url(
    registry: ToolRegistry,
    global_config: Any,
) -> str:
    """Return the live Backend API address for an isolated Tool Gateway."""

    url = _live_workbench_api_base_url(
        registry.audit_context,
        global_config,
    )
    if not url:
        raise ValueError("live HASHI Backend API endpoint is unavailable")
    return url


@dataclass(frozen=True)
class GatewayContext:
    schema_version: int
    agent: str
    backend: str
    workspace_dir: str
    access_root: str
    media_roots: list[str]
    allowed_tools: list[str]
    workbench_api_base_url: str = ""
    # Compatibility snapshot for schema v3 readers. New tools use the shared
    # Workbench endpoint above rather than a scheduler-specific address.
    scheduler_api_base_url: str = ""
    max_calls: int = 100
    max_identical_calls: int = 3
    max_consecutive_errors: int = 5
    tool_options: dict[str, Any] = field(default_factory=dict)
    secrets: dict[str, Any] = field(default_factory=dict)
    agents_config: list[dict[str, Any]] = field(default_factory=list)
    audit: dict[str, Any] = field(default_factory=dict)
    vision_enabled: bool = True
    enforce_legacy_limits: bool = False
    global_context: dict[str, Any] = field(default_factory=dict)
    canonical_audit: dict[str, Any] = field(default_factory=dict)
    access_roots: list[str] = field(default_factory=list)
    search_scope: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_registry(
        cls,
        registry: ToolRegistry,
        *,
        backend: str = "her",
        additional_allowed_tools: set[str] | None = None,
        media_roots: list[Path] | None = None,
        workbench_api_base_url: str = "",
        scheduler_api_base_url: str = "",
        vision_enabled: bool = True,
    ) -> GatewayContext:
        audit = _json_safe(registry.audit_context or {}) or {}
        required_secret_keys = set()
        allowed_tools = set(registry._allowed)
        allowed_tools.update(additional_allowed_tools or set())
        allowed_tools.intersection_update(ALL_TOOL_NAMES)
        for tool_name in allowed_tools:
            required_secret_keys.update(_TOOL_SECRET_KEYS.get(tool_name, set()))
            if tool_name.startswith("obsidian_"):
                required_secret_keys.update({"obsidian_base_url", "obsidian_api_key"})
        scoped_secrets = {
            key: value
            for key, value in registry.secrets.items()
            if key in required_secret_keys
        }
        effective_workbench_url = (
            str(workbench_api_base_url or scheduler_api_base_url or "")
            .strip()
            .rstrip("/")
        )
        global_config = (registry.audit_context or {}).get("global_config")
        if allowed_tools.intersection({"ask_user", "get_user_answer"}) and audit.get("request_id") and registry.secrets.get("workbench_admin_token"):
            from orchestrator.run_questions import issue_tool_token
            scoped_secrets["run_question_token"] = issue_tool_token(registry.secrets["workbench_admin_token"],
                instance_id=str(getattr(global_config, "instance_id", None) or audit.get("instance_id") or ""),
                agent_id=str(audit.get("agent_name") or ""), request_id=str(audit["request_id"]))
        canonical = getattr(registry, "canonical_audit", None)
        return cls(
            schema_version=CONTEXT_SCHEMA_VERSION,
            agent=str(audit.get("agent_name") or registry.workspace_dir.name),
            backend=backend,
            workspace_dir=str(registry.workspace_dir.resolve()),
            access_root=str(registry.access_root.resolve()),
            access_roots=[str(root) for root in registry.access_roots],
            search_scope=_json_safe(registry.search_scope) or {},
            media_roots=[
                str(Path(root).expanduser().resolve())
                for root in (
                    media_roots if media_roots is not None else registry.media_roots
                )
            ],
            allowed_tools=sorted(allowed_tools),
            workbench_api_base_url=effective_workbench_url,
            scheduler_api_base_url=effective_workbench_url,
            max_calls=(
                max(1, int(registry.max_loops) * 4)
                if registry.max_loops is not None
                else LEGACY_HER_GATEWAY_MAX_CALLS
            ),
            max_identical_calls=max(
                1,
                int(
                    (registry.tool_options.get("gateway") or {}).get(
                        "max_identical_calls", 3
                    )
                ),
            ),
            max_consecutive_errors=max(
                1,
                int(
                    (registry.tool_options.get("gateway") or {}).get(
                        "max_consecutive_errors", 5
                    )
                ),
            ),
            tool_options=_json_safe(registry.tool_options) or {},
            secrets=_json_safe(scoped_secrets) or {},
            agents_config=_json_safe(registry.agents_config) or [],
            audit={**audit, "backend": backend},
            vision_enabled=vision_enabled,
            enforce_legacy_limits=backend in {"her", "her-v1"},
            global_context=(
                {
                    "instance_id": str(getattr(global_config, "instance_id", "HASHI")),
                    # Paths are authority facts, not secret contents. Preserve
                    # the same read/execute protection through the CLI gateway.
                    **{key: str(getattr(global_config, key)) for key in
                       ("project_root", "bridge_home", "secrets_path")
                       if getattr(global_config, key, None) is not None},
                    "central_memory": _json_safe(
                        getattr(global_config, "central_memory", None) or {}
                    ),
                    "wiki_provider": _json_safe(
                        getattr(global_config, "wiki_provider", None) or {}
                    ),
                }
                if global_config is not None
                else {}
            ),
            canonical_audit=(
                _json_safe(canonical.descriptor()) or {}
                if canonical is not None and hasattr(canonical, "descriptor")
                else {}
            ),
        )

    def build_registry(self) -> ToolRegistry:
        audit = dict(self.audit)
        if self.global_context:
            audit["global_config"] = SimpleNamespace(**self.global_context)
        workbench_api_base_url = (
            (self.workbench_api_base_url or self.scheduler_api_base_url)
            .strip()
            .rstrip("/")
        )
        if workbench_api_base_url:
            audit["workbench_api_base_url"] = workbench_api_base_url
            audit["scheduler_api_base_url"] = workbench_api_base_url
        canonical = None
        if self.canonical_audit:
            from orchestrator.canonical_audit import CanonicalAuditStore

            descriptor = dict(self.canonical_audit)
            canonical = CanonicalAuditStore(
                descriptor["bridge_home"],
                instance_id=descriptor["instance_id"],
                agent_id=descriptor["agent_id"],
                config=descriptor.get("config"),
            )
        return ToolRegistry(
            allowed_tools=self.allowed_tools,
            access_root=Path(self.access_root),
            access_roots=[Path(root) for root in self.access_roots],
            search_scope=self.search_scope,
            workspace_dir=Path(self.workspace_dir),
            secrets=self.secrets,
            tool_options=self.tool_options,
            max_loops=(max(1, self.max_calls // 4) if self.enforce_legacy_limits else None),
            agents_config=self.agents_config,
            audit_context=audit,
            media_roots=[Path(root) for root in self.media_roots],
            canonical_audit=canonical,
        )


def write_gateway_context(
    registry: ToolRegistry,
    path: Path,
    *,
    additional_allowed_tools: set[str] | None = None,
    media_roots: list[Path] | None = None,
    workbench_api_base_url: str = "",
    scheduler_api_base_url: str = "",
    vision_enabled: bool = True,
    backend: str = "her",
) -> GatewayContext:
    context = GatewayContext.from_registry(
        registry,
        backend=backend,
        additional_allowed_tools=additional_allowed_tools,
        media_roots=media_roots,
        workbench_api_base_url=workbench_api_base_url,
        scheduler_api_base_url=scheduler_api_base_url,
        vision_enabled=vision_enabled,
    )
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(asdict(context), ensure_ascii=False, indent=2, sort_keys=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        tighten_fd_permissions(fd)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        Path(temporary).unlink(missing_ok=True)
        raise
    return context


def load_gateway_context(path: Path) -> GatewayContext:
    path = Path(path)
    if os.name != "nt":
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            raise PermissionError(
                f"gateway context must be owner-only (0600): {path} mode={mode:o}"
            )
    # Windows' stat result exposes synthetic POSIX bits (normally 0666) and
    # cannot describe the NTFS DACL.  The portable installer owns the local
    # data-tree ACL, so rejecting that synthetic mode would make every fixed
    # Tool Gateway context unreadable on the supported Windows runtime.
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") not in _COMPATIBLE_CONTEXT_SCHEMA_VERSIONS:
        raise ValueError(
            f"unsupported gateway context schema_version={payload.get('schema_version')!r}"
        )
    return GatewayContext(**payload)
