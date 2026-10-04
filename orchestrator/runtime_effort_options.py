"""Function-layer views of instance effort overrides and registry defaults.

The protected registry owns shared capabilities. Instance model opt-ins stay in
allowed_backends and are resolved without changing that process-wide catalogue.
"""

from collections.abc import Mapping

from orchestrator.flexible_backend_registry import (
    HER_V2_ENGINE,
    canonical_backend_engine,
    get_available_efforts as registry_efforts,
    get_backend_entry,
    get_available_models as registry_models,
    get_provider_reasoning_efforts as registry_provider_efforts,
    normalize_effort as registry_normalize,
)


def configured_model_efforts(backend: Mapping, model: str | None) -> list[str] | None:
    configured = backend.get("model_efforts") or {}
    if not isinstance(configured, Mapping) or model not in configured:
        return None
    raw = configured[model]
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)) or any(
        not isinstance(value, str) or not value.strip() for value in raw
    ):
        raise ValueError(f"model_efforts for '{model}' must contain effort strings")
    return list(dict.fromkeys(value.strip().casefold() for value in raw))


def get_available_efforts(engine, model=None, *, allowed_backends=(), provider=False):
    engine = canonical_backend_engine(engine)
    # HERV3 uses Provider/model reasoning levels; instance opt-ins still belong
    # to the selected concrete Provider rather than the compatibility Engine ID.
    if engine != HER_V2_ENGINE:
        candidates = []
        for backend in allowed_backends:
            owner = canonical_backend_engine(backend.get("engine"))
            if owner == engine or (
                engine == "hashi-api" and get_backend_entry(owner).get("gateway_enabled")
            ):
                efforts = configured_model_efforts(backend, model)
                if efforts is not None:
                    candidates.append(efforts)
        if candidates:
            if any(set(values) != set(candidates[0]) for values in candidates[1:]):
                raise ValueError(f"conflicting model_efforts for '{model}'")
            return list(candidates[0])
    fallback = registry_provider_efforts if provider else registry_efforts
    return fallback(engine, model)


def normalize_effort(engine, effort, model=None, *, allowed_backends=()):
    choices = get_available_efforts(engine, model, allowed_backends=allowed_backends)
    normalized = str(effort or "").strip().casefold()
    normalized = {"extra": "xhigh", "extra_high": "xhigh"}.get(normalized, normalized)
    if normalized in choices:
        return normalized
    default = registry_normalize(engine, effort, model)
    return default if default in choices else next(iter(choices), None)


def get_available_models(engine, *, allowed_backends=()):
    """One Function-side model view: compatibility baseline plus Agent opt-ins."""
    engine = canonical_backend_engine(engine)
    models = registry_models(engine)
    for backend in allowed_backends:
        if canonical_backend_engine(backend.get("engine")) != engine:
            continue
        configured = backend.get("models")
        values = list(configured) if isinstance(configured, list) else []
        values.extend([backend.get("model"), backend.get("default_model")])
        for value in values:
            model = str(value or "").strip()
            if model and model not in models:
                models.append(model)
    return models
