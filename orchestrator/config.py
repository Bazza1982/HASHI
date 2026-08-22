from __future__ import annotations
import json
import logging
import os
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, Any, List

from orchestrator.enterprise.profile import (
    parse_profile_context,
    validate_profile_context,
)
from orchestrator.pathing import resolve_command_value, resolve_path_value
from orchestrator.runtime_defaults import DEFAULT_HASHI_REMOTE_PORT, DEFAULT_WORKBENCH_PORT
from orchestrator.flexible_backend_registry import (
    canonical_backend_engine,
    normalize_allowed_backends,
)

# Valid access_scope values:
#   "workspace" - only the agent's workspace_dir (most restrictive)
#   "project"   - the project root / repo root (sensible default)
#   "drive"     - full drive root e.g. C:\ (least restrictive)
VALID_ACCESS_SCOPES = {"workspace", "project", "drive"}
SESSION_MODE_BACKENDS = frozenset({"claude-cli", "codex-cli", "grok-cli"})
LEGACY_FIXED_CONFIG_BACKUP_SUFFIX = ".pre-flex-migration.bak"
config_logger = logging.getLogger("BridgeU.Config")


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def resolve_access_root(scope: str, workspace_dir: Path, project_root: Path) -> Path:
    """Resolve an access_scope string to an actual filesystem path."""
    if scope == "workspace":
        return workspace_dir
    elif scope == "project":
        return project_root if project_root is not None else workspace_dir
    elif scope == "drive":
        return Path(workspace_dir.anchor)
    # Safe fallback
    return workspace_dir


@dataclass
class GlobalConfig:
    authorized_id: int
    deployment_profile: str = "personal"
    organization_id: str | None = None
    base_logs_dir: Path | None = None
    base_media_dir: Path | None = None
    instance_id: str = "HASHI"
    display_name: str = "HASHI Instance"
    api_host: str = "127.0.0.1"
    remote_port: int = DEFAULT_HASHI_REMOTE_PORT
    project_root: Path = None
    bridge_home: Path = None
    config_path: Path = None
    secrets_path: Path = None
    workbench_port: int = DEFAULT_WORKBENCH_PORT
    api_gateway_port: int = 18801
    gemini_cmd: str = "gemini"
    claude_cmd: str = "claude"
    codex_cmd: str = "codex"
    grok_cmd: str = "grok"
    gh_copilot_cmd: str = "gh copilot"
    hermes_home: str | None = None
    xai_api_base_url: str = "https://api.x.ai/v1"
    xai_use_responses_api: bool = False
    xai_oauth: Dict[str, Any] = field(default_factory=dict)
    openrouter_url: str = "https://openrouter.ai/api/v1/chat/completions"
    claw_providers: Dict[str, Any] = field(default_factory=dict)
    her_providers: Dict[str, Any] = field(default_factory=dict)
    enterprise_auth_providers: List[Dict[str, Any]] = field(default_factory=list)
    enterprise_database_url: str | None = None
    enterprise_scheduler_lease_enabled: bool = False
    enterprise_scheduler_lease_backend: str = "db"
    enterprise_scheduler_lease_name: str = "superloop-scheduler"
    enterprise_scheduler_lease_holder: str | None = None
    enterprise_scheduler_lease_ttl_seconds: int = 60
    enterprise_scheduler_lease_kubernetes_namespace: str | None = None
    enterprise_scheduler_lease_kubernetes_in_cluster: bool = True
    enterprise_scheduler_lease_kubeconfig_path: str | None = None
    enterprise_scheduler_lease_pool_enabled: bool = False
    enterprise_scheduler_lease_pool_min_size: int = 1
    enterprise_scheduler_lease_pool_max_size: int = 4

@dataclass
class AgentConfig:
    """Concrete one-backend adapter configuration built by Flex runtime."""

    name: str
    engine: str
    workspace_dir: Path
    system_md: Path
    model: str
    is_active: bool
    access_scope: str = "project"
    extra: Dict[str, Any] = None
    project_root: Path = field(default=None, repr=False)

    def resolve_access_root(self) -> Path:
        return resolve_access_root(self.access_scope, self.workspace_dir, self.project_root)

@dataclass
class FlexibleAgentConfig:
    name: str
    workspace_dir: Path
    system_md: Path
    telegram_token_key: str
    allowed_backends: List[Dict[str, Any]]
    active_backend: str
    is_active: bool = True
    type: str = "flex"
    default_mode: str = "flex"
    access_scope: str = "project"
    extra: Dict[str, Any] = None
    project_root: Path = field(default=None, repr=False)

    def resolve_access_root(self) -> Path:
        return resolve_access_root(self.access_scope, self.workspace_dir, self.project_root)

class ConfigManager:
    def __init__(self, config_path: Path, secrets_path: Path, bridge_home: Path | None = None):
        self.config_path = config_path
        self.secrets_path = secrets_path
        self.bridge_home = bridge_home or config_path.parent

    def _migrate_legacy_fixed_agents(self, raw_cfg: dict) -> list[str]:
        """Convert explicit legacy fixed rows to the sole Flex runtime shape.

        The migration is deliberately limited to explicit ``type: fixed`` rows.
        Missing types remain invalid so a typo can never select a runtime by
        accident. Session-capable CLI backends retain their old conversational
        behavior through Flex's supported ``default_mode: fixed`` contract.
        """

        migrated_names: list[str] = []
        for index, original in enumerate(raw_cfg.get("agents", [])):
            if not isinstance(original, dict) or original.get("type") != "fixed":
                continue
            row = dict(original)
            name = str(row.get("name") or f"agent-{index}")
            engine = canonical_backend_engine(row.pop("engine", None))
            if not engine:
                raise ValueError(
                    f"Agent '{name}' uses legacy type='fixed' but has no engine to migrate."
                )
            model = row.pop("model", "default")
            row.pop("resume_policy", None)
            row["type"] = "flex"
            row.setdefault("telegram_token_key", name)
            row["active_backend"] = engine
            if not row.get("allowed_backends"):
                backend = {"engine": engine}
                if model and model != "default":
                    backend["model"] = model
                row["allowed_backends"] = [backend]
            row["default_mode"] = (
                "fixed" if engine in SESSION_MODE_BACKENDS else "flex"
            )
            raw_cfg["agents"][index] = row
            migrated_names.append(name)
            config_logger.warning(
                "Migrated retired type='fixed' agent '%s' to Flex runtime "
                "(backend=%s, default_mode=%s).",
                name,
                engine,
                row["default_mode"],
            )
        return migrated_names

    def _persist_legacy_fixed_migration(self, raw_cfg: dict) -> None:
        """Atomically persist the one-time config migration with a local backup."""

        backup_path = self.config_path.with_name(
            self.config_path.name + LEGACY_FIXED_CONFIG_BACKUP_SUFFIX
        )
        if not backup_path.exists():
            backup_path.write_bytes(self.config_path.read_bytes())
        temp_path = self.config_path.with_name(
            f".{self.config_path.name}.fixed-migration-{os.getpid()}.tmp"
        )
        temp_path.write_text(
            json.dumps(raw_cfg, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temp_path.replace(self.config_path)

    def load(self) -> tuple[GlobalConfig, list[FlexibleAgentConfig], dict]:
        with open(self.config_path, "r", encoding="utf-8-sig") as f:
            raw_cfg = json.load(f)

        migrated_fixed_agents = self._migrate_legacy_fixed_agents(raw_cfg)
        if migrated_fixed_agents:
            try:
                self._persist_legacy_fixed_migration(raw_cfg)
            except OSError as exc:
                config_logger.warning(
                    "Legacy fixed agents were migrated in memory but the normalized "
                    "configuration could not be persisted: %s",
                    exc,
                )

        with open(self.secrets_path, "r", encoding="utf-8-sig") as f:
            secrets = json.load(f)

        g_raw = raw_cfg["global"]
        profile_ctx = parse_profile_context(g_raw)
        validate_profile_context(profile_ctx)
        config_dir = self.config_path.parent
        code_root = Path(__file__).resolve().parent.parent
        bridge_home = self.bridge_home

        # authorized_id: secrets.json takes priority (written by Hashiko during
        # AI-driven onboarding); falls back to agents.json for manual setups.
        # Value of 0 means Telegram not yet configured (workbench-only mode).
        _auth_id = int(secrets.get("authorized_telegram_id", 0)) or int(g_raw.get("authorized_id", 0))

        workbench_port = int(g_raw.get("workbench_port", DEFAULT_WORKBENCH_PORT))
        api_gateway_port = int(g_raw.get("api_gateway_port", workbench_port + 1))
        enterprise_database_url = (
            os.environ.get("HASHI_ENTERPRISE_DATABASE_URL")
            or g_raw.get("enterprise_database_url")
            or None
        )
        enterprise_scheduler_lease_enabled = _truthy(
            os.environ.get(
                "HASHI_ENTERPRISE_SCHEDULER_LEASE_ENABLED",
                g_raw.get("enterprise_scheduler_lease_enabled", False),
            )
        )
        enterprise_scheduler_lease_backend = str(
            os.environ.get(
                "HASHI_ENTERPRISE_SCHEDULER_LEASE_BACKEND",
                g_raw.get("enterprise_scheduler_lease_backend", "db"),
            )
            or "db"
        ).strip().lower()
        enterprise_scheduler_lease_name = str(
            os.environ.get(
                "HASHI_ENTERPRISE_SCHEDULER_LEASE_NAME",
                g_raw.get("enterprise_scheduler_lease_name", "superloop-scheduler"),
            )
            or "superloop-scheduler"
        )
        enterprise_scheduler_lease_holder = (
            os.environ.get("HASHI_ENTERPRISE_SCHEDULER_LEASE_HOLDER")
            or g_raw.get("enterprise_scheduler_lease_holder")
            or None
        )
        enterprise_scheduler_lease_ttl_seconds = max(
            1,
            int(
                os.environ.get(
                    "HASHI_ENTERPRISE_SCHEDULER_LEASE_TTL_SECONDS",
                    g_raw.get("enterprise_scheduler_lease_ttl_seconds", 60),
                )
                or 60
            ),
        )
        enterprise_scheduler_lease_kubernetes_namespace = (
            os.environ.get("HASHI_ENTERPRISE_SCHEDULER_LEASE_K8S_NAMESPACE")
            or os.environ.get("POD_NAMESPACE")
            or g_raw.get("enterprise_scheduler_lease_kubernetes_namespace")
            or None
        )
        enterprise_scheduler_lease_kubernetes_in_cluster = _truthy(
            os.environ.get(
                "HASHI_ENTERPRISE_SCHEDULER_LEASE_K8S_IN_CLUSTER",
                g_raw.get("enterprise_scheduler_lease_kubernetes_in_cluster", True),
            )
        )
        enterprise_scheduler_lease_kubeconfig_path = (
            os.environ.get("HASHI_ENTERPRISE_SCHEDULER_LEASE_KUBECONFIG")
            or g_raw.get("enterprise_scheduler_lease_kubeconfig_path")
            or None
        )
        enterprise_scheduler_lease_pool_enabled = _truthy(
            os.environ.get(
                "HASHI_ENTERPRISE_SCHEDULER_LEASE_POOL_ENABLED",
                g_raw.get("enterprise_scheduler_lease_pool_enabled", False),
            )
        )
        enterprise_scheduler_lease_pool_min_size = max(
            1,
            int(
                os.environ.get(
                    "HASHI_ENTERPRISE_SCHEDULER_LEASE_POOL_MIN_SIZE",
                    g_raw.get("enterprise_scheduler_lease_pool_min_size", 1),
                )
                or 1
            ),
        )
        enterprise_scheduler_lease_pool_max_size = max(
            enterprise_scheduler_lease_pool_min_size,
            int(
                os.environ.get(
                    "HASHI_ENTERPRISE_SCHEDULER_LEASE_POOL_MAX_SIZE",
                    g_raw.get("enterprise_scheduler_lease_pool_max_size", 4),
                )
                or 4
            ),
        )

        global_cfg = GlobalConfig(
            authorized_id=_auth_id,
            deployment_profile=profile_ctx.profile.value,
            organization_id=profile_ctx.organization_id,
            base_logs_dir=resolve_path_value(
                g_raw.get("base_logs_dir", "logs"),
                config_dir=config_dir,
                bridge_home=bridge_home,
            ) or (bridge_home / "logs"),
            base_media_dir=resolve_path_value(
                g_raw.get("base_media_dir", "media"),
                config_dir=config_dir,
                bridge_home=bridge_home,
            ) or (bridge_home / "media"),
            instance_id=g_raw.get("instance_id", "HASHI"),
            display_name=g_raw.get("display_name", "HASHI Instance"),
            api_host=g_raw.get("api_host", "127.0.0.1"),
            remote_port=int(g_raw.get("remote_port", DEFAULT_HASHI_REMOTE_PORT)),
            project_root=code_root,
            bridge_home=bridge_home,
            config_path=self.config_path,
            secrets_path=self.secrets_path,
            workbench_port=workbench_port,
            api_gateway_port=api_gateway_port,
            gemini_cmd=resolve_command_value(
                g_raw.get("gemini_cmd", "gemini"),
                config_dir=config_dir,
                bridge_home=bridge_home,
            ),
            claude_cmd=resolve_command_value(
                g_raw.get("claude_cmd", "claude"),
                config_dir=config_dir,
                bridge_home=bridge_home,
            ),
            codex_cmd=resolve_command_value(
                g_raw.get("codex_cmd", "codex"),
                config_dir=config_dir,
                bridge_home=bridge_home,
            ),
            grok_cmd=resolve_command_value(
                g_raw.get("grok_cmd", "grok"),
                config_dir=config_dir,
                bridge_home=bridge_home,
            ),
            gh_copilot_cmd=resolve_command_value(
                g_raw.get("gh_copilot_cmd", "gh copilot"),
                config_dir=config_dir,
                bridge_home=bridge_home,
            ),
            hermes_home=g_raw.get("hermes_home"),
            xai_api_base_url=g_raw.get("xai_api_base_url", "https://api.x.ai/v1"),
            xai_use_responses_api=_truthy(g_raw.get("xai_use_responses_api", False)),
            xai_oauth=dict(g_raw.get("xai_oauth") or {}),
            openrouter_url=g_raw.get("openrouter_url", "https://openrouter.ai/api/v1/chat/completions"),
            her_providers=dict(g_raw.get("her_providers") or g_raw.get("claw_providers") or {}),
            # Deprecated compatibility alias for pre-HER deployments.
            claw_providers=dict(g_raw.get("her_providers") or g_raw.get("claw_providers") or {}),
            enterprise_auth_providers=list(g_raw.get("enterprise_auth_providers") or []),
            enterprise_database_url=enterprise_database_url,
            enterprise_scheduler_lease_enabled=enterprise_scheduler_lease_enabled,
            enterprise_scheduler_lease_backend=enterprise_scheduler_lease_backend,
            enterprise_scheduler_lease_name=enterprise_scheduler_lease_name,
            enterprise_scheduler_lease_holder=enterprise_scheduler_lease_holder,
            enterprise_scheduler_lease_ttl_seconds=enterprise_scheduler_lease_ttl_seconds,
            enterprise_scheduler_lease_kubernetes_namespace=enterprise_scheduler_lease_kubernetes_namespace,
            enterprise_scheduler_lease_kubernetes_in_cluster=enterprise_scheduler_lease_kubernetes_in_cluster,
            enterprise_scheduler_lease_kubeconfig_path=enterprise_scheduler_lease_kubeconfig_path,
            enterprise_scheduler_lease_pool_enabled=enterprise_scheduler_lease_pool_enabled,
            enterprise_scheduler_lease_pool_min_size=enterprise_scheduler_lease_pool_min_size,
            enterprise_scheduler_lease_pool_max_size=enterprise_scheduler_lease_pool_max_size,
        )

        agents = []
        for agent_raw in raw_cfg.get("agents", []):
            if not agent_raw.get("is_active", True):
                continue

            a_raw = dict(agent_raw)
            agent_type = a_raw.pop("type", None)
            if agent_type is None:
                raise ValueError(
                    "Agent '%s' has no explicit type. Set type='flex' for active agents; "
                    "legacy fixed runtime no longer accepts accidental fallback."
                    % a_raw.get("name", "<unnamed>")
                )
            if agent_type not in {"flex", "limited"}:
                raise ValueError(
                    "Agent '%s' has unsupported type '%s'. Expected 'flex' or 'limited'."
                    % (a_raw.get("name", "<unnamed>"), agent_type)
                )
            name = a_raw.pop("name")
            workspace_dir = resolve_path_value(
                a_raw.pop("workspace_dir"),
                config_dir=config_dir,
                bridge_home=bridge_home,
            )
            system_md = resolve_path_value(
                a_raw.pop("system_md"),
                config_dir=config_dir,
                bridge_home=bridge_home,
            )
            telegram_token_key = a_raw.pop("telegram_token_key", name)
            allowed_backends = normalize_allowed_backends(a_raw.pop("allowed_backends"))
            active_backend = canonical_backend_engine(a_raw.pop("active_backend"))
            is_active = a_raw.pop("is_active", True)
            default_mode = str(a_raw.pop("default_mode", "flex") or "flex").strip().lower()
            if default_mode not in {"flex", "fixed"}:
                raise ValueError(
                    f"Agent '{name}' has unsupported default_mode '{default_mode}'. "
                    "Expected 'flex' or 'fixed'."
                )
            if default_mode == "fixed" and active_backend not in SESSION_MODE_BACKENDS:
                raise ValueError(
                    f"Agent '{name}' requests default_mode='fixed' with stateless backend "
                    f"'{active_backend}'. Use default_mode='flex'."
                )
            access_scope = a_raw.pop("access_scope", "project")
            if access_scope not in VALID_ACCESS_SCOPES:
                config_logger.warning(
                    f"Agent '{name}': invalid access_scope '{access_scope}', defaulting to 'workspace'"
                )
                access_scope = "workspace"

            extra = a_raw.pop("extra", None) or a_raw or None
            cfg = FlexibleAgentConfig(
                name=name, workspace_dir=workspace_dir, system_md=system_md,
                telegram_token_key=telegram_token_key, allowed_backends=allowed_backends,
                active_backend=active_backend, is_active=is_active, type=agent_type,
                default_mode=default_mode, access_scope=access_scope, extra=extra,
                project_root=code_root,
            )
            agents.append(cfg)

        return global_cfg, agents, secrets
