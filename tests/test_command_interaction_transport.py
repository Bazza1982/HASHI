"""Worker-owned command menu transport over the existing runtime.slash RPC."""
from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from orchestrator.admin_local_testing import (
    execute_local_command,
    try_execute_slash_command_text,
)
from orchestrator.command_interaction_transport import TRANSPORT_PREFIX
from orchestrator.function_generation import build_source_manifest

ROOT = Path(__file__).resolve().parents[1]


def _wire(op: str = "catalogue", **extra) -> str:
    payload = {
        "version": 1,
        "op": op,
        "client_id": "clientabcdefghijk",
        "request_id": "requestabcdefghijkl",
        "ui_locale": "zh-CN",
        "connection_binding": "bindingabcdefghijk",
        **extra,
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return TRANSPORT_PREFIX + encoded


def _runtime():
    return NS(
        name="agent",
        global_config=NS(
            authorized_id=7,
            instance_id="HASHI1",
            deployment_profile="personal",
        ),
        _is_authorized_user=lambda actor: actor == 7,
    )


@pytest.mark.asyncio
async def test_command_transport_targets_selected_session_and_rejects_stale_generation(monkeypatch):
    observed = []
    resolved = []

    def current(runtime, **kwargs):
        resolved.append(kwargs)
        return {"session_id": "session-selected-b", "context_generation": 4}

    async def dispatch(runtime, payload, metadata):
        observed.append(metadata)
        return {"ok": True, "command_ui_version": 2, "messages": []}

    monkeypatch.setattr("orchestrator.runtime_session.current_session", current)
    monkeypatch.setattr("orchestrator.command_interaction_bridge.dispatch_command_interaction", dispatch)
    result = await try_execute_slash_command_text(
        _runtime(), _wire("open", command="/stop", session_id="session-selected-b", context_generation=4),
        source_channel="workbench_api",
    )
    assert result["ok"] is True
    assert resolved[0]["explicit_session_id"] == "session-selected-b"
    assert observed[0]["session_id"] == "session-selected-b"
    result = await try_execute_slash_command_text(
        _runtime(), _wire("open", command="/stop", session_id="session-selected-b", context_generation=3),
        source_channel="workbench_api",
    )
    assert result["ok"] is False
    assert len(observed) == 1


@pytest.mark.asyncio
async def test_workbench_owner_does_not_require_a_primary_telegram_user(tmp_path, monkeypatch):
    from types import MethodType
    from orchestrator.config_json import write_config_json
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
    from orchestrator.telegram_access import TelegramAccessService

    config_path, secrets_path = tmp_path / "agents.json", tmp_path / "secrets.json"
    write_config_json(config_path, {
        "global": {"authorized_id": 0, "owner_scope_id": "user:0"},
        "agents": [{"name": "agent"}],
    })
    write_config_json(secrets_path, {})
    runtime = _runtime()
    runtime.global_config.authorized_id = 0
    runtime.global_config.owner_scope_id = "user:0"
    runtime.global_config.config_path = config_path
    runtime.global_config.secrets_path = secrets_path
    runtime._authorized_telegram_ids = (0,)
    runtime._is_authorized_user = MethodType(FlexibleAgentRuntime._is_authorized_user, runtime)
    users = TelegramAccessService(config_path=config_path, secrets_path=secrets_path)
    state = users.status("agent")
    users.change("agent", expected_revision=state["revision"],
                 scope="global", action="add", user_id="7")
    assert users.status("agent")["primary_user_id"] is None
    monkeypatch.setattr("orchestrator.command_interaction_bridge._catalogue",
                        lambda _runtime, _locale: {"ok": True, "commands": [{"name": "reboot"}]})

    result = await try_execute_slash_command_text(
        runtime, _wire(), source_channel="workbench_api",
        session_metadata={"_hashi_owner_id": "user:0"},
    )
    assert result["ok"] is True
    assert runtime.global_config.authorized_id == 0
    assert runtime.global_config.owner_scope_id == "user:0"

    config_path.write_text("{broken", encoding="utf-8")
    independent = await try_execute_slash_command_text(
        runtime, _wire(), source_channel="workbench_api",
        session_metadata={"_hashi_owner_id": "user:0"},
    )
    assert independent["ok"] is True
    # A real Telegram actor still needs a readable current policy.
    assert runtime._is_authorized_user(7) is False
    denied = await try_execute_slash_command_text(runtime, _wire(), source_channel="workbench_api")
    assert denied["ok"] is False
    assert denied["error_code"] == "command_menu_forbidden"


@pytest.mark.asyncio
async def test_authenticated_workbench_owner_can_list_sessions_without_telegram(tmp_path):
    from types import MethodType
    from orchestrator import runtime_session
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
    from orchestrator.session_store import SessionStore

    runtime = _runtime()
    runtime.global_config.authorized_id = 0
    runtime.global_config.owner_scope_id = "user:0"
    runtime._authorized_telegram_ids = ()
    runtime._is_authorized_user = MethodType(FlexibleAgentRuntime._is_authorized_user, runtime)
    runtime.workspace_dir = tmp_path / "workspace"
    runtime.session_store = SessionStore(tmp_path / "state/sessions.sqlite3", instance_id="HASHI1")
    session = runtime.session_store.resolve_primary_session(
        owner_id="user:0", agent_id="agent", establish=True
    )
    runtime.cmd_sessions = MethodType(runtime_session.cmd_sessions, runtime)

    async def reply(update, text, **kwargs):
        return await update.message.reply_text(text, **kwargs)

    runtime._reply_text = reply
    result = await execute_local_command(
        runtime, "/sessions", source_channel="workbench_api",
        session_metadata={
            "_hashi_owner_id": "user:0", "session_surface": "workbench",
            "session_channel_key": "default", "session_id": session["session_id"],
            "context_generation": session["context_generation"],
        },
    )
    assert result["ok"] is True
    assert session["session_id"] in str(result["messages"])


@pytest.mark.asyncio
async def test_frontend_principal_is_bound_to_runtime_and_command_lifetime():
    import asyncio
    from types import MethodType

    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
    from orchestrator.frontend_command_context import bind_frontend_command_principal

    runtime = _runtime()
    other_runtime = _runtime()
    for item in (runtime, other_runtime):
        item._authorized_telegram_ids = (7,)
        item._is_authorized_user = MethodType(FlexibleAgentRuntime._is_authorized_user, item)

    release_child = asyncio.Event()

    async def inherited_task():
        await release_child.wait()
        return runtime._is_authorized_user("local:alice")

    assert not runtime._is_authorized_user("local:alice")
    with bind_frontend_command_principal(runtime, "local:alice"):
        assert runtime._is_authorized_user("local:alice")
        assert not runtime._is_authorized_user("local:bob")
        assert not other_runtime._is_authorized_user("local:alice")
        assert runtime._is_authorized_user(7)
        assert not runtime._is_authorized_user(8)
        child = asyncio.create_task(inherited_task())
    release_child.set()
    assert not await child
    assert not runtime._is_authorized_user("local:alice")


@pytest.mark.asyncio
async def test_authenticated_workbench_menu_does_not_refresh_telegram_policy(monkeypatch):
    observed = []

    async def dispatch(runtime, payload, metadata):
        observed.append(metadata)
        return {"ok": True, "commands": []}

    monkeypatch.setattr(
        "orchestrator.command_interaction_bridge.dispatch_command_interaction", dispatch
    )
    monkeypatch.setattr(
        "orchestrator.telegram_actor_scope.refresh_primary_user",
        lambda _runtime: pytest.fail("a frontend command must not require Telegram"),
    )
    result = await try_execute_slash_command_text(
        _runtime(), _wire(), source_channel="workbench_api",
        session_metadata={"_hashi_owner_id": "local:alice"},
    )
    assert result["ok"] is True
    assert observed[0]["actor_id"] == "local:alice"
    assert observed[0]["owner_id"] == "local:alice"


@pytest.mark.asyncio
async def test_version_command_returns_to_frontend_with_nonnumeric_owner(tmp_path, monkeypatch):
    from types import MethodType
    from unittest.mock import AsyncMock

    from orchestrator import runtime_version
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
    from orchestrator.session_store import SessionStore

    runtime = _runtime()
    runtime._authorized_telegram_ids = ()
    runtime._is_authorized_user = MethodType(FlexibleAgentRuntime._is_authorized_user, runtime)
    runtime.workspace_dir = tmp_path / "workspace"
    runtime.session_store = SessionStore(tmp_path / "state/sessions.sqlite3", instance_id="HASHI1")
    session = runtime.session_store.resolve_primary_session(
        owner_id="local:alice", agent_id="agent", establish=True
    )
    runtime.cmd_version = MethodType(runtime_version.command, runtime)
    runtime.send_long_message = AsyncMock(side_effect=AssertionError("must use frontend capture"))
    monkeypatch.setattr(runtime_version, "collect_version_payload", lambda _runtime: {})
    monkeypatch.setattr(runtime_version, "render_version_card", lambda *_args, **_kwargs: "version")

    result = await execute_local_command(
        runtime, "/version", source_channel="workbench_api",
        session_metadata={"_hashi_owner_id": "local:alice", "session_surface": "workbench",
                          "session_channel_key": "default", "session_id": session["session_id"]},
    )
    assert result["ok"] is True, result
    assert "version" in str(result["messages"])
    runtime.send_long_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_runtime_slash_entry_dispatches_menu_inside_worker(monkeypatch):
    observed = []

    async def dispatch(runtime, payload, metadata):
        observed.append((runtime, payload, metadata))
        return {"ok": True, "command_ui_version": 1, "commands": []}

    monkeypatch.setattr(
        "orchestrator.command_interaction_bridge.dispatch_command_interaction",
        dispatch,
    )
    result = await try_execute_slash_command_text(
        _runtime(), _wire(), source_channel="workbench_api"
    )

    assert result == {"ok": True, "command_ui_version": 1, "commands": []}
    assert observed[0][1]["op"] == "catalogue"
    assert observed[0][2] == {
        "actor_id": 7,
        "instance_id": "HASHI1",
        "session_surface": "workbench",
        "session_channel_key": "default",
        "connection_binding": "bindingabcdefghijk",
    }


@pytest.mark.asyncio
async def test_long_sys_command_crosses_workbench_transport_losslessly(monkeypatch):
    observed = []
    prompt = "word  " * 6000 + "\nfinal line"
    command = f"/sys 5 save {prompt}"

    async def dispatch(runtime, payload, metadata):
        observed.append(payload["command"])
        return {"ok": True, "command_ui_version": 2, "messages": []}

    monkeypatch.setattr(
        "orchestrator.command_interaction_bridge.dispatch_command_interaction",
        dispatch,
    )
    result = await try_execute_slash_command_text(
        _runtime(), _wire("open", command=command), source_channel="workbench_api"
    )

    assert result["ok"] is True
    assert observed == [command]


@pytest.mark.asyncio
async def test_unchanged_shared_command_path_routes_to_rebooted_worker(monkeypatch):
    worker = _runtime()

    async def dispatch(runtime, payload, metadata):
        return {"ok": True, "command_ui_version": 1, "commands": [payload["op"]]}

    monkeypatch.setattr(
        "orchestrator.command_interaction_bridge.dispatch_command_interaction",
        dispatch,
    )

    class ExistingProxy:
        is_function_worker_proxy = True

        async def execute_slash_command(self, text, **kwargs):
            return await try_execute_slash_command_text(worker, text, **kwargs)

    result = await execute_local_command(ExistingProxy(), _wire())

    assert result == {
        "ok": True,
        "command_ui_version": 1,
        "commands": ["catalogue"],
    }


@pytest.mark.asyncio
async def test_busy_worker_accepts_fc_command_with_canonical_session(monkeypatch):
    observed = []

    async def dispatch(runtime, payload, metadata):
        observed.append(metadata)
        return {"ok": True, "command_ui_version": 1, "messages": []}

    monkeypatch.setattr(
        "orchestrator.command_interaction_bridge.dispatch_command_interaction",
        dispatch,
    )
    monkeypatch.setattr(
        "orchestrator.runtime_session.current_session",
        lambda runtime, **kwargs: {
            "session_id": "canonical-session",
            "context_generation": 4,
        },
    )
    monkeypatch.setattr("orchestrator.runtime_session.owner_id", lambda runtime: "canonical-owner")

    runtime = _runtime()
    runtime.is_generating = True

    result = await try_execute_slash_command_text(
        runtime, _wire("open", command="/model"), source_channel="workbench_api"
    )

    assert result["ok"] is True
    assert runtime.is_generating is True
    assert observed[0]["owner_id"] == "canonical-owner"
    assert observed[0]["session_id"] == "canonical-session"
    assert observed[0]["context_generation"] == 4
    assert observed[0]["_durable_command_invocation"] is True
    assert observed[0]["connector_id"] == "workbench"
    assert observed[0]["ingress_transport"] == "workbench-command-ui"


@pytest.mark.asyncio
async def test_reserved_transport_is_rejected_outside_workbench_ingress(monkeypatch):
    called = False

    async def dispatch(*args):
        nonlocal called
        called = True

    monkeypatch.setattr(
        "orchestrator.command_interaction_bridge.dispatch_command_interaction",
        dispatch,
    )
    result = await try_execute_slash_command_text(
        _runtime(), _wire(), source_channel="telegram"
    )

    assert result["error_code"] == "command_menu_forbidden"
    assert result["http_status"] == 403
    assert called is False


@pytest.mark.asyncio
async def test_transport_rejects_forged_fields_before_dispatch(monkeypatch):
    called = False

    async def dispatch(*args):
        nonlocal called
        called = True

    monkeypatch.setattr(
        "orchestrator.command_interaction_bridge.dispatch_command_interaction",
        dispatch,
    )
    result = await try_execute_slash_command_text(
        _runtime(), _wire(actor_id=999), source_channel="workbench_api"
    )

    assert result["error_code"] == "command_menu_request_invalid"
    assert called is False


@pytest.mark.asyncio
async def test_plain_non_slash_text_keeps_existing_noop_behavior():
    assert await try_execute_slash_command_text(_runtime(), "hello") is None


def test_agent_reboot_generation_contains_complete_menu_transport():
    manifest = build_source_manifest(
        ["orchestrator.admin_local_testing"],
        code_root=ROOT,
    )
    assert {
        "orchestrator.command_interaction_transport",
        "orchestrator.command_interaction_bridge",
        "orchestrator.command_interactions",
    } <= set(manifest.module_names)
