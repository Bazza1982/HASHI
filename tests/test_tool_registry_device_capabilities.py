from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.capability_broker import CapabilityUnavailableError
from tools.registry import ToolRegistry
from tools.device_control_worker import DeviceWorkerState


class _CapabilityFacade:
    is_function_worker_facade = True

    def __init__(self, result="ok", *, capabilities=None) -> None:
        self.calls = []
        self.result = result
        self.capabilities = {
            "protocol_version": 1,
            "instance_id": "HASHI1",
            "capabilities": list(capabilities or []),
            "leases": [],
        }

    def capability_status(self):
        return self.capabilities

    async def invoke_capability(self, kind, action, args, **kwargs):
        self.calls.append((kind, action, args, kwargs))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class _ReplayCheckingFacade(_CapabilityFacade):
    """Exercise the sender against the Device Worker's real replay boundary."""

    def __init__(self, tmp_path):
        super().__init__(capabilities=[_browser_registration(actions=("active_tab",))])
        self.worker = DeviceWorkerState(
            bridge_home=tmp_path,
            capability_kind="browser_control",
            instance_id="HASHI1",
            device_id="test-device",
            user_session_id="test-session",
            worker_token="x" * 64,
            bind_host="127.0.0.1",
            advertise_host="127.0.0.1",
            wsl_distro=None,
            logger=logging.getLogger(__name__),
        )

    async def invoke_capability(self, kind, action, args, **kwargs):
        self.worker.mark_request(kwargs["request_id"])
        return await super().invoke_capability(kind, action, args, **kwargs)


def _registry(
    tmp_path: Path,
    tool: str,
    facade: _CapabilityFacade | None,
) -> ToolRegistry:
    runtime = (
        SimpleNamespace(orchestrator=facade)
        if facade is not None
        else SimpleNamespace(orchestrator=SimpleNamespace())
    )
    return ToolRegistry(
        [tool],
        access_root=tmp_path,
        workspace_dir=tmp_path,
        secrets={},
        audit_context={
            "_runtime": runtime,
            "agent_name": "agent1",
            "request_id": "request-7",
            "global_config": SimpleNamespace(instance_id="HASHI1"),
        },
    )


def _browser_registration(*, expires_at=None, actions=("get_text",)):
    return {
        "capability_id": "cap-browser",
        "capability_kind": "browser_control",
        "instance_id": "HASHI1",
        "device_id": "device-a",
        "user_session_id": "user-a",
        "supported_actions": list(actions),
        "expires_at": time.time() + 90 if expires_at is None else expires_at,
    }


def _computer_registration(*, expires_at=None, actions=("click",)):
    return {
        **_browser_registration(expires_at=expires_at, actions=actions),
        "capability_id": "cap-computer",
        "capability_kind": "computer_control",
    }


def _definition_names(registry):
    return {
        item["function"]["name"]
        for item in registry.get_tool_definitions()
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "second_scope", [{"request_id": "request-8"}, {"agent_name": "agent2"}]
)
async def test_device_call_counter_is_scoped_but_same_call_replay_still_rejected(
    tmp_path, second_scope
):
    facade = _ReplayCheckingFacade(tmp_path)
    registry = _registry(tmp_path, "browser_active_tab", facade)
    shared_task = {"task_id": "long-running-task"}
    first = await registry.execute_with_audit_context(
        "browser_active_tab", {}, "2", audit_context=shared_task
    )
    scope = {**shared_task, **second_scope}
    second = await registry.execute_with_audit_context(
        "browser_active_tab", {}, "2", audit_context=scope
    )
    # Changing arguments must not disguise a repeat of the same scoped call.
    replay = await registry.execute_with_audit_context(
        "browser_active_tab", {"url": "https://changed.test"}, "2", audit_context=scope
    )

    assert not first.is_error and not second.is_error
    assert len(facade.calls) == 2
    assert facade.calls[0][3]["task_id"] == facade.calls[1][3]["task_id"]
    assert replay.is_error and "replayed capability request rejected" in replay.output
    assert first.tool_call_id == second.tool_call_id == replay.tool_call_id == "2"


@pytest.mark.asyncio
async def test_unscoped_registries_do_not_share_low_device_call_counters(tmp_path):
    facade = _ReplayCheckingFacade(tmp_path)
    registries = [_registry(tmp_path, "browser_active_tab", facade) for _ in range(2)]
    for registry in registries:
        registry.audit_context.pop("request_id")
        registry.audit_context.pop("agent_name")
        result = await registry.execute("browser_active_tab", {}, "2")
        assert not result.is_error
    replay = await registries[-1].execute("browser_active_tab", {}, "2")
    assert replay.is_error and "replayed capability request rejected" in replay.output
    assert len(facade.calls) == 2


def test_catalogue_hides_unregistered_browser_and_explains_web_replacement(tmp_path):
    facade = _CapabilityFacade()
    registry = ToolRegistry(
        ["browser_get_text", "web_fetch"],
        access_root=tmp_path,
        workspace_dir=tmp_path,
        secrets={},
        audit_context={
            "_runtime": SimpleNamespace(orchestrator=facade),
            "global_config": SimpleNamespace(instance_id="HASHI1"),
        },
    )

    definitions = registry.get_tool_definitions()

    assert {item["function"]["name"] for item in definitions} == {"web_fetch"}
    assert "Browser control is currently unavailable" in definitions[0]["function"]["description"]


def test_catalogue_tracks_browser_registration_expiry_and_recovery(tmp_path):
    facade = _CapabilityFacade(capabilities=[_browser_registration()])
    registry = _registry(tmp_path, "browser_get_text", facade)

    assert _definition_names(registry) == {"browser_get_text"}

    facade.capabilities["capabilities"][0]["expires_at"] = time.time() - 1
    assert _definition_names(registry) == set()
    assert registry.tool_availability("browser_get_text")["reason"] == "registration_expired"

    facade.capabilities["capabilities"] = [_browser_registration()]
    assert _definition_names(registry) == {"browser_get_text"}


def test_connected_browser_catalogue_describes_bridge_without_standalone_options(tmp_path):
    chrome = {
        **_browser_registration(actions=("active_tab", "screenshot", "get_text")),
        "browser_id": "chrome",
        "browser_name": "Google Chrome",
    }
    facade = _CapabilityFacade(capabilities=[chrome])
    registry = ToolRegistry(
        ["browser_active_tab", "browser_screenshot", "browser_get_text"],
        access_root=tmp_path,
        workspace_dir=tmp_path,
        secrets={},
        audit_context={
            "_runtime": SimpleNamespace(orchestrator=facade),
            "global_config": SimpleNamespace(instance_id="HASHI1"),
        },
    )

    definitions = {
        item["function"]["name"]: item["function"]
        for item in registry.get_tool_definitions()
    }
    assert "Google Chrome [chrome]" in definitions["browser_active_tab"]["description"]
    for function in definitions.values():
        assert "cdp_url" not in function["parameters"]["properties"]
        assert "headed" not in function["parameters"]["properties"]
        assert "CDP" not in function["description"]
    assert "visible Chrome" not in definitions["browser_active_tab"]["description"]
    assert "logged-in browser" not in definitions["browser_get_text"]["description"]


@pytest.mark.asyncio
async def test_unregistered_browser_is_rejected_before_dispatch_with_typed_reason(tmp_path):
    facade = _CapabilityFacade()
    registry = _registry(tmp_path, "browser_get_text", facade)

    result = await registry.execute(
        "browser_get_text",
        {"url": "https://example.test"},
        tool_call_id="browser-missing",
    )

    assert result.is_error is True
    assert result.details["code"] == "capability_unavailable"
    assert result.details["reason"] == "worker_not_registered"
    assert "web_fetch" in result.details["next_step"]
    assert facade.calls == []


@pytest.mark.asyncio
async def test_capability_disappearing_after_catalogue_preflight_stays_typed(tmp_path):
    facade = _CapabilityFacade(
        CapabilityUnavailableError(
            "browser_control",
            action="get_text",
            reason="registration_missing_or_expired",
        ),
        capabilities=[_browser_registration()],
    )
    registry = _registry(tmp_path, "browser_get_text", facade)

    result = await registry.execute(
        "browser_get_text",
        {"url": "https://example.test"},
        tool_call_id="browser-race",
    )

    assert result.is_error is True
    assert result.details["code"] == "capability_unavailable"
    assert result.details["reason"] == "disappeared_before_execution"
    assert "unexpected failure" not in result.output


@pytest.mark.asyncio
async def test_requested_browser_missing_keeps_actionable_reason(tmp_path):
    facade = _CapabilityFacade(
        CapabilityUnavailableError(
            "browser_control", action="get_text",
            reason="requested_browser_not_connected",
        ),
        capabilities=[_browser_registration()],
    )
    registry = _registry(tmp_path, "browser_get_text", facade)
    result = await registry.execute(
        "browser_get_text", {"browser_target": "edge"}, tool_call_id="missing-edge"
    )
    assert result.is_error is True
    assert result.details["reason"] == "requested_browser_not_connected"
    assert "connected browser target" in result.output


@pytest.mark.asyncio
async def test_switching_a_task_bound_browser_reports_clear_error(tmp_path):
    facade = _CapabilityFacade(
        RuntimeError(
            "core.capability.invoke: CapabilityBrokerError: browser target is fixed "
            "for this task; start a new task to switch"
        ),
        capabilities=[_browser_registration()],
    )
    registry = _registry(tmp_path, "browser_get_text", facade)

    result = await registry.execute(
        "browser_get_text", {"browser_target": "edge"}, tool_call_id="locked-target"
    )

    assert result.is_error is True
    assert result.details["code"] == "browser_target_locked"
    assert "start a new task" in result.output
    assert "unexpected failure" not in result.output


def test_cross_instance_capability_never_enters_local_catalogue(tmp_path):
    facade = _CapabilityFacade(capabilities=[_browser_registration()])
    facade.capabilities["instance_id"] = "HASHI2"
    registry = _registry(tmp_path, "browser_get_text", facade)

    assert _definition_names(registry) == set()
    assert registry.tool_availability("browser_get_text")["reason"] == (
        "cross_instance_capability_snapshot"
    )


def test_unreadable_capability_snapshot_fails_closed_with_accurate_reason(tmp_path):
    facade = _CapabilityFacade()
    facade.capabilities = {}
    registry = _registry(tmp_path, "browser_get_text", facade)

    assert _definition_names(registry) == set()
    assert registry.tool_availability("browser_get_text")["reason"] == (
        "capability_status_unavailable"
    )


@pytest.mark.asyncio
async def test_windows_tools_route_through_core_capability_facade(tmp_path):
    facade = _CapabilityFacade(
        {"provider": "persistent-worker"},
        capabilities=[_computer_registration()],
    )
    registry = _registry(tmp_path, "windows_click", facade)

    result = await registry.execute(
        "windows_click",
        {"x": 10, "y": 20},
        tool_call_id="call-7",
    )

    assert result.is_error is False
    assert json.loads(result.output) == {"provider": "persistent-worker"}
    kind, action, args, kwargs = facade.calls[0]
    assert (kind, action) == ("computer_control", "click")
    assert args["_authorized_roots"] == [str(tmp_path)]
    assert kwargs["request_id"] != "call-7"
    assert kwargs == {
        "task_id": "request-7",
        "request_id": kwargs["request_id"],
        "authorization": "tool_registry",
    }


@pytest.mark.asyncio
async def test_browser_tools_route_with_audit_and_protocol_action_mapping(tmp_path):
    facade = _CapabilityFacade(
        "playing",
        capabilities=[_browser_registration(actions=("media_play",))],
    )
    registry = _registry(tmp_path, "browser_play", facade)

    result = await registry.execute(
        "browser_play",
        {"url": "https://example.test/video"},
        tool_call_id="call-browser",
    )

    assert result.output == "playing"
    kind, action, args, kwargs = facade.calls[0]
    assert (kind, action) == ("browser_control", "media_play")
    assert args["_audit"]["agent_name"] == "agent1"
    assert args["_audit"]["request_id"] == "request-7"
    assert kwargs["task_id"] == "request-7"


@pytest.mark.asyncio
async def test_missing_registered_capability_fails_closed_without_legacy_fallback(tmp_path):
    facade = _CapabilityFacade(RuntimeError("capability is unavailable"))
    registry = _registry(tmp_path, "windows_click", facade)

    result = await registry.execute("windows_click", {"x": 10, "y": 20})

    assert result.is_error is True
    assert "capability_unavailable" in result.output
    assert result.details["code"] == "capability_unavailable"
    assert len(facade.calls) == 0


@pytest.mark.asyncio
async def test_standalone_registry_keeps_explicit_legacy_executor_for_diagnostics(
    tmp_path,
    monkeypatch,
):
    from tools import windows_use

    async def info(_arguments):
        return "legacy diagnostic executor"

    monkeypatch.setattr(windows_use, "execute_windows_info", info)
    registry = _registry(tmp_path, "windows_info", None)

    result = await registry.execute("windows_info", {})

    assert result.output == "legacy diagnostic executor"


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["embedded", "Embedded", " embedded ", "other"])
async def test_standalone_browser_never_falls_back_from_explicit_other_provider(
    tmp_path, monkeypatch, target
):
    from tools import browser

    local_calls = []

    async def local_browser(arguments):
        local_calls.append(arguments)
        return "extension browser result"

    monkeypatch.setattr(browser, "execute_browser_get_text", local_browser)
    registry = _registry(tmp_path, "browser_get_text", None)

    result = await registry.execute(
        "browser_get_text",
        {"url": "https://example.test", "browser_target": target},
    )

    assert result.is_error is True
    assert local_calls == []
    assert (
        "invalid browser_target" if target == "other" else "no browser fallback"
    ) in result.output


@pytest.mark.asyncio
@pytest.mark.parametrize("target", [None, "extension"])
async def test_standalone_browser_preserves_default_and_extension_execution(
    tmp_path, monkeypatch, target
):
    from tools import browser

    local_calls = []

    async def local_browser(arguments):
        local_calls.append(arguments)
        return "extension browser result"

    monkeypatch.setattr(browser, "execute_browser_get_text", local_browser)
    registry = _registry(tmp_path, "browser_get_text", None)
    arguments = {"url": "https://example.test"}
    if target is not None:
        arguments["browser_target"] = target

    result = await registry.execute("browser_get_text", arguments)

    assert result.is_error is False
    assert result.output == "extension browser result"
    assert len(local_calls) == 1


@pytest.mark.asyncio
async def test_gateway_does_not_advertise_broker_browser_without_broker_executor(
    tmp_path, monkeypatch
):
    from tools import browser

    registry = _registry(tmp_path, "browser_get_text", None)
    monkeypatch.setattr(
        registry,
        "_capability_status_snapshot",
        lambda: {
            "instance_id": "HASHI1",
            "capabilities": [_browser_registration()],
        },
    )
    local_calls = []

    async def local_browser(_arguments):
        local_calls.append(True)
        return "local browser result"

    monkeypatch.setattr(browser, "execute_browser_get_text", local_browser)

    assert _definition_names(registry) == set()
    result = await registry.execute("browser_get_text", {"url": "https://example.test"})
    assert result.is_error is True
    assert result.details["reason"] == "broker_executor_unbound"
    assert local_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        (
            "shell",
            {"command": "python tools/hchat_send.py --to agent@HASHI2"},
        ),
        (
            "http_request",
            {"url": "http://peer.invalid/protocol/message", "method": "POST"},
        ),
        (
            "shell",
            {"command": "curl -X POST http://local.invalid/protocol/outbound"},
        ),
        (
            "http_request",
            {"url": "http://local.invalid/api/chat", "method": "POST"},
        ),
    ],
)
async def test_system_exchange_cannot_open_a_second_reply_channel(
    tmp_path,
    tool_name,
    arguments,
):
    registry = ToolRegistry(
        [tool_name],
        access_root=tmp_path,
        workspace_dir=tmp_path,
        secrets={},
        audit_context={
            "system_exchange": True,
            "system_exchange_kind": "message",
        },
    )

    result = await registry.execute(tool_name, arguments, "loop-attempt")

    assert result.is_error is True
    assert result.details["reason"] == "system_exchange_loop_guard"
    assert "let the protocol return it once" in result.output


@pytest.mark.asyncio
async def test_terminal_exchange_empty_allowlist_denies_every_tool(tmp_path):
    registry = ToolRegistry(
        ["windows_info"],
        access_root=tmp_path,
        workspace_dir=tmp_path,
        secrets={},
        audit_context={
            "system_exchange": True,
            "system_exchange_terminal": True,
            "request_tool_allowlist": [],
        },
    )

    result = await registry.execute("windows_info", {}, "terminal-tool")

    assert result.is_error is True
    assert "outside the current request" in result.output
