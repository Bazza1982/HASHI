from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.config_json import read_config_json, write_config_json
from orchestrator.connector_delivery_preferences import get_connector_preference
from orchestrator.session_store import SessionStore
from orchestrator.telegram_owner import (
    TelegramOwnerMigrationError,
    finalize_telegram_owner_secret,
    stage_telegram_owner_replacement,
)


def _instance(root: Path) -> tuple[Path, Path, SessionStore]:
    root.mkdir(parents=True, exist_ok=True)
    config_path = root / "agents.json"
    secrets_path = root / "secrets.json"
    write_config_json(
        config_path,
        {
            "global": {"instance_id": "MSI", "authorized_id": 8872},
            "agents": [
                {
                    "name": "hashiko",
                    "extra": {"authorized_telegram_ids": [8872, 7666]},
                    "unrelated": "keep",
                },
                {"name": "local-only", "unrelated": "keep"},
            ],
        },
    )
    write_config_json(
        secrets_path,
        {
            "authorized_telegram_id": 8872,
            "hashiko": "bot-token-is-preserved",
            "provider": "provider-secret-is-preserved",
        },
    )
    store = SessionStore(root / "state" / "sessions.sqlite3", instance_id="MSI")
    session = store.ensure_default_session(owner_id="user:8872", agent_id="hashiko")
    store.bind_channel(
        owner_id="user:8872",
        agent_id="hashiko",
        surface="telegram",
        channel_key="7666",
        session_id=session["session_id"],
        actor_id=7666,
        actor_role="secondary",
    )
    return config_path, secrets_path, store


def test_stage_uses_latest_direct_ingress_and_revokes_every_other_actor(tmp_path):
    config_path, secrets_path, _store = _instance(tmp_path)

    result = stage_telegram_owner_replacement(
        bridge_home=tmp_path,
        new_owner_id=7666,
        expected_current_owner_id=8872,
    )

    assert result["staged"] is True
    assert result["agents_updated"] == 2
    config = read_config_json(config_path)
    assert config["global"]["telegram_owner_id"] == 7666
    assert config["global"]["authorized_id"] == 7666
    assert config["global"]["owner_scope_id"] == "user:8872"
    assert all(
        row["extra"]["authorized_telegram_ids"] == [7666]
        for row in config["agents"]
    )
    assert config["agents"][0]["unrelated"] == "keep"
    assert config["agents"][1]["unrelated"] == "keep"
    # Rollback safety: staging does not change the private legacy fallback.
    assert read_config_json(secrets_path)["authorized_telegram_id"] == 8872
    assert get_connector_preference(
        tmp_path, "user:8872", "telegram", "mirror", default=False
    ) is True


def test_stage_rejects_a_candidate_without_latest_direct_runtime_proof(tmp_path):
    config_path, secrets_path, store = _instance(tmp_path)
    before_config = config_path.read_bytes()
    before_secrets = secrets_path.read_bytes()
    session = store.resolve_primary_session(
        owner_id="user:8872",
        agent_id="hashiko",
        establish=True,
    )
    store.bind_channel(
        owner_id="user:8872",
        agent_id="hashiko",
        surface="telegram",
        channel_key="-1007666",
        session_id=session["session_id"],
        actor_id=7666,
        actor_role="secondary",
    )

    with pytest.raises(TelegramOwnerMigrationError, match="direct private chat"):
        stage_telegram_owner_replacement(
            bridge_home=tmp_path,
            new_owner_id=7666,
            expected_current_owner_id=8872,
        )

    assert config_path.read_bytes() == before_config
    assert secrets_path.read_bytes() == before_secrets


def test_finalize_replaces_only_private_owner_fallback(tmp_path):
    config_path, secrets_path, _store = _instance(tmp_path)
    stage_telegram_owner_replacement(
        bridge_home=tmp_path,
        new_owner_id=7666,
        expected_current_owner_id=8872,
    )

    result = finalize_telegram_owner_secret(
        bridge_home=tmp_path,
        owner_id=7666,
        expected_previous_owner_id=8872,
    )

    assert result["finalized"] is True
    secrets = read_config_json(secrets_path)
    assert secrets["authorized_telegram_id"] == 7666
    assert secrets["hashiko"] == "bot-token-is-preserved"
    assert secrets["provider"] == "provider-secret-is-preserved"
    assert read_config_json(config_path)["global"]["owner_scope_id"] == "user:8872"
