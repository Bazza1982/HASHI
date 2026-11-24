"""PCM projection profile: cloud | local.

``pcm_profile`` is a context-projection preference, not a new Agent Working
Mode.  It decides how much HASHI-generated and retrieved context is injected
into each model request:

- ``cloud``  — generous but not wasteful; retains full permanent memory, HCC,
  catalogues and richer history when useful.
- ``local``  — the minimum necessary effective context for small local models;
  relies on native tool schemas, on-demand tool discovery and explicit
  retrieval for the rest.

Profile ownership follows the existing Provider/model configuration:

- An explicit ``pcm_profile`` value in the backend ``extra`` configuration
  always wins.
- HLI and Ollama default to ``local``.
- Known cloud providers default to ``cloud``.
- HERV3 (``her-v2``) resolves the profile from the *actual* inference target:
  when the active HERV3 route serves an HLI/Ollama model the profile is
  ``local``, otherwise ``cloud``.

Local/cloud status is never inferred from model names, parameter counts,
endpoint addresses or API keys.
"""
from __future__ import annotations

from collections.abc import Mapping

PCM_PROFILE_CLOUD = "cloud"
PCM_PROFILE_LOCAL = "local"
PCM_PROFILES = frozenset({PCM_PROFILE_CLOUD, PCM_PROFILE_LOCAL})

# Engines that are local inference by construction.  New local providers are
# added here; per-Provider override remains available through backend extra.
LOCAL_DEFAULT_ENGINES = frozenset({"hli-api", "ollama-api"})

# Context budget/selection constants for the Local profile.  These are
# optimization targets, never hard quotas that reject valid user tasks.
LOCAL_MAX_RECENT_EXCHANGES = 4
LOCAL_MEMORY_SEARCH_LIMIT = 3
LOCAL_HCC_PROJECTION_CHARS = 1200
LOCAL_PROMPT_TOKEN_BUDGET = 16_000


def normalize_pcm_profile(value: object) -> str:
    """Return a valid profile name or raise for an unsupported value."""
    raw = str(value or "").strip().casefold()
    if raw in {"", PCM_PROFILE_CLOUD}:
        return PCM_PROFILE_CLOUD
    if raw == PCM_PROFILE_LOCAL:
        return PCM_PROFILE_LOCAL
    raise ValueError(
        f"Unsupported pcm_profile {value!r}; expected 'cloud' or 'local'."
    )


def resolve_pcm_profile(
    engine: object,
    *,
    backend_extra: Mapping[str, object] | None = None,
    her_v3_target_engine: object = None,
) -> str:
    """Resolve the effective PCM profile for one inference target.

    Explicit ``pcm_profile`` in backend ``extra`` overrides every default.
    ``her_v3_target_engine`` supplies the HERV3 provider engine actually
    serving the current route (for model/provider switching); it is ignored
    when a concrete non-HERV3 engine is given.
    """
    if isinstance(backend_extra, Mapping):
        override = backend_extra.get("pcm_profile")
        if override not in (None, ""):
            return normalize_pcm_profile(override)

    target = str(her_v3_target_engine or engine or "").strip()
    if not target:
        return PCM_PROFILE_CLOUD
    canonical = str(target).strip().lower()
    if canonical == "her-v2" and str(her_v3_target_engine or "").strip():
        canonical = str(her_v3_target_engine).strip().lower()
    return (
        PCM_PROFILE_LOCAL
        if canonical in LOCAL_DEFAULT_ENGINES
        else PCM_PROFILE_CLOUD
    )
