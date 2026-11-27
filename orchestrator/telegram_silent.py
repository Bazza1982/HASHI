"""Instance-wide Telegram safety silence.

``/telegram off global`` only stops outbound mirrors: conversations still
receive messages and still get their own replies.  The safety silence is a
superset: inbound updates are dropped at the single ingest point, outbound
delivery is suppressed at send time, and the Bot token plus its connection are
left untouched so recovery is instant.

Nothing here may stop the connector.  Telegram retains undelivered updates and
would replay the whole backlog the moment the silence is lifted, which is the
opposite of a controlled cut-off.

Reads fail open: a damaged preference document must not silence an instance on
its own.  Every enabling mutation is verified by its caller instead.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

logger = logging.getLogger("HASHI.Telegram.Silence")

# Bound the in-memory drop counters: at most one entry per instance root.
_MAX_TRACKED_ROOTS = 32
_dropped: dict[str, int] = {}
_last_notice: ContextVar[bool] = ContextVar("telegram_silent_last_notice", default=False)


def bridge_home(runtime: Any) -> Any:
    """Return the instance root the runtime belongs to."""

    return getattr(getattr(runtime, "global_config", None), "bridge_home", None)


def silenced_home(bridge_home_value: Any) -> bool:
    """Report whether this instance root is under the safety silence."""

    if bridge_home_value is None:
        return False
    try:
        from orchestrator.connector_delivery_preferences import (
            instance_telegram_silenced,
        )

        return instance_telegram_silenced(bridge_home_value, default=False)
    except Exception:  # a broken store must not answer "silenced"
        return False


def silenced(runtime: Any) -> bool:
    return silenced_home(bridge_home(runtime))


def suppressed(runtime: Any) -> bool:
    """True when this outbound message must not leave the instance.

    One message is exempt: the receipt that announces the silence change
    itself, wrapped in :func:`last_notice`.
    """

    if _last_notice.get():
        return False
    return silenced(runtime)


@contextmanager
def last_notice() -> Iterator[None]:
    """Allow exactly the receipt that reports a silence change."""

    token = _last_notice.set(True)
    try:
        yield
    finally:
        _last_notice.reset(token)


def record_dropped(runtime: Any) -> int:
    """Count one dropped inbound update and report the running total."""

    key = str(bridge_home(runtime) or "")
    count = _dropped.get(key, 0) + 1
    if key not in _dropped and len(_dropped) >= _MAX_TRACKED_ROOTS:
        # Never grow the map without bound; the log line still records the
        # drop, which is the durable evidence.
        logger.warning(
            "Telegram silence dropped an update (%s); counters at capacity", key
        )
        return count
    _dropped[key] = count
    if count == 1 or count % 25 == 0:
        logger.warning(
            "Telegram silence dropped %s inbound update(s) for %s",
            count,
            key or "(unknown root)",
        )
    return count


def dropped_since_start(runtime: Any) -> int:
    """Dropped updates seen by this process; resets on Worker restart."""

    return _dropped.get(str(bridge_home(runtime) or ""), 0)


def reset_counters() -> None:
    """Test hook: clear the per-process drop counters."""

    _dropped.clear()


__all__ = [
    "bridge_home",
    "dropped_since_start",
    "last_notice",
    "record_dropped",
    "reset_counters",
    "silenced",
    "silenced_home",
    "suppressed",
]
# HASHI-Fingerprint: hfp_37fc07f331704c6494eb345cd63de856
