from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from orchestrator.config_json import write_config_json
from orchestrator.personal_data_owner import (
    read_personal_data_binding,
    read_personal_data_owner_for_config,
    resolve_personal_data_owner,
)


def test_read_only_fallback_does_not_establish_binding(tmp_path):
    assert resolve_personal_data_owner(tmp_path, {}, None) == "user:0"
    assert not (tmp_path / "state" / "personal_data_owner.json").exists()


def test_concurrent_first_establishment_has_one_stable_owner(tmp_path):
    barrier = Barrier(8)

    def establish(actor):
        barrier.wait()
        return resolve_personal_data_owner(tmp_path, {}, actor, establish=True)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(establish, range(1, 9)))
    assert len(set(results)) == 1
    assert read_personal_data_binding(tmp_path) == (results[0], True)
    assert resolve_personal_data_owner(tmp_path, {}, 99) == results[0]


def test_personal_binding_is_not_applied_to_governed_profile(tmp_path):
    resolve_personal_data_owner(tmp_path, {}, 42, establish=True)
    assert read_personal_data_owner_for_config(tmp_path, {"deployment_profile": "enterprise"}) is None


def test_malformed_binding_does_not_silently_adopt_a_different_owner(tmp_path):
    path = tmp_path / "state" / "personal_data_owner.json"
    path.parent.mkdir()
    write_config_json(path, {"version": 1, "data_owner_id": "", "legacy_owner_known": False})
    with pytest.raises(ValueError, match="binding is invalid"):
        resolve_personal_data_owner(tmp_path, {"owner_scope_id": "another-owner"}, 99)


def test_legacy_projection_never_overwrites_existing_owner(tmp_path):
    from orchestrator.config_json import read_config_json
    from orchestrator.personal_data_owner import project_legacy_data_owner

    config_path = tmp_path / "agents.json"
    write_config_json(config_path, {
        "global": {"owner_scope_id": "pre-existing-owner"}, "agents": []
    })
    before = config_path.read_bytes()
    assert not project_legacy_data_owner(config_path, "different-owner")
    assert config_path.read_bytes() == before
    assert read_config_json(config_path)["global"]["owner_scope_id"] == "pre-existing-owner"


def test_legacy_projection_retries_revision_conflict_without_overwriting_other_fields(tmp_path, monkeypatch):
    from orchestrator import agent_config_json
    from orchestrator.config_json import ConfigConflictError, read_config_json
    from orchestrator.personal_data_owner import project_legacy_data_owner

    config_path = tmp_path / "agents.json"
    write_config_json(config_path, {"global": {}, "agents": []})
    original = agent_config_json.write_agent_config_json
    attempts = []

    def concurrent_change(path, document):
        attempts.append(1)
        if len(attempts) == 1:
            changed = read_config_json(path)
            changed["global"]["unrelated"] = "preserve"
            original(path, changed)
            raise ConfigConflictError("simulated concurrent write")
        return original(path, document)

    monkeypatch.setattr(agent_config_json, "write_agent_config_json", concurrent_change)
    assert project_legacy_data_owner(config_path, "owner:local")
    saved = read_config_json(config_path)
    assert saved["global"] == {"unrelated": "preserve", "owner_scope_id": "owner:local"}
    assert len(attempts) == 2
