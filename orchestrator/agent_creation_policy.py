"""PAO-owned creation policy and mechanically derived effective backend view.

The instance explicitly references one template. Only selection policy crosses
that boundary; tool grants, credentials, identity and filesystem permissions do
not. Public catalogues never confer authorization.
"""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from collections.abc import Mapping

from orchestrator.flexible_backend_registry import (
    BACKEND_REGISTRY, HER_V2_ENGINE, canonical_backend_engine,
    public_backend_engine, get_backend_entry, get_available_models,
    get_secret_lookup_order, is_selectable_backend,
)
from orchestrator.runtime_effort_options import get_available_efforts, get_available_models as effective_models
from orchestrator.her_v2.v3_config import (
    HERv3ModelTarget, build_v3_provider_options, resolve_v3_target,
)
from orchestrator.privacy_levels import (
    require_backend_compatibility, require_herv3_provider_compatibility,
)

SELECTION_FIELDS = frozenset({
    "engine", "model", "default_model", "models", "available_models", "effort", "model_efforts",
})


def selection_row(row):
    result = {key: deepcopy(value) for key, value in row.items() if key in SELECTION_FIELDS}
    result["engine"] = canonical_backend_engine(row.get("engine"))
    if result["engine"] == HER_V2_ENGINE:
        raw = row.get("her_v2") or {}
        target = resolve_v3_target(raw)
        result["her_v2"] = {"main": target.to_dict()}
        if "v3_provider_allowlist" in raw:
            result["her_v2"]["v3_provider_allowlist"] = deepcopy(raw["v3_provider_allowlist"])
    return result


def resolve_creation_policy(raw):
    policy = (raw.get("global") or {}).get("agent_creation")
    if policy is None:
        return {"source": "legacy_selected", "grant_mode": "selected", "template_agent": None, "rows": None}
    if not isinstance(policy, Mapping) or set(policy) - {"template_agent", "grant_mode"}:
        raise ValueError("creation template policy is invalid")
    template_name = str(policy.get("template_agent") or "").strip()
    mode = policy.get("grant_mode", "template")
    template = next((row for row in raw.get("agents", []) if row.get("name") == template_name), None)
    if not template_name or mode not in {"template", "selected"} or not template:
        raise ValueError("creation template does not exist or has an invalid grant mode")
    rows = template.get("allowed_backends")
    if not isinstance(rows, list) or not rows:
        raise ValueError("creation template has no allowed backends")
    return {"source": "instance_template", "grant_mode": mode,
            "template_agent": template_name, "rows": [selection_row(row) for row in rows]}


def build_effective_backend_catalogue(rows, profiles, *, availability=None, privacy_level=1):
    """Project allowed selections; unavailable rows retain bounded reason codes."""
    availability = availability or {}
    backends = {}
    for row in rows:
        engine = canonical_backend_engine(row.get("engine"))
        if not is_selectable_backend(engine):
            continue
        registry = get_backend_entry(engine)
        ok, reason = availability.get(engine, (True, None))
        try:
            require_backend_compatibility(engine, privacy_level)
        except ValueError:
            ok, reason = False, "privacy_incompatible"
        entry = {"engine": public_backend_engine(engine), "label": registry.get("label", engine),
                 "available": ok, "reason": reason if not ok else None,
                 "privacy_levels": list(registry.get("privacy_levels") or []),
                 "allow_custom_models": bool(registry.get("allow_custom_models")) and any(
                     "available_models" not in grant for grant in rows
                     if canonical_backend_engine(grant.get("engine")) == engine
                 )}
        if engine == HER_V2_ENGINE:
            target = resolve_v3_target(row.get("her_v2") or {})
            options = build_v3_provider_options(rows, profiles, target,
                (row.get("her_v2") or {}).get("v3_provider_allowlist"))
            providers = {}
            for option in options:
                provider = option["engine"]
                provider_ok, provider_reason = availability.get(provider, (True, None))
                try:
                    require_herv3_provider_compatibility(provider, privacy_level)
                except ValueError:
                    provider_ok, provider_reason = False, "privacy_incompatible"
                available = bool(option["available"] and provider_ok)
                providers[provider] = {**option, "available": available,
                    "reason": None if available else provider_reason or "provider_unavailable",
                    "model_efforts": {model: get_available_efforts(provider, model, allowed_backends=rows, provider=True)
                                      for model in option["models"]}}
            entry.update(models=[], model_efforts={}, default_model=None, default_effort=None,
                         providers=providers, creation={"mode": "provider_model"})
            if not any(value["available"] for value in providers.values()):
                entry.update(available=False, reason="no_available_provider")
        else:
            models = effective_models(engine, allowed_backends=rows)
            default = row.get("model") or row.get("default_model") or registry.get("default_model")
            if default and default not in models:
                default = next(iter(models), None)
            entry.update(models=models, default_model=default, efforts=list(registry.get("efforts") or []),
                         default_effort=row.get("effort") or registry.get("default_effort"),
                         model_efforts={model: get_available_efforts(engine, model, allowed_backends=rows)
                                        for model in models}, creation={"mode": "model"})
        backends[entry["engine"]] = entry
    return backends


def creation_availability(global_config, rows, profiles, secrets, *, agent_name=""):
    """Reuse installation/OAuth preflight; validate API credential references."""
    from orchestrator.backend_preflight import BackendPreflight
    attrs = dict(global_config) if isinstance(global_config, Mapping) else vars(global_config or SimpleNamespace())
    cfg = SimpleNamespace(claude_cmd="claude", codex_cmd="codex")
    cfg.__dict__.update(attrs)
    candidate = SimpleNamespace(name=agent_name, active_backend="", allowed_backends=rows)
    preflight = BackendPreflight().check_backend_availability(cfg, [candidate], secrets)
    status = {engine: (ok, None if ok else "backend_not_installed" if engine.endswith("-cli") else "credential_missing")
              for engine, (ok, _) in preflight.items()}
    for name, profile in profiles.items():
        engine = canonical_backend_engine(profile.get("engine") or (name if name.endswith("-api") else name + "-api"))
        reference = profile.get("secret")
        keys = get_secret_lookup_order(engine, agent_name)
        credential_required = bool(reference) or bool(keys)
        credential_present = bool(secrets.get(reference)) if reference else any(secrets.get(key) for key in keys)
        if credential_required and not credential_present:
            status[engine] = (False, "credential_missing")
        elif profile.get("status") == "disabled":
            status[engine] = (False, "provider_disabled")
        else:
            status.setdefault(engine, (True, None))
    return status


def runtime_backend_catalogue(runtime):
    """Read current Agent permissions without constructing another state owner."""
    from orchestrator.agent_creation import _provider_profiles
    manager = runtime.backend_manager
    profiles = _provider_profiles(runtime.global_config)
    rows = runtime.config.allowed_backends
    availability = creation_availability(runtime.global_config, rows, profiles,
        getattr(manager, "secrets", {}), agent_name=runtime.name)
    try:
        backends = build_effective_backend_catalogue(rows, profiles, availability=availability,
            privacy_level=int(getattr(manager, "privacy_level", 1)))
        her = backends.get("her-v3")
        if her:
            # Current runtime Provider choices also include its persisted target,
            # configured allowlist and dynamic privacy gate.
            live_options = {option["engine"]: option for option in manager.get_her_v3_provider_options()}
            for engine, option in her["providers"].items():
                live = live_options.get(engine)
                if not live or not live.get("available"):
                    option.update(available=False, reason="provider_unavailable")
        return {"ok": True, "backends": backends}
    except (ValueError, TypeError, KeyError):
        return {"ok": False, "error_code": "backend_catalogue_unavailable", "backends": {}}
