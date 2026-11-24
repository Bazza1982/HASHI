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

import re
from collections.abc import Mapping, Sequence

PCM_PROFILE_CLOUD = "cloud"
PCM_PROFILE_LOCAL = "local"
PCM_PROFILES = frozenset({PCM_PROFILE_CLOUD, PCM_PROFILE_LOCAL})

# Engines that are local inference by construction.  New local providers are
# added here; per-Provider override remains available through backend extra.
LOCAL_DEFAULT_ENGINES = frozenset({"hli-api", "ollama-api"})

# Context budget/selection constants for the Local profile.  These are
# optimization targets, never hard quotas that reject valid user tasks.
LOCAL_MEMORY_SEARCH_LIMIT = 3
LOCAL_HCC_PROJECTION_CHARS = 1200
LOCAL_MEMORY_PROJECTION_CHARS = 240
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
    provider_extra: Mapping[str, object] | None = None,
    her_v3_target_engine: object = None,
) -> str:
    """Resolve the effective PCM profile for one inference target.

    Precedence: explicit ``backend_extra.pcm_profile`` (agent/backend level),
    then explicit ``provider_extra.pcm_profile`` (the selected HERV3
    Provider/model configuration), then the engine default. A concrete
    non-HERV3 engine ignores ``her_v3_target_engine``.
    """
    if isinstance(backend_extra, Mapping):
        override = backend_extra.get("pcm_profile")
        if override not in (None, ""):
            return normalize_pcm_profile(override)
    if isinstance(provider_extra, Mapping):
        override = provider_extra.get("pcm_profile")
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


_QUERY_TOKEN_RE = re.compile(r"[a-z0-9_]{2,}")


def _query_terms(query: object) -> set[str]:
    return set(_QUERY_TOKEN_RE.findall(str(query or "").casefold()))


def lexical_unit_selection(
    units: Sequence[tuple[str, str]],
    query: object,
    max_chars: int,
    *,
    truncation_note: str = "",
) -> str:
    """Bounded, deterministic, lexical relevance selection.

    Units are ``(label, body)`` pairs. Each unit is scored by how many of the
    request's alphanumeric terms occur in its body; matching units are kept in
    document order until ``max_chars``. With no overlap the document head is
    kept instead, so the projection never becomes empty. No classifier,
    embedding, router call or new storage is introduced.
    """
    terms = _query_terms(query)
    scored: list[tuple[int, int, str, str]] = []
    for index, (label, body) in enumerate(units):
        body_words = set(_QUERY_TOKEN_RE.findall(str(body or "").casefold()))
        scored.append((len(body_words & terms), index, str(label or ""), str(body or "")))
    matches = [row for row in scored if row[0] > 0]
    chosen = matches if matches else scored
    chosen.sort(key=lambda row: row[1])
    parts: list[str] = []
    used = 0
    included = 0
    for _score, _index, label, body in chosen:
        block = (f"[{label}]\n" if label else "") + body.strip()
        if used + len(block) > max_chars and included:
            break
        parts.append(block)
        used += len(block)
        included += 1
    text = "\n\n".join(parts)
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"
    if truncation_note and (len(text) >= max_chars or included < len(chosen)):
        text += truncation_note
    return text


def project_memory_for_request(memory_text: object, query: object, max_chars: int) -> str:
    """Project the agent.md [memory] block by lexical relevance.

    The block is split into paragraph units; units sharing terms with the
    current request are kept first, in document order, within ``max_chars``.
    """
    units: list[tuple[str, str]] = []
    for paragraph in re.split(r"\n\s*\n", str(memory_text or "")):
        paragraph = paragraph.strip()
        if paragraph:
            units.append(("", paragraph))
    if not units:
        return ""
    return lexical_unit_selection(
        units,
        query,
        max_chars,
        truncation_note="\n…[projected by relevance; the rest remains in agent.md]",
    )
