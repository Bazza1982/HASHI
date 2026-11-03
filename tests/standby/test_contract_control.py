"""Standby S1 controls use native storage; unqualified live paths stay closed."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from orchestrator.session_store import SessionStore
from orchestrator.standby.config import StandbyProfiles
from orchestrator.standby.contract import StandbyError, normalize_profile
from orchestrator.standby.service import StandbyService


def profile(**changes):
    result = {
        "name": "Synthetic quiet profile",
        "raw_goal": "Only help when explicitly asked.",
        "confirmed_interpretation": "Observe explicit requests; stay silent otherwise.",
        "targets": [{"id": "direct-help", "description": "Direct request", "action": "wake_agent"}],
    }
    result.update(changes)
    return result


@pytest.fixture
def native(tmp_path):
    store = SessionStore(tmp_path / "sessions.sqlite3", instance_id="SB-TEST")
    session = store.ensure_default_session(owner_id="user:7", agent_id="alice")
    binding = {key: session[key] for key in ("agent_id", "session_id", "context_generation")}
    config = StandbyProfiles(tmp_path / "standby_profiles.json")
    saved = config.save("user:7", "alice", "quiet", profile(), expected_revision=None)
    return store, config, binding, saved


def test_profile_defaults_are_no_capture_no_recording_and_no_memory():
    parsed = normalize_profile(profile())
    assert parsed["inputs"] == []
    assert parsed["recording"] == {"transcript": False, "notes": False}
    assert parsed["allow_external_processing"] is False
    assert parsed["duration_seconds"] == 7200
    assert "privacy_level" not in parsed
    for changes in [
        {"privacy_level": 0}, {"model_url": "https://example.invalid"},
        {"inputs": ["screen"]}, {"duration_seconds": True},
        {"recording": {"notes": True, "raw_audio": True}},
        {"targets": [{"id": "x", "description": "x", "action": "shell"}]},
        {"targets": [{"id": "x", "description": "x", "action": "none"}] * 2},
        {"confirmed_interpretation": ""},
    ]:
        with pytest.raises(StandbyError):
            normalize_profile(profile(**changes))


def test_profile_cas_scopes_missing_and_corrupt_file_do_not_overwrite(tmp_path):
    path = tmp_path / "profiles.json"
    configs = StandbyProfiles(path)
    assert configs.list("user:7", "alice") == {"revision": None, "profiles": []}
    assert not path.exists()
    saved = configs.save("user:7", "alice", "quiet", profile(), expected_revision=None)
    with pytest.raises(StandbyError, match="standby_revision_conflict"):
        configs.save("user:7", "alice", "quiet", profile(name="stale"), expected_revision=None)
    assert configs.list("user:other", "alice")["profiles"] == []
    assert configs.list("user:7", "bob")["profiles"] == []
    assert configs.get("user:7", "alice", "quiet")["profile"]["name"] == profile()["name"]
    configs.save("user:7", "alice", "quiet", profile(name="new"), expected_revision=saved["revision"])
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(StandbyError, match="standby_configuration_invalid"):
        configs.list("user:7", "alice")
    assert path.read_text(encoding="utf-8") == "{broken"


def configure(store, binding, *, operation_id="configure-one", now=1000):
    return store.standby_control(
        owner_id="user:7",
        body={"operation": "configure", "operation_id": operation_id,
              "issued_at": now, "binding": binding, "profile_id": "quiet",
              "profile_revision": "a" * 64},
        now=now,
    )


def mutate(store, binding, snapshot, operation, operation_id, *, now=1000):
    return store.standby_control(owner_id="user:7", body={
        "operation": operation, "operation_id": operation_id, "issued_at": now,
        "binding": binding, "standby_id": snapshot["standby_id"],
        "expected_revision": snapshot["revision"], "epoch": snapshot["epoch"],
    }, now=now)


def test_native_controls_replay_reopen_scope_and_stop_fence(native, tmp_path):
    store, _, binding, _ = native
    configured = configure(store, binding)
    assert configured["lifecycle"] == "CONFIGURED"
    assert configured["mode"] == "SHADOW" and configured["epoch"] == 1
    assert configure(SessionStore(store.db_path, instance_id="SB-TEST"), binding) == configured
    with pytest.raises(StandbyError, match="standby_operation_conflict"):
        configure(store, {**binding, "context_generation": binding["context_generation"] + 1})
    ended = mutate(store, binding, configured, "end", "end-one")
    assert ended["epoch"] > configured["epoch"] and ended["lifecycle"] == "STOPPED"
    assert mutate(store, binding, configured, "end", "end-one") == ended
    with pytest.raises(StandbyError, match="standby_revision_conflict"):
        mutate(store, binding, configured, "pause", "stale-pause")
    with pytest.raises(StandbyError, match="standby_scope_changed"):
        store.standby_snapshot(owner_id="other", binding=binding, standby_id=ended["standby_id"])
    deleted = mutate(store, binding, ended, "delete_data", "delete-one")
    assert deleted["disposal"] == "PURGED_LOCAL"
    assert deleted["cleanup"] == {"control_only": True, "external_copies": "not_created"}
    assert store.standby_recent(owner_id="user:7", binding=binding, now=1001) == []
    # Body canaries never enter control tables or a second chat history.
    with store._connection() as connection:
        assert connection.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM standby_control_operations").fetchone()[0] == 3


def test_concurrent_mutations_only_one_revision_wins(native):
    store, _, binding, _ = native
    configured = configure(store, binding)
    def update(number):
        other = SessionStore(store.db_path, instance_id="SB-TEST")
        try:
            return mutate(other, binding, configured, "pause", "pause-" + str(number))
        except StandbyError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(update, range(2)))
    assert sum(isinstance(value, dict) for value in results) == 1
    assert "standby_revision_conflict" in results


def test_status_does_not_renew_lease_and_terminal_read_cannot_restart(native):
    store, _, binding, _ = native
    first = configure(store, binding)
    for _ in range(3):
        assert store.standby_snapshot(owner_id="user:7", binding=binding, standby_id=first["standby_id"]) == first
    with pytest.raises(StandbyError, match="standby_operation_expired"):
        store.standby_control(
            owner_id="user:7", body={"operation":"configure","operation_id":"old",
            "issued_at":1000,"binding":binding,"profile_id":"quiet","profile_revision":"a"*64}, now=1401)
    with pytest.raises(StandbyError, match="standby_request_invalid"):
        store.standby_control(owner_id="user:7", body={"operation":"configure",
            "operation_id":"media","issued_at":1000,"binding":binding,
            "profile_id":"quiet","profile_revision":"a"*64,"audio":"SB_CANARY"}, now=1000)



def test_profile_capacity_failure_never_publishes_partial_document(tmp_path, monkeypatch):
    from orchestrator.standby import config
    path = tmp_path / "bounded.json"
    monkeypatch.setattr(config, "MAX_CONFIG_BYTES", 128)
    with pytest.raises(StandbyError, match="standby_capacity_reached"):
        StandbyProfiles(path).save("user:7", "alice", "quiet", profile(), expected_revision=None)
    assert not path.exists()


async def test_readiness_never_uses_fake_call_or_unqualified_native_model(native):
    store, config, binding, saved = native
    seen = []
    class Ports:
        def validate(self, owner, requested):
            assert owner == "user:7" and requested == binding
        def privacy_level(self, agent):
            seen.append("privacy")
            return 2
    service = StandbyService(store, config, Ports(), now=lambda: 1000)
    preflight = await service.invoke("user:7", {"operation": "preflight", "binding": binding, "profile_id": "quiet"})
    assert preflight["ready"] is False
    assert {"standby_media_unqualified", "standby_judge_unqualified", "standby_engine_retention_unqualified"} <= set(preflight["blockers"])
    assert preflight["capture_started"] is False and preflight["privacy_level"] == 2
    for operation in ("start_shadow", "arm", "resume", "ingest_audio", "ingest_frame", "finish_notes", "promote_artifact"):
        with pytest.raises(StandbyError, match="standby_capability_unqualified"):
            await service.invoke("user:7", {"operation": operation, "binding": binding, "profile_id": "quiet"})
    assert seen == ["privacy"]
    assert store.standby_recent(owner_id="user:7", binding=binding, now=1000) == []


async def test_configuration_then_stop_is_available_when_profile_file_breaks(native):
    store, config, binding, saved = native
    class Ports:
        def validate(self, owner, binding):
            raise AssertionError("stop must not depend on a running Agent")
        def privacy_level(self, agent):
            raise AssertionError("stop must not depend on model/privacy availability")
    first = configure(store, binding)
    config.path.write_text("{broken", encoding="utf-8")
    service = StandbyService(store, config, Ports(), now=lambda: 1000)
    response = await service.invoke("user:7", {"operation":"end","operation_id":"end-off",
        "issued_at":1000,"binding":binding,"standby_id":first["standby_id"],
        "expected_revision":first["revision"],"epoch":first["epoch"]})
    assert response["session"]["lifecycle"] == "STOPPED"
