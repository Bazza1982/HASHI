"""Non-production regression and parallel acceptance tests for HERV3 / PR #193 residual fixes.

Covers:
  - P1: Scoped Workzone / Shell / File tool locking (orchestrator/execution_resources.py)
  - P2: Partial-known cost accounting & cache savings truth (tools/token_tracker.py, tools/meter_cost.py)
  - P2: Historical Memory state.json compatibility, /memory on, CJK retrieval & agent isolation
  - P2: /steer post-effect SessionConflict handling with real SQLite SessionStore & replay protection
  - Stage 1 & Stage 2: Isolated 4-agent parallel acceptance including SQLite concurrency and H-11 fixed compaction
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator import (
    fresh_context,
    runtime_control,
    runtime_session,
    runtime_usage,
    runtime_workspace,
    ui_language,
)
from orchestrator.bridge_memory import BridgeContextAssembler, BridgeMemoryStore
from orchestrator.context_compaction import (
    CAPSULE_FORMAT,
    ContextCompactionCoordinator,
    install_history_section,
)
from orchestrator.execution_resources import (
    ExecutionResourceTimeout,
    _extract_shell_explicit_dirs,
    _shell_lock_targets,
    leases,
    tool_resource,
)
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator.frontend_command_admission import execute_telegram_command_invocation
from orchestrator.her_v2.backend_session import HerBackendSessionCoordinator
from orchestrator.memory_search_mode import (
    apply_memory_injection_preferences,
    is_memory_search_enabled,
)
from orchestrator.pcm import render_pcm_document
from orchestrator.session_store import SessionConflict, SessionStore
from orchestrator.workspace_state import WorkspaceStateStore
from tools.meter_cost import PerCallUsageLineItem, UsageReceipt, format_cost_tail
from tools.registry import ToolRegistry
from tools.token_tracker import (
    format_summary_text,
    format_usage_cost,
    get_summary,
    get_summary_extended,
    record_usage,
    resolve_usage_cost,
)


# ---------------------------------------------------------------------------
# P1: Workzone cross-agent lock scoping
# ---------------------------------------------------------------------------


def test_p1_shell_lock_targets_ignore_unrelated_shared_workzones(tmp_path: Path):
    projects = tmp_path / "projects"
    repo_a = projects / "repo-a"
    repo_b = projects / "repo-b"
    ws_a = tmp_path / "workspaces" / "agent_a"
    for d in (repo_a, repo_b, ws_a):
        d.mkdir(parents=True)
    drive_root = Path(tmp_path.anchor or "/")

    reg_a = SimpleNamespace(
        workspace_dir=ws_a,
        access_roots=[drive_root, projects, tmp_path / "downloads"],
        _effective_audit_context=lambda: {},
    )
    roots = [str(drive_root), str(projects), str(ws_a)]

    # 1. Explicit `cd repo-a; pytest` locks ONLY repo-a, NOT shared parent `projects` or `drive_root`.
    targets_cd = _shell_lock_targets(
        reg_a,
        {"command": f"cd '{repo_a}'; pytest -q"},
        roots,
    )
    assert targets_cd == [repo_a.resolve()]
    assert projects.resolve() not in targets_cd
    assert drive_root not in targets_cd

    # 2. Explicit `cwd=repo-a` locks ONLY repo-a.
    targets_cwd = _shell_lock_targets(
        reg_a,
        {"command": "pytest -q", "cwd": str(repo_a)},
        roots,
    )
    assert targets_cwd == [repo_a.resolve()]
    assert projects.resolve() not in targets_cwd

    # 3. `git -C repo-a status` locks ONLY repo-a.
    targets_git = _shell_lock_targets(
        reg_a,
        {"command": f"git -C '{repo_a}' status"},
        roots,
    )
    assert targets_git == [repo_a.resolve()]

    # 4. Opaque shell without cwd/cd locks ONLY Agent A's own workspace, NOT shared `projects`.
    targets_default = _shell_lock_targets(reg_a, {"command": "python -m pytest"}, roots)
    assert targets_default == [ws_a.resolve()]
    assert projects.resolve() not in targets_default

    # 5. Invalid `cwd` returns empty list instead of escalating to all Workzones.
    assert _shell_lock_targets(reg_a, {"command": "echo hi", "cwd": "bad\x00path"}, roots) == []

    # 6. WSL `bash -lc 'cd /mnt/c/...'` extracts the Windows target on Windows.
    if os.name == "nt":
        wsl_targets = _extract_shell_explicit_dirs(
            "wsl.exe -d Ubuntu-22.04 -- bash -lc 'cd /mnt/c/Users/thene/projects/HASHI3 && pytest'",
            ws_a,
            "powershell",
        )
        assert wsl_targets == [Path("C:/Users/thene/projects/HASHI3").resolve()]


@pytest.mark.asyncio
async def test_p1_shared_parent_workzone_shell_and_text_replace_run_in_parallel(tmp_path: Path):
    """Agent A running a real long shell in repo-A must not serialize Agent B editing repo-B."""
    projects = tmp_path / "projects"
    repo_a = projects / "repo-a"
    repo_b = projects / "repo-b"
    ws_a = tmp_path / "workspaces" / "agent_a"
    ws_b = tmp_path / "workspaces" / "agent_b"
    for d in (repo_a, repo_b, ws_a, ws_b):
        d.mkdir(parents=True)

    target_b = repo_b / "app.py"
    target_b.write_text("VALUE = 1\n", encoding="utf-8")
    ready = repo_a / "shell_ready"
    release = repo_a / "shell_release"

    shared_ctx = {"global_config": SimpleNamespace(bridge_home=tmp_path)}
    reg_a = ToolRegistry(
        allowed_tools=["shell"],
        workspace_dir=ws_a,
        access_root=projects,
        secrets={},
        audit_context=shared_ctx,
    )
    reg_b = ToolRegistry(
        allowed_tools=["text_replace", "file_manage"],
        workspace_dir=ws_b,
        access_root=projects,
        secrets={},
        audit_context=shared_ctx,
    )

    if os.name == "nt":
        cmd_a = (
            f"cd '{repo_a}'; "
            f"Set-Content -LiteralPath '{ready}' -Value ready; "
            f"while (-not (Test-Path -LiteralPath '{release}')) {{ Start-Sleep -Milliseconds 50 }}; "
            f"Write-Output DONE_A"
        )
    else:
        cmd_a = (
            f"cd '{repo_a}' && printf ready > '{ready}' && "
            f"while [ ! -f '{release}' ]; do sleep 0.05; done && printf DONE_A"
        )

    task_a = asyncio.create_task(reg_a.execute("shell", {"command": cmd_a}, "call-a"))
    try:
        for _ in range(200):
            if ready.exists():
                break
            if task_a.done():
                pytest.fail(f"Agent A shell exited early: {task_a.result().output}")
            await asyncio.sleep(0.05)
        assert ready.exists(), "Agent A shell did not reach running state"

        # Agent B edits repo-B while Agent A's shell is still holding its repo-A lock.
        t0 = time.monotonic()
        res_b = await asyncio.wait_for(
            reg_b.execute(
                "text_replace",
                {"path": str(target_b), "find": "VALUE = 1", "replace": "VALUE = 2", "expected_count": 1},
                "call-b",
            ),
            timeout=5.0,
        )
        elapsed_b = time.monotonic() - t0
        assert not res_b.is_error, res_b.output
        assert "VALUE = 2" in target_b.read_text(encoding="utf-8")
        assert not task_a.done(), "Agent B must finish BEFORE Agent A releases its shell"
        assert elapsed_b < 5.0
    finally:
        release.write_text("go", encoding="utf-8")
        res_a = await asyncio.wait_for(task_a, timeout=15.0)
    assert not res_a.is_error, res_a.output
    assert "DONE_A" in res_a.output


@pytest.mark.asyncio
async def test_p1_same_repo_and_same_file_still_coordinate_and_invalid_args_never_lock_all(tmp_path: Path):
    projects = tmp_path / "projects"
    repo_a = projects / "repo-a"
    repo_b = projects / "repo-b"
    ws_a = tmp_path / "workspaces" / "agent_a"
    ws_b = tmp_path / "workspaces" / "agent_b"
    for d in (repo_a, repo_b, ws_a, ws_b):
        d.mkdir(parents=True)
    shared_file = repo_a / "shared.py"
    shared_file.write_text("x = 1\n", encoding="utf-8")

    shared_ctx = lambda: {"global_config": SimpleNamespace(bridge_home=tmp_path)}
    reg_a = SimpleNamespace(workspace_dir=ws_a, access_roots=[projects], _effective_audit_context=shared_ctx)
    reg_b = SimpleNamespace(workspace_dir=ws_b, access_roots=[projects], _effective_audit_context=shared_ctx)

    # 1. Shell targeting repo-A MUST coordinate with text_replace inside repo-A.
    entered_same_repo = asyncio.Event()

    async def edit_same_repo():
        async with tool_resource(reg_b, "text_replace", {"path": str(shared_file), "find": "1", "replace": "2"}):
            entered_same_repo.set()

    async with tool_resource(reg_a, "shell", {"command": f"cd '{repo_a}'; pytest"}):
        t_same = asyncio.create_task(edit_same_repo())
        await asyncio.sleep(0.15)
        assert not entered_same_repo.is_set(), "overlapping write in same repo must wait for shell"
    await asyncio.wait_for(t_same, timeout=2.0)
    assert entered_same_repo.is_set()

    # 2. Two agents editing the exact same file coordinate.
    entered_same_file = asyncio.Event()

    async def edit_same_file():
        async with tool_resource(reg_b, "file_write", {"path": str(shared_file), "content": "x = 3\n"}):
            entered_same_file.set()

    async with tool_resource(reg_a, "text_replace", {"path": str(shared_file), "find": "1", "replace": "2"}):
        t_file = asyncio.create_task(edit_same_file())
        await asyncio.sleep(0.15)
        assert not entered_same_file.is_set()
    await asyncio.wait_for(t_file, timeout=2.0)
    assert entered_same_file.is_set()

    # 3. Invalid or missing paths MUST NOT escalate to full-Workzone locks.
    invalid_calls = [
        ("file_write", {}),
        ("text_replace", {"path": ""}),
        ("apply_patch", {"path": "bad\x00file"}),
        ("file_manage", {"operation": "unknown"}),
        ("file_manage", {"operation": "copy", "source": str(shared_file)}),
        ("shell", {"cwd": "bad\x00dir", "command": "echo hi"}),
    ]
    for tool_name, bad_args in invalid_calls:
        async with tool_resource(reg_a, tool_name, bad_args):
            # Agent B must remain completely unblocked in repo-A and repo-B.
            async with tool_resource(reg_b, "file_write", {"path": str(repo_b / "ok.txt")}):
                pass
            async with tool_resource(reg_b, "file_write", {"path": str(shared_file)}):
                pass


@pytest.mark.asyncio
async def test_p1_cancellation_timeout_and_exception_release_leases(tmp_path: Path):
    repo_a = tmp_path / "repo-a"
    ws_a = tmp_path / "ws_a"
    repo_a.mkdir()
    ws_a.mkdir()
    target = repo_a / "locked.txt"
    ctx = lambda: {"global_config": SimpleNamespace(bridge_home=tmp_path)}
    reg = SimpleNamespace(workspace_dir=ws_a, access_roots=[repo_a], _effective_audit_context=ctx)

    # 1. Exception inside `async with tool_resource` releases the lease.
    with pytest.raises(RuntimeError, match="boom"):
        async with tool_resource(reg, "file_write", {"path": str(target)}):
            raise RuntimeError("boom")
    async with tool_resource(reg, "file_write", {"path": str(target)}):
        pass

    # 2. Task cancellation (simulating /stop or /steer) releases both active and waiting leases.
    held = asyncio.Event()

    async def hold_until_cancelled():
        async with tool_resource(reg, "shell", {"command": f"cd '{repo_a}'; pytest"}):
            held.set()
            await asyncio.sleep(30)

    holder = asyncio.create_task(hold_until_cancelled())
    await asyncio.wait_for(held.wait(), timeout=2.0)

    # Timeout while waiting raises ExecutionResourceTimeout cleanly without leaking.
    key = "filesystem:" + os.path.normcase(str(repo_a.resolve()))
    with pytest.raises(ExecutionResourceTimeout):
        async with leases(tmp_path / "state" / "instance", [(key, 1, False)], timeout=0.15):
            pass

    holder.cancel()
    with pytest.raises(asyncio.CancelledError):
        await holder

    # Immediately re-acquirable after cancellation.
    async with tool_resource(reg, "file_write", {"path": str(target)}):
        pass


# ---------------------------------------------------------------------------
# P2: Cost Accounting H-08
# ---------------------------------------------------------------------------


def test_p2_partial_unknown_turn_preserves_known_subtotal_and_unknown_call_count(
    tmp_path: Path, monkeypatch
):
    # Ensure cold pricing cache so unpriced calls remain genuinely unknown without network I/O.
    monkeypatch.setenv("HASHI_PRICING_CACHE_FILE", str(tmp_path / "empty-pricing-cache.json"))

    line_items = [
        PerCallUsageLineItem(
            phase="direct",
            engine="deepseek-api",
            model="deepseek-v4-pro",
            input_tokens=98_548,
            output_tokens=2_372,
            token_source="provider",
            thinking_in_output=True,
            cost_usd=None,
            cost_source="unknown",
            status="completed",
        ),
        PerCallUsageLineItem(
            phase="direct",
            engine="deepseek-api",
            model="deepseek-v4-pro",
            input_tokens=0,
            output_tokens=0,
            token_source="provider",
            thinking_in_output=True,
            cost_usd=None,
            cost_source="unknown",
            status="failed_after_partial_response",
        ),
    ]
    # 79 known calls totaling $1.452765 (70 main + 9 persona)
    for _ in range(70):
        line_items.append(
            PerCallUsageLineItem(
                phase="direct",
                engine="deepseek-api",
                model="deepseek-v4-pro",
                input_tokens=140_000,
                output_tokens=750,
                prompt_cache_hit_tokens=130_000,
                prompt_cache_miss_tokens=10_000,
                token_source="provider",
                thinking_in_output=True,
                cost_usd=0.02075,
                cost_source="openrouter_reference",
                pricing_revision="openrouter:rev-1",
            )
        )
    for _ in range(9):
        line_items.append(
            PerCallUsageLineItem(
                phase="persona",
                engine="deepseek-api",
                model="deepseek-flash",
                input_tokens=280,
                output_tokens=40,
                prompt_cache_hit_tokens=128,
                prompt_cache_miss_tokens=152,
                token_source="provider",
                thinking_in_output=True,
                cost_usd=0.00002944,
                cost_source="openrouter_reference",
                pricing_revision="openrouter:rev-2",
            )
        )

    expected_known = round(70 * 0.02075 + 9 * 0.00002944, 6)
    receipt = record_usage(
        tmp_path,
        model="deepseek-v4-pro",
        backend="her-v2",
        engine="deepseek-api",
        input_tokens=0,
        output_tokens=0,
        session_id="sess-test-1",
        request_id="req-test-0003",
        line_items=line_items,
    )

    assert receipt.cost_usd is None
    assert receipt.known_cost_usd == pytest.approx(expected_known)
    assert receipt.known_cost_requests == 79
    assert receipt.unknown_cost_requests == 2
    # Cache savings cannot be fabricated when pricing cache is empty/unknown.
    assert receipt.cache_savings_usd is None

    persisted = json.loads((tmp_path / "token_usage.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert persisted["cost_usd"] is None
    assert persisted["cost_known"] is False
    assert persisted["cost_source"] == "unknown"
    assert persisted["known_cost_usd"] == pytest.approx(expected_known)
    assert persisted["known_cost_requests"] == 79
    assert persisted["unknown_cost_requests"] == 2
    assert persisted["cache_savings_usd"] is None

    # /meter rendering shows both known subtotal and 2 unknown calls, and cache savings unavailable.
    meter_en = format_cost_tail(receipt, locale="en")
    assert "known subtotal" in meter_en.casefold()
    assert "1.45" in meter_en
    assert "2 requests cost pending" in meter_en.casefold()
    assert "unavailable" in meter_en.casefold()

    meter_zh = format_cost_tail(receipt, locale="zh-CN")
    assert "已知小计" in meter_zh
    assert "1.45" in meter_zh
    assert "2" in meter_zh
    assert "不可用" in meter_zh

    # /usage summary retains the $1.4528 known subtotal and unknown call count = 2.
    summary = get_summary(tmp_path, session_id="sess-test-1")
    ext = get_summary_extended(tmp_path, session_id="sess-test-1")
    for block in (summary["all_time"], summary["session"], ext["all_time"]):
        assert block["cost_usd"] == pytest.approx(expected_known)
        assert block["known_cost_usd"] == pytest.approx(expected_known)
        assert block["known_cost_requests"] == 79
        assert block["unknown_cost_requests"] == 2

    with ui_language.language_scope(SimpleNamespace(), locale="en"):
        usage_en = format_summary_text(summary, agent_name="test_agent")
        assert "Known subtotal $1.4528" in usage_en
        assert "cost unknown for 2 requests" in usage_en
    with ui_language.language_scope(SimpleNamespace(), locale="zh-CN"):
        usage_zh = format_summary_text(summary, agent_name="test_agent")
        assert "已知小计 $1.4528" in usage_zh
        assert "2 次请求成本未知" in usage_zh


def test_p2_legacy_token_usage_jsonl_compatibility_and_cold_cache_nonblocking(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("HASHI_PRICING_CACHE_FILE", str(tmp_path / "nonexistent-cache.json"))

    # Cold cache must return unknown immediately without raising or blocking.
    t0 = time.monotonic()
    cost, source, rev = resolve_usage_cost(
        cost_usd=None,
        model="deepseek-v4-pro",
        engine="deepseek-api",
        input_tokens=50_000,
        output_tokens=1_000,
        cached_tokens=40_000,
        schedule_missing=False,
    )
    assert (time.monotonic() - t0) < 1.0
    assert (cost, source, rev) == (None, "unknown", "unknown")

    # Legacy JSONL records (pre-fix schema without known_cost_usd / unknown_cost_requests).
    legacy_lines = [
        {"ts": "2026-10-01T00:00:00+00:00", "model": "m1", "backend": "her-v2", "input": 1000, "output": 100, "thinking": 0, "cost_usd": 0.25, "session_id": "s1"},
        {"ts": "2026-10-01T00:01:00+00:00", "model": "m1", "backend": "her-v2", "input": 500, "output": 50, "thinking": 0, "cost_usd": None, "session_id": "s1"},
        {"ts": "2026-10-01T00:02:00+00:00", "model": "local", "backend": "ollama-api", "input": 200, "output": 20, "thinking": 0, "cost_usd": 0.0, "session_id": "s1"},
    ]
    (tmp_path / "token_usage.jsonl").write_text(
        "\n".join(json.dumps(r) for r in legacy_lines) + "\n",
        encoding="utf-8",
    )

    summary = get_summary(tmp_path, session_id="s1")
    all_t = summary["all_time"]
    assert all_t["cost_usd"] == pytest.approx(0.25)
    assert all_t["known_cost_usd"] == pytest.approx(0.25)
    assert all_t["unknown_cost_requests"] == 1
    assert all_t["requests"] == 3
    with ui_language.language_scope(SimpleNamespace(), locale="en"):
        assert format_usage_cost(all_t) == "Known subtotal $0.2500 · cost unknown for 1 request"
        assert format_usage_cost(summary["by_model"]["local"]) == "$0.0000"


# ---------------------------------------------------------------------------
# P2: Memory historical config compatibility, CJK search & isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p2_memory_legacy_state_json_recovery_cjk_and_isolation(tmp_path: Path):
    ws_legacy = tmp_path / "agent_legacy"
    ws_fresh = tmp_path / "agent_fresh"
    ws_peer = tmp_path / "agent_peer"
    for d in (ws_legacy, ws_fresh, ws_peer):
        d.mkdir(parents=True)

    # Simulate legacy state.json (mirrors historical persisted config with enabled=false + fresh_context suppression).
    WorkspaceStateStore(ws_legacy).update(
        lambda state: {
            **state,
            "memory_search": {
                "version": 1,
                "enabled": False,
                "updated_at": "2026-08-26T20:40:38.205774Z",
            },
            "fresh_context": {
                "version": 1,
                "generation": 1,
                "cutoff_epoch": 1787776838.135236,
                "created_at": "2026-08-26T20:40:38.135236Z",
                "automatic_context_suppressed": True,
                "habit_context_suppressed": True,
            },
        }
    )

    # 1. Fresh workspace defaults to ON; legacy explicit false is respected.
    assert is_memory_search_enabled(ws_fresh) is True
    assert is_memory_search_enabled(ws_legacy) is False

    pcm_legacy = ws_legacy / "agent.md"
    pcm_legacy.write_text(render_pcm_document(persona="测试代理", system="系统指令", memory=""), encoding="utf-8")
    store_legacy = BridgeMemoryStore(ws_legacy)
    assembler_legacy = BridgeContextAssembler(store_legacy, pcm_legacy)
    apply_memory_injection_preferences(assembler_legacy, ws_legacy)
    assert assembler_legacy.saved_memory_injection_enabled is False

    replies: list[str] = []

    async def fake_reply(_update, text, **_kwargs):
        replies.append(text)

    runtime_legacy = SimpleNamespace(
        name="agent_legacy",
        workspace_dir=ws_legacy,
        config=SimpleNamespace(workspace_dir=ws_legacy, name="agent_legacy"),
        backend_manager=SimpleNamespace(agent_mode="fixed", state_store=WorkspaceStateStore(ws_legacy)),
        memory_store=store_legacy,
        context_assembler=assembler_legacy,
        _is_authorized_user=lambda _uid: True,
        _reply_text=fake_reply,
    )
    assert fresh_context.automatic_context_suppressed(runtime_legacy) is True

    update = SimpleNamespace(effective_user=SimpleNamespace(id=1), effective_chat=SimpleNamespace(id=42))

    # 2. `/memory on` restores saved_memory_injection AND clears automatic_context_suppressed.
    await runtime_workspace.cmd_memory(runtime_legacy, update, SimpleNamespace(args=["on"]))
    assert is_memory_search_enabled(ws_legacy) is True
    assert assembler_legacy.saved_memory_injection_enabled is True
    assert fresh_context.automatic_context_suppressed(runtime_legacy) is False

    # 3. CJK memory write, retrieval, and prompt injection on agent_legacy.
    store_legacy.record_memory(
        "preference",
        "chat",
        "爸爸最喜欢的颜色是黄绿色 chartreuse，非生产测试必须严格隔离。",
        importance=1.0,
    )
    hits = store_legacy.retrieve_memories("爸爸喜欢什么颜色", limit=5)
    assert hits, "CJK memory search must return relevant hits"
    assert any("黄绿色" in h.get("content", "") for h in hits)

    assembled = assembler_legacy.build_prompt("爸爸喜欢什么颜色？", "her-v2")
    assert "黄绿色" in assembled

    # 4. Strict isolation: peer agent cannot see legacy agent's memories.
    pcm_peer = ws_peer / "agent.md"
    pcm_peer.write_text(render_pcm_document(persona="Peer", system="Sys", memory=""), encoding="utf-8")
    store_peer = BridgeMemoryStore(ws_peer)
    assembler_peer = BridgeContextAssembler(store_peer, pcm_peer)
    apply_memory_injection_preferences(assembler_peer, ws_peer)
    assert store_peer.retrieve_memories("黄绿色", limit=5) == []
    assert "黄绿色" not in assembler_peer.build_prompt("爸爸喜欢什么颜色？", "her-v2")

    # 5. User can explicitly disable memory again and it persists across reloads.
    await runtime_workspace.cmd_memory(runtime_legacy, update, SimpleNamespace(args=["search", "off"]))
    assert is_memory_search_enabled(ws_legacy) is False
    assert assembler_legacy.saved_memory_injection_enabled is False


# ---------------------------------------------------------------------------
# P2: /steer SessionConflict on real SQLite SessionStore
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p2_real_session_store_steer_transport_conflict_completes_and_blocks_replay(
    tmp_path: Path, monkeypatch
):
    """Reproduce exact worker-53792 SessionConflict on /steer reply using real SessionStore."""
    import orchestrator.admin_local_testing as local_testing
    import orchestrator.flexible_agent_runtime as agent_runtime
    from tests.test_frontend_command_admission import _Runtime, _update

    runtime = _Runtime(tmp_path)
    runtime.name = "test_her_agent"
    runtime.global_config.project_root = tmp_path
    runtime.app = SimpleNamespace(bot=None)
    runtime.telegram_logger = SimpleNamespace(warning=lambda *a, **k: None)
    runtime.logger = SimpleNamespace(warning=lambda *a, **k: None, info=lambda *a, **k: None)
    runtime.config = SimpleNamespace(active_backend="her-v2", engine="her-v2")
    runtime.queue = asyncio.Queue()
    runtime.session_store = SessionStore(tmp_path / "sessions.sqlite3", instance_id="HASHI_TEST")

    monkeypatch.setattr(local_testing, "local_command_capture", lambda *_a: None)
    monkeypatch.setattr(agent_runtime, "apply_disable_notification_default", lambda *_a: None)
    monkeypatch.setattr(
        agent_runtime.telegram_delivery_failover,
        "handle_blocked_send",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(agent_runtime.asyncio, "sleep", AsyncMock())

    update = _update()
    update.update_id = 91001
    update.message.message_id = 13220
    update.effective_chat.id = 42
    update.message.chat = update.effective_chat
    update.message.text = "/steer 不用管手机输入法 单独处理"
    # Telegram succeeds and returns message_id=7777 — which is ALREADY bound to an earlier Event!
    update.message.reply_text = AsyncMock(return_value=SimpleNamespace(message_id=7777))

    # Pre-bind transport_message_id="7777" to an existing canonical assistant Event
    # by delivering a prior reply through the real _reply_text pipeline.
    session = runtime_session.current_session_for_update(runtime, update)
    owner = runtime_session.owner_id(runtime)
    await FlexibleAgentRuntime._reply_text(runtime, update, "Prior assistant message")
    assert update.message.reply_text.await_count == 1
    with sqlite3.connect(tmp_path / "sessions.sqlite3") as conn:
        conn.row_factory = sqlite3.Row
        prior_row = conn.execute(
            "SELECT event_id, endpoint_id FROM frontend_transport_references "
            "WHERE connector_id='telegram' AND transport_message_id='7777'"
        ).fetchone()
        assert prior_row is not None
        prior_event_id = str(prior_row["event_id"])
        prior_endpoint_id = str(prior_row["endpoint_id"])
    # Reset await count so we measure the /steer invocation cleanly.
    update.message.reply_text.reset_mock()

    # Wire real FlexibleAgentRuntime._reply_text onto our test runtime.
    async def bound_reply(upd, text, **kwargs):
        return await FlexibleAgentRuntime._reply_text(runtime, upd, text, **kwargs)

    runtime._reply_text = bound_reply

    # Simulate an active running turn and track steer execution + continuation creation.
    shutdown = AsyncMock()
    enqueued_continuations: list[dict] = []
    created_run_id = "run-steer-successor-1"

    async def fake_enqueue(chat_id, prompt, source, summary, **kwargs):
        req_id = f"req-steer-{len(enqueued_continuations) + 1}"
        enqueued_continuations.append({"req_id": req_id, "chat_id": chat_id, "source": source, "summary": summary})
        return req_id

    runtime.backend_manager = SimpleNamespace(
        current_backend=SimpleNamespace(shutdown=shutdown),
        initialize_active_backend=AsyncMock(return_value=True),
    )
    runtime.current_request_meta = {
        "request_id": "req-active-1",
        "hashi_session_id": str(session["session_id"]),
        "chat_id": 42,
        "prompt": "Original task",
        "source": "session-api",
        "summary": "Original task",
        "hashi_run_id": "run-old-1",
    }
    runtime.last_prompt = SimpleNamespace(prompt="Original task", chat_id=42)
    runtime.is_generating = True
    runtime._notify_right_brain_interrupted = lambda *a, **k: None
    runtime.enqueue_request = fake_enqueue

    orig_get_run = runtime.session_store.get_run_by_request
    monkeypatch.setattr(
        runtime.session_store,
        "get_run_by_request",
        lambda request_id, **kw: {"request_id": request_id, "run_id": created_run_id}
        if str(request_id).startswith("req-steer-")
        else orig_get_run(request_id, **kw),
    )

    ctx = SimpleNamespace(args=["不用管手机输入法", "单独处理"])
    invoke_calls = 0

    async def invoke_steer():
        nonlocal invoke_calls
        invoke_calls += 1
        return await runtime_control.cmd_steer(runtime, update, ctx)

    # 1. First Telegram invocation: /steer executes, reply sends (msg 7777), reference binding conflicts.
    first = await execute_telegram_command_invocation(
        runtime,
        update,
        command_name="steer",
        arguments=ctx.args,
        transport_id=91001,
        invoke_handler=invoke_steer,
        command_known=True,
    )

    assert first["ok"] is True
    assert first["execution_state"] == "completed"
    assert first["transport_delivery_state"] == "sent_reference_conflict"
    assert first["result"]["status"] == "accepted"
    assert first["result"]["new_run"]["created"] is True
    assert first["result"]["reply_delivery"]["sent"] is True
    assert first["result"]["reply_delivery"]["transport_message_id"] == "7777"
    assert first["result"]["reply_delivery"]["error_code"] == "telegram_transport_reference_conflict"
    assert invoke_calls == 1
    assert len(enqueued_continuations) == 1
    assert update.message.reply_text.await_count == 1

    # 2. Conflicting Event binding in SessionStore MUST NOT be overwritten!
    with sqlite3.connect(tmp_path / "sessions.sqlite3") as conn:
        conn.row_factory = sqlite3.Row
        ref_row = conn.execute(
            "SELECT event_id FROM frontend_transport_references "
            "WHERE connector_id='telegram' AND endpoint_id=? AND transport_message_id='7777'",
            (prior_endpoint_id,),
        ).fetchone()
        assert ref_row is not None
        assert str(ref_row["event_id"]) == prior_event_id

        outbox_rows = [
            dict(r)
            for r in conn.execute(
                "SELECT state, last_error_code FROM connector_delivery_tasks ORDER BY created_at"
            ).fetchall()
        ]
        assert any(
            r["state"] == "unknown" and r["last_error_code"] == "telegram_transport_reference_conflict"
            for r in outbox_rows
        )

    # 3. Direct SessionStore conflict guard remains enforced.
    with pytest.raises(SessionConflict, match="already bound to another Event"):
        runtime.session_store.record_frontend_transport_reference(
            session_id=str(session["session_id"]),
            owner_id=owner,
            connector_id="telegram",
            endpoint_id=prior_endpoint_id,
            transport_message_id="7777",
            event_id="evt-different-attempt",
        )

    # 4. Duplicate Telegram replay MUST NOT re-execute /steer, MUST NOT duplicate continuation,
    #    and MUST NOT be misclassified as outcome_unknown!
    replay = await execute_telegram_command_invocation(
        runtime,
        update,
        command_name="steer",
        arguments=ctx.args,
        transport_id=91001,
        invoke_handler=invoke_steer,
        command_known=True,
    )
    assert replay["replayed"] is True
    assert replay.get("outcome_unknown") is not True
    assert replay["ok"] is True
    assert replay["execution_state"] == "completed"
    assert invoke_calls == 1
    assert len(enqueued_continuations) == 1
    assert update.message.reply_text.await_count == 1


# ---------------------------------------------------------------------------
# Stage 2: 4-Agent Isolated Non-Production Parallel Acceptance (+ H-11 check)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage2_four_isolated_herv3_agents_parallel_acceptance(tmp_path: Path, monkeypatch):
    """Run 4 isolated non-production her-v3 workspaces concurrently covering:
    cross-repo shell/text_replace/file_manage, SQLite concurrency, CJK memory,
    partial-unknown billing, /steer+/stop lease safety, and H-11 fixed compaction.
    """
    monkeypatch.setenv("HASHI_PRICING_CACHE_FILE", str(tmp_path / "isolated-pricing-cache.json"))

    projects = tmp_path / "projects"
    projects.mkdir()
    bridge_home = tmp_path / "nonprod_instance"
    bridge_home.mkdir()
    shared_ctx = {"global_config": SimpleNamespace(bridge_home=bridge_home)}

    agent_names = ("her_alpha", "her_beta", "her_gamma", "her_delta")
    codenames = ("青龙", "白虎", "朱雀", "玄武")
    start_barrier = asyncio.Event()

    async def run_agent(idx: int, name: str) -> dict:
        codename = codenames[idx]
        ws = bridge_home / "workspaces" / name
        repo = projects / f"repo_{idx}"
        ws.mkdir(parents=True)
        repo.mkdir(parents=True)

        src_file = repo / "module.py"
        src_file.write_text(f"AGENT = '{name}'\nCOUNT = 0\n", encoding="utf-8")

        reg = ToolRegistry(
            allowed_tools=["shell", "text_replace", "file_manage"],
            workspace_dir=ws,
            access_root=projects,
            secrets={},
            audit_context=shared_ctx,
        )

        # 1. Parallel cross-repo shell + text_replace + file_manage under shared `projects/`.
        await start_barrier.wait()
        shell_cmd = (
            f"cd '{repo}'; Start-Sleep -Milliseconds 150; Write-Output '{name}_ok'"
            if os.name == "nt"
            else f"cd '{repo}' && sleep 0.15 && printf '{name}_ok'"
        )
        t_start = time.monotonic()
        res_shell = await reg.execute("shell", {"command": shell_cmd}, f"{name}-sh")
        assert not res_shell.is_error, res_shell.output
        assert f"{name}_ok" in res_shell.output

        res_rep = await reg.execute(
            "text_replace",
            {"path": str(src_file), "find": "COUNT = 0", "replace": f"COUNT = {idx + 1}", "expected_count": 1},
            f"{name}-rep",
        )
        assert not res_rep.is_error, res_rep.output

        res_cp = await reg.execute(
            "file_manage",
            {"operation": "copy", "source": str(src_file), "destination": str(repo / "module_copy.py")},
            f"{name}-cp",
        )
        assert not res_cp.is_error, res_cp.output
        tool_elapsed = time.monotonic() - t_start

        # 2. Isolated SQLite concurrency: BridgeMemoryStore (CJK) + HerSessionStore + SessionStore.
        mem_store = BridgeMemoryStore(ws)
        mem_store.record_memory(
            "fact",
            "chat",
            f"{name} 专属中文记忆：测试代号 {codename}，互不串库。",
            importance=1.0,
        )
        cjk_hits = mem_store.retrieve_memories(codename, limit=3)
        assert any(codename in h.get("content", "") for h in cjk_hits)

        s_store = SessionStore(ws / "sessions.sqlite3", instance_id="NONPROD")
        sess = s_store.create_session(
            owner_id="user:1",
            agent_id=name,
        )
        assert sess["session_id"]

        # 3. Partial-unknown cost accounting in isolated workspace.
        rec = record_usage(
            ws,
            model="deepseek-v4-pro",
            backend="her-v2",
            engine="deepseek-api",
            input_tokens=0,
            output_tokens=0,
            session_id=f"s-{name}",
            request_id=f"req-{name}-1",
            line_items=[
                PerCallUsageLineItem(
                    phase="direct",
                    engine="deepseek-api",
                    model="deepseek-v4-pro",
                    input_tokens=10_000,
                    output_tokens=500,
                    token_source="provider",
                    thinking_in_output=True,
                    cost_usd=0.05,
                    cost_source="openrouter_reference",
                ),
                PerCallUsageLineItem(
                    phase="direct",
                    engine="deepseek-api",
                    model="deepseek-v4-pro",
                    input_tokens=0,
                    output_tokens=0,
                    token_source="provider",
                    thinking_in_output=True,
                    cost_usd=None,
                    cost_source="unknown",
                    status="failed_after_partial_response",
                ),
            ],
        )
        assert rec.cost_usd is None
        assert rec.known_cost_usd == pytest.approx(0.05)
        assert rec.unknown_cost_requests == 1
        summary = get_summary(ws, session_id=f"s-{name}")
        assert summary["all_time"]["cost_usd"] == pytest.approx(0.05)
        assert summary["all_time"]["unknown_cost_requests"] == 1

        # 4. H-11 Fixed-Mode Compaction + /compact force verification (isolated workspace, no H-11 edits).
        pcm = ws / "agent.md"
        pcm.write_text(render_pcm_document(persona=name, system="System", memory=""), encoding="utf-8")
        for turn_i in range(10):
            mem_store.record_turn("user", "text", f"[{name}] Turn {turn_i} " + ("工作内容 " * 40))
            mem_store.record_turn("assistant", "her-v2", f"[{name}] Reply {turn_i} " + ("结果记录 " * 40))

        HerBackendSessionCoordinator(
            ws / "backend_state" / "her_v2",
            execution_owner={"owner_id": f"owner-{name}", "owner_pid": os.getpid(), "generation_id": "g-test"},
        )
        state_store = WorkspaceStateStore(ws)
        manager = SimpleNamespace(
            state_store=state_store,
            agent_mode="fixed",
            privacy_level=1,
            config=SimpleNamespace(
                active_backend="her-v2",
                allowed_backends=[
                    {
                        "engine": "deepseek-api",
                        "model": "deepseek-v4-pro",
                        "models": ["deepseek-flash", "deepseek-v4-pro"],
                        "context_window_tokens": 128_000,
                        "response_headroom_tokens": 16_384,
                    }
                ],
            ),
            get_her_v2_configuration=lambda: SimpleNamespace(
                provider="deepseek-api",
                fast_provider="deepseek-api",
                fast_model="deepseek-flash",
                pro_model="deepseek-v4-pro",
            ),
        )

        async def _empty_sections(*_a, **_kw):
            return []

        rt = SimpleNamespace(
            name=name,
            workspace_dir=ws,
            config=SimpleNamespace(active_backend="her-v2", system_md=pcm, name=name),
            global_config=SimpleNamespace(bridge_home=bridge_home, timezone="UTC", instance_id="NONPROD"),
            memory_store=mem_store,
            context_assembler=BridgeContextAssembler(mem_store, pcm),
            backend_manager=manager,
            _get_active_skill_sections=lambda: [],
            _get_available_skill_catalogue=lambda: [],
            _get_available_tool_catalogue=lambda: [],
            _workzone_prompt_section=lambda: [],
            _request_meta_by_id={},
            _build_pre_turn_context_sections=_empty_sections,
        )
        sections, snapshot = install_history_section(rt, [])
        assert snapshot is not None and len(sections) == 1

        async def _compact_invoker(_route, req, _sys, _usr):
            return SimpleNamespace(
                text=json.dumps(
                    {
                        "format": CAPSULE_FORMAT,
                        "source_segment_ids": list(req.source_segment_ids),
                        "source_digest": req.source_digest,
                        "active_historical_goals": [f"Goal for {name}"],
                        "decisions_and_constraints": [],
                        "completed_work_and_verification": [],
                        "unresolved_work_questions_failures": [],
                        "evidence_refs": [],
                        "preferences_and_definitions": [],
                        "omissions_and_uncertainty": [],
                        "summary": f"Compacted fixed history for {name}.",
                    }
                ),
                structured_data=None,
            )

        compactor = ContextCompactionCoordinator(rt, invoker=_compact_invoker)
        # /compact force below 64k window must succeed in fixed mode!
        compact_outcome = await compactor.compact(
            trigger="manual_command",
            request_ref=f"req-{name}-compact",
            force=True,
        )
        assert compact_outcome.status == "completed"
        assert compact_outcome.changed is True

        return {"agent": name, "tool_elapsed": tool_elapsed, "compacted": compact_outcome.changed}

    tasks = [asyncio.create_task(run_agent(i, n)) for i, n in enumerate(agent_names)]
    start_barrier.set()
    results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=30.0)
    assert len(results) == 4
    assert all(r["compacted"] is True for r in results)

    # Confirm cross-agent memory isolation across all 4 agents.
    for i, name in enumerate(agent_names):
        ws = bridge_home / "workspaces" / name
        other_code = codenames[(i + 1) % len(codenames)]
        other_hits = BridgeMemoryStore(ws).retrieve_memories(other_code, limit=3)
        assert not any(other_code in h.get("content", "") for h in other_hits)
