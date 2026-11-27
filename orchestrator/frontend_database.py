"""Bounded FC database work with lightweight slow-query & spin alerting.

SQLite connections stay inside their owning call. No extra monitor thread or
background daemon is created; alerts are derived in O(1) from bounded
sliding-window samples and projected read-only into /api/health and Worker
metadata.
"""
from __future__ import annotations

import asyncio
import contextvars
import hashlib
import logging
import os
import sqlite3
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)
_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="frontend-db")
_SLOTS = threading.BoundedSemaphore(8)

# Thresholds for long-term abnormal resource / slow-DB alerting.
SLOW_DB_INFO_SECONDS = 0.1
SEVERE_DB_WARN_SECONDS = 0.5
SUSTAINED_SLOW_WINDOW_SECONDS = 60.0
SUSTAINED_SLOW_COUNT_THRESHOLD = 5
HIGH_DUTY_CYCLE_THRESHOLD = 0.05  # >= 5% of wall clock spent in one DB op over 60s
# Zero-progress loop spin is a duty-cycle question, not a per-scan latency one:
# a loop that scans slowly but then sleeps is idle, while a tight loop that
# never yields burns wall clock.  Both directions were previously misjudged.
ZERO_PROGRESS_SPIN_DUTY_CYCLE_THRESHOLD = 0.8
ZERO_PROGRESS_SPIN_MIN_BUSY_SECONDS = 1.0
ZERO_PROGRESS_SPIN_MIN_ITERATIONS = 3
ALERT_LOG_COOLDOWN_SECONDS = 60.0

_METRICS_LOCK = threading.Lock()
# Per-operation deque of (monotonic_ts, duration_s, queued_s, instance, role)
_OP_SAMPLES: dict[str, deque[tuple[float, float, float, str, str]]] = {}
# Per-operation zero-progress loop state: {"streak": int, "busy_s": float, "window_start_ts": float, "last_ts": float, "instance": str, "role": str}
_LOOP_SPIN: dict[str, dict[str, Any]] = {}
# Rate-limiting for WARNING log emissions: (operation, code) -> last_warn_monotonic
_LAST_ALERT_LOG: dict[tuple[str, str], float] = {}
# Aggregate monotonic counters (never reset except in tests)
_COUNTERS: dict[str, int] = {
    "total_calls": 0,
    "slow_calls": 0,
    "severe_calls": 0,
    "alerts_emitted": 0,
}
# Edge-triggered resource alert listeners: callback(alerts_list) called when
# active alert keys transition (appear or clear). Zero background threads.
_ACTIVE_ALERT_KEYS: set[tuple[str, str]] = set()
_ALERT_LISTENERS: list[Any] = []


class DatabaseWorkBusy(RuntimeError):
    """Admission or BEGIN failed before any write; no operation was replayed."""


def is_sqlite_busy(error):
    return isinstance(error, sqlite3.OperationalError) and (
        getattr(error, "sqlite_errorcode", 0) & 255
    ) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}


def register_resource_alert_listener(listener: Any) -> None:
    """Register an edge-triggered callback invoked when active alerts change."""
    with _METRICS_LOCK:
        if listener not in _ALERT_LISTENERS:
            _ALERT_LISTENERS.append(listener)


def unregister_resource_alert_listener(listener: Any) -> None:
    with _METRICS_LOCK:
        _ALERT_LISTENERS[:] = [
            existing for existing in _ALERT_LISTENERS if existing != listener
        ]


def reset_database_work_metrics(*, clear_listeners: bool = False) -> None:
    """Reset in-process sliding-window metrics (intended for unit tests)."""
    with _METRICS_LOCK:
        _OP_SAMPLES.clear()
        _LOOP_SPIN.clear()
        _LAST_ALERT_LOG.clear()
        _ACTIVE_ALERT_KEYS.clear()
        if clear_listeners:
            _ALERT_LISTENERS.clear()
        for key in _COUNTERS:
            _COUNTERS[key] = 0


def _prune_samples_locked(samples: deque[tuple[float, float, float, str, str]], cutoff: float) -> None:
    while samples and samples[0][0] < cutoff:
        samples.popleft()


def _maybe_warn_locked(
    *,
    now: float,
    operation: str,
    code: str,
    message: str,
    args: tuple[Any, ...],
) -> None:
    key = (operation, code)
    last = _LAST_ALERT_LOG.get(key, 0.0)
    if now - last < ALERT_LOG_COOLDOWN_SECONDS:
        return
    _LAST_ALERT_LOG[key] = now
    _COUNTERS["alerts_emitted"] += 1
    logger.warning(message, *args)


def record_database_work_sample(
    *,
    operation: str,
    instance: str,
    role: str,
    duration_s: float,
    queued_s: float = 0.0,
    now: float | None = None,
) -> None:
    """Record one completed DB work sample and escalate to WARNING when abnormal."""
    ts = time.monotonic() if now is None else float(now)
    dur = max(0.0, float(duration_s))
    q = max(0.0, float(queued_s))
    total = dur + q
    cutoff = ts - SUSTAINED_SLOW_WINDOW_SECONDS

    with _METRICS_LOCK:
        _COUNTERS["total_calls"] += 1
        samples = _OP_SAMPLES.setdefault(operation, deque(maxlen=512))
        samples.append((ts, dur, q, instance, role))
        _prune_samples_locked(samples, cutoff)

        if total >= SLOW_DB_INFO_SECONDS:
            _COUNTERS["slow_calls"] += 1
        if total >= SEVERE_DB_WARN_SECONDS:
            _COUNTERS["severe_calls"] += 1
            _maybe_warn_locked(
                now=ts,
                operation=operation,
                code="slow_database_work",
                message=(
                    "Resource alert [slow_database_work]: operation=%s instance=%s "
                    "pid=%s role=%s duration_ms=%.1f queue_ms=%.1f exceeds %.0fms threshold"
                ),
                args=(
                    operation,
                    instance,
                    os.getpid(),
                    role,
                    dur * 1000.0,
                    q * 1000.0,
                    SEVERE_DB_WARN_SECONDS * 1000.0,
                ),
            )

        slow_in_window = sum(1 for s in samples if (s[1] + s[2]) >= SLOW_DB_INFO_SECONDS)
        if slow_in_window >= SUSTAINED_SLOW_COUNT_THRESHOLD:
            avg_ms = (sum(s[1] + s[2] for s in samples) / len(samples)) * 1000.0
            _maybe_warn_locked(
                now=ts,
                operation=operation,
                code="slow_database_work",
                message=(
                    "Resource alert [slow_database_work]: sustained slow DB work "
                    "operation=%s instance=%s pid=%s role=%s slow_calls_60s=%d avg_ms=%.1f"
                ),
                args=(operation, instance, os.getpid(), role, slow_in_window, avg_ms),
            )

        window_db_s = sum(s[1] for s in samples)
        duty_cycle = window_db_s / SUSTAINED_SLOW_WINDOW_SECONDS
        if duty_cycle >= HIGH_DUTY_CYCLE_THRESHOLD:
            _maybe_warn_locked(
                now=ts,
                operation=operation,
                code="high_database_duty_cycle",
                message=(
                    "Resource alert [high_database_duty_cycle]: operation=%s instance=%s "
                    "pid=%s role=%s db_time_60s=%.2fs duty_cycle=%.1f%%"
                ),
                args=(
                    operation,
                    instance,
                    os.getpid(),
                    role,
                    window_db_s,
                    duty_cycle * 100.0,
                ),
            )
        notify_listeners, alerts_snapshot = _detect_alert_transition_locked(
            ts, wall_now=time.time()
        )
    _invoke_alert_listeners(notify_listeners, alerts_snapshot)


def record_loop_zero_progress(
    *,
    operation: str,
    instance: str,
    role: str,
    duration_s: float,
    items_processed: int,
    now: float | None = None,
) -> None:
    """Detect background scan loops burning wall clock without progress.

    Each iteration is judged by how much of the elapsed wall clock it occupied.
    A loop that finishes quickly and then waits between iterations is idle even
    when a single scan was slow (queueing, cold cache), and a tight loop that
    never yields is reported as soon as it has burned enough wall clock.
    """
    ts = time.monotonic() if now is None else float(now)
    dur = max(0.0, float(duration_s))
    with _METRICS_LOCK:
        if items_processed > 0:
            _LOOP_SPIN.pop(operation, None)
            notify_listeners, alerts_snapshot = _detect_alert_transition_locked(
                ts, wall_now=time.time()
            )
        else:
            entry = _LOOP_SPIN.get(operation)
            if entry is None:
                entry = {
                    "streak": 0,
                    "busy_s": 0.0,
                    "window_start_ts": ts,
                    "last_ts": ts,
                    "instance": instance,
                    "role": role,
                }
                _LOOP_SPIN[operation] = entry
            else:
                gap_s = ts - float(entry["last_ts"])
                if gap_s > SUSTAINED_SLOW_WINDOW_SECONDS:
                    # The loop went quiet long enough to be considered idle:
                    # start a fresh observation window instead of carrying a
                    # stale streak across the sleep.
                    entry["streak"] = 0
                    entry["busy_s"] = 0.0
                    entry["window_start_ts"] = ts
                else:
                    occupied_s = max(gap_s, dur)
                    iteration_duty = dur / occupied_s if occupied_s > 0 else 1.0
                    if iteration_duty >= ZERO_PROGRESS_SPIN_DUTY_CYCLE_THRESHOLD:
                        entry["streak"] += 1
                        entry["busy_s"] += dur
                    else:
                        # A real wait broke the streak; this iteration becomes
                        # the baseline of a new window.
                        entry["streak"] = 0
                        entry["busy_s"] = 0.0
                        entry["window_start_ts"] = ts
                entry["last_ts"] = ts
                entry["instance"] = instance
                entry["role"] = role
            streak = int(entry["streak"])
            busy_s = float(entry["busy_s"])
            wall_s = max(0.0, ts - float(entry["window_start_ts"]))
            duty_cycle = busy_s / wall_s if wall_s > 0 else 0.0
            if (
                streak >= ZERO_PROGRESS_SPIN_MIN_ITERATIONS
                and busy_s >= ZERO_PROGRESS_SPIN_MIN_BUSY_SECONDS
                and duty_cycle >= ZERO_PROGRESS_SPIN_DUTY_CYCLE_THRESHOLD
            ):
                _maybe_warn_locked(
                    now=ts,
                    operation=operation,
                    code="idle_loop_spin",
                    message=(
                        "Resource alert [idle_loop_spin]: operation=%s instance=%s pid=%s "
                        "role=%s zero_progress_iterations=%d busy_s=%.2f wall_s=%.2f "
                        "duty_cycle=%.1f%% (threshold=%.0f%%): the scan loop covered "
                        "most of its wall clock with zero deliveries"
                    ),
                    args=(
                        operation,
                        instance,
                        os.getpid(),
                        role,
                        streak,
                        busy_s,
                        wall_s,
                        duty_cycle * 100.0,
                        ZERO_PROGRESS_SPIN_DUTY_CYCLE_THRESHOLD * 100.0,
                    ),
                )
            notify_listeners, alerts_snapshot = _detect_alert_transition_locked(
                ts, wall_now=time.time()
            )
    _invoke_alert_listeners(notify_listeners, alerts_snapshot)


def _collect_alerts_locked(ts: float, *, wall_now: float) -> list[dict[str, Any]]:
    cutoff = ts - SUSTAINED_SLOW_WINDOW_SECONDS
    alerts: list[dict[str, Any]] = []
    for operation, samples in list(_OP_SAMPLES.items()):
        _prune_samples_locked(samples, cutoff)
        if not samples:
            continue
        latest_ts = samples[-1][0]
        latest_instance = samples[-1][3]
        latest_role = samples[-1][4]
        remaining_ttl_s = max(
            1.0,
            SUSTAINED_SLOW_WINDOW_SECONDS - max(0.0, ts - latest_ts),
        )
        observed_at = round(wall_now, 3)
        expires_at = round(wall_now + remaining_ttl_s, 3)
        max_total_s = max(s[1] + s[2] for s in samples)
        slow_count = sum(1 for s in samples if (s[1] + s[2]) >= SLOW_DB_INFO_SECONDS)
        window_db_s = sum(s[1] for s in samples)
        duty_cycle = window_db_s / SUSTAINED_SLOW_WINDOW_SECONDS
        if max_total_s >= SEVERE_DB_WARN_SECONDS or slow_count >= SUSTAINED_SLOW_COUNT_THRESHOLD:
            alerts.append(
                {
                    "code": "slow_database_work",
                    "category": "performance",
                    "component": "database",
                    "severity": "warning",
                    "operation": operation,
                    "instance": latest_instance,
                    "role": latest_role,
                    "pid": os.getpid(),
                    "observed_at": observed_at,
                    "expires_at": expires_at,
                    "summary": (
                        f"Slow database work detected on {operation} "
                        f"(slow_calls_60s={slow_count}, max_ms={max_total_s * 1000.0:.1f})."
                    ),
                    "details": {
                        "operation": operation,
                        "slow_calls_60s": slow_count,
                        "calls_60s": len(samples),
                        "max_ms": round(max_total_s * 1000.0, 1),
                        "window_db_ms": round(window_db_s * 1000.0, 1),
                    },
                }
            )
        if duty_cycle >= HIGH_DUTY_CYCLE_THRESHOLD:
            alerts.append(
                {
                    "code": "high_database_duty_cycle",
                    "category": "performance",
                    "component": "database",
                    "severity": "warning",
                    "operation": operation,
                    "instance": latest_instance,
                    "role": latest_role,
                    "pid": os.getpid(),
                    "observed_at": observed_at,
                    "expires_at": expires_at,
                    "summary": (
                        f"High database duty cycle on {operation} "
                        f"({duty_cycle * 100.0:.1f}% over 60s)."
                    ),
                    "details": {
                        "operation": operation,
                        "duty_cycle_pct": round(duty_cycle * 100.0, 2),
                        "window_db_ms": round(window_db_s * 1000.0, 1),
                        "calls_60s": len(samples),
                    },
                }
            )
    for operation, entry in list(_LOOP_SPIN.items()):
        age_s = ts - float(entry["last_ts"])
        if age_s > SUSTAINED_SLOW_WINDOW_SECONDS:
            continue
        streak = int(entry["streak"])
        busy_s = float(entry["busy_s"])
        wall_s = max(0.0, float(entry["last_ts"]) - float(entry["window_start_ts"]))
        duty_cycle = busy_s / wall_s if wall_s > 0 else 0.0
        if (
            streak >= ZERO_PROGRESS_SPIN_MIN_ITERATIONS
            and busy_s >= ZERO_PROGRESS_SPIN_MIN_BUSY_SECONDS
            and duty_cycle >= ZERO_PROGRESS_SPIN_DUTY_CYCLE_THRESHOLD
        ):
            remaining_ttl_s = max(1.0, SUSTAINED_SLOW_WINDOW_SECONDS - max(0.0, age_s))
            alerts.append(
                {
                    "code": "idle_loop_spin",
                    "category": "performance",
                    "component": "background_loop",
                    "severity": "warning",
                    "operation": operation,
                    "instance": entry["instance"],
                    "role": entry["role"],
                    "pid": os.getpid(),
                    "observed_at": round(wall_now, 3),
                    "expires_at": round(wall_now + remaining_ttl_s, 3),
                    "summary": (
                        f"Zero-progress loop spin on {operation} "
                        f"({duty_cycle * 100.0:.1f}% of {wall_s:.1f}s wall clock busy "
                        f"across {streak} zero-result iterations)."
                    ),
                    "details": {
                        "operation": operation,
                        "zero_progress_streak": streak,
                        "zero_progress_iterations": streak,
                        "busy_s": round(busy_s, 3),
                        "wall_s": round(wall_s, 3),
                        "duty_cycle_pct": round(duty_cycle * 100.0, 1),
                        "threshold_pct": round(
                            ZERO_PROGRESS_SPIN_DUTY_CYCLE_THRESHOLD * 100.0, 1
                        ),
                        "avg_ms": round((busy_s / max(1, streak)) * 1000.0, 1),
                    },
                }
            )
    return alerts


def _detect_alert_transition_locked(
    ts: float, *, wall_now: float
) -> tuple[tuple[Any, ...], list[dict[str, Any]]]:
    alerts = _collect_alerts_locked(ts, wall_now=wall_now)
    current_keys = {(str(a["operation"]), str(a["code"])) for a in alerts}
    if current_keys == _ACTIVE_ALERT_KEYS:
        return (), []
    _ACTIVE_ALERT_KEYS.clear()
    _ACTIVE_ALERT_KEYS.update(current_keys)
    return tuple(_ALERT_LISTENERS), [dict(a) for a in alerts]


def _invoke_alert_listeners(
    listeners: tuple[Any, ...], alerts: list[dict[str, Any]]
) -> None:
    for listener in listeners:
        try:
            listener(alerts)
        except Exception:
            continue


def resource_alerts(
    *, now: float | None = None, wall_now: float | None = None
) -> list[dict[str, Any]]:
    """Return active abnormal DB/idle-spin alerts within the sliding window (read-only)."""
    ts = time.monotonic() if now is None else float(now)
    wall = time.time() if wall_now is None else float(wall_now)
    with _METRICS_LOCK:
        return _collect_alerts_locked(ts, wall_now=wall)


def database_work_health_snapshot(
    *, now: float | None = None, wall_now: float | None = None
) -> dict[str, Any]:
    """Read-only process-local DB & idle-loop health projection."""
    ts = time.monotonic() if now is None else float(now)
    wall = time.time() if wall_now is None else float(wall_now)
    cutoff = ts - SUSTAINED_SLOW_WINDOW_SECONDS
    operations: dict[str, dict[str, Any]] = {}
    with _METRICS_LOCK:
        alerts = _collect_alerts_locked(ts, wall_now=wall)
        counters = dict(_COUNTERS)
        for operation, samples in list(_OP_SAMPLES.items()):
            _prune_samples_locked(samples, cutoff)
            if not samples:
                continue
            window_db_s = sum(s[1] for s in samples)
            operations[operation] = {
                "calls_60s": len(samples),
                "slow_calls_60s": sum(
                    1 for s in samples if (s[1] + s[2]) >= SLOW_DB_INFO_SECONDS
                ),
                "max_ms": round(max(s[1] + s[2] for s in samples) * 1000.0, 1),
                "duty_cycle_pct": round(
                    (window_db_s / SUSTAINED_SLOW_WINDOW_SECONDS) * 100.0, 2
                ),
            }
    return {
        "pid": os.getpid(),
        "observed_at": round(wall, 3),
        "counters": counters,
        "operations": operations,
        "alerts": alerts,
    }


async def run_database_work(function, /, *args, _db_role="frontend", _db_instance=None, _db_operation=None, **kwargs):
    """At most two running/eight admitted calls per process, including cancelled calls.

    Do not cancel/release a running write when its HTTP/IPC caller disappears.
    Settle it first and propagate cancellation without replaying its effect.
    No request/response/Telegram objects are passed by this executor.
    """
    operation = _db_operation or getattr(function, "__qualname__", type(function).__name__)
    bound = getattr(function, "__self__", None)
    instance = _db_instance or getattr(bound, "instance_id", None)
    if instance is None and args:
        instance = getattr(args[0], "instance_id", None)
    instance = instance or "unknown"
    scope = next((str(kwargs[key]) for key in ("request_id", "question_id", "event_id", "session_id")
                  if kwargs.get(key)), uuid4().hex)
    correlation = hashlib.sha256(scope.encode()).hexdigest()[:16]
    queued_at = time.monotonic()
    context = contextvars.copy_context()

    def call():
        started = time.monotonic()
        try:
            return context.run(partial(function, *args, **kwargs))
        finally:
            duration = time.monotonic() - started
            queued = started - queued_at
            record_database_work_sample(
                operation=operation,
                instance=instance,
                role=_db_role,
                duration_s=duration,
                queued_s=queued,
            )
            if duration + queued >= SLOW_DB_INFO_SECONDS:
                logger.info("frontend.database_work operation=%s instance=%s pid=%s role=%s correlation=%s queue_ms=%.1f duration_ms=%.1f",
                            operation, instance, os.getpid(), _db_role, correlation, queued * 1000, duration * 1000)

    if not _SLOTS.acquire(blocking=False):
        raise DatabaseWorkBusy("database work capacity reached")
    try:
        submitted = _EXECUTOR.submit(call)
    except BaseException:
        _SLOTS.release()
        raise
    submitted.add_done_callback(lambda _: _SLOTS.release())
    future = asyncio.wrap_future(submitted)
    cancelled = False
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break
    if cancelled:
        # Retrieve any storage error; the abandoned HTTP request is not a receipt.
        try:
            future.result()
        except Exception:
            logger.warning("frontend.database_work abandoned operation=%s correlation=%s outcome=unconfirmed",
                           operation, correlation)
        raise asyncio.CancelledError
    return future.result()
# HASHI-Fingerprint: hfp_3bcedf8ba47f43a1813c4a921cd50593
