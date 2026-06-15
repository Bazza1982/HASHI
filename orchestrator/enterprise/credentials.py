from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from orchestrator.enterprise.store import EnterpriseStore


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


@dataclass(frozen=True)
class ConnectorCredential:
    id: str
    org_id: str
    connector_type: str
    display_name: str
    secret_ref: str
    scopes: tuple[str, ...]
    status: str
    created_at: str
    revoked_at: str | None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "org_id": self.org_id,
            "connector_type": self.connector_type,
            "display_name": self.display_name,
            "secret_ref": self.secret_ref,
            "scopes": list(self.scopes),
            "status": self.status,
            "created_at": self.created_at,
            "revoked_at": self.revoked_at,
        }


class ConnectorCredentialStore:
    def __init__(self, store: EnterpriseStore):
        self.store = store
        self.store.init_schema()

    @classmethod
    def from_path(cls, db_path: Path | str) -> "ConnectorCredentialStore":
        return cls(EnterpriseStore(db_path))

    def create_credential(
        self,
        *,
        org_id: str,
        connector_type: str,
        display_name: str,
        secret_ref: str,
        scopes: list[str] | tuple[str, ...],
        credential_id: str | None = None,
    ) -> ConnectorCredential:
        credential_id = _require_id(credential_id or f"cred-{uuid4().hex}", "credential_id")
        now = _utc_now_iso()
        with self.store.connect() as con:
            con.execute(
                """
                INSERT INTO connector_credentials(
                    id, org_id, connector_type, display_name, secret_ref,
                    scopes_json, status, created_at, revoked_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    credential_id,
                    _require_id(org_id, "org_id"),
                    _require_text(connector_type, "connector_type").lower(),
                    _require_text(display_name, "display_name"),
                    _require_text(secret_ref, "secret_ref"),
                    json.dumps(_normalize_scopes(scopes), sort_keys=True),
                    "active",
                    now,
                ),
            )
        return self.get_credential(credential_id)

    def get_credential(self, credential_id: str) -> ConnectorCredential:
        with self.store.connect() as con:
            row = con.execute(
                "SELECT * FROM connector_credentials WHERE id = ?",
                (_require_id(credential_id, "credential_id"),),
            ).fetchone()
        if row is None:
            raise ValueError(f"connector credential not found: {credential_id!r}")
        return _credential_from_row(row)

    def list_credentials(
        self,
        *,
        org_id: str,
        connector_type: str | None = None,
        include_revoked: bool = False,
    ) -> list[ConnectorCredential]:
        clauses = ["org_id = ?"]
        params = [_require_id(org_id, "org_id")]
        if connector_type:
            clauses.append("connector_type = ?")
            params.append(str(connector_type).strip().lower())
        if not include_revoked:
            clauses.append("status != ?")
            params.append("revoked")
        sql = f"""
            SELECT *
            FROM connector_credentials
            WHERE {' AND '.join(clauses)}
            ORDER BY created_at ASC, id ASC
        """
        with self.store.connect() as con:
            rows = con.execute(sql, params).fetchall()
        return [_credential_from_row(row) for row in rows]

    def revoke_credential(self, credential_id: str) -> ConnectorCredential:
        now = _utc_now_iso()
        with self.store.connect() as con:
            cur = con.execute(
                """
                UPDATE connector_credentials
                SET status = ?, revoked_at = ?
                WHERE id = ? AND status != ?
                """,
                ("revoked", now, _require_id(credential_id, "credential_id"), "revoked"),
            )
            if cur.rowcount == 0:
                raise ValueError(f"active connector credential not found: {credential_id!r}")
        return self.get_credential(credential_id)


def _credential_from_row(row) -> ConnectorCredential:
    return ConnectorCredential(
        id=row["id"],
        org_id=row["org_id"],
        connector_type=row["connector_type"],
        display_name=row["display_name"],
        secret_ref=row["secret_ref"],
        scopes=tuple(json.loads(row["scopes_json"] or "[]")),
        status=row["status"],
        created_at=row["created_at"],
        revoked_at=row["revoked_at"],
    )


def _normalize_scopes(scopes) -> list[str]:
    return sorted({str(scope).strip() for scope in scopes or [] if str(scope).strip()})


def _require_id(value: str, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


def _require_text(value: str, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized
