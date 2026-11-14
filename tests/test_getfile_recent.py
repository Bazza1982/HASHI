"""Focused selection contracts; lifecycle and delivery are not wired yet."""
from orchestrator.getfile_recent import select_recent_agent_files


def _event(path, time, *, event_id="e1", action="modified", evidence="verified", root="r1"):
    return {
        "root_id": root, "relative_path": path, "action": action,
        "evidence": evidence, "observed_at": time, "source_event_id": event_id,
    }


def test_recent_files_are_newest_distinct_per_root():
    records = [
        _event("a.txt", "2026-11-12T01:00:00Z", event_id="1"),
        _event("b.txt", "2026-11-12T02:00:00Z", event_id="2"),
        _event("a.txt", "2026-11-12T03:00:00Z", event_id="3"),
        _event("a.txt", "2026-11-12T04:00:00Z", event_id="4", root="r2"),
    ]
    result = select_recent_agent_files(records)
    assert [(row["root_id"], row["relative_path"], row["source_event_id"]) for row in result] == [
        ("r2", "a.txt", "4"), ("r1", "a.txt", "3"), ("r1", "b.txt", "2"),
    ]


def test_recent_files_reject_unsafe_and_unverified_claims():
    records = [
        _event("../secret", "2026-11-12T02:00:00Z"),
        _event("/etc/passwd", "2026-11-12T02:00:00Z"),
        _event("C:\\secret.txt", "2026-11-12T02:00:00Z"),
        _event("report.docx", "2026-11-12T02:00:00Z", action="created", evidence="engine_reported"),
        _event("reported.md", "2026-11-12T03:00:00Z", action="touched", evidence="engine_reported"),
        _event("valid.md", "2026-11-12T04:00:00Z"),
    ]
    assert [row["relative_path"] for row in select_recent_agent_files(records)] == ["valid.md", "reported.md"]


def test_recent_files_casefold_is_explicit_and_limit_bounded():
    rows = [
        _event("Report.DOCX", "2026-11-12T02:00:00Z", event_id="1"),
        _event("report.docx", "2026-11-12T03:00:00Z", event_id="2"),
    ]
    assert len(select_recent_agent_files(rows, case_sensitive=True)) == 2
    assert len(select_recent_agent_files(rows, case_sensitive=False)) == 1
    assert [r["source_event_id"] for r in select_recent_agent_files(rows, limit=1)] == ["2"]
