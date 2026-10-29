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
    from orchestrator.session_store import SessionStore

    monkeypatch.setattr(registry, "DEFAULT_PRIVATE_COMMAND_DIR", tmp_path / "no-private")
    monkeypatch.setenv("HASHI_PRIVATE_COMMAND_DIRS", "")
    registry.invalidate_runtime_registry_cache()
    runtime, path = runtime_for(tmp_path, [{"name": "agent"}])
    before = path.read_bytes()
    runtime.session_store = SessionStore(tmp_path / "state" / "sessions.sqlite3", instance_id="TEST")
    session = runtime.session_store.create_session(owner_id="user:7", agent_id="agent")
    metadata = {
        "actor_id": 7, "instance_id": "TEST", "session_id": session["session_id"],
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
    rows = runtime.session_store.messages(session["session_id"], owner_id="user:7")
    assert any(row["text"] == result["messages"][-1]["text"] for row in rows)
    assert all(not row["history_eligible"] for row in rows)
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_shared_request_views_render_current_scope_and_do_not_grant(tmp_path, monkeypatch):
    from dataclasses import replace
    import orchestrator.command_registry as registry
    from orchestrator.permissions.authority import PermissionAuthority
    from orchestrator.permissions.models import ExecutionContext, normalize_scope
    from orchestrator.session_store import SessionStore

    class NoApprovals:
        def verify(self, reference):
            return None

    monkeypatch.setattr(registry, "DEFAULT_PRIVATE_COMMAND_DIR", tmp_path / "no-private")
    monkeypatch.setenv("HASHI_PRIVATE_COMMAND_DIRS", "")
    registry.invalidate_runtime_registry_cache()
    runtime, path = runtime_for(tmp_path, [{"name": "agent"}])
    before = path.read_bytes()
    runtime.session_store = SessionStore(tmp_path / "sessions.sqlite", instance_id="TEST")
    session = runtime.session_store.create_session(owner_id="user:7", agent_id="agent")
    runtime.permission_authority = PermissionAuthority(tmp_path / "permissions.sqlite", verifier=NoApprovals())
    context = ExecutionContext(
        instance_id="TEST", owner_id="user:7", agent_id="agent", task_id="task",
        run_id="run", session_id=session["session_id"], turn_id="turn",
        worker_generation="worker", context_generation="1", execution_epoch="epoch",
        backend_id="her-v3", executor_id="files", policy_revision=1,
    )
    runtime.permission_authority.open_task(context)
    scope = normalize_scope("file", str(tmp_path / "report&.txt"), ["read"], cwd=tmp_path)
    request = runtime.permission_authority.request_access(
        context, [scope], purpose="<script>not markup</script>",
    )
    other = replace(context, owner_id="user:8", task_id="other", execution_epoch="other")
    runtime.permission_authority.open_task(other)
    hidden = runtime.permission_authority.request_access(other, [scope], purpose="private other user")
    metadata = {
        "actor_id": 7, "instance_id": "TEST", "session_id": session["session_id"],
        "context_generation": 1, "session_surface": "workbench", "session_channel_key": "default",
        "connection_binding": "bindingabcdefghijk", "owner_id": "user:7",
    }
    for index, command in enumerate(("/safety requests", f"/safety request {request.request_id}")):
        result = await dispatch_command_interaction(runtime, {
            "version": 1, "op": "open", "command": command, "ui_locale": "zh-CN",
            "client_id": "clientabcdefghijkl", "request_id": f"requestabcdefghijk{index}",
        }, metadata)
        assert result["ok"], result
        text = result["messages"][-1]["text"]
        assert request.request_id in text and hidden.request_id not in text
        assert "不会授予权限" in text
        if index:
            assert "<script>" not in text and "&lt;script&gt;" in text
            assert "report&amp;.txt" in text and "读取" in text
        assert not runtime.permission_authority.evaluate(context, [scope])
    rows = runtime.session_store.messages(session["session_id"], owner_id="user:7")
    assert any(request.request_id in row["text"] for row in rows)
    assert all(not row["history_eligible"] for row in rows)
    assert path.read_bytes() == before

    withdrawn = await dispatch_command_interaction(runtime, {
        "version": 1, "op": "open",
        "command": f"/safety withdraw {request.request_id} --revision 1",
        "ui_locale": "zh-CN", "client_id": "clientabcdefghijkl",
        "request_id": "requestwithdrawabcdef",
    }, metadata)
    assert withdrawn["ok"], withdrawn
    assert "已撤回" in withdrawn["messages"][-1]["text"]
    assert runtime.permission_authority.get_request(context, request.request_id).state == "withdrawn"
    assert runtime.permission_authority.decision_count(request.request_id) == 0

    # No fallback to the current owner's status, and no details on a stale page.
    sent = []
    update = update_for(7, sent)
    update._hashi_session_surface = "workbench"
    update._hashi_session_channel_key = "default"
    update._hashi_session_owner_id = "user:7"
    update._hashi_session_id = session["session_id"]
    update._hashi_session_context_generation = 2
    await safety_command(runtime, update, SimpleNamespace(args=["request", request.request_id]))
    assert request.request_id not in sent[0][0] and "unavailable" in sent[0][0].lower()
