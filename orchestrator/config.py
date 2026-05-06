from __future__ import annotations
import json
import logging
import os
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, Any, List, Union

from orchestrator.pathing import resolve_command_value, resolve_path_value

# Valid access_scope values:
#   "workspace" - only the agent's workspace_dir (most restrictive)
#   "project"   - the project root / repo root (sensible default)
#   "drive"     - full drive root e.g. C:\ (least restrictive)
VALID_ACCESS_SCOPES = {"workspace", "project", "drive"}
LEGACY_FIXED_RUNTIME_ENV = "HASHI_ENABLE_LEGACY_FIXED_RUNTIME"
config_logger = logging.getLogger("BridgeU.Config")


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
    base_logs_dir: Path
    base_media_dir: Path
    instance_id: str = "HASHI"
    display_name: str = "HASHI Instance"
    api_host: str = "127.0.0.1"
    remote_port: int = 8766
    project_root: Path = None
    bridge_home: Path = None
    config_path: Path = None
    secrets_path: Path = None
    workbench_port: int = 18800
    api_gateway_port: int = 18801
    gemini_cmd: str = "gemini"
    claude_cmd: str = "claude"
    codex_cmd: str = "codex"
    gh_copilot_cmd: str = "gh copilot"
    openrouter_url: str = "https://openrouter.ai/api/v1/chat/completions"

@dataclass
class AgentConfig:
    name: str
    engine: str
    workspace_dir: Path
    system_md: Path
    model: str
    is_active: bool
    resume_policy: str = "latest"
    type: str = "fixed"
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
    access_scope: str = "project"
    extra: Dict[str, Any] = None
    project_root: Path = field(default=None, repr=False)

    def resolve_access_root(self) -> Path:
        return resolve_access_root(self.access_scope, self.workspace_dir, self.project_root)

AgentConfigType = Union[AgentConfig, FlexibleAgentConfig]

class ConfigManager:
    def __init__(self, config_path: Path, secrets_path: Path, bridge_home: Path | None = None):
        self.config_path = config_path
        self.secrets_path = secrets_path
        self.bridge_home = bridge_home or config_path.parent

    def load(self) -> tuple[GlobalConfig, list[AgentConfigType], dict]:
        with open(self.config_path, "r", encoding="utf-8-sig") as f:
            raw_cfg = json.load(f)

        with open(self.secrets_path, "r", encoding="utf-8-sig") as f:
            secrets = json.load(f)

        g_raw = raw_cfg["global"]
        config_dir = self.config_path.parent
        code_root = Path(__file__).resolve().parent.parent
        bridge_home = self.bridge_home

        # authorized_id: secrets.json takes priority (written by Hashiko during
        # AI-driven onboarding); falls back to agents.json for manual setups.
        # Value of 0 means Telegram not yet configured (workbench-only mode).
        _auth_id = int(secrets.get("authorized_telegram_id", 0)) or int(g_raw.get("authorized_id", 0))

        workbench_port = int(g_raw.get("workbench_port", 18800))
        api_gateway_port = int(g_raw.get("api_gateway_port", workbench_port + 1))

        global_cfg = GlobalConfig(
            authorized_id=_auth_id,
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
            remote_port=int(g_raw.get("remote_port", 8766)),
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
            gh_copilot_cmd=resolve_command_value(
                g_raw.get("gh_copilot_cmd", "gh copilot"),
                config_dir=config_dir,
                bridge_home=bridge_home,
            ),
            openrouter_url=g_raw.get("openrouter_url", "https://openrouter.ai/api/v1/chat/completions")
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
            if agent_type not in {"flex", "limited", "fixed"}:
                raise ValueError(
                    "Agent '%s' has unsupported type '%s'. Expected 'flex', 'limited', or 'fixed'."
                    % (a_raw.get("name", "<unnamed>"), agent_type)
                )
            if agent_type == "fixed" and os.environ.get(LEGACY_FIXED_RUNTIME_ENV) != "1":
                raise ValueError(
                    "Agent '%s' requests retired legacy fixed runtime. Convert it to type='flex' "
                    "or set %s=1 for an explicit emergency legacy start."
                    % (a_raw.get("name", "<unnamed>"), LEGACY_FIXED_RUNTIME_ENV)
                )
            if agent_type == "fixed":
                config_logger.warning(
                    "Agent '%s' is using retired legacy fixed runtime because %s=1.",
                    a_raw.get("name", "<unnamed>"),
                    LEGACY_FIXED_RUNTIME_ENV,
                )

            if agent_type in {"flex", "limited"}:
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
                allowed_backends = a_raw.pop("allowed_backends")
                active_backend = a_raw.pop("active_backend")
                is_active = a_raw.pop("is_active", True)
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
                    access_scope=access_scope, extra=extra, project_root=code_root
                )
                agents.append(cfg)
            else:
                # Extract core fields, leave rest in extra
                name = a_raw.pop("name")
                engine = a_raw.pop("engine")
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
                model = a_raw.pop("model", "default")
                is_active = a_raw.pop("is_active", True)
                resume_policy = a_raw.pop("resume_policy", "latest")
                access_scope = a_raw.pop("access_scope", "project")
                if access_scope not in VALID_ACCESS_SCOPES:
                    config_logger.warning(
                        f"Agent '{name}': invalid access_scope '{access_scope}', defaulting to 'workspace'"
                    )
                    access_scope = "workspace"

                cfg = AgentConfig(
                    name=name, engine=engine, workspace_dir=workspace_dir,
                    system_md=system_md, model=model, is_active=is_active,
                    resume_policy=resume_policy, type="fixed",
                    access_scope=access_scope, extra=a_raw, project_root=code_root
                )
                agents.append(cfg)

        return global_cfg, agents, secrets
