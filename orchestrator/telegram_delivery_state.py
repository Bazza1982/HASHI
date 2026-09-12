"""Transactional ownership store for Telegram delivery recovery state."""
from __future__ import annotations

import copy
import hashlib
import re
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

from orchestrator.agent_incarnation import (
    lifecycle_id_from_config,
    valid_agent_lifecycle_id,
)
from orchestrator.config_json import (
    ConfigConflictError,
    ConfigDocument,
    new_config_json,
    read_config_json,
    write_config_json,
)

STATE_VERSION = 2
MAX_QUARANTINE_RECORDS = 200
_PLACEHOLDER_TOKENS = {"", "WORKBENCH_ONLY_NO_TOKEN"}
_BOT_FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_TELEGRAM_TOKEN_RE = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b")
_TELEGRAM_BOT_URL_RE = re.compile(
    r"(?i)(api\.telegram\.org/bot)[^/\s]+"
)
_RAW_TOKEN_FIELDS = {"token", "telegram_token", "bot_token"}
_T = TypeVar("_T")


class DeliveryStateError(RuntimeError):
    """Delivery state is unreadable or violates the owned schema."""


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def telegram_bot_fingerprint(token: object) -> str | None:
    normalized = str(token or "").strip()
    if normalized in _PLACEHOLDER_TOKENS:
        return None
    digest = hashlib.sha256(
        b"HASHI telegram bot identity v1\0" + normalized.encode("utf-8")
    ).hexdigest()
    return f"sha256:{digest}"


def owner_for_runtime(runtime: object) -> dict[str, str] | None:
    config = getattr(runtime, "config", None)
    lifecycle_id = lifecycle_id_from_config(config)
    metadata = getattr(runtime, "metadata", None)
    if lifecycle_id is None and isinstance(metadata, Mapping):
        candidate = str(metadata.get("agent_lifecycle_id") or "").strip().casefold()
        lifecycle_id = candidate if valid_agent_lifecycle_id(candidate) else None
    global_config = getattr(runtime, "global_config", None) or getattr(
        runtime, "global_cfg", None
    )
    instance_id = str(
        getattr(global_config, "instance_id", "")
        or (metadata.get("instance_id") if isinstance(metadata, Mapping) else "")
        or ""
    ).strip().upper()
    fingerprint = telegram_bot_fingerprint(getattr(runtime, "token", None))
    if fingerprint is None and isinstance(metadata, Mapping):
        fingerprint = str(metadata.get("telegram_bot_fingerprint") or "").strip() or None
    if not instance_id or not lifecycle_id or not fingerprint:
        return None
    return {
        "instance_id": instance_id,
        "agent_lifecycle_id": lifecycle_id,
        "telegram_bot_fingerprint": fingerprint,
    }


def empty_state(path: Path) -> ConfigDocument:
    return new_config_json(
        path,
        {"version": STATE_VERSION, "agents": {}, "quarantine": []},
    )


def _validate_state(document: ConfigDocument) -> ConfigDocument:
    agents = document.get("agents")
    if not isinstance(agents, dict):
        raise DeliveryStateError("Telegram delivery state agents must be an object")
    quarantine = document.get("quarantine", [])
    if not isinstance(quarantine, list):
        raise DeliveryStateError("Telegram delivery state quarantine must be an array")
    document.setdefault("version", 1)
    document["quarantine"] = quarantine
    return document


def read_state(path: str | Path) -> ConfigDocument:
    target = Path(path).expanduser().resolve()
    try:
        return _validate_state(read_config_json(target))
    except FileNotFoundError:
        return empty_state(target)
    except (ValueError, OSError) as exc:
        raise DeliveryStateError(
            f"Telegram delivery state could not be read: {type(exc).__name__}"
        ) from exc


def mutate_state(
    path: str | Path,
    mutation: Callable[[ConfigDocument], tuple[_T, bool]],
    *,
    max_conflicts: int = 6,
) -> _T:
    """Re-evaluate one narrow mutation after each cross-process conflict."""

    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(max_conflicts + 1):
        document = read_state(target)
        value, changed = mutation(document)
        if not changed:
            return value
        document["version"] = STATE_VERSION
        try:
            write_config_json(target, document)
            return value
        except ConfigConflictError:
            if attempt >= max_conflicts:
                raise
    raise AssertionError("unreachable")


def record_owner(record: Mapping[str, Any]) -> dict[str, str] | None:
    owner = record.get("owner")
    if not isinstance(owner, Mapping):
        return None
    values = {
        "instance_id": str(owner.get("instance_id") or "").strip().upper(),
        "agent_lifecycle_id": str(
            owner.get("agent_lifecycle_id") or ""
        ).strip().casefold(),
        "telegram_bot_fingerprint": str(
            owner.get("telegram_bot_fingerprint") or ""
        ).strip(),
    }
    if (
        not all(values.values())
        or not valid_agent_lifecycle_id(values["agent_lifecycle_id"])
        or not _BOT_FINGERPRINT_RE.fullmatch(values["telegram_bot_fingerprint"])
    ):
        return None
    return values


def owners_equal(left: Mapping[str, Any] | None, right: Mapping[str, Any] | None) -> bool:
    return bool(left and right and dict(left) == dict(right))


def owned_record(
    state: Mapping[str, Any], agent_name: str, owner: Mapping[str, Any] | None
) -> dict[str, Any] | None:
    record = (state.get("agents") or {}).get(str(agent_name))
    if not isinstance(record, dict) or not owners_equal(record_owner(record), owner):
        return None
    return record


def _safe_record_copy(record: Mapping[str, Any]) -> dict[str, Any]:
    def sanitize(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {
                str(key): sanitize(item)
                for key, item in value.items()
                if str(key).casefold() not in _RAW_TOKEN_FIELDS
            }
        if isinstance(value, list):
            return [sanitize(item) for item in value]
        if isinstance(value, tuple):
            return [sanitize(item) for item in value]
        if isinstance(value, str):
            value = _TELEGRAM_BOT_URL_RE.sub(r"\1[REDACTED]", value)
            return _TELEGRAM_TOKEN_RE.sub("[REDACTED TELEGRAM TOKEN]", value)
        return copy.deepcopy(value)

    return sanitize(record)


def quarantine_record(
    state: dict[str, Any],
    agent_name: str,
    *,
    reason: str,
    expected_owner: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    agents = state.setdefault("agents", {})
    record = agents.pop(str(agent_name), None)
    if not isinstance(record, dict):
        return None
    entry = {
        "agent_name": str(agent_name),
        "reason": str(reason),
        "quarantined_at": now_iso(),
        "observed_owner": record_owner(record),
        "expected_owner": dict(expected_owner) if expected_owner else None,
        "incident_id": record.get("incident_id"),
        "record": _safe_record_copy(record),
    }
    quarantine = state.setdefault("quarantine", [])
    quarantine.append(entry)
    del quarantine[:-MAX_QUARANTINE_RECORDS]
    return entry


def retire_agent_state(
    root: str | Path,
    agent_name: str,
    *,
    lifecycle_id: object = None,
    reason: str,
) -> dict[str, Any] | None:
    path = Path(root).expanduser().resolve() / "state" / "telegram_delivery_health.json"
    expected_lifecycle = str(lifecycle_id or "").strip().casefold()

    def mutation(state: ConfigDocument):
        record = (state.get("agents") or {}).get(str(agent_name))
        if not isinstance(record, dict):
            return None, False
        observed = record_owner(record)
        if expected_lifecycle and observed and (
            observed.get("agent_lifecycle_id") != expected_lifecycle
        ):
            return None, False
        entry = quarantine_record(
            state,
            agent_name,
            reason=reason,
        )
        return entry, entry is not None

    return mutate_state(path, mutation)


def export_owned_state(
    root: str | Path,
    agent_name: str,
    owner: Mapping[str, Any],
) -> dict[str, Any] | None:
    path = Path(root).expanduser().resolve() / "state" / "telegram_delivery_health.json"
    record = owned_record(read_state(path), agent_name, owner)
    return _safe_record_copy(record) if record else None


def adopt_transferred_state(
    root: str | Path,
    agent_name: str,
    *,
    target_owner: Mapping[str, Any],
    transferred_record: Mapping[str, Any] | None,
    operation: str,
) -> dict[str, Any]:
    """Adopt only a proven move record; Clone starts with no pending notice."""

    normalized_target_owner = record_owner({"owner": target_owner})
    if normalized_target_owner is None:
        raise DeliveryStateError("target Telegram delivery owner is incomplete")
    target_owner = normalized_target_owner
    path = Path(root).expanduser().resolve() / "state" / "telegram_delivery_health.json"

    def mutation(state: ConfigDocument):
        changed = False
        existing = (state.get("agents") or {}).get(str(agent_name))
        if isinstance(existing, dict):
            quarantine_record(
                state,
                agent_name,
                reason="target_state_replaced_by_agent_transfer",
                expected_owner=target_owner,
            )
            changed = True
        imported = False
        if operation == "move" and isinstance(transferred_record, Mapping):
            source_owner = record_owner(transferred_record)
            if (
                source_owner
                and source_owner.get("agent_lifecycle_id")
                == target_owner.get("agent_lifecycle_id")
                and source_owner.get("telegram_bot_fingerprint")
                == target_owner.get("telegram_bot_fingerprint")
            ):
                record = _safe_record_copy(transferred_record)
                record["owner"] = dict(target_owner)
                state.setdefault("agents", {})[str(agent_name)] = record
                imported = True
                changed = True
            else:
                quarantine = state.setdefault("quarantine", [])
                quarantine.append(
                    {
                        "agent_name": str(agent_name),
                        "reason": "transferred_state_owner_mismatch",
                        "quarantined_at": now_iso(),
                        "observed_owner": source_owner,
                        "expected_owner": dict(target_owner),
                        "incident_id": transferred_record.get("incident_id"),
                        "record": _safe_record_copy(transferred_record),
                    }
                )
                del quarantine[:-MAX_QUARANTINE_RECORDS]
                changed = True
        return {"imported": imported, "operation": operation}, changed

    return mutate_state(path, mutation)
