from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from adapters.deepseek_api import DeepSeekAdapter
from adapters.her_v2_provider import _CognitiveControlToolRegistry
from adapters.openrouter_api import (
    _provider_call_retry_delay,
    compact_intra_turn_tool_messages,
)
from orchestrator import runtime_delivery, runtime_session
from orchestrator.canonical_audit import CanonicalAuditStore
from orchestrator.execution_resources import (
    _is_strictly_readonly_shell_command,
    tool_resource,
)
from orchestrator.fingerprint.execution import dispatch_controlled
from orchestrator.fingerprint.models import FingerprintPolicy
from orchestrator.frontend_delivery import freeze_run_delivery_route
from orchestrator.her_v2.audit import DurableAuditLog
from orchestrator.her_v2.models import Effort, Stage, StageRequest
from orchestrator.her_v2.task_state import HERTaskState
from orchestrator.her_v2.v3_config import (
    HERv3ModelTarget,
    apply_v3_target,
    normalise_v3_config,
)
from orchestrator.her_v2.v3_prompt import compile_main_prompt
from orchestrator.session_store import SessionStore
from tools.registry import ToolRegistry, ToolResult
from tools.smart_tools import adapt_legacy_result
from tools.tool_presentation import OnDemandToolView


# ---------------------------------------------------------------------------
# WP1: Cross-agent filesystem leases & text_replace synchronization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_file_concurrent_text_replace_file_write_and_apply_patch_serialize(tmp_path):
    registry = SimpleNamespace(
        workspace_dir=tmp_path,
        access_roots=[tmp_path],
        _effective_audit_context=lambda: {},
    )
    file_write_entered = asyncio.Event()
    patch_entered = asyncio.Event()

    async def do_write():
        async with tool_resource(registry, "file_write", {"path": "shared.py"}):
            file_write_entered.set()

    async def do_patch():
        async with tool_resource(registry, "apply_patch", {"path": "shared.py", "patch": "@@ -1 +1 @@"}):
            patch_entered.set()

    async with tool_resource(registry, "text_replace", {"path": "shared.py", "find": "a", "replace": "b"}):
        t_write = asyncio.create_task(do_write())
        t_patch = asyncio.create_task(do_patch())
        await asyncio.sleep(0.08)
        assert not file_write_entered.is_set(), "file_write must wait for text_replace on same file"
        assert not patch_entered.is_set(), "apply_patch must wait for text_replace on same file"

    await asyncio.wait_for(asyncio.gather(t_write, t_patch), timeout=2.0)
    assert file_write_entered.is_set()
    assert patch_entered.is_set()


@pytest.mark.asyncio
async def test_different_files_text_replace_apply_patch_and_file_write_overlap(tmp_path):
    registry = SimpleNamespace(
        workspace_dir=tmp_path,
        access_roots=[tmp_path],
        _effective_audit_context=lambda: {},
    )
    async with tool_resource(registry, "text_replace", {"path": "a.py", "find": "x", "replace": "y"}):
        async with tool_resource(registry, "apply_patch", {"path": "b.py", "patch": "@@ -1 +1 @@"}):
            async with tool_resource(registry, "file_write", {"path": "c.py", "content": "ok"}):
                pass


@pytest.mark.asyncio
async def test_text_replace_preview_only_does_not_block_same_file_write(tmp_path):
    registry = SimpleNamespace(
        workspace_dir=tmp_path,
        access_roots=[tmp_path],
        _effective_audit_context=lambda: {},
    )
    async with tool_resource(registry, "file_write", {"path": "same.py"}):
        async with tool_resource(
            registry,
            "text_replace",
            {"path": "same.py", "find": "a", "replace": "b", "preview_only": True},
        ) as wait_ms:
            assert wait_ms == 0


@pytest.mark.asyncio
async def test_readonly_shell_allows_concurrent_reads_and_file_edits_while_mutating_shell_blocks(tmp_path):
    assert _is_strictly_readonly_shell_command("git status --short")
    assert _is_strictly_readonly_shell_command("git -C repo diff --stat")
    assert _is_strictly_readonly_shell_command("rg -n foo orchestrator/")
    assert not _is_strictly_readonly_shell_command("git status && rm -rf x")
    assert not _is_strictly_readonly_shell_command("git branch new-branch")
    assert not _is_strictly_readonly_shell_command("git commit -m test")
    assert not _is_strictly_readonly_shell_command("pytest -q")
    assert not _is_strictly_readonly_shell_command("echo hi > file.txt")

    registry = SimpleNamespace(
        workspace_dir=tmp_path,
        access_roots=[tmp_path],
        _effective_audit_context=lambda: {},
    )
    # Two read-only shell commands and an independent file edit overlap.
    async with tool_resource(registry, "shell", {"command": "git status"}):
        async with tool_resource(registry, "shell", {"command": "rg pattern ."}):
            async with tool_resource(registry, "text_replace", {"path": "a.py", "find": "1", "replace": "2"}):
                pass

    # A mutating shell command still blocks text_replace in the same workspace.
    entered = asyncio.Event()

    async def edit():
        async with tool_resource(registry, "text_replace", {"path": "a.py", "find": "1", "replace": "2"}):
            entered.set()

    async with tool_resource(registry, "shell", {"command": "pytest -q"}):
        task = asyncio.create_task(edit())
        await asyncio.sleep(0.08)
        assert not entered.is_set()
    await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_shell_in_agent_workspace_does_not_block_unrelated_sibling_workzone_edit(tmp_path):
    ws_a = tmp_path / "workspaces" / "agent_a"
    ws_b = tmp_path / "personal" / "records"
    ws_a.mkdir(parents=True)
    ws_b.mkdir(parents=True)
    shared_ctx = lambda: {"global_config": SimpleNamespace(bridge_home=tmp_path)}
    reg_a = SimpleNamespace(
        workspace_dir=ws_a,
        access_roots=[tmp_path],
        _effective_audit_context=shared_ctx,
    )
    reg_b = SimpleNamespace(
        workspace_dir=ws_b,
        access_roots=[tmp_path],
        _effective_audit_context=shared_ctx,
    )

    # Agent A runs a test command in ws_a without referencing ws_b or parent traversal.
    async with tool_resource(reg_a, "shell", {"command": "pytest tests/unit.py"}):
        # Agent B edits a file in ws_b with apply_patch/text_replace: must not block.
        async with tool_resource(reg_b, "apply_patch", {"path": str(ws_b / "note.md"), "patch": "@@ -1 +1 @@"}):
            async with tool_resource(reg_b, "text_replace", {"path": str(ws_b / "other.md"), "find": "a", "replace": "b"}):
                pass


@pytest.mark.asyncio
async def test_file_manage_serializes_competing_writes_to_same_destination(tmp_path):
    registry = SimpleNamespace(
        workspace_dir=tmp_path,
        access_roots=[tmp_path],
        _effective_audit_context=lambda: {},
    )
    entered = asyncio.Event()

    async def copy_to_dest():
        async with tool_resource(
            registry,
            "file_manage",
            {"operation": "copy", "source": "src.txt", "destination": "dst.txt"},
        ):
            entered.set()

    async with tool_resource(registry, "file_write", {"path": "dst.txt"}):
        task = asyncio.create_task(copy_to_dest())
        await asyncio.sleep(0.08)
        assert not entered.is_set()
    await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_fingerprint_dispatch_controls_text_replace_and_skips_preview(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    target = work / "sample.txt"
    target.write_text("hello world\n", encoding="utf-8")

    store = CanonicalAuditStore(tmp_path / "home", instance_id="TEST", agent_id="agent")
    registry = ToolRegistry(
        ["text_replace"],
        work,
        work,
        {},
        canonical_audit=store,
        audit_context={
            "instance_id": "TEST",
            "agent_name": "agent",
            "hashi_session_id": "session",
            "hashi_run_id": "run",
            "request_id": "request",
            "fingerprint_policy": FingerprintPolicy(enabled=True).snapshot(),
        },
    )

    preview_res = await dispatch_controlled(
        registry,
        "text_replace",
        {"path": "sample.txt", "find": "world", "replace": "hashi", "preview_only": True},
        tool_call_id="tc-preview",
    )
    assert "fingerprint" not in (getattr(preview_res, "details", None) or {})
    assert target.read_text(encoding="utf-8") == "hello world\n"

    edit_res = await dispatch_controlled(
        registry,
        "text_replace",
        {"path": "sample.txt", "find": "world", "replace": "hashi"},
        tool_call_id="tc-edit",
    )
    assert target.read_text(encoding="utf-8") == "hello hashi\n"
    assert edit_res.details["fingerprint"]["business_status"] == "completed"
    assert edit_res.details["fingerprint"]["evidence_grade"] == "controlled_execution"


# ---------------------------------------------------------------------------
# WP2: Developer toolkit explicit opt-in, on_demand view & prompt guidance
# ---------------------------------------------------------------------------


def test_explicit_toolkit_plus_on_demand_reduces_initial_schemas(tmp_path):
    explicit = [
        "*",
        "tool_find",
        "project_locate",
        "code_search",
        "git_inspect",
        "file_manage",
        "text_replace",
    ]
    registry = ToolRegistry(
        explicit,
        tmp_path,
        tmp_path,
        {"herv3_tool_presentation": {"mode": "on_demand"}},
    )
    full_count = len(registry.get_tool_definitions())
    view = OnDemandToolView(registry)
    starter_names = {d["function"]["name"] for d in view.get_tool_definitions()}
    assert "text_replace" in starter_names
    assert "code_search" in starter_names
    assert "tool_find" in starter_names
    assert len(starter_names) < full_count


def test_v3_prompt_mentions_developer_toolkit_and_parallel_reads():
    system, _user = compile_main_prompt(
        pcm_input={"sections": [], "current_request": "fix bug"},
        fallback_request="fix bug",
        context={},
        tools_available=True,
    )
    assert "text_replace" in system
    assert "code_search" in system
    assert "git_inspect" in system
    assert "independent read-only inspections" in system


# ---------------------------------------------------------------------------
# WP3: Telegram outbox duplicate claim must not crash with KeyError('claim')
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_already_claimed_outbox_does_not_raise_keyerror(tmp_path, monkeypatch):
    import orchestrator.admin_local_testing as local_testing
    from tests.test_telegram_actor_scope import instance

    runtime, _observations, _ = instance(tmp_path)
    store = SessionStore(tmp_path / "delivery.sqlite3", instance_id="dup-claim-test")
    owner = "user:7"
    session = store.ensure_default_session(owner_id=owner, agent_id=runtime.name)
    route = freeze_run_delivery_route(
        message_source_id="telegram",
        session_surface="telegram",
        session_channel_key="7",
        chat_id=7,
        telegram_requested=True,
    )
    accepted = store.accept_run(
        session_id=session["session_id"],
        owner_id=owner,
        agent_id=runtime.name,
        request_id="req-dup-claim-test",
        text="hello",
        source="telegram",
        idempotency_key="dup-claim-key",
        delivery_route=route,
    )
    store.mark_request_running(accepted.request_id, worker_id="worker-a")
    store.finish_request(accepted.request_id, success=True, assistant_text="done")

    # Worker A claims the outbox first.
    first_claim = store.claim_run_delivery_outbox(
        request_id=accepted.request_id,
        owner_id=owner,
        surface="telegram",
        channel_key="7",
        worker_id="sweep-worker-a",
    )
    assert first_claim["state"] == "claimed" and "claim" in first_claim

    # Worker B invokes runtime_delivery.send_long_message while Worker A holds the lease.
    import logging
    runtime.logger = logging.getLogger("test.duplicate_claim")
    runtime.session_store = store
    runtime._request_meta_by_id = {
        accepted.request_id: {"request_id": accepted.request_id, "owner_id": owner}
    }
    monkeypatch.setattr(local_testing, "capture_local_command_output", AsyncMock(return_value=False))
    monkeypatch.setattr(runtime_session, "ensure_store", lambda _rt: store)
    dispatch = AsyncMock(return_value=(0.1, 1))
    monkeypatch.setattr(runtime_delivery, "dispatch_claimed_telegram_event", dispatch)

    result = await runtime_delivery.send_long_message(
        runtime,
        chat_id=7,
        text="done",
        request_id=accepted.request_id,
        frontend_outbox=True,
    )
    assert result == (0.0, 0)
    assert dispatch.await_count == 0


# ---------------------------------------------------------------------------
# WP4: HERV3 auxiliary profile defaults reasoning to "off"
# ---------------------------------------------------------------------------


def test_v3_normalise_and_apply_target_disable_auxiliary_reasoning_by_default():
    raw = {
        "main": {
            "provider": "deepseek-api",
            "model": "deepseek-v4-pro",
            "reasoning": "high",
        }
    }
    norm = normalise_v3_config(raw)
    assert norm["profiles"]["main"]["reasoning"] == "high"
    assert norm["profiles"]["auxiliary"]["reasoning"] == "off"

    switched = apply_v3_target(raw, HERv3ModelTarget("deepseek-api", "deepseek-v4-pro"))
    assert switched["profiles"]["auxiliary"]["reasoning"] == "off"

    # Explicit auxiliary reasoning is still honoured.
    explicit = normalise_v3_config(
        {
            **raw,
            "auxiliary": {
                "provider": "deepseek-api",
                "model": "deepseek-flash",
                "reasoning": "low",
            },
        }
    )
    assert explicit["profiles"]["auxiliary"]["model"] == "deepseek-flash"
    assert explicit["profiles"]["auxiliary"]["reasoning"] == "low"


# ---------------------------------------------------------------------------
# WP5: Bounded intra-turn context maintenance
# ---------------------------------------------------------------------------


def test_intra_turn_compaction_folds_old_bulky_tools_preserving_recent_and_pairing():
    messages: list[dict] = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "fix issue"},
    ]
    for idx in range(16):
        call_id = f"call_{idx}"
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": "file_read",
                            "arguments": json.dumps({"path": f"mod_{idx}.py"}),
                        },
                    }
                ],
            }
        )
        messages.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": f"HEAD_{idx}\n" + ("x" * 5000) + f"\nTAIL_{idx}",
            }
        )

    # Below threshold: untouched (preserves KV cache).
    assert (
        compact_intra_turn_tool_messages(
            messages,
            last_input_tokens=50_000,
            trigger_tokens=110_000,
            trigger_chars=360_000,
            keep_recent_tool_messages=12,
        )
        == 0
    )

    # Above threshold: compacts the 4 oldest tool messages, keeps 12 newest verbatim.
    compacted = compact_intra_turn_tool_messages(
        messages,
        last_input_tokens=120_000,
        trigger_tokens=110_000,
        trigger_chars=360_000,
        keep_recent_tool_messages=12,
    )
    assert compacted == 4
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 16
    assert [m["tool_call_id"] for m in tool_msgs] == [f"call_{i}" for i in range(16)]
    for i in range(4):
        assert tool_msgs[i]["content"].startswith("[HASHI intra-turn compacted tool output")
        assert f"mod_{i}.py" in tool_msgs[i]["content"]
        assert f"HEAD_{i}" in tool_msgs[i]["content"]
        assert f"TAIL_{i}" in tool_msgs[i]["content"]
        assert len(tool_msgs[i]["content"]) < 1000
    for i in range(4, 16):
        assert len(tool_msgs[i]["content"]) > 5000

    # Idempotent: calling again does not re-compact already folded entries.
    assert (
        compact_intra_turn_tool_messages(
            messages,
            last_input_tokens=120_000,
            trigger_tokens=110_000,
            trigger_chars=360_000,
            keep_recent_tool_messages=12,
        )
        == 0
    )


# ---------------------------------------------------------------------------
# WP6: Exponential backoff on transient provider retries
# ---------------------------------------------------------------------------


def test_provider_call_retry_delay_exponential_backoff_and_retry_after():
    err = httpx.ConnectError("network blip")
    assert _provider_call_retry_delay(err, default_s=1.5, maximum_s=16.0, attempt=1) == 1.5
    assert _provider_call_retry_delay(err, default_s=1.5, maximum_s=16.0, attempt=2) == 3.0
    assert _provider_call_retry_delay(err, default_s=1.5, maximum_s=16.0, attempt=3) == 6.0
    assert _provider_call_retry_delay(err, default_s=1.5, maximum_s=16.0, attempt=5) == 16.0
    assert _provider_call_retry_delay(err, default_s=0.0, maximum_s=16.0, attempt=3) == 0.0

    req = httpx.Request("POST", "https://api.deepseek.com/v1/chat/completions")
    http_err = httpx.HTTPStatusError(
        "429",
        request=req,
        response=httpx.Response(429, headers={"Retry-After": "9"}, request=req),
    )
    assert _provider_call_retry_delay(http_err, default_s=1.5, maximum_s=16.0, attempt=1) == 9.0
    assert _provider_call_retry_delay(http_err, default_s=1.5, maximum_s=5.0, attempt=1) == 5.0


# ---------------------------------------------------------------------------
# WP7: Bounded parallel execution for independent read-only tool batches
# ---------------------------------------------------------------------------


def _deepseek_adapter(tmp_path) -> DeepSeekAdapter:
    cfg = SimpleNamespace(
        name="test",
        engine="deepseek-api",
        workspace_dir=tmp_path,
        model="deepseek-v4-pro",
        system_md=None,
        extra={},
    )
    return DeepSeekAdapter(cfg, SimpleNamespace(), api_key="test-key")


@pytest.mark.asyncio
async def test_parallel_readonly_tool_batch_runs_concurrently_and_preserves_order(tmp_path):
    adapter = _deepseek_adapter(tmp_path)
    inflight = 0
    peak_inflight = 0

    class SlowReadRegistry:
        permission_durable_wait = False

        async def execute(self, tool_name, arguments, tool_call_id=""):
            nonlocal inflight, peak_inflight
            inflight += 1
            peak_inflight = max(peak_inflight, inflight)
            try:
                await asyncio.sleep(0.08)
                return ToolResult(
                    tool_call_id=tool_call_id,
                    output=f"read:{arguments['path']}",
                    is_error=False,
                    details={"smart_status": "success"},
                )
            finally:
                inflight -= 1

    adapter.tool_registry = SlowReadRegistry()
    tool_calls = [
        {
            "id": f"tc-{i}",
            "type": "function",
            "function": {"name": "file_read", "arguments": json.dumps({"path": f"f{i}.py"})},
        }
        for i in range(4)
    ]
    messages: list[dict] = [{"role": "assistant", "tool_calls": tool_calls}]
    started = time.perf_counter()
    await adapter._run_tool_calls(tool_calls, messages, None)
    elapsed = time.perf_counter() - started

    assert peak_inflight >= 2, f"expected concurrent read-only execution, got peak={peak_inflight}"
    assert elapsed < 0.25, f"expected parallel wall time < 0.25s, got {elapsed:.3f}s"
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["tc-0", "tc-1", "tc-2", "tc-3"]
    assert [m["content"] for m in tool_msgs] == ["read:f0.py", "read:f1.py", "read:f2.py", "read:f3.py"]


@pytest.mark.asyncio
async def test_mixed_write_and_read_batch_remains_strictly_sequential(tmp_path):
    adapter = _deepseek_adapter(tmp_path)
    inflight = 0
    peak_inflight = 0

    class MixedRegistry:
        permission_durable_wait = False

        async def execute(self, tool_name, arguments, tool_call_id=""):
            nonlocal inflight, peak_inflight
            inflight += 1
            peak_inflight = max(peak_inflight, inflight)
            try:
                await asyncio.sleep(0.04)
                return ToolResult(
                    tool_call_id=tool_call_id,
                    output=f"ok:{tool_name}",
                    is_error=False,
                    details={"smart_status": "success"},
                )
            finally:
                inflight -= 1

    adapter.tool_registry = MixedRegistry()
    tool_calls = [
        {
            "id": "tc-read",
            "type": "function",
            "function": {"name": "file_read", "arguments": json.dumps({"path": "a.py"})},
        },
        {
            "id": "tc-write",
            "type": "function",
            "function": {
                "name": "text_replace",
                "arguments": json.dumps({"path": "a.py", "find": "x", "replace": "y"}),
            },
        },
    ]
    messages: list[dict] = [{"role": "assistant", "tool_calls": tool_calls}]
    await adapter._run_tool_calls(tool_calls, messages, None)
    assert peak_inflight == 1


# ---------------------------------------------------------------------------
# WP8: SmartTool shell classification & missing task delta audit noise
# ---------------------------------------------------------------------------


def test_shell_outcome_does_not_misclassify_exit_zero_output_containing_phrases():
    _spec, ok_timed_out_text = adapt_legacy_result(
        "shell",
        output=(
            "tools/smart_tools.py:645: if 'timed out' in lowered:\n"
            "tools/smart_tools.py:651: if 'blocked by policy' in lowered:"
        ),
        raw_is_error=False,
        details={"exit_code": 0, "foreground_cleanup": {"forced": False, "status": "normal_completion"}},
    )
    assert ok_timed_out_text.status == "success"
    assert ok_timed_out_text.error is None

    _spec, real_timeout = adapt_legacy_result(
        "shell",
        output="Error: command timed out after 60s",
        raw_is_error=False,
        details={"exit_code": 0, "foreground_cleanup": {"forced": True, "status": "force_killed"}},
    )
    assert real_timeout.status == "failed"
    assert real_timeout.error is not None
    assert real_timeout.error.code == "timeout"

    _spec, real_policy = adapt_legacy_result(
        "shell",
        output="Error: command blocked by policy (matched: 'rm -rf')",
        raw_is_error=False,
        details={},
    )
    assert real_policy.status == "failed"
    assert real_policy.error is not None
    assert real_policy.error.code == "permission_denied"


def test_missing_task_delta_does_not_emit_noisy_audit_event_while_real_delta_does(tmp_path):
    audit_path = tmp_path / "her_v2_audit.jsonl"
    audit_log = DurableAuditLog(audit_path, tmp_path / "fallback.jsonl")
    task_state = HERTaskState(goal="Fix harness")
    request = StageRequest(
        turn_id="turn-1",
        request_ref="req-1",
        stage=Stage.DIRECT,
        role="main",
        attempt=1,
        goal="Fix harness",
        classification=None,
        effort=Effort.HIGH,
        allow_tools=True,
        allow_side_effects=True,
        invocation_id="turn-1:direct:1",
        task_state=task_state,
    )
    wrapper = _CognitiveControlToolRegistry(
        ToolRegistry(["file_read"], tmp_path, tmp_path, {}),
        request,
        audit_log=audit_log,
    )

    missing_app = wrapper._apply_task_delta(None, tool_call_id="call-1")
    assert missing_app.status == "missing"
    assert not audit_path.exists() or "task_state_delta" not in audit_path.read_text(encoding="utf-8")

    task_state.observe_evidence(("ev-1",))
    real_app = wrapper._apply_task_delta(
        {
            "delta_id": "delta-1",
            "add_facts": [{"id": "f1", "claim": "verified", "evidence_refs": ["ev-1"]}],
        },
        tool_call_id="call-2",
    )
    assert real_app.status == "accepted"
    lines = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert any(item.get("event") == "task_state_delta" for item in lines)
