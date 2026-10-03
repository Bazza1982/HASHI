from __future__ import annotations

from types import MethodType, SimpleNamespace

import pytest

from orchestrator.admin_local_testing import execute_local_command
from orchestrator.command_interaction_bridge import dispatch_command_interaction
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator.session_store import SessionStore


def _operation(index: int, mode: str) -> dict:
    return {
        "operation_id": f"{index:032x}",
        "action": "reboot",
        "mode": mode,
        "status": "accepted",
        "terminal": False,
        "latest_sequence": 1,
    }


class _Orchestrator:
    def __init__(self):
        self.requests: list[dict] = []
        self.runtimes = []

    async def request_reboot(self, **request):
        self.requests.append(request)
        operation = _operation(len(self.requests), str(request["mode"]))
        return {
            "accepted": True,
            "duplicate": False,
            "reason": "accepted",
            "record": {
                "id": operation["operation_id"],
                "status": "accepted",
            },
            "operation": operation,
        }

    async def reboot_status(self, **_origin):
        return None

    def configured_agent_names(self):
        return ["zelda"]


def _runtime(tmp_path):
    orchestrator = _Orchestrator()
    runtime = SimpleNamespace(
        name="zelda",
        orchestrator=orchestrator,
        workspace_dir=tmp_path / "workspaces" / "zelda",
        global_config=SimpleNamespace(
            authorized_id=42,
            bridge_home=tmp_path,
            project_root=tmp_path,
            instance_id="TEST",
            ui_language="en",
        ),
        _is_authorized_user=lambda actor: actor == 42,
        _is_command_allowed=lambda command: command == "reboot",
    )
    runtime.workspace_dir.mkdir(parents=True)

    async def reply_text(_self, update, text, **kwargs):
        return await update.message.reply_text(text, **kwargs)

    runtime._reply_text = MethodType(reply_text, runtime)
    runtime.cmd_reboot = MethodType(FlexibleAgentRuntime.cmd_reboot, runtime)
    runtime.callback_toggle = MethodType(
        FlexibleAgentRuntime.callback_toggle, runtime
    )
    runtime._wrap_callback = lambda _kind, callback: callback
    return runtime, orchestrator


def _metadata(session_id: str = "sessionabcdefghijkl") -> dict:
    return {
        "actor_id": 42,
        "owner_id": "user:42",
        "instance_id": "TEST",
        "session_id": session_id,
        "context_generation": 1,
        "session_surface": "workbench",
        "session_channel_key": "default",
        "connection_binding": "bindingabcdefghijkl",
        "connector_id": "workbench",
        "source_channel": "workbench_command_ui",
    }


def _open(command: str, request_id: str) -> dict:
    return {
        "version": 1,
        "op": "open",
        "client_id": "clientabcdefghijkl",
        "request_id": request_id,
        "ui_locale": "en",
        "command": command,
    }


@pytest.mark.asyncio
async def test_legacy_command_response_returns_same_pao_reboot_projection(tmp_path):
    runtime, orchestrator = _runtime(tmp_path)

    response = await execute_local_command(
        runtime,
        "/reboot max",
        source_channel="workbench_api",
        session_metadata={"_hashi_owner_id": "user:42"},
    )

    assert response["ok"] is True
    assert response["result"]["operation"] == _operation(1, "max")
    assert response["result"]["record"]["id"] == _operation(1, "max")[
        "operation_id"
    ]
    assert orchestrator.requests[0]["origin"]["owner_id"] == "user:42"


@pytest.mark.asyncio
async def test_typed_open_persists_and_replays_same_reboot_operation(tmp_path):
    runtime, orchestrator = _runtime(tmp_path)
    store = SessionStore(tmp_path / "sessions.sqlite3", instance_id="TEST")
    session = store.create_session(
        owner_id="user:42", agent_id="zelda", is_default=True
    )
    runtime.session_store = store
    metadata = {
        **_metadata(session["session_id"]),
        "_durable_command_invocation": True,
    }
    request = _open("/reboot max", "requestabcdefghijkl")

    first = await dispatch_command_interaction(runtime, request, metadata)
    replay = await dispatch_command_interaction(runtime, request, metadata)

    assert first["ok"] is True
    assert first["result"]["operation"] == _operation(1, "max")
    assert replay["result"]["operation"] == first["result"]["operation"]
    assert replay["replayed"] is True
    assert len(orchestrator.requests) == 1
    events = store.events(session["session_id"], owner_id="user:42")
    [event] = [row for row in events if row["kind"] == "frontend.command_result"]
    assert event["detail"]["result"]["result"]["operation"] == _operation(
        1, "max"
    )


@pytest.mark.asyncio
async def test_typed_reboot_menu_action_returns_callback_operation(tmp_path):
    runtime, orchestrator = _runtime(tmp_path)
    metadata = _metadata()
    opened = await dispatch_command_interaction(
        runtime,
        _open("/reboot", "requestmenuabcdefghi"),
        metadata,
    )
    menu = opened["messages"][0]["command_ui"]
    internal = runtime._command_interaction_store.menus[menu["menu_id"]]
    button_id = next(
        key
        for key, (data, _fingerprint) in internal.actions.items()
        if data == "tgl:reboot:max"
    )

    result = await dispatch_command_interaction(
        runtime,
        {
            "version": 1,
            "op": "act",
            "client_id": "clientabcdefghijkl",
            "request_id": "requestactionabcdef",
            "ui_locale": "en",
            "menu_id": menu["menu_id"],
            "revision": menu["revision"],
            "button_id": button_id,
        },
        metadata,
    )

    assert result["ok"] is True
    assert result["result"]["operation"] == _operation(1, "max")
    assert len(orchestrator.requests) == 1
