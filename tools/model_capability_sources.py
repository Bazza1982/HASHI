"""Cache-backed exact model capability facts for PAO media routing.

The message path is cache-only.  Network refresh is asynchronous and shares
only bounded HTTP evidence with pricing discovery; capability validation,
freshness, persistence, and failure state remain independent.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence
from urllib.parse import quote
from uuid import uuid4

from tools.pricing_sources import (
    EXACT_OPENROUTER_MODEL_MAPPINGS,
    HttpEvidence,
    MAX_RESPONSE_BYTES,
    OPENROUTER_MODELS_URL,
    PricingSourceError,
    resolve_source_model_id as _resolve_pricing_source_model_id,
    shared_bounded_https_get,
)


CACHE_SCHEMA_VERSION = 2
ADAPTER_REVISION = "openrouter-model-capability.v2"
SUCCESS_TTL = timedelta(hours=24)
NEGATIVE_TTL = timedelta(minutes=15)
LOCK_WAIT_SECONDS = 20.0
LOCK_STALE_SECONDS = 60.0

OPENROUTER_ENGINE = "openrouter-api"
OPENROUTER_HOST = "openrouter.ai"
OPENROUTER_SOURCE_KIND = "openrouter_models_api"
MODALITY_STATES = frozenset({"supported", "unsupported", "unknown"})
INPUT_MODALITIES = ("text", "image", "audio", "video", "file", "document")
OUTPUT_MODALITIES = ("text", "image", "audio", "video", "file")
_MODEL_ID = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._:+-]*$"
)
_MODALITY_ID = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

_SCHEDULE_GUARD = threading.Lock()
_SCHEDULED_CALLBACKS: dict[str, list[Callable[[Any], None]]] = {}
_REFRESH_GUARD = threading.Lock()
_REFRESH_LOCKS: dict[str, threading.Lock] = {}


class CapabilitySourceError(RuntimeError):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = str(reason or "capability_source_error")


@dataclass(frozen=True)
class CapabilityFact:
    status: str
    engine: str
    requested_model_id: str
    source_engine: str | None
    source_model_id: str | None
    canonical_model_id: str | None
    input_modalities: Mapping[str, str]
    output_modalities: Mapping[str, str]
    source_url: str | None
    source_kind: str | None
    fetched_at: str
    expires_at: str
    source_revision: str | None
    revision_kind: str | None
    evidence_sha256: str | None
    unknown_reason: str | None
    stale: bool = False
    last_known_revision: str | None = None
    last_known_fetched_at: str | None = None
    last_known_canonical_model_id: str | None = None
    last_known_input_modalities: Mapping[str, str] | None = None
    last_known_output_modalities: Mapping[str, str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Any) -> "CapabilityFact":
        if not isinstance(value, dict):
            raise ValueError("capability fact must be an object")
        fact = cls(**dict(value))
        if fact.status not in {"known", "unknown"}:
            raise ValueError("invalid capability fact status")
        return fact

    def input_status(self, modality: str) -> str:
        return str(self.input_modalities.get(str(modality).casefold()) or "unknown")

    def output_status(self, modality: str) -> str:
        return str(self.output_modalities.get(str(modality).casefold()) or "unknown")


def _utc(value: datetime | None = None) -> datetime:
    selected = value or datetime.now(timezone.utc)
    if selected.tzinfo is None:
        selected = selected.replace(tzinfo=timezone.utc)
    return selected.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat()


def _parse_time(value: str) -> datetime:
    return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))


def normalize_engine(engine: str) -> str:
    normalized = str(engine or "").strip().casefold().replace("_", "-")
    aliases = {
        "openrouter": OPENROUTER_ENGINE,
        "codex": "codex-cli",
        "hashi": "hashi-api",
    }
    return aliases.get(normalized, normalized)


def default_cache_path() -> Path:
    explicit = str(
        os.environ.get("HASHI_MODEL_CAPABILITY_CACHE_FILE") or ""
    ).strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    bridge_home = str(
        os.environ.get("BRIDGE_HOME")
        or os.environ.get("HASHI_REMOTE_ROOT")
        or ""
    ).strip()
    root = Path(bridge_home).expanduser() if bridge_home else Path.cwd()
    return (root / "tmp" / "model-capability-facts-v1.json").resolve()


def _selected_cache_path(cache_path: Path | str | None) -> Path:
    return (
        default_cache_path()
        if cache_path is None
        else Path(cache_path).expanduser().resolve()
    )


def _cache_key(engine: str, model: str) -> str:
    return json.dumps(
        [normalize_engine(engine), str(model or "").strip()],
        ensure_ascii=True,
        separators=(",", ":"),
    )


def _refresh_lock(path: Path, key: str) -> threading.Lock:
    """Serialize one exact fact without blocking unrelated model fetches."""

    lock_key = f"{path}@{key}"
    with _REFRESH_GUARD:
        return _REFRESH_LOCKS.setdefault(lock_key, threading.Lock())


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
                raise CapabilitySourceError("refresh_busy")
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


def _openrouter_url(model: str) -> str:
    selected = str(model or "").strip()
    if _MODEL_ID.fullmatch(selected) is None:
        raise CapabilitySourceError("invalid_source_model_id")
    author, slug = selected.split("/", 1)
    return (
        "https://openrouter.ai/api/v1/model/"
        + quote(author, safe="._-")
        + "/"
        + quote(slug, safe="._:+-")
    )


def resolve_source_model_id(
    engine: str,
    model: str,
    *,
    mappings: Mapping[tuple[str, str], str | Sequence[str]] | None = None,
    catalogue_evidence: HttpEvidence | None = None,
) -> str:
    """Return one exact OpenRouter catalogue ID or fail closed."""
    try:
        return _resolve_pricing_source_model_id(
            engine,
            model,
            mappings=mappings,
            catalogue_evidence=catalogue_evidence,
        )
    except PricingSourceError as exc:
        raise CapabilitySourceError(exc.reason) from exc


def _unknown_modalities(values: Sequence[str]) -> dict[str, str]:
    return {name: "unknown" for name in values}


def _last_known_snapshot(
    previous: CapabilityFact | None,
) -> tuple[
    str | None,
    str | None,
    str | None,
    Mapping[str, str] | None,
    Mapping[str, str] | None,
]:
    if previous is None:
        return None, None, None, None, None
    if previous.status == "known":
        return (
            previous.source_revision,
            previous.fetched_at,
            previous.canonical_model_id,
            dict(previous.input_modalities),
            dict(previous.output_modalities),
        )
    return (
        previous.last_known_revision,
        previous.last_known_fetched_at,
        previous.last_known_canonical_model_id,
        (
            dict(previous.last_known_input_modalities)
            if previous.last_known_input_modalities is not None
            else None
        ),
        (
            dict(previous.last_known_output_modalities)
            if previous.last_known_output_modalities is not None
            else None
        ),
    )


def _unknown_fact(
    engine: str,
    model: str,
    *,
    reason: str,
    now: datetime,
    previous: CapabilityFact | None = None,
    source_model_id: str | None = None,
    source_url: str | None = None,
) -> CapabilityFact:
    (
        last_revision,
        last_fetched,
        last_canonical,
        last_input,
        last_output,
    ) = _last_known_snapshot(previous)
    return CapabilityFact(
        status="unknown",
        engine=normalize_engine(engine),
        requested_model_id=str(model or "").strip(),
        source_engine=(OPENROUTER_ENGINE if source_model_id else None),
        source_model_id=source_model_id,
        canonical_model_id=None,
        input_modalities=_unknown_modalities(INPUT_MODALITIES),
        output_modalities=_unknown_modalities(OUTPUT_MODALITIES),
        source_url=source_url,
        source_kind=(OPENROUTER_SOURCE_KIND if source_model_id else None),
        fetched_at=_iso(now),
        expires_at=_iso(now + NEGATIVE_TTL),
        source_revision=None,
        revision_kind=None,
        evidence_sha256=None,
        unknown_reason=str(reason or "unknown"),
        stale=last_revision is not None,
        last_known_revision=last_revision,
        last_known_fetched_at=last_fetched,
        last_known_canonical_model_id=last_canonical,
        last_known_input_modalities=last_input,
        last_known_output_modalities=last_output,
    )


def _fact_is_fresh(fact: CapabilityFact, now: datetime) -> bool:
    try:
        return _parse_time(fact.expires_at) > _utc(now)
    except (TypeError, ValueError):
        return False


def _valid_modality_map(
    value: Mapping[str, str],
    *,
    required: Sequence[str],
) -> bool:
    if not isinstance(value, Mapping) or any(name not in value for name in required):
        return False
    return all(
        _MODALITY_ID.fullmatch(str(name)) is not None
        and str(state) in MODALITY_STATES
        for name, state in value.items()
    )


def _valid_cached_fact(fact: CapabilityFact, engine: str, model: str) -> bool:
    if (
        fact.engine != normalize_engine(engine)
        or fact.requested_model_id != str(model or "").strip()
        or not _valid_modality_map(
            fact.input_modalities,
            required=INPUT_MODALITIES,
        )
        or not _valid_modality_map(
            fact.output_modalities,
            required=OUTPUT_MODALITIES,
        )
    ):
        return False
    try:
        fetched_at = _parse_time(fact.fetched_at)
        expires_at = _parse_time(fact.expires_at)
    except (TypeError, ValueError):
        return False
    if expires_at <= fetched_at:
        return False

    if fact.status == "known":
        return bool(
            fact.source_engine == OPENROUTER_ENGINE
            and fact.source_model_id
            and fact.canonical_model_id
            and _MODEL_ID.fullmatch(fact.source_model_id)
            and _MODEL_ID.fullmatch(fact.canonical_model_id)
            and fact.source_url == _openrouter_url(fact.source_model_id)
            and fact.source_kind == OPENROUTER_SOURCE_KIND
            and fact.source_revision
            and fact.source_revision.startswith("openrouter-capability:sha256:")
            and fact.revision_kind == "content_sha256"
            and fact.evidence_sha256
            and _SHA256.fullmatch(fact.evidence_sha256)
            and fact.evidence_sha256 in fact.source_revision
            and fact.unknown_reason is None
            and fact.stale is False
        )

    if not str(fact.unknown_reason or "").strip():
        return False
    if (
        fact.canonical_model_id is not None
        or fact.source_revision is not None
        or fact.revision_kind is not None
        or fact.evidence_sha256 is not None
    ):
        return False
    if fact.source_model_id is not None:
        try:
            if fact.source_url != _openrouter_url(fact.source_model_id):
                return False
        except CapabilitySourceError:
            return False
    if any(
        state != "unknown"
        for state in (
            *fact.input_modalities.values(),
            *fact.output_modalities.values(),
        )
    ):
        return False
    if fact.stale:
        if not (
            fact.last_known_revision
            and fact.last_known_revision.startswith(
                "openrouter-capability:sha256:"
            )
            and fact.last_known_fetched_at
            and fact.last_known_canonical_model_id
            and _MODEL_ID.fullmatch(fact.last_known_canonical_model_id)
            and fact.last_known_input_modalities
            and _valid_modality_map(
                fact.last_known_input_modalities,
                required=INPUT_MODALITIES,
            )
            and fact.last_known_output_modalities
            and _valid_modality_map(
                fact.last_known_output_modalities,
                required=OUTPUT_MODALITIES,
            )
        ):
            return False
        try:
            _parse_time(fact.last_known_fetched_at)
        except (TypeError, ValueError):
            return False
        return True
    return fact.last_known_revision is None


def _cached_fact(
    payload: Mapping[str, Any],
    key: str,
    engine: str,
    model: str,
) -> CapabilityFact | None:
    try:
        fact = CapabilityFact.from_dict(payload.get("facts", {}).get(key))
    except (TypeError, ValueError):
        return None
    return fact if _valid_cached_fact(fact, engine, model) else None


def get_cached_capability_fact(
    engine: str,
    model: str,
    *,
    cache_path: Path | str | None = None,
    now: datetime | None = None,
) -> CapabilityFact:
    """Read one exact fact without network I/O; stale data grants no route."""

    selected_now = _utc(now)
    payload = _read_cache(_selected_cache_path(cache_path))
    fact = _cached_fact(payload, _cache_key(engine, model), engine, model)
    if fact is None:
        return _unknown_fact(engine, model, reason="cache_miss", now=selected_now)
    if not _fact_is_fresh(fact, selected_now):
        return _unknown_fact(
            engine,
            model,
            reason="stale_cache",
            now=selected_now,
            previous=fact,
            source_model_id=fact.source_model_id,
            source_url=fact.source_url,
        )
    return fact


def _listed_modalities(value: Any, *, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise CapabilitySourceError("missing_capability_field", field)
    normalized: list[str] = []
    for raw in value:
        item = str(raw or "").strip().casefold()
        if not item or _MODALITY_ID.fullmatch(item) is None:
            raise CapabilitySourceError("invalid_capability_schema", field)
        if item not in normalized:
            normalized.append(item)
    if not normalized:
        raise CapabilitySourceError("missing_capability_field", field)
    return tuple(normalized)


def _state_map(
    listed: Sequence[str],
    *,
    known: Sequence[str],
    document_unknown: bool = False,
) -> dict[str, str]:
    values = {
        modality: ("supported" if modality in listed else "unsupported")
        for modality in known
    }
    for modality in listed:
        values.setdefault(modality, "supported")
    if document_unknown:
        # OpenRouter's ``file`` entry does not prove that an adapter can send
        # arbitrary HASHI documents, local paths, audio, or video.
        values["document"] = "unknown"
    return values


def _openrouter_fact(
    engine: str,
    model: str,
    source_model_id: str,
    evidence: HttpEvidence,
    *,
    now: datetime,
) -> CapabilityFact:
    if len(evidence.body) > MAX_RESPONSE_BYTES:
        raise CapabilitySourceError("response_too_large")
    if evidence.status in {301, 302, 303, 307, 308}:
        raise CapabilitySourceError("redirect_rejected")
    if evidence.status != 200:
        raise CapabilitySourceError(f"http_{evidence.status}")
    if evidence.content_type.split(";", 1)[0].strip().casefold() != "application/json":
        raise CapabilitySourceError("unsupported_content_type")
    try:
        payload = json.loads(evidence.body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CapabilitySourceError("malformed_json", str(exc)) from exc
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise CapabilitySourceError("invalid_capability_schema")
    if str(data.get("id") or "").strip() != source_model_id:
        raise CapabilitySourceError("model_id_mismatch")
    if evidence.url != _openrouter_url(source_model_id):
        raise CapabilitySourceError("source_url_mismatch")
    canonical = str(data.get("canonical_slug") or "").strip()
    if _MODEL_ID.fullmatch(canonical) is None:
        raise CapabilitySourceError("missing_capability_field", "canonical_slug")
    architecture = data.get("architecture")
    if not isinstance(architecture, dict):
        raise CapabilitySourceError("missing_capability_field", "architecture")
    inputs = _listed_modalities(
        architecture.get("input_modalities"),
        field="architecture.input_modalities",
    )
    outputs = _listed_modalities(
        architecture.get("output_modalities"),
        field="architecture.output_modalities",
    )
    evidence_hash = hashlib.sha256(evidence.body).hexdigest()
    return CapabilityFact(
        status="known",
        engine=normalize_engine(engine),
        requested_model_id=str(model or "").strip(),
        source_engine=OPENROUTER_ENGINE,
        source_model_id=source_model_id,
        canonical_model_id=canonical,
        input_modalities=_state_map(
            inputs,
            known=INPUT_MODALITIES,
            document_unknown=True,
        ),
        output_modalities=_state_map(outputs, known=OUTPUT_MODALITIES),
        source_url=evidence.url,
        source_kind=OPENROUTER_SOURCE_KIND,
        fetched_at=_iso(evidence.fetched_at),
        expires_at=_iso(now + SUCCESS_TTL),
        source_revision=f"openrouter-capability:sha256:{evidence_hash}",
        revision_kind="content_sha256",
        evidence_sha256=evidence_hash,
        unknown_reason=None,
    )


def refresh_capability_fact(
    engine: str,
    model: str,
    *,
    cache_path: Path | str | None = None,
    fetcher: Callable[[str], HttpEvidence] | None = None,
    now: datetime | None = None,
    force: bool = False,
    source_mappings: Mapping[
        tuple[str, str], str | Sequence[str]
    ] | None = None,
) -> CapabilityFact:
    """Refresh and atomically persist one independently validated fact."""

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
        except CapabilitySourceError as exc:
            # A stale process lock must not crash a best-effort background
            # refresh. The atomic cache remains authoritative and untouched.
            previous = _cached_fact(
                _read_cache(path),
                key,
                engine,
                model,
            )
            return _unknown_fact(
                engine,
                model,
                reason=exc.reason,
                now=selected_now,
                previous=previous,
            )
        except OSError:
            previous = _cached_fact(
                _read_cache(path),
                key,
                engine,
                model,
            )
            return _unknown_fact(
                engine,
                model,
                reason="cache_io_error",
                now=selected_now,
                previous=previous,
            )

        # Network I/O deliberately occurs outside the whole-cache file lock.
        # Different exact models can refresh concurrently; the process-local
        # key lock above still coalesces duplicate refreshes for one fact.
        source_model_id: str | None = None
        url: str | None = None
        fact: CapabilityFact | None = None
        failure_reason: str | None = None
        selected_fetcher = fetcher or shared_bounded_https_get
        try:
            try:
                source_model_id = resolve_source_model_id(
                    engine,
                    model,
                    mappings=source_mappings,
                )
            except CapabilitySourceError as exc:
                if exc.reason != "catalogue_required":
                    raise
                catalogue = selected_fetcher(OPENROUTER_MODELS_URL)
                source_model_id = resolve_source_model_id(
                    engine,
                    model,
                    mappings=source_mappings,
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
        except CapabilitySourceError as exc:
            failure_reason = exc.reason
        except PricingSourceError as exc:
            failure_reason = exc.reason
        except TimeoutError:
            failure_reason = "timeout"
        except Exception:
            failure_reason = "fetch_failed"

        try:
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
                        source_model_id=source_model_id,
                        source_url=url,
                    )
                payload["facts"][key] = fact.to_dict()
                _write_cache(path, payload)
                return fact
        except CapabilitySourceError as exc:
            return _unknown_fact(
                engine,
                model,
                reason=exc.reason,
                now=selected_now,
                previous=_cached_fact(
                    _read_cache(path),
                    key,
                    engine,
                    model,
                )
                or previous,
                source_model_id=source_model_id,
                source_url=url,
            )
        except OSError:
            return _unknown_fact(
                engine,
                model,
                reason="cache_io_error",
                now=selected_now,
                previous=previous,
                source_model_id=source_model_id,
                source_url=url,
            )


def schedule_prewarm(
    engine: str,
    model: str,
    *,
    cache_path: Path | str | None = None,
    on_complete: Callable[[CapabilityFact], None] | None = None,
) -> bool:
    """Start one best-effort, deduplicated asynchronous refresh."""

    key = _cache_key(engine, model) + "@" + str(_selected_cache_path(cache_path))
    with _SCHEDULE_GUARD:
        callbacks = _SCHEDULED_CALLBACKS.get(key)
        if callbacks is not None:
            if on_complete is not None:
                callbacks.append(on_complete)
            return False
        _SCHEDULED_CALLBACKS[key] = (
            [on_complete] if on_complete is not None else []
        )

    def run() -> None:
        fact: CapabilityFact | None = None
        try:
            fact = refresh_capability_fact(engine, model, cache_path=cache_path)
        finally:
            with _SCHEDULE_GUARD:
                completion_callbacks = _SCHEDULED_CALLBACKS.pop(key, [])
        if fact is not None:
            for callback in completion_callbacks:
                try:
                    callback(fact)
                except Exception:
                    pass

    threading.Thread(
        target=run,
        name="hashi-model-capability-prewarm",
        daemon=True,
    ).start()
    return True
