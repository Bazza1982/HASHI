from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from adapters.hashi_mcp import prepare_hashi_mcp
from tools.browser_gateway_proxy import BrowserGatewayProxy, MAX_ACTIVE_SCOPES
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


def _pao_audit(tmp_path, facade, agent="fixture", request_id="read-request"):
    from orchestrator.session_store import SessionStore
    store = SessionStore(tmp_path/"state/sessions.sqlite3", instance_id="HASHI4")
    session = store.create_session(owner_id="owner", agent_id=agent)
    accepted = store.accept_run(session_id=session["session_id"], owner_id="owner",
        agent_id=agent, request_id=request_id, text="test tools", source="test",
        idempotency_key=request_id)
    fence = store.mark_request_running(request_id, worker_id="HASHI4:"+agent)
    return {"_runtime":SimpleNamespace(orchestrator=facade, session_store=store),
        "agent_name":agent, "request_id":request_id, "owner_id":"owner", "instance_id":"HASHI4",
        "hashi_session_id":session["session_id"], "hashi_run_id":accepted.run_id,
        "hashi_fencing_token":fence}


@pytest.mark.asyncio
async def test_installed_cli_file_tools_return_to_authoritative_owner(tmp_path):
    from tools.gateway.mcp_stdio import ToolGateway
    facade = _Facade()
    config = SimpleNamespace(
        instance_id="HASHI4", project_root=Path(__file__).resolve().parents[1],
        bridge_home=tmp_path, secrets_path=tmp_path/"secret.json",
    )
    audit = {**_pao_audit(tmp_path, facade), "global_config":config}
    workspace = tmp_path/"workspace"
    workspace.mkdir()
    note = workspace/"note.txt"
    note.write_text("owner-read-evidence", encoding="utf-8")
    registry = ToolRegistry(["file_read", "file_write"], access_root=workspace,
                            workspace_dir=workspace, secrets={}, audit_context=audit)
    adapter = SimpleNamespace(tool_registry=registry,
        config=SimpleNamespace(workspace_dir=workspace), global_config=config)
    invocation = prepare_hashi_mcp(adapter, backend="codex-cli")
    try:
        context = load_gateway_context(Path(invocation["context_path"]))
        assert context.audit.get("tool_gateway_proxy"), "Installed CLI lost its owner authority"
        gateway = ToolGateway(context)
        result = await gateway.call("hashi_file_read", {"path":str(note)}, "read")
        assert result["isError"] is False
        assert "owner-read-evidence" in json.dumps(result)
        outside = tmp_path/"outside.txt"
        from dataclasses import replace
        altered = replace(context, access_root=str(tmp_path), access_roots=[str(tmp_path)])
        denied = await ToolGateway(altered).call(
            "hashi_file_write", {"path":str(outside), "content":"unsafe"}, "write")
        assert denied["isError"] is True
        assert not outside.exists()
        descriptor = dict(context.audit["tool_gateway_proxy"])
        invocation.close()
        rejected = await gateway.call("hashi_file_read", {"path":str(note)}, "revoked")
        assert rejected["isError"] is True
    finally:
        invocation.close()
        proxy = getattr(adapter, "_browser_gateway_proxy", None)
        if proxy:
            await asyncio.to_thread(proxy.close)

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
async def test_cli_browser_gateway_tokens_are_concurrent_and_independently_revoked(tmp_path):
    facade = _Facade()
    owner_registry = _registry(tmp_path, {
        "_runtime": SimpleNamespace(orchestrator=facade),
        "agent_name": "agent1",
        "request_id": "bootstrap",
        "global_config": SimpleNamespace(instance_id="HASHI4"),
    })
    proxy = BrowserGatewayProxy(owner_registry, asyncio.get_running_loop())
    try:
        descriptors = {
            request_id: proxy.issue({
                "_runtime": SimpleNamespace(orchestrator=facade),
                "agent_name": "agent1",
                "request_id": request_id,
                "global_config": SimpleNamespace(instance_id="HASHI4"),
            })
            for request_id in ("request-a", "request-b")
        }
        gateways = {
            request_id: _registry(tmp_path, {
                "browser_gateway_proxy": descriptor,
                "agent_name": "agent1",
                "request_id": request_id,
                "global_config": SimpleNamespace(instance_id="HASHI4"),
            })
            for request_id, descriptor in descriptors.items()
        }

        first = await asyncio.gather(*(
            registry.execute("browser_get_text", {}, tool_call_id=f"call-{request_id}")
            for request_id, registry in gateways.items()
        ))
        assert [result.output for result in first] == [
            "page from Broker", "page from Broker"
        ]
        assert {call[3]["task_id"] for call in facade.calls} == {
            "request-a", "request-b"
        }

        assert proxy.revoke(descriptors["request-a"]["token"]) is True
        revoked, still_live = await asyncio.gather(
            gateways["request-a"].execute(
                "browser_get_text", {}, tool_call_id="revoked"
            ),
            gateways["request-b"].execute(
                "browser_get_text", {}, tool_call_id="still-live"
            ),
        )
        assert revoked.is_error is True
        assert still_live.is_error is False
        assert facade.calls[-1][3]["task_id"] == "request-b"
    finally:
        await asyncio.to_thread(proxy.close)


@pytest.mark.asyncio
async def test_cli_browser_gateway_scope_limit_is_fail_closed_and_recoverable(tmp_path):
    facade = _Facade()
    audit = {
        "_runtime": SimpleNamespace(orchestrator=facade),
        "agent_name": "agent1",
        "request_id": "bounded",
        "global_config": SimpleNamespace(instance_id="HASHI4"),
    }
    proxy = BrowserGatewayProxy(_registry(tmp_path, audit), asyncio.get_running_loop())
    try:
        descriptors = [proxy.issue(audit) for _index in range(MAX_ACTIVE_SCOPES)]
        with pytest.raises(RuntimeError, match="too many active request scopes"):
            proxy.issue(audit)
        assert proxy.revoke(descriptors[0]["token"]) is True
        assert proxy.issue(audit)["token"]
    finally:
        await asyncio.to_thread(proxy.close)


@pytest.mark.asyncio
async def test_cli_context_publishes_only_owning_worker_proxy(tmp_path):
    facade = _Facade()
    registry = _registry(tmp_path, {**_pao_audit(tmp_path, facade, "agent1", "request-8"),
        "global_config": SimpleNamespace(instance_id="HASHI4")})
    adapter = SimpleNamespace(
        tool_registry=registry,
        config=SimpleNamespace(workspace_dir=tmp_path),
        global_config=SimpleNamespace(project_root=Path(__file__).resolve().parents[1]),
    )
    descriptor = None
    try:
        descriptor = prepare_hashi_mcp(adapter, backend="codex-cli")
        assert descriptor is not None
        context = load_gateway_context(Path(descriptor["context_path"]))
        assert context.audit["tool_gateway_proxy"]["url"].startswith(
            "http://127.0.0.1:"
        )
        assert context.audit["request_id"] == "request-8"
        assert "_runtime" not in context.audit
    finally:
        if descriptor is not None:
            descriptor.close()
        proxy = getattr(adapter, "_browser_gateway_proxy", None)
        if proxy is not None:
            await asyncio.to_thread(proxy.close)
# HASHI-Fingerprint: hfp_7c9dcfd71f854fe18d24f90d38c02b99
