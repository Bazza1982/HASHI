from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from adapters.hashi_mcp import prepare_hashi_mcp
from tools.browser_gateway_proxy import BrowserGatewayProxy
from tools.gateway.context import load_gateway_context
from tools.registry import ToolRegistry


class _Facade:
    is_function_worker_facade = True

    def __init__(self) -> None:
        self.calls = []

    def resolve_service_endpoint(self, service, *, expected_instance=None):
        return {
            "service": service, "instance_id": expected_instance,
            "host": "127.0.0.1", "port": 18888,
            "base_url": "http://127.0.0.1:18888",
        }

    def capability_status(self):
        return {
            "instance_id": "HASHI4",
            "capabilities": [{
                "capability_id": "cap-chrome", "capability_kind": "browser_control",
                "browser_id": "chrome", "browser_name": "Google Chrome",
                "supported_actions": ["get_text"], "expires_at": time.time() + 90,
            }],
        }

    async def invoke_capability(self, kind, action, args, **kwargs):
        self.calls.append((kind, action, args, kwargs))
        return "page from Broker"


def _registry(tmp_path: Path, audit: dict) -> ToolRegistry:
    return ToolRegistry(
        ["browser_get_text"], access_root=tmp_path, workspace_dir=tmp_path,
        secrets={}, audit_context=audit,
    )


@pytest.mark.asyncio
async def test_cli_browser_gateway_uses_owner_registry_and_core_facade(tmp_path):
    facade = _Facade()
    owner_audit = {
        "_runtime": SimpleNamespace(orchestrator=facade),
        "agent_name": "agent1", "request_id": "request-7",
        "global_config": SimpleNamespace(instance_id="HASHI4"),
    }
    owner_registry = _registry(tmp_path, owner_audit)
    proxy = BrowserGatewayProxy(owner_registry, asyncio.get_running_loop())
    try:
        gateway_audit = {
            "browser_gateway_proxy": proxy.issue(owner_audit),
            "agent_name": "agent1", "request_id": "request-7",
            "global_config": SimpleNamespace(instance_id="HASHI4"),
        }
        gateway_registry = _registry(tmp_path, gateway_audit)
        gateway_registry._capability_status_snapshot = facade.capability_status
        assert gateway_registry.tool_availability("browser_get_text")["available"] is True
        descriptions = gateway_registry.get_tool_definitions()
        assert "Google Chrome [chrome]" in descriptions[0]["function"]["description"]
        result = await gateway_registry.execute(
            "browser_get_text", {"url": "https://example.test", "browser_target": "chrome"},
            tool_call_id="call-7",
        )
        assert result.is_error is False
        assert result.output == "page from Broker"
        assert facade.calls[0][0:2] == ("browser_control", "get_text")
        assert facade.calls[0][2]["_browser_target"] == "chrome"
        assert facade.calls[0][3]["task_id"] == "request-7"
        gateway_audit["browser_gateway_proxy"]["token"] = "wrong"
        rejected = await gateway_registry.execute("browser_get_text", {}, tool_call_id="bad")
        assert rejected.is_error is True
        assert len(facade.calls) == 1
    finally:
        await asyncio.to_thread(proxy.close)


@pytest.mark.asyncio
async def test_cli_context_publishes_only_owning_worker_proxy(tmp_path):
    facade = _Facade()
    registry = _registry(tmp_path, {
        "_runtime": SimpleNamespace(orchestrator=facade),
        "agent_name": "agent1", "request_id": "request-8",
        "global_config": SimpleNamespace(instance_id="HASHI4"),
    })
    adapter = SimpleNamespace(
        tool_registry=registry,
        config=SimpleNamespace(workspace_dir=tmp_path),
        global_config=SimpleNamespace(project_root=Path(__file__).resolve().parents[1]),
    )
    try:
        descriptor = prepare_hashi_mcp(adapter, backend="codex-cli")
        assert descriptor is not None
        context = load_gateway_context(Path(descriptor["context_path"]))
        assert context.audit["browser_gateway_proxy"]["url"].startswith(
            "http://127.0.0.1:"
        )
        assert context.audit["request_id"] == "request-8"
        assert "_runtime" not in context.audit
    finally:
        proxy = getattr(adapter, "_browser_gateway_proxy", None)
        if proxy is not None:
            await asyncio.to_thread(proxy.close)
