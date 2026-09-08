from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.wiki_consolidation_evidence import check_today_consolidation


NOW = datetime.fromisoformat("2026-05-04T04:15:00+10:00")


def _check(
    tmp_path: Path,
    *events: dict[str, object],
    now: datetime = NOW,
    timezone: str = "Australia/Sydney",
) -> tuple[bool, str]:
    log = tmp_path / "consolidation_log.jsonl"
    log.write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )
    config = SimpleNamespace(consolidation_log=log, timezone=timezone)
    return check_today_consolidation(config, now)


def _scan(timestamp: str, *, inserted: int = 0, errors: int = 0) -> dict[str, object]:
    return {
        "timestamp": timestamp,
        "new_inserted": inserted,
        "errors": errors,
    }


def _embed(
    timestamp: str,
    *,
    embedded: int = 0,
    errors: int = 0,
    phase: str = "embed",
) -> dict[str, object]:
    return {
        "timestamp": timestamp,
        "phase": phase,
        "embedded": embedded,
        "errors": errors,
    }


def test_existing_scan_and_clean_embed_format_remains_valid(tmp_path: Path) -> None:
    ok, reason = _check(
        tmp_path,
        _scan("2026-05-03T18:05:00+00:00", inserted=10),
        _embed("2026-05-03T18:08:00+00:00", embedded=10),
    )

    assert ok is True
    assert "embed completed" in reason


def test_new_scan_after_old_success_requires_a_new_embed(tmp_path: Path) -> None:
    ok, reason = _check(
        tmp_path,
        _scan("2026-05-03T18:05:00+00:00", inserted=10),
        _embed("2026-05-03T18:08:00+00:00", embedded=10),
        _scan("2026-05-03T18:12:00+00:00", inserted=2),
    )

    assert ok is False
    assert "embed phase not complete" in reason


def test_latest_failed_scan_blocks_even_if_an_embed_follows(tmp_path: Path) -> None:
    ok, reason = _check(
        tmp_path,
        _scan("2026-05-03T18:05:00+00:00"),
        _embed("2026-05-03T18:08:00+00:00"),
        _scan("2026-05-03T18:10:00+00:00", inserted=2, errors=1),
        _embed("2026-05-03T18:12:00+00:00", embedded=2),
    )

    assert ok is False
    assert "latest scan" in reason
    assert "1 error" in reason


@pytest.mark.parametrize("phase", ["embed", "embed_error"])
def test_latest_embed_error_outcome_blocks(tmp_path: Path, phase: str) -> None:
    ok, reason = _check(
        tmp_path,
        _scan("2026-05-03T18:05:00+00:00"),
        _embed("2026-05-03T18:08:00+00:00"),
        _embed("2026-05-03T18:12:00+00:00", errors=1, phase=phase),
    )

    assert ok is False
    assert "latest embed" in reason
    assert "1 error" in reason


def test_zero_pending_clean_embed_is_completion_evidence(tmp_path: Path) -> None:
    ok, reason = _check(
        tmp_path,
        _scan("2026-05-03T18:05:00+00:00", inserted=0),
        _embed("2026-05-03T18:08:00+00:00", embedded=0),
    )

    assert ok is True
    assert "embed completed" in reason


def test_now_and_events_use_configured_timezone(tmp_path: Path) -> None:
    ok, reason = _check(
        tmp_path,
        _scan("2026-05-03T18:05:00+00:00"),
        _embed("2026-05-03T18:08:00+00:00"),
        now=datetime.fromisoformat("2026-05-03T18:10:00+00:00"),
    )

    assert ok is True
    assert "2026-05-04T04:08:00+10:00" in reason


def test_newer_recovery_pair_supersedes_old_errors(tmp_path: Path) -> None:
    ok, _ = _check(
        tmp_path,
        _scan("2026-05-03T18:01:00+00:00", errors=1),
        _embed("2026-05-03T18:02:00+00:00", errors=1, phase="embed_error"),
        _scan("2026-05-03T18:05:00+00:00"),
        _embed("2026-05-03T18:08:00+00:00"),
    )

    assert ok is True


@pytest.mark.parametrize(
    "bad_line",
    [
        "{not-json",
        json.dumps({"timestamp": "not-a-date", "phase": "embed", "errors": 0}),
        json.dumps({"timestamp": "2026-05-04T04:12:00", "phase": "embed", "errors": 0}),
        json.dumps({"timestamp": "2026-05-03T18:12:00+00:00", "phase": []}),
        json.dumps({"timestamp": "2026-05-03T18:12:00+00:00", "phase": "unknown"}),
        json.dumps(
            {
                "timestamp": "2026-05-03T18:12:00+00:00",
                "phase": "embed_error",
                "new_inserted": 0,
                "errors": 0,
            }
        ),
        json.dumps(
            {
                "timestamp": "2026-05-03T18:12:00+00:00",
                "phase": "embed",
                "embedded": 0,
                "errors": "0",
            }
        ),
    ],
)
def test_malformed_evidence_fails_closed(tmp_path: Path, bad_line: str) -> None:
    log = tmp_path / "consolidation_log.jsonl"
    log.write_text(
        json.dumps(_scan("2026-05-03T18:05:00+00:00"))
        + "\n"
        + json.dumps(_embed("2026-05-03T18:08:00+00:00"))
        + "\n"
        + bad_line
        + "\n",
        encoding="utf-8",
    )
    config = SimpleNamespace(consolidation_log=log, timezone="Australia/Sydney")

    ok, reason = check_today_consolidation(config, NOW)

    assert ok is False
    assert "bad log line" in reason
