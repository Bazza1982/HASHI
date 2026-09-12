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
async def test_worker_derives_canonical_session_for_mutating_menu_operation(monkeypatch):
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

    result = await try_execute_slash_command_text(
        _runtime(), _wire("open", command="/model"), source_channel="workbench_api"
    )

    assert result["ok"] is True
    assert observed[0]["owner_id"] == "canonical-owner"
    assert observed[0]["session_id"] == "canonical-session"
    assert observed[0]["context_generation"] == 4


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
