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
        # The numbered heading can carry a whole paragraph after an em dash.
        # Navigation needs the entry title; the full paragraph remains in the
        # canonical original offered to the foreground on request.
        title = re.split(r"\s+[—–-]\s+", title, maxsplit=1)[0].strip() or title
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
        group_line = f"Section {label}: {len(items)} numbered entries."
        if len("\n".join((*lines, group_line))) > max_chars:
            break
        lines.append(group_line)
        shown = 0
        for number, title in items[:30]:
            item_line = f"{number}. {title[:170]}"
            # Preserve space for an explicit continuation count when this is a
            # compact opening index rather than the full report handoff.
            reserve = 55 if shown + 1 < len(items) else 0
            if len("\n".join((*lines, item_line))) + reserve > max_chars:
                break
            lines.append(item_line)
            shown += 1
        if shown < len(items):
            remainder = f"{len(items) - shown} further numbered entries remain in the original."
            if len("\n".join((*lines, remainder))) <= max_chars:
                lines.append(remainder)
        if shown < len(items):
            break
    return "\n".join(lines)
