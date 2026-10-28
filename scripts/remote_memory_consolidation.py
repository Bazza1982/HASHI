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
import os
import re
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# Remote export has its own privacy boundary. Keep these checks available when
# this standalone script is deployed outside the HASHI source checkout.
PRIVATE_CONTENT_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"爱的拥抱",
        r"深情的吻",
        r"亲一下",
        r"好想你",
        r"想念你",
        r"喜欢你",
        r"我爱你",
        r"(?i)\bintimate\b",
        r"(?i)\bromantic\b",
        r"(?i)\bpersonal life\b",
        r"(?i)\brelationship\b",
    )
)
SENSITIVE_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"[A-Za-z0-9_\-]{20,}:[A-Za-z0-9_\-]{30,}",
        r"sk-[A-Za-z0-9]{32,}",
        r"eyJ[A-Za-z0-9+/]{40,}",
        r"(?i)(password|passwd|secret|api.?key)\s*[=:]\s*\S+",
    )
)


def has_private_content(content: str) -> bool:
    return any(pattern.search(content or "") for pattern in PRIVATE_CONTENT_PATTERNS)


def has_sensitive_content(content: str) -> bool:
    return any(pattern.search(content or "") for pattern in SENSITIVE_PATTERNS)


SCHEMA_VERSION = 1
DEFAULT_CONFIG = Path("private/remote_memory_config.json")
GENERATED_ZONE_NAMES = ("10_GENERATED_TOPICS", "30_GENERATED_INDEXES", "00_SYSTEM")
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


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
    incoming_root: Path
    inbox_root: Path
    import_ack_root: Path
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
        if not config_path.is_absolute():
            config_path = root / config_path
        data: dict[str, Any] = {}
        if config_path.exists():
            data = load_json(config_path)
        base_private = root / "private"

        def resolve_path(value: Any, default: Path) -> Path:
            candidate = Path(value) if value else default
            if not candidate.is_absolute():
                candidate = root / candidate
            return candidate.resolve()

        return cls(
            root=root,
            export_root=resolve_path(
                args.export_root or data.get("export_root"),
                base_private / "remote_memory_export",
            ),
            incoming_root=resolve_path(
                args.incoming_root or data.get("incoming_root"),
                base_private / "remote_memory_incoming",
            ),
            inbox_root=resolve_path(
                args.inbox_root or data.get("inbox_root"),
                base_private / "remote_memory_inbox",
            ),
            import_ack_root=resolve_path(
                data.get("import_ack_root"),
                base_private / "remote_memory_import_ack",
            ),
            accepted_store=resolve_path(
                args.accepted_store or data.get("accepted_store"),
                base_private / "remote_memory_accepted" / "accepted_records.jsonl",
            ),
            quarantine_root=resolve_path(
                args.quarantine_root or data.get("quarantine_root"),
                base_private / "remote_memory_quarantine",
            ),
            logs_root=resolve_path(
                args.logs_root or data.get("logs_root"),
                root / "logs",
            ),
            vault_root=resolve_path(
                args.vault_root or data.get("vault_root"),
                root / "wiki",
            ),
            mirror_root=resolve_path(
                args.mirror_root or data.get("mirror_root"),
                base_private / "remote_wiki_mirror",
            ),
            consolidated_db=resolve_path(
                args.consolidated_db
                or data.get("consolidated_db"),
                root / "workspaces" / "lily" / "consolidated_memory.sqlite",
            ),
        )


def print_header(mode: str, cfg: RemoteConfig, args: argparse.Namespace) -> None:
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = cfg.root / config_path
    print(f"[remote-memory] mode={mode}")
    print(f"[remote-memory] root={cfg.root}")
    print(f"[remote-memory] config={config_path.resolve()}")
    print(f"[remote-memory] dry_run={bool(getattr(args, 'dry_run', False))}")
    print(f"[remote-memory] check={bool(getattr(args, 'check', False))}")
    print(f"[remote-memory] export_root={cfg.export_root}")
    print(f"[remote-memory] inbox_root={cfg.inbox_root}")
    print(f"[remote-memory] accepted_store={cfg.accepted_store}")
    print(f"[remote-memory] quarantine_root={cfg.quarantine_root}")
    print(f"[remote-memory] logs_root={cfg.logs_root}")


def _forget_export_quarantined(workspace: Path) -> bool:
    """Standalone fail-closed guard for audit transcripts after /forget."""
    database = workspace / "bridge_memory.sqlite"
    if not database.exists():
        return False
    if database.is_symlink():
        raise RuntimeError("memory export boundary is a link")
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        tables = {str(row[0]) for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'pcm_forgotten_%'"
        )}
        if not tables:
            return False
        if not {"pcm_forgotten_ranges", "pcm_forgotten_origins"}.issubset(tables):
            raise RuntimeError("memory export boundary is incomplete")
        return connection.execute("SELECT 1 FROM pcm_forgotten_ranges LIMIT 1").fetchone() is not None


def _safe_forget_lines(workspace: Path, source_path: Path, source_kind: str):
    """Export only intact old rounds and records appended after cleanup."""
    database = workspace / "bridge_memory.sqlite"
    relative = source_path.relative_to(workspace).as_posix()
    if source_path.is_symlink() or source_path.parent.is_symlink() or not source_path.is_file():
        return []
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        tables = {str(row[0]) for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if "pcm_forget_export_checkpoints" not in tables:
            return []
        checkpoint = connection.execute(
            """SELECT prefix_size, prefix_sha256, recorded_at
               FROM pcm_forget_export_checkpoints WHERE source_path=?""", (relative,),
        ).fetchone()
        history = []
        if "pcm_forget_export_checkpoint_history" in tables:
            history = list(connection.execute(
                """SELECT prefix_size, prefix_sha256, recorded_at
                   FROM pcm_forget_export_checkpoint_history WHERE source_path=?""",
                (relative,),
            ))
        windows = list(connection.execute(
            "SELECT start_epoch, end_epoch FROM pcm_forgotten_ranges"
        ))
    if checkpoint is None or not windows:
        return []
    offset, expected_hash, recorded_at = checkpoint
    try:
        checkpoint_time = datetime.fromisoformat(str(recorded_at).replace("Z", "+00:00"))
        if checkpoint_time.tzinfo is None or checkpoint_time.utcoffset() is None:
            return []
        checkpoint_epoch = checkpoint_time.timestamp()
        first_start = min(float(row[0]) for row in windows)
        before = source_path.stat()
        if offset < 0 or before.st_size < offset:
            return []
        candidates = []
        for old_offset, old_hash, old_time in history + [checkpoint]:
            try:
                instant = datetime.fromisoformat(str(old_time).replace("Z", "+00:00"))
                if instant.tzinfo is None or instant.utcoffset() is None:
                    continue
                if 0 <= int(old_offset) <= int(offset) and instant.timestamp() <= checkpoint_epoch:
                    candidates.append((int(old_offset), str(old_hash), str(old_time), instant.timestamp()))
            except (TypeError, ValueError):
                continue
        valid_checkpoints = []
        digest = hashlib.sha256()
        with source_path.open("rb") as stream:
            position = 0
            for old_offset, old_hash, old_time, old_epoch in sorted(candidates):
                remaining = old_offset - position
                while remaining:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        return []
                    digest.update(chunk)
                    position += len(chunk)
                    remaining -= len(chunk)
                if digest.hexdigest() == old_hash:
                    valid_checkpoints.append((old_offset, old_hash, old_time, old_epoch))
            if not any(item[:3] == (int(offset), str(expected_hash), str(recorded_at))
                       for item in valid_checkpoints):
                return []
            stream.seek(0)
            parsed = []
            position = 0
            line_no = 0
            while raw := stream.readline():
                start = position
                position += len(raw)
                line_no += 1
                if not raw.endswith(b"\n"):
                    continue
                text, role, timestamp, metadata = parse_source_line(raw.decode("utf-8", errors="strict").strip())
                try:
                    instant = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
                    if instant.tzinfo is None or instant.utcoffset() is None:
                        instant = None
                except (TypeError, ValueError):
                    instant = None
                parsed.append((start, position, line_no, text, role, timestamp, metadata, instant))
        after = source_path.stat()
        if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
            return []
    except (OSError, ValueError, UnicodeError):
        return []

    def safe(group):
        if not group or any(row[7] is None for row in group):
            return False
        epochs = [row[7].timestamp() for row in group]
        if any(lower <= value <= upper for value in epochs for lower, upper in windows):
            return False
        old = (source_kind == "transcript" and all(row[1] <= offset for row in group)
               and all(value < first_start for value in epochs))
        trusted = valid_checkpoints if source_kind == "transcript" else [
            (int(offset), str(expected_hash), str(recorded_at), checkpoint_epoch)
        ]
        new = any(all(row[0] >= point[0] for row in group)
                  and all(value >= point[3] for value in epochs) for point in trusted)
        return old or new

    if source_kind != "transcript":
        return [row[2:7] for row in parsed if safe([row])]
    groups = []
    current = []
    for row in parsed:
        if row[4] == "user":
            if current:
                groups.append(current)
            current = [row]
        elif current:
            current.append(row)
    if current:
        groups.append(current)
    return [row[2:7] for group in groups
            if any(row[4] == "assistant" for row in group) and safe(group)
            for row in group]


def iter_source_records(root: Path, instance_id: str, agent_ids: Iterable[str]) -> Iterable[dict[str, Any]]:
    for agent_id in agent_ids:
        workspace = root / "workspaces" / agent_id
        guarded = _forget_export_quarantined(workspace)
        sources = (
            ("transcript", workspace / "transcript.jsonl"),
            ("left_brain_notepad", workspace / "memory" / "left_brain_continuity.jsonl"),
        )

        def ordinary_lines(source_path):
            with source_path.open("r", encoding="utf-8", errors="replace") as handle:
                for line_no, line in enumerate(handle, start=1):
                    if line.strip():
                        yield (line_no, *parse_source_line(line.strip()))

        for source_kind, source_path in sources:
            if not source_path.exists():
                continue
            lines = (_safe_forget_lines(workspace, source_path, source_kind)
                     if guarded else ordinary_lines(source_path))
            for line_no, text, role, source_ts, metadata in lines:
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
    ack_log_path = cfg.logs_root / "remote_memory_import_ack.jsonl"
    if args.acknowledge and not args.destination_instance_id:
        print("[remote-memory] ERROR --acknowledge requires --destination-instance-id", file=sys.stderr)
        return 2
    if args.check:
        print(f"[remote-memory] consolidated_db_exists={cfg.consolidated_db.exists()}")
        print("[remote-memory] check_result=ok")
        log_event(log_path, {"mode": "check", "ok": True})
        return 0

    batch_dirs = sorted(path for path in cfg.inbox_root.glob("*/*/*") if path.is_dir())
    if args.batch_dir:
        batch_dirs = [Path(args.batch_dir).resolve()]
    stats = {"batches": 0, "accepted": 0, "duplicates": 0, "quarantined": 0, "failed": 0}
    if args.acknowledge:
        stats.update({"acknowledged": 0, "ack_failed": 0})
    for batch_dir in batch_dirs:
        stats["batches"] += 1
        try:
            result = import_batch(cfg, batch_dir, dry_run=args.dry_run)
        except Exception as exc:
            stats["failed"] += 1
            stats["quarantined"] += 1
            print(f"[remote-memory] ERROR batch={batch_dir} error={exc}", file=sys.stderr)
            if not args.dry_run:
                quarantine_batch(cfg, batch_dir, str(exc))
            continue

        for key in ("accepted", "duplicates"):
            stats[key] += int(result.get(key, 0))
        print(f"[remote-memory] imported batch={batch_dir.name} result={result}")
        if args.acknowledge and not args.dry_run:
            try:
                receipt, receipt_status = write_import_ack(
                    cfg,
                    batch_dir,
                    result,
                    destination_instance_id=args.destination_instance_id,
                )
                stats["acknowledged"] += 1
                print(
                    f"[remote-memory] import_ack={receipt_status} "
                    f"batch={batch_dir.name} path="
                    f"{cfg.import_ack_root / receipt['source_instance_id'] / (receipt['batch_id'] + '.json')}"
                )
                log_event(
                    ack_log_path,
                    {
                        "mode": "import-ack",
                        "ok": True,
                        "source_instance_id": receipt["source_instance_id"],
                        "destination_instance_id": receipt["destination_instance_id"],
                        "batch_id": receipt["batch_id"],
                        "payload_sha256": receipt["payload_sha256"],
                        "manifest_sha256": receipt["manifest_sha256"],
                        "record_count": receipt["record_count"],
                        "accepted": receipt["accepted"],
                        "duplicates": receipt["duplicates"],
                        "receipt_status": receipt_status,
                    },
                )
            except Exception as exc:
                stats["ack_failed"] += 1
                print(
                    f"[remote-memory] ERROR import_ack batch={batch_dir.name} error={exc}",
                    file=sys.stderr,
                )
        elif args.acknowledge:
            print(
                f"[remote-memory] dry-run would acknowledge batch={batch_dir.name} "
                "after a durable import"
            )

    ok = stats["failed"] == 0 and stats.get("ack_failed", 0) == 0
    log_event(log_path, {"mode": "import", "dry_run": args.dry_run, "ok": ok, **stats})
    print(f"[remote-memory] summary={stats}")
    print(f"[remote-memory] success={ok}")
    return 0 if ok else 3


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


def write_import_ack(
    cfg: RemoteConfig,
    batch_dir: Path,
    result: dict[str, int],
    *,
    destination_instance_id: str,
) -> tuple[dict[str, Any], str]:
    """Persist a per-batch receipt only after import_batch committed successfully."""
    manifest_path = batch_dir / "manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = load_json(manifest_path)
    validate_manifest(manifest)
    payload_path = batch_dir / str(manifest["payload_file"])
    payload_bytes = payload_path.read_bytes()
    payload_sha256 = sha256_bytes(payload_bytes)
    if payload_sha256 != manifest["payload_sha256"]:
        raise ValueError("cannot acknowledge a payload whose checksum no longer matches")

    record_count = int(manifest["record_count"])
    imported_count = int(result.get("accepted", 0)) + int(result.get("duplicates", 0))
    if imported_count != record_count:
        raise ValueError(
            f"cannot acknowledge incomplete import: records={record_count} imported={imported_count}"
        )

    source_instance_id = safe_path_component(manifest["instance_id"], "manifest instance_id").upper()
    batch_id = safe_path_component(manifest["batch_id"], "manifest batch_id")
    destination_instance_id = safe_path_component(
        destination_instance_id.strip(), "destination_instance_id"
    ).upper()

    receipt: dict[str, Any] = {
        "format": "hashi-remote-memory-import-ack-v1",
        "status": "imported" if int(result.get("accepted", 0)) else "already_present",
        "acknowledged_at": utc_now(),
        "source_instance_id": source_instance_id,
        "destination_instance_id": destination_instance_id,
        "batch_id": batch_id,
        "manifest_sha256": sha256_bytes(manifest_bytes),
        "payload_sha256": payload_sha256,
        "record_count": record_count,
        "accepted": int(result.get("accepted", 0)),
        "duplicates": int(result.get("duplicates", 0)),
    }
    receipt_path = cfg.import_ack_root / receipt["source_instance_id"] / f"{receipt['batch_id']}.json"
    immutable_fields = (
        "format",
        "source_instance_id",
        "destination_instance_id",
        "batch_id",
        "manifest_sha256",
        "payload_sha256",
        "record_count",
    )
    if receipt_path.exists():
        previous = load_json(receipt_path)
        conflicts = [field for field in immutable_fields if previous.get(field) != receipt.get(field)]
        if conflicts:
            raise ValueError(
                f"existing import acknowledgement conflicts on immutable fields: {conflicts}"
            )
        return previous, "already_acknowledged"

    dump_json(receipt_path, receipt)
    return receipt, "created"


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


@dataclass(frozen=True)
class ValidatedPromotion:
    source: Path
    target: Path
    manifest: dict[str, Any]
    manifest_bytes: bytes
    payload_bytes: bytes


def safe_path_component(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or value in {".", ".."}:
        raise ValueError(f"{field} must be a non-empty safe path component")
    if value != value.strip() or value.endswith((".", " ")):
        raise ValueError(f"{field} is not a safe path component: {value!r}")
    if any(character in value for character in '<>:"/\\|?*\x00'):
        raise ValueError(f"{field} is not a safe path component: {value!r}")
    if any(ord(character) < 32 for character in value) or value.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES:
        raise ValueError(f"{field} is not a safe path component: {value!r}")
    return value


def promotion_source(cfg: RemoteConfig, batch_dir: Path) -> tuple[Path, str, str]:
    candidate = batch_dir if batch_dir.is_absolute() else cfg.root / batch_dir
    if candidate.is_symlink() or candidate.parent.is_symlink():
        raise ValueError(f"batch directory or instance directory is a symlink: {candidate}")
    source = candidate.resolve()
    incoming_root = cfg.incoming_root.resolve()
    try:
        relative = source.relative_to(incoming_root)
    except ValueError as exc:
        raise ValueError(f"batch directory is outside incoming_root: {source}") from exc
    if len(relative.parts) != 2:
        raise ValueError(f"batch directory must be incoming_root/<INSTANCE>/<batch_id>: {source}")
    directory_instance = safe_path_component(relative.parts[0], "directory instance")
    directory_batch = safe_path_component(relative.parts[1], "directory batch_id")
    if not source.is_dir() or source.is_symlink():
        raise ValueError(f"batch directory is missing or is a symlink: {source}")
    return source, directory_instance, directory_batch


def validate_promotion_batch(cfg: RemoteConfig, batch_dir: Path) -> ValidatedPromotion:
    source, directory_instance, directory_batch = promotion_source(cfg, batch_dir)
    manifest_path = source / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError(f"manifest is missing or is a symlink: {manifest_path}")
    manifest_bytes = manifest_path.read_bytes()
    try:
        decoded_manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"manifest is not readable UTF-8 JSON: {manifest_path}") from exc
    if not isinstance(decoded_manifest, dict):
        raise ValueError(f"Expected JSON object: {manifest_path}")
    manifest = decoded_manifest
    validate_manifest(manifest)

    if not isinstance(manifest["schema_version"], int) or isinstance(manifest["schema_version"], bool):
        raise ValueError("manifest schema_version must be an integer")
    instance_id = safe_path_component(manifest["instance_id"], "manifest instance_id")
    batch_id = safe_path_component(manifest["batch_id"], "manifest batch_id")
    if instance_id != directory_instance or batch_id != directory_batch:
        raise ValueError(
            "manifest identity does not match incoming directory "
            f"(directory={directory_instance}/{directory_batch}, manifest={instance_id}/{batch_id})"
        )

    agent_ids = manifest.get("agent_ids")
    if not isinstance(agent_ids, list) or not agent_ids:
        raise ValueError("manifest agent_ids must be a non-empty list")
    safe_agent_ids = [safe_path_component(agent_id, "manifest agent_id") for agent_id in agent_ids]
    if len(set(safe_agent_ids)) != len(safe_agent_ids):
        raise ValueError("manifest agent_ids must not contain duplicates")
    agent_component = safe_agent_ids[0] if len(safe_agent_ids) == 1 else "multi"

    payload_name = safe_path_component(manifest["payload_file"], "manifest payload_file")
    payload_path = source / payload_name
    if payload_path.is_symlink() or not payload_path.is_file():
        raise ValueError(f"payload is missing or is a symlink: {payload_path}")
    expected_entries = {"manifest.json", payload_name}
    actual_entries = {path.name for path in source.iterdir()}
    if actual_entries != expected_entries:
        raise ValueError(
            "incoming batch must contain exactly manifest.json and its declared payload "
            f"(expected={sorted(expected_entries)}, actual={sorted(actual_entries)})"
        )
    payload_bytes = payload_path.read_bytes()
    expected_hash = manifest["payload_sha256"]
    if (
        not isinstance(expected_hash, str)
        or len(expected_hash) != 64
        or any(character not in "0123456789abcdefABCDEF" for character in expected_hash)
    ):
        raise ValueError("manifest payload_sha256 must be a 64-character hexadecimal SHA256")
    actual_hash = sha256_bytes(payload_bytes)
    if actual_hash != expected_hash.lower():
        raise ValueError(f"checksum mismatch expected={expected_hash} actual={actual_hash}")

    record_count = manifest["record_count"]
    if not isinstance(record_count, int) or isinstance(record_count, bool) or record_count < 0:
        raise ValueError("manifest record_count must be a non-negative integer")
    try:
        payload_text = gzip.decompress(payload_bytes).decode("utf-8")
        records = [json.loads(line) for line in payload_text.splitlines() if line.strip()]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("payload must be readable gzip-compressed UTF-8 JSONL") from exc
    if any(not isinstance(record, dict) for record in records):
        raise ValueError("payload JSONL records must be objects")
    if len(records) != record_count:
        raise ValueError(f"record_count mismatch manifest={record_count} actual={len(records)}")
    _, privacy = privacy_filter(records)
    if privacy["private_skipped"]:
        raise ValueError(f"privacy scan rejected {privacy['private_skipped']} private records")

    target = (cfg.inbox_root / instance_id / agent_component / batch_id).resolve()
    try:
        target.relative_to(cfg.inbox_root.resolve())
    except ValueError as exc:
        raise ValueError(f"derived promotion target escapes inbox_root: {target}") from exc
    return ValidatedPromotion(
        source=source,
        target=target,
        manifest=manifest,
        manifest_bytes=manifest_bytes,
        payload_bytes=payload_bytes,
    )


def existing_ancestor(path: Path) -> Path:
    candidate = path
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            raise ValueError(f"no existing ancestor for target: {path}")
        candidate = parent
    return candidate


def promotion_target_is_identical(batch: ValidatedPromotion, target: Path | None = None) -> bool:
    target = target or batch.target
    if not target.is_dir():
        return False
    manifest_path = target / "manifest.json"
    payload_path = target / str(batch.manifest["payload_file"])
    expected_entries = {"manifest.json", str(batch.manifest["payload_file"])}
    if {path.name for path in target.iterdir()} != expected_entries:
        return False
    if (
        manifest_path.is_symlink()
        or payload_path.is_symlink()
        or not manifest_path.is_file()
        or not payload_path.is_file()
    ):
        return False
    return manifest_path.read_bytes() == batch.manifest_bytes and payload_path.read_bytes() == batch.payload_bytes


def cmd_promote_incoming(args: argparse.Namespace) -> int:
    cfg = RemoteConfig.from_args(args)
    print_header("promote-incoming", cfg, args)
    print(f"[remote-memory] incoming_root={cfg.incoming_root}")
    log_path = cfg.logs_root / "remote_memory_promotion.jsonl"
    if args.batch_dir:
        selected = Path(args.batch_dir)
        batch_dirs = [selected if selected.is_absolute() else cfg.root / selected]
    else:
        batch_dirs = sorted(path for path in cfg.incoming_root.glob("*/*") if path.is_dir())
    stats: dict[str, Any] = {
        "batches": len(batch_dirs),
        "promoted": 0,
        "already_present": 0,
        "validated": 0,
        "failed": 0,
    }
    results: list[dict[str, Any]] = []

    for batch_dir in batch_dirs:
        source = batch_dir.resolve()
        target: Path | None = None
        try:
            batch = validate_promotion_batch(cfg, batch_dir)
            source = batch.source
            target = batch.target
            legacy_target = (
                cfg.inbox_root
                / str(batch.manifest["instance_id"])
                / "all"
                / str(batch.manifest["batch_id"])
            ).resolve()
            existing_targets = [
                candidate
                for candidate in dict.fromkeys((target, legacy_target))
                if candidate.exists()
            ]
            if existing_targets:
                conflicting_targets = [
                    candidate
                    for candidate in existing_targets
                    if not promotion_target_is_identical(batch, candidate)
                ]
                if conflicting_targets:
                    raise FileExistsError(
                        "promotion target exists with different or unexpected bytes: "
                        + ", ".join(str(candidate) for candidate in conflicting_targets)
                    )
                target = existing_targets[0]
                status = "already_present"
                stats["already_present"] += 1
            elif args.check:
                status = "valid"
                stats["validated"] += 1
            elif args.dry_run:
                status = "dry_run"
                stats["validated"] += 1
            else:
                target_ancestor = existing_ancestor(target.parent)
                if batch.source.stat().st_dev != target_ancestor.stat().st_dev:
                    raise OSError("incoming and inbox targets are on different volumes; atomic rename is unavailable")
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    raise FileExistsError(f"promotion target appeared before rename: {target}")
                os.rename(batch.source, target)
                status = "promoted"
                stats["promoted"] += 1
            result = {"source": str(source), "target": str(target), "status": status, "ok": True}
            results.append(result)
            print(f"[remote-memory] source={source} target={target} status={status} success=true")
        except Exception as exc:
            stats["failed"] += 1
            result = {
                "source": str(source),
                "target": str(target) if target is not None else None,
                "status": "failed",
                "ok": False,
                "error": str(exc),
            }
            results.append(result)
            print(
                f"[remote-memory] source={source} target={target or 'unresolved'} status=failed error={exc}",
                file=sys.stderr,
            )

    ok = stats["failed"] == 0
    summary = {
        "mode": "check" if args.check else "dry-run" if args.dry_run else "promote-incoming",
        "dry_run": args.dry_run,
        "check": args.check,
        "ok": ok,
        "incoming_root": str(cfg.incoming_root),
        "inbox_root": str(cfg.inbox_root),
        **stats,
        "results": results,
    }
    if not args.check and not args.dry_run:
        log_event(log_path, summary)
    print(f"[remote-memory] summary={json.dumps(summary, ensure_ascii=False, sort_keys=True)}")
    print(f"[remote-memory] success={str(ok).lower()}")
    return 0 if ok else 5


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
    parser.add_argument("--incoming-root")
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

    promote = sub.add_parser("promote-incoming", help="Validate and atomically promote incoming batches")
    promote.add_argument("--batch-dir", help="One batch directory under incoming_root; defaults to all batches")
    promote.add_argument("--dry-run", action="store_true")
    promote.add_argument("--check", action="store_true")
    promote.set_defaults(func=cmd_promote_incoming)

    imp = sub.add_parser("import", help="Import central inbox batches")
    imp.add_argument("--batch-dir")
    imp.add_argument("--dry-run", action="store_true")
    imp.add_argument("--check", action="store_true")
    imp.add_argument("--acknowledge", action="store_true")
    imp.add_argument("--destination-instance-id")
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
