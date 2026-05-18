#!/usr/bin/env python3
"""Remote memory consolidation package for cross-LAN HASHI wiki management.

This script is intentionally standalone. It does not require HASHI runtime
restart, command wiring, or scheduler changes. Remote machines can run export
and sync commands on startup/cron, while Lily PC can run import before the wiki
pipeline.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    from scripts.wiki.fetcher import has_private_content, has_sensitive_content
except Exception:  # pragma: no cover - keeps remote package usable before wiki deps import cleanly
    PRIVATE_FALLBACK_TERMS = ("亲一下", "好想你", "想念你", "我爱你", "relationship", "romantic", "intimate")

    def has_private_content(content: str) -> bool:
        lowered = (content or "").lower()
        return any(term in lowered for term in PRIVATE_FALLBACK_TERMS)

    def has_sensitive_content(content: str) -> bool:
        lowered = (content or "").lower()
        return any(term in lowered for term in ("password=", "passwd=", "secret=", "api_key="))


SCHEMA_VERSION = 1
DEFAULT_CONFIG = Path("private/remote_memory_config.json")
GENERATED_ZONE_NAMES = ("10_GENERATED_TOPICS", "30_GENERATED_INDEXES", "00_SYSTEM")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def stable_source_id(*parts: str) -> int:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return int(digest[:15], 16)


def log_event(log_path: Path, event: dict[str, Any]) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"ts": utc_now(), **event}
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return payload


def dump_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
    tmp.replace(path)


@dataclass(frozen=True)
class RemoteConfig:
    root: Path
    export_root: Path
    inbox_root: Path
    accepted_store: Path
    quarantine_root: Path
    logs_root: Path
    vault_root: Path
    mirror_root: Path
    consolidated_db: Path

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "RemoteConfig":
        root = Path(args.root).resolve()
        config_path = Path(args.config)
        data: dict[str, Any] = {}
        if config_path.exists():
            data = load_json(config_path)
        base_private = root / "private"
        return cls(
            root=root,
            export_root=Path(args.export_root or data.get("export_root") or base_private / "remote_memory_export").resolve(),
            inbox_root=Path(args.inbox_root or data.get("inbox_root") or base_private / "remote_memory_inbox").resolve(),
            accepted_store=Path(args.accepted_store or data.get("accepted_store") or base_private / "remote_memory_accepted" / "accepted_records.jsonl").resolve(),
            quarantine_root=Path(args.quarantine_root or data.get("quarantine_root") or base_private / "remote_memory_quarantine").resolve(),
            logs_root=Path(args.logs_root or data.get("logs_root") or root / "logs").resolve(),
            vault_root=Path(args.vault_root or data.get("vault_root") or "/mnt/c/Users/thene/Documents/lily_hashi_wiki").resolve(),
            mirror_root=Path(args.mirror_root or data.get("mirror_root") or base_private / "remote_wiki_mirror").resolve(),
            consolidated_db=Path(args.consolidated_db or data.get("consolidated_db") or root / "workspaces/lily/consolidated_memory.sqlite").resolve(),
        )


def print_header(mode: str, cfg: RemoteConfig, args: argparse.Namespace) -> None:
    print(f"[remote-memory] mode={mode}")
    print(f"[remote-memory] root={cfg.root}")
    print(f"[remote-memory] config={Path(args.config).resolve()}")
    print(f"[remote-memory] dry_run={bool(getattr(args, 'dry_run', False))}")
    print(f"[remote-memory] check={bool(getattr(args, 'check', False))}")
    print(f"[remote-memory] export_root={cfg.export_root}")
    print(f"[remote-memory] inbox_root={cfg.inbox_root}")
    print(f"[remote-memory] accepted_store={cfg.accepted_store}")
    print(f"[remote-memory] quarantine_root={cfg.quarantine_root}")
    print(f"[remote-memory] logs_root={cfg.logs_root}")


def iter_source_records(root: Path, instance_id: str, agent_ids: Iterable[str]) -> Iterable[dict[str, Any]]:
    for agent_id in agent_ids:
        workspace = root / "workspaces" / agent_id
        sources = (
            ("transcript", workspace / "transcript.jsonl"),
            ("left_brain_notepad", workspace / "memory" / "left_brain_continuity.jsonl"),
        )
        for source_kind, source_path in sources:
            if not source_path.exists():
                continue
            with source_path.open("r", encoding="utf-8", errors="replace") as handle:
                for line_no, line in enumerate(handle, start=1):
                    raw_line = line.strip()
                    if not raw_line:
                        continue
                    text, role, source_ts, metadata = parse_source_line(raw_line)
                    if not text.strip():
                        continue
                    yield {
                        "schema_version": SCHEMA_VERSION,
                        "source_instance": instance_id,
                        "source_agent": agent_id,
                        "source_kind": source_kind,
                        "source_path": str(source_path.relative_to(root)),
                        "source_record_id": str(metadata.get("id") or line_no),
                        "source_ts": source_ts or utc_now(),
                        "role": role,
                        "text": text,
                        "content_hash": f"sha256:{sha256_text(text)}",
                        "metadata": metadata,
                    }


def parse_source_line(raw_line: str) -> tuple[str, str, str | None, dict[str, Any]]:
    try:
        payload = json.loads(raw_line)
    except json.JSONDecodeError:
        return raw_line, "note", None, {"parse": "raw_text"}
    if not isinstance(payload, dict):
        return str(payload), "note", None, {"parse": "json_non_object"}
    role = str(payload.get("role") or payload.get("type") or "note")
    source_ts = payload.get("ts") or payload.get("timestamp") or payload.get("created_at")
    text = payload.get("text") or payload.get("content") or payload.get("message") or payload.get("note") or ""
    if isinstance(text, list):
        text = " ".join(str(item) for item in text)
    metadata = {key: value for key, value in payload.items() if key not in {"text", "content", "message", "note"}}
    return str(text), role, str(source_ts) if source_ts else None, metadata


def privacy_filter(records: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    accepted: list[dict[str, Any]] = []
    stats = {"seen": 0, "accepted": 0, "private_skipped": 0, "sensitive_redacted": 0}
    for record in records:
        stats["seen"] += 1
        text = str(record.get("text") or "")
        if has_private_content(text):
            stats["private_skipped"] += 1
            continue
        if has_sensitive_content(text):
            record = dict(record)
            record["text"] = "[REDACTED: sensitive content detected]"
            record["content_hash"] = f"sha256:{sha256_text(record['text'])}"
            stats["sensitive_redacted"] += 1
        accepted.append(record)
        stats["accepted"] += 1
    return accepted, stats


def cmd_export(args: argparse.Namespace) -> int:
    cfg = RemoteConfig.from_args(args)
    print_header("export", cfg, args)
    instance_id = args.instance_id.upper()
    agent_ids = args.agent or sorted(path.name for path in (cfg.root / "workspaces").glob("*") if path.is_dir())
    print(f"[remote-memory] instance_id={instance_id}")
    print(f"[remote-memory] agent_ids={','.join(agent_ids)}")
    log_path = cfg.logs_root / "remote_memory_export.jsonl"

    if args.check:
        ok = bool(agent_ids)
        print(f"[remote-memory] check_result={'ok' if ok else 'failed'}")
        log_event(log_path, {"mode": "check", "ok": ok, "instance_id": instance_id, "agents": agent_ids})
        return 0 if ok else 2

    records, privacy = privacy_filter(iter_source_records(cfg.root, instance_id, agent_ids))
    source_files = sorted({str(record.get("source_path")) for record in records if record.get("source_path")})
    batch_id = args.batch_id or f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{instance_id}_{len(records):06d}"
    batch_dir = cfg.export_root / "pending" / batch_id
    payload_name = "records.jsonl.gz"
    payload_bytes = gzip.compress(("".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in records)).encode("utf-8"))
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "batch_id": batch_id,
        "instance_id": instance_id,
        "agent_ids": agent_ids,
        "source_files": source_files,
        "created_at": utc_now(),
        "payload_file": payload_name,
        "payload_sha256": sha256_bytes(payload_bytes),
        "record_count": len(records),
        "privacy_scan": {"status": "passed", **privacy},
        "transport": "filesystem_or_hashi_remote",
    }
    print(f"[remote-memory] batch_id={batch_id}")
    print(f"[remote-memory] record_count={len(records)} privacy={privacy}")
    if not args.dry_run:
        batch_dir.mkdir(parents=True, exist_ok=False)
        (batch_dir / payload_name).write_bytes(payload_bytes)
        dump_json(batch_dir / "manifest.json", manifest)
    else:
        print(f"[remote-memory] dry-run would write batch_dir={batch_dir}")
    log_event(log_path, {"mode": "export", "dry_run": args.dry_run, "ok": True, "batch_id": batch_id, "record_count": len(records), "privacy": privacy})
    print("[remote-memory] success=true")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    cfg = RemoteConfig.from_args(args)
    print_header("import", cfg, args)
    log_path = cfg.logs_root / "remote_memory_import.jsonl"
    if args.check:
        print(f"[remote-memory] consolidated_db_exists={cfg.consolidated_db.exists()}")
        print("[remote-memory] check_result=ok")
        log_event(log_path, {"mode": "check", "ok": True})
        return 0

    batch_dirs = sorted(path for path in cfg.inbox_root.glob("*/*/*") if path.is_dir())
    if args.batch_dir:
        batch_dirs = [Path(args.batch_dir).resolve()]
    stats = {"batches": 0, "accepted": 0, "duplicates": 0, "quarantined": 0, "failed": 0}
    for batch_dir in batch_dirs:
        stats["batches"] += 1
        try:
            result = import_batch(cfg, batch_dir, dry_run=args.dry_run)
            for key in ("accepted", "duplicates"):
                stats[key] += int(result.get(key, 0))
            print(f"[remote-memory] imported batch={batch_dir.name} result={result}")
        except Exception as exc:
            stats["failed"] += 1
            stats["quarantined"] += 1
            print(f"[remote-memory] ERROR batch={batch_dir} error={exc}", file=sys.stderr)
            if not args.dry_run:
                quarantine_batch(cfg, batch_dir, str(exc))
    log_event(log_path, {"mode": "import", "dry_run": args.dry_run, "ok": stats["failed"] == 0, **stats})
    print(f"[remote-memory] summary={stats}")
    print(f"[remote-memory] success={stats['failed'] == 0}")
    return 0 if stats["failed"] == 0 else 3


def import_batch(cfg: RemoteConfig, batch_dir: Path, *, dry_run: bool) -> dict[str, int]:
    manifest_path = batch_dir / "manifest.json"
    manifest = load_json(manifest_path)
    validate_manifest(manifest)
    payload_path = batch_dir / str(manifest["payload_file"])
    payload_bytes = payload_path.read_bytes()
    actual_hash = sha256_bytes(payload_bytes)
    print(f"[remote-memory] checksum_status={'passed' if actual_hash == manifest['payload_sha256'] else 'failed'} batch={manifest['batch_id']}")
    if actual_hash != manifest["payload_sha256"]:
        raise ValueError(f"checksum mismatch expected={manifest['payload_sha256']} actual={actual_hash}")
    records = [json.loads(line) for line in gzip.decompress(payload_bytes).decode("utf-8").splitlines() if line.strip()]
    if len(records) != int(manifest["record_count"]):
        raise ValueError(f"record_count mismatch manifest={manifest['record_count']} actual={len(records)}")
    accepted_records, privacy = privacy_filter(records)
    if privacy["private_skipped"]:
        raise ValueError(f"privacy scan rejected {privacy['private_skipped']} private records")
    accepted = 0
    duplicates = 0
    if not dry_run:
        cfg.accepted_store.parent.mkdir(parents=True, exist_ok=True)
        cfg.consolidated_db.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(cfg.consolidated_db) as con, cfg.accepted_store.open("a", encoding="utf-8") as accepted_handle:
            ensure_consolidated_schema(con)
            for record in accepted_records:
                inserted = insert_consolidated(con, manifest, record)
                if inserted:
                    accepted += 1
                    accepted_handle.write(json.dumps({"batch_id": manifest["batch_id"], **record}, ensure_ascii=False, sort_keys=True) + "\n")
                else:
                    duplicates += 1
            con.commit()
    else:
        accepted = len(accepted_records)
    return {"accepted": accepted, "duplicates": duplicates}


def validate_manifest(manifest: dict[str, Any]) -> None:
    required = ("schema_version", "batch_id", "instance_id", "payload_file", "payload_sha256", "record_count")
    missing = [key for key in required if key not in manifest]
    if missing:
        raise ValueError(f"manifest missing required fields: {missing}")
    if int(manifest["schema_version"]) != SCHEMA_VERSION:
        raise ValueError(f"unsupported schema_version={manifest['schema_version']}")
    if "/" in str(manifest["payload_file"]) or "\\" in str(manifest["payload_file"]):
        raise ValueError("payload_file must be a local filename")
    privacy = manifest.get("privacy_scan")
    if not isinstance(privacy, dict) or privacy.get("status") != "passed":
        raise ValueError("manifest privacy_scan.status must be passed")


def cmd_deliver(args: argparse.Namespace) -> int:
    cfg = RemoteConfig.from_args(args)
    print_header("deliver", cfg, args)
    log_path = cfg.logs_root / "remote_memory_delivery.jsonl"
    pending_root = cfg.export_root / "pending"
    if args.check:
        ok = pending_root.exists()
        print(f"[remote-memory] pending_root_exists={ok}")
        print(f"[remote-memory] check_result={'ok' if ok else 'failed'}")
        log_event(log_path, {"mode": "check", "ok": ok, "pending_root": str(pending_root)})
        return 0 if ok else 2
    batch_dirs = [pending_root / args.batch_id] if args.batch_id else sorted(path for path in pending_root.glob("*") if path.is_dir())
    delivered = 0
    failed = 0
    for batch_dir in batch_dirs:
        try:
            manifest = load_json(batch_dir / "manifest.json")
            validate_manifest(manifest)
            payload = batch_dir / str(manifest["payload_file"])
            checksum = sha256_bytes(payload.read_bytes())
            if checksum != manifest["payload_sha256"]:
                raise ValueError(f"pending payload checksum mismatch for {batch_dir.name}")
            agent_ids = manifest.get("agent_ids") or ["multi"]
            agent_part = str(agent_ids[0]) if len(agent_ids) == 1 else "multi"
            target_inbox = Path(args.target_inbox).resolve() if args.target_inbox else cfg.inbox_root
            target = target_inbox / str(manifest["instance_id"]) / agent_part / str(manifest["batch_id"])
            print(f"[remote-memory] deliver batch={manifest['batch_id']} source={batch_dir} target={target} checksum_status=passed")
            if not args.dry_run:
                target.mkdir(parents=True, exist_ok=True)
                shutil.copy2(batch_dir / "manifest.json", target / "manifest.json")
                shutil.copy2(payload, target / payload.name)
            delivered += 1
        except Exception as exc:
            failed += 1
            print(f"[remote-memory] ERROR deliver batch={batch_dir} error={exc}", file=sys.stderr)
    log_event(log_path, {"mode": "deliver", "dry_run": args.dry_run, "ok": failed == 0, "delivered": delivered, "failed": failed})
    print(f"[remote-memory] delivered={delivered} failed={failed}")
    print(f"[remote-memory] success={failed == 0}")
    return 0 if failed == 0 else 4


def quarantine_batch(cfg: RemoteConfig, batch_dir: Path, reason: str) -> None:
    target = cfg.quarantine_root / batch_dir.name
    if target.exists():
        target = cfg.quarantine_root / f"{batch_dir.name}_{datetime.now(timezone.utc).strftime('%H%M%S')}"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(batch_dir), str(target))
    (target / "quarantine_reason.txt").write_text(reason + "\n", encoding="utf-8")


def ensure_consolidated_schema(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS consolidated (
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
            ts_source       TEXT    NOT NULL DEFAULT 'remote_memory',
            consolidated_at TEXT    NOT NULL,
            UNIQUE(instance, agent_id, source_id)
        )
        """
    )


def insert_consolidated(con: sqlite3.Connection, manifest: dict[str, Any], record: dict[str, Any]) -> bool:
    source_id = stable_source_id(
        str(record.get("source_instance") or manifest["instance_id"]),
        str(record.get("source_agent") or ""),
        str(record.get("source_path") or ""),
        str(record.get("source_record_id") or ""),
        str(record.get("content_hash") or ""),
    )
    cursor = con.execute(
        """
        INSERT OR IGNORE INTO consolidated(
            instance, agent_id, source_id, domain, memory_type, importance,
            content, summary, embedding, source_ts, ts_source, consolidated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?)
        """,
        (
            str(record.get("source_instance") or manifest["instance_id"]),
            str(record.get("source_agent") or "unknown"),
            source_id,
            "remote_memory",
            "episodic",
            1.0,
            str(record.get("text") or ""),
            str(record.get("source_ts") or manifest.get("created_at") or utc_now()),
            f"remote:{manifest['batch_id']}:{record.get('source_kind')}:{record.get('source_path')}",
            utc_now(),
        ),
    )
    return cursor.rowcount > 0


def cmd_sync_wiki(args: argparse.Namespace) -> int:
    cfg = RemoteConfig.from_args(args)
    print_header("sync-wiki", cfg, args)
    log_path = cfg.logs_root / "remote_wiki_sync.jsonl"
    print(f"[remote-memory] vault_root={cfg.vault_root}")
    print(f"[remote-memory] mirror_root={cfg.mirror_root}")
    if args.check:
        missing = [zone for zone in GENERATED_ZONE_NAMES if not (cfg.vault_root / zone).exists()]
        writable_parent = cfg.mirror_root.exists() or cfg.mirror_root.parent.exists()
        print(f"[remote-memory] generated_zones_missing={missing}")
        print(f"[remote-memory] mirror_parent_available={writable_parent}")
        ok = not missing and writable_parent
        log_event(log_path, {"mode": "check", "ok": ok, "missing": missing, "mirror_root": str(cfg.mirror_root)})
        print(f"[remote-memory] check_result={'ok' if ok else 'failed'}")
        return 0 if ok else 2
    copied = 0
    missing: list[str] = []
    for zone in GENERATED_ZONE_NAMES:
        source = cfg.vault_root / zone
        target = cfg.mirror_root / zone
        if not source.exists():
            missing.append(zone)
            continue
        if not args.dry_run:
            tmp_target = target.with_name(f".{target.name}.tmp")
            if tmp_target.exists():
                shutil.rmtree(tmp_target)
            shutil.copytree(source, tmp_target)
            if target.exists():
                shutil.rmtree(target)
            tmp_target.rename(target)
        copied += 1
        print(f"[remote-memory] generated_zone={zone} source={source} target={target}")
    manifest = {"schema_version": SCHEMA_VERSION, "synced_at": utc_now(), "source_vault": str(cfg.vault_root), "zones": [z for z in GENERATED_ZONE_NAMES if z not in missing], "missing": missing}
    if not args.dry_run:
        dump_json(cfg.mirror_root / "remote_wiki_mirror_manifest.json", manifest)
    log_event(log_path, {"mode": "sync-wiki", "dry_run": args.dry_run, "ok": True, "copied_zones": copied, "missing": missing})
    print(f"[remote-memory] copied_zones={copied} missing={missing}")
    print("[remote-memory] success=true")
    return 0


def cmd_diagnose(args: argparse.Namespace) -> int:
    cfg = RemoteConfig.from_args(args)
    print_header("diagnose", cfg, args)
    print(f"[remote-memory] consolidated_db_exists={cfg.consolidated_db.exists()}")
    print(f"[remote-memory] vault_root_exists={cfg.vault_root.exists()}")
    print(f"[remote-memory] mirror_root_exists={cfg.mirror_root.exists()}")
    print(f"[remote-memory] pending_batches={len(list((cfg.export_root / 'pending').glob('*'))) if (cfg.export_root / 'pending').exists() else 0}")
    print("[remote-memory] success=true")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="HASHI remote memory consolidation package")
    parser.add_argument("--root", default=".", help="HASHI repo root")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Optional private config JSON")
    parser.add_argument("--export-root")
    parser.add_argument("--inbox-root")
    parser.add_argument("--accepted-store")
    parser.add_argument("--quarantine-root")
    parser.add_argument("--logs-root")
    parser.add_argument("--vault-root")
    parser.add_argument("--mirror-root")
    parser.add_argument("--consolidated-db")
    sub = parser.add_subparsers(dest="command", required=True)

    export = sub.add_parser("export", help="Export remote memory batch")
    export.add_argument("--instance-id", required=True)
    export.add_argument("--agent", action="append", help="Agent id to export; repeatable. Defaults to all workspaces.")
    export.add_argument("--batch-id")
    export.add_argument("--dry-run", action="store_true")
    export.add_argument("--check", action="store_true")
    export.set_defaults(func=cmd_export)

    deliver = sub.add_parser("deliver", help="Copy pending export batches to a central inbox/drop folder")
    deliver.add_argument("--batch-id", help="Pending batch id. Defaults to all pending batches.")
    deliver.add_argument("--target-inbox", help="Override central inbox/drop folder")
    deliver.add_argument("--dry-run", action="store_true")
    deliver.add_argument("--check", action="store_true")
    deliver.set_defaults(func=cmd_deliver)

    imp = sub.add_parser("import", help="Import central inbox batches")
    imp.add_argument("--batch-dir")
    imp.add_argument("--dry-run", action="store_true")
    imp.add_argument("--check", action="store_true")
    imp.set_defaults(func=cmd_import)

    sync = sub.add_parser("sync-wiki", help="Copy generated wiki zones to read-only mirror/cache")
    sync.add_argument("--dry-run", action="store_true")
    sync.add_argument("--check", action="store_true")
    sync.set_defaults(func=cmd_sync_wiki)

    diag = sub.add_parser("diagnose", help="Print package diagnostics")
    diag.add_argument("--dry-run", action="store_true")
    diag.add_argument("--check", action="store_true")
    diag.set_defaults(func=cmd_diagnose)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:
        print(f"[remote-memory] fatal={exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
