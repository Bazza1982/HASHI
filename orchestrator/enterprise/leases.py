from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from orchestrator.enterprise.store import EnterpriseStore


@dataclass(frozen=True)
class EnterpriseLease:
    org_id: str
    name: str
    holder_id: str
    expires_at: str
    acquired_at: str
    renewed_at: str
    metadata: dict[str, Any]

    @property
    def is_expired(self) -> bool:
        return _parse_ts(self.expires_at) <= _utc_now()


@dataclass(frozen=True)
class EnterpriseLeaseAttempt:
    acquired: bool
    lease: EnterpriseLease | None
    current_holder_id: str | None = None


class EnterpriseLeaseStore:
    def __init__(self, store: EnterpriseStore, *, org_id: str):
        self.store = store
        self.org_id = org_id
        self.store.init_schema()

    @classmethod
    def from_path(cls, db_path: Path | str, *, org_id: str) -> "EnterpriseLeaseStore":
        return cls(EnterpriseStore(db_path), org_id=org_id)

    @classmethod
    def from_url(cls, database_url: str | Path | None, *, org_id: str):
        value = str(database_url or "").strip()
        if not value:
            raise ValueError("enterprise database URL is required")
        parsed = urlparse(value)
        if parsed.scheme in {"postgres", "postgresql"}:
            return PostgresEnterpriseLeaseStore(value, org_id=org_id)
        if parsed.scheme == "sqlite":
            if not value.startswith("sqlite:///"):
                raise ValueError(f"unsupported SQLite enterprise database URL: {value}")
            return cls.from_path(Path(value[len("sqlite:///"):]).expanduser(), org_id=org_id)
        if parsed.scheme:
            raise ValueError(f"unsupported enterprise database URL: {value}")
        return cls.from_path(Path(value).expanduser(), org_id=org_id)

    def acquire(
        self,
        name: str,
        *,
        holder_id: str,
        ttl_seconds: int | float = 60,
        metadata: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> EnterpriseLeaseAttempt:
        lease_name = _require_text(name, "lease name")
        holder = _require_text(holder_id, "holder id")
        issued_at = _normalize_now(now)
        expires_at = issued_at + timedelta(seconds=max(1.0, float(ttl_seconds)))
        metadata_json = json.dumps(metadata or {}, sort_keys=True)
        with self.store.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT * FROM enterprise_leases WHERE org_id = ? AND name = ?",
                (self.org_id, lease_name),
            ).fetchone()
            if row is not None:
                current = _row_to_lease(row)
                current_expired = _parse_ts(current.expires_at) <= issued_at
                if not current_expired and current.holder_id != holder:
                    return EnterpriseLeaseAttempt(
                        acquired=False,
                        lease=current,
                        current_holder_id=current.holder_id,
                    )
                acquired_at = current.acquired_at if current.holder_id == holder and not current_expired else _format_ts(issued_at)
                con.execute(
                    """
                    UPDATE enterprise_leases
                    SET holder_id = ?, expires_at = ?, acquired_at = ?, renewed_at = ?, metadata_json = ?
                    WHERE org_id = ? AND name = ?
                    """,
                    (
                        holder,
                        _format_ts(expires_at),
                        acquired_at,
                        _format_ts(issued_at),
                        metadata_json,
                        self.org_id,
                        lease_name,
                    ),
                )
                lease = EnterpriseLease(
                    org_id=self.org_id,
                    name=lease_name,
                    holder_id=holder,
                    expires_at=_format_ts(expires_at),
                    acquired_at=acquired_at,
                    renewed_at=_format_ts(issued_at),
                    metadata=metadata or {},
                )
            else:
                con.execute(
                    """
                    INSERT INTO enterprise_leases(
                        org_id, name, holder_id, expires_at, acquired_at, renewed_at, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self.org_id,
                        lease_name,
                        holder,
                        _format_ts(expires_at),
                        _format_ts(issued_at),
                        _format_ts(issued_at),
                        metadata_json,
                    ),
                )
                lease = EnterpriseLease(
                    org_id=self.org_id,
                    name=lease_name,
                    holder_id=holder,
                    expires_at=_format_ts(expires_at),
                    acquired_at=_format_ts(issued_at),
                    renewed_at=_format_ts(issued_at),
                    metadata=metadata or {},
                )
            return EnterpriseLeaseAttempt(acquired=True, lease=lease, current_holder_id=holder)

    def renew(
        self,
        name: str,
        *,
        holder_id: str,
        ttl_seconds: int | float = 60,
        now: datetime | None = None,
    ) -> EnterpriseLeaseAttempt:
        lease_name = _require_text(name, "lease name")
        holder = _require_text(holder_id, "holder id")
        issued_at = _normalize_now(now)
        expires_at = issued_at + timedelta(seconds=max(1.0, float(ttl_seconds)))
        with self.store.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT * FROM enterprise_leases WHERE org_id = ? AND name = ?",
                (self.org_id, lease_name),
            ).fetchone()
            if row is None:
                return EnterpriseLeaseAttempt(acquired=False, lease=None, current_holder_id=None)
            current = _row_to_lease(row)
            if current.holder_id != holder or _parse_ts(current.expires_at) <= issued_at:
                return EnterpriseLeaseAttempt(
                    acquired=False,
                    lease=current,
                    current_holder_id=current.holder_id,
                )
            con.execute(
                """
                UPDATE enterprise_leases
                SET expires_at = ?, renewed_at = ?
                WHERE org_id = ? AND name = ? AND holder_id = ?
                """,
                (_format_ts(expires_at), _format_ts(issued_at), self.org_id, lease_name, holder),
            )
            lease = EnterpriseLease(
                org_id=current.org_id,
                name=current.name,
                holder_id=current.holder_id,
                expires_at=_format_ts(expires_at),
                acquired_at=current.acquired_at,
                renewed_at=_format_ts(issued_at),
                metadata=current.metadata,
            )
            return EnterpriseLeaseAttempt(acquired=True, lease=lease, current_holder_id=holder)

    def release(self, name: str, *, holder_id: str) -> bool:
        lease_name = _require_text(name, "lease name")
        holder = _require_text(holder_id, "holder id")
        with self.store.connect() as con:
            con.execute(
                "DELETE FROM enterprise_leases WHERE org_id = ? AND name = ? AND holder_id = ?",
                (self.org_id, lease_name, holder),
            )
            return con.total_changes > 0

    def get(self, name: str) -> EnterpriseLease | None:
        lease_name = _require_text(name, "lease name")
        with self.store.connect() as con:
            row = con.execute(
                "SELECT * FROM enterprise_leases WHERE org_id = ? AND name = ?",
                (self.org_id, lease_name),
            ).fetchone()
        return _row_to_lease(row) if row is not None else None


def _row_to_lease(row) -> EnterpriseLease:
    return EnterpriseLease(
        org_id=str(row["org_id"]),
        name=str(row["name"]),
        holder_id=str(row["holder_id"]),
        expires_at=str(row["expires_at"]),
        acquired_at=str(row["acquired_at"]),
        renewed_at=str(row["renewed_at"]),
        metadata=json.loads(row["metadata_json"] or "{}"),
    )


class PostgresEnterpriseLeaseStore:
    def __init__(self, dsn: str, *, org_id: str, connect=None, init_schema: bool = True):
        self.dsn = _require_text(dsn, "PostgreSQL DSN")
        self.org_id = org_id
        if connect is None:
            try:
                import psycopg
            except ImportError as exc:
                raise RuntimeError(
                    "PostgreSQL enterprise leases require the optional 'psycopg' package"
                ) from exc
            connect = psycopg.connect
        self._connect = connect
        if init_schema:
            self.init_schema()

    def connect(self):
        return self._connect(self.dsn)

    def init_schema(self) -> None:
        with self.connect() as con:
            with con.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS enterprise_leases (
                        org_id TEXT NOT NULL,
                        name TEXT NOT NULL,
                        holder_id TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        acquired_at TEXT NOT NULL,
                        renewed_at TEXT NOT NULL,
                        metadata_json TEXT NOT NULL,
                        PRIMARY KEY(org_id, name)
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_enterprise_leases_org_expires
                    ON enterprise_leases(org_id, expires_at)
                    """
                )

    def acquire(
        self,
        name: str,
        *,
        holder_id: str,
        ttl_seconds: int | float = 60,
        metadata: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> EnterpriseLeaseAttempt:
        lease_name = _require_text(name, "lease name")
        holder = _require_text(holder_id, "holder id")
        issued_at = _normalize_now(now)
        expires_at = issued_at + timedelta(seconds=max(1.0, float(ttl_seconds)))
        metadata_json = json.dumps(metadata or {}, sort_keys=True)
        with self.connect() as con:
            with con.cursor() as cur:
                _postgres_lock_lease_key(cur, self.org_id, lease_name)
                row = _postgres_fetch_lease(cur, self.org_id, lease_name, for_update=True)
                if row is not None:
                    current = _postgres_row_to_lease(row)
                    current_expired = _parse_ts(current.expires_at) <= issued_at
                    if not current_expired and current.holder_id != holder:
                        return EnterpriseLeaseAttempt(
                            acquired=False,
                            lease=current,
                            current_holder_id=current.holder_id,
                        )
                    acquired_at = (
                        current.acquired_at
                        if current.holder_id == holder and not current_expired
                        else _format_ts(issued_at)
                    )
                    cur.execute(
                        """
                        UPDATE enterprise_leases
                        SET holder_id = %s, expires_at = %s, acquired_at = %s,
                            renewed_at = %s, metadata_json = %s
                        WHERE org_id = %s AND name = %s
                        """,
                        (
                            holder,
                            _format_ts(expires_at),
                            acquired_at,
                            _format_ts(issued_at),
                            metadata_json,
                            self.org_id,
                            lease_name,
                        ),
                    )
                    lease = EnterpriseLease(
                        org_id=self.org_id,
                        name=lease_name,
                        holder_id=holder,
                        expires_at=_format_ts(expires_at),
                        acquired_at=acquired_at,
                        renewed_at=_format_ts(issued_at),
                        metadata=metadata or {},
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO enterprise_leases(
                            org_id, name, holder_id, expires_at, acquired_at, renewed_at, metadata_json
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            self.org_id,
                            lease_name,
                            holder,
                            _format_ts(expires_at),
                            _format_ts(issued_at),
                            _format_ts(issued_at),
                            metadata_json,
                        ),
                    )
                    lease = EnterpriseLease(
                        org_id=self.org_id,
                        name=lease_name,
                        holder_id=holder,
                        expires_at=_format_ts(expires_at),
                        acquired_at=_format_ts(issued_at),
                        renewed_at=_format_ts(issued_at),
                        metadata=metadata or {},
                    )
        return EnterpriseLeaseAttempt(acquired=True, lease=lease, current_holder_id=holder)

    def renew(
        self,
        name: str,
        *,
        holder_id: str,
        ttl_seconds: int | float = 60,
        now: datetime | None = None,
    ) -> EnterpriseLeaseAttempt:
        lease_name = _require_text(name, "lease name")
        holder = _require_text(holder_id, "holder id")
        issued_at = _normalize_now(now)
        expires_at = issued_at + timedelta(seconds=max(1.0, float(ttl_seconds)))
        with self.connect() as con:
            with con.cursor() as cur:
                _postgres_lock_lease_key(cur, self.org_id, lease_name)
                row = _postgres_fetch_lease(cur, self.org_id, lease_name, for_update=True)
                if row is None:
                    return EnterpriseLeaseAttempt(acquired=False, lease=None, current_holder_id=None)
                current = _postgres_row_to_lease(row)
                if current.holder_id != holder or _parse_ts(current.expires_at) <= issued_at:
                    return EnterpriseLeaseAttempt(
                        acquired=False,
                        lease=current,
                        current_holder_id=current.holder_id,
                    )
                cur.execute(
                    """
                    UPDATE enterprise_leases
                    SET expires_at = %s, renewed_at = %s
                    WHERE org_id = %s AND name = %s AND holder_id = %s
                    """,
                    (_format_ts(expires_at), _format_ts(issued_at), self.org_id, lease_name, holder),
                )
        lease = EnterpriseLease(
            org_id=current.org_id,
            name=current.name,
            holder_id=current.holder_id,
            expires_at=_format_ts(expires_at),
            acquired_at=current.acquired_at,
            renewed_at=_format_ts(issued_at),
            metadata=current.metadata,
        )
        return EnterpriseLeaseAttempt(acquired=True, lease=lease, current_holder_id=holder)

    def release(self, name: str, *, holder_id: str) -> bool:
        lease_name = _require_text(name, "lease name")
        holder = _require_text(holder_id, "holder id")
        with self.connect() as con:
            with con.cursor() as cur:
                cur.execute(
                    "DELETE FROM enterprise_leases WHERE org_id = %s AND name = %s AND holder_id = %s",
                    (self.org_id, lease_name, holder),
                )
                return int(getattr(cur, "rowcount", 0) or 0) > 0

    def get(self, name: str) -> EnterpriseLease | None:
        lease_name = _require_text(name, "lease name")
        with self.connect() as con:
            with con.cursor() as cur:
                row = _postgres_fetch_lease(cur, self.org_id, lease_name, for_update=False)
        return _postgres_row_to_lease(row) if row is not None else None


def _postgres_lock_lease_key(cur, org_id: str, lease_name: str) -> None:
    cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"{org_id}:{lease_name}",))


def _postgres_fetch_lease(cur, org_id: str, lease_name: str, *, for_update: bool):
    suffix = " FOR UPDATE" if for_update else ""
    cur.execute(
        """
        SELECT org_id, name, holder_id, expires_at, acquired_at, renewed_at, metadata_json
        FROM enterprise_leases
        WHERE org_id = %s AND name = %s
        """ + suffix,
        (org_id, lease_name),
    )
    return cur.fetchone()


def _postgres_row_to_lease(row) -> EnterpriseLease:
    if isinstance(row, dict):
        values = row
    else:
        values = {
            "org_id": row[0],
            "name": row[1],
            "holder_id": row[2],
            "expires_at": row[3],
            "acquired_at": row[4],
            "renewed_at": row[5],
            "metadata_json": row[6],
        }
    return EnterpriseLease(
        org_id=str(values["org_id"]),
        name=str(values["name"]),
        holder_id=str(values["holder_id"]),
        expires_at=str(values["expires_at"]),
        acquired_at=str(values["acquired_at"]),
        renewed_at=str(values["renewed_at"]),
        metadata=json.loads(values["metadata_json"] or "{}"),
    )


def _require_text(value: str, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{label} is required")
    return text


def _normalize_now(value: datetime | None) -> datetime:
    if value is None:
        return _utc_now()
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_ts(value: datetime) -> str:
    return _normalize_now(value).isoformat().replace("+00:00", "Z")


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
