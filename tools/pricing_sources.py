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
from typing import Any, Callable, Iterator
from urllib.parse import quote, urlsplit
from uuid import uuid4

CACHE_SCHEMA_VERSION = 1
ADAPTER_REVISION = "openrouter-model-api.v1"
SUCCESS_TTL = timedelta(hours=24)
NEGATIVE_TTL = timedelta(minutes=15)
MAX_RESPONSE_BYTES = 64 * 1024
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
_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._:+-]*$")
_PLAIN_DECIMAL = re.compile(r"^(?:0|[0-9]+\.[0-9]+)$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SCHEDULE_GUARD = threading.Lock()
_SCHEDULED: set[str] = set()
_EVIDENCE_GUARD = threading.Lock()
_EVIDENCE_IN_FLIGHT: dict[str, "_EvidenceFetch"] = {}
_RECENT_EVIDENCE: dict[str, tuple[float, "HttpEvidence"]] = {}
_RECENT_EVIDENCE_SECONDS = 2.0


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

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["cache_write_tiers"] = [
            [name, rate] for name, rate in self.cache_write_tiers
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
    normalized = str(engine or "").strip().casefold()
    if normalized in OPENROUTER_ENGINES:
        return OPENROUTER_ENGINE
    return normalized


def source_scope(engine: str) -> str:
    return OPENROUTER_SCOPE if normalize_engine(engine) == OPENROUTER_ENGINE else "direct_provider"


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
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
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
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


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
        unit_source_url=(
            OPENROUTER_UNIT_SOURCE_URL
            if normalize_engine(engine) == OPENROUTER_ENGINE
            else None
        ),
        source_kind=(
            "openrouter_models_api"
            if normalize_engine(engine) == OPENROUTER_ENGINE
            else None
        ),
        fetched_at=_iso(now),
        expires_at=_iso(now + NEGATIVE_TTL),
        source_revision=None,
        revision_kind=None,
        evidence_sha256=None,
        unknown_reason=reason,
        last_known_revision=(last_known.source_revision if last_known else None),
        last_known_fetched_at=(last_known.fetched_at if last_known else None),
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

    if fact.status == "unknown":
        expected_kind = (
            "openrouter_models_api"
            if normalized_engine == OPENROUTER_ENGINE
            else None
        )
        expected_unit_url = (
            OPENROUTER_UNIT_SOURCE_URL
            if normalized_engine == OPENROUTER_ENGINE
            else None
        )
        if (
            fact.canonical_model_id is not None
            or fact.currency is not None
            or fact.unit is not None
            or fact.input_per_unit is not None
            or fact.output_per_unit is not None
            or fact.cache_read_per_unit is not None
            or fact.cache_write_per_unit is not None
            or fact.cache_write_tiers
            or fact.thinking_per_unit is not None
            or fact.request_per_unit is not None
            or fact.source_kind != expected_kind
            or fact.unit_source_url != expected_unit_url
            or fact.source_revision is not None
            or fact.revision_kind is not None
            or fact.evidence_sha256 is not None
            or not str(fact.unknown_reason or "").strip()
        ):
            return False
        if fact.source_url is not None:
            try:
                if fact.source_url != _openrouter_url(requested):
                    return False
            except PricingSourceError:
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

    if normalized_engine != OPENROUTER_ENGINE:
        return False
    evidence_hash = str(fact.evidence_sha256 or "")
    try:
        expected_url = _openrouter_url(requested)
    except PricingSourceError:
        return False
    if (
        _MODEL_ID.fullmatch(str(fact.canonical_model_id or "")) is None
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
    )
    all_zero = all(value in {None, 0, 0.0} for value in modeled_rates)
    if fact.status == "known_zero":
        return requested.endswith(":free") and all_zero
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
        if content_length.isdigit() and int(content_length) > MAX_RESPONSE_BYTES:
            raise PricingSourceError("response_too_large")
        body = bytearray()
        while True:
            remaining = TOTAL_TIMEOUT_SECONDS - (time.monotonic() - started)
            if remaining <= 0:
                raise PricingSourceError("timeout")
            if connection.sock is not None:
                connection.sock.settimeout(max(0.1, min(CONNECT_TIMEOUT_SECONDS, remaining)))
            chunk = response.read(min(8192, MAX_RESPONSE_BYTES + 1 - len(body)))
            if not chunk:
                break
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
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


def _openrouter_fact(
    engine: str,
    model: str,
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
    if response_model_id != requested:
        raise PricingSourceError("model_id_mismatch")
    if evidence.url != _openrouter_url(requested):
        raise PricingSourceError("source_url_mismatch")
    if (
        not isinstance(pricing, dict)
        or _MODEL_ID.fullmatch(canonical) is None
    ):
        raise PricingSourceError("invalid_pricing_schema")
    overrides = pricing.get("overrides")
    if overrides is not None and overrides != []:
        if not isinstance(overrides, list):
            raise PricingSourceError("invalid_pricing_schema")
        raise PricingSourceError("unsupported_pricing_overrides")

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
        )
    )
    if all_modeled_rates_zero and not requested.endswith(":free"):
        raise PricingSourceError("unproven_zero_price")
    status = "known_zero" if all_modeled_rates_zero else "known"
    return PricingFact(
        status=status,
        scope=OPENROUTER_SCOPE,
        engine=OPENROUTER_ENGINE,
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
    with _exclusive_cache_lock(path):
        payload = _read_cache(path)
        previous = _cached_fact(payload, key, engine, model)
        if previous is not None and _fact_is_fresh(previous, selected_now) and not force:
            return previous

        url: str | None = None
        try:
            if normalize_engine(engine) != OPENROUTER_ENGINE:
                raise PricingSourceError("source_not_qualified")
            url = _openrouter_url(model)
            evidence = (fetcher or shared_bounded_https_get)(url)
            fact = _openrouter_fact(
                engine,
                model,
                evidence,
                now=selected_now,
            )
        except PricingSourceError as exc:
            fact = _unknown_fact(
                engine,
                model,
                reason=exc.reason,
                now=selected_now,
                previous=previous,
                source_url=url,
            )
        except TimeoutError:
            fact = _unknown_fact(
                engine,
                model,
                reason="timeout",
                now=selected_now,
                previous=previous,
                source_url=url,
            )
        except Exception:
            # Price discovery must never block model use or expose arbitrary
            # exception details in a shared cache.
            fact = _unknown_fact(
                engine,
                model,
                reason="fetch_failed",
                now=selected_now,
                previous=previous,
                source_url=url,
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
    if cached_count and fact.cache_read_per_unit is None:
        return None
    separate_thinking = 0 if thinking_in_output else max(0, int(thinking_tokens or 0))
    if separate_thinking and fact.thinking_per_unit is None:
        return None
    total = (
        (input_count - cached_count) * fact.input_per_unit
        + cached_count * (fact.cache_read_per_unit or 0.0)
        + output_count * fact.output_per_unit
        + separate_thinking * (fact.thinking_per_unit or 0.0)
        + (fact.request_per_unit or 0.0)
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
