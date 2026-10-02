from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from scripts.remote_memory_consolidation import RemoteConfig, build_parser, main


def test_private_filter_is_preserved_when_script_runs_outside_hashi_tree(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1] / "scripts" / "remote_memory_consolidation.py"
    deployed = tmp_path / source.name
    deployed.write_bytes(source.read_bytes())
    probe = (
        "import runpy, sys; "
        "module = runpy.run_path(sys.argv[1]); "
        "_, stats = module['privacy_filter']([{'text': '我喜欢你'}]); "
        "print(stats['private_skipped'])"
    )

    result = subprocess.run(
        [sys.executable, "-I", "-c", probe, str(deployed)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1"


def write_incoming_batch(
    root: Path,
    *,
    instance_id: str = "INTEL",
    batch_id: str = "batch-test",
    agent_ids: tuple[str, ...] = ("sakura",),
    records: list[dict[str, object]] | None = None,
    manifest_updates: dict[str, object] | None = None,
) -> Path:
    batch = root / "private" / "remote_memory_incoming" / instance_id / batch_id
    batch.mkdir(parents=True)
    records = records or [
        {
            "schema_version": 1,
            "source_instance": instance_id,
            "source_agent": agent_ids[0],
            "source_kind": "transcript",
            "source_path": f"workspaces/{agent_ids[0]}/transcript.jsonl",
            "source_record_id": "1",
            "source_ts": "2026-09-29T00:00:00Z",
            "role": "assistant",
            "text": "A durable remote memory fact.",
            "content_hash": "sha256:test",
            "metadata": {},
        }
    ]
    payload = gzip.compress(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records).encode("utf-8")
    )
    manifest: dict[str, object] = {
        "schema_version": 1,
        "batch_id": batch_id,
        "instance_id": instance_id,
        "agent_ids": list(agent_ids),
        "created_at": "2026-09-29T00:00:00Z",
        "payload_file": "records.jsonl.gz",
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
        "record_count": len(records),
        "privacy_scan": {"status": "passed"},
    }
    manifest.update(manifest_updates or {})
    (batch / "records.jsonl.gz").write_bytes(payload)
    (batch / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    return batch


def test_relative_config_and_default_vault_are_root_bound(tmp_path: Path) -> None:
    config = tmp_path / "private" / "remote_memory_config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"mirror_root": "mirror"}), encoding="utf-8")
    args = build_parser().parse_args(["--root", str(tmp_path), "diagnose"])

    resolved = RemoteConfig.from_args(args)

    assert resolved.root == tmp_path.resolve()
    assert resolved.incoming_root == (tmp_path / "private" / "remote_memory_incoming").resolve()
    assert resolved.vault_root == (tmp_path / "wiki").resolve()
    assert resolved.mirror_root == (tmp_path / "mirror").resolve()

    overridden = RemoteConfig.from_args(
        build_parser().parse_args(
            ["--root", str(tmp_path), "--incoming-root", "incoming-override", "diagnose"]
        )
    )
    assert overridden.incoming_root == (tmp_path / "incoming-override").resolve()


def test_promote_incoming_atomically_moves_to_manifest_target(tmp_path: Path) -> None:
    source = write_incoming_batch(tmp_path, agent_ids=("sakura", "ying"))
    target = tmp_path / "private" / "remote_memory_inbox" / "INTEL" / "multi" / "batch-test"

    code = main(["--root", str(tmp_path), "promote-incoming"])

    assert code == 0
    assert not source.exists()
    assert target.is_dir()
    assert sorted(path.name for path in target.iterdir()) == ["manifest.json", "records.jsonl.gz"]
    events = [
        json.loads(line)
        for line in (tmp_path / "logs" / "remote_memory_promotion.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert events[-1]["promoted"] == 1
    assert events[-1]["failed"] == 0


def test_promote_incoming_check_and_dry_run_do_not_mutate(tmp_path: Path) -> None:
    source = write_incoming_batch(tmp_path)
    target = tmp_path / "private" / "remote_memory_inbox" / "INTEL" / "sakura" / "batch-test"

    assert main(["--root", str(tmp_path), "promote-incoming", "--check"]) == 0
    assert main(["--root", str(tmp_path), "promote-incoming", "--dry-run"]) == 0

    assert source.is_dir()
    assert not target.exists()
    assert not (tmp_path / "logs").exists()


def test_promote_incoming_identical_target_is_idempotent(tmp_path: Path) -> None:
    source = write_incoming_batch(tmp_path)
    target = tmp_path / "private" / "remote_memory_inbox" / "INTEL" / "sakura" / "batch-test"
    target.mkdir(parents=True)
    for path in source.iterdir():
        (target / path.name).write_bytes(path.read_bytes())

    code = main(["--root", str(tmp_path), "promote-incoming"])

    assert code == 0
    assert source.is_dir()
    assert target.is_dir()
    event = json.loads(
        (tmp_path / "logs" / "remote_memory_promotion.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[-1]
    )
    assert event["already_present"] == 1


def test_promote_incoming_identical_legacy_all_target_is_idempotent(tmp_path: Path) -> None:
    source = write_incoming_batch(tmp_path, agent_ids=("sakura", "ying"))
    target = tmp_path / "private" / "remote_memory_inbox" / "INTEL" / "all" / "batch-test"
    target.mkdir(parents=True)
    for path in source.iterdir():
        (target / path.name).write_bytes(path.read_bytes())

    code = main(["--root", str(tmp_path), "promote-incoming"])

    assert code == 0
    assert source.is_dir()
    assert target.is_dir()
    assert not (
        tmp_path / "private" / "remote_memory_inbox" / "INTEL" / "multi" / "batch-test"
    ).exists()
    event = json.loads(
        (tmp_path / "logs" / "remote_memory_promotion.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[-1]
    )
    assert event["already_present"] == 1
    assert event["results"][0]["target"] == str(target)


def test_promote_incoming_conflicting_legacy_all_target_fails_closed(tmp_path: Path) -> None:
    source = write_incoming_batch(tmp_path)
    target = tmp_path / "private" / "remote_memory_inbox" / "INTEL" / "all" / "batch-test"
    target.mkdir(parents=True)
    for path in source.iterdir():
        (target / path.name).write_bytes(path.read_bytes())
    (target / "records.jsonl.gz").write_bytes(b"different")

    code = main(["--root", str(tmp_path), "promote-incoming"])

    assert code != 0
    assert source.is_dir()
    assert target.is_dir()
    assert (target / "records.jsonl.gz").read_bytes() == b"different"


def test_promote_incoming_conflict_fails_and_preserves_both_sides(tmp_path: Path) -> None:
    source = write_incoming_batch(tmp_path)
    target = tmp_path / "private" / "remote_memory_inbox" / "INTEL" / "sakura" / "batch-test"
    target.mkdir(parents=True)
    for path in source.iterdir():
        (target / path.name).write_bytes(path.read_bytes())
    (target / "records.jsonl.gz").write_bytes(b"different")

    code = main(["--root", str(tmp_path), "promote-incoming"])

    assert code != 0
    assert source.is_dir()
    assert target.is_dir()
    assert (target / "records.jsonl.gz").read_bytes() == b"different"


def test_promote_incoming_rejects_unexpected_source_or_target_entries(tmp_path: Path) -> None:
    unexpected_source = write_incoming_batch(tmp_path, batch_id="source-extra")
    (unexpected_source / "unexpected.txt").write_text("unexpected", encoding="utf-8")

    unexpected_target = write_incoming_batch(tmp_path, batch_id="target-extra")
    target = tmp_path / "private" / "remote_memory_inbox" / "INTEL" / "sakura" / "target-extra"
    target.mkdir(parents=True)
    for path in unexpected_target.iterdir():
        (target / path.name).write_bytes(path.read_bytes())
    (target / "unexpected.txt").write_text("unexpected", encoding="utf-8")

    code = main(["--root", str(tmp_path), "promote-incoming"])

    assert code != 0
    assert unexpected_source.is_dir()
    assert unexpected_target.is_dir()
    assert target.is_dir()


def test_promote_incoming_invalid_or_incomplete_batches_remain_incoming(tmp_path: Path) -> None:
    checksum = write_incoming_batch(
        tmp_path,
        batch_id="bad-checksum",
        manifest_updates={"payload_sha256": "0" * 64},
    )
    incomplete = tmp_path / "private" / "remote_memory_incoming" / "INTEL" / "incomplete"
    incomplete.mkdir(parents=True)
    private = write_incoming_batch(
        tmp_path,
        batch_id="private-content",
        records=[{"text": "private relationship memory"}],
    )

    code = main(["--root", str(tmp_path), "promote-incoming"])

    assert code != 0
    assert checksum.is_dir()
    assert incomplete.is_dir()
    assert private.is_dir()
    assert not (tmp_path / "private" / "remote_memory_inbox").exists()


def test_promote_incoming_empty_root_is_successful_noop(tmp_path: Path) -> None:
    code = main(["--root", str(tmp_path), "promote-incoming"])

    assert code == 0
    event = json.loads(
        (tmp_path / "logs" / "remote_memory_promotion.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[-1]
    )
    assert event["batches"] == 0
    assert event["ok"] is True


def test_promote_incoming_rejects_traversal_and_identity_mismatch(tmp_path: Path) -> None:
    traversal = write_incoming_batch(
        tmp_path,
        batch_id="traversal",
        manifest_updates={"payload_file": "../records.jsonl.gz"},
    )
    mismatch = write_incoming_batch(
        tmp_path,
        batch_id="directory-name",
        manifest_updates={"batch_id": "manifest-name"},
    )

    traversal_code = main(
        ["--root", str(tmp_path), "promote-incoming", "--batch-dir", str(traversal)]
    )
    mismatch_code = main(
        ["--root", str(tmp_path), "promote-incoming", "--batch-dir", str(mismatch)]
    )

    assert traversal_code != 0
    assert mismatch_code != 0
    assert traversal.is_dir()
    assert mismatch.is_dir()
    assert not (tmp_path / "private" / "remote_memory_inbox").exists()


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


def test_import_ack_is_durable_idempotent_and_bound_to_batch_bytes(tmp_path: Path) -> None:
    source = write_incoming_batch(tmp_path, batch_id="ack-test")
    inbox_batch = tmp_path / "private" / "remote_memory_inbox" / "INTEL" / "sakura" / "ack-test"
    inbox_batch.parent.mkdir(parents=True)
    source.rename(inbox_batch)
    db = tmp_path / "workspaces" / "lily" / "consolidated_memory.sqlite"
    args = [
        "--root",
        str(tmp_path),
        "--consolidated-db",
        str(db),
        "import",
        "--batch-dir",
        str(inbox_batch),
        "--acknowledge",
        "--destination-instance-id",
        "HASHI4",
    ]

    assert main(args) == 0
    receipt_path = tmp_path / "private" / "remote_memory_import_ack" / "INTEL" / "ack-test.json"
    first_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    first_bytes = receipt_path.read_bytes()
    assert first_receipt["format"] == "hashi-remote-memory-import-ack-v1"
    assert first_receipt["status"] == "imported"
    assert first_receipt["destination_instance_id"] == "HASHI4"
    assert first_receipt["accepted"] == 1
    assert first_receipt["duplicates"] == 0

    assert main(args) == 0

    assert receipt_path.read_bytes() == first_bytes
    events = [
        json.loads(line)
        for line in (tmp_path / "logs" / "remote_memory_import_ack.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if json.loads(line).get("mode") == "import-ack"
    ]
    assert [event["receipt_status"] for event in events] == ["created", "already_acknowledged"]


def test_import_ack_conflict_fails_without_quarantining_committed_batch(tmp_path: Path) -> None:
    source = write_incoming_batch(tmp_path, batch_id="ack-conflict")
    inbox_batch = tmp_path / "private" / "remote_memory_inbox" / "INTEL" / "sakura" / "ack-conflict"
    inbox_batch.parent.mkdir(parents=True)
    source.rename(inbox_batch)
    db = tmp_path / "workspaces" / "lily" / "consolidated_memory.sqlite"
    args = [
        "--root",
        str(tmp_path),
        "--consolidated-db",
        str(db),
        "import",
        "--batch-dir",
        str(inbox_batch),
        "--acknowledge",
        "--destination-instance-id",
        "HASHI4",
    ]

    assert main(args) == 0
    receipt_path = tmp_path / "private" / "remote_memory_import_ack" / "INTEL" / "ack-conflict.json"
    first_bytes = receipt_path.read_bytes()
    manifest = json.loads((inbox_batch / "manifest.json").read_text(encoding="utf-8"))
    changed_record = {
        "schema_version": 1,
        "source_instance": "INTEL",
        "source_agent": "sakura",
        "source_kind": "transcript",
        "source_path": "workspaces/sakura/transcript.jsonl",
        "source_record_id": "2",
        "source_ts": "2026-09-29T00:01:00Z",
        "role": "assistant",
        "text": "A different durable remote memory fact.",
        "content_hash": "sha256:changed",
        "metadata": {},
    }
    payload = gzip.compress((json.dumps(changed_record, sort_keys=True) + "\n").encode("utf-8"))
    (inbox_batch / "records.jsonl.gz").write_bytes(payload)
    manifest["payload_sha256"] = hashlib.sha256(payload).hexdigest()
    manifest["record_count"] = 1
    (inbox_batch / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")

    assert main(args) == 3

    assert receipt_path.read_bytes() == first_bytes
    assert inbox_batch.is_dir()
    assert not (tmp_path / "private" / "remote_memory_quarantine" / "ack-conflict").exists()


def test_import_ack_rejects_unsafe_batch_id_without_creating_outside_receipt(tmp_path: Path) -> None:
    source = write_incoming_batch(tmp_path, batch_id="ack-unsafe")
    inbox_batch = tmp_path / "private" / "remote_memory_inbox" / "INTEL" / "sakura" / "ack-unsafe"
    inbox_batch.parent.mkdir(parents=True)
    source.rename(inbox_batch)
    manifest_path = inbox_batch / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["batch_id"] = "../escape"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    db = tmp_path / "workspaces" / "lily" / "consolidated_memory.sqlite"

    code = main(
        [
            "--root",
            str(tmp_path),
            "--consolidated-db",
            str(db),
            "import",
            "--batch-dir",
            str(inbox_batch),
            "--acknowledge",
            "--destination-instance-id",
            "HASHI4",
        ]
    )

    assert code == 3
    assert inbox_batch.is_dir()
    assert not (tmp_path / "private" / "remote_memory_import_ack").exists()


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

    code = main(
        [
            "--root",
            str(root),
            "import",
            "--batch-dir",
            str(batch),
            "--acknowledge",
            "--destination-instance-id",
            "HASHI4",
        ]
    )

    assert code == 3
    assert not batch.exists()
    assert (root / "private" / "remote_memory_quarantine" / "bad-batch" / "quarantine_reason.txt").exists()
    assert not (root / "private" / "remote_memory_import_ack").exists()


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
