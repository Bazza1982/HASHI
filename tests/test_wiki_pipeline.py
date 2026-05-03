from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime
from pathlib import Path

from scripts.wiki.config import WikiConfig
from scripts.wiki.fetcher import fetch_new_memories
from scripts.wiki.run_pipeline import check_today_consolidation
from scripts.wiki.state import WikiState


def _make_consolidated_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.execute(
        """
        CREATE TABLE consolidated (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            instance        TEXT    NOT NULL,
            agent_id        TEXT    NOT NULL,
            source_id       INTEGER NOT NULL,
            domain          TEXT    NOT NULL,
            memory_type     TEXT    NOT NULL,
            importance      REAL    NOT NULL DEFAULT 1.0,
            content         TEXT    NOT NULL,
            summary         TEXT,
            embedding       BLOB,
            source_ts       TEXT    NOT NULL,
            consolidated_at TEXT    NOT NULL,
            ts_source       TEXT    NOT NULL DEFAULT 'test'
        )
        """
    )
    rows = [
        ("HASHI1", "lily", 1, "project", "semantic", "HASHI scheduler design decision with enough useful context.", "2026-05-04T03:10:00+10:00"),
        ("HASHI1", "lily", 2, "personal", "episodic", "Private relationship memory that should not become wiki material.", "2026-05-04T03:11:00+10:00"),
        ("HASHI1", "temp", 3, "project", "semantic", "Temporary agent output should not become durable wiki material.", "2026-05-04T03:12:00+10:00"),
        ("HASHI1", "lily", 4, "project", "semantic", "api_key = sk-abcdefghijklmnopqrstuvwxyz1234567890 should redact.", "2026-05-04T03:13:00+10:00"),
    ]
    con.executemany(
        """
        INSERT INTO consolidated(
            instance, agent_id, source_id, domain, memory_type, content, source_ts, consolidated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, '2026-05-04T03:20:00+10:00')
        """,
        rows,
    )
    con.commit()
    con.close()


def test_wiki_state_schema_initializes(tmp_path: Path) -> None:
    state_path = tmp_path / "wiki_state.sqlite"
    with WikiState(state_path) as state:
        state.init_schema()
        assert state.get_last_classified_id() == 0
        assert state.count_rows("run_state") == 1


def test_fetcher_applies_privacy_and_redaction_filters(tmp_path: Path) -> None:
    consolidated = tmp_path / "consolidated_memory.sqlite"
    _make_consolidated_db(consolidated)
    config = WikiConfig(consolidated_db=consolidated, wiki_state_db=tmp_path / "wiki_state.sqlite")
    with WikiState(config.wiki_state_db) as state:
        state.init_schema()
        result = fetch_new_memories(config, state)

    assert [record.id for record in result.classifiable] == [1]
    assert [record.id for record in result.redacted] == [4]
    assert {record.reason for record in result.skipped} == {"private_domain:personal", "temp_agent"}
    assert result.max_seen_id == 4


def test_consolidation_check_requires_today_embed_phase(tmp_path: Path) -> None:
    log = tmp_path / "consolidation_log.jsonl"
    log.write_text(
        '{"timestamp":"2026-05-03T18:05:00+00:00","new_inserted":10,"errors":0}\n'
        '{"timestamp":"2026-05-03T18:08:00+00:00","phase":"embed","embedded":10,"errors":0}\n',
        encoding="utf-8",
    )
    config = WikiConfig(consolidation_log=log)
    ok, reason = check_today_consolidation(
        config,
        datetime.fromisoformat("2026-05-04T04:10:00+10:00"),
    )
    assert ok is True
    assert "embed completed" in reason


def test_consolidation_check_blocks_without_embed_phase(tmp_path: Path) -> None:
    log = tmp_path / "consolidation_log.jsonl"
    log.write_text(
        '{"timestamp":"2026-05-03T18:05:00+00:00","new_inserted":10,"errors":0}\n',
        encoding="utf-8",
    )
    config = WikiConfig(consolidation_log=log)
    ok, reason = check_today_consolidation(
        config,
        datetime.fromisoformat("2026-05-04T04:10:00+10:00"),
    )
    assert ok is False
    assert "embed phase not complete" in reason
