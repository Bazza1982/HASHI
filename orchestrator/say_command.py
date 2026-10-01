"""Bounded selection of final Agent replies for connector-local speech."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence


@dataclass(frozen=True)
class SaySelection:
    start: int = 1
    end: int = 1


def parse_say_selection(arguments: Sequence[str]) -> SaySelection:
    """Parse /say N or /say A-B; positions count from the newest reply."""
    if not arguments:
        return SaySelection()
    if len(arguments) != 1:
        raise ValueError("invalid /say selection")
    value = str(arguments[0]).strip()
    if value in {"1", "2", "3", "4"}:
        return SaySelection(1, int(value))
    if len(value) == 3 and value[1] == "-" and value[0] in "1234" and value[2] in "1234":
        start, end = int(value[0]), int(value[2])
        if start <= end:
            return SaySelection(start, end)
    raise ValueError("invalid /say selection")


def is_speakable_final_reply(message: Mapping[str, object]) -> bool:
    """Treat presentation events as display material, never Agent speech."""
    if message.get("role") != "assistant" or not str(message.get("text") or "").strip():
        return False
    if message.get("history_eligible") is False or message.get("presentation_only") is True:
        return False
    kind = str(message.get("kind") or "").strip().casefold()
    channel = str(message.get("channel") or message.get("presentation_channel") or "").strip().casefold()
    source = str(message.get("source") or "").strip().casefold()
    if source in {"meter-cost", "system", "workbench-local-speech"}:
        return False
    if kind and kind != "final":
        return False
    return not channel or channel == "final"


def select_recent_replies(
    messages: Sequence[Mapping[str, object]], selection: SaySelection
) -> tuple[list[dict[str, object]], int]:
    """Return selected replies oldest first and the available recent count."""
    newest: list[dict[str, object]] = []
    seen: set[str] = set()
    for message in reversed(messages):
        if not is_speakable_final_reply(message):
            continue
        identity = str(
            message.get("message_id") or message.get("message_ref") or message.get("run_id") or ""
        )
        if identity and identity in seen:
            continue
        if identity:
            seen.add(identity)
        newest.append(dict(message))
        if len(newest) >= selection.end:
            break
    return list(reversed(newest[selection.start - 1 : selection.end])), len(newest)
