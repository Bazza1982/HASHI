from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from orchestrator.config_json import read_config_json, write_config_json
from orchestrator.connector_delivery_preferences import (
    get_connector_preference,
    set_connector_preference,
)
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


def test_stage_preserves_other_grants_and_keeps_owner_scope(tmp_path):
    config_path, secrets_path, _store = _instance(tmp_path)
    config = read_config_json(config_path)
    config["agents"][0]["extra"]["authorized_telegram_ids"].append(7333)
    config["agents"][0]["extra"]["scoped_telegram_user_ids"] = [9444, 8872]
    write_config_json(config_path, config)
    set_connector_preference(
        tmp_path,
        "user:8872",
        "telegram",
        "mirror",
        False,
    )
    preference_path = tmp_path / "state" / "frontend_delivery_preferences.json"
    before_preferences = preference_path.read_bytes()

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
    assert config["agents"][0]["extra"]["authorized_telegram_ids"] == [7666, 7333]
    assert config["agents"][0]["extra"]["scoped_telegram_user_ids"] == [9444]
    assert "extra" not in config["agents"][1]
    assert config["agents"][0]["unrelated"] == "keep"
    assert config["agents"][1]["unrelated"] == "keep"
    # Rollback safety: staging does not change the private legacy fallback.
    assert read_config_json(secrets_path)["authorized_telegram_id"] == 8872
    assert get_connector_preference(
        tmp_path, "user:8872", "telegram", "mirror", default=False
    ) is False
    assert preference_path.read_bytes() == before_preferences


def test_observed_proof_selects_the_candidate_private_binding_not_the_latest_group(tmp_path):
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

    result = stage_telegram_owner_replacement(
        bridge_home=tmp_path,
        new_owner_id=7666,
        expected_current_owner_id=8872,
        proof_mode="observed",
    )
    assert result["proof_mode"] == "observed"
    assert config_path.read_bytes() != before_config
    assert secrets_path.read_bytes() == before_secrets


def test_stage_rejects_direct_runtime_proof_older_than_ten_minutes(tmp_path):
    config_path, secrets_path, store = _instance(tmp_path)
    before_config = config_path.read_bytes()
    before_secrets = secrets_path.read_bytes()
    binding = store.latest_channel_binding(
        owner_id="user:8872",
        agent_id="hashiko",
        surface="telegram",
        actor_id=7666,
        channel_key=7666,
    )
    observed_at = datetime.fromisoformat(
        str(binding["updated_at"]).replace("Z", "+00:00")
    ).astimezone(timezone.utc)

    with pytest.raises(TelegramOwnerMigrationError, match="fresh within 10 minutes"):
        stage_telegram_owner_replacement(
            bridge_home=tmp_path,
            new_owner_id=7666,
            expected_current_owner_id=8872,
            now=observed_at + timedelta(minutes=10, microseconds=1),
            proof_mode="observed",
        )

    assert config_path.read_bytes() == before_config
    assert secrets_path.read_bytes() == before_secrets


def test_observed_proof_checks_candidate_and_configurable_expiry(tmp_path):
    config_path, _, store = _instance(tmp_path)
    binding = store.latest_channel_binding(
        owner_id="user:8872", agent_id="hashiko", surface="telegram", actor_id=7666, channel_key=7666,
    )
    observed_at = datetime.fromisoformat(str(binding["updated_at"]).replace("Z", "+00:00"))
    before = config_path.read_bytes()
    with pytest.raises(TelegramOwnerMigrationError, match="No runtime-observed"):
        stage_telegram_owner_replacement(
            bridge_home=tmp_path, new_owner_id=9999, expected_current_owner_id=8872,
            proof_mode="observed", now=observed_at + timedelta(minutes=30),
            proof_max_age=timedelta(hours=1),
        )
    assert config_path.read_bytes() == before
    result = stage_telegram_owner_replacement(
        bridge_home=tmp_path, new_owner_id=7666, expected_current_owner_id=8872,
        proof_mode="observed", now=observed_at + timedelta(minutes=30),
        proof_max_age=timedelta(hours=1),
    )
    assert result["proof_mode"] == "observed"


def test_stage_resolves_existing_owner_with_legacy_secret_precedence(tmp_path):
    config_path, _secrets_path, _store = _instance(tmp_path)
    config = read_config_json(config_path)
    config["global"]["authorized_id"] = 9999
    write_config_json(config_path, config)

    result = stage_telegram_owner_replacement(
        bridge_home=tmp_path,
        new_owner_id=7666,
        expected_current_owner_id=8872,
    )

    assert result["previous_owner_id"] == 8872
    assert result["owner_scope_id"] == "user:8872"


def test_administrator_can_select_owner_without_recent_telegram_conversation(tmp_path):
    config_path, _secrets_path, _store = _instance(tmp_path)
    # An explicit administrator selection also works without a Session DB.
    (tmp_path / "state" / "sessions.sqlite3").unlink()
    result = stage_telegram_owner_replacement(
        bridge_home=tmp_path, new_owner_id=9999, expected_current_owner_id=8872,
    )
    assert result["proof_mode"] == "administrator_selected"
    assert result["proof_observed_at"] is None
    assert read_config_json(config_path)["global"]["telegram_owner_id"] == 9999


def test_primary_can_be_selected_after_the_previous_primary_was_removed(tmp_path):
    from orchestrator.telegram_access import TelegramAccessService

    config_path, secrets_path, _store = _instance(tmp_path)
    service = TelegramAccessService(config_path=config_path, secrets_path=secrets_path)
    state = service.status("hashiko")
    service.change("hashiko", expected_revision=state["revision"],
                   scope="global", action="remove", user_id="8872")
    result = stage_telegram_owner_replacement(
        bridge_home=tmp_path, new_owner_id=9999, expected_current_owner_id=None,
    )
    assert result["previous_owner_id"] is None
    assert read_config_json(config_path)["global"]["owner_scope_id"] == "user:8872"
    finalized = finalize_telegram_owner_secret(
        bridge_home=tmp_path, owner_id=9999, expected_previous_owner_id=None,
        expected_config_revision=result["config_revision"],
    )
    assert finalized["finalized"] is True
    assert read_config_json(secrets_path)["authorized_telegram_id"] == 9999


def test_finalize_replaces_only_private_owner_fallback(tmp_path):
    config_path, secrets_path, _store = _instance(tmp_path)
    staged = stage_telegram_owner_replacement(
        bridge_home=tmp_path,
        new_owner_id=7666,
        expected_current_owner_id=8872,
    )

    result = finalize_telegram_owner_secret(
        bridge_home=tmp_path,
        owner_id=7666,
        expected_previous_owner_id=8872,
        expected_config_revision=staged["config_revision"],
    )

    assert result["finalized"] is True
    secrets = read_config_json(secrets_path)
    assert secrets["authorized_telegram_id"] == 7666
    assert secrets["hashiko"] == "bot-token-is-preserved"
    assert secrets["provider"] == "provider-secret-is-preserved"
    assert read_config_json(config_path)["global"]["owner_scope_id"] == "user:8872"


def test_owner_replacement_preserves_global_grants_and_fences_finalization(tmp_path):
    config_path, secrets_path, _store = _instance(tmp_path)
    config = read_config_json(config_path)
    config["global"]["authorized_telegram_user_ids"] = [9888, 9777]
    write_config_json(config_path, config)
    staged_result = stage_telegram_owner_replacement(
        bridge_home=tmp_path, new_owner_id=7666, expected_current_owner_id=8872,
    )
    staged = read_config_json(config_path)
    assert staged["global"]["authorized_telegram_user_ids"] == [9888, 9777]
    staged["global"]["authorized_telegram_user_ids"].append(9666)
    write_config_json(config_path, staged)
    before = secrets_path.read_bytes()
    with pytest.raises(TelegramOwnerMigrationError):
        finalize_telegram_owner_secret(
            bridge_home=tmp_path, owner_id=7666, expected_previous_owner_id=8872,
            expected_config_revision=staged_result["config_revision"],
        )
    assert secrets_path.read_bytes() == before
    latest_revision = read_config_json(config_path).revision
    result = finalize_telegram_owner_secret(
        bridge_home=tmp_path, owner_id=7666, expected_previous_owner_id=8872,
        expected_config_revision=latest_revision,
    )
    assert result["finalized"] is True
    assert read_config_json(config_path)["global"]["authorized_telegram_user_ids"] == [9888, 9777, 9666]


def test_owner_replacement_can_explicitly_revoke_all_delegate_families(tmp_path):
    config_path, _, _ = _instance(tmp_path)
    config = read_config_json(config_path)
    config["global"]["authorized_telegram_user_ids"] = [9888]
    config["agents"][0]["extra"]["scoped_telegram_user_ids"] = [9777]
    config["agents"][1]["extra"] = {"authorized_telegram_ids": [9555]}
    write_config_json(config_path, config)
    stage_telegram_owner_replacement(
        bridge_home=tmp_path, new_owner_id=7666, expected_current_owner_id=8872,
        revoke_all_delegates=True,
    )
    staged = read_config_json(config_path)
    assert staged["global"]["authorized_telegram_user_ids"] == []
    for row in staged["agents"]:
        assert row["extra"].get("authorized_telegram_ids", []) == []
        assert row["extra"].get("scoped_telegram_user_ids", []) == []


def test_finalize_preserves_a_concurrently_changed_secret_owner(tmp_path):
    _config_path, secrets_path, _store = _instance(tmp_path)
    staged = stage_telegram_owner_replacement(
        bridge_home=tmp_path, new_owner_id=7666, expected_current_owner_id=8872,
    )
    secrets = read_config_json(secrets_path)
    secrets["authorized_telegram_id"] = 9999
    write_config_json(secrets_path, secrets)
    before = secrets_path.read_bytes()
    with pytest.raises(TelegramOwnerMigrationError, match="fallback changed"):
        finalize_telegram_owner_secret(
            bridge_home=tmp_path, owner_id=7666, expected_previous_owner_id=8872,
            expected_config_revision=staged["config_revision"],
        )
    assert secrets_path.read_bytes() == before


def test_finalize_accepts_a_revision_checked_legacy_staged_transition(tmp_path):
    config_path, secrets_path, _store = _instance(tmp_path)
    config = read_config_json(config_path)
    config["global"].update({"telegram_owner_id": 7666, "authorized_id": 7666,
                             "owner_scope_id": "user:8872"})
    for row in config["agents"]:
        row["extra"] = {"authorized_telegram_ids": [7666]}
    revision = write_config_json(config_path, config)
    result = finalize_telegram_owner_secret(
        bridge_home=tmp_path, owner_id=7666, expected_previous_owner_id=8872,
        expected_config_revision=revision,
    )
    assert result["finalized"] is True
    assert read_config_json(secrets_path)["authorized_telegram_id"] == 7666



def test_owner_replacement_preserves_conflicting_legacy_data_key(tmp_path):
    from orchestrator.personal_data_owner import read_personal_data_binding, resolve_personal_data_owner

    config_path, _, _ = _instance(tmp_path)
    assert resolve_personal_data_owner(tmp_path, {}, 8872, establish=True) == "user:8872"
    config = read_config_json(config_path)
    config["global"]["owner_scope_id"] = "legacy:other"
    write_config_json(config_path, config)
    result = stage_telegram_owner_replacement(
        bridge_home=tmp_path, new_owner_id=7666, expected_current_owner_id=8872,
    )
    saved = read_config_json(config_path)
    assert saved["global"]["telegram_owner_id"] == 7666
    assert saved["global"]["owner_scope_id"] == "legacy:other"
    assert result["owner_scope_id"] == "user:8872"
    assert read_personal_data_binding(tmp_path) == ("user:8872", True)



def test_admin_selected_owner_can_be_staged_without_secrets_or_unused_proof(tmp_path):
    config_path = tmp_path / "agents.json"
    write_config_json(config_path, {
        "global": {"instance_id": "TEST"},
        "agents": [{"name": "lily", "unrelated": "keep"}],
    })
    secrets_path = tmp_path / "secrets.json"
    result = stage_telegram_owner_replacement(
        bridge_home=tmp_path, new_owner_id=7654, expected_current_owner_id=None,
        proof_mode="administrator_selected", proof_max_age=timedelta(0),
    )
    assert result["staged"] is True
    assert not secrets_path.exists()
    assert not (tmp_path / "state" / "sessions.sqlite3").exists()
    assert read_config_json(config_path)["agents"][0]["unrelated"] == "keep"
    finalized = finalize_telegram_owner_secret(
        bridge_home=tmp_path, owner_id=7654, expected_previous_owner_id=None,
        expected_config_revision=result["config_revision"],
    )
    assert finalized["finalized"] is True
    assert read_config_json(secrets_path)["authorized_telegram_id"] == 7654


def test_owner_finalization_preserves_new_unrelated_fallback_when_initially_absent(tmp_path):
    config_path = tmp_path / "agents.json"
    secrets_path = tmp_path / "secrets.json"
    write_config_json(config_path, {
        "global": {"instance_id": "TEST"}, "agents": [{"name": "lily"}],
    })
    staged = stage_telegram_owner_replacement(
        bridge_home=tmp_path, new_owner_id=7654, expected_current_owner_id=None,
    )
    write_config_json(secrets_path, {"authorized_telegram_id": 9988, "other": "keep"})
    before = secrets_path.read_bytes()
    with pytest.raises(TelegramOwnerMigrationError, match="fallback changed"):
        finalize_telegram_owner_secret(
            bridge_home=tmp_path, owner_id=7654, expected_previous_owner_id=None,
            expected_config_revision=staged["config_revision"],
        )
    assert secrets_path.read_bytes() == before


def test_observed_owner_mode_still_validates_expiry(tmp_path):
    _instance(tmp_path)
    with pytest.raises(TelegramOwnerMigrationError, match="proof expiry"):
        stage_telegram_owner_replacement(
            bridge_home=tmp_path, new_owner_id=7666, expected_current_owner_id=8872,
            proof_mode="observed", proof_max_age=timedelta(0),
        )



def test_admin_selected_owner_ignores_unselected_observed_expiry(tmp_path):
    _instance(tmp_path)
    result = stage_telegram_owner_replacement(
        bridge_home=tmp_path, new_owner_id=7666, expected_current_owner_id=8872,
        proof_mode="administrator_selected", proof_max_age=timedelta(0),
    )
    assert result["staged"] is True
    assert result["proof_observed_at"] is None



def test_new_fallback_publication_does_not_overwrite_a_racing_secret_writer(tmp_path, monkeypatch):
    from orchestrator import telegram_owner
    from orchestrator.config_json import ConfigConflictError

    config_path = tmp_path / "agents.json"
    secrets_path = tmp_path / "secrets.json"
    write_config_json(config_path, {
        "global": {"instance_id": "TEST"}, "agents": [{"name": "lily"}],
    })
    staged = stage_telegram_owner_replacement(
        bridge_home=tmp_path, new_owner_id=7654, expected_current_owner_id=None,
    )
    actual_write = telegram_owner.write_config_json

    def racing_write(path, payload, **kwargs):
        # Simulate an independent writer between the absent read and publication.
        write_config_json(secrets_path, {"other": "racing-writer"})
        return actual_write(path, payload, **kwargs)

    monkeypatch.setattr(telegram_owner, "write_config_json", racing_write)
    with pytest.raises(ConfigConflictError):
        finalize_telegram_owner_secret(
            bridge_home=tmp_path, owner_id=7654, expected_previous_owner_id=None,
            expected_config_revision=staged["config_revision"],
        )
    assert read_config_json(secrets_path) == {"other": "racing-writer"}



def test_admin_cli_stages_account_on_a_fresh_instance_without_a_bot(tmp_path):
    import json
    import subprocess
    import sys

    config_path = tmp_path / "agents.json"
    write_config_json(config_path, {
        "global": {"instance_id": "TEST"}, "agents": [{"name": "lily"}],
    })
    script = Path(__file__).resolve().parents[1] / "scripts" / "telegram_owner_migrate.py"
    result = subprocess.run(
        [sys.executable, str(script), "--root", str(tmp_path), "stage",
         "--new-owner", "7654", "--expected-current-owner", "0",
         "--proof-max-age-seconds", "0"],
        capture_output=True, text=True, timeout=30, check=True,
    )
    assert json.loads(result.stdout)["staged"] is True
    assert read_config_json(config_path)["global"]["telegram_owner_id"] == 7654
    assert not (tmp_path / "secrets.json").exists()
