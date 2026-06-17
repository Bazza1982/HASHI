from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

from orchestrator.enterprise.store import EnterpriseStore


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _hash_password(password: str, *, salt: bytes | None = None, iterations: int = 210_000) -> str:
    if not password:
        raise ValueError("password is required")
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    salt_b64 = base64.b64encode(salt).decode("ascii")
    digest_b64 = base64.b64encode(digest).decode("ascii")
    return f"pbkdf2_sha256${iterations}${salt_b64}${digest_b64}"


def _verify_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, iterations_s, salt_b64, digest_b64 = stored_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iterations_s)
        salt = base64.b64decode(salt_b64.encode("ascii"))
        expected = base64.b64decode(digest_b64.encode("ascii"))
    except Exception:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


class EnterpriseRole(str, Enum):
    INDIVIDUAL_USER = "individual_user"
    TEAM_ADMIN = "team_admin"
    ORG_ADMIN = "org_admin"
    SECURITY_ADMIN = "security_admin"
    SYSTEM_OPERATOR = "system_operator"
    AUDITOR = "auditor"
    SERVICE_ACCOUNT = "service_account"

    @classmethod
    def from_value(cls, value: str) -> "EnterpriseRole":
        normalized = (value or "").strip().lower()
        for role in cls:
            if role.value == normalized:
                return role
        raise ValueError(f"unsupported enterprise role: {value!r}")


@dataclass(frozen=True)
class Organization:
    id: str
    name: str
    created_at: str


@dataclass(frozen=True)
class User:
    id: str
    org_id: str
    email: str
    display_name: str
    status: str
    created_at: str


@dataclass(frozen=True)
class Project:
    id: str
    org_id: str
    name: str
    workspace_root: str | None
    created_at: str


@dataclass(frozen=True)
class Session:
    id: str
    user_id: str
    token: str
    expires_at: str
    created_at: str


@dataclass(frozen=True)
class ApiToken:
    id: str
    user_id: str
    token: str
    scopes: tuple[str, ...]
    expires_at: str | None
    created_at: str
    revoked_at: str | None = None


@dataclass(frozen=True)
class ServiceAccount:
    id: str
    org_id: str
    name: str
    scopes: tuple[str, ...]
    status: str
    created_at: str


class IdentityService:
    def __init__(self, store: EnterpriseStore):
        self.store = store
        self.store.init_schema()

    @classmethod
    def from_path(cls, db_path: Path | str) -> "IdentityService":
        return cls(EnterpriseStore(db_path))

    def create_organization(self, *, org_id: str, name: str) -> Organization:
        org_id = _require_id(org_id, "org_id")
        name = _require_text(name, "name")
        now = _utc_now_iso()
        with self.store.connect() as con:
            con.execute(
                "INSERT INTO organizations(id, name, created_at) VALUES (?, ?, ?)",
                (org_id, name, now),
            )
        return Organization(id=org_id, name=name, created_at=now)

    def get_organization(self, org_id: str) -> Organization | None:
        with self.store.connect() as con:
            row = con.execute("SELECT * FROM organizations WHERE id = ?", (org_id,)).fetchone()
        return _organization_from_row(row) if row else None

    def bootstrap_org_admin(
        self,
        *,
        org_id: str,
        org_name: str,
        email: str,
        display_name: str,
        password: str,
        user_id: str | None = None,
    ) -> User:
        with self.store.connect() as con:
            existing = con.execute("SELECT id FROM organizations WHERE id = ?", (org_id,)).fetchone()
            if existing:
                users = con.execute("SELECT COUNT(*) FROM users WHERE org_id = ?", (org_id,)).fetchone()[0]
                if users:
                    raise ValueError(f"organization {org_id!r} is already bootstrapped")
            else:
                con.execute(
                    "INSERT INTO organizations(id, name, created_at) VALUES (?, ?, ?)",
                    (_require_id(org_id, "org_id"), _require_text(org_name, "org_name"), _utc_now_iso()),
                )
            user = self._create_user_with_connection(
                con,
                org_id=org_id,
                email=email,
                display_name=display_name,
                password=password,
                user_id=user_id,
                status="active",
            )
            project = self._create_project_with_connection(
                con,
                org_id=org_id,
                name="Default",
                workspace_root=None,
                project_id=f"{org_id}-default",
            )
            self._assign_membership_with_connection(
                con,
                user_id=user.id,
                project_id=project.id,
                role=EnterpriseRole.ORG_ADMIN,
            )
            return user

    def create_user(
        self,
        *,
        org_id: str,
        email: str,
        display_name: str,
        password: str,
        user_id: str | None = None,
        status: str = "active",
    ) -> User:
        with self.store.connect() as con:
            return self._create_user_with_connection(
                con,
                org_id=org_id,
                email=email,
                display_name=display_name,
                password=password,
                user_id=user_id,
                status=status,
            )

    def upsert_oidc_user(
        self,
        *,
        org_id: str,
        email: str,
        display_name: str,
        default_project_id: str | None = None,
        default_role: EnterpriseRole | str = EnterpriseRole.INDIVIDUAL_USER,
    ) -> tuple[User, bool]:
        with self.store.connect() as con:
            row = con.execute(
                "SELECT * FROM users WHERE org_id = ? AND lower(email) = lower(?)",
                (_require_id(org_id, "org_id"), _normalize_email(email)),
            ).fetchone()
            if row:
                user = _user_from_row(row)
                if user.status != "active":
                    raise ValueError("OIDC user is not active")
                created = False
            else:
                user = self._create_user_with_connection(
                    con,
                    org_id=org_id,
                    email=email,
                    display_name=display_name,
                    password=secrets.token_urlsafe(32),
                    user_id=None,
                    status="active",
                )
                created = True
            if default_project_id:
                self._assign_membership_with_connection(
                    con,
                    user_id=user.id,
                    project_id=default_project_id,
                    role=default_role,
                )
        return user, created

    def get_user(self, user_id: str) -> User | None:
        with self.store.connect() as con:
            row = con.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return _user_from_row(row) if row else None

    def get_user_by_email(self, *, org_id: str, email: str) -> User | None:
        with self.store.connect() as con:
            row = con.execute(
                "SELECT * FROM users WHERE org_id = ? AND lower(email) = lower(?)",
                (_require_id(org_id, "org_id"), _normalize_email(email)),
            ).fetchone()
        return _user_from_row(row) if row else None

    def list_users(self, *, org_id: str) -> list[User]:
        with self.store.connect() as con:
            rows = con.execute(
                "SELECT * FROM users WHERE org_id = ? ORDER BY email",
                (_require_id(org_id, "org_id"),),
            ).fetchall()
        return [_user_from_row(row) for row in rows]

    def authenticate_user(self, *, org_id: str, email: str, password: str) -> User | None:
        with self.store.connect() as con:
            row = con.execute(
                "SELECT * FROM users WHERE org_id = ? AND lower(email) = lower(?) AND status = 'active'",
                (org_id, email),
            ).fetchone()
        if not row or not _verify_password(password, row["password_hash"]):
            return None
        return _user_from_row(row)

    def create_session(self, *, user_id: str, ttl_seconds: int = 86400) -> Session:
        token = "hs_sess_" + secrets.token_urlsafe(32)
        now = _utc_now_iso()
        expires_at = (datetime.now(tz=timezone.utc) + timedelta(seconds=max(1, ttl_seconds))).isoformat()
        session_id = "sess_" + secrets.token_urlsafe(12)
        with self.store.connect() as con:
            con.execute(
                """
                INSERT INTO sessions(id, user_id, token_hash, expires_at, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, user_id, _hash_secret(token), expires_at, now),
            )
        return Session(id=session_id, user_id=user_id, token=token, expires_at=expires_at, created_at=now)

    def get_session_user(self, token: str) -> User | None:
        token_hash = _hash_secret(token)
        now = _utc_now_iso()
        with self.store.connect() as con:
            row = con.execute(
                """
                SELECT users.*
                FROM sessions
                JOIN users ON users.id = sessions.user_id
                WHERE sessions.token_hash = ?
                  AND sessions.revoked_at IS NULL
                  AND sessions.expires_at > ?
                  AND users.status = 'active'
                """,
                (token_hash, now),
            ).fetchone()
        return _user_from_row(row) if row else None

    def revoke_session(self, token: str) -> bool:
        with self.store.connect() as con:
            cur = con.execute(
                "UPDATE sessions SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
                (_utc_now_iso(), _hash_secret(token)),
            )
            return cur.rowcount > 0

    def create_project(
        self,
        *,
        org_id: str,
        name: str,
        workspace_root: str | None = None,
        project_id: str | None = None,
    ) -> Project:
        with self.store.connect() as con:
            return self._create_project_with_connection(
                con,
                org_id=org_id,
                name=name,
                workspace_root=workspace_root,
                project_id=project_id,
            )

    def get_project(self, project_id: str) -> Project | None:
        with self.store.connect() as con:
            row = con.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return _project_from_row(row) if row else None

    def list_projects(self, *, org_id: str) -> list[Project]:
        with self.store.connect() as con:
            rows = con.execute(
                "SELECT * FROM projects WHERE org_id = ? ORDER BY name",
                (_require_id(org_id, "org_id"),),
            ).fetchall()
        return [_project_from_row(row) for row in rows]

    def assign_project_role(self, *, user_id: str, project_id: str, role: EnterpriseRole | str) -> None:
        with self.store.connect() as con:
            self._assign_membership_with_connection(con, user_id=user_id, project_id=project_id, role=role)

    def list_project_memberships(self, *, user_id: str) -> list[dict[str, str]]:
        with self.store.connect() as con:
            rows = con.execute(
                """
                SELECT project_id, role, created_at
                FROM project_memberships
                WHERE user_id = ?
                ORDER BY project_id
                """,
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_api_token(
        self,
        *,
        user_id: str,
        scopes: list[str] | tuple[str, ...],
        expires_at: str | None = None,
    ) -> ApiToken:
        token = "hs_api_" + secrets.token_urlsafe(32)
        token_id = "api_" + secrets.token_urlsafe(12)
        scopes_tuple = tuple(str(scope).strip() for scope in scopes if str(scope).strip())
        if not scopes_tuple:
            raise ValueError("at least one API token scope is required")
        now = _utc_now_iso()
        with self.store.connect() as con:
            con.execute(
                """
                INSERT INTO api_tokens(id, user_id, token_hash, scopes_json, expires_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (token_id, user_id, _hash_secret(token), json.dumps(list(scopes_tuple)), expires_at, now),
            )
        return ApiToken(id=token_id, user_id=user_id, token=token, scopes=scopes_tuple, expires_at=expires_at, created_at=now)

    def validate_api_token(self, token: str) -> ApiToken | None:
        now = _utc_now_iso()
        with self.store.connect() as con:
            row = con.execute(
                """
                SELECT *
                FROM api_tokens
                WHERE token_hash = ?
                  AND revoked_at IS NULL
                  AND (expires_at IS NULL OR expires_at > ?)
                """,
                (_hash_secret(token), now),
            ).fetchone()
        return _api_token_from_row(row, token=token) if row else None

    def revoke_api_token(self, token: str) -> bool:
        with self.store.connect() as con:
            cur = con.execute(
                "UPDATE api_tokens SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
                (_utc_now_iso(), _hash_secret(token)),
            )
            return cur.rowcount > 0

    def revoke_api_token_by_id(self, token_id: str) -> bool:
        with self.store.connect() as con:
            cur = con.execute(
                "UPDATE api_tokens SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                (_utc_now_iso(), _require_id(token_id, "token_id")),
            )
            return cur.rowcount > 0

    def list_api_tokens(
        self,
        *,
        org_id: str | None = None,
        user_id: str | None = None,
        include_revoked: bool = False,
    ) -> list[ApiToken]:
        clauses = []
        params: list[str] = []
        if org_id is not None:
            clauses.append("users.org_id = ?")
            params.append(_require_id(org_id, "org_id"))
        if user_id is not None:
            clauses.append("api_tokens.user_id = ?")
            params.append(_require_id(user_id, "user_id"))
        if not include_revoked:
            clauses.append("api_tokens.revoked_at IS NULL")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.store.connect() as con:
            rows = con.execute(
                f"""
                SELECT api_tokens.*
                FROM api_tokens
                JOIN users ON users.id = api_tokens.user_id
                {where}
                ORDER BY api_tokens.created_at DESC, api_tokens.id
                """,
                tuple(params),
            ).fetchall()
        return [_api_token_from_row(row, token="") for row in rows]

    def create_service_account(
        self,
        *,
        org_id: str,
        name: str,
        scopes: list[str] | tuple[str, ...],
        service_account_id: str | None = None,
    ) -> ServiceAccount:
        scopes_tuple = tuple(str(scope).strip() for scope in scopes if str(scope).strip())
        if not scopes_tuple:
            raise ValueError("at least one service account scope is required")
        account_id = service_account_id or "svc_" + secrets.token_urlsafe(12)
        now = _utc_now_iso()
        with self.store.connect() as con:
            con.execute(
                """
                INSERT INTO service_accounts(id, org_id, name, scopes_json, status, created_at)
                VALUES (?, ?, ?, ?, 'active', ?)
                """,
                (
                    _require_id(account_id, "service_account_id"),
                    _require_id(org_id, "org_id"),
                    _require_text(name, "name"),
                    json.dumps(list(scopes_tuple)),
                    now,
                ),
            )
        return ServiceAccount(id=account_id, org_id=org_id, name=name, scopes=scopes_tuple, status="active", created_at=now)

    def _create_user_with_connection(
        self,
        con,
        *,
        org_id: str,
        email: str,
        display_name: str,
        password: str,
        user_id: str | None,
        status: str,
    ) -> User:
        now = _utc_now_iso()
        user = User(
            id=user_id or "usr_" + secrets.token_urlsafe(12),
            org_id=_require_id(org_id, "org_id"),
            email=_normalize_email(email),
            display_name=_require_text(display_name, "display_name"),
            status=_require_text(status, "status"),
            created_at=now,
        )
        con.execute(
            """
            INSERT INTO users(id, org_id, email, display_name, password_hash, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user.id, user.org_id, user.email, user.display_name, _hash_password(password), user.status, user.created_at),
        )
        return user

    def _create_project_with_connection(
        self,
        con,
        *,
        org_id: str,
        name: str,
        workspace_root: str | None,
        project_id: str | None,
    ) -> Project:
        now = _utc_now_iso()
        project = Project(
            id=project_id or "prj_" + secrets.token_urlsafe(12),
            org_id=_require_id(org_id, "org_id"),
            name=_require_text(name, "name"),
            workspace_root=str(workspace_root) if workspace_root not in (None, "") else None,
            created_at=now,
        )
        con.execute(
            """
            INSERT INTO projects(id, org_id, name, workspace_root, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (project.id, project.org_id, project.name, project.workspace_root, project.created_at),
        )
        return project

    def _assign_membership_with_connection(
        self,
        con,
        *,
        user_id: str,
        project_id: str,
        role: EnterpriseRole | str,
    ) -> None:
        role_value = role.value if isinstance(role, EnterpriseRole) else EnterpriseRole.from_value(str(role)).value
        con.execute(
            """
            INSERT INTO project_memberships(user_id, project_id, role, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, project_id) DO UPDATE SET role = excluded.role
            """,
            (_require_id(user_id, "user_id"), _require_id(project_id, "project_id"), role_value, _utc_now_iso()),
        )


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


def _normalize_email(value: str) -> str:
    email = _require_text(value, "email").lower()
    if "@" not in email:
        raise ValueError("email must contain @")
    return email


def _organization_from_row(row) -> Organization:
    return Organization(id=row["id"], name=row["name"], created_at=row["created_at"])


def _user_from_row(row) -> User:
    return User(
        id=row["id"],
        org_id=row["org_id"],
        email=row["email"],
        display_name=row["display_name"],
        status=row["status"],
        created_at=row["created_at"],
    )


def _project_from_row(row) -> Project:
    return Project(
        id=row["id"],
        org_id=row["org_id"],
        name=row["name"],
        workspace_root=row["workspace_root"],
        created_at=row["created_at"],
    )


def _api_token_from_row(row, *, token: str) -> ApiToken:
    return ApiToken(
        id=row["id"],
        user_id=row["user_id"],
        token=token,
        scopes=tuple(json.loads(row["scopes_json"])),
        expires_at=row["expires_at"],
        created_at=row["created_at"],
        revoked_at=row["revoked_at"],
    )
