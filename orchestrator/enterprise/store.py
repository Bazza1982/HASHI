from __future__ import annotations

import sqlite3
from pathlib import Path


SCHEMA_VERSION = 6


class EnterpriseStore:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        return con

    def init_schema(self) -> None:
        with self.connect() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS organizations (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    org_id TEXT NOT NULL,
                    email TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(org_id, email),
                    FOREIGN KEY(org_id) REFERENCES organizations(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    org_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    workspace_root TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(org_id, name),
                    FOREIGN KEY(org_id) REFERENCES organizations(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS project_memberships (
                    user_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(user_id, project_id),
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS api_tokens (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    scopes_json TEXT NOT NULL,
                    expires_at TEXT,
                    revoked_at TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    expires_at TEXT NOT NULL,
                    revoked_at TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS service_accounts (
                    id TEXT PRIMARY KEY,
                    org_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    scopes_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(org_id, name),
                    FOREIGN KEY(org_id) REFERENCES organizations(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS channels (
                    id TEXT PRIMARY KEY,
                    org_id TEXT NOT NULL,
                    type TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    risk_tier TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(org_id, type),
                    FOREIGN KEY(org_id) REFERENCES organizations(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS channel_bindings (
                    channel_id TEXT NOT NULL,
                    scope_type TEXT NOT NULL,
                    scope_id TEXT NOT NULL,
                    permission TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(channel_id, scope_type, scope_id, permission),
                    FOREIGN KEY(channel_id) REFERENCES channels(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS policy_rules (
                    id TEXT PRIMARY KEY,
                    org_id TEXT NOT NULL,
                    scope_type TEXT NOT NULL,
                    scope_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    resource TEXT NOT NULL,
                    effect TEXT NOT NULL,
                    conditions_json TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 100,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(org_id) REFERENCES organizations(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS approval_requests (
                    id TEXT PRIMARY KEY,
                    org_id TEXT NOT NULL,
                    actor_id TEXT,
                    action TEXT NOT NULL,
                    resource TEXT NOT NULL,
                    status TEXT NOT NULL,
                    rule_id TEXT,
                    reason TEXT,
                    context_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    decided_by TEXT,
                    decided_at TEXT,
                    decision_reason TEXT,
                    FOREIGN KEY(org_id) REFERENCES organizations(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS audit_events (
                    id TEXT PRIMARY KEY,
                    org_id TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    actor_id TEXT,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    project_id TEXT,
                    task_id TEXT,
                    request_id TEXT,
                    correlation_id TEXT,
                    parent_event_id TEXT,
                    context_json TEXT NOT NULL,
                    FOREIGN KEY(org_id) REFERENCES organizations(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_audit_events_org_ts
                    ON audit_events(org_id, ts);
                CREATE INDEX IF NOT EXISTS idx_audit_events_org_type_ts
                    ON audit_events(org_id, event_type, ts);
                CREATE INDEX IF NOT EXISTS idx_audit_events_org_actor_ts
                    ON audit_events(org_id, actor_id, ts);
                CREATE INDEX IF NOT EXISTS idx_audit_events_org_project_ts
                    ON audit_events(org_id, project_id, ts);

                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    org_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    user_id TEXT,
                    agent_id TEXT,
                    status TEXT NOT NULL,
                    prompt_summary TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    failed_reason TEXT,
                    metadata_json TEXT NOT NULL,
                    FOREIGN KEY(org_id) REFERENCES organizations(id) ON DELETE CASCADE,
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE SET NULL
                );

                CREATE INDEX IF NOT EXISTS idx_tasks_org_project_status
                    ON tasks(org_id, project_id, status);
                CREATE INDEX IF NOT EXISTS idx_tasks_org_user_created
                    ON tasks(org_id, user_id, created_at);

                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY,
                    org_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    type TEXT NOT NULL,
                    path TEXT NOT NULL,
                    hash TEXT,
                    created_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    FOREIGN KEY(org_id) REFERENCES organizations(id) ON DELETE CASCADE,
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
                    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_artifacts_org_task
                    ON artifacts(org_id, task_id);

                CREATE TABLE IF NOT EXISTS evidence_bundles (
                    id TEXT PRIMARY KEY,
                    org_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    audit_event_ids_json TEXT NOT NULL,
                    artifact_ids_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    FOREIGN KEY(org_id) REFERENCES organizations(id) ON DELETE CASCADE,
                    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_evidence_bundles_org_task
                    ON evidence_bundles(org_id, task_id);
                """
            )
            con.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
                ("schema_version", str(SCHEMA_VERSION)),
            )

    def schema_version(self) -> int | None:
        if not self.db_path.exists():
            return None
        with self.connect() as con:
            try:
                row = con.execute("SELECT value FROM schema_meta WHERE key = ?", ("schema_version",)).fetchone()
            except sqlite3.OperationalError:
                return None
        return int(row["value"]) if row else None

    def migrate(self) -> dict:
        before = self.schema_version()
        self.init_schema()
        after = self.schema_version()
        return {
            "db_path": str(self.db_path),
            "before": before,
            "after": after,
            "schema_version": SCHEMA_VERSION,
        }
