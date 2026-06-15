from __future__ import annotations

import sqlite3

import pytest

from orchestrator.enterprise import ConnectorCredentialStore, IdentityService
from orchestrator.enterprise.store import SCHEMA_VERSION, EnterpriseStore


def _init_store(tmp_path):
    db_path = tmp_path / "enterprise.sqlite"
    IdentityService.from_path(db_path).create_organization(org_id="ORG-001", name="Acme")
    return ConnectorCredentialStore.from_path(db_path), db_path


def test_connector_credential_store_creates_lists_and_sorts_scopes(tmp_path):
    store, db_path = _init_store(tmp_path)

    credential = store.create_credential(
        org_id="ORG-001",
        connector_type="GitHub",
        display_name="GitHub App",
        secret_ref="vault://github/app",
        scopes=["repo:read", "repo:write", "repo:read"],
        credential_id="cred-github",
    )

    assert credential.connector_type == "github"
    assert credential.scopes == ("repo:read", "repo:write")
    assert credential.status == "active"
    assert store.list_credentials(org_id="ORG-001") == [credential]
    with sqlite3.connect(db_path) as con:
        row = con.execute("SELECT secret_ref FROM connector_credentials WHERE id = ?", ("cred-github",)).fetchone()
    assert row[0] == "vault://github/app"


def test_connector_credential_store_revokes_and_hides_by_default(tmp_path):
    store, _ = _init_store(tmp_path)
    store.create_credential(
        org_id="ORG-001",
        connector_type="slack",
        display_name="Slack Bot",
        secret_ref="vault://slack/bot",
        scopes=["chat:write"],
        credential_id="cred-slack",
    )

    revoked = store.revoke_credential("cred-slack")

    assert revoked.status == "revoked"
    assert revoked.revoked_at is not None
    assert store.list_credentials(org_id="ORG-001") == []
    assert store.list_credentials(org_id="ORG-001", include_revoked=True) == [revoked]


def test_connector_credential_store_rejects_double_revoke(tmp_path):
    store, _ = _init_store(tmp_path)
    store.create_credential(
        org_id="ORG-001",
        connector_type="teams",
        display_name="Teams Bot",
        secret_ref="vault://teams/bot",
        scopes=["message.send"],
        credential_id="cred-teams",
    )
    store.revoke_credential("cred-teams")

    with pytest.raises(ValueError, match="active connector credential not found"):
        store.revoke_credential("cred-teams")


def test_enterprise_store_schema_version_includes_connector_credentials(tmp_path):
    store = EnterpriseStore(tmp_path / "enterprise.sqlite")
    result = store.migrate()

    assert result["schema_version"] == SCHEMA_VERSION
    with store.connect() as con:
        row = con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'connector_credentials'"
        ).fetchone()
    assert row is not None
