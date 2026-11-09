"""Owner authority and cancellation across the real MCP transport."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import json
from pathlib import Path

import pytest

from adapters.hashi_mcp import prepare_hashi_mcp
from tests.test_browser_gateway_proxy import _Facade, _pao_audit
from tools.browser_gateway_proxy import BrowserGatewayProxy
from tools.gateway.context import load_gateway_context
from tools.gateway.mcp_stdio import ToolGateway
from tools.gateway.owner_proxy import execute_via_owner
from tools.registry import ToolRegistry


def setup(tmp_path):
    audit = _pao_audit(tmp_path, _Facade())
    root = tmp_path/"workspace"
    root.mkdir()
    registry = ToolRegistry(["file_read", "file_write"], access_root=root,
        workspace_dir=root, secrets={}, audit_context=audit)
    return audit, root, registry


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["stop", "revoke"])
async def test_queued_owner_call_does_not_execute_after_scope_ends(tmp_path, monkeypatch, ending):
    import orchestrator.execution_resources as resources
    audit, root, registry = setup(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def blocked(*args, **kwargs):
        entered.set()
        await release.wait()
        yield 0

    monkeypatch.setattr(resources, "tool_resource", blocked)
    proxy = BrowserGatewayProxy(registry, asyncio.get_running_loop())
    descriptor = proxy.issue(audit, all_tools=True)
    target = root/"must-not-exist.txt"
    task = asyncio.create_task(execute_via_owner(descriptor, "file_write",
        {"path":str(target), "content":"unsafe"}, "queued"))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        if ending == "stop":
            audit["_runtime"].session_store.cancel_run(audit["hashi_run_id"], owner_id="owner")
        else:
            proxy.revoke(descriptor["token"])
        release.set()
        result = await asyncio.wait_for(task, 5)
        assert result.is_error
        assert not target.exists()
        if ending == "stop":
            assert result.details["tool_executed"] is False
            assert result.details["error_code"] == "owner_run_not_current"
    finally:
        release.set()
        proxy.revoke(descriptor["token"])
        await asyncio.to_thread(proxy.close)


@pytest.mark.asyncio
async def test_owner_run_stopped_during_fingerprint_preparation_never_writes(tmp_path, monkeypatch):
    """The final Run fence belongs at the business execution boundary."""
    import threading
    import orchestrator.fingerprint.execution as fingerprint_execution
    from orchestrator.canonical_audit import CanonicalAuditStore
    from orchestrator.fingerprint.models import FingerprintPolicy

    audit, root, registry = setup(tmp_path)
    policy = FingerprintPolicy(enabled=True).snapshot()
    audit["fingerprint_policy"] = policy
    registry.audit_context["fingerprint_policy"] = policy
    registry.canonical_audit = CanonicalAuditStore(
        tmp_path / "audit-home", instance_id="HASHI4", agent_id=audit["agent_name"]
    )
    entered = threading.Event()
    release = threading.Event()
    original_observe = fingerprint_execution.observe
    observations = 0

    def pause_first_observation(path):
        nonlocal observations
        observations += 1
        if observations == 1:
            entered.set()
            assert release.wait(5), "fingerprint preparation was not released"
        return original_observe(path)

    monkeypatch.setattr(fingerprint_execution, "observe", pause_first_observation)
    proxy = BrowserGatewayProxy(registry, asyncio.get_running_loop())
    descriptor = proxy.issue(audit, all_tools=True)
    target = root / "must-not-execute-after-stop.txt"
    task = asyncio.create_task(execute_via_owner(
        descriptor, "file_write", {"path": str(target), "content": "unsafe"},
        "stop-during-fingerprint-preparation",
    ))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        audit["_runtime"].session_store.cancel_run(
            audit["hashi_run_id"], owner_id=audit["owner_id"]
        )
        release.set()
        result = await asyncio.wait_for(task, 5)
        assert result.is_error
        assert result.details["tool_executed"] is False
        assert result.details["error_code"] == "owner_run_not_current"
        assert not target.exists()
    finally:
        release.set()
        proxy.revoke(descriptor["token"])
        await asyncio.to_thread(proxy.close)


@pytest.mark.asyncio
async def test_cancelled_mcp_call_cancels_owner_without_revoking_other_calls(tmp_path, monkeypatch):
    import orchestrator.execution_resources as resources
    audit, root, registry = setup(tmp_path)
    entered, cleaned = asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def blocked(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
            yield 0
        finally:
            cleaned.set()

    monkeypatch.setattr(resources, "tool_resource", blocked)
    proxy = BrowserGatewayProxy(registry, asyncio.get_running_loop())
    descriptor = proxy.issue(audit, all_tools=True)
    task = asyncio.create_task(execute_via_owner(descriptor, "file_write",
        {"path":str(root/"cancelled.txt"), "content":"unsafe"}, "cancel"))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(cleaned.wait(), 5)
        assert not (root/"cancelled.txt").exists()
        assert descriptor["token"] in proxy._scopes
    finally:
        proxy.revoke(descriptor["token"])
        await asyncio.to_thread(proxy.close)


@pytest.mark.asyncio
async def test_real_stdio_uses_owner_and_rejects_changed_context(tmp_path):
    from types import SimpleNamespace
    audit, root, registry = setup(tmp_path)
    config = SimpleNamespace(instance_id="HASHI4", project_root=Path(__file__).resolve().parents[1],
                             bridge_home=tmp_path, secrets_path=tmp_path/"secret.json")
    audit["global_config"] = config
    registry.audit_context.update(audit)
    note = root/"note.txt"
    note.write_text("real-stdio-owner-evidence", encoding="utf-8")
    adapter = SimpleNamespace(tool_registry=registry, config=SimpleNamespace(workspace_dir=root),
                              global_config=config)
    invocation = prepare_hashi_mcp(adapter, backend="codex-cli")
    try:
        context_path = Path(invocation["context_path"])
        context = load_gateway_context(context_path)
        assert "_runtime" not in context.audit and not context.secrets
        assert "--owner-proxy-required" in invocation["args"]
        process = await asyncio.create_subprocess_exec(invocation["command"], *invocation["args"],
            cwd=invocation["cwd"], stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        messages = [
            {"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"hashi_file_read",
                "arguments":{"path":str(note)}}},
            {"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"hashi_file_write",
                "arguments":{"path":str(tmp_path/"outside.txt"),"content":"unsafe"}}},
        ]
        out, err = await asyncio.wait_for(process.communicate(
            b"".join(json.dumps(m).encode()+b"\n" for m in messages)), 20)
        assert process.returncode == 0, err.decode()
        replies = {r["id"]:r["result"] for r in map(json.loads, out.splitlines())}
        assert not replies[1]["isError"]
        assert "real-stdio-owner-evidence" in json.dumps(replies[1])
        assert replies[2]["isError"] and not (tmp_path/"outside.txt").exists()
        context_path.write_text(context_path.read_text()+" ", encoding="utf-8")
        changed = await asyncio.create_subprocess_exec(invocation["command"], *invocation["args"],
            cwd=invocation["cwd"], stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, changed_err = await asyncio.wait_for(changed.communicate(), 20)
        assert changed.returncode != 0
        assert b"changed after owner admission" in changed_err
    finally:
        invocation.close()
        await asyncio.to_thread(adapter._browser_gateway_proxy.close)


@pytest.mark.asyncio
async def test_cancel_before_tool_registration_prevents_execution(tmp_path):
    import http.client
    from urllib.parse import urlsplit
    audit, root, registry = setup(tmp_path)
    proxy = BrowserGatewayProxy(registry, asyncio.get_running_loop())
    descriptor = proxy.issue(audit, all_tools=True)
    parsed = urlsplit(descriptor["url"])
    dispatch_id = "cancel-before-register"
    target = root/"must-not-execute.txt"

    def post(path, payload):
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
        try:
            connection.request("POST", path, body=json.dumps(payload).encode(),
                headers={"Authorization":"Bearer "+descriptor["token"], "Content-Type":"application/json"})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()
    try:
        status, payload = await asyncio.to_thread(post, "/tool-cancel", {"dispatch_id":dispatch_id})
        assert status == 200 and json.loads(payload)["cleanup_confirmed"] is False
        status, payload = await asyncio.to_thread(post, "/tool", {"tool_name":"file_write",
            "arguments":{"path":str(target), "content":"unsafe"}, "tool_call_id":"late",
            "dispatch_id":dispatch_id})
        assert status != 200
        assert not target.exists()
    finally:
        proxy.revoke(descriptor["token"])
        await asyncio.to_thread(proxy.close)


@pytest.mark.asyncio
async def test_owner_scope_does_not_follow_registry_workzone_changes(tmp_path):
    audit, root, registry = setup(tmp_path)
    proxy = BrowserGatewayProxy(registry, asyncio.get_running_loop())
    descriptor = proxy.issue(audit, all_tools=True)
    target = tmp_path/"outside.txt"
    try:
        registry.access_roots = (tmp_path,)
        registry.read_access_roots = (tmp_path,)
        result = await execute_via_owner(descriptor, "file_write",
            {"path":str(target), "content":"unsafe"}, "changed-roots")
        assert result.is_error
        assert not target.exists()
    finally:
        proxy.revoke(descriptor["token"])
        await asyncio.to_thread(proxy.close)


@pytest.mark.asyncio
async def test_owner_preserves_tool_activity_progress(tmp_path, monkeypatch):
    from types import SimpleNamespace
    audit, root, registry = setup(tmp_path)
    async def observed(name, arguments, call_id="", **kwargs):
        from tools.registry import ToolResult
        observer = kwargs["audit_context"]["tool_activity_observer"]
        await observer(SimpleNamespace(metadata={
            "state":"running", "update_sequence":1, "operation_id":"op", "tool_call_id":call_id,
            "function_generation":"test", "visible_update":True,
            "elapsed_ms":1200, "display_summary":"Working", "selected_roots":[],
        }))
        await asyncio.sleep(1.2)
        return ToolResult(tool_call_id=call_id, output="completed", is_error=False)
    monkeypatch.setattr(registry, "execute_with_audit_context", observed)
    proxy = BrowserGatewayProxy(registry, asyncio.get_running_loop())
    descriptor = proxy.issue(audit, all_tools=True)
    events = []
    try:
        result = await execute_via_owner(descriptor, "file_read", {"path":str(root/"note")},
            "progress", on_tool_activity=events.append)
        assert not result.is_error
        assert events and events[0].metadata["state"] == "running"
    finally:
        proxy.revoke(descriptor["token"])
        await asyncio.to_thread(proxy.close)


@pytest.mark.asyncio
async def test_new_mcp_requires_current_strict_installation_qualification(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import orchestrator.runtime_contract as contract
    audit, workspace, registry = setup(tmp_path)
    config = SimpleNamespace(instance_id="HASHI4", project_root=Path(__file__).resolve().parents[1])
    registry.audit_context.update(audit)
    adapter = SimpleNamespace(tool_registry=registry, config=SimpleNamespace(workspace_dir=workspace),
                              global_config=config)
    admitted = SimpleNamespace(manifest_path=tmp_path/"qualified.json", manifest_digest="sha256:pinned")
    monkeypatch.setattr(contract, "current_process_runtime", lambda: admitted)
    def corrupted(*args, **kwargs):
        raise contract.RuntimeContractError("runtime_integrity_failed: unlisted executable file")
    monkeypatch.setattr(contract, "qualified_runtime_from_reference", corrupted)
    invocation = None
    try:
        with pytest.raises(contract.RuntimeContractError, match="runtime_integrity_failed"):
            invocation = prepare_hashi_mcp(adapter, backend="codex-cli")
    finally:
        if invocation:
            invocation.close()
        proxy = getattr(adapter, "_browser_gateway_proxy", None)
        if proxy:
            await asyncio.to_thread(proxy.close)


@pytest.mark.asyncio
async def test_preflight_catalog_has_no_executable_owner_capability(tmp_path):
    from types import SimpleNamespace
    facade = _Facade()
    registry = ToolRegistry(["file_read"], access_root=tmp_path, workspace_dir=tmp_path,
        secrets={}, audit_context={"_runtime":SimpleNamespace(orchestrator=facade)})
    adapter = SimpleNamespace(tool_registry=registry, config=SimpleNamespace(workspace_dir=tmp_path),
        global_config=SimpleNamespace(project_root=Path(__file__).resolve().parents[1]))
    # A preflight has no live service endpoint in this test; provide the same
    # resolver the owner uses, without adding a PAO Run.
    registry.audit_context["global_config"] = SimpleNamespace(instance_id="HASHI4")
    invocation = prepare_hashi_mcp(adapter, backend="codex-cli")
    try:
        context = load_gateway_context(Path(invocation["context_path"]))
        assert context.audit["tool_gateway_required"]
        assert "tool_gateway_proxy" not in context.audit
        result = await ToolGateway(context).call("hashi_file_read", {"path":str(tmp_path/"any")}, "no-run")
        assert result["isError"]
        assert "runtime_protection_context_unavailable" in json.dumps(result)
    finally:
        invocation.close()
# HASHI-Fingerprint: hfp_5b79a497817c452f879fc4d963a8256d
