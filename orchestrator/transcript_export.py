from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path


def export_daily_transcript(
    transcript_path: Path,
    journal_dir: Path,
    cutoff_dt: datetime,
) -> bool:
    """Export the preceding 24 hours of a Flex transcript as a journal page."""

    transcript_path = Path(transcript_path)
    if not transcript_path.exists():
        return False

    start_dt = cutoff_dt - timedelta(days=1)
    export_entries: list[dict] = []
    with transcript_path.open("r", encoding="utf-8") as stream:
        for raw_line in stream:
            line = raw_line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                entry_dt = datetime.fromisoformat(entry["timestamp"])
                role = str(entry["role"])
                text = str(entry["text"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
            if start_dt <= entry_dt < cutoff_dt:
                export_entries.append(
                    {"timestamp": entry_dt, "role": role, "text": text}
                )

    if not export_entries:
        return False

    journal_date = start_dt.date().isoformat()
    journal_dir = Path(journal_dir)
    journal_dir.mkdir(parents=True, exist_ok=True)
    journal_path = journal_dir / f"{journal_date}.md"
    lines = [f"# Conversation Journal - {journal_date}", ""]
    for entry in export_entries:
        timestamp = entry["timestamp"].strftime("%Y-%m-%d %H:%M:%S")
        role = "User" if entry["role"] == "user" else "Agent"
        lines.extend(
            [
                f"## {role} - {timestamp}",
                "",
                entry["text"],
                "",
            ]
        )
    journal_path.write_text(
        "\n".join(lines).strip() + "\n",
        encoding="utf-8",
    )
    return True
