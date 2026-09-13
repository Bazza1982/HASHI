"""Short-lived durable outbox for exact Exchange retry with original IDs."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from remote.exchange_protocol import canonical_json, parse_timestamp


TERMINAL_STATES = frozenset({"delivered", "rejected", "expired"})
RETRYABLE_STATES = frozenset(
    {"prepared", "accepted", "delivery_unknown", "sending"}
)
CORRELATION_RETENTION_SECONDS = 30 * 24 * 60 * 60


class ExchangeOutboxConflict(ValueError):
    """One message ID was reused with different immutable content."""


@dataclass(frozen=True, slots=True)
class OutboxRecord:
    message_id: str
    from_agent: str
    conversation_id: str
    to_address: str
    expires_at: float
    frame: dict[str, Any]
    frame_digest: str
    state: str
    code: str | None
    created_at: float
    updated_at: float
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class OutboundCorrelation:
    message_id: str
    from_agent: str
    conversation_id: str
    recipient: dict[str, str]
    message_type: str
    frame_digest: str
    delivery_state: str
    accepted_at: float | None
    created_at: float
    retained_until: float


class ExchangeOutbox:
    """Persist only the bounded send frame needed for exact retry.

    Rows expire with the ten-minute Exchange delivery deadline.  This is a
    local transport spool, not a second transcript or a cloud mailbox.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS exchange_outbox (
                    message_id TEXT PRIMARY KEY,
                    from_agent TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    to_address TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    frame_json TEXT NOT NULL,
                    frame_digest TEXT NOT NULL,
                    state TEXT NOT NULL,
                    code TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS exchange_correlations (
                    message_id TEXT PRIMARY KEY,
                    from_agent TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    recipient_json TEXT NOT NULL,
                    message_type TEXT NOT NULL,
                    frame_digest TEXT NOT NULL,
                    delivery_state TEXT NOT NULL,
                    accepted_at REAL,
                    created_at REAL NOT NULL,
                    retained_until REAL NOT NULL
                )
                """
            )
            columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(exchange_correlations)"
                ).fetchall()
            }
            if "frame_digest" not in columns:
                connection.execute(
                    "ALTER TABLE exchange_correlations "
                    "ADD COLUMN frame_digest TEXT"
                )
                connection.execute(
                    """
                    UPDATE exchange_correlations
                    SET frame_digest = (
                        SELECT exchange_outbox.frame_digest
                        FROM exchange_outbox
                        WHERE exchange_outbox.message_id =
                              exchange_correlations.message_id
                    )
                    WHERE frame_digest IS NULL
                    """
                )
            if "delivery_state" not in columns:
                connection.execute(
                    "ALTER TABLE exchange_correlations "
                    "ADD COLUMN delivery_state TEXT NOT NULL "
                    "DEFAULT 'unknown'"
                )
                connection.execute(
                    """
                    UPDATE exchange_correlations
                    SET delivery_state = COALESCE((
                        SELECT exchange_outbox.state
                        FROM exchange_outbox
                        WHERE exchange_outbox.message_id =
                              exchange_correlations.message_id
                    ), 'unknown')
                    """
                )
            if "accepted_at" not in columns:
                connection.execute(
                    "ALTER TABLE exchange_correlations "
                    "ADD COLUMN accepted_at REAL"
                )
                connection.execute(
                    """
                    UPDATE exchange_correlations
                    SET accepted_at = (
                        SELECT exchange_outbox.updated_at
                        FROM exchange_outbox
                        WHERE exchange_outbox.message_id =
                              exchange_correlations.message_id
                          AND exchange_outbox.state IN ('accepted','delivered')
                    )
                    WHERE accepted_at IS NULL
                    """
                )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS exchange_correlations_retention
                ON exchange_correlations(retained_until)
                """
            )
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def _connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _record(row: sqlite3.Row, *, replayed: bool = False) -> OutboxRecord:
        return OutboxRecord(
            message_id=str(row["message_id"]),
            from_agent=str(row["from_agent"]),
            conversation_id=str(row["conversation_id"]),
            to_address=str(row["to_address"]),
            expires_at=float(row["expires_at"]),
            frame=json.loads(str(row["frame_json"])),
            frame_digest=str(row["frame_digest"]),
            state=str(row["state"]),
            code=str(row["code"]) if row["code"] is not None else None,
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            replayed=replayed,
        )

    @staticmethod
    def _correlation_record(row: sqlite3.Row) -> OutboundCorrelation:
        recipient = json.loads(str(row["recipient_json"]))
        if not isinstance(recipient, dict):
            raise ValueError("invalid Exchange correlation recipient")
        return OutboundCorrelation(
            message_id=str(row["message_id"]),
            from_agent=str(row["from_agent"]),
            conversation_id=str(row["conversation_id"]),
            recipient={str(key): str(value) for key, value in recipient.items()},
            message_type=str(row["message_type"]),
            frame_digest=str(row["frame_digest"] or ""),
            delivery_state=str(row["delivery_state"] or "unknown"),
            accepted_at=(
                float(row["accepted_at"])
                if row["accepted_at"] is not None
                else None
            ),
            created_at=float(row["created_at"]),
            retained_until=float(row["retained_until"]),
        )

    @staticmethod
    def _store_correlation(
        connection: sqlite3.Connection,
        *,
        body: Mapping[str, Any],
        frame_digest: str,
        now: float,
    ) -> None:
        recipient = body.get("to")
        if not isinstance(recipient, Mapping):
            raise ValueError("incomplete Exchange outbox recipient")
        recipient_json = canonical_json(dict(recipient)).decode("ascii")
        message_id = str(body["message_id"])
        connection.execute(
            """
            DELETE FROM exchange_correlations
            WHERE message_id=? AND retained_until<=?
            """,
            (message_id, now),
        )
        existing = connection.execute(
            "SELECT * FROM exchange_correlations WHERE message_id=?",
            (message_id,),
        ).fetchone()
        if existing is not None:
            if (
                str(existing["from_agent"]) != str(body["from_agent"])
                or str(existing["conversation_id"])
                != str(body["conversation_id"])
                or str(existing["recipient_json"]) != recipient_json
                or str(existing["message_type"]) != str(body["message_type"])
                or not str(existing["frame_digest"] or "")
                or str(existing["frame_digest"]) != frame_digest
            ):
                raise ExchangeOutboxConflict(
                    "Exchange message ID is already bound to another payload"
                )
            return
        connection.execute(
            """
            INSERT INTO exchange_correlations(
                message_id, from_agent, conversation_id, recipient_json,
                message_type, frame_digest, delivery_state, accepted_at,
                created_at, retained_until
            ) VALUES (?, ?, ?, ?, ?, ?, 'prepared', NULL, ?, ?)
            """,
            (
                message_id,
                str(body["from_agent"]),
                str(body["conversation_id"]),
                recipient_json,
                str(body["message_type"]),
                frame_digest,
                now,
                now + CORRELATION_RETENTION_SECONDS,
            ),
        )

    def put(self, frame: Mapping[str, Any]) -> OutboxRecord:
        body = dict(frame)
        message_id = str(body.get("message_id") or "")
        from_agent = str(body.get("from_agent") or "")
        conversation_id = str(body.get("conversation_id") or "")
        to = body.get("to")
        to_address = (
            str(to.get("address") or "") if isinstance(to, Mapping) else ""
        )
        message_type = str(body.get("message_type") or "")
        expires_at = parse_timestamp(body.get("expires_at"))
        if not all(
            (message_id, from_agent, conversation_id, to_address, message_type)
        ):
            raise ValueError("incomplete Exchange outbox frame")
        encoded = canonical_json(body)
        digest = hashlib.sha256(encoded).hexdigest()
        rendered = encoded.decode("ascii")
        now = time.time()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM exchange_outbox WHERE message_id=?",
                (message_id,),
            ).fetchone()
            if row is not None:
                if str(row["frame_digest"]) != digest:
                    raise ExchangeOutboxConflict(
                        "Exchange message ID is already bound to another payload"
                    )
                self._store_correlation(
                    connection,
                    body=body,
                    frame_digest=digest,
                    now=now,
                )
                return self._record(row, replayed=True)
            connection.execute(
                """
                INSERT INTO exchange_outbox(
                    message_id, from_agent, conversation_id, to_address,
                    expires_at, frame_json, frame_digest, state,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'prepared', ?, ?)
                """,
                (
                    message_id,
                    from_agent,
                    conversation_id,
                    to_address,
                    expires_at,
                    rendered,
                    digest,
                    now,
                    now,
                ),
            )
            self._store_correlation(
                connection,
                body=body,
                frame_digest=digest,
                now=now,
            )
            row = connection.execute(
                "SELECT * FROM exchange_outbox WHERE message_id=?",
                (message_id,),
            ).fetchone()
        return self._record(row)

    def set_state(
        self,
        message_id: str,
        state: str,
        *,
        code: str | None = None,
    ) -> OutboxRecord | None:
        normalized = str(state or "").strip().lower()
        if normalized not in RETRYABLE_STATES | TERMINAL_STATES | {"unknown"}:
            raise ValueError("invalid Exchange outbox state")
        now = time.time()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM exchange_outbox WHERE message_id=?",
                (str(message_id),),
            ).fetchone()
            if (
                current is not None
                and str(current["state"]) in TERMINAL_STATES
            ):
                return self._record(current)
            connection.execute(
                """
                UPDATE exchange_outbox
                SET state=?, code=?, updated_at=?
                WHERE message_id=?
                """,
                (normalized, str(code) if code else None, now, str(message_id)),
            )
            connection.execute(
                """
                UPDATE exchange_correlations
                SET delivery_state=?,
                    accepted_at=(
                        CASE
                            WHEN ? IN ('accepted','delivered')
                            THEN COALESCE(accepted_at, ?)
                            ELSE accepted_at
                        END
                    )
                WHERE message_id=?
                """,
                (normalized, normalized, now, str(message_id)),
            )
            row = connection.execute(
                "SELECT * FROM exchange_outbox WHERE message_id=?",
                (str(message_id),),
            ).fetchone()
        return self._record(row) if row is not None else None

    def get(self, message_id: str) -> OutboxRecord | None:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM exchange_outbox WHERE message_id=?",
                (str(message_id),),
            ).fetchone()
        return self._record(row) if row is not None else None

    def get_correlation(
        self,
        message_id: str,
        *,
        now: float | None = None,
    ) -> OutboundCorrelation | None:
        timestamp = time.time() if now is None else float(now)
        with self._lock, self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM exchange_correlations
                WHERE message_id=? AND retained_until>?
                """,
                (str(message_id), timestamp),
            ).fetchone()
        return self._correlation_record(row) if row is not None else None

    def pending(self, *, now: float | None = None) -> list[OutboxRecord]:
        timestamp = time.time() if now is None else float(now)
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE exchange_outbox
                SET state='expired', code='MESSAGE_EXPIRED', updated_at=?
                WHERE expires_at <= ? AND state NOT IN ('delivered','rejected','expired')
                """,
                (timestamp, timestamp),
            )
            rows = connection.execute(
                """
                SELECT * FROM exchange_outbox
                WHERE expires_at > ?
                  AND state IN ('prepared','accepted','delivery_unknown','sending','unknown')
                ORDER BY created_at, message_id
                """,
                (timestamp,),
            ).fetchall()
        return [self._record(row) for row in rows]

    def purge(self, *, now: float | None = None, grace_seconds: float = 3600) -> int:
        timestamp = time.time() if now is None else float(now)
        cutoff = timestamp - max(0.0, float(grace_seconds))
        with self._lock, self._connection() as connection:
            connection.execute(
                "DELETE FROM exchange_correlations WHERE retained_until<=?",
                (timestamp,),
            )
            cursor = connection.execute(
                "DELETE FROM exchange_outbox WHERE expires_at < ?",
                (cutoff,),
            )
            return int(cursor.rowcount)


__all__ = [
    "CORRELATION_RETENTION_SECONDS",
    "ExchangeOutbox",
    "ExchangeOutboxConflict",
    "OutboundCorrelation",
    "OutboxRecord",
    "RETRYABLE_STATES",
    "TERMINAL_STATES",
]
