"""Strict, cache-backed public model-pricing facts for PAO metering.

Only sources with a machine-readable exact model identifier and independently
documented currency/unit contract are enabled.  Usage finalization calls the
cache-only API; network refresh is reserved for model configuration, explicit
diagnostics, or an operator-requested prewarm.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import math
import os
import re
import ssl
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence
from urllib.parse import quote, urlsplit
from uuid import uuid4

CACHE_SCHEMA_VERSION = 3
ADAPTER_REVISION = "openrouter-model-api.v3"
SUCCESS_TTL = timedelta(hours=24)
NEGATIVE_TTL = timedelta(minutes=15)
MAX_RESPONSE_BYTES = 64 * 1024
CATALOG_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
CONNECT_TIMEOUT_SECONDS = 5.0
TOTAL_TIMEOUT_SECONDS = 15.0
LOCK_WAIT_SECONDS = 20.0
LOCK_STALE_SECONDS = 60.0

OPENROUTER_SCOPE = "openrouter_route"
OPENROUTER_ENGINE = "openrouter-api"
OPENROUTER_ENGINES = frozenset(
    {"openrouter", "openrouter-api", "openrouter_api"}
)
OPENROUTER_HOST = "openrouter.ai"
OPENROUTER_UNIT_SOURCE_URL = "https://openrouter.ai/openapi.json"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._:+-]*$")
_PLAIN_DECIMAL = re.compile(r"^(?:0|[0-9]+\.[0-9]+)$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SCHEDULE_GUARD = threading.Lock()
_SCHEDULED: set[str] = set()
_EVIDENCE_GUARD = threading.Lock()
_EVIDENCE_IN_FLIGHT: dict[str, "_EvidenceFetch"] = {}
_RECENT_EVIDENCE: dict[str, tuple[float, "HttpEvidence"]] = {}
_RECENT_EVIDENCE_SECONDS = 2.0
_REFRESH_GUARD = threading.Lock()
_REFRESH_LOCKS: dict[str, threading.Lock] = {}

# These are Provider namespaces, not model rows. New exact model names under a
# known public Provider therefore need no code change. A fully-qualified model
# ID always wins, and unknown/broker Engines use an exact unique catalogue
# basename rather than fuzzy or family-prefix matching.
ENGINE_VENDOR_NAMESPACES: dict[str, str] = {
    "codex-cli": "openai",
    "openai-api": "openai",
    "claude-cli": "anthropic",
    "anthropic-api": "anthropic",
    "gemini-cli": "google",
    "google-api": "google",
    "deepseek-api": "deepseek",
    "xai-api": "x-ai",
    "grok-cli": "x-ai",
}

EXACT_OPENROUTER_MODEL_MAPPINGS: dict[
    tuple[str, str], tuple[str, ...]
] = {}


class PricingSourceError(RuntimeError):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason


@dataclass(frozen=True)
class HttpEvidence:
    status: int
    content_type: str
    body: bytes
    fetched_at: datetime
    url: str


@dataclass
class _EvidenceFetch:
    """One process-local HTTP fetch shared by independent fact validators."""

    ready: threading.Event
    evidence: HttpEvidence | None = None
    error: BaseException | None = None


@dataclass(frozen=True)
class PricingFact:
    status: str
    scope: str
    engine: str
    requested_model_id: str
    canonical_model_id: str | None
    currency: str | None
    unit: str | None
    input_per_unit: float | None
    output_per_unit: float | None
    cache_read_per_unit: float | None
    cache_write_per_unit: float | None
    cache_write_tiers: tuple[tuple[str, float], ...]
    thinking_per_unit: float | None
    request_per_unit: float | None
    source_url: str | None
    unit_source_url: str | None
    source_kind: str | None
    fetched_at: str
    expires_at: str
    source_revision: str | None
    revision_kind: str | None
    evidence_sha256: str | None
    unknown_reason: str | None
    last_known_revision: str | None = None
    last_known_fetched_at: str | None = None
    source_engine: str | None = None
    source_model_id: str | None = None
    conditional_price_tiers: tuple[Mapping[str, Any], ...] = ()
    additional_per_unit: tuple[tuple[str, float], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["cache_write_tiers"] = [
            [name, rate] for name, rate in self.cache_write_tiers
        ]
        payload["conditional_price_tiers"] = [
            dict(item) for item in self.conditional_price_tiers
        ]
        payload["additional_per_unit"] = [
            [name, rate] for name, rate in self.additional_per_unit
        ]
        return payload

    @classmethod
    def from_dict(cls, value: Any) -> PricingFact:
        if not isinstance(value, dict):
            raise ValueError("pricing fact must be an object")
        fields = dict(value)
        raw_tiers = fields.get("cache_write_tiers") or []
        fields["cache_write_tiers"] = tuple(
            (str(item[0]), float(item[1]))
            for item in raw_tiers
            if isinstance(item, (list, tuple)) and len(item) == 2
        )
        fields["conditional_price_tiers"] = tuple(
            dict(item)
            for item in (fields.get("conditional_price_tiers") or [])
            if isinstance(item, dict)
        )
        fields["additional_per_unit"] = tuple(
            (str(item[0]), float(item[1]))
            for item in (fields.get("additional_per_unit") or [])
            if isinstance(item, (list, tuple)) and len(item) == 2
        )
        fact = cls(**fields)
        if fact.status not in {"known", "known_zero", "unknown"}:
            raise ValueError("invalid pricing fact status")
        return fact


def _utc(value: datetime | None = None) -> datetime:
    selected = value or datetime.now(timezone.utc)
    if selected.tzinfo is None:
        selected = selected.replace(tzinfo=timezone.utc)
    return selected.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat()


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return _utc(parsed)


def normalize_engine(engine: str) -> str:
    normalized = str(engine or "").strip().casefold().replace("_", "-")
    if normalized in {item.replace("_", "-") for item in OPENROUTER_ENGINES}:
        return OPENROUTER_ENGINE
    return {
        "codex": "codex-cli",
        "hashi": "hashi-api",
        "xai": "xai-api",
    }.get(normalized, normalized)


def source_scope(engine: str) -> str:
    return (
        OPENROUTER_SCOPE
        if normalize_engine(engine) == OPENROUTER_ENGINE
        else "openrouter_reference"
    )


def _refresh_lock(path: Path, key: str) -> threading.Lock:
    lock_key = f"{path}@{key}"
    with _REFRESH_GUARD:
        return _REFRESH_LOCKS.setdefault(lock_key, threading.Lock())


def _cache_key(engine: str, model: str) -> str:
    return json.dumps(
        [source_scope(engine), normalize_engine(engine), str(model or "").strip()],
        ensure_ascii=True,
        separators=(",", ":"),
    )


def default_cache_path() -> Path:
    explicit = str(os.environ.get("HASHI_PRICING_CACHE_FILE") or "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    bridge_home = str(
        os.environ.get("BRIDGE_HOME")
        or os.environ.get("HASHI_REMOTE_ROOT")
        or ""
    ).strip()
    root = Path(bridge_home).expanduser() if bridge_home else Path.cwd()
    return (root / "tmp" / "pricing-facts-v1.json").resolve()


def _selected_cache_path(cache_path: Path | str | None) -> Path:
    return (
        default_cache_path()
        if cache_path is None
        else Path(cache_path).expanduser().resolve()
    )


def _empty_cache() -> dict[str, Any]:
    return {
        "schema_version": CACHE_SCHEMA_VERSION,
        "adapter_revision": ADAPTER_REVISION,
        "facts": {},
    }


def _read_cache(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return _empty_cache()
    try:
        if path.stat().st_size > 2 * 1024 * 1024:
            return _empty_cache()
        from orchestrator.config_json import read_config_json

        payload = read_config_json(path)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return _empty_cache()
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != CACHE_SCHEMA_VERSION
        or payload.get("adapter_revision") != ADAPTER_REVISION
        or not isinstance(payload.get("facts"), dict)
    ):
        return _empty_cache()
    return payload


def _write_cache(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    from orchestrator.config_json import write_config_json

    write_config_json(path, payload)


@contextmanager
def _exclusive_cache_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    token = f"{os.getpid()}:{threading.get_ident()}:{uuid4().hex}"
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    descriptor: int | None = None
    while descriptor is None:
        try:
            descriptor = os.open(
                lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
            os.write(descriptor, token.encode("ascii"))
        except FileExistsError:
            try:
                stale = time.time() - lock_path.stat().st_mtime > LOCK_STALE_SECONDS
            except OSError:
                stale = False
            if stale:
                try:
                    lock_path.unlink()
                except OSError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise PricingSourceError("refresh_busy")
            time.sleep(0.025)
    try:
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            if lock_path.read_text(encoding="ascii") == token:
                lock_path.unlink()
        except OSError:
            pass


def _unknown_fact(
    engine: str,
    model: str,
    *,
    reason: str,
    now: datetime,
    previous: PricingFact | None = None,
    source_url: str | None = None,
    source_model_id: str | None = None,
) -> PricingFact:
    last_known = previous if previous and previous.status in {"known", "known_zero"} else None
    return PricingFact(
        status="unknown",
        scope=source_scope(engine),
        engine=normalize_engine(engine),
        requested_model_id=str(model or "").strip(),
        canonical_model_id=None,
        currency=None,
        unit=None,
        input_per_unit=None,
        output_per_unit=None,
        cache_read_per_unit=None,
        cache_write_per_unit=None,
        cache_write_tiers=(),
        thinking_per_unit=None,
        request_per_unit=None,
        source_url=source_url,
        unit_source_url=(OPENROUTER_UNIT_SOURCE_URL if source_model_id else None),
        source_kind=("openrouter_models_api" if source_model_id else None),
        fetched_at=_iso(now),
        expires_at=_iso(now + NEGATIVE_TTL),
        source_revision=None,
        revision_kind=None,
        evidence_sha256=None,
        unknown_reason=reason,
        last_known_revision=(last_known.source_revision if last_known else None),
        last_known_fetched_at=(last_known.fetched_at if last_known else None),
        source_engine=(OPENROUTER_ENGINE if source_model_id else None),
        source_model_id=source_model_id,
    )


def _fact_is_fresh(fact: PricingFact, now: datetime) -> bool:
    try:
        return _parse_time(fact.expires_at) > _utc(now)
    except (TypeError, ValueError):
        return False


def _safe_stored_rate(value: Any, *, required: bool) -> bool:
    if value is None:
        return not required
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and 0.0 <= float(value) <= 1_000.0
    )


def _valid_cached_fact(fact: PricingFact, engine: str, model: str) -> bool:
    normalized_engine = normalize_engine(engine)
    requested = str(model or "").strip()
    if (
        fact.scope != source_scope(engine)
        or fact.engine != normalized_engine
        or fact.requested_model_id != requested
    ):
        return False
    try:
        fetched_at = _parse_time(fact.fetched_at)
        expires_at = _parse_time(fact.expires_at)
    except (TypeError, ValueError):
        return False
    if expires_at <= fetched_at:
        return False

    rates = (
        (fact.input_per_unit, fact.status != "unknown"),
        (fact.output_per_unit, fact.status != "unknown"),
        (fact.cache_read_per_unit, False),
        (fact.cache_write_per_unit, False),
        (fact.thinking_per_unit, False),
        (fact.request_per_unit, False),
    )
    if any(not _safe_stored_rate(value, required=required) for value, required in rates):
        return False
    tier_names: set[str] = set()
    for name, value in fact.cache_write_tiers:
        if (
            name not in {"5m", "1h"}
            or name in tier_names
            or not _safe_stored_rate(value, required=True)
        ):
            return False
        tier_names.add(name)
    additional_names: set[str] = set()
    for name, value in fact.additional_per_unit:
        if (
            re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name) is None
            or name in additional_names
            or not _safe_stored_rate(value, required=True)
        ):
            return False
        additional_names.add(name)
    previous_minimum = -1
    tier_rate_fields = {
        "input_per_unit",
        "output_per_unit",
        "cache_read_per_unit",
        "cache_write_per_unit",
        "thinking_per_unit",
        "request_per_unit",
    }
    for tier in fact.conditional_price_tiers:
        if not isinstance(tier, Mapping):
            return False
        if set(tier) - (
            {"min_prompt_tokens", "additional_per_unit"} | tier_rate_fields
        ):
            return False
        minimum = tier.get("min_prompt_tokens")
        if (
            isinstance(minimum, bool)
            or not isinstance(minimum, int)
            or minimum < 0
            or minimum <= previous_minimum
        ):
            return False
        previous_minimum = minimum
        if not any(field in tier for field in tier_rate_fields) and not tier.get(
            "additional_per_unit"
        ):
            return False
        if any(
            not _safe_stored_rate(tier.get(field), required=True)
            for field in tier_rate_fields
            if field in tier
        ):
            return False
        tier_additional = tier.get("additional_per_unit", {})
        if not isinstance(tier_additional, Mapping) or any(
            re.fullmatch(r"[a-z][a-z0-9_]{0,63}", str(name)) is None
            or not _safe_stored_rate(value, required=True)
            for name, value in tier_additional.items()
        ):
            return False

    if fact.status == "unknown":
        has_source = bool(fact.source_model_id)
        expected_kind = "openrouter_models_api" if has_source else None
        expected_unit_url = OPENROUTER_UNIT_SOURCE_URL if has_source else None
        if (
            fact.canonical_model_id is not None
            or fact.currency is not None
            or fact.unit is not None
            or fact.input_per_unit is not None
            or fact.output_per_unit is not None
            or fact.cache_read_per_unit is not None
            or fact.cache_write_per_unit is not None
            or fact.cache_write_tiers
            or fact.conditional_price_tiers
            or fact.additional_per_unit
            or fact.thinking_per_unit is not None
            or fact.request_per_unit is not None
            or fact.source_kind != expected_kind
            or fact.unit_source_url != expected_unit_url
            or fact.source_engine
            != (OPENROUTER_ENGINE if has_source else None)
            or fact.source_revision is not None
            or fact.revision_kind is not None
            or fact.evidence_sha256 is not None
            or not str(fact.unknown_reason or "").strip()
        ):
            return False
        if has_source:
            try:
                if fact.source_url != _openrouter_url(
                    str(fact.source_model_id)
                ):
                    return False
            except PricingSourceError:
                return False
        elif fact.source_url is not None:
            return False
        if fact.last_known_revision is not None:
            if not fact.last_known_revision.startswith("openrouter:sha256:"):
                return False
            try:
                _parse_time(str(fact.last_known_fetched_at))
            except (TypeError, ValueError):
                return False
        elif fact.last_known_fetched_at is not None:
            return False
        return True

    evidence_hash = str(fact.evidence_sha256 or "")
    try:
        expected_url = _openrouter_url(str(fact.source_model_id or ""))
    except PricingSourceError:
        return False
    if (
        fact.source_engine != OPENROUTER_ENGINE
        or _MODEL_ID.fullmatch(str(fact.source_model_id or "")) is None
        or _MODEL_ID.fullmatch(str(fact.canonical_model_id or "")) is None
        or fact.currency != "USD"
        or fact.unit != "token"
        or fact.source_url != expected_url
        or fact.unit_source_url != OPENROUTER_UNIT_SOURCE_URL
        or fact.source_kind != "openrouter_models_api"
        or fact.revision_kind != "content_sha256"
        or _SHA256.fullmatch(evidence_hash) is None
        or fact.source_revision != f"openrouter:sha256:{evidence_hash}"
        or fact.unknown_reason is not None
        or fact.last_known_revision is not None
        or fact.last_known_fetched_at is not None
    ):
        return False
    modeled_rates = (
        fact.input_per_unit,
        fact.output_per_unit,
        fact.cache_read_per_unit,
        fact.cache_write_per_unit,
        fact.thinking_per_unit,
        fact.request_per_unit,
        *(value for _, value in fact.cache_write_tiers),
        *(value for _, value in fact.additional_per_unit),
        *(
            value
            for tier in fact.conditional_price_tiers
            for key, value in tier.items()
            if key not in {"min_prompt_tokens", "additional_per_unit"}
        ),
        *(
            value
            for tier in fact.conditional_price_tiers
            for value in dict(tier.get("additional_per_unit", {})).values()
        ),
    )
    all_zero = all(value in {None, 0, 0.0} for value in modeled_rates)
    if fact.status == "known_zero":
        return str(fact.source_model_id).endswith(":free") and all_zero
    return fact.status == "known" and not all_zero


def _cached_fact(
    payload: dict[str, Any], key: str, engine: str, model: str
) -> PricingFact | None:
    raw = (payload.get("facts") or {}).get(key)
    if raw is None:
        return None
    try:
        fact = PricingFact.from_dict(raw)
    except (TypeError, ValueError):
        return None
    return fact if _valid_cached_fact(fact, engine, model) else None


def get_cached_pricing_fact(
    engine: str,
    model: str,
    *,
    cache_path: Path | str | None = None,
    now: datetime | None = None,
) -> PricingFact:
    """Return a fresh cached fact without performing any network operation."""

    selected_now = _utc(now)
    payload = _read_cache(_selected_cache_path(cache_path))
    fact = _cached_fact(payload, _cache_key(engine, model), engine, model)
    if fact is None:
        return _unknown_fact(engine, model, reason="cache_miss", now=selected_now)
    if _fact_is_fresh(fact, selected_now):
        return fact
    stale = _unknown_fact(
        engine,
        model,
        reason="stale_cache",
        now=selected_now,
        previous=fact,
        source_url=fact.source_url,
        source_model_id=fact.source_model_id,
    )
    if fact.status == "unknown":
        stale = replace(
            stale,
            last_known_revision=fact.last_known_revision,
            last_known_fetched_at=fact.last_known_fetched_at,
        )
    return stale


def _openrouter_url(model: str) -> str:
    exact = str(model or "").strip()
    if _MODEL_ID.fullmatch(exact) is None:
        raise PricingSourceError("invalid_exact_model_id")
    author, slug = exact.split("/", 1)
    return (
        "https://openrouter.ai/api/v1/model/"
        + quote(author, safe="._-")
        + "/"
        + quote(slug, safe="._:+-")
    )


def _catalog_source_model_id(model: str, evidence: HttpEvidence) -> str:
    """Resolve only a unique exact catalogue identity from bounded evidence."""

    if evidence.url != OPENROUTER_MODELS_URL:
        raise PricingSourceError("source_url_mismatch")
    if len(evidence.body) > CATALOG_MAX_RESPONSE_BYTES:
        raise PricingSourceError("response_too_large")
    if evidence.status != 200:
        raise PricingSourceError(f"http_{evidence.status}")
    if evidence.content_type.split(";", 1)[0].strip().casefold() != "application/json":
        raise PricingSourceError("unsupported_content_type")
    try:
        payload = json.loads(evidence.body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PricingSourceError("malformed_json", str(exc)) from exc
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise PricingSourceError("invalid_model_catalogue")
    requested = str(model or "").strip().casefold()
    candidates: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        model_id = str(row.get("id") or "").strip()
        canonical = str(row.get("canonical_slug") or "").strip()
        aliases = row.get("aliases") or ()
        exact_names = {
            model_id.casefold(),
            canonical.casefold(),
            model_id.rsplit("/", 1)[-1].casefold(),
            canonical.rsplit("/", 1)[-1].casefold(),
        }
        if isinstance(aliases, list):
            exact_names.update(
                str(item or "").strip().casefold()
                for item in aliases
                if str(item or "").strip()
            )
        if requested in exact_names and _MODEL_ID.fullmatch(model_id):
            candidates.append(model_id)
    candidates = list(dict.fromkeys(candidates))
    if not candidates:
        raise PricingSourceError("source_not_qualified")
    if len(candidates) != 1:
        raise PricingSourceError("source_alias_ambiguous")
    return candidates[0]


def resolve_source_model_id(
    engine: str,
    model: str,
    *,
    mappings: Mapping[tuple[str, str], str | Sequence[str]] | None = None,
    catalogue_evidence: HttpEvidence | None = None,
) -> str:
    """Resolve one exact OpenRouter model ID without fuzzy family matching."""

    normalized_engine = normalize_engine(engine).replace("_", "-")
    requested = str(model or "").strip()
    if _MODEL_ID.fullmatch(requested):
        return requested

    selected_mappings = mappings or EXACT_OPENROUTER_MODEL_MAPPINGS
    raw = selected_mappings.get((normalized_engine, requested.casefold()))
    if isinstance(raw, str):
        candidates = (raw,)
    elif isinstance(raw, Sequence):
        candidates = tuple(
            str(item or "").strip() for item in raw if str(item or "").strip()
        )
    else:
        candidates = ()
    candidates = tuple(dict.fromkeys(candidates))
    if len(candidates) > 1:
        raise PricingSourceError("source_alias_ambiguous")
    if len(candidates) == 1:
        if _MODEL_ID.fullmatch(candidates[0]) is None:
            raise PricingSourceError("invalid_source_model_id")
        return candidates[0]

    namespace = ENGINE_VENDOR_NAMESPACES.get(normalized_engine)
    if namespace and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:+-]*", requested):
        return f"{namespace}/{requested}"
    if catalogue_evidence is not None:
        return _catalog_source_model_id(requested, catalogue_evidence)
    raise PricingSourceError("catalogue_required")


def bounded_https_get(url: str) -> HttpEvidence:
    """Fetch one allowlisted JSON resource with a hard streaming byte limit."""

    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != OPENROUTER_HOST
        or parsed.port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise PricingSourceError("source_not_allowlisted")
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    response_limit = (
        CATALOG_MAX_RESPONSE_BYTES
        if url == OPENROUTER_MODELS_URL
        else MAX_RESPONSE_BYTES
    )
    started = time.monotonic()
    connection = http.client.HTTPSConnection(
        OPENROUTER_HOST,
        timeout=CONNECT_TIMEOUT_SECONDS,
        context=ssl.create_default_context(),
    )
    try:
        connection.request(
            "GET",
            path,
            headers={
                "Accept": "application/json",
                "User-Agent": "HASHI-PAO-Pricing/1",
            },
        )
        response = connection.getresponse()
        content_type = str(response.getheader("Content-Type") or "")
        content_length = str(response.getheader("Content-Length") or "").strip()
        if content_length.isdigit() and int(content_length) > response_limit:
            raise PricingSourceError("response_too_large")
        body = bytearray()
        while True:
            remaining = TOTAL_TIMEOUT_SECONDS - (time.monotonic() - started)
            if remaining <= 0:
                raise PricingSourceError("timeout")
            if connection.sock is not None:
                connection.sock.settimeout(max(0.1, min(CONNECT_TIMEOUT_SECONDS, remaining)))
            chunk = response.read(min(8192, response_limit + 1 - len(body)))
            if not chunk:
                break
            body.extend(chunk)
            if len(body) > response_limit:
                raise PricingSourceError("response_too_large")
        return HttpEvidence(
            status=int(response.status),
            content_type=content_type,
            body=bytes(body),
            fetched_at=datetime.now(timezone.utc),
            url=url,
        )
    except TimeoutError as exc:
        raise PricingSourceError("timeout", str(exc)) from exc
    except OSError as exc:
        raise PricingSourceError("network_error", str(exc)) from exc
    finally:
        connection.close()


def shared_bounded_https_get(url: str) -> HttpEvidence:
    """Coalesce one allowlisted evidence fetch across metadata consumers.

    Pricing and capability discovery validate and persist the response
    independently.  Only the bounded raw HTTP evidence is shared, including a
    very short process-local reuse window so back-to-back prewarm threads do
    not make duplicate catalogue requests.
    """

    selected_url = str(url or "").strip()
    now = time.monotonic()
    with _EVIDENCE_GUARD:
        recent = _RECENT_EVIDENCE.get(selected_url)
        if recent is not None and now - recent[0] <= _RECENT_EVIDENCE_SECONDS:
            return recent[1]
        fetch = _EVIDENCE_IN_FLIGHT.get(selected_url)
        owner = fetch is None
        if owner:
            fetch = _EvidenceFetch(ready=threading.Event())
            _EVIDENCE_IN_FLIGHT[selected_url] = fetch

    assert fetch is not None
    if owner:
        try:
            fetch.evidence = bounded_https_get(selected_url)
        except BaseException as exc:
            fetch.error = exc
        finally:
            with _EVIDENCE_GUARD:
                if fetch.evidence is not None:
                    _RECENT_EVIDENCE[selected_url] = (
                        time.monotonic(),
                        fetch.evidence,
                    )
                    cutoff = time.monotonic() - _RECENT_EVIDENCE_SECONDS
                    for cached_url, (stored_at, _evidence) in tuple(
                        _RECENT_EVIDENCE.items()
                    ):
                        if stored_at < cutoff:
                            _RECENT_EVIDENCE.pop(cached_url, None)
                _EVIDENCE_IN_FLIGHT.pop(selected_url, None)
                fetch.ready.set()
    elif not fetch.ready.wait(TOTAL_TIMEOUT_SECONDS + CONNECT_TIMEOUT_SECONDS + 1):
        raise PricingSourceError("timeout")

    if fetch.error is not None:
        raise fetch.error
    if fetch.evidence is None:
        raise PricingSourceError("fetch_failed")
    return fetch.evidence


def _rate(value: Any, *, required: bool, field: str) -> float | None:
    if value is None and not required:
        return None
    raw = str(value) if not isinstance(value, bool) else ""
    if _PLAIN_DECIMAL.fullmatch(raw) is None:
        raise PricingSourceError("invalid_pricing_schema", f"invalid {field} rate")
    parsed = float(raw)
    if not math.isfinite(parsed) or parsed < 0 or parsed > 1_000:
        raise PricingSourceError("invalid_pricing_schema", f"unsafe {field} rate")
    return parsed


_OPENROUTER_RATE_FIELDS = {
    "prompt": "input_per_unit",
    "completion": "output_per_unit",
    "input_cache_read": "cache_read_per_unit",
    "input_cache_write": "cache_write_per_unit",
    "internal_reasoning": "thinking_per_unit",
    "request": "request_per_unit",
}
_OPENROUTER_CACHE_TIER_FIELDS = {
    "input_cache_write_5m",
    "input_cache_write_1h",
}


def _additional_rates(pricing: Mapping[str, Any]) -> tuple[tuple[str, float], ...]:
    excluded = {
        "overrides",
        *_OPENROUTER_RATE_FIELDS,
        *_OPENROUTER_CACHE_TIER_FIELDS,
    }
    values = []
    for name in sorted(set(pricing) - excluded):
        rate = _rate(pricing.get(name), required=False, field=name)
        if rate is not None:
            values.append((str(name), rate))
    return tuple(values)


def _conditional_tiers(
    pricing: Mapping[str, Any],
) -> tuple[Mapping[str, Any], ...]:
    overrides = pricing.get("overrides")
    if overrides is None or overrides == [] or overrides == ():
        return ()
    if not isinstance(overrides, list):
        raise PricingSourceError("invalid_pricing_schema")
    tiers: list[dict[str, Any]] = []
    for raw in overrides:
        if not isinstance(raw, dict):
            raise PricingSourceError("invalid_pricing_schema")
        condition_keys = set(raw) - set(_OPENROUTER_RATE_FIELDS)
        condition_keys -= _OPENROUTER_CACHE_TIER_FIELDS
        condition_keys -= {"min_prompt_tokens"}
        # Unknown numeric keys are price dimensions, not conditions.
        unknown_price_keys = {
            key
            for key in condition_keys
            if _PLAIN_DECIMAL.fullmatch(str(raw.get(key))) is not None
        }
        condition_keys -= unknown_price_keys
        if condition_keys:
            raise PricingSourceError("unsupported_pricing_condition")
        minimum = raw.get("min_prompt_tokens")
        if (
            isinstance(minimum, bool)
            or not isinstance(minimum, int)
            or minimum < 0
        ):
            raise PricingSourceError("invalid_pricing_schema")
        tier: dict[str, Any] = {"min_prompt_tokens": minimum}
        for source_name, stored_name in _OPENROUTER_RATE_FIELDS.items():
            if source_name in raw:
                tier[stored_name] = _rate(
                    raw.get(source_name), required=True, field=source_name
                )
        additional = {
            str(name): _rate(raw.get(name), required=True, field=str(name))
            for name in sorted(unknown_price_keys)
        }
        if additional:
            tier["additional_per_unit"] = additional
        if len(tier) == 1:
            raise PricingSourceError("invalid_pricing_schema")
        tiers.append(tier)
    tiers.sort(key=lambda item: int(item["min_prompt_tokens"]))
    if len({int(item["min_prompt_tokens"]) for item in tiers}) != len(tiers):
        raise PricingSourceError("invalid_pricing_schema")
    return tuple(tiers)


def _openrouter_fact(
    engine: str,
    model: str,
    source_model_id: str,
    evidence: HttpEvidence,
    *,
    now: datetime,
) -> PricingFact:
    if len(evidence.body) > MAX_RESPONSE_BYTES:
        raise PricingSourceError("response_too_large")
    if evidence.status in {301, 302, 303, 307, 308}:
        raise PricingSourceError("redirect_rejected")
    if evidence.status != 200:
        raise PricingSourceError(f"http_{evidence.status}")
    if evidence.content_type.split(";", 1)[0].strip().casefold() != "application/json":
        raise PricingSourceError("unsupported_content_type")
    try:
        payload = json.loads(evidence.body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PricingSourceError("malformed_json", str(exc)) from exc
    data = payload.get("data") if isinstance(payload, dict) else None
    pricing = data.get("pricing") if isinstance(data, dict) else None
    requested = str(model or "").strip()
    response_model_id = str(data.get("id") or "").strip() if isinstance(data, dict) else ""
    canonical = (
        str(data.get("canonical_slug") or "").strip()
        if isinstance(data, dict)
        else ""
    )
    if response_model_id != source_model_id:
        raise PricingSourceError("model_id_mismatch")
    if evidence.url != _openrouter_url(source_model_id):
        raise PricingSourceError("source_url_mismatch")
    if (
        not isinstance(pricing, dict)
        or _MODEL_ID.fullmatch(canonical) is None
    ):
        raise PricingSourceError("invalid_pricing_schema")
    conditional_tiers = _conditional_tiers(pricing)
    additional_rates = _additional_rates(pricing)

    prompt = _rate(pricing.get("prompt"), required=True, field="prompt")
    completion = _rate(
        pricing.get("completion"), required=True, field="completion"
    )
    request = _rate(pricing.get("request"), required=False, field="request")
    cache_read = _rate(
        pricing.get("input_cache_read"), required=False, field="input_cache_read"
    )
    cache_write = _rate(
        pricing.get("input_cache_write"), required=False, field="input_cache_write"
    )
    thinking = _rate(
        pricing.get("internal_reasoning"),
        required=False,
        field="internal_reasoning",
    )
    tier_fields = (
        "input_cache_write_5m",
        "input_cache_write_1h",
    )
    tiers = tuple(
        (field.removeprefix("input_cache_write_"), rate)
        for field in tier_fields
        if (rate := _rate(pricing.get(field), required=False, field=field))
        is not None
    )
    evidence_hash = hashlib.sha256(evidence.body).hexdigest()
    all_modeled_rates_zero = prompt == completion == 0.0 and all(
        value in {None, 0.0}
        for value in (
            request,
            cache_read,
            cache_write,
            thinking,
            *(rate for _, rate in tiers),
            *(rate for _, rate in additional_rates),
            *(
                value
                for tier in conditional_tiers
                for name, value in tier.items()
                if name not in {"min_prompt_tokens", "additional_per_unit"}
            ),
            *(
                value
                for tier in conditional_tiers
                for value in dict(tier.get("additional_per_unit", {})).values()
            ),
        )
    )
    if all_modeled_rates_zero and not source_model_id.endswith(":free"):
        raise PricingSourceError("unproven_zero_price")
    status = "known_zero" if all_modeled_rates_zero else "known"
    return PricingFact(
        status=status,
        scope=source_scope(engine),
        engine=normalize_engine(engine),
        requested_model_id=requested,
        canonical_model_id=canonical,
        currency="USD",
        unit="token",
        input_per_unit=prompt,
        output_per_unit=completion,
        cache_read_per_unit=cache_read,
        cache_write_per_unit=cache_write,
        cache_write_tiers=tiers,
        thinking_per_unit=thinking,
        request_per_unit=request,
        source_url=evidence.url,
        unit_source_url=OPENROUTER_UNIT_SOURCE_URL,
        source_kind="openrouter_models_api",
        fetched_at=_iso(evidence.fetched_at),
        expires_at=_iso(now + SUCCESS_TTL),
        source_revision=f"openrouter:sha256:{evidence_hash}",
        revision_kind="content_sha256",
        evidence_sha256=evidence_hash,
        unknown_reason=None,
        source_engine=OPENROUTER_ENGINE,
        source_model_id=source_model_id,
        conditional_price_tiers=conditional_tiers,
        additional_per_unit=additional_rates,
    )


def refresh_pricing_fact(
    engine: str,
    model: str,
    *,
    cache_path: Path | str | None = None,
    fetcher: Callable[[str], HttpEvidence] | None = None,
    now: datetime | None = None,
    force: bool = False,
) -> PricingFact:
    """Refresh one exact provider/model fact and persist it atomically."""

    selected_now = _utc(now)
    path = _selected_cache_path(cache_path)
    key = _cache_key(engine, model)
    with _refresh_lock(path, key):
        try:
            with _exclusive_cache_lock(path):
                payload = _read_cache(path)
                previous = _cached_fact(payload, key, engine, model)
                if (
                    previous is not None
                    and _fact_is_fresh(previous, selected_now)
                    and not force
                ):
                    return previous
        except (PricingSourceError, OSError):
            previous = _cached_fact(_read_cache(path), key, engine, model)

        # Network I/O is outside the whole-cache lock. Independent models can
        # discover concurrently, while the exact-key lock coalesces duplicates.
        source_model_id: str | None = None
        url: str | None = None
        fact: PricingFact | None = None
        failure_reason: str | None = None
        selected_fetcher = fetcher or shared_bounded_https_get
        try:
            try:
                source_model_id = resolve_source_model_id(engine, model)
            except PricingSourceError as exc:
                if exc.reason != "catalogue_required":
                    raise
                catalogue = selected_fetcher(OPENROUTER_MODELS_URL)
                source_model_id = resolve_source_model_id(
                    engine,
                    model,
                    catalogue_evidence=catalogue,
                )
            url = _openrouter_url(source_model_id)
            evidence = selected_fetcher(url)
            fact = _openrouter_fact(
                engine,
                model,
                source_model_id,
                evidence,
                now=selected_now,
            )
        except PricingSourceError as exc:
            failure_reason = exc.reason
        except TimeoutError:
            failure_reason = "timeout"
        except Exception:
            # Discovery must never block model use or expose arbitrary
            # exception details in a shared cache.
            failure_reason = "fetch_failed"

        with _exclusive_cache_lock(path):
            payload = _read_cache(path)
            latest = _cached_fact(payload, key, engine, model)
            if (
                latest is not None
                and _fact_is_fresh(latest, selected_now)
                and not force
            ):
                return latest
            if fact is None:
                fact = _unknown_fact(
                    engine,
                    model,
                    reason=failure_reason or "fetch_failed",
                    now=selected_now,
                    previous=latest or previous,
                    source_url=url,
                    source_model_id=source_model_id,
                )
            payload["facts"][key] = fact.to_dict()
            _write_cache(path, payload)
            return fact


def calculate_cost(
    fact: PricingFact,
    *,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int = 0,
    thinking_tokens: int = 0,
    thinking_in_output: bool = False,
) -> float | None:
    """Calculate an estimate only when every used dimension has a rate."""

    if fact.status not in {"known", "known_zero"}:
        return None
    if fact.currency != "USD" or fact.unit != "token":
        return None
    if (
        fact.input_per_unit is None
        or fact.output_per_unit is None
    ):
        return None
    input_count = max(0, int(input_tokens or 0))
    output_count = max(0, int(output_tokens or 0))
    cached_count = min(input_count, max(0, int(cached_tokens or 0)))
    rates = {
        "input_per_unit": fact.input_per_unit,
        "output_per_unit": fact.output_per_unit,
        "cache_read_per_unit": fact.cache_read_per_unit,
        "thinking_per_unit": fact.thinking_per_unit,
        "request_per_unit": fact.request_per_unit,
    }
    for tier in fact.conditional_price_tiers:
        if input_count < int(tier["min_prompt_tokens"]):
            break
        for name in tuple(rates):
            if name in tier:
                rates[name] = float(tier[name])
    if cached_count and rates["cache_read_per_unit"] is None:
        return None
    separate_thinking = 0 if thinking_in_output else max(0, int(thinking_tokens or 0))
    if separate_thinking and rates["thinking_per_unit"] is None:
        return None
    total = (
        (input_count - cached_count) * float(rates["input_per_unit"] or 0.0)
        + cached_count * float(rates["cache_read_per_unit"] or 0.0)
        + output_count * float(rates["output_per_unit"] or 0.0)
        + separate_thinking * float(rates["thinking_per_unit"] or 0.0)
        + float(rates["request_per_unit"] or 0.0)
    )
    return round(total, 6)


def schedule_prewarm(
    engine: str,
    model: str,
    *,
    cache_path: Path | str | None = None,
) -> bool:
    """Start a best-effort deduplicated background refresh after selection."""

    key = _cache_key(engine, model) + "@" + str(_selected_cache_path(cache_path))
    with _SCHEDULE_GUARD:
        if key in _SCHEDULED:
            return False
        _SCHEDULED.add(key)

    def run() -> None:
        try:
            refresh_pricing_fact(engine, model, cache_path=cache_path)
        finally:
            with _SCHEDULE_GUARD:
                _SCHEDULED.discard(key)

    threading.Thread(
        target=run,
        name="hashi-pricing-prewarm",
        daemon=True,
    ).start()
    return True
