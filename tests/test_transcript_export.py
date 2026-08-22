from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator.transcript_export import export_daily_transcript


def _entry(timestamp: datetime, role: str, text: str) -> str:
    return json.dumps(
        {
            "timestamp": timestamp.isoformat(),
            "role": role,
            "text": text,
        }
    )


def test_export_daily_transcript_keeps_only_preceding_day(tmp_path: Path) -> None:
    cutoff = datetime(2026, 8, 22, 12, 0, 0)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        "\n".join(
            [
                _entry(cutoff - timedelta(days=2), "user", "too old"),
                _entry(cutoff - timedelta(hours=2), "user", "current request"),
                "not-json",
                _entry(cutoff, "assistant", "too new"),
                _entry(cutoff - timedelta(hours=1), "assistant", "current answer"),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    exported = export_daily_transcript(transcript, tmp_path / "journals", cutoff)

    assert exported is True
    journal = (tmp_path / "journals" / "2026-08-21.md").read_text(
        encoding="utf-8"
    )
    assert "current request" in journal
    assert "current answer" in journal
    assert "too old" not in journal
    assert "too new" not in journal


@pytest.mark.asyncio
async def test_flex_manual_job_preserves_transcript_export_action(tmp_path: Path) -> None:
    cutoff_entry = datetime.now() - timedelta(minutes=5)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        _entry(cutoff_entry, "user", "keep this") + "\n",
        encoding="utf-8",
    )
    runtime = object.__new__(FlexibleAgentRuntime)
    runtime.workspace_dir = tmp_path
    runtime.transcript_log_path = transcript
    runtime._primary_chat_id = lambda: 123
    runtime.send_long_message = AsyncMock()

    result = await FlexibleAgentRuntime._run_job_now(
        runtime,
        {"id": "daily-export", "action": "export_transcript"},
        kind="cron",
    )

    assert result == (True, "Transcript exported.")
    runtime.send_long_message.assert_awaited_once_with(
        chat_id=123,
        text="Transcript exported.",
        request_id="job-daily-export",
        purpose="skill-job-run",
    )
