"""Durable audit projection, ownership and evidence contracts."""
from pathlib import Path

from orchestrator.canonical_audit import CanonicalAuditStore
from orchestrator.getfile_index import RecentFileIndex


def audit(tmp_path, agent="alice"):
    return CanonicalAuditStore(tmp_path / "home", instance_id="TEST", agent_id=agent)


def record(store, root, name, *, when="2026-11-12T01:00:00Z", action="created"):
    return store.record("tool_call", {
        "tool_name": "file_write", "is_error": False,
        "arguments": {"path": str(root / name)},
        "details": {"file_effect": {"path": str(root / name), "action": action, "verified": True}},
    }, request_id="run-1", provenance={"workspace_dir": str(root), "access_roots": [str(root)],
                                     "owner_id": "owner"},
    ) if when is None else store.record_many([{
        "event_type": "tool_call", "recorded_at": when, "request_id": "run-1",
        "payload": {"tool_name": "file_write", "is_error": False,
                    "arguments": {"path": str(root / name)},
                    "details": {"file_effect": {"path": str(root / name), "action": action, "verified": True}}},
        "provenance": {"workspace_dir": str(root), "access_roots": [str(root)], "owner_id": "owner"},
    }])[0]


def test_audit_projects_newest_ten_and_survives_reopen_without_fingerprint(tmp_path):
    store = audit(tmp_path)
    root = tmp_path / "work"; root.mkdir()
    for n in range(13):
        record(store, root, f"{n}.txt", when=f"2026-11-12T01:{n:02d}:00Z")
    record(store, root, "0.txt", when="2026-11-12T02:00:00Z", action="modified")
    rows = RecentFileIndex(audit(tmp_path)).recent(owner_id="owner")
    assert [Path(r["relative_path"]).name for r in rows] == ["0.txt"] + [f"{n}.txt" for n in range(12, 3, -1)]
    assert rows[0]["action"] == "modified"
    assert rows[0]["evidence"] == "verified"
    assert RecentFileIndex(audit(tmp_path, "bob")).recent(owner_id="owner") == []
    assert RecentFileIndex(store).recent(owner_id="someone-else") == []


def test_engine_relative_paths_use_frozen_run_cwd_and_never_claim_verified_authorship(tmp_path):
    store = audit(tmp_path); root = tmp_path / "work"
    store.record("provider_stream_event", {"kind": "file_edit", "file_path": "native.txt",
        "metadata": {"action": "created"}}, provenance={
        "getfile_scope": {"cwd": str(root), "roots": [str(root)], "owner_id": "owner"}})
    row, = RecentFileIndex(store).recent(owner_id="owner")
    assert row["relative_path"] == "native.txt"
    assert row["action"] == "touched" and row["evidence"] == "engine_reported"
    store.record("provider_stream_event", {"kind": "file_edit", "file_path": "../outside.txt"},
                 provenance={"getfile_scope": {"cwd": str(root), "roots": [str(root)], "owner_id": "owner"}})
    store.record("provider_stream_event", {"kind": "file_edit", "file_path": "ambiguous.txt"})
    assert len(RecentFileIndex(store).recent(owner_id="owner")) == 1


def test_projection_can_rebuild_from_bounded_canonical_tail_without_full_scan(tmp_path, monkeypatch):
    store = audit(tmp_path); root = tmp_path / "work"
    record(store, root, "kept.txt")
    index = RecentFileIndex(store)
    index.path.unlink()
    monkeypatch.setattr(store, "read_events", lambda *_: (_ for _ in ()).throw(AssertionError("unbounded read")))
    row, = RecentFileIndex(store).recent(owner_id="owner")
    assert row["relative_path"] == "kept.txt"


def test_failed_tools_and_internal_transients_do_not_pollute_menu(tmp_path):
    store = audit(tmp_path); root = tmp_path / "work"
    record(store, root, ".git/index")
    record(store, root, "node_modules/cache.js")
    store.record("tool_call", {"tool_name": "file_write", "arguments": {"path": str(root / "failed.txt")},
                 "is_error": True}, provenance={"workspace_dir": str(root), "access_roots": [str(root)], "owner_id": "owner"})
    record(store, root, "answer.docx")
    assert [r["relative_path"] for r in RecentFileIndex(store).recent(owner_id="owner")] == ["answer.docx"]
