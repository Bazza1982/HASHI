"""Function-layer views of effective model and effort choices.

The qualified registry owns compatibility defaults. Native catalogue
observations and instance opt-ins derive views without mutating that registry.
"""

from collections.abc import Mapping

from adapters.codex_models import native_model_catalogue
from orchestrator.flexible_backend_registry import (
    HER_V2_ENGINE,
    canonical_backend_engine,
    allows_custom_models,
    get_available_efforts as registry_efforts,
    get_available_models as registry_models,
    get_backend_entry,
    get_provider_reasoning_efforts as registry_provider_efforts,
    normalize_effort as registry_normalize,
)


def get_available_models(engine: str, *, backend: Mapping | None = None) -> list[str]:
    """Grant all known models by default; only an explicit allow-list narrows it.

    Selecting ``model`` or adding ``models`` is not a restriction. An optional
    ``available_models`` list in allowed_backends is an explicit restriction,
    including an empty list. Public projections are never written back to it.
    """
    engine = canonical_backend_engine(engine)
    models = registry_models(engine)
    if engine == "codex-cli":
        models.extend(model for model in native_model_catalogue() if model not in models)
    if backend is not None:
        configured = backend.get("models")
        configured = list(configured) if isinstance(configured, list) else []
        configured.extend([backend.get("model"), backend.get("default_model")])
        for raw in configured:
            model = str(raw or "").strip()
            if model and model not in models:
                models.append(model)
        if "available_models" in backend:
            allowed = backend["available_models"]
            if not isinstance(allowed, list) or any(
                not isinstance(model, str) or not model.strip() for model in allowed
            ):
                raise ValueError("available_models must be a list of model strings")
            allowed = {value.strip() for value in allowed}
            models = [model for model in models if model in allowed]
    return models


def model_is_allowed(engine: str, model: str, *, backend: Mapping | None = None) -> bool:
    if model in get_available_models(engine, backend=backend):
        return True
    return (
        (backend is None or "available_models" not in backend)
        and allows_custom_models(canonical_backend_engine(engine))
    )


def backend_model_view(backend: Mapping) -> dict:
    """Derive public choices from the same owner as command validation."""
    row = dict(backend)
    engine = canonical_backend_engine(row.get("engine"))
    models = get_available_models(engine, backend=backend)
    row["models"] = models
    row["available_models"] = models
    row["model_efforts"] = {
        model: get_available_efforts(engine, model, allowed_backends=[backend])
        for model in models
    }
    return row


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
    if engine == "codex-cli" and not provider:
        native = native_model_catalogue().get(model)
        if native is not None:
            return list(native)
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
