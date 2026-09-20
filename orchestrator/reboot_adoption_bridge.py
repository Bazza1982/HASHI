"""One-generation bridge from legacy Worker-only broad reboot semantics.

The protected Core already owns a generic shared-Function handoff protocol.  A
legacy shared Function process cannot call newer PAO code, but it can qualify
and activate a new Agent Worker generation.  The deterministic leader in that
generation uses this module to publish exactly the same bounded Core request
after the legacy broad receipt has truthfully completed.

The successor shared process promotes that terminal legacy receipt only after
Core has committed the handoff.  Candidate failure therefore leaves the legacy
receipt schema and old shared process untouched.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
from collections.abc import Callable, Mapping
from pathlib import Path
import time
from typing import Any

from orchestrator.kernel_process import write_record


logger = logging.getLogger("BridgeU.FunctionWorker")

LEGACY_RECEIPT_SCHEMAS = frozenset({1, 2})
BROAD_REBOOT_MODES = frozenset({"same", "max"})
MAX_RECEIPT_BYTES = 128 * 1024
MAX_MARKER_BYTES = 4096
MAX_RECORDS = 50
MAX_OPERATION_AGE_SECONDS = 15 * 60.0
BRIDGE_POLL_SECONDS = 0.5
BRIDGE_WAIT_SECONDS = 5 * 60.0


def _runtime_state_dir(bridge_home: Path | str) -> Path:
    return Path(bridge_home) / "state" / "instance"


def _valid_operation_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 32
        and all(char in "0123456789abcdef" for char in value)
    )


def _valid_generation_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("sha256:")
        and len(value) == 71
        and all(char in "0123456789abcdef" for char in value[7:])
    )


def _positive_pid(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _read_json_object(path: Path, *, max_bytes: int) -> dict[str, Any]:
    if path.stat().st_size > max_bytes:
        raise ValueError(f"oversized JSON record: {path.name}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"invalid JSON record: {path.name}")
    return value


def _legacy_candidate(
    bridge_home: Path | str,
    *,
    agent_name: str,
    worker_pid: int,
    generation_id: str,
    shared_pid: int,
    shared_generation_id: str,
    worker_started_at: float,
    now: float,
) -> dict[str, Any] | None:
    if (
        not agent_name
        or _positive_pid(worker_pid) is None
        or _positive_pid(shared_pid) is None
        or not _valid_generation_id(generation_id)
        or not _valid_generation_id(shared_generation_id)
        or generation_id == shared_generation_id
    ):
        return None
    receipt_path = _runtime_state_dir(bridge_home) / "reboot-receipts.json"
    try:
        payload = _read_json_object(receipt_path, max_bytes=MAX_RECEIPT_BYTES)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    records = payload.get("records")
    if (
        payload.get("schema") not in LEGACY_RECEIPT_SCHEMAS
        or not isinstance(records, list)
        or len(records) > MAX_RECORDS
    ):
        return None

    for record in reversed(records):
        if not isinstance(record, dict):
            continue
        targets = record.get("targets")
        workers = record.get("workers")
        generations = record.get("generations")
        online = record.get("online")
        shared = record.get("shared_replacement")
        try:
            updated_at = float(record.get("updated_at"))
        except (TypeError, ValueError, OverflowError):
            continue
        if (
            not math.isfinite(updated_at)
            or updated_at < float(worker_started_at) - 5.0
            or updated_at > now + 5.0
            or now - updated_at > MAX_OPERATION_AGE_SECONDS
            or not _valid_operation_id(record.get("id"))
            or record.get("mode") not in BROAD_REBOOT_MODES
            or record.get("status") != "succeeded"
            or record.get("phase") != "finished"
            or record.get("committed") is not True
            or not isinstance(targets, list)
            or not targets
            or len(targets) > 100
            or any(not isinstance(name, str) or not name for name in targets)
            or len(set(targets)) != len(targets)
            or min(targets) != agent_name
            or not isinstance(workers, dict)
            or not isinstance(generations, dict)
            or not isinstance(online, dict)
            or (
                shared is not None
                and (
                    not isinstance(shared, dict)
                    or shared.get("status") not in {None, "not_requested"}
                    or shared.get("request_id") is not None
                )
            )
        ):
            continue

        valid_workers = True
        for name in targets:
            evidence = workers.get(name)
            if (
                not isinstance(evidence, dict)
                or _positive_pid(evidence.get("new_pid")) is None
                or evidence.get("generation_id") != generation_id
                or evidence.get("online") is not True
                or generations.get(name) != generation_id
                or online.get(name) is not True
            ):
                valid_workers = False
                break
        own = workers.get(agent_name) or {}
        if valid_workers and own.get("new_pid") == worker_pid:
            return record
    return None


def _validate_marker(value: Mapping[str, Any]) -> dict[str, Any]:
    marker = dict(value)
    valid = (
        marker.get("schema") == 1
        and _valid_operation_id(marker.get("operation_id"))
        and isinstance(marker.get("requested_at"), (int, float))
        and not isinstance(marker.get("requested_at"), bool)
        and math.isfinite(float(marker["requested_at"]))
        and _positive_pid(marker.get("old_shared_pid")) is not None
        and _positive_pid(marker.get("worker_pid")) is not None
        and _valid_generation_id(marker.get("old_shared_generation_id"))
        and _valid_generation_id(marker.get("expected_generation_id"))
        and marker["old_shared_generation_id"]
        != marker["expected_generation_id"]
        and isinstance(marker.get("leader_agent"), str)
        and bool(marker["leader_agent"])
        and len(marker["leader_agent"]) <= 200
    )
    if not valid:
        raise ValueError("invalid legacy reboot handoff marker")
    return marker


def legacy_handoff_markers(
    bridge_home: Path | str,
) -> tuple[tuple[Path, dict[str, Any]], ...]:
    """Return bounded, validated promotion markers for the successor PAO."""

    marker_dir = _runtime_state_dir(bridge_home) / "legacy-reboot-handoffs"
    if not marker_dir.is_dir():
        return ()
    result: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(marker_dir.glob("*.json"))[:MAX_RECORDS]:
        try:
            value = _validate_marker(
                _read_json_object(path, max_bytes=MAX_MARKER_BYTES)
            )
            if path.stem != value["operation_id"]:
                raise ValueError("legacy handoff marker identity mismatch")
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            logger.warning("Ignored invalid legacy reboot marker %s: %s", path, exc)
            continue
        result.append((path, value))
    return tuple(result)


def _publish_candidate(
    bridge_home: Path | str,
    record: Mapping[str, Any],
    *,
    agent_name: str,
    worker_pid: int,
    generation_id: str,
    shared_pid: int,
    shared_generation_id: str,
    requested_at: float,
) -> dict[str, Any]:
    state_dir = _runtime_state_dir(bridge_home)
    operation_id = str(record["id"])
    marker_path = state_dir / "legacy-reboot-handoffs" / f"{operation_id}.json"
    request_path = state_dir / "kernel-requests" / f"{operation_id}.json"
    replacement_path = state_dir / f"replacement-{operation_id}.json"
    marker = {
        "schema": 1,
        "operation_id": operation_id,
        "requested_at": float(requested_at),
        "old_shared_pid": int(shared_pid),
        "old_shared_generation_id": shared_generation_id,
        "expected_generation_id": generation_id,
        "leader_agent": agent_name,
        "worker_pid": int(worker_pid),
    }
    existing = None
    if marker_path.exists():
        existing = _validate_marker(
            _read_json_object(marker_path, max_bytes=MAX_MARKER_BYTES)
        )
        immutable = {
            key: value for key, value in marker.items() if key != "requested_at"
        }
        existing_immutable = {
            key: value for key, value in existing.items() if key != "requested_at"
        }
        if existing_immutable != immutable:
            raise ValueError("legacy reboot handoff marker conflicts with candidate")
    else:
        write_record(marker_path, marker)

    # The marker is durable before publication. The same live leader may retry
    # a failed write, while a recovered Worker never duplicates an in-flight
    # Core replacement. A committed Core receipt always wins.
    if replacement_path.exists() or request_path.exists():
        return existing or marker
    if existing is not None and existing.get("worker_pid") != os.getpid():
        return existing
    write_record(request_path, {"id": operation_id})
    logger.info(
        "Legacy broad reboot promoted to shared Function handoff: operation=%s",
        operation_id,
    )
    return marker


async def bridge_legacy_broad_reboot(
    bridge_home: Path | str,
    *,
    agent_name: str,
    worker_pid: int,
    generation_id: str,
    shared_pid: int,
    shared_generation_id: str,
    worker_started_at: float,
    stop_event: asyncio.Event | None = None,
    now: Callable[[], float] = time.time,
    poll_interval: float = BRIDGE_POLL_SECONDS,
    timeout: float = BRIDGE_WAIT_SECONDS,
) -> dict[str, Any] | None:
    """Publish one legacy broad handoff after its Worker transaction succeeds."""

    deadline = time.monotonic() + max(0.0, float(timeout))
    first = True
    while first or time.monotonic() <= deadline:
        first = False
        if stop_event is not None and stop_event.is_set():
            return None
        moment = float(now())
        record = _legacy_candidate(
            bridge_home,
            agent_name=agent_name,
            worker_pid=worker_pid,
            generation_id=generation_id,
            shared_pid=shared_pid,
            shared_generation_id=shared_generation_id,
            worker_started_at=worker_started_at,
            now=moment,
        )
        if record is not None:
            try:
                return _publish_candidate(
                    bridge_home,
                    record,
                    agent_name=agent_name,
                    worker_pid=worker_pid,
                    generation_id=generation_id,
                    shared_pid=shared_pid,
                    shared_generation_id=shared_generation_id,
                    requested_at=moment,
                )
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                logger.warning("Legacy broad reboot bridge retrying: %s", exc)
        remaining = deadline - time.monotonic()
        if remaining <= 0 or poll_interval <= 0:
            break
        delay = min(float(poll_interval), remaining)
        if stop_event is None:
            await asyncio.sleep(delay)
            continue
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
        except TimeoutError:
            continue
        return None
    return None
