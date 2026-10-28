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
    if not points or any(point is None for point in points):
        # An unverifiable source is quarantined rather than silently reused.
        return True, True
    values = [float(point) for point in points if point is not None]
    return min(values) <= upper and max(values) >= lower, False


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
            return [
                entries for entries in rounds
                if not blocked(connection, timestamps=(entry.get("ts") for entry in entries))
            ]
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

    All selected exchanges are removed whole.  Untimestamped material is moved
    to a non-retrievable quarantine and reported; the audit transcript remains
    intact.  Source identities prevent a later promotion from recreating rows.
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
    safe_refs: set[str] = set()
    turn_ids: set[int] = set()
    memory_ids: set[int] = set()
    quarantined = 0

    for row in connection.execute("SELECT * FROM conversation_exchanges"):
        selected, uncertain = _overlaps(
            (row["user_ts"], row["assistant_ts"]), lower, upper, naive_timezone
        )
        if not selected:
            if row["origin_ref"]:
                safe_refs.add(str(row["origin_ref"]))
            continue
        exchange_ids.add(int(row["id"]))
        if row["origin_ref"]:
            refs.add(str(row["origin_ref"]))
        for field in ("user_turn_id", "assistant_turn_id"):
            if row[field]:
                turn_ids.add(int(row[field]))
        _quarantine(connection, "exchange", row)
        if uncertain:
            quarantined += 1

    for row in connection.execute("SELECT * FROM turns"):
        selected, uncertain = _overlaps((row["ts"],), lower, upper, naive_timezone)
        if selected or int(row["id"]) in turn_ids:
            turn_ids.add(int(row["id"]))
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
        selected, uncertain = _overlaps((row["ts"],), lower, upper, naive_timezone)
        reference = str(row["origin_ref"] or "").strip()
        instant = _epoch(row["ts"], naive_timezone)
        # A later untethered summary may have been generated from a forgotten
        # exchange. Keep its audit copy but do not return it as Agent memory.
        if quarantine_late and instant is not None and instant > upper and reference not in safe_refs:
            selected, uncertain = True, True
        if selected or (reference and reference in refs):
            memory_ids.add(int(row["id"]))
            if row["origin_ref"]:
                refs.add(str(row["origin_ref"]))
            _quarantine(connection, "memory", row)
            if uncertain:
                quarantined += 1

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
    if "derived_relationship_cache" in tables:
        for row in connection.execute("SELECT * FROM derived_relationship_cache").fetchall():
            _quarantine(connection, "anatta_relationship_cache", row, identity=row["relationship_key"])
            quarantined += 1
        connection.execute("DELETE FROM derived_relationship_cache")

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
