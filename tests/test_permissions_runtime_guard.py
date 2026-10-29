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


def test_admin_update_and_runtime_agree_on_nested_level(tmp_path):
    from types import SimpleNamespace

    from orchestrator.config_admin import ConfigAdmin
    from orchestrator.permissions.models import PermissionLevel, PermissionSettings

    row = _minimal_flex_agent()
    row["extra"] = {"permission_settings": {"requested_level": "L2"}, "unrelated": "kept"}
    config_path, secrets_path = _write_base_files(tmp_path, row)
    admin = ConfigAdmin(SimpleNamespace(config_path=config_path))
    snapshot = admin.read_agent_permission_settings("flexy")
    assert snapshot.settings.requested_level is PermissionLevel.L2
    admin.write_agent_permission_settings(
        "flexy", PermissionSettings(requested_level=PermissionLevel.L1),
        expected_revision=snapshot.revision,
    )
    global_config, agents, secrets = ConfigManager(
        config_path, secrets_path, bridge_home=tmp_path,
    ).load()
    agent = agents[0]
    assert agent.permission_settings["requested_level"] == "L1"
    assert agent.extra["unrelated"] == "kept"
    manager = FlexibleBackendManager(agent, global_config, secrets)
    with pytest.raises(ValueError, match="permission"):
        manager._build_adapter_config("ollama-api", {"engine": "ollama-api", "model": "example"})


@pytest.mark.asyncio
async def test_user_selected_cli_keeps_its_own_controls_and_discloses_hashi_coverage(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    from orchestrator import runtime_menu_views, runtime_model_selection, runtime_session, ui_language
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
    from tests.test_backend_selection_transaction import make_selection

    manager, _, candidate, _ = make_selection(tmp_path, monkeypatch)
    manager.config.permission_settings = {"requested_level": "L2"}
    manager.config.permission_settings_present = True
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.name = manager.config.name
    runtime.config = manager.config
    runtime.backend_manager = manager
    runtime.global_config = manager.global_config
    runtime._evaluate_enterprise_policy = lambda *a, **kw: SimpleNamespace(allowed=True)
    runtime._backend_busy = lambda: False
    runtime._sync_workzone_to_backend_config = lambda: None
    runtime._clear_handoff_state = lambda: None
    runtime._arm_session_primer = lambda *a, **kw: None
    runtime.get_current_model = lambda: manager.current_backend.config.model
    runtime.get_current_provider = lambda: None
    runtime._get_current_effort = lambda: None
    monkeypatch.setattr(runtime_session, "current_session", lambda *a, **kw: {"session_id": "test"})
    monkeypatch.setattr(runtime_session, "apply_session_workzones", lambda *a, **kw: None)
    monkeypatch.setattr(runtime_model_selection, "schedule_current_model_metadata", lambda *a, **kw: None)
    with ui_language.language_scope(runtime, None, locale="en"):
        ok, notice = await runtime._switch_backend_mode(1, "claude-cli", "claude-haiku-4-5")
    assert ok and manager.current_backend is candidate
    assert manager.config.active_backend == "claude-cli"
    assert json.loads(manager.state_store.path.read_text(encoding="utf-8"))["active_backend"] == "claude-cli"
    assert manager.config.permission_settings == {"requested_level": "L2"}
    assert "L2" in notice and "not fully enforced by HASHI" in notice
    status = manager.permission_status()
    assert status.requested_level.value == "L2" and status.state == "unmanaged"
    assert status.backend_id == "claude-cli"
    assert "permission_settings" not in candidate.config.extra

    from orchestrator.commands.safety import safety_command

    config_path = tmp_path / "agents.json"
    config_path.write_text(json.dumps({"global": {}, "agents": [{
        "name": manager.config.name, "permission_settings": {"requested_level": "L2"},
    }]}), encoding="utf-8")
    runtime.global_config.config_path = config_path
    runtime._is_authorized_user = lambda actor: actor == 1
    messages = []

    async def reply(update, text, **kwargs):
        messages.append(text)

    runtime._reply_text = reply
    with ui_language.language_scope(runtime, None, locale="en"):
        await safety_command(
            runtime, SimpleNamespace(effective_user=SimpleNamespace(id=1)),
            SimpleNamespace(args=["status"]),
        )
    assert "not fully enforced by HASHI" in messages[0] and "claude-cli" in messages[0]

    # L0 is still a true compatibility path with no new notice.
    manager.config.permission_settings = {"requested_level": "L0"}
    assert manager.permission_status() is None
    plain = runtime_menu_views.backend_switch_notice_text(
        backend="claude-cli", model="example", mode="fixed", provider=None,
        effort=None, with_context=False,
    )
    assert "L2" not in plain and "not fully enforced" not in plain
