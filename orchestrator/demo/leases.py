from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


class DemoLeaseError(RuntimeError):
    code = "demo_unavailable"
    status = 503


class DemoIdentityRequired(DemoLeaseError):
    code = "demo_identity_required"
    status = 401


class DemoExpired(DemoLeaseError):
    code = "demo_expired"
    status = 410


class DemoBusy(DemoLeaseError):
    code = "demo_busy"
    status = 429


class DemoConflict(DemoLeaseError):
    code = "demo_idempotency_conflict"
    status = 409


class DemoBudgetExhausted(DemoLeaseError):
    code = "demo_budget_exhausted"
    status = 429


@dataclass(frozen=True)
class DemoLease:
    lease_id: str
    owner_id: str
    agent_id: str
    lease_epoch: str
    csrf_token: str
    created_at: float
    expires_at: float
    last_user_activity_at: float
    idle_expires_at: float | None
    state: str
    locale: str

    def expired(self, now: float | None = None) -> bool:
        current = time.time() if now is None else float(now)
        return current >= self.expires_at or (
            self.idle_expires_at is not None and current >= self.idle_expires_at
        )


def _digest(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


class DemoLeaseStore:
    """Small durable anonymous-lease registry. Raw visitor tokens are never stored."""

    def __init__(
        self,
        db_path: Path,
        *,
        max_live_visitors: int,
        absolute_ttl_seconds: int,
        idle_ttl_seconds: int,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.max_live_visitors = int(max_live_visitors)
        self.absolute_ttl_seconds = int(absolute_ttl_seconds)
        self.idle_ttl_seconds = int(idle_ttl_seconds)
        self._lock = threading.RLock()
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=30.0, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._lock, self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS demo_leases (
                    lease_id TEXT PRIMARY KEY,
                    credential_digest TEXT NOT NULL UNIQUE,
                    owner_id TEXT NOT NULL UNIQUE,
                    agent_id TEXT NOT NULL UNIQUE,
                    lease_epoch TEXT NOT NULL UNIQUE,
                    csrf_token TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    last_user_activity_at REAL NOT NULL,
                    idle_expires_at REAL,
                    state TEXT NOT NULL,
                    locale TEXT NOT NULL DEFAULT 'en',
                    cleanup_error_code TEXT
                );
                CREATE INDEX IF NOT EXISTS demo_leases_state_expiry
                    ON demo_leases(state, expires_at, idle_expires_at);

                CREATE TABLE IF NOT EXISTS demo_budget_windows (
                    window_key TEXT PRIMARY KEY,
                    accepted_runs INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS demo_budget_reservations (
                    lease_id TEXT NOT NULL,
                    window_key TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    run_id TEXT,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(lease_id, window_key, idempotency_key),
                    FOREIGN KEY(lease_id) REFERENCES demo_leases(lease_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS demo_session_intents (
                    lease_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(lease_id, idempotency_key),
                    FOREIGN KEY(lease_id) REFERENCES demo_leases(lease_id) ON DELETE CASCADE
                );
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> DemoLease:
        return DemoLease(
            lease_id=str(row["lease_id"]),
            owner_id=str(row["owner_id"]),
            agent_id=str(row["agent_id"]),
            lease_epoch=str(row["lease_epoch"]),
            csrf_token=str(row["csrf_token"]),
            created_at=float(row["created_at"]),
            expires_at=float(row["expires_at"]),
            last_user_activity_at=float(row["last_user_activity_at"]),
            idle_expires_at=(
                None if row["idle_expires_at"] is None else float(row["idle_expires_at"])
            ),
            state=str(row["state"]),
            locale=str(row["locale"] or "en"),
        )

    def allocate(self, *, locale: str = "en", now: float | None = None) -> tuple[DemoLease, str]:
        current = time.time() if now is None else float(now)
        token = secrets.token_urlsafe(32)
        lease_id = "lease_" + uuid.uuid4().hex
        agent_id = "demo_" + uuid.uuid4().hex[:24]
        owner_id = "demo:" + lease_id
        epoch = "le_" + uuid.uuid4().hex
        csrf = secrets.token_urlsafe(32)
        expires_at = current + self.absolute_ttl_seconds
        idle_expires_at = (
            current + self.idle_ttl_seconds if self.idle_ttl_seconds > 0 else None
        )
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            live = connection.execute(
                """
                SELECT COUNT(*) AS value FROM demo_leases
                WHERE state IN ('provisioning','ready','expiring','cleanup_pending')
                """
            ).fetchone()
            if int(live["value"] if live else 0) >= self.max_live_visitors:
                raise DemoBusy("demo visitor capacity reached")
            connection.execute(
                """
                INSERT INTO demo_leases(
                    lease_id, credential_digest, owner_id, agent_id, lease_epoch,
                    csrf_token, created_at, expires_at, last_user_activity_at,
                    idle_expires_at, state, locale
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    lease_id,
                    _digest(token),
                    owner_id,
                    agent_id,
                    epoch,
                    csrf,
                    current,
                    expires_at,
                    current,
                    idle_expires_at,
                    "provisioning",
                    str(locale or "en"),
                ),
            )
            row = connection.execute(
                "SELECT * FROM demo_leases WHERE lease_id=?", (lease_id,)
            ).fetchone()
        return self._row(row), token

    def mark_ready(self, lease_id: str) -> DemoLease:
        with self._lock, self._connection() as connection:
            connection.execute(
                "UPDATE demo_leases SET state='ready', cleanup_error_code=NULL WHERE lease_id=?",
                (str(lease_id),),
            )
            row = connection.execute(
                "SELECT * FROM demo_leases WHERE lease_id=?", (str(lease_id),)
            ).fetchone()
        if row is None:
            raise DemoIdentityRequired("lease not found")
        return self._row(row)

    def mark_cleanup_pending(self, lease_id: str, code: str) -> None:
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                UPDATE demo_leases
                SET state='cleanup_pending', cleanup_error_code=?
                WHERE lease_id=?
                """,
                (str(code)[:80], str(lease_id)),
            )

    def revoke(self, lease_id: str) -> None:
        with self._lock, self._connection() as connection:
            connection.execute(
                "UPDATE demo_leases SET state='expiring' WHERE lease_id=?",
                (str(lease_id),),
            )

    def authenticate(
        self,
        token: str | None,
        *,
        allow_provisioning: bool = False,
        now: float | None = None,
    ) -> DemoLease:
        if not token:
            raise DemoIdentityRequired("visitor credential required")
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM demo_leases WHERE credential_digest=?",
                (_digest(str(token)),),
            ).fetchone()
        if row is None:
            raise DemoIdentityRequired("visitor credential not found")
        lease = self._row(row)
        if lease.expired(now):
            self.revoke(lease.lease_id)
            raise DemoExpired("demo lease expired")
        allowed = {"ready"} | ({"provisioning"} if allow_provisioning else set())
        if lease.state not in allowed:
            if lease.state in {"expiring", "cleanup_pending"}:
                raise DemoExpired("demo lease expired")
            raise DemoIdentityRequired("demo lease unavailable")
        return lease

    def touch(self, lease_id: str, *, now: float | None = None) -> DemoLease:
        current = time.time() if now is None else float(now)
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM demo_leases WHERE lease_id=?", (str(lease_id),)
            ).fetchone()
            if row is None:
                raise DemoIdentityRequired("lease not found")
            lease = self._row(row)
            idle = (
                min(lease.expires_at, current + self.idle_ttl_seconds)
                if self.idle_ttl_seconds > 0
                else None
            )
            connection.execute(
                """
                UPDATE demo_leases
                SET last_user_activity_at=?, idle_expires_at=?
                WHERE lease_id=?
                """,
                (current, idle, str(lease_id)),
            )
            updated = connection.execute(
                "SELECT * FROM demo_leases WHERE lease_id=?", (str(lease_id),)
            ).fetchone()
        return self._row(updated)

    def list_expired_or_pending(self, *, now: float | None = None, limit: int = 100) -> list[DemoLease]:
        current = time.time() if now is None else float(now)
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM demo_leases
                WHERE state='cleanup_pending'
                   OR state='expiring'
                   OR expires_at<=?
                   OR (idle_expires_at IS NOT NULL AND idle_expires_at<=?)
                ORDER BY created_at ASC LIMIT ?
                """,
                (current, current, max(1, min(int(limit), 500))),
            ).fetchall()
        return [self._row(row) for row in rows]

    def delete(self, lease_id: str) -> None:
        with self._lock, self._connection() as connection:
            connection.execute("DELETE FROM demo_leases WHERE lease_id=?", (str(lease_id),))

    @staticmethod
    def _utc_window(now: float | None = None) -> str:
        current = time.time() if now is None else float(now)
        return datetime.fromtimestamp(current, timezone.utc).strftime("%Y-%m-%d")

    def reserve_daily_run(
        self,
        *,
        lease_id: str,
        idempotency_key: str,
        text: str,
        limit: int,
        now: float | None = None,
    ) -> bool:
        """Reserve one conservative UTC daily request unit.

        Returns True when the same reservation already exists. A reservation is
        deliberately not refunded after uncertain provider admission; this keeps
        the fallback budget conservative across process restarts.
        """
        if int(limit) <= 0:
            raise DemoBudgetExhausted("demo daily budget is not configured")
        current = time.time() if now is None else float(now)
        window = self._utc_window(current)
        digest = hashlib.sha256(str(text).encode("utf-8")).hexdigest()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT request_digest FROM demo_budget_reservations
                WHERE lease_id=? AND window_key=? AND idempotency_key=?
                """,
                (str(lease_id), window, str(idempotency_key)),
            ).fetchone()
            if existing is not None:
                if str(existing["request_digest"]) != digest:
                    raise DemoConflict(
                        "run idempotency key reused with different content"
                    )
                return True
            row = connection.execute(
                "SELECT accepted_runs FROM demo_budget_windows WHERE window_key=?",
                (window,),
            ).fetchone()
            used = int(row["accepted_runs"] if row is not None else 0)
            if used >= int(limit):
                raise DemoBudgetExhausted("demo daily request budget exhausted")
            connection.execute(
                """
                INSERT INTO demo_budget_windows(window_key,accepted_runs,updated_at)
                VALUES(?,?,?)
                ON CONFLICT(window_key) DO UPDATE SET
                    accepted_runs=accepted_runs+1,
                    updated_at=excluded.updated_at
                """,
                (window, 1, current),
            )
            connection.execute(
                """
                INSERT INTO demo_budget_reservations(
                    lease_id,window_key,idempotency_key,request_digest,created_at
                ) VALUES(?,?,?,?,?)
                """,
                (str(lease_id), window, str(idempotency_key), digest, current),
            )
        return False

    def commit_daily_run(
        self,
        *,
        lease_id: str,
        idempotency_key: str,
        run_id: str,
        now: float | None = None,
    ) -> None:
        window = self._utc_window(now)
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                UPDATE demo_budget_reservations SET run_id=?
                WHERE lease_id=? AND window_key=? AND idempotency_key=?
                """,
                (str(run_id), str(lease_id), window, str(idempotency_key)),
            )

    def budget_used(self, *, now: float | None = None) -> int:
        window = self._utc_window(now)
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT accepted_runs FROM demo_budget_windows WHERE window_key=?",
                (window,),
            ).fetchone()
        return int(row["accepted_runs"] if row is not None else 0)

    def session_intent(
        self,
        *,
        lease_id: str,
        idempotency_key: str,
        title: str,
        create_session,
    ) -> tuple[str, str, bool]:
        digest = hashlib.sha256(
            json.dumps({"title": str(title)}, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT * FROM demo_session_intents
                WHERE lease_id=? AND idempotency_key=?
                """,
                (str(lease_id), str(idempotency_key)),
            ).fetchone()
            if existing is not None:
                if str(existing["request_digest"]) != digest:
                    raise DemoConflict("session idempotency key reused with different content")
                return str(existing["session_id"]), str(existing["title"]), True

            session = create_session()
            session_id = str(session["session_id"])
            stored_title = str(session["title"])
            connection.execute(
                """
                INSERT INTO demo_session_intents(
                    lease_id,idempotency_key,request_digest,session_id,title,created_at
                ) VALUES(?,?,?,?,?,?)
                """,
                (
                    str(lease_id),
                    str(idempotency_key),
                    digest,
                    session_id,
                    stored_title,
                    time.time(),
                ),
            )
            return session_id, stored_title, False
