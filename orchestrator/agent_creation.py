"""HASHI-owned agent creation service.

Converts a public creation intent (name / display_name / backend / model /
effort / is_active) into the internal ``agents.json`` row.  HASHI is the only
authority for validation, backend resolution, HERV3 target construction,
workspace creation, lifecycle identity and configuration publication.
Workbench and other callers never build raw HASHI configuration.

The service performs pre-flight duplicate and workspace-collision checks,
then delegates to the existing ``ConfigAdmin.add_agent_to_config`` scaffold
so the revision-checked publication in ``orchestrator.config_json`` remains
the single write path.  On publication conflict it cleans up only resources
created by the current attempt and never touches pre-existing data.
"""

from __future__ import annotations

import logging
from copy import deepcopy
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
    public_backend_engine,
)
from orchestrator.her_v2.v3_config import (
    HERv3ModelTarget,
    build_v3_provider_options,
)
from orchestrator.pathing import BridgePaths

logger = logging.getLogger("BridgeU.AgentCreation")

# Conservative V1 identifier rule.  Letters/digits/hyphen/underscore, no
# leading separator, at most 64 characters.  Names are workspace path
# components, so nothing that could traverse outside workspaces_root.
AGENT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

DEFAULT_HER_EFFORT = "high"

class AgentCreationError(Exception):
    """Base class for domain errors mapped to stable HTTP error codes."""

    error_code = "creation_failed"


class CreationPolicyError(AgentCreationError):
    error_code = "creation_policy_invalid"


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
    model: str | None = None
    effort: str | None = None
    is_active: bool = False
    provider: str | None = None
    restricted: bool = False


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


def _tier_choice(
    provider_profiles: dict[str, dict[str, Any]],
) -> tuple[str, str, str]:
    """Resolve one default HERV3 Provider/model from instance configuration."""
    if not provider_profiles:
        raise CreationFailedError(
            "no available HERV3 Provider is configured on this instance"
        )
    seed: HERv3ModelTarget | None = None
    for name, profile in provider_profiles.items():
        engine = canonical_backend_engine(
            str(
                profile.get("engine")
                or (name if str(name).endswith("-api") else f"{name}-api")
            )
        )
        models = list(
            dict.fromkeys(
                str(value).strip()
                for value in [
                    *(profile.get("models") or []),
                    profile.get("pro_model"),
                    profile.get("default_model"),
                    profile.get("model"),
                    *get_available_models(engine),
                ]
                if str(value or "").strip()
                and str(value).strip().casefold() != "role-configured"
            )
        )
        if engine and models:
            seed = HERv3ModelTarget(engine, models[0])
            break
    if seed is None:
        raise CreationFailedError(
            "no usable HERV3 Provider model is configured on this instance"
        )
    options = build_v3_provider_options([], provider_profiles, seed)
    available = [option for option in options if option.get("available")]
    if not available:
        raise CreationFailedError(
            "no available HERV3 Provider is configured on this instance"
        )
    chosen = available[0]
    engine = str(chosen.get("engine") or "").strip()
    model = str(chosen.get("default_model") or "").strip()
    if not engine or not model:
        raise CreationFailedError(
            "no usable HERV3 Provider model is available"
        )
    return engine, model, model


def build_her_backend_row(
    effort: str,
    provider_profiles: dict[str, dict[str, Any]],
    model: str | None = None,
    provider: str | None = None,
) -> dict:
    """Build a HERV3 row with one Provider, model and reasoning effort."""

    engine, fast_model, pro_model = _tier_choice(provider_profiles)
    del fast_model
    seed = HERv3ModelTarget(engine, pro_model)
    options = build_v3_provider_options([], provider_profiles, seed)
    selected = seed
    requested_model = str(model or "").strip()
    requested_provider = canonical_backend_engine(provider) if provider else None
    if requested_provider:
        option = next((option for option in options if option["engine"] == requested_provider and option.get("available")), None)
        if option is None:
            raise InvalidBackendError("HERV3 Provider is not available")
        selected = HERv3ModelTarget(requested_provider, requested_model or option["default_model"])
    if requested_model:
        matches = [
            option
            for option in options
            if option.get("available") and requested_model in option.get("models", [])
            and (not requested_provider or option["engine"] == requested_provider)
        ]
        if not matches:
            raise InvalidModelError(
                f"model {requested_model!r} is not available from a configured HERV3 Provider"
            )
        selected = HERv3ModelTarget(str(matches[0]["engine"]), requested_model)

    choices = [
        str(value).strip().casefold()
        for value in get_provider_reasoning_efforts(
            selected.provider,
            selected.model,
        )
        if str(value or "").strip()
    ]
    requested_effort = str(effort or DEFAULT_HER_EFFORT).strip().casefold()
    requested_effort = {"extra": "xhigh", "extra_high": "xhigh"}.get(
        requested_effort,
        requested_effort,
    )
    if requested_effort in {"none", "zero"}:
        requested_effort = "off" if "off" in choices else "none"
    if choices and requested_effort not in choices:
        raise InvalidEffortError(
            f"HERV3 model effort must be one of: {', '.join(choices)}"
        )
    row = apply_backend_policy_defaults(
        {"engine": HER_V2_ENGINE, "model": selected.model}
    )
    if choices:
        row["effort"] = requested_effort
    row["her_v2"] = {
        "main": selected.to_dict(),
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


def build_agent_config(spec: AgentCreationSpec, provider_profiles: dict, *, creation_rows=None, catalogue=None, grant_mode="selected") -> dict:
    """Convert a validated creation intent into the internal agent row."""
    backend = canonical_backend_engine(str(spec.backend or "").strip())
    if backend in REMOVED_ENGINE_IDS:
        raise InvalidBackendError(
            f"backend {spec.backend!r} has been removed; configure 'her-v3' instead"
        )
    if not is_selectable_backend(backend):
        raise InvalidBackendError(f"backend {spec.backend!r} is not selectable")
    if creation_rows is not None:
        backend_row = _build_policy_backend_row(spec, creation_rows, catalogue or {})
    elif backend == HER_V2_ENGINE:
        backend_row = build_her_backend_row(
            str(spec.effort or DEFAULT_HER_EFFORT).strip(),
            provider_profiles,
            model=spec.model,
            provider=spec.provider,
        )
    else:
        backend_row = build_ordinary_backend_row(backend, spec.model, spec.effort)
    return {
        "name": spec.name,
        "display_name": spec.display_name,
        "type": "flex",
        "workspace_dir": f"workspaces/{spec.name}",
        "is_active": bool(spec.is_active),
        "active_backend": backend,
        "allowed_backends": (
            _granted_policy_rows(spec, creation_rows, catalogue or {}, backend_row)
            if creation_rows is not None and grant_mode == "template" and not spec.restricted
            else _selected_policy_rows(creation_rows or [], backend_row)
        ),
        "default_mode": default_agent_mode_for_backend(backend),
    }


def _selected_policy_rows(rows, selected):
    result = [selected]
    providers = (selected.get("her_v2") or {}).get("v3_provider_allowlist", [])
    result.extend(deepcopy(row) for row in rows if row["engine"] in providers and not is_selectable_backend(row["engine"]))
    return result


def _granted_policy_rows(spec, rows, catalogue, selected):
    result = []
    active = canonical_backend_engine(spec.backend)
    for row in rows:
        engine = row["engine"]
        entry = catalogue.get(public_backend_engine(engine))
        if not entry or not entry.get("available"):
            continue
        if engine == active:
            result.append(deepcopy(selected))
        elif engine == HER_V2_ENGINE:
            providers = entry["providers"]
            configured = (row.get("her_v2") or {}).get("main", {}).get("provider")
            provider = configured if providers.get(configured, {}).get("available") else next(key for key, value in providers.items() if value.get("available"))
            result.append(_build_policy_backend_row(AgentCreationSpec(
                name=spec.name, backend=engine, provider=provider,
            ), rows, catalogue))
        else:
            result.append(deepcopy(row))
    # Provider-only rows contain concrete model/effort policy, never top-level
    # Engine choices. Preserve those only for explicitly granted HERV3 Providers.
    granted_providers = set()
    for row in result:
        if row["engine"] == HER_V2_ENGINE:
            granted_providers.update(row["her_v2"]["v3_provider_allowlist"])
    result.extend(deepcopy(row) for row in rows if row["engine"] in granted_providers and not is_selectable_backend(row["engine"]))
    return result


def _build_policy_backend_row(spec, rows, catalogue):
    engine = canonical_backend_engine(spec.backend)
    entry = catalogue.get(public_backend_engine(engine))
    if not entry or not entry.get("available"):
        raise InvalidBackendError("backend is not allowed or available in the creation template")
    source = next(row for row in rows if row["engine"] == engine)
    result = deepcopy(source)
    if engine == HER_V2_ENGINE:
        providers = entry["providers"]
        default_provider = (result.get("her_v2") or {}).get("main", {}).get("provider")
        provider = canonical_backend_engine(spec.provider or default_provider)
        option = providers.get(provider)
        if not option or not option.get("available"):
            raise InvalidBackendError("HERV3 Provider is not allowed or available in the creation template")
        model = str(spec.model or option.get("default_model") or "").strip()
        if model not in option["models"]:
            raise InvalidModelError("model is not available from the selected HERV3 Provider")
        choices = option["model_efforts"].get(model, [])
        result["her_v2"]["main"] = {"provider": provider, "model": model}
        # Freeze the effective granted Provider set, including credentials and privacy.
        result["her_v2"]["v3_provider_allowlist"] = [key for key, value in providers.items() if value.get("available")]
    else:
        model = str(spec.model or entry.get("default_model") or "").strip()
        if engine == "claude-cli":
            model = CLAUDE_MODEL_ALIASES.get(model.casefold(), model)
        if model not in entry["models"] and not entry.get("allow_custom_models"):
            raise InvalidModelError("model is not allowed by the creation template")
        choices = entry["model_efforts"].get(model, entry.get("efforts") or [])
    result["model"] = model
    result["default_model"] = model
    requested = str(spec.effort or source.get("effort") or entry.get("default_effort") or "").casefold()
    requested = {"extra": "xhigh", "extra_high": "xhigh", "zero": "off"}.get(requested, requested)
    if requested == "none" and "off" in choices:
        requested = "off"
    if spec.effort and requested not in choices:
        raise InvalidEffortError("effort is not allowed for the selected model")
    if choices:
        result["effort"] = requested if requested in choices else ("high" if "high" in choices else choices[0])
    else:
        result.pop("effort", None)
    return result


class AgentCreationService:
    """Safe public creation path.  See module docstring for the contract."""

    def __init__(self, paths: BridgePaths, global_config: Any = None):
        self.paths = paths
        self.global_config = global_config
        self.admin = ConfigAdmin(paths)

    def _creation_view(self, raw, *, agent_name=""):
        from orchestrator.agent_creation_policy import (
            resolve_creation_policy, creation_availability, build_effective_backend_catalogue,
        )
        from orchestrator.config_json import read_config_json
        try:
            policy = resolve_creation_policy(raw)
        except (ValueError, TypeError, KeyError) as exc:
            raise CreationPolicyError(str(exc)) from exc
        profiles = _provider_profiles((raw.get("global") or {})) or _provider_profiles(self.global_config)
        rows = policy["rows"]
        if rows is None:
            from orchestrator.flexible_backend_registry import BACKEND_REGISTRY
            rows = [{"engine": engine} for engine in BACKEND_REGISTRY if is_selectable_backend(engine) and engine != HER_V2_ENGINE]
            if profiles:
                rows.append(build_her_backend_row(DEFAULT_HER_EFFORT, profiles))
        secrets = read_config_json(self.paths.secrets_path) if self.paths.secrets_path.exists() else {}
        global_values = dict(raw.get("global") or {})
        if self.global_config is not None:
            base = dict(self.global_config) if isinstance(self.global_config, dict) else vars(self.global_config)
            global_values = {**base, **global_values}
        try:
            availability = creation_availability(global_values, rows, profiles, secrets, agent_name=agent_name)
            backends = build_effective_backend_catalogue(rows, profiles, availability=availability)
        except (ValueError, TypeError, KeyError) as exc:
            raise CreationPolicyError("creation template model policy is invalid") from exc
        view = {key: value for key, value in policy.items() if key != "rows"}
        view.update(ok=True, backends=backends,
                    allowed_backends=[engine for engine, entry in backends.items() if entry.get("available")])
        return policy, profiles, view

    def creation_catalogue(self):
        try:
            return self._creation_view(self.admin.load_raw_config())[2]
        except AgentCreationError as exc:
            return {"ok": False, "error_code": exc.error_code, "backends": {}, "allowed_backends": []}

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
            "agent_create.requested agent_name=%s backend=%s effort=%s is_active=%s",
            name,
            backend,
            (
                str(spec.effort or DEFAULT_HER_EFFORT)
                if backend == HER_V2_ENGINE
                else ""
            ),
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

        policy, provider_profiles, view = self._creation_view(raw, agent_name=name)
        available_entry = view["backends"].get(public_backend_engine(backend))
        if not available_entry or not available_entry.get("available"):
            raise InvalidBackendError("backend is not installed, allowed, or available for creation")
        agent_cfg = build_agent_config(
            AgentCreationSpec(
                name=name,
                backend=backend,
                display_name=display_name,
                model=spec.model,
                effort=spec.effort,
                is_active=spec.is_active,
                provider=spec.provider,
                restricted=spec.restricted,
            ),
            provider_profiles,
            creation_rows=policy["rows"],
            catalogue=view["backends"],
            grant_mode=policy["grant_mode"],
        )
        logger.info("agent_create.config_built agent_name=%s", name)

        agent_md_preexisted = (ws_dir / "agent.md").exists()
        try:
            published = self.admin.add_agent_to_config(name, agent_cfg, raw_config=raw)
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
                    active_backend=public_backend_engine(backend),
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
            active_backend=public_backend_engine(backend),
            workspace_created=True,
            config_published=True,
        )
