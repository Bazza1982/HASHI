"""Search continuations retain their unreturned results across Worker replacement."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from tools.registry import ToolRegistry


def registry(root, home, instance_home):
    return ToolRegistry(
        ["file_search", "file_list", "log_query", "tool_output_read"],
        root, home, {},
        audit_context={"agent_name": "fixture", "global_config": SimpleNamespace(bridge_home=instance_home)},
        search_scope={"agent_id": "fixture", "agent_home": str(home), "owner_id": "owner:one"},
    )


def data(result, tool):
    assert not result.is_error, result.output
    value = json.loads(result.output.splitlines()[-1] if tool == "file_list" else result.output)
    return value["data"] if "status" in value else value


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["file_search", "file_list", "log_query"])
async def test_cursor_survives_registry_replacement_and_rejects_other_run(tmp_path, tool):
    root, home, instance = tmp_path / "files", tmp_path / "agent", tmp_path / "instance"
    for directory in (root, home, instance):
        directory.mkdir()
    (root / "one.txt").write_text("needle needle needle", encoding="utf-8")
    (root / "two.txt").write_text("needle", encoding="utf-8")
    if tool == "file_search":
        args = {"query": "needle", "mode": "content", "roots": [str(root)], "max_results": 1}
    elif tool == "file_list":
        args = {"path": str(root), "recursive": True, "max_results": 1}
    else:
        args = {"path": str(root / "one.txt"), "terms": ["needle"], "max_results": 1}
    context = {
        "instance_id": "HASHI5", "agent_name": "fixture", "owner_id": "owner:one",
        "hashi_session_id": "session-one", "hashi_run_id": "run-one",
        "request_id": "request-one", "function_generation": "old-worker",
    }
    first_registry = registry(root, home, instance)
    first = data(await first_registry.execute_with_audit_context(tool, args, audit_context=context), tool)
    cursor = first["next_cursor"]
    assert cursor
    replacement = registry(root, home, instance)
    changed = {**args, "cursor": cursor}
    denied = await replacement.execute_with_audit_context(
        tool, changed, audit_context={**context, "owner_id": "owner:other"})
    assert denied.is_error and "Cursor is unavailable" in denied.output
    new_context = {**context, "function_generation": "replacement-worker"}
    second_result = await replacement.execute_with_audit_context(tool, changed, audit_context=new_context)
    second = data(second_result, tool)
    if tool == "file_list":
        assert ".txt" in second_result.output
    else:
        assert second["matches"]
    replay_result = await replacement.execute_with_audit_context(tool, changed, audit_context=new_context)
    replay = data(replay_result, tool)
    if tool == "file_list":
        assert replay_result.output.splitlines()[:-1] == second_result.output.splitlines()[:-1]
    else:
        assert replay["matches"] == second["matches"]


@pytest.mark.asyncio
async def test_cancelled_continuation_can_be_retried_and_terminal_cleanup_removes_it(tmp_path, monkeypatch):
    from tools import file_search
    from tools.tool_output_store import run_binding

    root, home, instance = tmp_path / "files", tmp_path / "agent", tmp_path / "instance"
    for directory in (root, home, instance):
        directory.mkdir()
    (root / "one.txt").write_text("needle needle", encoding="utf-8")
    context = {
        "instance_id": "HASHI5", "agent_name": "fixture", "owner_id": "owner:one",
        "hashi_session_id": "session-one", "hashi_run_id": "run-one", "request_id": "request-one",
    }
    reg = registry(root, home, instance)
    args = {"query": "needle", "mode": "content", "roots": [str(root)], "max_results": 1}
    first = data(await reg.execute_with_audit_context("file_search", args, audit_context=context), "file_search")
    args["cursor"] = first["next_cursor"]
    original = file_search.run_worker

    async def cancelled(_config):
        raise asyncio.CancelledError()

    monkeypatch.setattr(file_search, "run_worker", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await reg.execute_with_audit_context("file_search", args, audit_context=context)
    monkeypatch.setattr(file_search, "run_worker", original)
    assert data(await reg.execute_with_audit_context("file_search", args, audit_context=context), "file_search")["matches"]
    binding = run_binding(context)
    reg._tool_output_store._state_resolver = lambda _binding: "running"
    assert not reg._tool_output_store.cleanup_if_terminal(binding)
    reg._tool_output_store._state_resolver = lambda _binding: "stopped"
    assert reg._tool_output_store.cleanup_if_terminal(binding)
    lost = await reg.execute_with_audit_context("file_search", args, audit_context=context)
    assert lost.is_error and "cursor_stale" in lost.output
# HASHI-Fingerprint: hfp_95e3f915245944a58bf9016567545ba3


def test_many_run_cursors_remain_bounded_isolated_and_have_configurable_capacity(tmp_path):
    import tracemalloc

    from tools.file_search import RunContinuations
    from tools.tool_output_store import ToolOutputStore

    statuses = {"run-one": "running", "run-two": "running"}
    store = ToolOutputStore(instance_home=tmp_path, state_resolver=lambda binding: statuses[binding[-2]])
    one = ("HASHI5", "fixture", "owner:one", "session-one", "run-one", "request-one")
    two = ("HASHI5", "fixture", "owner:two", "session-two", "run-two", "request-two")
    first = RunContinuations(store, one)
    second = RunContinuations(store, two)
    first.configure({})
    second.configure({})
    blob = "x" * (128 * 1024)
    tracemalloc.start()
    try:
        tokens = [first.put("query", {"ordinal": i, "payload": blob}, "operation") for i in range(70)]
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert all(tokens) and peak < 8 * 1024 * 1024
    assert first.get(tokens[0], "query")[0]["ordinal"] == 0
    assert first.get(tokens[-1], "query")[0]["ordinal"] == 69
    with pytest.raises(ValueError):
        second.get(tokens[0], "query")
    with pytest.raises(ValueError):
        first.get(tokens[0], "different-query")
    with pytest.raises(ValueError):
        store.page(one, tokens[0], 0)

    second.configure({"continuations": {"max_entries": 1, "max_bytes": 1000, "max_state_bytes": 1000}})
    token = second.put("query", {"ordinal": 1}, "operation")
    assert token
    assert second.put("query", {"ordinal": 2}, "operation") is None
    assert second.get(token, "query")[0]["ordinal"] == 1
    statuses["run-one"] = "stopped"
    assert store.cleanup_if_terminal(one)
    assert second.get(token, "query")[0]["ordinal"] == 1
    with pytest.raises(ValueError):
        first.get(tokens[0], "query")
    statuses["run-two"] = "completed"
    assert store.cleanup_if_terminal(two)


@pytest.mark.asyncio
async def test_complete_paging_after_replacement_keeps_all_matches(tmp_path):
    root, home, instance = tmp_path / "files", tmp_path / "agent", tmp_path / "instance"
    for directory in (root, home, instance):
        directory.mkdir()
    (root / "one.txt").write_text("needle needle needle needle", encoding="utf-8")
    args = {"query": "needle", "mode": "content", "roots": [str(root)], "max_results": 1}
    context = {
        "instance_id": "HASHI5", "agent_name": "fixture", "owner_id": "owner:one",
        "hashi_session_id": "session-one", "hashi_run_id": "run-one", "request_id": "request-one",
    }
    offsets = []
    for generation in range(8):
        current = registry(root, home, instance)
        page = data(await current.execute_with_audit_context(
            "file_search", args, audit_context={**context, "function_generation": generation}), "file_search")
        offsets.extend(match["character_offset"] for match in page["matches"])
        if page["coverage_complete"]:
            break
        assert page["next_cursor"]
        args["cursor"] = page["next_cursor"]
    assert offsets == [0, 7, 14, 21]
    assert page["coverage_complete"]


def test_cursor_expiry_and_byte_limits_apply_without_evicting_other_active_cursors(tmp_path, monkeypatch):
    from tools import file_search
    from tools.file_search import RunContinuations
    from tools.tool_output_store import ToolOutputStore

    now = [1_000.0]
    monkeypatch.setattr(file_search.time, "time", lambda: now[0])
    store = ToolOutputStore(instance_home=tmp_path)
    binding = ("HASHI5", "fixture", "owner", "session", "run", "request")
    cursors = RunContinuations(store, binding)
    cursors.configure({"continuations": {
        "max_entries": 1, "max_bytes": 1000, "max_state_bytes": 1000, "ttl_seconds": 10,
    }})
    token = cursors.put("query", {"name": "路径🌸"}, "operation")
    assert cursors.get(token, "query")[0]["name"] == "路径🌸"
    assert cursors.put("query", {"name": "second"}, "operation") is None
    now[0] += 11
    with pytest.raises(ValueError):
        cursors.get(token, "query")
    second = cursors.put("query", {"name": "second"}, "operation")
    assert second and cursors.get(second, "query")[0]["name"] == "second"

    isolated = RunContinuations(store, ("HASHI5", "fixture", "owner", "session", "run2", "request2"))
    isolated.configure({"continuations": {"max_bytes": 80, "max_state_bytes": 80}})
    assert isolated.put("query", {"name": "x" * 200}, "operation") is None


@pytest.mark.asyncio
async def test_continuation_recovers_in_a_fresh_python_process(tmp_path):
    import os
    import subprocess
    import sys

    root, home, instance = tmp_path / "files", tmp_path / "agent", tmp_path / "instance"
    for directory in (root, home, instance):
        directory.mkdir()
    (root / "one.txt").write_text("needle needle", encoding="utf-8")
    args = {"query": "needle", "mode": "content", "roots": [str(root)], "max_results": 1}
    context = {
        "instance_id": "HASHI5", "agent_name": "fixture", "owner_id": "owner:one",
        "hashi_session_id": "session-one", "hashi_run_id": "run-one",
        "request_id": "request-one", "function_generation": "original-process",
    }
    first = data(await registry(root, home, instance).execute_with_audit_context(
        "file_search", args, audit_context=context), "file_search")
    code = """
import asyncio, json, sys
from pathlib import Path
from types import SimpleNamespace
from tools.registry import ToolRegistry
root,home,instance=map(Path,sys.argv[1:4])
context={"instance_id":"HASHI5","agent_name":"fixture","owner_id":"owner:one",
"hashi_session_id":"session-one","hashi_run_id":"run-one","request_id":"request-one",
"function_generation":"new-process"}
registry=ToolRegistry(["file_search"],root,home,{},
    audit_context={"agent_name":"fixture","global_config":SimpleNamespace(bridge_home=instance)},
    search_scope={"agent_id":"fixture","agent_home":str(home),"owner_id":"owner:one"})
args={"query":"needle","mode":"content","roots":[str(root)],"max_results":1,"cursor":sys.argv[4]}
result=asyncio.run(registry.execute_with_audit_context("file_search",args,audit_context=context))
print(json.dumps({"error":result.is_error,"result":json.loads(result.output)}))
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(str(item) for item in sys.path if item)
    process = subprocess.run(
        [sys.executable, "-B", "-c", code, str(root), str(home), str(instance), first["next_cursor"]],
        cwd=home, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", timeout=30, check=True,
    )
    observed = json.loads(process.stdout.splitlines()[-1])
    assert not observed["error"], process.stdout + process.stderr
    assert observed["result"]["data"]["matches"][0]["character_offset"] == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["read", "write"])
async def test_slow_cursor_storage_does_not_block_or_prevent_cancellation(tmp_path, monkeypatch, operation):
    import threading

    root, home, instance = tmp_path / "files", tmp_path / "agent", tmp_path / "instance"
    for directory in (root, home, instance):
        directory.mkdir()
    (root / "one.txt").write_text("needle needle", encoding="utf-8")
    context = {
        "instance_id": "HASHI5", "agent_name": "fixture", "owner_id": "owner:one",
        "hashi_session_id": "session-one", "hashi_run_id": "run-one", "request_id": "request-one",
    }
    reg = registry(root, home, instance)
    run_state = {"value": "running"}
    reg._tool_output_store._state_resolver = lambda _binding: run_state["value"]
    args = {"query": "needle", "mode": "content", "roots": [str(root)], "max_results": 1}
    if operation == "read":
        first = data(await reg.execute_with_audit_context("file_search", args, audit_context=context), "file_search")
        args["cursor"] = first["next_cursor"]
    name = "read_search_continuation" if operation == "read" else "retain_search_continuation"
    original = getattr(reg._tool_output_store, name)
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def slow(*call_args, **call_kwargs):
        started.set()
        try:
            if not release.wait(5):
                raise TimeoutError("fixture storage was not released")
            return original(*call_args, **call_kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(reg._tool_output_store, name, slow)
    emergency_release = threading.Timer(3, release.set)
    emergency_release.start()
    task = asyncio.create_task(reg.execute_with_audit_context("file_search", args, audit_context=context))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        assert not release.is_set(), "synchronous storage blocked the event loop"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        if operation == "write":
            run_state["value"] = "stopped"
    finally:
        release.set()
        emergency_release.cancel()
        assert await asyncio.to_thread(finished.wait, 6)
    monkeypatch.setattr(reg._tool_output_store, name, original)
    if operation == "read":
        assert data(await reg.execute_with_audit_context(
            "file_search", args, audit_context=context), "file_search")["matches"]
    else:
        from tools.tool_output_store import run_binding
        assert not reg._tool_output_store.has_bound(run_binding(context))


@pytest.mark.asyncio
async def test_herv3_gateway_retains_actual_run_binding_across_registry_replacement(tmp_path):
    from orchestrator.flexible_backend_manager import FlexibleBackendManager
    from orchestrator.session_store import SessionStore
    from tools.gateway.context import GatewayContext
    from tools.tool_output_store import run_binding

    root, home, instance = tmp_path / "files", tmp_path / "agent", tmp_path / "instance"
    for directory in (root, home, instance):
        directory.mkdir()
    (root / "one.txt").write_text("needle needle", encoding="utf-8")
    store = SessionStore(instance / "state" / "sessions.sqlite3", instance_id="HASHI5")
    session = store.ensure_default_session(owner_id="owner:one", agent_id="fixture")
    run = store.accept_run(
        session_id=session["session_id"], owner_id="owner:one", agent_id="fixture",
        request_id="request-one", text="fixture", source="test", idempotency_key="request-one",
    )
    fence = store.mark_request_running("request-one", worker_id="fixture-worker")
    global_config = SimpleNamespace(instance_id="HASHI5", bridge_home=instance)
    runtime = SimpleNamespace(
        name="fixture", global_config=global_config, session_store=store,
        _request_meta_by_id={"request-one": {
            "request_id": "request-one", "hashi_session_id": session["session_id"],
            "hashi_run_id": run.run_id, "hashi_fencing_token": fence, "owner_id": "owner:one",
        }},
    )
    source = registry(root, home, instance)
    source.audit_context["global_config"] = global_config
    manager = FlexibleBackendManager.__new__(FlexibleBackendManager)
    manager.runtime = runtime
    manager.config = SimpleNamespace(active_backend="her-v3")
    manager.current_backend = SimpleNamespace(tool_registry=source)
    manager._refresh_tool_runtime_context("request-one")
    gateway = GatewayContext.from_registry(source, backend="her-v3")
    args = {"query": "needle", "mode": "content", "roots": [str(root)], "max_results": 1}
    first = data(await gateway.build_registry().execute("file_search", args), "file_search")
    cursor = first["next_cursor"]
    assert cursor
    replacement = gateway.build_registry()
    second = data(await replacement.execute("file_search", {**args, "cursor": cursor}), "file_search")
    assert second["matches"][0]["character_offset"] == 7
    binding = run_binding(gateway.audit)
    assert replacement._tool_output_store.has_bound(binding)
    store.cancel_run(run.run_id, owner_id="owner:one")
    after_cancel = gateway.build_registry()
    assert not after_cancel._tool_output_store.has_bound(binding)
