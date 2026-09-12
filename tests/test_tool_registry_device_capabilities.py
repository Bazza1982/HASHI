from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.capability_broker import CapabilityUnavailableError
from tools.registry import ToolRegistry


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
    assert kwargs == {
        "task_id": "request-7",
        "request_id": "call-7",
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
