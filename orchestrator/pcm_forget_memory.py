"""Selective removal and durable re-import fences for one local PCM store.

The original transcript is retained as audit evidence.  This module owns only
derived BridgeMemoryStore rows and never reads or changes another Agent store.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from orchestrator.timezone_policy import parse_absolute_timestamp


def install_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS pcm_forgotten_ranges (
            start_epoch REAL NOT NULL,
            end_epoch REAL NOT NULL,
            naive_timezone TEXT,
            PRIMARY KEY (start_epoch, end_epoch)
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS pcm_forgotten_origins (
            origin_ref TEXT PRIMARY KEY
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS pcm_forgotten_fingerprints (
            source_epoch REAL NOT NULL,
            content_sha256 TEXT NOT NULL,
            PRIMARY KEY(source_epoch, content_sha256)
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS pcm_forget_export_checkpoints (
            source_path TEXT PRIMARY KEY,
            prefix_size INTEGER NOT NULL,
            prefix_sha256 TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS pcm_forget_export_checkpoint_history (
            source_path TEXT NOT NULL, prefix_size INTEGER NOT NULL,
            prefix_sha256 TEXT NOT NULL, recorded_at TEXT NOT NULL,
            PRIMARY KEY(source_path, prefix_size, prefix_sha256, recorded_at)
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS pcm_forget_quarantine (
            item_type TEXT NOT NULL,
            item_id TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            quarantined_at TEXT NOT NULL,
            PRIMARY KEY (item_type, item_id, payload_hash)
        )"""
    )


def validate_window(start: datetime, end: datetime) -> tuple[float, float]:
    if not isinstance(start, datetime) or not isinstance(end, datetime):
        raise ValueError("forget interval requires datetimes")
    if start.tzinfo is None or start.utcoffset() is None:
        raise ValueError("forget start must include a timezone")
    if end.tzinfo is None or end.utcoffset() is None:
        raise ValueError("forget end must include a timezone")
    lower, upper = start.timestamp(), end.timestamp()
    if lower >= upper:
        raise ValueError("forget start must be before end")
    return lower, upper


def _epoch(value: object, naive_timezone: str | None) -> float | None:
    parsed = parse_absolute_timestamp(value, naive_timezone=naive_timezone)
    return parsed.timestamp() if parsed is not None else None


def _overlaps(
    timestamps: Iterable[object], lower: float, upper: float,
    naive_timezone: str | None,
) -> tuple[bool, bool]:
    points = [_epoch(value, naive_timezone) for value in timestamps]
    values = [float(point) for point in points if point is not None]
    uncertain = not points or any(point is None for point in points)
    # Only a known source time inside the window establishes a match.
    return any(lower <= point <= upper for point in values), uncertain


def blocked(
    connection: sqlite3.Connection,
    *,
    timestamps: Iterable[object] = (),
    origin_ref: str = "",
) -> bool:
    reference = str(origin_ref or "").strip()
    if reference and connection.execute(
        "SELECT 1 FROM pcm_forgotten_origins WHERE origin_ref=?", (reference,)
    ).fetchone():
        return True
    points = tuple(timestamps)
    if not points:
        return False
    for row in connection.execute(
        "SELECT start_epoch, end_epoch, naive_timezone FROM pcm_forgotten_ranges"
    ):
        matches, _ = _overlaps(points, float(row[0]), float(row[1]), row[2])
        if matches:
            return True
    return False


def has_forget_boundary(workspace: Path) -> bool:
    """Return whether a local PCM store contains any durable forget interval."""
    database = Path(workspace) / "bridge_memory.sqlite"
    if not database.exists():
        return False
    try:
        with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as connection:
            tables = {
                str(row[0]) for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'pcm_forgotten_%'"
                )
            }
            if not tables:
                return False
            if not {"pcm_forgotten_ranges", "pcm_forgotten_origins"}.issubset(tables):
                raise RuntimeError("incomplete forget boundary")
            return connection.execute("SELECT 1 FROM pcm_forgotten_ranges LIMIT 1").fetchone() is not None
    except sqlite3.Error as exc:
        raise RuntimeError("cannot verify the forget boundary") from exc


def oldest_forget_start(workspace: Path) -> float | None:
    """Find the earliest erased interval; fail closed for damaged boundaries."""
    database = Path(workspace) / "bridge_memory.sqlite"
    if not database.exists():
        return None
    if database.is_symlink():
        raise RuntimeError("memory boundary is a link")
    try:
        with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as connection:
            tables = {str(row[0]) for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'pcm_forgotten_%'"
            )}
            if not tables:
                return None
            if not {"pcm_forgotten_ranges", "pcm_forgotten_origins"}.issubset(tables):
                raise RuntimeError("incomplete forget boundary")
            value = connection.execute("SELECT MIN(start_epoch) FROM pcm_forgotten_ranges").fetchone()[0]
            return float(value) if value is not None else None
    except sqlite3.Error as exc:
        raise RuntimeError("cannot verify the forget boundary") from exc


def forgotten_raw_fingerprints(workspace: Path) -> set[tuple[float, str]]:
    """Return exact time-and-content fingerprints of forgotten raw records."""
    database = Path(workspace) / "bridge_memory.sqlite"
    if not database.exists():
        return set()
    if database.is_symlink():
        raise RuntimeError("memory boundary is a link")
    try:
        with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pcm_forgotten_fingerprints'"
            ).fetchone()
            if not exists:
                return set()  # A pre-feature store has no provable raw fingerprints.
            return {(float(row[0]), str(row[1])) for row in connection.execute(
                "SELECT source_epoch, content_sha256 FROM pcm_forgotten_fingerprints"
            )}
    except sqlite3.Error as exc:
        raise RuntimeError("cannot verify forgotten raw record fingerprints") from exc


def checkpoint_forget_exports(workspace: Path) -> None:
    """Mark retained log prefixes after cleanup, before Agent work resumes."""
    workspace = Path(workspace)
    database = workspace / "bridge_memory.sqlite"
    if oldest_forget_start(workspace) is None or database.is_symlink():
        raise RuntimeError("forget boundary unavailable for export checkpoint")
    checkpoints = []
    for relative in ("transcript.jsonl", "memory/left_brain_continuity.jsonl"):
        path = workspace / relative
        if path.is_symlink() or path.parent.is_symlink():
            raise RuntimeError("forget export source is a link")
        digest = hashlib.sha256()
        size = 0
        if path.exists():
            before = path.stat()
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
                    size += len(chunk)
            after = path.stat()
            if (before.st_size != size or after.st_size != size
                    or before.st_mtime_ns != after.st_mtime_ns):
                raise RuntimeError("forget export source changed during checkpoint")
        checkpoints.append((relative, size, digest.hexdigest(),
                            datetime.now(timezone.utc).isoformat()))
    with sqlite3.connect(database.resolve().as_uri() + "?mode=rw", uri=True) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS pcm_forget_export_checkpoints (
                source_path TEXT PRIMARY KEY, prefix_size INTEGER NOT NULL,
                prefix_sha256 TEXT NOT NULL, recorded_at TEXT NOT NULL)"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS pcm_forget_export_checkpoint_history (
                source_path TEXT NOT NULL, prefix_size INTEGER NOT NULL,
                prefix_sha256 TEXT NOT NULL, recorded_at TEXT NOT NULL,
                PRIMARY KEY(source_path, prefix_size, prefix_sha256, recorded_at))"""
        )
        connection.execute(
            """INSERT OR IGNORE INTO pcm_forget_export_checkpoint_history
               SELECT source_path, prefix_size, prefix_sha256, recorded_at
               FROM pcm_forget_export_checkpoints"""
        )
        connection.executemany(
            """INSERT OR IGNORE INTO pcm_forget_export_checkpoint_history
               (source_path, prefix_size, prefix_sha256, recorded_at) VALUES (?, ?, ?, ?)""",
            checkpoints,
        )
        connection.executemany(
            """INSERT OR REPLACE INTO pcm_forget_export_checkpoints
               (source_path, prefix_size, prefix_sha256, recorded_at) VALUES (?, ?, ?, ?)""",
            checkpoints,
        )


def inherit_forget_fences(agent_workspace: Path, memory_store: Any) -> None:
    """Apply durable Agent-wide boundaries before a Session store is used."""
    database = Path(agent_workspace) / "bridge_memory.sqlite"
    if not database.exists():
        return
    if database.is_symlink():
        raise RuntimeError("Agent memory boundary is a link")
    try:
        with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as connection:
            tables = {
                str(row[0]) for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'pcm_forgotten_%'"
                )
            }
            if not tables:
                return
            if not {"pcm_forgotten_ranges", "pcm_forgotten_origins"}.issubset(tables):
                raise RuntimeError("incomplete Agent forget boundary")
            ranges = [tuple(row) for row in connection.execute(
                "SELECT start_epoch, end_epoch, naive_timezone FROM pcm_forgotten_ranges"
            )]
            references = {str(row[0]) for row in connection.execute(
                "SELECT origin_ref FROM pcm_forgotten_origins"
            )}
        if not ranges:
            return
        with memory_store._connect() as connection:
            current_ranges = {tuple(row) for row in connection.execute(
                "SELECT start_epoch, end_epoch, naive_timezone FROM pcm_forgotten_ranges"
            )}
            current_refs = {str(row[0]) for row in connection.execute(
                "SELECT origin_ref FROM pcm_forgotten_origins"
            )}
        if set(ranges).issubset(current_ranges) and references.issubset(current_refs):
            return
        for lower, upper, naive_timezone in ranges:
            memory_store.forget_window(
                start=datetime.fromtimestamp(float(lower), timezone.utc),
                end=datetime.fromtimestamp(float(upper), timezone.utc),
                naive_timezone=naive_timezone, origin_refs=tuple(sorted(references)),
                quarantine_late=(lower, upper, naive_timezone) not in current_ranges,
            )
    except sqlite3.Error as exc:
        raise RuntimeError("cannot inherit the Agent forget boundary") from exc


def filter_transcript_rounds(
    workspace: Path, rounds: list[list[dict[str, Any]]],
) -> list[list[dict[str, Any]]]:
    """Keep the audit file intact while excluding forgotten rounds from handoff."""
    database = Path(workspace) / "bridge_memory.sqlite"
    if not database.exists():
        return rounds
    # Read-only: projecting history must not initialise or migrate a database.
    try:
        with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as connection:
            tables = {
                str(row[0]) for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'pcm_forgotten_%'"
                )
            }
            if not tables:
                return rounds  # Legacy store which has never had a forget operation.
            if not {"pcm_forgotten_ranges", "pcm_forgotten_origins"}.issubset(tables):
                raise RuntimeError("incomplete forget boundary")
            filtered = [
                [entry for entry in entries
                 if not blocked(connection, timestamps=(entry.get("ts"),))]
                for entries in rounds
            ]
            return [entries for entries in filtered if entries]
    except sqlite3.Error as exc:
        raise RuntimeError("cannot verify the forget boundary for handoff") from exc


def _quarantine(
    connection: sqlite3.Connection, kind: str, row: sqlite3.Row, *, identity: object | None = None,
) -> None:
    from orchestrator.pcm_forget_audit import protect

    value = dict(row)
    item_id = str(row["id"] if identity is None else identity)
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    if connection.execute(
        "SELECT 1 FROM pcm_forget_quarantine WHERE item_type=? AND item_id=? AND payload_hash=?",
        (kind, item_id, digest),
    ).fetchone():
        return
    payload = json.dumps(protect(kind, item_id, value), ensure_ascii=False, separators=(",", ":"))
    connection.execute(
        """INSERT INTO pcm_forget_quarantine
           (item_type, item_id, payload_json, payload_hash, quarantined_at)
           VALUES (?, ?, ?, ?, ?)""",
        (kind, item_id, payload, digest, datetime.now(timezone.utc).isoformat()),
    )


def _remove_ids(
    connection: sqlite3.Connection, table: str, id_column: str,
    ids: set[int],
) -> None:
    if ids:
        connection.executemany(
            f"DELETE FROM {table} WHERE {id_column} = ?",
            ((value,) for value in sorted(ids)),
        )


def forget_window(
    connection: sqlite3.Connection,
    *,
    start: datetime,
    end: datetime,
    naive_timezone: str | None = None,
    origin_refs: Iterable[str] = (),
    quarantine_late: bool = True,
) -> dict[str, int]:
    """Fence and remove one interval in the caller's SQLite transaction.

    Known-timestamped turns and directly attributed memories are removed.
    Unknown or mixed summary content is retained and may still contain
    information from the window. Audit transcripts remain intact.
    """
    lower, upper = validate_window(start, end)
    if naive_timezone:
        from orchestrator.timezone_policy import canonical_timezone_name
        naive_timezone = canonical_timezone_name(naive_timezone)

    connection.execute(
        """INSERT INTO pcm_forgotten_ranges(start_epoch, end_epoch, naive_timezone)
           VALUES (?, ?, ?)
           ON CONFLICT(start_epoch, end_epoch) DO UPDATE SET
             naive_timezone=excluded.naive_timezone""",
        (lower, upper, naive_timezone),
    )
    refs = {str(ref).strip() for ref in origin_refs if str(ref).strip()}
    exchange_ids: set[int] = set()
    turn_ids: set[int] = set()
    memory_ids: set[int] = set()
    quarantined = 0

    def remember_raw_fingerprint(instant: float | None, text: object) -> None:
        content = str(text or "").strip()
        if instant is None or not content:
            return
        connection.execute(
            "INSERT OR IGNORE INTO pcm_forgotten_fingerprints(source_epoch, content_sha256) VALUES (?, ?)",
            (instant, hashlib.sha256(content.encode("utf-8")).hexdigest()),
        )

    for row in connection.execute("SELECT * FROM conversation_exchanges"):
        user_time = _epoch(row["user_ts"], naive_timezone)
        assistant_time = _epoch(row["assistant_ts"], naive_timezone)
        selected_user = user_time is not None and lower <= user_time <= upper
        selected_assistant = assistant_time is not None and lower <= assistant_time <= upper
        if not (selected_user or selected_assistant):
            continue
        _quarantine(connection, "exchange", row)
        if selected_user:
            remember_raw_fingerprint(user_time, row["user_text"])
        if selected_assistant:
            remember_raw_fingerprint(assistant_time, row["assistant_text"])
        if selected_user and row["user_turn_id"]:
            turn_ids.add(int(row["user_turn_id"]))
        if selected_assistant and row["assistant_turn_id"]:
            turn_ids.add(int(row["assistant_turn_id"]))
        if selected_user and selected_assistant:
            exchange_ids.add(int(row["id"]))
            if row["origin_ref"]:
                refs.add(str(row["origin_ref"]))
        else:
            # Preserve the other message instead of removing a boundary-crossing pair.
            for side, selected in (("user", selected_user), ("assistant", selected_assistant)):
                if selected:
                    connection.execute(
                        f"UPDATE conversation_exchanges SET {side}_text='', {side}_turn_id=NULL WHERE id=?",
                        (int(row["id"]),),
                    )

    for row in connection.execute("SELECT * FROM turns"):
        selected, uncertain = _overlaps((row["ts"],), lower, upper, naive_timezone)
        if str(row["role"]) == "recovery" and int(row["id"]) not in turn_ids:
            # A recovery capsule is a summary, not an event with known source time.
            continue
        if selected or int(row["id"]) in turn_ids:
            turn_ids.add(int(row["id"]))
            remember_raw_fingerprint(_epoch(row["ts"], naive_timezone), row["text"])
            # Recovery capsules use a stable source identity across replay.
            # Remember it before removing the row, even if the replay occurs
            # long after the original forgotten interval.
            source = str(row["source"] or "").strip()
            if source.startswith("wip-recovery:"):
                refs.add(source)
            _quarantine(connection, "turn", row)
            if uncertain:
                quarantined += 1

    for row in connection.execute("SELECT * FROM memories"):
        reference = str(row["origin_ref"] or "").strip()
        # A generated memory's creation time does not identify the time of its
        # source facts. Only a known source reference permits targeted removal.
        if reference and reference in refs:
            memory_ids.add(int(row["id"]))
            if row["origin_ref"]:
                refs.add(str(row["origin_ref"]))
            _quarantine(connection, "memory", row)

    tables = {str(row[0]) for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    if "emotional_annotations" in tables:
        for row in connection.execute("SELECT * FROM emotional_annotations").fetchall():
            if str(row["bridge_row_type"]) == "bootstrap":
                continue
            selected, uncertain = _overlaps(
                (row["event_ts"], row["created_at"]), lower, upper, naive_timezone,
            )
            source_removed = (
                str(row["bridge_row_type"]) == "turn" and int(row["bridge_row_id"]) in turn_ids
            ) or (
                str(row["bridge_row_type"]) == "memory" and int(row["bridge_row_id"]) in memory_ids
            )
            if selected or source_removed:
                _quarantine(connection, "anatta_emotion", row)
                connection.execute("DELETE FROM emotional_annotations WHERE id=?", (row["id"],))
                quarantined += int(uncertain)
    if "relationship_events" in tables:
        for row in connection.execute("SELECT * FROM relationship_events").fetchall():
            selected, uncertain = _overlaps((row["event_ts"],), lower, upper, naive_timezone)
            if selected:
                _quarantine(connection, "anatta_relationship", row)
                connection.execute("DELETE FROM relationship_events WHERE id=?", (row["id"],))
                quarantined += int(uncertain)
    # A derived relationship cache can mix facts from multiple periods and
    # has no per-item source provenance. Leave it unchanged.

    connection.executemany(
        "INSERT OR IGNORE INTO pcm_forgotten_origins(origin_ref) VALUES (?)",
        ((ref,) for ref in sorted(refs)),
    )
    _remove_ids(connection, "memory_fts", "memory_id", memory_ids)
    # An existing sqlite-vec table must be writable; failure rolls back the
    # whole transaction instead of leaving a searchable stale embedding.
    vec_tables = {
        str(row[0]) for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('memory_vec','turns_vec')"
        )
    }
    if "memory_vec" in vec_tables:
        _remove_ids(connection, "memory_vec", "memory_id", memory_ids)
    if "turns_vec" in vec_tables:
        _remove_ids(connection, "turns_vec", "turn_id", turn_ids)
    _remove_ids(connection, "memories", "id", memory_ids)
    _remove_ids(connection, "turns", "id", turn_ids)
    _remove_ids(connection, "conversation_exchanges", "id", exchange_ids)
    return {
        "exchanges": len(exchange_ids),
        "turns": len(turn_ids),
        "memories": len(memory_ids),
        "quarantined": quarantined,
    }
