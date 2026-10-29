"""Read-only permission status must not create authority or approve requests."""
import json
from types import SimpleNamespace

import pytest

from orchestrator import ui_language
from orchestrator.command_interaction_bridge import dispatch_command_interaction
from orchestrator.command_registry import load_runtime_commands
from orchestrator.commands.safety import safety_command


def runtime_for(tmp_path, agents):
    path = tmp_path / "agents.json"
    path.write_text(json.dumps({"global": {}, "agents": agents}), encoding="utf-8")

    async def reply(update, text, **kwargs):
        await update.message.reply_text(text, **kwargs)

    runtime = SimpleNamespace(
        name="agent",
        global_config=SimpleNamespace(
            config_path=path, bridge_home=tmp_path, project_root=tmp_path,
            authorized_id=7, instance_id="TEST", deployment_profile="personal",
        ),
        _is_authorized_user=lambda user: user == 7,
        _is_command_allowed=lambda name: name == "safety",
        _reply_text=reply,
    )
    return runtime, path


def update_for(actor, sent):
    async def reply(text, **kwargs):
        sent.append((text, kwargs))

    return SimpleNamespace(effective_user=SimpleNamespace(id=actor), message=SimpleNamespace(reply_text=reply))


@pytest.mark.asyncio
@pytest.mark.parametrize("locale", ["en", "zh-CN"])
@pytest.mark.parametrize("settings,level", [(None, "L0"), ({"requested_level": "L1"}, "L1"),
                                            ({"requested_level": "L2"}, "L2")])
async def test_status_reads_config_without_claiming_controls_or_writing(tmp_path, settings, level, locale):
    row = {"name": "agent"}
    if settings is not None:
        row["extra"] = {"permission_settings": settings}
    runtime, path = runtime_for(tmp_path, [row])
    before = path.read_bytes()
    sent = []
    with ui_language.language_scope(runtime, update_for(7, sent), locale=locale):
        await safety_command(runtime, update_for(7, sent), SimpleNamespace(args=[]))
    assert len(sent) == 1
    assert f"<code>{level}</code>" in sent[0][0]
    assert ("not verified" if locale == "en" else "尚未验证") in sent[0][0]
    assert path.read_bytes() == before
    assert sorted(item.name for item in tmp_path.iterdir()) == ["agents.json"]


@pytest.mark.asyncio
async def test_unknown_or_conflicting_settings_are_not_reported_as_l0(tmp_path):
    runtime, path = runtime_for(tmp_path, [{
        "name": "agent", "permission_settings": {"requested_level": "L0"},
        "extra": {"permission_settings": {"requested_level": "L2"}},
    }])
    before = path.read_bytes()
    sent = []
    await safety_command(runtime, update_for(7, sent), SimpleNamespace(args=["status"]))
    assert "unavailable" in sent[0][0].lower()
    assert "<code>L0</code>" not in sent[0][0]
    assert path.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [
    ["approve", "request", "once", "--revision", "1"],
    ["set-level", "L0"],
    ["deny", "request", "--revision", "1"],
])
async def test_mutating_commands_are_unavailable_and_cannot_change_configuration(tmp_path, args):
    runtime, path = runtime_for(tmp_path, [{"name": "agent", "permission_settings": {"requested_level": "L2"}}])
    before = path.read_bytes()
    sent = []
    await safety_command(runtime, update_for(7, sent), SimpleNamespace(args=args))
    assert "unavailable" in sent[0][0].lower()
    assert "no permissions" in sent[0][0].lower()
    assert path.read_bytes() == before
    assert sorted(item.name for item in tmp_path.iterdir()) == ["agents.json"]


@pytest.mark.asyncio
async def test_status_does_not_disclose_configuration_to_an_unapproved_actor(tmp_path):
    runtime, path = runtime_for(tmp_path, [{"name": "agent", "permission_settings": {"requested_level": "L2"}}])
    before = path.read_bytes()
    sent = []
    await safety_command(runtime, update_for(8, sent), SimpleNamespace(args=[]))
    assert sent == []
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_shared_frontend_command_projection_uses_existing_contract(tmp_path, monkeypatch):
    import orchestrator.command_registry as registry

    monkeypatch.setattr(registry, "DEFAULT_PRIVATE_COMMAND_DIR", tmp_path / "no-private")
    monkeypatch.setenv("HASHI_PRIVATE_COMMAND_DIRS", "")
    registry.invalidate_runtime_registry_cache()
    runtime, path = runtime_for(tmp_path, [{"name": "agent"}])
    before = path.read_bytes()
    metadata = {
        "actor_id": 7, "instance_id": "TEST", "session_id": "sessionabcdefghijkl",
        "context_generation": 1, "session_surface": "workbench", "session_channel_key": "default",
        "connection_binding": "bindingabcdefghijk", "owner_id": "user:7",
    }
    operation = {
        "version": 1, "op": "open", "command": "/safety", "ui_locale": "zh-CN",
        "client_id": "clientabcdefghijkl", "request_id": "requestabcdefghijk",
    }
    assert "safety" in {command.name for command in load_runtime_commands()}
    result = await dispatch_command_interaction(runtime, operation, metadata)
    assert result["ok"], result
    assert "权限" in result["messages"][-1]["text"]
    assert "<code>L0</code>" in result["messages"][-1]["text"]
    assert "尚未验证" in result["messages"][-1]["text"]
    assert path.read_bytes() == before
