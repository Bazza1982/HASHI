from __future__ import annotations

import gzip
import json
import sqlite3
from pathlib import Path

from scripts.remote_memory_consolidation import main
from scripts.wiki.config import WikiConfig
from scripts.wiki.fetcher import fetch_new_memories
from scripts.wiki.state import WikiState


def test_export_dry_run_does_not_write_batch(tmp_path: Path) -> None:
    root = tmp_path
    workspace = root / "workspaces" / "sakura"
    workspace.mkdir(parents=True)
    (workspace / "transcript.jsonl").write_text(
        json.dumps({"role": "user", "text": "Remote useful HASHI memory with enough context for central wiki.", "ts": "2026-05-16T01:00:00Z"}) + "\n",
        encoding="utf-8",
    )

    code = main(["--root", str(root), "export", "--instance-id", "INTEL", "--agent", "sakura", "--dry-run"])

    assert code == 0
    assert not (root / "private" / "remote_memory_export").exists()


def test_export_and_import_are_idempotent(tmp_path: Path) -> None:
    root = tmp_path
    workspace = root / "workspaces" / "sakura"
    workspace.mkdir(parents=True)
    (workspace / "transcript.jsonl").write_text(
        json.dumps({"role": "assistant", "text": "Remote agent discovered a durable HASHI wiki consolidation fact.", "ts": "2026-05-16T01:00:00Z"}) + "\n",
        encoding="utf-8",
    )

    assert main(["--root", str(root), "export", "--instance-id", "INTEL", "--agent", "sakura", "--batch-id", "batch-test"]) == 0
    batch = root / "private" / "remote_memory_export" / "pending" / "batch-test"
    inbox_batch = root / "private" / "remote_memory_inbox" / "INTEL" / "sakura" / "batch-test"
    inbox_batch.mkdir(parents=True)
    for item in batch.iterdir():
        (inbox_batch / item.name).write_bytes(item.read_bytes())

    db = root / "workspaces" / "lily" / "consolidated_memory.sqlite"
    db.parent.mkdir(parents=True)
    args = ["--root", str(root), "--consolidated-db", str(db), "import", "--batch-dir", str(inbox_batch)]
    assert main(args) == 0
    assert main(args) == 0

    with sqlite3.connect(db) as con:
        rows = con.execute("SELECT instance, agent_id, domain, memory_type, content, ts_source FROM consolidated").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "INTEL"
    assert rows[0][1] == "sakura"
    assert rows[0][2] == "remote_memory"
    assert rows[0][3] == "episodic"
    assert rows[0][5].startswith("remote:batch-test:")

    wiki_state = root / "workspaces" / "lily" / "wiki_state.sqlite"
    with WikiState(wiki_state) as state:
        state.init_schema()
        fetched = fetch_new_memories(WikiConfig(hashi_root=root, consolidated_db=db, wiki_state_db=wiki_state), state)
    assert fetched.total_seen == 1
    assert fetched.classifiable[0].instance == "INTEL"
    assert fetched.classifiable[0].agent_id == "sakura"
    assert fetched.classifiable[0].domain == "remote_memory"


def test_import_dedupes_same_record_across_different_batches(tmp_path: Path) -> None:
    root = tmp_path
    workspace = root / "workspaces" / "sakura"
    workspace.mkdir(parents=True)
    (workspace / "transcript.jsonl").write_text(
        json.dumps({"role": "assistant", "text": "Remote duplicate memory should dedupe across export batches.", "ts": "2026-05-16T01:00:00Z"}) + "\n",
        encoding="utf-8",
    )
    for batch_id in ("batch-a", "batch-b"):
        assert main(["--root", str(root), "export", "--instance-id", "INTEL", "--agent", "sakura", "--batch-id", batch_id]) == 0
        source = root / "private" / "remote_memory_export" / "pending" / batch_id
        target = root / "private" / "remote_memory_inbox" / "INTEL" / "sakura" / batch_id
        target.mkdir(parents=True)
        for item in source.iterdir():
            (target / item.name).write_bytes(item.read_bytes())

    db = root / "workspaces" / "lily" / "consolidated_memory.sqlite"
    assert main(["--root", str(root), "--consolidated-db", str(db), "import", "--batch-dir", str(root / "private" / "remote_memory_inbox" / "INTEL" / "sakura" / "batch-a")]) == 0
    assert main(["--root", str(root), "--consolidated-db", str(db), "import", "--batch-dir", str(root / "private" / "remote_memory_inbox" / "INTEL" / "sakura" / "batch-b")]) == 0

    with sqlite3.connect(db) as con:
        count = con.execute("SELECT COUNT(*) FROM consolidated").fetchone()[0]
    assert count == 1


def test_import_quarantines_bad_checksum(tmp_path: Path) -> None:
    root = tmp_path
    batch = root / "private" / "remote_memory_inbox" / "INTEL" / "sakura" / "bad-batch"
    batch.mkdir(parents=True)
    payload = gzip.compress(b'{"schema_version":1,"text":"hello"}\n')
    (batch / "records.jsonl.gz").write_bytes(payload)
    (batch / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "batch_id": "bad-batch",
                "instance_id": "INTEL",
                "payload_file": "records.jsonl.gz",
                "payload_sha256": "wrong",
                "record_count": 1,
                "privacy_scan": {"status": "passed"},
            }
        ),
        encoding="utf-8",
    )

    code = main(["--root", str(root), "import", "--batch-dir", str(batch)])

    assert code == 3
    assert not batch.exists()
    assert (root / "private" / "remote_memory_quarantine" / "bad-batch" / "quarantine_reason.txt").exists()


def test_deliver_copies_batch_without_deleting_pending(tmp_path: Path) -> None:
    root = tmp_path
    workspace = root / "workspaces" / "sakura"
    workspace.mkdir(parents=True)
    (workspace / "transcript.jsonl").write_text(
        json.dumps({"role": "user", "text": "Remote delivery memory with enough durable HASHI wiki context.", "ts": "2026-05-16T01:00:00Z"}) + "\n",
        encoding="utf-8",
    )
    assert main(["--root", str(root), "export", "--instance-id", "INTEL", "--agent", "sakura", "--batch-id", "deliver-test"]) == 0
    target = root / "drop"

    assert main(["--root", str(root), "deliver", "--batch-id", "deliver-test", "--target-inbox", str(target)]) == 0

    assert (root / "private" / "remote_memory_export" / "pending" / "deliver-test" / "manifest.json").exists()
    assert (target / "INTEL" / "sakura" / "deliver-test" / "manifest.json").exists()


def test_import_quarantines_manifest_without_passed_privacy_scan(tmp_path: Path) -> None:
    root = tmp_path
    batch = root / "private" / "remote_memory_inbox" / "INTEL" / "sakura" / "privacy-bad"
    batch.mkdir(parents=True)
    payload = gzip.compress(b'{"schema_version":1,"text":"hello"}\n')
    (batch / "records.jsonl.gz").write_bytes(payload)
    (batch / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "batch_id": "privacy-bad",
                "instance_id": "INTEL",
                "payload_file": "records.jsonl.gz",
                "payload_sha256": "not_checked",
                "record_count": 1,
                "privacy_scan": {"status": "failed"},
            }
        ),
        encoding="utf-8",
    )

    code = main(["--root", str(root), "import", "--batch-dir", str(batch)])

    assert code == 3
    assert (root / "private" / "remote_memory_quarantine" / "privacy-bad" / "quarantine_reason.txt").exists()


def test_sync_wiki_copies_only_generated_zones(tmp_path: Path) -> None:
    root = tmp_path
    vault = root / "vault"
    (vault / "10_GENERATED_TOPICS").mkdir(parents=True)
    (vault / "10_GENERATED_TOPICS" / "A.md").write_text("generated", encoding="utf-8")
    (vault / "Human").mkdir()
    (vault / "Human" / "Note.md").write_text("human", encoding="utf-8")
    mirror = root / "mirror"

    code = main(["--root", str(root), "--vault-root", str(vault), "--mirror-root", str(mirror), "sync-wiki"])

    assert code == 0
    assert (mirror / "10_GENERATED_TOPICS" / "A.md").exists()
    assert not (mirror / "Human").exists()
    assert (mirror / "remote_wiki_mirror_manifest.json").exists()


def test_sync_wiki_check_validates_generated_zones(tmp_path: Path) -> None:
    root = tmp_path
    vault = root / "vault"
    for zone in ("10_GENERATED_TOPICS", "30_GENERATED_INDEXES", "00_SYSTEM"):
        (vault / zone).mkdir(parents=True)
    mirror = root / "mirror"

    assert main(["--root", str(root), "--vault-root", str(vault), "--mirror-root", str(mirror), "sync-wiki", "--check"]) == 0
