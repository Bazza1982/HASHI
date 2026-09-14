"""Host-independent timezone policy for HASHI Function owners.

Absolute events are represented as aware UTC datetimes (or epoch seconds).
Wall-clock rules retain an IANA timezone.  Ambiguous recurring wall times use
the first occurrence (``fold=0``); nonexistent wall times advance to the first
valid minute.  Callers handling one-time user input may request fail-closed gap
handling instead.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


UTC_TIMEZONE_NAME = "UTC"


class TimezonePolicyError(ValueError):
    """Raised when an IANA timezone or local wall time is invalid."""


def canonical_timezone_name(
    value: object,
    *,
    default: str = UTC_TIMEZONE_NAME,
) -> str:
    """Return a validated IANA timezone name without consulting the host."""

    name = str(value or default).strip() or str(default).strip()
    try:
        zone = ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise TimezonePolicyError(f"Unknown IANA timezone: {name!r}") from exc
    return str(getattr(zone, "key", None) or name)


def timezone_for_name(value: object, *, default: str = UTC_TIMEZONE_NAME) -> tzinfo:
    return ZoneInfo(canonical_timezone_name(value, default=default))


def _valid_wall_candidates(wall: datetime, zone: tzinfo) -> list[datetime]:
    naive = wall.replace(tzinfo=None)
    candidates: list[datetime] = []
    seen: set[tuple[object, object]] = set()
    for fold in (0, 1):
        candidate = naive.replace(tzinfo=zone, fold=fold)
        round_trip = (
            candidate.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
        )
        if round_trip != naive:
            continue
        identity = (candidate.utcoffset(), candidate.dst())
        if identity not in seen:
            seen.add(identity)
            candidates.append(candidate)
    return candidates


def resolve_local_wall_time(
    value: datetime,
    timezone_name: object,
    *,
    fold: int = 0,
    gap_policy: str = "next_valid",
) -> datetime:
    """Resolve one naive wall time using explicit fold and gap policies."""

    name = canonical_timezone_name(timezone_name)
    zone = timezone_for_name(name)
    wall = value.replace(tzinfo=None)
    candidates = _valid_wall_candidates(wall, zone)
    if candidates:
        if len(candidates) == 1:
            return candidates[0]
        return candidates[1 if int(fold) else 0]
    if gap_policy == "reject":
        raise TimezonePolicyError(
            f"Local wall time {wall.isoformat()} does not exist in {name}."
        )
    if gap_policy != "next_valid":
        raise TimezonePolicyError(f"Unsupported DST gap policy: {gap_policy!r}")
    for minutes in range(1, 181):
        shifted = wall + timedelta(minutes=minutes)
        candidates = _valid_wall_candidates(shifted, zone)
        if candidates:
            return candidates[0]
    raise TimezonePolicyError(
        f"Could not resolve DST gap near {wall.isoformat()} in {name}."
    )


def aware_in_timezone(value: datetime, timezone_name: object) -> datetime:
    """Interpret a naive datetime as wall time, or convert an aware instant."""

    zone = timezone_for_name(timezone_name)
    if value.tzinfo is None:
        return resolve_local_wall_time(value, timezone_name)
    return value.astimezone(zone)


def utc_datetime_from_epoch(value: float | int) -> datetime:
    return datetime.fromtimestamp(float(value), tz=timezone.utc)


def parse_absolute_timestamp(
    value: Any,
    *,
    naive_timezone: object | None = None,
) -> datetime | None:
    """Parse one absolute timestamp to UTC without a host-timezone fallback.

    Legacy naive ISO strings are rejected unless their known source timezone is
    supplied explicitly.
    """

    if isinstance(value, (int, float)):
        try:
            return utc_datetime_from_epoch(value)
        except (ValueError, OSError, OverflowError):
            return None
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return utc_datetime_from_epoch(float(text))
    except (ValueError, OSError, OverflowError):
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (ValueError, OSError, OverflowError):
        return None
    if parsed.tzinfo is None:
        if naive_timezone is None or not str(naive_timezone).strip():
            return None
        try:
            parsed = resolve_local_wall_time(parsed, naive_timezone)
        except TimezonePolicyError:
            return None
    return parsed.astimezone(timezone.utc)


def format_epoch(
    epoch: float | int,
    *,
    timezone_name: object = UTC_TIMEZONE_NAME,
    timespec: str = "seconds",
    include_iana: bool = True,
) -> str:
    """Render an epoch in one explicit named timezone."""

    name = canonical_timezone_name(timezone_name)
    local = utc_datetime_from_epoch(epoch).astimezone(timezone_for_name(name))
    rendered = local.isoformat(timespec=timespec)
    if not include_iana:
        return rendered
    abbreviation = local.tzname() or name
    return f"{rendered} {abbreviation} [{name}]"
