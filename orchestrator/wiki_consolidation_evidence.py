"""Fail-closed completion evidence for the instance-local Wiki pipeline."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ConsolidationEvidenceConfig(Protocol):
    """Configuration fields consumed by the consolidation evidence gate."""

    consolidation_log: Path
    timezone: str


def check_today_consolidation(
    config: ConsolidationEvidenceConfig,
    now: datetime,
) -> tuple[bool, str]:
    """Require the latest local-day scan to precede a clean embed outcome.

    Scan and embed ordering is derived from timezone-aware event timestamps, with
    line order used only to break equal-timestamp ties.  Any unreadable or
    malformed evidence fails closed instead of allowing a stale success event.
    """

    if not config.consolidation_log.exists():
        return False, f"missing consolidation log: {config.consolidation_log}"

    try:
        timezone = ZoneInfo(config.timezone)
    except (TypeError, ZoneInfoNotFoundError) as exc:
        return False, f"invalid consolidation timezone: {exc}"

    local_now = (
        now.replace(tzinfo=timezone) if now.tzinfo is None else now.astimezone(timezone)
    )
    today = local_now.date()
    scan_events: list[tuple[tuple[datetime, int], int]] = []
    embed_events: list[tuple[tuple[datetime, int], str, int]] = []

    try:
        lines = config.consolidation_log.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        return False, f"unable to read consolidation log: {exc}"

    def require_count(event: dict[str, object], key: str) -> int:
        value = event.get(key)
        if type(value) is not int or value < 0:
            raise ValueError(f"{key} must be a non-negative integer")
        return value

    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError("event must be a JSON object")
            timestamp = event["timestamp"]
            if not isinstance(timestamp, str):
                raise ValueError("timestamp must be a string")
            parsed_timestamp = datetime.fromisoformat(timestamp)
            if parsed_timestamp.tzinfo is None:
                raise ValueError("timestamp must include a timezone offset")
            event_timestamp = parsed_timestamp.astimezone(timezone)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            return False, f"bad log line {line_number}: {exc}"

        if event_timestamp.date() != today:
            continue

        event_key = (event_timestamp, line_number)
        phase = event.get("phase")
        try:
            if phase is not None and not isinstance(phase, str):
                raise ValueError("phase must be a string")
            if "new_inserted" in event:
                if phase is not None:
                    raise ValueError("scan event must not declare an embed phase")
                require_count(event, "new_inserted")
                scan_events.append((event_key, require_count(event, "errors")))
            elif phase in ("embed", "embed_error"):
                require_count(event, "embedded")
                embed_events.append(
                    (event_key, str(phase), require_count(event, "errors"))
                )
            else:
                raise ValueError("unrecognized consolidation event")
        except ValueError as exc:
            return False, f"bad log line {line_number}: {exc}"

    if not scan_events:
        return False, f"no consolidation scan event for local date {today.isoformat()}"

    latest_scan_key, latest_scan_errors = max(scan_events, key=lambda event: event[0])
    latest_scan = latest_scan_key[0]
    if latest_scan_errors:
        suffix = "" if latest_scan_errors == 1 else "s"
        return False, (
            f"today latest scan at {latest_scan.isoformat()} reported "
            f"{latest_scan_errors} error{suffix}"
        )

    following_embeds = [event for event in embed_events if event[0] > latest_scan_key]
    if not following_embeds:
        return (
            False,
            f"today scan found at {latest_scan.isoformat()} but embed phase not complete",
        )

    latest_embed_key, latest_embed_phase, latest_embed_errors = max(
        following_embeds,
        key=lambda event: event[0],
    )
    latest_embed = latest_embed_key[0]
    if latest_embed_phase != "embed" or latest_embed_errors:
        suffix = "" if latest_embed_errors == 1 else "s"
        return False, (
            f"today latest embed outcome at {latest_embed.isoformat()} is "
            f"{latest_embed_phase} with {latest_embed_errors} error{suffix}"
        )

    return True, f"today embed completed at {latest_embed.isoformat()}"


__all__ = ["ConsolidationEvidenceConfig", "check_today_consolidation"]
