"""Per-turn Office presentation, shared by HERV3 and local API adapters.

Hints never authorize tools or paths. Runtime availability and request-local
allowlists are applied by the same ToolRegistry after selecting the tier.
"""
from __future__ import annotations

from collections.abc import Mapping
from .contract import OFFICE_TOOL_NAMES
from .task_signal import office_task_hint


def stage_tiers(tiers, goal="", attachments=()):
    if tiers is None:
        return None
    selected = list(tiers)
    if office_task_hint(str(goal), attachments)["activate"] and "office_basic" not in selected:
        selected.append("office_basic")
    return selected


def message_tiers(tiers, messages):
    selected = list(tiers)
    recent = list(messages)[-12:]
    latest = next((m for m in reversed(recent) if m.get("role") == "user"), {})
    content = latest.get("content", "")
    text = content if isinstance(content, str) else " ".join(
        str(p.get("text", "")) for p in content if isinstance(p, Mapping))
    attachments = [] if isinstance(content, str) else [
        p for p in content if isinstance(p, Mapping) and (p.get("filename") or p.get("name"))]
    active = office_task_hint(text, attachments)["activate"]
    # Continue a recent Office lifecycle, including "send that file" followups.
    active = active or any(
        m.get("name") in OFFICE_TOOL_NAMES or m.get("tool_name") in OFFICE_TOOL_NAMES or
        any((c.get("function") or {}).get("name") in OFFICE_TOOL_NAMES for c in m.get("tool_calls", ()))
        for m in recent)
    if active and "office_basic" not in selected:
        selected.append("office_basic")
    return selected
