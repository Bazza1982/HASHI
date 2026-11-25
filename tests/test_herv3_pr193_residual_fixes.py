"""Non-production regression and parallel acceptance tests for HERV3 / PR #193 residual fixes
and PR #196 code-review remediation.

Covers:
  - Task 1 (P1): Remove unjustified Shell directory locks; file-level coordination only
  - Task 2 (P1): Multiline Bash & PowerShell command target recognition (real execution)
  - Task 3 (P2): No fake `cwd` parameter assumption; real ToolRegistry.execute() acceptance
  - Task 4 (P2): Truthful /steer & /focus reply delivery statuses across delivered,
                 suppressed, RetryAfter, pre-submit failure, post-submit outcome_unknown,
                 sent_reference_conflict, and duplicate Telegram replay
  - Task 5 (P3): Cost accounting zero-call boundary across all 5 cases (79+2, 1+0, 0+1, 0+0, legacy)
  - Task 6 (PIT): 4-agent real-subprocess barrier test proving true temporal overlap
                  (including same-repo unrelated files), same-file coordination,
                  cancel/timeout/exception lease cleanup, SQLite concurrency, and H-11 compaction
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

import httpx
import pytest
from telegram.error import NetworkError, RetryAfter, TimedOut

from orchestrator import (
    fresh_context,
    runtime_control,
    runtime_session,
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
    _shell_file_write_targets,
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
from tools.meter_cost import PerCallUsageLineItem, format_cost_tail
from tools.registry import ToolRegistry
from tools.schemas import TOOL_SCHEMAS
from orchestrator.process_execution import resolve_shell_invocation
from tools.token_tracker import (
    format_summary_text,
    format_usage_cost,
    get_summary,
    get_summary_extended,
    record_usage,
    resolve_usage_cost,
)


# ---------------------------------------------------------------------------
# Task 1, 2, 3 (P1/P2): File-only shell targets, multiline resolution, no fake cwd
# ---------------------------------------------------------------------------


def test_p1_shell_file_write_targets_only_concrete_files_and_multiline_resolution(tmp_path: Path):
    projects = tmp_path / "projects"
    repo_a = projects / "repo-a"
    ws_a = tmp_path / "workspaces" / "agent_a"
    for d in (repo_a, ws_a):
        d.mkdir(parents=True)

    # Confirm the real `shell` schema does NOT expose a `cwd` parameter (Task 3).
    shell_schema = next(s["function"] for s in TOOL_SCHEMAS if s["function"]["name"] == "shell")
    assert "cwd" not in shell_schema["parameters"]["properties"]

    # 1. Opaque / directory-changing commands without literal write targets MUST NOT lock any directory.
    for cmd in (
        "pytest -q",
        f"cd '{repo_a}'; pytest -q",
        f"Set-Location '{repo_a}'\r\npytest -q",
        f"git -C '{repo_a}' status",
        "python update_project.py",
    ):
        assert _shell_file_write_targets(cmd, ws_a.resolve()) == [], cmd

    # 2. Multiline Bash (\n, blank lines, ;\n) tracks `cd` to resolve ONLY the concrete redirect file.
    bash_multiline = f"echo step1\n\ncd '{repo_a}';\necho hello > shared.txt\n"
    assert _shell_file_write_targets(bash_multiline, ws_a.resolve(), "bash") == [
        (repo_a / "shared.txt").resolve()
    ]

    # 3. Multiline PowerShell (\r\n) tracks `Set-Location` to resolve ONLY the concrete redirect file.
    ps_multiline = (
        'Write-Output "step1"\r\n'
        f"Set-Location -LiteralPath '{repo_a}'\r\n"
        'Write-Output "hello" >> shared.txt\r\n'
    )
    assert _shell_file_write_targets(ps_multiline, ws_a.resolve(), "powershell") == [
        (repo_a / "shared.txt").resolve()
    ]

    # 4. `git -C repo-a diff --output=shared.py` locks ONLY repo-a/shared.py (NOT repo-a);
    #    `git -C repo-a diff > root.patch` resolves the shell redirect against workspace_dir.
    assert _shell_file_write_targets(
        f"git -C '{repo_a}' diff --output=shared.py", ws_a.resolve()
    ) == [(repo_a / "shared.py").resolve()]
    assert _shell_file_write_targets(
        f"git -C '{repo_a}' diff > root.patch", ws_a.resolve()
    ) == [(ws_a / "root.patch").resolve()]

    # 5. Nested WSL bash -lc extracts ONLY concrete file redirects, never directories.
    if os.name == "nt":
        assert (
            _shell_file_write_targets(
                "wsl.exe -d Ubuntu-22.04 -- bash -lc 'cd /mnt/c/hashi-fixtures/synthetic-repo && pytest'",
                ws_a.resolve(),
                "powershell",
            )
            == []
        )
        assert _shell_file_write_targets(
            "wsl.exe -d Ubuntu-22.04 -- bash -lc 'cd /mnt/c/hashi-fixtures/synthetic-repo\necho ok > out.txt'",
            ws_a.resolve(),
            "powershell",
        ) == [Path("C:/hashi-fixtures/synthetic-repo/out.txt").resolve()]


@pytest.mark.asyncio
async def test_p1_real_tool_registry_same_repo_shell_and_unrelated_file_edit_run_in_parallel(
    tmp_path: Path,
):
    """Real ToolRegistry.execute():
    - Agent A runs a long shell command inside repo-a.
    - Agent B edits repo-a/docs/README.md (SAME repo!) concurrently without waiting for A.
    - Agent C edits repo-b/app.py (sibling repo under shared projects/) concurrently.
    """
    projects = tmp_path / "projects"
    repo_a = projects / "repo-a"
    repo_b = projects / "repo-b"
    docs_a = repo_a / "docs"
    ws_a = tmp_path / "workspaces" / "agent_a"
    ws_b = tmp_path / "workspaces" / "agent_b"
    ws_c = tmp_path / "workspaces" / "agent_c"
    for d in (docs_a, repo_b, ws_a, ws_b, ws_c):
        d.mkdir(parents=True)

    readme_a = docs_a / "README.md"
    readme_a.write_text("# Repo A\nSTATUS = draft\n", encoding="utf-8")
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
        allowed_tools=["text_replace", "file_write"],
        workspace_dir=ws_b,
        access_root=projects,
        secrets={},
        audit_context=shared_ctx,
    )
    reg_c = ToolRegistry(
        allowed_tools=["text_replace"],
        workspace_dir=ws_c,
        access_root=projects,
        secrets={},
        audit_context=shared_ctx,
    )

    if os.name == "nt":
        cmd_a = (
            f"Set-Location -LiteralPath '{repo_a}'\r\n"
            f"Set-Content -LiteralPath '{ready}' -Value ready\r\n"
            f"while (-not (Test-Path -LiteralPath '{release}')) {{ Start-Sleep -Milliseconds 50 }}\r\n"
            "Write-Output DONE_A"
        )
    else:
        cmd_a = (
            f"cd '{repo_a}'\n"
            f"printf ready > '{ready}'\n"
            f"while [ ! -f '{release}' ]; do sleep 0.05; done\n"
            "printf DONE_A"
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

        # Agent B edits repo-a/docs/README.md (SAME repo!) while Agent A's shell is still running.
        res_b, res_c = await asyncio.wait_for(
            asyncio.gather(
                reg_b.execute(
                    "text_replace",
                    {
                        "path": str(readme_a),
                        "find": "STATUS = draft",
                        "replace": "STATUS = published",
                        "expected_count": 1,
                    },
                    "call-b-same-repo",
                ),
                reg_c.execute(
                    "text_replace",
                    {
                        "path": str(target_b),
                        "find": "VALUE = 1",
                        "replace": "VALUE = 2",
                        "expected_count": 1,
                    },
                    "call-c-other-repo",
                ),
            ),
            timeout=5.0,
        )
        assert not res_b.is_error, res_b.output
        assert not res_c.is_error, res_c.output
        assert "STATUS = published" in readme_a.read_text(encoding="utf-8")
        assert "VALUE = 2" in target_b.read_text(encoding="utf-8")
        assert not task_a.done(), "Agent B & C MUST complete while Agent A's shell is still running"
    finally:
        release.write_text("go", encoding="utf-8")
        res_a = await asyncio.wait_for(task_a, timeout=15.0)
    assert not res_a.is_error, res_a.output
    assert "DONE_A" in res_a.output


@pytest.mark.asyncio
async def test_p1_real_tool_registry_multiline_shell_redirect_coordinates_with_file_write(
    tmp_path: Path,
):
    """Real ToolRegistry.execute() starting from workspace_dir (no `cwd` arg):
    Multiline PowerShell and multiline Bash commands that `cd`/`Set-Location` on line 2
    and redirect `> shared.txt` on line 3 MUST coordinate on `repo-a/shared.txt` with
    a competing `file_write`, while leaving `repo-a/sibling.txt` unblocked.
    """
    ws_a = tmp_path / "workspaces" / "agent_a"
    ws_b = tmp_path / "workspaces" / "agent_b"
    repo_a = ws_a / "repo-a"
    for d in (repo_a, ws_b):
        d.mkdir(parents=True)

    shared_ctx = {"global_config": SimpleNamespace(bridge_home=tmp_path)}
    reg_a = ToolRegistry(
        allowed_tools=["shell"],
        workspace_dir=ws_a,
        access_root=tmp_path,
        secrets={},
        audit_context=shared_ctx,
    )
    reg_b = ToolRegistry(
        allowed_tools=["file_write"],
        workspace_dir=ws_b,
        access_root=tmp_path,
        secrets={},
        audit_context=shared_ctx,
    )

    async def _run_multiline_case(
        *,
        label: str,
        shell_name: str,
        command: str,
        ready_file: Path,
        release_file: Path,
        target_file: Path,
        sibling_file: Path,
    ) -> None:
        shell_args = {"command": command, "shell": shell_name}
        task_a = asyncio.create_task(reg_a.execute("shell", shell_args, f"call-a-{label}"))
        task_b_conflict = None
        try:
            for _ in range(200):
                if ready_file.exists():
                    break
                if task_a.done():
                    pytest.fail(f"{label} shell exited early: {task_a.result().output}")
                await asyncio.sleep(0.05)
            assert ready_file.exists(), f"{label} shell did not reach running state"

            # Sibling file in the SAME directory MUST proceed concurrently.
            res_sibling = await asyncio.wait_for(
                reg_b.execute(
                    "file_write",
                    {"path": str(sibling_file), "content": f"sibling-{label}\n"},
                    f"call-b-sibling-{label}",
                ),
                timeout=5.0,
            )
            assert not res_sibling.is_error, res_sibling.output
            assert sibling_file.read_text(encoding="utf-8") == f"sibling-{label}\n"

            # Competing file_write to the exact multiline redirect target MUST wait.
            task_b_conflict = asyncio.create_task(
                reg_b.execute(
                    "file_write",
                    {"path": str(target_file), "content": f"FROM_B_{label}\n"},
                    f"call-b-conflict-{label}",
                )
            )
            await asyncio.sleep(0.25)
            assert not task_b_conflict.done(), (
                f"{label}: competing file_write on {target_file.name} must wait for multiline shell redirect"
            )
            assert not task_a.done()
        finally:
            release_file.write_text("go", encoding="utf-8")
            res_a = await asyncio.wait_for(task_a, timeout=15.0)
        assert not res_a.is_error, res_a.output
        assert task_b_conflict is not None
        res_b = await asyncio.wait_for(task_b_conflict, timeout=10.0)
        assert not res_b.is_error, res_b.output
        assert target_file.read_text(encoding="utf-8") == f"FROM_B_{label}\n"

    # Case 1: Multiline PowerShell (on Windows) or Bash (on POSIX).
    if os.name == "nt":
        ready_ps = tmp_path / "ready_ps"
        release_ps = tmp_path / "release_ps"
        ps_cmd = (
            'Write-Output "step1"\r\n'
            "Set-Location -LiteralPath 'repo-a'\r\n"
            f"& {{ Set-Content -LiteralPath '{ready_ps}' -Value ready; "
            f"while (-not (Test-Path -LiteralPath '{release_ps}')) {{ Start-Sleep -Milliseconds 50 }}; "
            "Write-Output 'FROM_PS' } > shared_ps.txt"
        )
        await _run_multiline_case(
            label="powershell",
            shell_name="powershell",
            command=ps_cmd,
            ready_file=ready_ps,
            release_file=release_ps,
            target_file=repo_a / "shared_ps.txt",
            sibling_file=repo_a / "sibling_ps.txt",
        )

    # Case 2: Multiline Bash (run live whenever `bash` is resolvable in this environment).
    bash_available = True
    try:
        resolve_shell_invocation("echo ok", "bash")
    except RuntimeError:
        bash_available = False

    if bash_available:
        ready_sh = tmp_path / "ready_sh"
        release_sh = tmp_path / "release_sh"
        # Use relative paths from workspace_dir (`../ready_sh`) so both Git Bash and
        # WSL bash (when launched with cwd=workspace_dir) resolve identically.
        bash_cmd = (
            "echo step1\n"
            "cd repo-a\n"
            "touch ../../../ready_sh && "
            "while [ ! -f ../../../release_sh ]; do sleep 0.05; done && "
            "printf 'FROM_BASH\\n' > shared_bash.txt"
        )
        await _run_multiline_case(
            label="bash",
            shell_name="bash",
            command=bash_cmd,
            ready_file=ready_sh,
            release_file=release_sh,
            target_file=repo_a / "shared_bash.txt",
            sibling_file=repo_a / "sibling_bash.txt",
        )


@pytest.mark.asyncio
async def test_p1_same_file_coordination_and_invalid_args_never_lock_all(tmp_path: Path):
    projects = tmp_path / "projects"
    repo_a = projects / "repo-a"
    repo_b = projects / "repo-b"
    ws_a = tmp_path / "workspaces" / "agent_a"
    ws_b = tmp_path / "workspaces" / "agent_b"
    for d in (repo_a, repo_b, ws_a, ws_b):
        d.mkdir(parents=True)
    shared_file = repo_a / "shared.py"
    other_file = repo_a / "other.py"
    shared_file.write_text("x = 1\n", encoding="utf-8")
    other_file.write_text("y = 1\n", encoding="utf-8")

    shared_ctx = lambda: {"global_config": SimpleNamespace(bridge_home=tmp_path)}
    reg_a = SimpleNamespace(workspace_dir=ws_a, access_roots=[projects], _effective_audit_context=shared_ctx)
    reg_b = SimpleNamespace(workspace_dir=ws_b, access_roots=[projects], _effective_audit_context=shared_ctx)

    # 1. Two agents editing DIFFERENT files in the SAME repository run in parallel.
    async with tool_resource(reg_a, "text_replace", {"path": str(shared_file), "find": "1", "replace": "2"}):
        async with tool_resource(reg_b, "file_write", {"path": str(other_file), "content": "y = 2\n"}):
            pass

    # 2. Two agents editing the EXACT same file coordinate.
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
        ("shell", {"command": ""}),
        ("shell", {"command": "echo hi > bad\x00file"}),
    ]
    for tool_name, bad_args in invalid_calls:
        async with tool_resource(reg_a, tool_name, bad_args):
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

    # 2. Task cancellation (simulating /stop or /steer) releases active redirect lease.
    held = asyncio.Event()

    async def hold_until_cancelled():
        async with tool_resource(reg, "shell", {"command": f"cd '{repo_a}'; echo hi > locked.txt"}):
            held.set()
            await asyncio.sleep(30)

    holder = asyncio.create_task(hold_until_cancelled())
    await asyncio.wait_for(held.wait(), timeout=2.0)

    # Timeout while waiting on the locked target file raises ExecutionResourceTimeout cleanly without leaking.
    key = "filesystem:" + os.path.normcase(str(target.resolve()))
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
# Task 5 (P2/P3): Cost Accounting H-08 & Zero-Call Boundary Cases
# ---------------------------------------------------------------------------


def test_p2_partial_unknown_turn_preserves_known_subtotal_and_unknown_call_count(
    tmp_path: Path, monkeypatch
):
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
    assert receipt.cache_savings_usd is None

    persisted = json.loads((tmp_path / "token_usage.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert persisted["cost_usd"] is None
    assert persisted["cost_known"] is False
    assert persisted["cost_source"] == "unknown"
    assert persisted["known_cost_usd"] == pytest.approx(expected_known)
    assert persisted["known_cost_requests"] == 79
    assert persisted["unknown_cost_requests"] == 2
    assert persisted["cache_savings_usd"] is None

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


def test_p3_cost_accounting_all_five_boundary_cases(tmp_path: Path, monkeypatch):
    """Verify all 5 required boundary cases in tools/token_tracker.py:
      1. 79 known + 2 unknown
      2. 1 known + 0 unknown
      3. 0 known + 1 unknown
      4. 0 known + 0 unknown (both cost_usd=None and cost_usd=0.0; NEVER inflated to 1)
      5. Legacy JSONL lacking known_cost_requests / unknown_cost_requests
    """
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

    def _summarize(name: str, rows: list[dict]) -> dict:
        ws = tmp_path / name
        ws.mkdir(parents=True)
        (ws / "token_usage.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n",
            encoding="utf-8",
        )
        return get_summary(ws, session_id="s1")

    # Case 1: 79 known + 2 unknown
    s1 = _summarize(
        "case_79_2",
        [
            {
                "ts": "2026-11-22T00:00:00+00:00",
                "model": "m1",
                "backend": "her-v2",
                "input": 1000,
                "output": 100,
                "thinking": 0,
                "cost_usd": None,
                "known_cost_usd": 1.452765,
                "known_cost_requests": 79,
                "unknown_cost_requests": 2,
                "session_id": "s1",
            }
        ],
    )
    assert s1["all_time"]["known_cost_requests"] == 79
    assert s1["all_time"]["unknown_cost_requests"] == 2
    assert s1["all_time"]["cost_usd"] == pytest.approx(1.452765)

    # Case 2: 1 known + 0 unknown
    s2 = _summarize(
        "case_1_0",
        [
            {
                "ts": "2026-11-22T00:00:00+00:00",
                "model": "m1",
                "backend": "her-v2",
                "input": 1000,
                "output": 100,
                "thinking": 0,
                "cost_usd": 0.05,
                "known_cost_usd": 0.05,
                "known_cost_requests": 1,
                "unknown_cost_requests": 0,
                "session_id": "s1",
            }
        ],
    )
    assert s2["all_time"]["known_cost_requests"] == 1
    assert s2["all_time"]["unknown_cost_requests"] == 0
    assert s2["all_time"]["cost_usd"] == pytest.approx(0.05)
    with ui_language.language_scope(SimpleNamespace(), locale="en"):
        assert format_usage_cost(s2["all_time"]) == "$0.0500"

    # Case 3: 0 known + 1 unknown
    s3 = _summarize(
        "case_0_1",
        [
            {
                "ts": "2026-11-22T00:00:00+00:00",
                "model": "m1",
                "backend": "her-v2",
                "input": 1000,
                "output": 100,
                "thinking": 0,
                "cost_usd": None,
                "known_cost_usd": None,
                "known_cost_requests": 0,
                "unknown_cost_requests": 1,
                "session_id": "s1",
            }
        ],
    )
    assert s3["all_time"]["known_cost_requests"] == 0
    assert s3["all_time"]["unknown_cost_requests"] == 1
    assert s3["all_time"]["cost_usd"] == 0.0
    with ui_language.language_scope(SimpleNamespace(), locale="en"):
        assert format_usage_cost(s3["all_time"]) == "Cost unknown"

    # Case 4a: 0 known + 0 unknown with cost_usd=None -> MUST stay 0 and 0 (NEVER coerced to 1!).
    s4a = _summarize(
        "case_0_0_none",
        [
            {
                "ts": "2026-11-22T00:00:00+00:00",
                "model": "m1",
                "backend": "her-v2",
                "input": 0,
                "output": 0,
                "thinking": 0,
                "cost_usd": None,
                "known_cost_usd": None,
                "known_cost_requests": 0,
                "unknown_cost_requests": 0,
                "session_id": "s1",
            }
        ],
    )
    assert s4a["all_time"]["known_cost_requests"] == 0
    assert s4a["all_time"]["unknown_cost_requests"] == 0
    assert s4a["all_time"]["cost_usd"] == 0.0

    # Case 4b: 0 known + 0 unknown with cost_usd=0.0 -> MUST stay 0 and 0 (NEVER coerced to 1!).
    s4b = _summarize(
        "case_0_0_zero",
        [
            {
                "ts": "2026-11-22T00:00:00+00:00",
                "model": "m1",
                "backend": "her-v2",
                "input": 0,
                "output": 0,
                "thinking": 0,
                "cost_usd": 0.0,
                "known_cost_usd": 0.0,
                "known_cost_requests": 0,
                "unknown_cost_requests": 0,
                "session_id": "s1",
            }
        ],
    )
    assert s4b["all_time"]["known_cost_requests"] == 0
    assert s4b["all_time"]["unknown_cost_requests"] == 0
    assert s4b["all_time"]["cost_usd"] == 0.0
    with ui_language.language_scope(SimpleNamespace(), locale="en"):
        assert format_usage_cost(s4b["all_time"]) == "$0.0000"

    # Case 5: Legacy JSONL without new fields falls back to 1 per record.
    s5 = _summarize(
        "case_legacy",
        [
            {"ts": "2026-10-01T00:00:00+00:00", "model": "m1", "backend": "her-v2", "input": 1000, "output": 100, "thinking": 0, "cost_usd": 0.25, "session_id": "s1"},
            {"ts": "2026-10-01T00:01:00+00:00", "model": "m1", "backend": "her-v2", "input": 500, "output": 50, "thinking": 0, "cost_usd": None, "session_id": "s1"},
            {"ts": "2026-10-01T00:02:00+00:00", "model": "local", "backend": "ollama-api", "input": 200, "output": 20, "thinking": 0, "cost_usd": 0.0, "session_id": "s1"},
        ],
    )
    all_t = s5["all_time"]
    assert all_t["cost_usd"] == pytest.approx(0.25)
    assert all_t["known_cost_usd"] == pytest.approx(0.25)
    assert all_t["known_cost_requests"] == 2
    assert all_t["unknown_cost_requests"] == 1
    assert all_t["requests"] == 3
    with ui_language.language_scope(SimpleNamespace(), locale="en"):
        assert format_usage_cost(all_t) == "Known subtotal $0.2500 · cost unknown for 1 request"
        assert format_usage_cost(s5["by_model"]["local"]) == "$0.0000"


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

    await runtime_workspace.cmd_memory(runtime_legacy, update, SimpleNamespace(args=["on"]))
    assert is_memory_search_enabled(ws_legacy) is True
    assert assembler_legacy.saved_memory_injection_enabled is True
    assert fresh_context.automatic_context_suppressed(runtime_legacy) is False

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

    pcm_peer = ws_peer / "agent.md"
    pcm_peer.write_text(render_pcm_document(persona="Peer", system="Sys", memory=""), encoding="utf-8")
    store_peer = BridgeMemoryStore(ws_peer)
    assembler_peer = BridgeContextAssembler(store_peer, pcm_peer)
    apply_memory_injection_preferences(assembler_peer, ws_peer)
    assert store_peer.retrieve_memories("黄绿色", limit=5) == []
    assert "黄绿色" not in assembler_peer.build_prompt("爸爸喜欢什么颜色？", "her-v2")

    await runtime_workspace.cmd_memory(runtime_legacy, update, SimpleNamespace(args=["search", "off"]))
    assert is_memory_search_enabled(ws_legacy) is False
    assert assembler_legacy.saved_memory_injection_enabled is False


# ---------------------------------------------------------------------------
# Task 4 (P2): /steer & /focus Truthful Reply Delivery Statuses + Replay Guard
# ---------------------------------------------------------------------------


def _make_real_steer_harness(tmp_path: Path, monkeypatch, *, case_name: str, busy: bool = True):
    import orchestrator.admin_local_testing as local_testing
    import orchestrator.flexible_agent_runtime as agent_runtime
    from tests.test_frontend_command_admission import _Runtime, _update

    case_dir = tmp_path / case_name
    case_dir.mkdir(parents=True, exist_ok=True)
    runtime = _Runtime(case_dir)
    runtime.name = f"test_her_{case_name}"
    runtime.global_config.project_root = case_dir
    runtime.app = SimpleNamespace(bot=None)
    runtime.telegram_logger = SimpleNamespace(warning=lambda *a, **k: None)
    runtime.logger = SimpleNamespace(warning=lambda *a, **k: None, info=lambda *a, **k: None)
    runtime.config = SimpleNamespace(active_backend="her-v2", engine="her-v2")
    runtime.queue = asyncio.Queue()
    runtime.session_store = SessionStore(case_dir / "sessions.sqlite3", instance_id="HASHI_TEST")

    monkeypatch.setattr(local_testing, "local_command_capture", lambda *_a: None)
    monkeypatch.setattr(agent_runtime, "apply_disable_notification_default", lambda *_a: None)
    monkeypatch.setattr(
        agent_runtime.telegram_delivery_failover,
        "handle_blocked_send",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        agent_runtime.telegram_delivery_failover,
        "handle_retry_after",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(agent_runtime.asyncio, "sleep", AsyncMock())

    update = _update()
    update.effective_chat.id = 42
    update.message.chat = update.effective_chat

    session = runtime_session.current_session_for_update(runtime, update)

    async def bound_reply(upd, text, **kwargs):
        return await FlexibleAgentRuntime._reply_text(runtime, upd, text, **kwargs)

    runtime._reply_text = bound_reply

    enqueued_continuations: list[dict] = []
    created_run_id = f"run-steer-{case_name}"

    async def fake_enqueue(chat_id, prompt, source, summary, **kwargs):
        req_id = f"req-steer-{case_name}-{len(enqueued_continuations) + 1}"
        enqueued_continuations.append(
            {"req_id": req_id, "chat_id": chat_id, "source": source, "summary": summary}
        )
        return req_id

    runtime.backend_manager = SimpleNamespace(
        current_backend=SimpleNamespace(shutdown=AsyncMock()),
        initialize_active_backend=AsyncMock(return_value=True),
    )
    runtime.last_prompt = SimpleNamespace(
        prompt="Original task", chat_id=42, session_id=str(session["session_id"])
    )
    if busy:
        runtime.current_request_meta = {
            "request_id": f"req-active-{case_name}",
            "hashi_session_id": str(session["session_id"]),
            "chat_id": 42,
            "prompt": "Original task",
            "source": "session-api",
            "summary": "Original task",
            "hashi_run_id": f"run-old-{case_name}",
        }
        runtime.is_generating = True
    else:
        runtime.current_request_meta = None
        runtime.is_generating = False

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
    return runtime, update, session, enqueued_continuations, case_dir / "sessions.sqlite3"


@pytest.mark.asyncio
async def test_p2_real_session_store_steer_delivery_truth_and_replay_protection(
    tmp_path: Path, monkeypatch
):
    """Verify truthful reply delivery status across ALL 5 required scenarios with real
    SessionStore, execute_telegram_command_invocation, cmd_steer/cmd_focus, and
    FlexibleAgentRuntime._reply_text:
      1. Normal delivered reply (busy /steer, idle /steer, and idle /focus)
      2. Suppressed reply (delivery_allowed=False and handle_blocked_send=True)
      3. Telegram send failure (RetryAfter, pre-submit ConnectError, post-submit TimedOut)
      4. Sent + SessionConflict (preserves existing Event binding)
      5. Duplicate Telegram replay on every case (no re-execution, no duplicate continuation)
    """
    import orchestrator.flexible_agent_runtime as agent_runtime
    import orchestrator.telegram_actor_scope as actor_scope

    # --- Scenario 1A: Busy /steer normal delivered ---
    rt, upd, _sess, enq, _db = _make_real_steer_harness(tmp_path, monkeypatch, case_name="ok_busy", busy=True)
    upd.update_id = 92001
    upd.message.message_id = 14001
    upd.message.text = "/steer keep tests fast"
    upd.message.reply_text = AsyncMock(return_value=SimpleNamespace(message_id=5001))
    calls = 0

    async def invoke_1a():
        nonlocal calls
        calls += 1
        return await runtime_control.cmd_steer(rt, upd, SimpleNamespace(args=["keep", "tests", "fast"]))

    res = await execute_telegram_command_invocation(
        rt, upd, command_name="steer", arguments=["keep", "tests", "fast"],
        transport_id=92001, invoke_handler=invoke_1a, command_known=True,
    )
    assert res["ok"] is True and res["execution_state"] == "completed"
    assert res["transport_delivery_state"] == "delivered"
    assert res["result"]["reply_delivery"]["status"] == "delivered"
    assert res["result"]["reply_delivery"]["sent"] is True
    assert res["result"]["reply_delivery"]["transport_message_id"] == "5001"
    replay = await execute_telegram_command_invocation(
        rt, upd, command_name="steer", arguments=["keep", "tests", "fast"],
        transport_id=92001, invoke_handler=invoke_1a, command_known=True,
    )
    assert replay["replayed"] is True and replay["transport_delivery_state"] == "delivered"
    assert calls == 1 and len(enq) == 1

    # --- Scenario 1B: Idle /steer & Idle /focus normal delivered (consistent transport_delivery_state) ---
    for cmd_name, tid, msg_id, text_cmd, args in (
        ("steer", 92002, 14002, "/steer fresh task", ["fresh", "task"]),
        ("focus", 92003, 14003, "/focus", []),
    ):
        rt_i, upd_i, _s, enq_i, _db_i = _make_real_steer_harness(
            tmp_path, monkeypatch, case_name=f"idle_{cmd_name}", busy=False
        )
        upd_i.update_id = tid
        upd_i.message.message_id = msg_id
        upd_i.message.text = text_cmd
        upd_i.message.reply_text = AsyncMock(return_value=SimpleNamespace(message_id=msg_id + 1000))
        c_i = 0

        async def _inv(r=rt_i, u=upd_i, a=args, cn=cmd_name):
            nonlocal c_i
            c_i += 1
            fn = runtime_control.cmd_focus if cn == "focus" else runtime_control.cmd_steer
            return await fn(r, u, SimpleNamespace(args=a))

        res_i = await execute_telegram_command_invocation(
            rt_i, upd_i, command_name=cmd_name, arguments=args,
            transport_id=tid, invoke_handler=_inv, command_known=True,
        )
        assert res_i["ok"] is True and res_i["execution_state"] == "completed"
        assert res_i["transport_delivery_state"] == "delivered"
        assert res_i["result"]["reply_delivery"]["sent"] is True
        assert res_i["result"]["reply_delivery"]["transport_message_id"] == str(msg_id + 1000)
        rep_i = await execute_telegram_command_invocation(
            rt_i, upd_i, command_name=cmd_name, arguments=args,
            transport_id=tid, invoke_handler=_inv, command_known=True,
        )
        assert rep_i["replayed"] is True and c_i == 1 and len(enq_i) == 1

    # --- Scenario 2A: Suppressed reply (delivery_allowed=False) -> NEVER falsely "delivered" ---
    rt_s, upd_s, _s, enq_s, _db_s = _make_real_steer_harness(tmp_path, monkeypatch, case_name="suppressed", busy=True)
    upd_s.update_id = 92004
    upd_s.message.message_id = 14004
    upd_s.message.text = "/steer adjust plan"
    upd_s.message.reply_text = AsyncMock(return_value=SimpleNamespace(message_id=5004))
    monkeypatch.setattr(actor_scope, "delivery_allowed", lambda *_a, **_k: False)
    calls_s = 0

    async def invoke_suppressed():
        nonlocal calls_s
        calls_s += 1
        return await runtime_control.cmd_steer(rt_s, upd_s, SimpleNamespace(args=["adjust", "plan"]))

    res_s = await execute_telegram_command_invocation(
        rt_s, upd_s, command_name="steer", arguments=["adjust", "plan"],
        transport_id=92004, invoke_handler=invoke_suppressed, command_known=True,
    )
    assert res_s["ok"] is True and res_s["execution_state"] == "completed"
    assert res_s["transport_delivery_state"] == "suppressed"
    assert res_s["result"]["reply_delivery"]["sent"] is False
    assert res_s["result"]["reply_delivery"]["error_code"] == "telegram_access_revoked"
    assert upd_s.message.reply_text.await_count == 0
    rep_s = await execute_telegram_command_invocation(
        rt_s, upd_s, command_name="steer", arguments=["adjust", "plan"],
        transport_id=92004, invoke_handler=invoke_suppressed, command_known=True,
    )
    assert rep_s["replayed"] is True and calls_s == 1 and len(enq_s) == 1
    monkeypatch.undo()

    # --- Scenario 2B: Suppressed reply (handle_blocked_send=True) ---
    rt_b, upd_b, _s, enq_b, _db_b = _make_real_steer_harness(tmp_path, monkeypatch, case_name="blocked", busy=True)
    monkeypatch.setattr(
        agent_runtime.telegram_delivery_failover,
        "handle_blocked_send",
        AsyncMock(return_value=True),
    )
    upd_b.update_id = 92005
    upd_b.message.message_id = 14005
    upd_b.message.text = "/steer blocked reply"
    upd_b.message.reply_text = AsyncMock(return_value=SimpleNamespace(message_id=5005))
    calls_b = 0

    async def invoke_blocked():
        nonlocal calls_b
        calls_b += 1
        return await runtime_control.cmd_steer(rt_b, upd_b, SimpleNamespace(args=["blocked", "reply"]))

    res_b = await execute_telegram_command_invocation(
        rt_b, upd_b, command_name="steer", arguments=["blocked", "reply"],
        transport_id=92005, invoke_handler=invoke_blocked, command_known=True,
    )
    assert res_b["ok"] is True and res_b["execution_state"] == "completed"
    assert res_b["transport_delivery_state"] == "suppressed"
    assert res_b["result"]["reply_delivery"]["sent"] is False
    assert res_b["result"]["reply_delivery"]["error_code"] == "telegram_delivery_blocked"
    assert calls_b == 1 and len(enq_b) == 1

    # --- Scenario 3A: RetryAfter (caught inside _reply_text, returns None) ---
    rt_ra, upd_ra, _s, enq_ra, _db_ra = _make_real_steer_harness(tmp_path, monkeypatch, case_name="retry_after", busy=True)
    upd_ra.update_id = 92006
    upd_ra.message.message_id = 14006
    upd_ra.message.text = "/steer rate limited"
    upd_ra.message.reply_text = AsyncMock(side_effect=RetryAfter(12))
    calls_ra = 0

    async def invoke_ra():
        nonlocal calls_ra
        calls_ra += 1
        return await runtime_control.cmd_steer(rt_ra, upd_ra, SimpleNamespace(args=["rate", "limited"]))

    res_ra = await execute_telegram_command_invocation(
        rt_ra, upd_ra, command_name="steer", arguments=["rate", "limited"],
        transport_id=92006, invoke_handler=invoke_ra, command_known=True,
    )
    assert res_ra["ok"] is True and res_ra["execution_state"] == "completed"
    assert res_ra["transport_delivery_state"] == "retry_after"
    assert res_ra["result"]["reply_delivery"]["sent"] is False
    assert res_ra["result"]["reply_delivery"]["error_code"] == "telegram_retry_after"
    rep_ra = await execute_telegram_command_invocation(
        rt_ra, upd_ra, command_name="steer", arguments=["rate", "limited"],
        transport_id=92006, invoke_handler=invoke_ra, command_known=True,
    )
    assert rep_ra["replayed"] is True and calls_ra == 1 and len(enq_ra) == 1

    # --- Scenario 3B: Pre-submit ConnectError -> delivery_failed (sent=False) ---
    rt_ce, upd_ce, _s, enq_ce, _db_ce = _make_real_steer_harness(tmp_path, monkeypatch, case_name="connect_err", busy=True)
    upd_ce.update_id = 92007
    upd_ce.message.message_id = 14007
    upd_ce.message.text = "/steer connect fail"
    connect_exc = NetworkError("connect failed")
    connect_exc.__cause__ = httpx.ConnectError("dns fail")
    upd_ce.message.reply_text = AsyncMock(side_effect=connect_exc)
    calls_ce = 0

    async def invoke_ce():
        nonlocal calls_ce
        calls_ce += 1
        return await runtime_control.cmd_steer(rt_ce, upd_ce, SimpleNamespace(args=["connect", "fail"]))

    res_ce = await execute_telegram_command_invocation(
        rt_ce, upd_ce, command_name="steer", arguments=["connect", "fail"],
        transport_id=92007, invoke_handler=invoke_ce, command_known=True,
    )
    assert res_ce["ok"] is True and res_ce["execution_state"] == "completed"
    assert res_ce["transport_delivery_state"] == "delivery_failed"
    assert res_ce["result"]["reply_delivery"]["sent"] is False
    assert res_ce["result"]["reply_delivery"]["error_code"] == "telegram_reply_not_submitted"
    rep_ce = await execute_telegram_command_invocation(
        rt_ce, upd_ce, command_name="steer", arguments=["connect", "fail"],
        transport_id=92007, invoke_handler=invoke_ce, command_known=True,
    )
    assert rep_ce["replayed"] is True and calls_ce == 1 and len(enq_ce) == 1

    # --- Scenario 3C: Post-submit TimedOut -> outcome_unknown (sent=None) ---
    rt_to, upd_to, _s, enq_to, _db_to = _make_real_steer_harness(tmp_path, monkeypatch, case_name="timed_out", busy=True)
    upd_to.update_id = 92008
    upd_to.message.message_id = 14008
    upd_to.message.text = "/steer read timeout"
    upd_to.message.reply_text = AsyncMock(side_effect=TimedOut("read timeout"))
    calls_to = 0

    async def invoke_to():
        nonlocal calls_to
        calls_to += 1
        return await runtime_control.cmd_steer(rt_to, upd_to, SimpleNamespace(args=["read", "timeout"]))

    res_to = await execute_telegram_command_invocation(
        rt_to, upd_to, command_name="steer", arguments=["read", "timeout"],
        transport_id=92008, invoke_handler=invoke_to, command_known=True,
    )
    assert res_to["ok"] is True and res_to["execution_state"] == "completed"
    assert res_to["transport_delivery_state"] == "outcome_unknown"
    assert res_to["result"]["reply_delivery"]["sent"] is None
    assert res_to["result"]["reply_delivery"]["error_code"] == "telegram_reply_outcome_unknown"
    rep_to = await execute_telegram_command_invocation(
        rt_to, upd_to, command_name="steer", arguments=["read", "timeout"],
        transport_id=92008, invoke_handler=invoke_to, command_known=True,
    )
    assert rep_to["replayed"] is True and calls_to == 1 and len(enq_to) == 1

    # --- Scenario 4: Sent + SessionConflict (exact worker-53792 repro) + Replay ---
    rt_cf, upd_cf, sess_cf, enq_cf, db_cf = _make_real_steer_harness(tmp_path, monkeypatch, case_name="conflict", busy=True)
    upd_cf.update_id = 91001
    upd_cf.message.message_id = 13220
    upd_cf.message.text = "/steer 不用管手机输入法 单独处理"
    upd_cf.message.reply_text = AsyncMock(return_value=SimpleNamespace(message_id=7777))

    # Pre-bind transport_message_id="7777" to an earlier Event.
    owner_cf = runtime_session.owner_id(rt_cf)
    await FlexibleAgentRuntime._reply_text(rt_cf, upd_cf, "Prior assistant message")
    with sqlite3.connect(db_cf) as conn:
        conn.row_factory = sqlite3.Row
        prior_row = conn.execute(
            "SELECT event_id, endpoint_id FROM frontend_transport_references "
            "WHERE connector_id='telegram' AND transport_message_id='7777'"
        ).fetchone()
        assert prior_row is not None
        prior_event_id = str(prior_row["event_id"])
        prior_endpoint_id = str(prior_row["endpoint_id"])
    upd_cf.message.reply_text.reset_mock()

    calls_cf = 0

    async def invoke_cf():
        nonlocal calls_cf
        calls_cf += 1
        return await runtime_control.cmd_steer(
            rt_cf, upd_cf, SimpleNamespace(args=["不用管手机输入法", "单独处理"])
        )

    first = await execute_telegram_command_invocation(
        rt_cf, upd_cf, command_name="steer", arguments=["不用管手机输入法", "单独处理"],
        transport_id=91001, invoke_handler=invoke_cf, command_known=True,
    )
    assert first["ok"] is True
    assert first["execution_state"] == "completed"
    assert first["transport_delivery_state"] == "sent_reference_conflict"
    assert first["result"]["status"] == "accepted"
    assert first["result"]["new_run"]["created"] is True
    assert first["result"]["reply_delivery"]["sent"] is True
    assert first["result"]["reply_delivery"]["transport_message_id"] == "7777"
    assert first["result"]["reply_delivery"]["error_code"] == "telegram_transport_reference_conflict"
    assert calls_cf == 1 and len(enq_cf) == 1 and upd_cf.message.reply_text.await_count == 1

    # Conflicting Event binding MUST remain intact!
    with sqlite3.connect(db_cf) as conn:
        conn.row_factory = sqlite3.Row
        ref_row = conn.execute(
            "SELECT event_id FROM frontend_transport_references "
            "WHERE connector_id='telegram' AND endpoint_id=? AND transport_message_id='7777'",
            (prior_endpoint_id,),
        ).fetchone()
        assert ref_row is not None and str(ref_row["event_id"]) == prior_event_id
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

    # Direct SessionStore conflict guard remains enforced.
    with pytest.raises(SessionConflict, match="already bound to another Event"):
        rt_cf.session_store.record_frontend_transport_reference(
            session_id=str(sess_cf["session_id"]),
            owner_id=owner_cf,
            connector_id="telegram",
            endpoint_id=prior_endpoint_id,
            transport_message_id="7777",
            event_id="evt-different-attempt",
        )

    # Replay MUST NOT re-execute /steer or duplicate continuation.
    replay_cf = await execute_telegram_command_invocation(
        rt_cf, upd_cf, command_name="steer", arguments=["不用管手机输入法", "单独处理"],
        transport_id=91001, invoke_handler=invoke_cf, command_known=True,
    )
    assert replay_cf["replayed"] is True
    assert replay_cf.get("outcome_unknown") is not True
    assert replay_cf["ok"] is True
    assert replay_cf["execution_state"] == "completed"
    assert replay_cf["transport_delivery_state"] == "sent_reference_conflict"
    assert calls_cf == 1 and len(enq_cf) == 1 and upd_cf.message.reply_text.await_count == 1


@pytest.mark.asyncio
async def test_p2_steer_and_focus_enqueue_none_and_missing_run_id_truth(
    tmp_path: Path, monkeypatch
):
    """Verify truthful status & frontend admission when enqueue_request() returns None
    or when request_id is valid while Run ID is temporarily unavailable, plus Telegram
    replay idempotency via real SessionStore:
      1. Idle /steer: enqueue succeeds -> ok=True, execution_state=completed
      2. Idle /steer: enqueue returns None -> ok=False, execution_state=failed, steer_enqueue_failed
      3. Idle /focus: enqueue returns None -> ok=False, execution_state=failed, focus_enqueue_failed
      4. Busy /steer: interrupt + continuation enqueue returns None -> ok=False, failed, interrupted=True
      5. Busy /focus: interrupt + continuation enqueue returns None -> ok=False, failed, interrupted=True
      6. Busy /steer: valid request_id with Run ID temporarily unavailable -> ok=True, completed (NOT failed!)
      7. Duplicate Telegram replay on every case -> never re-executes cmd_steer/cmd_focus
    """
    # --- 1. Idle /steer: enqueue succeeds ---
    rt1, upd1, _s1, enq1, _db1 = _make_real_steer_harness(
        tmp_path, monkeypatch, case_name="idle_steer_ok", busy=False
    )
    upd1.update_id = 93001
    upd1.message.message_id = 15001
    upd1.message.text = "/steer write release notes"
    upd1.message.reply_text = AsyncMock(return_value=SimpleNamespace(message_id=6001))
    calls1 = 0

    async def invoke1():
        nonlocal calls1
        calls1 += 1
        return await runtime_control.cmd_steer(
            rt1, upd1, SimpleNamespace(args=["write", "release", "notes"])
        )

    res1 = await execute_telegram_command_invocation(
        rt1, upd1, command_name="steer", arguments=["write", "release", "notes"],
        transport_id=93001, invoke_handler=invoke1, command_known=True,
    )
    assert res1["ok"] is True
    assert res1["execution_state"] == "completed"
    assert res1["transport_delivery_state"] == "delivered"
    assert res1["result"]["request_id"] == "req-steer-idle_steer_ok-1"
    assert "queued your text" in upd1.message.reply_text.call_args.args[0]
    rep1 = await execute_telegram_command_invocation(
        rt1, upd1, command_name="steer", arguments=["write", "release", "notes"],
        transport_id=93001, invoke_handler=invoke1, command_known=True,
    )
    assert rep1["replayed"] is True and calls1 == 1 and len(enq1) == 1

    # --- 2. Idle /steer: enqueue_request returns None ---
    rt2, upd2, _s2, _enq2, _db2 = _make_real_steer_harness(
        tmp_path, monkeypatch, case_name="idle_steer_none", busy=False
    )
    rt2.enqueue_request = AsyncMock(return_value=None)
    upd2.update_id = 93002
    upd2.message.message_id = 15002
    upd2.message.text = "/steer drop me"
    upd2.message.reply_text = AsyncMock(return_value=SimpleNamespace(message_id=6002))
    calls2 = 0

    async def invoke2():
        nonlocal calls2
        calls2 += 1
        return await runtime_control.cmd_steer(
            rt2, upd2, SimpleNamespace(args=["drop", "me"])
        )

    res2 = await execute_telegram_command_invocation(
        rt2, upd2, command_name="steer", arguments=["drop", "me"],
        transport_id=93002, invoke_handler=invoke2, command_known=True,
    )
    assert res2["ok"] is False
    assert res2["execution_state"] == "failed"
    assert res2["error_code"] == "steer_enqueue_failed"
    assert res2["transport_delivery_state"] == "delivered"
    assert res2["result"]["request_id"] is None
    reply2_text = upd2.message.reply_text.call_args.args[0]
    assert "could not be queued" in reply2_text
    assert "queued your text" not in reply2_text
    rep2 = await execute_telegram_command_invocation(
        rt2, upd2, command_name="steer", arguments=["drop", "me"],
        transport_id=93002, invoke_handler=invoke2, command_known=True,
    )
    assert rep2["replayed"] is True
    assert rep2["ok"] is False
    assert rep2["execution_state"] == "failed"
    assert rep2["error_code"] == "steer_enqueue_failed"
    assert calls2 == 1 and rt2.enqueue_request.await_count == 1

    # --- 3. Idle /focus: enqueue_request returns None ---
    rt3, upd3, _s3, _enq3, _db3 = _make_real_steer_harness(
        tmp_path, monkeypatch, case_name="idle_focus_none", busy=False
    )
    rt3.enqueue_request = AsyncMock(return_value=None)
    upd3.update_id = 93003
    upd3.message.message_id = 15003
    upd3.message.text = "/focus"
    upd3.message.reply_text = AsyncMock(return_value=SimpleNamespace(message_id=6003))
    calls3 = 0

    async def invoke3():
        nonlocal calls3
        calls3 += 1
        return await runtime_control.cmd_focus(rt3, upd3, SimpleNamespace(args=[]))

    res3 = await execute_telegram_command_invocation(
        rt3, upd3, command_name="focus", arguments=[],
        transport_id=93003, invoke_handler=invoke3, command_known=True,
    )
    assert res3["ok"] is False
    assert res3["execution_state"] == "failed"
    assert res3["error_code"] == "focus_enqueue_failed"
    assert res3["transport_delivery_state"] == "delivered"
    assert res3["result"]["request_id"] is None
    reply3_text = upd3.message.reply_text.call_args.args[0]
    assert "could not be queued" in reply3_text
    assert "Focus applied" not in reply3_text
    rep3 = await execute_telegram_command_invocation(
        rt3, upd3, command_name="focus", arguments=[],
        transport_id=93003, invoke_handler=invoke3, command_known=True,
    )
    assert rep3["replayed"] is True and rep3["ok"] is False and calls3 == 1

    # --- 4. Busy /steer: active turn interrupted, continuation enqueue returns None ---
    rt4, upd4, _s4, _enq4, _db4 = _make_real_steer_harness(
        tmp_path, monkeypatch, case_name="busy_steer_none", busy=True
    )
    rt4.enqueue_request = AsyncMock(return_value=None)
    upd4.update_id = 93004
    upd4.message.message_id = 15004
    upd4.message.text = "/steer pivot now"
    upd4.message.reply_text = AsyncMock(return_value=SimpleNamespace(message_id=6004))
    ctx4 = SimpleNamespace(args=["pivot", "now"])
    calls4 = 0

    async def invoke4():
        nonlocal calls4
        calls4 += 1
        return await runtime_control.cmd_steer(rt4, upd4, ctx4)

    res4 = await execute_telegram_command_invocation(
        rt4, upd4, command_name="steer", arguments=["pivot", "now"],
        transport_id=93004, invoke_handler=invoke4, command_known=True,
    )
    assert res4["ok"] is False
    assert res4["execution_state"] == "failed"
    assert res4["error_code"] == "steer_continuation_enqueue_failed"
    assert res4["transport_delivery_state"] == "delivered"
    assert res4["result"]["status"] == "failed"
    assert res4["result"]["interrupted"] is True
    assert res4["result"]["old_run"]["terminal"] == "steered"
    assert res4["result"]["new_run"]["request_id"] is None
    assert res4["result"]["new_run"]["created"] is False
    assert getattr(ctx4, "derived_request_id", None) is None
    rt4.backend_manager.current_backend.shutdown.assert_awaited_once()
    reply4_text = upd4.message.reply_text.call_args.args[0]
    assert "interrupted active work" in reply4_text.casefold()
    assert "could not queue the continuation" in reply4_text.casefold()
    assert "🧭 Steered." not in reply4_text
    rep4 = await execute_telegram_command_invocation(
        rt4, upd4, command_name="steer", arguments=["pivot", "now"],
        transport_id=93004, invoke_handler=invoke4, command_known=True,
    )
    assert rep4["replayed"] is True
    assert rep4["ok"] is False
    assert rep4["execution_state"] == "failed"
    assert calls4 == 1
    assert rt4.backend_manager.current_backend.shutdown.await_count == 1

    # --- 5. Busy /focus: active turn interrupted, continuation enqueue returns None ---
    rt5, upd5, _s5, _enq5, _db5 = _make_real_steer_harness(
        tmp_path, monkeypatch, case_name="busy_focus_none", busy=True
    )
    rt5.enqueue_request = AsyncMock(return_value=None)
    upd5.update_id = 93005
    upd5.message.message_id = 15005
    upd5.message.text = "/focus"
    upd5.message.reply_text = AsyncMock(return_value=SimpleNamespace(message_id=6005))
    calls5 = 0

    async def invoke5():
        nonlocal calls5
        calls5 += 1
        return await runtime_control.cmd_focus(rt5, upd5, SimpleNamespace(args=[]))

    res5 = await execute_telegram_command_invocation(
        rt5, upd5, command_name="focus", arguments=[],
        transport_id=93005, invoke_handler=invoke5, command_known=True,
    )
    assert res5["ok"] is False
    assert res5["execution_state"] == "failed"
    assert res5["error_code"] == "focus_continuation_enqueue_failed"
    assert res5["result"]["interrupted"] is True
    assert res5["result"]["new_run"]["created"] is False
    rep5 = await execute_telegram_command_invocation(
        rt5, upd5, command_name="focus", arguments=[],
        transport_id=93005, invoke_handler=invoke5, command_known=True,
    )
    assert rep5["replayed"] is True and rep5["ok"] is False and calls5 == 1

    # --- 6. Busy /steer: request_id valid, Run ID temporarily unavailable -> NOT failed! ---
    rt6, upd6, _s6, _enq6, _db6 = _make_real_steer_harness(
        tmp_path, monkeypatch, case_name="busy_steer_no_run_yet", busy=True
    )
    rt6.enqueue_request = AsyncMock(return_value="req-valid-no-run-yet")
    # Real SessionStore lookup returns nothing for a request whose Run isn't visible yet.
    monkeypatch.setattr(
        rt6.session_store,
        "get_run_by_request",
        lambda request_id, **kw: None,
    )
    upd6.update_id = 93006
    upd6.message.message_id = 15006
    upd6.message.text = "/steer continue cleanly"
    upd6.message.reply_text = AsyncMock(return_value=SimpleNamespace(message_id=6006))
    ctx6 = SimpleNamespace(args=["continue", "cleanly"])
    calls6 = 0

    async def invoke6():
        nonlocal calls6
        calls6 += 1
        return await runtime_control.cmd_steer(rt6, upd6, ctx6)

    res6 = await execute_telegram_command_invocation(
        rt6, upd6, command_name="steer", arguments=["continue", "cleanly"],
        transport_id=93006, invoke_handler=invoke6, command_known=True,
    )
    assert res6["ok"] is True
    assert res6["execution_state"] == "completed"
    assert res6["transport_delivery_state"] == "delivered"
    assert res6["result"]["new_run"]["request_id"] == "req-valid-no-run-yet"
    assert res6["result"]["new_run"]["run_id"] is None
    assert res6["result"]["new_run"]["created"] is False
    assert res6["result"]["status"] == "outcome_unknown"
    assert ctx6.derived_request_id == "req-valid-no-run-yet"
    rep6 = await execute_telegram_command_invocation(
        rt6, upd6, command_name="steer", arguments=["continue", "cleanly"],
        transport_id=93006, invoke_handler=invoke6, command_known=True,
    )
    assert rep6["replayed"] is True and rep6["ok"] is True and calls6 == 1


# ---------------------------------------------------------------------------
# Task 6 (PIT Stage 2): 4-Agent Real Subprocess Barrier Overlap Acceptance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage2_four_isolated_herv3_agents_real_parallel_barrier_acceptance(
    tmp_path: Path, monkeypatch
):
    """Real 4-agent non-production parallel acceptance using real ToolRegistry + real OS
    subprocesses and a strict file barrier proving TRUE temporal overlap:
      - Agents 0 & 1 share the SAME repository (`projects/repo_shared`) on unrelated files.
      - Agents 2 & 3 work in `projects/repo_2` and `projects/repo_3`.
      - All 4 real subprocesses MUST reach `ready_<i>` SIMULTANEOUSLY before `go_all` is written
        (impossible if any two were serialized!).
      - While all 4 shell subprocesses are simultaneously alive, Agent 1 edits
        `repo_shared/docs/README.md` via real ToolRegistry.execute("text_replace", ...)
        and completes BEFORE `go_all` is released.
      - Also verifies SQLite concurrency (4 isolated BridgeMemoryStore CJK + SessionStore),
        partial-unknown billing, and H-11 fixed-mode /compact force.
    """
    monkeypatch.setenv("HASHI_PRICING_CACHE_FILE", str(tmp_path / "isolated-pricing-cache.json"))

    projects = tmp_path / "projects"
    repo_shared = projects / "repo_shared"
    docs_shared = repo_shared / "docs"
    docs_shared.mkdir(parents=True)
    shared_readme = docs_shared / "README.md"
    shared_readme.write_text("# Shared Repo\nEDIT = pending\n", encoding="utf-8")

    bridge_home = tmp_path / "nonprod_instance"
    bridge_home.mkdir()
    barrier_dir = tmp_path / "barriers"
    barrier_dir.mkdir()
    go_all = barrier_dir / "go_all"

    shared_ctx = {"global_config": SimpleNamespace(bridge_home=bridge_home)}
    agent_names = ("her_alpha", "her_beta", "her_gamma", "her_delta")
    codenames = ("青龙", "白虎", "朱雀", "玄武")
    agent_repos = (repo_shared, repo_shared, projects / "repo_2", projects / "repo_3")

    registries: list[ToolRegistry] = []
    ready_files: list[Path] = []

    for idx, name in enumerate(agent_names):
        ws = bridge_home / "workspaces" / name
        repo = agent_repos[idx]
        ws.mkdir(parents=True, exist_ok=True)
        repo.mkdir(parents=True, exist_ok=True)
        (repo / f"module_{idx}.py").write_text(f"AGENT = '{name}'\nCOUNT = 0\n", encoding="utf-8")
        registries.append(
            ToolRegistry(
                allowed_tools=["shell", "text_replace", "file_manage"],
                workspace_dir=ws,
                access_root=projects,
                secrets={},
                audit_context=shared_ctx,
            )
        )
        ready_files.append(barrier_dir / f"ready_{idx}")

    async def launch_shell_barrier(idx: int) -> dict:
        name = agent_names[idx]
        repo = agent_repos[idx]
        ready_f = ready_files[idx]
        if os.name == "nt":
            cmd = (
                f"Set-Location -LiteralPath '{repo}'\r\n"
                f"Set-Content -LiteralPath '{ready_f}' -Value '{name}'\r\n"
                f"while (-not (Test-Path -LiteralPath '{go_all}')) {{ Start-Sleep -Milliseconds 50 }}\r\n"
                f"Write-Output '{name}_done'"
            )
        else:
            cmd = (
                f"cd '{repo}'\n"
                f"printf '{name}' > '{ready_f}'\n"
                f"while [ ! -f '{go_all}' ]; do sleep 0.05; done\n"
                f"printf '{name}_done'"
            )
        res = await registries[idx].execute("shell", {"command": cmd}, f"{name}-barrier-sh")
        assert not res.is_error, res.output
        assert f"{name}_done" in res.output
        return {"agent": name, "shell_ok": True}

    # 1. Start all 4 real OS subprocesses concurrently.
    shell_tasks = [asyncio.create_task(launch_shell_barrier(i)) for i in range(4)]
    try:
        # Wait until ALL 4 real subprocesses are simultaneously alive (strict temporal overlap proof).
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            if all(rf.exists() for rf in ready_files):
                break
            for t in shell_tasks:
                if t.done():
                    pytest.fail(f"Barrier subprocess exited early: {t.result()}")
            await asyncio.sleep(0.05)
        assert all(rf.exists() for rf in ready_files), (
            f"Not all 4 real subprocesses overlapped simultaneously: {[rf.exists() for rf in ready_files]}"
        )
        assert all(not t.done() for t in shell_tasks), "All 4 shell subprocesses must still be alive"

        # 2. While Agent 0 & Agent 1's shells are STILL alive inside `repo_shared`,
        #    Agent 1 edits `repo_shared/docs/README.md` AND all 4 edit distinct files concurrently!
        edit_results = await asyncio.wait_for(
            asyncio.gather(
                registries[1].execute(
                    "text_replace",
                    {
                        "path": str(shared_readme),
                        "find": "EDIT = pending",
                        "replace": "EDIT = done_while_shells_alive",
                        "expected_count": 1,
                    },
                    "beta-readme-edit",
                ),
                *[
                    registries[i].execute(
                        "text_replace",
                        {
                            "path": str(agent_repos[i] / f"module_{i}.py"),
                            "find": "COUNT = 0",
                            "replace": f"COUNT = {i + 1}",
                            "expected_count": 1,
                        },
                        f"edit-mod-{i}",
                    )
                    for i in range(4)
                ],
            ),
            timeout=10.0,
        )
        assert all(not r.is_error for r in edit_results), [r.output for r in edit_results]
        assert "EDIT = done_while_shells_alive" in shared_readme.read_text(encoding="utf-8")
        assert all(not t.done() for t in shell_tasks), "Edits must complete BEFORE shells are released"
    finally:
        go_all.write_text("release", encoding="utf-8")
        shell_results = await asyncio.wait_for(asyncio.gather(*shell_tasks), timeout=20.0)
    assert len(shell_results) == 4 and all(r["shell_ok"] for r in shell_results)

    # 3. Parallel isolated SQLite concurrency, partial-unknown billing, and H-11 compaction across all 4 agents.
    async def run_agent_state_checks(idx: int, name: str) -> dict:
        codename = codenames[idx]
        ws = bridge_home / "workspaces" / name
        repo = agent_repos[idx]

        res_cp = await registries[idx].execute(
            "file_manage",
            {
                "operation": "copy",
                "source": str(repo / f"module_{idx}.py"),
                "destination": str(repo / f"module_{idx}_copy.py"),
            },
            f"{name}-cp",
        )
        assert not res_cp.is_error, res_cp.output

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
        sess = s_store.create_session(owner_id="user:1", agent_id=name)
        assert sess["session_id"]

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
        assert rec.cost_usd is None and rec.known_cost_usd == pytest.approx(0.05)
        assert rec.known_cost_requests == 1 and rec.unknown_cost_requests == 1
        summary = get_summary(ws, session_id=f"s-{name}")
        assert summary["all_time"]["cost_usd"] == pytest.approx(0.05)
        assert summary["all_time"]["unknown_cost_requests"] == 1

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
        compact_outcome = await compactor.compact(
            trigger="manual_command",
            request_ref=f"req-{name}-compact",
            force=True,
        )
        assert compact_outcome.status == "completed" and compact_outcome.changed is True
        return {"agent": name, "compacted": True}

    state_results = await asyncio.wait_for(
        asyncio.gather(*(run_agent_state_checks(i, n) for i, n in enumerate(agent_names))),
        timeout=30.0,
    )
    assert len(state_results) == 4 and all(r["compacted"] for r in state_results)

    # Confirm strict cross-agent memory isolation across all 4 agents.
    for i, name in enumerate(agent_names):
        ws = bridge_home / "workspaces" / name
        other_code = codenames[(i + 1) % len(codenames)]
        other_hits = BridgeMemoryStore(ws).retrieve_memories(other_code, limit=3)
        assert not any(other_code in h.get("content", "") for h in other_hits)
