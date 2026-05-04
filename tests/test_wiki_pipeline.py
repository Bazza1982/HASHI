from __future__ import annotations

import argparse
import subprocess
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from scripts.wiki.backend_client import BackendPolicyError, call_lily_cli_backend
from scripts.wiki.classifier import (
    ClassificationAssignment,
    build_classification_prompt,
    parse_classification_response,
)
from scripts.wiki.config import WikiConfig
from scripts.wiki.fetcher import FetchResult, fetch_new_memories
from scripts.wiki.page_generator import fetch_topic_memories, generate_dry_run_pages
from scripts.wiki.run_pipeline import check_today_consolidation, drop_existing_completed_runs, run_stage0
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
        ("HASHI1", "lily", 5, "project", "episodic", "User: 好想你，亲一下。 Assistant: This is private relationship content.", "2026-05-04T03:14:00+10:00"),
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
    assert {record.reason for record in result.skipped} == {
        "private_domain:personal",
        "private_content_pattern",
        "temp_agent",
    }
    assert result.max_seen_id == 5


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


def test_classifier_prompt_contains_topic_taxonomy(tmp_path: Path) -> None:
    record = _record(1, "HASHI scheduler and memory consolidation decision.")
    prompt = build_classification_prompt([record])
    assert "HASHI_Architecture" in prompt
    assert "AI_Memory_Systems" in prompt
    assert "HASHI_Ops_Security" in prompt
    assert '"id": 1' in prompt


def test_classifier_response_parser_validates_topics() -> None:
    record = _record(7, "HASHI wiki design.")
    call = subprocess.CompletedProcess(
        ["claude"],
        0,
        stdout='[{"id":7,"topics":["Obsidian_Wiki"],"confidence":0.91}]',
        stderr="",
    )
    result = parse_classification_response(
        call=type("Call", (), {"text": call.stdout, "backend": "claude-cli", "model": "claude-sonnet-4-6"})(),
        memories=[record],
    )
    assert result.assignments[0].topics == ("Obsidian_Wiki",)


def test_backend_client_refuses_remote_api_backend(tmp_path: Path) -> None:
    _write_lily_state(tmp_path, "openrouter-api", "anthropic/claude-sonnet-4.6")
    config = WikiConfig(hashi_root=tmp_path)
    with pytest.raises(BackendPolicyError):
        call_lily_cli_backend("hello", config, runner=_fake_runner)


def test_backend_client_calls_lily_cli_backend(tmp_path: Path) -> None:
    _write_lily_state(tmp_path, "claude-cli", "claude-sonnet-4-6")
    (tmp_path / "agents.json").write_text('{"global":{"claude_cmd":"/usr/bin/claude"}}', encoding="utf-8")
    config = WikiConfig(hashi_root=tmp_path)
    result = call_lily_cli_backend("hello", config, runner=_fake_runner)
    assert result.backend == "claude-cli"
    assert result.model == "claude-sonnet-4-6"
    assert result.text.startswith("[")


def test_backend_client_sends_claude_prompt_via_stdin(tmp_path: Path) -> None:
    _write_lily_state(tmp_path, "claude-cli", "claude-sonnet-4-6")
    (tmp_path / "agents.json").write_text('{"global":{"claude_cmd":"/usr/bin/claude"}}', encoding="utf-8")
    seen = {}

    def runner(argv, **kwargs):
        seen["argv"] = argv
        seen["input"] = kwargs.get("input")
        return _fake_runner(argv, **kwargs)

    call_lily_cli_backend("large prompt body", WikiConfig(hashi_root=tmp_path), runner=runner)
    assert seen["argv"] == ["/usr/bin/claude", "--model", "claude-sonnet-4-6", "--print"]
    assert seen["input"] == "large prompt body"


def test_run_pipeline_mock_classifier_dry_run(tmp_path: Path) -> None:
    consolidated = tmp_path / "consolidated_memory.sqlite"
    _make_consolidated_db(consolidated)
    log = tmp_path / "consolidation_log.jsonl"
    log.write_text(
        '{"timestamp":"2026-05-03T18:08:00+00:00","phase":"embed","embedded":10,"errors":0}\n',
        encoding="utf-8",
    )
    config = WikiConfig(
        hashi_root=tmp_path,
        consolidated_db=consolidated,
        wiki_state_db=tmp_path / "wiki_state.sqlite",
        consolidation_log=log,
        dry_run_report_latest=tmp_path / "wiki_dry_run.md",
    )
    args = argparse.Namespace(
        daily=True,
        weekly_if_saturday=False,
        dry_run=True,
        classify=False,
        classify_dry_run=True,
        mock_classifier=True,
        limit=10,
        max_classify=None,
        persist_classifications=False,
        pages_dry_run=False,
        skip_consolidation_check=False,
    )
    lines = run_stage0(config, args)
    text = "\n".join(lines)
    assert "Classifier stage: True" in text
    assert "Backend: mock" in text
    assert "Assignments: 1" in text


def test_mock_classifier_routes_security_operations_to_ops_topic() -> None:
    from scripts.wiki.classifier import classify_memories_dry_run

    result = classify_memories_dry_run(
        [_record(9, "Run pip-audit, review chmod permissions, and check exposed port 3390.")],
        WikiConfig(),
        mock=True,
    )
    assert result.assignments[0].topics == ("HASHI_Ops_Security",)


def test_state_persists_assignments_and_advances_watermark(tmp_path: Path) -> None:
    state_path = tmp_path / "wiki_state.sqlite"
    with WikiState(state_path) as state:
        state.init_schema()
        skipped = [_record(1, "personal skipped")]
        classified = [_record(2, "HASHI scheduler design")]
        state.record_skipped_runs(skipped, batch_id="batch-1", status="skipped")
        state.record_assignments(
            classified,
            [
                ClassificationAssignment(
                    consolidated_id=2,
                    topics=("HASHI_Architecture",),
                    confidence=0.92,
                )
            ],
            batch_id="batch-1",
            classifier_model="claude-cli/claude-sonnet-4-6",
        )
        assert state.advance_watermark() == 2
        assert state.get_last_classified_id() == 2
        assert state.count_rows("classification_run") == 2
        assert state.count_rows("classification_assignment") == 1


def test_state_does_not_advance_watermark_across_failed_row(tmp_path: Path) -> None:
    state_path = tmp_path / "wiki_state.sqlite"
    with WikiState(state_path) as state:
        state.init_schema()
        state.record_skipped_runs([_record(1, "failed")], batch_id="batch-1", status="failed")
        state.record_skipped_runs([_record(2, "ok skipped")], batch_id="batch-1", status="skipped")
        assert state.advance_watermark() == 0
        assert state.get_last_classified_id() == 0


def test_state_records_missing_classifier_assignments_as_failed(tmp_path: Path) -> None:
    state_path = tmp_path / "wiki_state.sqlite"
    with WikiState(state_path) as state:
        state.init_schema()
        state.record_assignments(
            [_record(1, "HASHI scheduler design"), _record(2, "missing classifier result")],
            [
                ClassificationAssignment(
                    consolidated_id=1,
                    topics=("HASHI_Architecture",),
                    confidence=0.9,
                )
            ],
            batch_id="batch-1",
            classifier_model="mock/mock",
        )
        failed = state.conn.execute(
            "SELECT status FROM classification_run WHERE consolidated_id = 2"
        ).fetchone()
        assert failed["status"] == "failed"
        assert state.advance_watermark() == 1


def test_drop_existing_completed_runs_keeps_failed_rows_for_retry(tmp_path: Path) -> None:
    state_path = tmp_path / "wiki_state.sqlite"
    records = [_record(1, "done"), _record(2, "retry"), _record(3, "new")]
    fetch_result = FetchResult(classifiable=records, skipped=[], redacted=[], max_seen_id=3)
    with WikiState(state_path) as state:
        state.init_schema()
        state.record_assignments(
            [records[0]],
            [
                ClassificationAssignment(
                    consolidated_id=1,
                    topics=("HASHI_Architecture",),
                    confidence=0.9,
                )
            ],
            batch_id="batch-1",
            classifier_model="mock/mock",
        )
        state.record_skipped_runs([records[1]], batch_id="batch-1", status="failed")
        filtered = drop_existing_completed_runs(state, fetch_result)
        assert [record.id for record in filtered.classifiable] == [2, 3]


def test_run_pipeline_persists_mock_classifier_assignments(tmp_path: Path) -> None:
    consolidated = tmp_path / "consolidated_memory.sqlite"
    _make_consolidated_db(consolidated)
    log = tmp_path / "consolidation_log.jsonl"
    log.write_text(
        '{"timestamp":"2026-05-03T18:08:00+00:00","phase":"embed","embedded":10,"errors":0}\n',
        encoding="utf-8",
    )
    state_path = tmp_path / "wiki_state.sqlite"
    config = WikiConfig(
        hashi_root=tmp_path,
        consolidated_db=consolidated,
        wiki_state_db=state_path,
        consolidation_log=log,
        report_latest=tmp_path / "wiki_latest.md",
        dry_run_report_latest=tmp_path / "wiki_dry_run.md",
    )
    args = argparse.Namespace(
        daily=True,
        weekly_if_saturday=False,
        dry_run=False,
        classify=True,
        classify_dry_run=False,
        mock_classifier=True,
        limit=10,
        max_classify=None,
        persist_classifications=True,
        pages_dry_run=False,
        skip_consolidation_check=False,
    )
    run_stage0(config, args)
    with WikiState(state_path) as state:
        state.init_schema()
        assert state.get_last_classified_id() == 5
        assert state.count_rows("classification_run") == 5
        assert state.count_rows("classification_assignment") == 1


def test_page_generator_writes_dry_run_topic_pages(tmp_path: Path) -> None:
    consolidated = tmp_path / "consolidated_memory.sqlite"
    _make_consolidated_db(consolidated)
    state_path = tmp_path / "wiki_state.sqlite"
    config = WikiConfig(
        consolidated_db=consolidated,
        wiki_state_db=state_path,
        dry_run_pages_dir=tmp_path / "wiki_pages_dry_run",
    )
    with WikiState(state_path) as state:
        state.init_schema()
        state.record_assignments(
            [
                _record(1, "HASHI scheduler design"),
                _record(5, "User: 好想你，亲一下。 Assistant: This is private relationship content."),
            ],
            [
                ClassificationAssignment(
                    consolidated_id=1,
                    topics=("HASHI_Architecture",),
                    confidence=0.9,
                ),
                ClassificationAssignment(
                    consolidated_id=5,
                    topics=("HASHI_Architecture",),
                    confidence=0.9,
                )
            ],
            batch_id="batch-1",
            classifier_model="mock/mock",
        )

    memories = fetch_topic_memories(config, "HASHI_Architecture")
    assert len(memories) == 2
    assert "[private content filtered]" in {memory.content for memory in memories}
    drafts = generate_dry_run_pages(config)
    assert [draft.topic_id for draft in drafts] == ["HASHI_Architecture"]
    page = drafts[0].path.read_text(encoding="utf-8")
    assert "status: dry-run" in page
    assert "Memory 1" in page
    assert "[private content filtered]" in page
    assert "好想你" not in page


def test_run_pipeline_generates_page_drafts_from_persisted_state(tmp_path: Path) -> None:
    consolidated = tmp_path / "consolidated_memory.sqlite"
    _make_consolidated_db(consolidated)
    log = tmp_path / "consolidation_log.jsonl"
    log.write_text(
        '{"timestamp":"2026-05-03T18:08:00+00:00","phase":"embed","embedded":10,"errors":0}\n',
        encoding="utf-8",
    )
    config = WikiConfig(
        hashi_root=tmp_path,
        consolidated_db=consolidated,
        wiki_state_db=tmp_path / "wiki_state.sqlite",
        consolidation_log=log,
        report_latest=tmp_path / "wiki_latest.md",
        dry_run_pages_dir=tmp_path / "wiki_pages_dry_run",
    )
    args = argparse.Namespace(
        daily=True,
        weekly_if_saturday=False,
        dry_run=False,
        classify=True,
        classify_dry_run=False,
        mock_classifier=True,
        limit=10,
        max_classify=None,
        persist_classifications=True,
        pages_dry_run=True,
        skip_consolidation_check=False,
    )
    lines = run_stage0(config, args)
    text = "\n".join(lines)
    assert "Page Drafts" in text
    assert "HASHI_Architecture" in text


def _record(consolidated_id: int, content: str):
    from scripts.wiki.fetcher import MemoryRecord

    return MemoryRecord(
        id=consolidated_id,
        instance="HASHI1",
        agent_id="lily",
        domain="project",
        memory_type="semantic",
        content=content,
        source_ts="2026-05-04T03:10:00+10:00",
        ts_source="test",
    )


def _write_lily_state(root: Path, backend: str, model: str) -> None:
    state_dir = root / "workspaces/lily"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "state.json").write_text(
        f'{{"active_backend":"{backend}","active_model":"{model}"}}',
        encoding="utf-8",
    )


def _fake_runner(argv, **kwargs):
    return subprocess.CompletedProcess(
        argv,
        0,
        stdout='[{"id":1,"topics":["HASHI_Architecture"],"confidence":0.9}]',
        stderr="",
    )
