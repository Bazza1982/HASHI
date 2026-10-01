"""Deterministic structure cues derived from a saved report, never new facts."""
from __future__ import annotations

import re


_NUMBERED = re.compile(r"^\s*(?:\*\*)?(\d{1,3})\.\s+(.+)")
_HASH_HEADING = re.compile(r"^\s*#{1,6}\s+(.+?)\s*$")
_BOLD_HEADING = re.compile(r"^\s*(?:[^\w\s]{1,3}\s*)?\*\*(?!\d+\.)([^*]{1,70})\*\*\s*$")


def numbered_source_outline(original: str, *, max_chars: int = 2800) -> str:
    """Identify explicit numbered groups and their source headings.

    A prose introduction can mention a different number of themes. This view
    states only what the saved report explicitly numbered; it does not infer
    how many other unnumbered findings the report contains.
    """

    groups: list[tuple[str, list[tuple[int, str]]]] = []
    heading = ""
    current: list[tuple[int, str]] = []
    current_heading = ""

    def finish() -> None:
        nonlocal current
        if current:
            groups.append((current_heading, current))
            current = []

    for raw in original.splitlines():
        line = raw.strip()
        heading_match = _HASH_HEADING.match(line) or _BOLD_HEADING.match(line)
        if heading_match:
            finish()
            heading = heading_match.group(1).strip()
            continue
        match = _NUMBERED.match(line)
        if not match:
            continue
        number = int(match.group(1))
        if current and (heading != current_heading or number != current[-1][0] + 1):
            finish()
        if not current:
            current_heading = heading
        title = match.group(2).replace("**", "").strip()
        current.append((number, title))
    finish()
    if not groups:
        return ""

    lines = [
        "Explicit numbered structure in the saved original. Counts below are "
        "the number of numbered entries, not prose themes or unnumbered findings."
    ]
    for group_heading, items in groups:
        label = group_heading or "Unlabelled numbered sequence"
        group_lines = [f"Section {label}: {len(items)} numbered entries."]
        for number, title in items[:30]:
            group_lines.append(f"{number}. {title[:170]}")
        if len(items) > 30:
            group_lines.append(f"{len(items) - 30} further numbered entries remain in the original.")
        candidate = "\n".join((*lines, *group_lines))
        if len(candidate) > max_chars:
            lines.append("Further numbered groups remain in the saved original.")
            break
        lines.extend(group_lines)
    return "\n".join(lines)
