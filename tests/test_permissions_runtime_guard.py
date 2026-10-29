"""An unsupported permission setting must never start as legacy L0."""
import pytest

from orchestrator.config import ConfigManager
from orchestrator.flexible_backend_manager import FlexibleBackendManager
from tests.test_config import _minimal_flex_agent, _write_base_files


def _load(tmp_path, value, *, extra=None, present=True):
    agent = _minimal_flex_agent()
    if present:
        agent["permission_settings"] = value
    if extra is not None:
        agent["extra"] = extra
    config_path, secrets_path = _write_base_files(tmp_path, agent)
    global_config, agents, secrets = ConfigManager(
        config_path, secrets_path, bridge_home=tmp_path
    ).load()
    return global_config, agents[0], secrets


@pytest.mark.parametrize("level", ["L1", "L2", "invalid", None])
def test_explicit_unavailable_or_invalid_level_cannot_start_an_unmanaged_backend(tmp_path, level):
    value = None if level is None else {"requested_level": level}
    global_config, agent, secrets = _load(tmp_path, value, extra={"unrelated": "kept"})
    assert agent.extra["unrelated"] == "kept"
    assert agent.permission_settings_present
    manager = FlexibleBackendManager(agent, global_config, secrets)
    with pytest.raises(ValueError, match="permission"):
        manager._build_adapter_config("ollama-api", {"engine": "ollama-api", "model": "example"})


@pytest.mark.asyncio
async def test_unavailable_level_cannot_initialize_a_real_or_fallback_backend(tmp_path, monkeypatch):
    global_config, agent, secrets = _load(tmp_path, {"requested_level": "L1"})
    manager = FlexibleBackendManager(agent, global_config, secrets)

    def forbidden_backend(_engine):
        raise AssertionError("an unprotected backend must not be constructed")

    monkeypatch.setattr("adapters.registry.get_backend_class", forbidden_backend)
    assert await manager._create_initialized_backend("ollama-api", target_model="example") is None
    with pytest.raises(ValueError, match="permission"):
        manager.create_ephemeral_backend("ollama-api", target_model="example")


def test_legacy_and_explicit_l0_still_build_the_same_backend_config(tmp_path):
    left = tmp_path / "legacy"
    right = tmp_path / "explicit"
    left.mkdir()
    right.mkdir()
    global_one, legacy, secret_one = _load(left, None, present=False)
    global_two, explicit, secret_two = _load(right, {"requested_level": "L0"})
    first = FlexibleBackendManager(legacy, global_one, secret_one)._build_adapter_config(
        "ollama-api", {"engine": "ollama-api", "model": "example"}
    )
    second = FlexibleBackendManager(explicit, global_two, secret_two)._build_adapter_config(
        "ollama-api", {"engine": "ollama-api", "model": "example"}
    )
    assert first.engine == second.engine
    assert first.model == second.model
    assert first.extra == second.extra


def test_nested_setting_cannot_be_hidden_by_other_extra_fields(tmp_path):
    global_config, agent, secrets = _load(
        tmp_path, None, present=False,
        extra={"permission_settings": {"requested_level": "L1"}, "unrelated": True},
    )
    manager = FlexibleBackendManager(agent, global_config, secrets)
    with pytest.raises(ValueError, match="permission"):
        manager._build_adapter_config("ollama-api", {"engine": "ollama-api", "model": "example"})


def test_settings_cannot_hide_in_direct_or_backend_configuration(tmp_path):
    global_config, agent, secrets = _load(tmp_path, None, present=False)
    agent.extra = {"permission_settings": {"requested_level": "L1"}}
    manager = FlexibleBackendManager(agent, global_config, secrets)
    with pytest.raises(ValueError, match="permission"):
        manager._build_adapter_config("ollama-api", {"engine": "ollama-api", "model": "example"})

    agent.extra = None
    with pytest.raises(ValueError, match="permission"):
        manager._build_adapter_config(
            "ollama-api",
            {"engine": "ollama-api", "model": "example",
             "permission_settings": {"requested_level": "L1"}},
        )


def test_conflicting_duplicate_settings_fail_closed(tmp_path):
    global_config, agent, secrets = _load(
        tmp_path, {"requested_level": "L0"},
        extra={"permission_settings": {"requested_level": "L1"}},
    )
    manager = FlexibleBackendManager(agent, global_config, secrets)
    with pytest.raises(ValueError, match="permission"):
        manager._build_adapter_config("ollama-api", {"engine": "ollama-api", "model": "example"})
