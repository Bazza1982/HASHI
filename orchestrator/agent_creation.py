"""HASHI-owned agent creation service.

Converts a public creation intent (name / display_name / backend / preset /
model / effort / is_active) into the internal ``agents.json`` row.  HASHI is
the only authority for validation, backend resolution, HER v2 preset
construction, workspace creation, lifecycle identity and configuration
publication.  Workbench and other callers never build raw HASHI configuration.

The service performs pre-flight duplicate and workspace-collision checks,
then delegates to the existing ``ConfigAdmin.add_agent_to_config`` scaffold
so the revision-checked publication in ``orchestrator.config_json`` remains
the single write path.  On publication conflict it cleans up only resources
created by the current attempt and never touches pre-existing data.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestrator.config import default_agent_mode_for_backend
from orchestrator.config_admin import ConfigAdmin
from orchestrator.config_json import ConfigConflictError, ConfigDurabilityError
from orchestrator.flexible_backend_registry import (
    BACKEND_REGISTRY,
    CLAUDE_MODEL_ALIASES,
    HER_V2_ENGINE,
    REMOVED_ENGINE_IDS,
    apply_backend_policy_defaults,
    canonical_backend_engine,
    get_available_efforts,
    get_available_models,
    get_backend_entry,
    get_default_model,
    get_provider_reasoning_efforts,
    is_selectable_backend,
    normalize_effort,
)
from orchestrator.her_v2.runtime_configuration import (
    build_her_v2_provider_options,
)
from orchestrator.pathing import BridgePaths

logger = logging.getLogger("BridgeU.AgentCreation")

# Conservative V1 identifier rule.  Letters/digits/hyphen/underscore, no
# leading separator, at most 64 characters.  Names are workspace path
# components, so nothing that could traverse outside workspaces_root.
AGENT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

DEFAULT_HER_PRESET = "balanced"
HER_CREATION_PRESETS: tuple[dict[str, str], ...] = (
    {"id": "fast", "label": "Fast"},
    {"id": "balanced", "label": "Balanced"},
    {"id": "maximum", "label": "Maximum"},
)
HER_PRESET_IDS = frozenset(preset["id"] for preset in HER_CREATION_PRESETS)

# The five HER v2 role profiles a creation preset must produce.
HER_ROLE_ORDER: tuple[str, ...] = (
    "lightweight",
    "triage",
    "premium",
    "reviewer",
    "orchestrator",
)

# Preset -> role -> (model tier, reasoning tier).  Model tiers are resolved
# against the instance's configured HER providers, never hard-coded models.
_HER_PRESET_POLICY: dict[str, dict[str, tuple[str, str]]] = {
    "fast": {
        "lightweight": ("fast", "moderate"),
        "triage": ("fast", "moderate"),
        "premium": ("pro", "moderate"),
        "reviewer": ("pro", "high"),
        "orchestrator": ("pro", "high"),
    },
    "balanced": {
        "lightweight": ("fast", "moderate"),
        "triage": ("fast", "moderate"),
        "premium": ("pro", "high"),
        "reviewer": ("pro", "max"),
        "orchestrator": ("pro", "max"),
    },
    "maximum": {
        "lightweight": ("pro", "high"),
        "triage": ("pro", "high"),
        "premium": ("pro", "high"),
        "reviewer": ("pro", "max"),
        "orchestrator": ("pro", "max"),
    },
}

_REASONING_PREFERENCE: dict[str, tuple[str, ...]] = {
    "moderate": ("medium", "high", "low", "none", "off"),
    "high": ("high", "xhigh", "max", "medium"),
    "max": ("max",),
}


class AgentCreationError(Exception):
    """Base class for domain errors mapped to stable HTTP error codes."""

    error_code = "creation_failed"


class InvalidAgentNameError(AgentCreationError):
    error_code = "invalid_agent_name"


class InvalidDisplayNameError(AgentCreationError):
    error_code = "invalid_display_name"


class InvalidBackendError(AgentCreationError):
    error_code = "invalid_backend"


class InvalidModelError(AgentCreationError):
    error_code = "invalid_model"


class InvalidEffortError(AgentCreationError):
    error_code = "invalid_effort"


class InvalidHerPresetError(AgentCreationError):
    error_code = "invalid_her_preset"


class AgentExistsError(AgentCreationError):
    error_code = "agent_exists"


class WorkspaceExistsError(AgentCreationError):
    error_code = "workspace_exists"


class ConfigConflictCreationError(AgentCreationError):
    error_code = "config_conflict"


class CreationFailedError(AgentCreationError):
    error_code = "creation_failed"


@dataclass(frozen=True)
class AgentCreationSpec:
    """Public creation intent.  Never carries arbitrary agent_cfg fields."""

    name: str
    backend: str
    display_name: str | None = None
    preset: str | None = None
    model: str | None = None
    effort: str | None = None
    is_active: bool = False


@dataclass(frozen=True)
class AgentCreationResult:
    name: str
    display_name: str
    is_active: bool
    active_backend: str
    workspace_created: bool
    config_published: bool
    durability_warning: bool = False


def validate_agent_name(name: str) -> str:
    """Validate and return the canonical agent name; raise otherwise.

    The identifier is never silently sanitized into a different name.
    """
    if not isinstance(name, str):
        raise InvalidAgentNameError("agent name is required")
    if not AGENT_NAME_PATTERN.match(name):
        raise InvalidAgentNameError(
            "agent name may contain letters, numbers, hyphens and underscores "
            "only, must start with a letter or number, and be at most 64 "
            "characters"
        )
    return name


def _provider_profiles(global_config: Any) -> dict[str, dict[str, Any]]:
    raw = (
        global_config.get("her_providers")
        if isinstance(global_config, dict)
        else getattr(global_config, "her_providers", None)
    )
    if not isinstance(raw, dict):
        return {}
    providers = raw.get("providers")
    if not isinstance(providers, dict):
        return {}
    return {
        str(name).strip(): dict(profile)
        for name, profile in providers.items()
        if str(name).strip() and isinstance(profile, dict)
    }


def _pick_reasoning(engine: str, model: str | None, tier: str) -> str | None:
    """Pick a valid provider reasoning value for the selected engine/model."""
    efforts = [
        str(value).strip().lower()
        for value in get_provider_reasoning_efforts(engine, model)
        if str(value or "").strip()
    ]
    if not efforts:
        return None
    if tier == "max":
        return efforts[-1]
    for candidate in _REASONING_PREFERENCE.get(tier, ()):
        if candidate in efforts:
            return candidate
    return efforts[-1]


def _creation_seed(provider_profiles: dict[str, dict[str, Any]]) -> dict:
    """Build a minimal valid her_v2 seed for the provider-option builder.

    ``resolve_her_v2_configuration`` validates route model slots, so the
    seed carries all five role profiles with one placeholder engine/model.
    The seed only makes resolution pass; the option list itself is derived
    from the real provider profiles.  Mirrors ``_provider_engine`` naming
    conventions and never hard-codes provider/model names.
    """
    for raw_name, raw_profile in provider_profiles.items():
        name = str(raw_name).strip().lower()
        explicit = canonical_backend_engine(
            str(raw_profile.get("engine") or "").strip()
        )
        engine = explicit or (name if name.endswith("-api") else f"{name}-api")
        models = [
            str(model).strip()
            for model in (
                list(raw_profile.get("models") or [])
                + [
                    raw_profile.get("default_model"),
                    raw_profile.get("model"),
                    raw_profile.get("fast_model"),
                    raw_profile.get("pro_model"),
                ]
            )
            if isinstance(model, str)
            and model.strip()
            and model.strip().casefold() != "role-configured"
        ]
        if engine and models:
            return {
                "profiles": {
                    role: {"engine": engine, "model": models[0]}
                    for role in HER_ROLE_ORDER
                }
            }
    for seed_engine, seed_entry in BACKEND_REGISTRY.items():
        if seed_engine == HER_V2_ENGINE:
            continue
        seed_models = [
            model
            for model in list(seed_entry.get("models") or [])
            if str(model).casefold() != "role-configured"
        ]
        if seed_models:
            return {
                "profiles": {
                    role: {"engine": seed_engine, "model": seed_models[0]}
                    for role in HER_ROLE_ORDER
                }
            }
    raise CreationFailedError(
        "no HER v2 provider model configuration is available"
    )


def _tier_choice(
    provider_profiles: dict[str, dict[str, Any]],
) -> tuple[str, str, str]:
    """Resolve (engine, fast_model, pro_model) from configured providers.

    Prefer an available provider that advertises distinct fast/pro tiers;
    otherwise fall back to the first available provider using its default
    model for both tiers.  Deterministic: never hard-codes provider names.
    """
    if not provider_profiles:
        raise CreationFailedError(
            "no available HER v2 provider is configured on this instance"
        )
    options = build_her_v2_provider_options(
        [],
        provider_profiles,
        _creation_seed(provider_profiles),
    )
    available = [option for option in options if option.get("available")]
    if not available:
        raise CreationFailedError(
            "no available HER v2 provider is configured on this instance"
        )
    tiered = [
        option
        for option in available
        if option.get("fast_model")
        and option.get("pro_model")
        and option["fast_model"] != option["pro_model"]
    ]
    chosen = tiered[0] if tiered else available[0]
    engine = str(chosen.get("engine") or "").strip()
    fast_model = str(chosen.get("fast_model") or chosen.get("pro_model") or "").strip()
    pro_model = str(chosen.get("pro_model") or chosen.get("fast_model") or "").strip()
    if not engine or not fast_model or not pro_model:
        raise CreationFailedError(
            "no usable HER v2 provider model configuration is available"
        )
    return engine, fast_model, pro_model


def build_her_backend_row(
    preset: str,
    provider_profiles: dict[str, dict[str, Any]],
) -> dict:
    """Build the internal her-v2 backend row for one HASHI-owned preset.

    The row contains exactly the five required role profiles with
    engine/model/reasoning resolved from the instance provider configuration.
    No provider secrets or base URLs are embedded.
    """
    if preset not in HER_PRESET_IDS:
        raise InvalidHerPresetError(
            f"HER preset must be one of: {', '.join(sorted(HER_PRESET_IDS))}"
        )
    engine, fast_model, pro_model = _tier_choice(provider_profiles)
    policy = _HER_PRESET_POLICY[preset]
    profiles: dict[str, dict[str, str]] = {}
    for role in HER_ROLE_ORDER:
        tier, reasoning_tier = policy[role]
        model = pro_model if tier == "pro" else fast_model
        profile: dict[str, str] = {"engine": engine, "model": model}
        reasoning = _pick_reasoning(engine, model, reasoning_tier)
        if reasoning:
            profile["reasoning"] = reasoning
        profiles[role] = profile
    row = apply_backend_policy_defaults(
        {"engine": HER_V2_ENGINE, "model": "role-configured"}
    )
    row["her_v2"] = {
        "profiles": profiles,
        "audit_failure_terminal": "ERROR",
        "shadow_mode": False,
        "user_idle_timeout_s": 300,
    }
    return row


def build_ordinary_backend_row(backend: str, model: str | None, effort: str | None) -> dict:
    """Build an ordinary backend row validated against the registry."""
    entry = get_backend_entry(backend)
    models = list(entry.get("models") or [])
    custom_allowed = bool(entry.get("allow_custom_models"))

    resolved_model: str | None = None
    if model is not None and str(model).strip():
        requested = str(model).strip()
        if backend == "claude-cli":
            requested = CLAUDE_MODEL_ALIASES.get(requested.lower(), requested)
        if requested in models:
            resolved_model = requested
        elif custom_allowed:
            resolved_model = requested
        else:
            raise InvalidModelError(
                f"model {requested!r} is not available for backend {backend!r}"
            )
    else:
        resolved_model = get_default_model(backend)
        if resolved_model is None and not custom_allowed:
            raise InvalidModelError(f"backend {backend!r} has no default model")

    row: dict[str, Any] = {"engine": backend}
    if resolved_model:
        row["model"] = resolved_model

    if effort is not None and str(effort).strip():
        raw_effort = str(effort).strip().lower()
        efforts = [
            str(value).lower()
            for value in get_available_efforts(backend, resolved_model)
        ]
        normalized = normalize_effort(backend, raw_effort, resolved_model)
        normalized_lower = (
            str(normalized).strip().lower() if normalized is not None else ""
        )
        valid = (
            bool(efforts)
            and (raw_effort in efforts or raw_effort in {"extra", "extra_high"})
            and normalized_lower in efforts
        )
        if not valid:
            raise InvalidEffortError(
                f"effort {raw_effort!r} is not available for backend {backend!r}"
            )
        row["effort"] = normalized
    return row


def build_agent_config(spec: AgentCreationSpec, provider_profiles: dict) -> dict:
    """Convert a validated creation intent into the internal agent row."""
    backend = canonical_backend_engine(str(spec.backend or "").strip())
    if backend in REMOVED_ENGINE_IDS:
        raise InvalidBackendError(
            f"backend {spec.backend!r} has been removed; configure 'her-v2' instead"
        )
    if not is_selectable_backend(backend):
        raise InvalidBackendError(f"backend {spec.backend!r} is not selectable")
    if backend == HER_V2_ENGINE:
        preset = str(spec.preset or DEFAULT_HER_PRESET).strip().lower()
        backend_row = build_her_backend_row(preset, provider_profiles)
    else:
        backend_row = build_ordinary_backend_row(backend, spec.model, spec.effort)
    return {
        "name": spec.name,
        "display_name": spec.display_name,
        "type": "flex",
        "workspace_dir": f"workspaces/{spec.name}",
        "is_active": bool(spec.is_active),
        "active_backend": backend,
        "allowed_backends": [backend_row],
        "default_mode": default_agent_mode_for_backend(backend),
    }


class AgentCreationService:
    """Safe public creation path.  See module docstring for the contract."""

    def __init__(self, paths: BridgePaths, global_config: Any = None):
        self.paths = paths
        self.global_config = global_config
        self.admin = ConfigAdmin(paths)

    def _name_collides(self, raw: dict, name: str) -> bool:
        existing = [
            str(agent.get("name") or "")
            for agent in raw.get("agents", [])
            if isinstance(agent, dict)
        ]
        if os.name == "nt":
            folded = {value.casefold() for value in existing}
            return name.casefold() in folded
        return name in existing

    def _agent_row_exists(self, name: str) -> bool:
        try:
            raw = self.admin.load_raw_config()
        except Exception:
            return False
        return self._name_collides(raw, name)

    def _rollback_created_scaffold(
        self, ws_dir: Path, agent_md_preexisted: bool
    ) -> None:
        """Remove only resources created by the current attempt."""
        try:
            agent_md = ws_dir / "agent.md"
            if not agent_md_preexisted and agent_md.exists():
                agent_md.unlink()
            if ws_dir.exists():
                ws_dir.rmdir()  # only succeeds while empty
        except OSError as exc:
            logger.warning(
                "agent_create.rollback incomplete workspace=%s error=%s",
                ws_dir.name,
                type(exc).__name__,
            )

    def create(self, spec: AgentCreationSpec) -> AgentCreationResult:
        started = time.monotonic()
        name = validate_agent_name(spec.name)
        backend = canonical_backend_engine(str(spec.backend or "").strip())
        display_name = str(spec.display_name or "").strip() or name
        if len(display_name) > 160:
            raise InvalidDisplayNameError(
                "display name must be at most 160 characters"
            )

        logger.info(
            "agent_create.requested agent_name=%s backend=%s preset=%s is_active=%s",
            name,
            backend,
            str(spec.preset or "") if backend == HER_V2_ENGINE else "",
            bool(spec.is_active),
        )

        workspaces_root = self.paths.workspaces_root.resolve()
        candidate = (workspaces_root / name).resolve()
        if candidate.parent != workspaces_root:
            raise InvalidAgentNameError(
                "agent name resolves outside the workspaces root"
            )
        raw = self.admin.load_raw_config()
        if self._name_collides(raw, name):
            raise AgentExistsError(f"agent '{name}' already exists")

        ws_dir = candidate
        if ws_dir.exists() or ws_dir.is_symlink():
            raise WorkspaceExistsError(
                f"workspace 'workspaces/{name}' already exists"
            )

        provider_profiles = _provider_profiles(self.global_config)
        agent_cfg = build_agent_config(
            AgentCreationSpec(
                name=name,
                backend=backend,
                display_name=display_name,
                preset=spec.preset,
                model=spec.model,
                effort=spec.effort,
                is_active=spec.is_active,
            ),
            provider_profiles,
        )
        logger.info("agent_create.config_built agent_name=%s", name)

        agent_md_preexisted = (ws_dir / "agent.md").exists()
        try:
            published = self.admin.add_agent_to_config(name, agent_cfg)
        except ConfigConflictError as exc:
            if self._agent_row_exists(name):
                raise ConfigConflictCreationError(
                    "configuration changed while the agent was being created; "
                    "an agent with this name now exists"
                ) from exc
            self._rollback_created_scaffold(ws_dir, agent_md_preexisted)
            logger.info(
                "agent_create.rollback agent_name=%s reason=config_conflict",
                name,
            )
            raise ConfigConflictCreationError(
                "configuration changed while the agent was being created; try again"
            ) from exc
        except ConfigDurabilityError as exc:
            # Publication already committed; never roll back blindly here.
            if self._agent_row_exists(name):
                logger.warning(
                    "agent_create.completed agent_name=%s durability=warning",
                    name,
                )
                return AgentCreationResult(
                    name=name,
                    display_name=display_name,
                    is_active=bool(spec.is_active),
                    active_backend=backend,
                    workspace_created=True,
                    config_published=True,
                    durability_warning=True,
                )
            raise CreationFailedError(
                "configuration publication could not be confirmed"
            ) from exc

        if not published:
            # Lost a duplicate race; the other request is authoritative.
            logger.info("agent_create.rejected agent_name=%s reason=agent_exists", name)
            raise AgentExistsError(f"agent '{name}' already exists")

        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.info(
            "agent_create.completed agent_name=%s backend=%s elapsed_ms=%s",
            name,
            backend,
            elapsed_ms,
        )
        return AgentCreationResult(
            name=name,
            display_name=display_name,
            is_active=bool(spec.is_active),
            active_backend=backend,
            workspace_created=True,
            config_published=True,
        )
