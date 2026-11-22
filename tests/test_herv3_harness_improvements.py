from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
import time
from pathlib import Path
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
from adapters.stream_events import KIND_FILE_READ, KIND_TOOL_END
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
from orchestrator.her_v2.v3_prompt import (
    DEVELOPER_TOOLKIT_CONTRACT,
    compile_main_prompt,
)
from orchestrator.session_store import SessionStore
from tools.registry import ToolRegistry, ToolResult
from tools.smart_tools import adapt_legacy_result
from tools.tool_presentation import (
    OnDemandToolView,
    developer_toolkit_guidance_enabled,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Helpers for cross-process Windows OS lease tests
# ---------------------------------------------------------------------------


def _spawn_lease_holder(
    *,
    bridge_home: Path,
    workspace_dir: Path,
    access_roots: list[Path],
    tool_name: str,
    arguments: dict,
    ready_file: Path,
    release_file: Path,
) -> subprocess.Popen:
    script = (
        "import asyncio, json, sys, time\n"
        "from pathlib import Path\n"
        "from types import SimpleNamespace\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from orchestrator.execution_resources import tool_resource\n"
        "cfg = json.loads(sys.argv[2])\n"
        "reg = SimpleNamespace(\n"
        "    workspace_dir=Path(cfg['workspace_dir']),\n"
        "    access_roots=[Path(p) for p in cfg['access_roots']],\n"
        "    _effective_audit_context=lambda: {'global_config': SimpleNamespace(bridge_home=Path(cfg['bridge_home']))},\n"
        ")\n"
        "async def main():\n"
        "    async with tool_resource(reg, cfg['tool_name'], cfg['arguments']):\n"
        "        Path(cfg['ready_file']).write_text('ready', encoding='utf-8')\n"
        "        deadline = time.monotonic() + 10.0\n"
        "        while not Path(cfg['release_file']).exists() and time.monotonic() < deadline:\n"
        "            await asyncio.sleep(0.02)\n"
        "asyncio.run(main())\n"
    )
    payload = json.dumps(
        {
            "bridge_home": str(bridge_home),
            "workspace_dir": str(workspace_dir),
            "access_roots": [str(p) for p in access_roots],
            "tool_name": tool_name,
            "arguments": arguments,
            "ready_file": str(ready_file),
            "release_file": str(release_file),
        }
    )
    return subprocess.Popen(
        [sys.executable, "-I", "-B", "-c", script, str(REPO_ROOT), payload],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _wait_for_file(path: Path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert path.exists(), f"timed out waiting for {path}"


# ---------------------------------------------------------------------------
# Fix 1 (WP2): L0 prompt preservation & opt-in Developer Toolkit guidance
# ---------------------------------------------------------------------------


def test_developer_toolkit_prompt_guidance_is_default_for_wildcard_and_respects_narrow_or_off(tmp_path):
    # 1. Baseline compile_main_prompt without flag preserves frozen L0 baseline.
    base_sys, _ = compile_main_prompt(
        pcm_input={"sections": [], "current_request": "fix bug"},
        fallback_request="fix bug",
        context={},
        tools_available=True,
    )
    assert DEVELOPER_TOOLKIT_CONTRACT not in base_sys
    assert "text_replace" not in base_sys

    # 2. Narrow allowlist without toolkit tools does NOT enable guidance.
    narrow_reg = ToolRegistry(["file_read", "file_write", "file_list"], tmp_path, tmp_path, {})
    narrow_names = {d["function"]["name"] for d in narrow_reg.get_tool_definitions()}
    assert "text_replace" not in narrow_names
    assert not developer_toolkit_guidance_enabled(
        narrow_reg.tool_options, narrow_names, model="deepseek-v4-pro", is_herv3=True
    )

    # 3. Default wildcard registry enables Developer Toolkit, on-demand view, and guidance out of the box.
    wildcard_reg = ToolRegistry(["*"], tmp_path, tmp_path, {})
    wildcard_names = {d["function"]["name"] for d in wildcard_reg.get_tool_definitions()}
    assert {"text_replace", "code_search", "git_inspect", "tool_find"} <= wildcard_names
    assert developer_toolkit_guidance_enabled(
        wildcard_reg.tool_options, wildcard_names, model="deepseek-v4-pro", is_herv3=True
    )
    optin_sys, _ = compile_main_prompt(
        pcm_input={"sections": [], "current_request": "fix bug"},
        fallback_request="fix bug",
        context={},
        tools_available=True,
        developer_toolkit_enabled=True,
    )
    assert optin_sys.count(DEVELOPER_TOOLKIT_CONTRACT) == 1
    for expected_token in (
        "code_search",
        "project_locate",
        "git_inspect",
        "text_replace",
        "tool_find",
        "independent read-only inspections",
        "PowerShell 5.1",
    ):
        assert expected_token in optin_sys

    # 4. Repeated prompt compilation -> never duplicated.
    optin_sys_repeat, _ = compile_main_prompt(
        pcm_input={"sections": [], "current_request": "fix bug"},
        fallback_request="fix bug",
        context={"developer_toolkit_enabled": True},
        tools_available=True,
        developer_toolkit_enabled=True,
    )
    assert optin_sys_repeat.count(DEVELOPER_TOOLKIT_CONTRACT) == 1
    assert optin_sys_repeat == optin_sys

    # 5. Explicit opt-out (`mode: "off"` / `"disabled"`) disables guidance and on-demand view.
    assert not developer_toolkit_guidance_enabled(
        {"herv3_tool_presentation": {"mode": "off"}},
        wildcard_names,
        model="deepseek-v4-pro",
        is_herv3=True,
    )
    assert not developer_toolkit_guidance_enabled(
        {"herv3_tool_presentation": {"mode": "disabled"}},
        wildcard_names,
        model="deepseek-v4-pro",
        is_herv3=True,
    )
    no_tools_sys, _ = compile_main_prompt(
        pcm_input={"sections": [], "current_request": "fix bug"},
        fallback_request="fix bug",
        context={"developer_toolkit_enabled": True},
        tools_available=False,
        developer_toolkit_enabled=True,
    )
    assert DEVELOPER_TOOLKIT_CONTRACT not in no_tools_sys

    # 6. On-demand tool view compacts initial schemas (including file_write + text_replace + code_search).
    view = OnDemandToolView(wildcard_reg)
    starter_names = {d["function"]["name"] for d in view.get_tool_definitions()}
    assert {"text_replace", "code_search", "tool_find", "file_write", "file_read"} <= starter_names
    assert len(starter_names) < len(wildcard_names)


# ---------------------------------------------------------------------------
# Fix 2: Argument-aware conservative read-only shell classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "git status --short",
        "git diff --stat",
        "git diff -- \"file with spaces.py\"",
        "git diff -- '--literal-file.txt'",
        "git log -5",
        "git log --oneline -n 10",
        "git -C C:\\repo\\project diff --name-only",
        "git branch --list",
        "git tag --list",
        "git rev-parse --short HEAD",
        "git remote -v",
        "rg -n pattern src/",
        "rg -n \"some search pattern\" src/",
        "rg -n \"foo\"'bar' src/",
        "rg -n --glob *.py 'def execute' C:\\repo\\src",
        "Get-ChildItem -Path C:\\repo\\src -Name",
        "Get-Content -LiteralPath 'C:\\repo\\app.py' -TotalCount 50",
        'Get-Content -LiteralPath "C:\\Projects\\my project\\file.txt"',
        "Select-String -Pattern 'def foo' -Path C:\\repo\\src\\app.py",
        "Test-Path -LiteralPath C:\\repo\\src\\app.py",
    ],
)
def test_readonly_shell_classifier_accepts_proven_readonly_commands(command):
    assert _is_strictly_readonly_shell_command(command) is True


@pytest.mark.parametrize(
    "command",
    [
        "git diff --output=file.txt",
        "git diff --output file.txt",
        "git diff '--output=shared.py'",
        'git diff "--output=shared.py"',
        "git diff '--output' shared.py",
        'git diff "--output" shared.py',
        "git diff '--out'\"put=shared.py\"",
        "git diff '--' '--output=shared.py'",
        "git diff --ext-diff",
        "git -c core.fsmonitor=evil status",
        "git '-c' core.fsmonitor=evil status",
        'git "-c" diff.external=python diff',
        "git -c diff.external=python diff",
        "rg --pre=python pattern .",
        "rg --pre python pattern .",
        "rg '--pre=python' pattern .",
        'rg "--pre" python pattern .',
        "rg --pre-glob=*.md --pre=cat pattern .",
        "rg --unknown-flag pattern .",
        "rg '--unknown-flag' pattern .",
        'rg -n "foo \\"bar\\"" src/',
        'git diff "unclosed.py',
        "rg -n 'unclosed src/",
        'rg -n "a;b" src/',
        'rg -n "a|b" src/',
        'git diff "a>b"',
        "git branch new-branch",
        "git branch -D old-branch",
        "git tag new-tag",
        "git tag -d old-tag",
        "git commit -m message",
        "git remote add origin https://example.com/repo.git",
        "python script.py",
        "pytest -q",
        "command > file.txt",
        "git status > out.txt",
        "command1 && command2",
        "git status; Remove-Item app.py",
        "Get-Content C:\\repo\\app.py | Set-Content C:\\repo\\copy.py",
        "Get-ChildItem $env:USERPROFILE",
        'Get-Content "$env:TEMP\\x.txt"',
        "Get-Content $(whoami).txt",
        "Invoke-Expression 'git status'",
        "powershell -Command \"git status\"",
        "Get-ChildItem -UnknownSwitch",
        "git status `n rm foo",
        "",
    ],
)
def test_readonly_shell_classifier_rejects_mutating_unknown_or_ambiguous_commands(command):
    assert _is_strictly_readonly_shell_command(command) is False


# ---------------------------------------------------------------------------
# Fix 3: Cross-Workzone Shell write-lock scope & same-file serialization
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
async def test_unknown_shell_script_blocks_competing_edits_in_targeted_workzone_only(tmp_path):
    ws_a = tmp_path / "agents" / "A"
    ws_b = tmp_path / "agents" / "B"
    ws_c = tmp_path / "agents" / "C"
    for d in (ws_a, ws_b, ws_c):
        d.mkdir(parents=True)
    shared_ctx = lambda: {"global_config": SimpleNamespace(bridge_home=tmp_path)}

    # Agent A has writable access to both ws_a and ws_b (not ws_c).
    reg_a = SimpleNamespace(workspace_dir=ws_a, access_roots=[ws_a, ws_b], _effective_audit_context=shared_ctx)
    reg_b = SimpleNamespace(workspace_dir=ws_b, access_roots=[ws_b], _effective_audit_context=shared_ctx)
    reg_c = SimpleNamespace(workspace_dir=ws_c, access_roots=[ws_c], _effective_audit_context=shared_ctx)

    # 1. Opaque shell without identifiable literal file write targets MUST NOT hold
    #    directory-wide locks blocking unrelated edits in ws_a, ws_b, or ws_c.
    async with tool_resource(reg_a, "shell", {"command": "python update_project.py"}):
        async with tool_resource(reg_a, "text_replace", {"path": "local.py", "find": "a", "replace": "b"}):
            pass
        async with tool_resource(reg_b, "text_replace", {"path": "target.py", "find": "a", "replace": "b"}):
            pass
        async with tool_resource(reg_c, "text_replace", {"path": "unrelated.py", "find": "1", "replace": "2"}):
            pass

    # 2. Shell with a literal file redirection coordinates on ws_b/target.py while
    #    leaving sibling files in ws_b and unrelated ws_c concurrent.
    tr_entered = asyncio.Event()
    fw_entered = asyncio.Event()
    ap_entered = asyncio.Event()

    async def tr_b():
        async with tool_resource(reg_b, "text_replace", {"path": "target.py", "find": "a", "replace": "b"}):
            tr_entered.set()

    async def fw_b():
        async with tool_resource(reg_b, "file_write", {"path": "target.py", "content": "x"}):
            fw_entered.set()

    async def ap_b():
        async with tool_resource(reg_b, "apply_patch", {"path": "target.py", "patch": "@@ -1 +1 @@"}):
            ap_entered.set()

    async with tool_resource(reg_a, "shell", {"command": f"cd '{ws_b}'; echo updated > target.py"}):
        tasks = [
            asyncio.create_task(tr_b()),
            asyncio.create_task(fw_b()),
            asyncio.create_task(ap_b()),
        ]
        # Sibling file in the SAME directory and unrelated ws_c must NOT be blocked.
        async with tool_resource(reg_b, "text_replace", {"path": "other.py", "find": "a", "replace": "b"}):
            pass
        async with tool_resource(reg_c, "text_replace", {"path": "unrelated.py", "find": "1", "replace": "2"}):
            pass
        await asyncio.sleep(0.08)
        assert not tr_entered.is_set(), "text_replace on ws_b/target.py must wait for shell redirecting to it"
        assert not fw_entered.is_set(), "file_write on ws_b/target.py must wait for shell redirecting to it"
        assert not ap_entered.is_set(), "apply_patch on ws_b/target.py must wait for shell redirecting to it"

    await asyncio.wait_for(asyncio.gather(*tasks), timeout=3.0)
    assert tr_entered.is_set() and fw_entered.is_set() and ap_entered.is_set()


@pytest.mark.asyncio
async def test_readonly_shell_inspections_proceed_concurrently_while_git_diff_output_blocks(tmp_path):
    registry = SimpleNamespace(
        workspace_dir=tmp_path,
        access_roots=[tmp_path],
        _effective_audit_context=lambda: {},
    )
    async with tool_resource(registry, "shell", {"command": "git status --short"}):
        async with tool_resource(registry, "shell", {"command": "rg -n pattern src/"}):
            async with tool_resource(registry, "text_replace", {"path": "a.py", "find": "1", "replace": "2"}):
                pass

    # git diff --output=shared.py writes to a file and must block competing edits.
    entered = asyncio.Event()

    async def edit():
        async with tool_resource(registry, "text_replace", {"path": "shared.py", "find": "1", "replace": "2"}):
            entered.set()

    async with tool_resource(registry, "shell", {"command": "git diff --output=shared.py"}):
        task = asyncio.create_task(edit())
        await asyncio.sleep(0.08)
        assert not entered.is_set()
    await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_multiple_leases_in_opposite_order_never_deadlock(tmp_path):
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b", encoding="utf-8")
    registry = SimpleNamespace(
        workspace_dir=tmp_path,
        access_roots=[tmp_path],
        _effective_audit_context=lambda: {},
    )

    async def move_ab():
        for _ in range(5):
            async with tool_resource(registry, "file_manage", {"operation": "move", "source": "a.txt", "destination": "b.txt"}):
                await asyncio.sleep(0.01)

    async def move_ba():
        for _ in range(5):
            async with tool_resource(registry, "file_manage", {"operation": "move", "source": "b.txt", "destination": "a.txt"}):
                await asyncio.sleep(0.01)

    await asyncio.wait_for(asyncio.gather(move_ab(), move_ba()), timeout=3.0)


@pytest.mark.asyncio
async def test_cross_process_shell_and_file_mutation_locking_on_windows(tmp_path):
    ws_a = tmp_path / "agents" / "A"
    ws_b = tmp_path / "agents" / "B"
    ws_a.mkdir(parents=True)
    ws_b.mkdir(parents=True)
    ready = tmp_path / "ready"
    release = tmp_path / "release"

    proc = _spawn_lease_holder(
        bridge_home=tmp_path,
        workspace_dir=ws_a,
        access_roots=[ws_a, ws_b],
        tool_name="shell",
        arguments={"command": f"cd '{ws_b}'; echo updated > shared.py"},
        ready_file=ready,
        release_file=release,
    )
    try:
        _wait_for_file(ready)
        reg_b = SimpleNamespace(
            workspace_dir=ws_b,
            access_roots=[ws_b],
            _effective_audit_context=lambda: {"global_config": SimpleNamespace(bridge_home=tmp_path)},
        )
        # Unrelated sibling file in the SAME directory must proceed concurrently.
        async with tool_resource(reg_b, "text_replace", {"path": "unrelated.py", "find": "a", "replace": "b"}):
            pass

        entered = asyncio.Event()

        async def edit_b():
            async with tool_resource(reg_b, "text_replace", {"path": "shared.py", "find": "a", "replace": "b"}):
                entered.set()

        task = asyncio.create_task(edit_b())
        await asyncio.sleep(0.15)
        assert not entered.is_set(), "cross-process shell redirecting to shared.py must block text_replace on shared.py"
        release.write_text("go", encoding="utf-8")
        await asyncio.wait_for(task, timeout=4.0)
        assert entered.is_set()
    finally:
        release.write_text("go", encoding="utf-8")
        stdout, stderr = proc.communicate(timeout=5)
    assert proc.returncode == 0, stderr.decode(errors="replace")


# ---------------------------------------------------------------------------
# Fix 4: file_manage mkdir / copy / move directory & file locking
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_file_manage_mkdir_uses_destination_and_allows_unrelated_file_edit(tmp_path):
    registry = SimpleNamespace(
        workspace_dir=tmp_path,
        access_roots=[tmp_path],
        _effective_audit_context=lambda: {},
    )
    same_dir_entered = asyncio.Event()

    async def write_inside_new_dir():
        async with tool_resource(registry, "file_write", {"path": "new-folder/child.txt"}):
            same_dir_entered.set()

    async with tool_resource(registry, "file_manage", {"operation": "mkdir", "destination": "new-folder"}):
        # Unrelated file edit must proceed concurrently.
        async with tool_resource(registry, "file_write", {"path": "unrelated.txt"}):
            pass
        # Write inside the same directory being created must wait.
        task = asyncio.create_task(write_inside_new_dir())
        await asyncio.sleep(0.08)
        assert not same_dir_entered.is_set()

    await asyncio.wait_for(task, timeout=2.0)
    assert same_dir_entered.is_set()


@pytest.mark.asyncio
async def test_file_manage_recursive_copy_and_dir_move_block_child_file_writes(tmp_path):
    src_dir = tmp_path / "src_tree"
    src_dir.mkdir()
    (src_dir / "child.py").write_text("v1", encoding="utf-8")
    dst_dir = tmp_path / "dst_tree"

    registry = SimpleNamespace(
        workspace_dir=tmp_path,
        access_roots=[tmp_path],
        _effective_audit_context=lambda: {},
    )

    # 1. Recursive copy protects source subtree AND destination subtree, while unrelated file stays concurrent.
    src_child_entered = asyncio.Event()
    dst_child_entered = asyncio.Event()

    async def edit_src_child():
        async with tool_resource(registry, "text_replace", {"path": "src_tree/child.py", "find": "v1", "replace": "v2"}):
            src_child_entered.set()

    async def edit_dst_child():
        async with tool_resource(registry, "file_write", {"path": "dst_tree/child.py"}):
            dst_child_entered.set()

    async with tool_resource(
        registry,
        "file_manage",
        {"operation": "copy", "source": "src_tree", "destination": "dst_tree", "recursive": True},
    ):
        async with tool_resource(registry, "file_write", {"path": "other.txt"}):
            pass
        t1 = asyncio.create_task(edit_src_child())
        t2 = asyncio.create_task(edit_dst_child())
        await asyncio.sleep(0.08)
        assert not src_child_entered.is_set(), "recursive copy must block concurrent child edit in source"
        assert not dst_child_entered.is_set(), "recursive copy must block concurrent child edit in destination"

    await asyncio.wait_for(asyncio.gather(t1, t2), timeout=2.0)

    # 2. Directory move blocks concurrent child edit in source.
    move_child_entered = asyncio.Event()

    async def edit_during_move():
        async with tool_resource(registry, "text_replace", {"path": "src_tree/child.py", "find": "v1", "replace": "v2"}):
            move_child_entered.set()

    async with tool_resource(
        registry,
        "file_manage",
        {"operation": "move", "source": "src_tree", "destination": "moved_tree", "recursive": True},
    ):
        t3 = asyncio.create_task(edit_during_move())
        await asyncio.sleep(0.08)
        assert not move_child_entered.is_set()
    await asyncio.wait_for(t3, timeout=2.0)


@pytest.mark.asyncio
async def test_file_manage_single_file_copy_and_existing_dir_destination_and_cross_workzone_move(tmp_path):
    ws_a = tmp_path / "ws_a"
    ws_b = tmp_path / "ws_b"
    existing_dir = ws_a / "existing_dir"
    ws_a.mkdir()
    ws_b.mkdir()
    existing_dir.mkdir()
    (ws_a / "src.txt").write_text("hello", encoding="utf-8")

    shared_ctx = lambda: {"global_config": SimpleNamespace(bridge_home=tmp_path)}
    registry = SimpleNamespace(workspace_dir=ws_a, access_roots=[ws_a, ws_b], _effective_audit_context=shared_ctx)

    # Copy single file into an existing directory -> derives existing_dir/src.txt.
    src_edit_entered = asyncio.Event()
    dst_edit_entered = asyncio.Event()

    async def edit_src():
        async with tool_resource(registry, "text_replace", {"path": "src.txt", "find": "h", "replace": "H"}):
            src_edit_entered.set()

    async def edit_derived_dst():
        async with tool_resource(registry, "file_write", {"path": "existing_dir/src.txt"}):
            dst_edit_entered.set()

    async with tool_resource(
        registry,
        "file_manage",
        {"operation": "copy", "source": "src.txt", "destination": "existing_dir"},
    ):
        # Different file inside existing_dir remains concurrent!
        async with tool_resource(registry, "file_write", {"path": "existing_dir/other.txt"}):
            pass
        t_src = asyncio.create_task(edit_src())
        t_dst = asyncio.create_task(edit_derived_dst())
        await asyncio.sleep(0.08)
        assert not src_edit_entered.is_set(), "single-file copy must block concurrent modification to source"
        assert not dst_edit_entered.is_set(), "copy into existing dir must block edits to derived destination file"

    await asyncio.wait_for(asyncio.gather(t_src, t_dst), timeout=2.0)

    # Move across two authorised Workzones blocks both endpoints but not unrelated files.
    move_dst_entered = asyncio.Event()

    async def edit_ws_b_target():
        async with tool_resource(registry, "file_write", {"path": str(ws_b / "moved.txt")}):
            move_dst_entered.set()

    async with tool_resource(
        registry,
        "file_manage",
        {"operation": "move", "source": str(ws_a / "src.txt"), "destination": str(ws_b / "moved.txt")},
    ):
        async with tool_resource(registry, "file_write", {"path": str(ws_b / "unrelated.txt")}):
            pass
        t_mv = asyncio.create_task(edit_ws_b_target())
        await asyncio.sleep(0.08)
        assert not move_dst_entered.is_set()
    await asyncio.wait_for(t_mv, timeout=2.0)


@pytest.mark.asyncio
async def test_cross_process_recursive_copy_blocks_child_text_replace(tmp_path):
    src_dir = tmp_path / "tree"
    src_dir.mkdir()
    (src_dir / "mod.py").write_text("x = 1\n", encoding="utf-8")
    ready = tmp_path / "copy_ready"
    release = tmp_path / "copy_release"

    proc = _spawn_lease_holder(
        bridge_home=tmp_path,
        workspace_dir=tmp_path,
        access_roots=[tmp_path],
        tool_name="file_manage",
        arguments={"operation": "copy", "source": "tree", "destination": "tree_backup", "recursive": True},
        ready_file=ready,
        release_file=release,
    )
    try:
        _wait_for_file(ready)
        reg = SimpleNamespace(
            workspace_dir=tmp_path,
            access_roots=[tmp_path],
            _effective_audit_context=lambda: {"global_config": SimpleNamespace(bridge_home=tmp_path)},
        )
        entered = asyncio.Event()

        async def edit_child():
            async with tool_resource(reg, "text_replace", {"path": "tree/mod.py", "find": "1", "replace": "2"}):
                entered.set()

        task = asyncio.create_task(edit_child())
        await asyncio.sleep(0.15)
        assert not entered.is_set(), "cross-process recursive copy must block child text_replace"
        release.write_text("go", encoding="utf-8")
        await asyncio.wait_for(task, timeout=4.0)
        assert entered.is_set()
    finally:
        release.write_text("go", encoding="utf-8")
        stdout, stderr = proc.communicate(timeout=5)
    assert proc.returncode == 0, stderr.decode(errors="replace")


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

    first_claim = store.claim_run_delivery_outbox(
        request_id=accepted.request_id,
        owner_id=owner,
        surface="telegram",
        channel_key="7",
        worker_id="sweep-worker-a",
    )
    assert first_claim["state"] == "claimed" and "claim" in first_claim

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
# Fix 5 (WP7): Bounded parallel read-only execution & airtight Tool ID cleanup
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


def _four_read_calls() -> list[dict]:
    return [
        {
            "id": f"tc-{i}",
            "type": "function",
            "function": {"name": "file_read", "arguments": json.dumps({"path": f"f{i}.py"})},
        }
        for i in range(4)
    ]


@pytest.mark.asyncio
async def test_parallel_provider_followup_keeps_json_and_advisory_separate(tmp_path):
    adapter = _deepseek_adapter(tmp_path)

    class JsonRegistry:
        permission_durable_wait = False

        async def execute(self, tool_name, arguments, tool_call_id=""):
            details = {}
            if tool_call_id == "tc-0":
                details["smart_repeat_warning"] = "same_result_repeated"
                details["smart_warning"] = {
                    "code": "same_result_repeated",
                    "message": "Untrusted text must not become a system instruction.",
                }
            return ToolResult(
                tool_call_id=tool_call_id,
                output=json.dumps({"path": arguments["path"], "ok": True}),
                is_error=False,
                details=details,
            )

    adapter.tool_registry = JsonRegistry()
    calls = _four_read_calls()
    messages = [{"role": "assistant", "tool_calls": calls}]
    await adapter._run_tool_calls(calls, messages, None)
    assert [message["role"] for message in messages] == [
        "assistant", "tool", "tool", "tool", "tool", "system",
    ]
    for index, message in enumerate(messages[1:5]):
        assert message["tool_call_id"] == f"tc-{index}"
        assert json.loads(message["content"]) == {"path": f"f{index}.py", "ok": True}
    assert "not a user request" in messages[5]["content"]
    assert "HASHI_SMART_GUIDANCE" in messages[5]["content"]
    assert "Untrusted text" not in messages[5]["content"]
    assert "tc-0" in messages[5]["content"]


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
    tool_calls = _four_read_calls()
    messages: list[dict] = [{"role": "assistant", "tool_calls": tool_calls}]
    started = time.perf_counter()
    await adapter._run_tool_calls(tool_calls, messages, None)
    elapsed = time.perf_counter() - started

    assert peak_inflight >= 2, f"expected concurrent read-only execution, got peak={peak_inflight}"
    assert elapsed < 0.25, f"expected parallel wall time < 0.25s, got {elapsed:.3f}s"
    assert adapter._inflight_tool_bindings == {}
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
    assert adapter._inflight_tool_bindings == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stage",
    [
        "before_first_start",
        "during_first_start",
        "during_later_start",
        "while_running",
        "after_one_completes",
        "during_tool_end",
        "during_result_recording",
    ],
)
async def test_parallel_readonly_batch_cancellation_releases_all_tool_id_claims(tmp_path, monkeypatch, stage):
    adapter = _deepseek_adapter(tmp_path)
    tool_calls = _four_read_calls()
    executed_ids: list[str] = []
    running_count = 0
    first_done = asyncio.Event()
    all_started = asyncio.Event()
    started_count = 0

    class StageRegistry:
        permission_durable_wait = False

        async def execute(self, tool_name, arguments, tool_call_id=""):
            nonlocal running_count, started_count
            running_count += 1
            started_count += 1
            executed_ids.append(tool_call_id)
            if started_count == 4:
                all_started.set()
            try:
                if stage == "after_one_completes" and tool_call_id == "tc-0":
                    res = ToolResult(tool_call_id=tool_call_id, output="fast:f0.py")
                    first_done.set()
                    return res
                if stage in {"while_running", "after_one_completes"}:
                    await asyncio.sleep(5.0)
                return ToolResult(tool_call_id=tool_call_id, output=f"ok:{arguments['path']}")
            finally:
                running_count -= 1

    adapter.tool_registry = StageRegistry()
    start_events = 0
    end_events = 0

    async def on_event(event):
        nonlocal start_events, end_events
        if event.kind == KIND_FILE_READ:
            start_events += 1
            if stage == "during_first_start" and start_events == 1:
                raise asyncio.CancelledError("stop on first start")
            if stage == "during_later_start" and start_events == 3:
                raise asyncio.CancelledError("stop on third start")
        elif event.kind == KIND_TOOL_END:
            end_events += 1
            if stage == "during_tool_end" and end_events == 2:
                raise asyncio.CancelledError("stop on second tool_end")

    if stage == "during_result_recording":
        orig = adapter._tool_result_messages
        calls = 0

        def fail_recording(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise asyncio.CancelledError("stop during recording")
            return orig(*args, **kwargs)

        monkeypatch.setattr(adapter, "_tool_result_messages", fail_recording)

    messages: list[dict] = [{"role": "assistant", "tool_calls": tool_calls}]
    batch_task = asyncio.create_task(adapter._run_tool_calls(tool_calls, messages, on_event))

    if stage == "before_first_start":
        batch_task.cancel()
    elif stage == "while_running":
        await asyncio.wait_for(all_started.wait(), timeout=2.0)
        batch_task.cancel()
    elif stage == "after_one_completes":
        await asyncio.wait_for(first_done.wait(), timeout=2.0)
        await asyncio.sleep(0.02)
        batch_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await batch_task

    # Every claim must be released and zero tasks left running.
    assert adapter._inflight_tool_bindings == {}
    assert running_count == 0

    # Completed tools recorded before cancellation must NOT be re-executed on subsequent call.
    if stage == "after_one_completes":
        recorded_ids = [m["tool_call_id"] for m in messages if m.get("role") == "tool"]
        assert "tc-0" in recorded_ids
        # Re-invoking the batch reuses tc-0 without executing it a second time.
        re_exec: list[str] = []

        class ReplayCheckRegistry:
            permission_durable_wait = False

            async def execute(self, tool_name, arguments, tool_call_id=""):
                re_exec.append(tool_call_id)
                return ToolResult(tool_call_id=tool_call_id, output=f"re:{arguments['path']}")

        adapter.tool_registry = ReplayCheckRegistry()
        await adapter._run_tool_calls(tool_calls, messages, None)
        assert "tc-0" not in re_exec
        assert adapter._inflight_tool_bindings == {}
    elif stage == "during_tool_end":
        # All 4 results were already recorded in exact original order before KIND_TOOL_END cancelled.
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        assert [m["tool_call_id"] for m in tool_msgs] == ["tc-0", "tc-1", "tc-2", "tc-3"]
    else:
        # Tool IDs can be safely reused by a later authorised call.
        class ReuseRegistry:
            permission_durable_wait = False

            async def execute(self, tool_name, arguments, tool_call_id=""):
                return ToolResult(tool_call_id=tool_call_id, output=f"later:{arguments['path']}")

        adapter.tool_registry = ReuseRegistry()
        fresh_messages: list[dict] = [{"role": "assistant", "tool_calls": tool_calls}]
        await adapter._run_tool_calls(tool_calls, fresh_messages, None)
        assert adapter._inflight_tool_bindings == {}
        assert [m["tool_call_id"] for m in fresh_messages if m.get("role") == "tool"] == [
            "tc-0",
            "tc-1",
            "tc-2",
            "tc-3",
        ]


@pytest.mark.asyncio
async def test_parallel_readonly_batch_registry_exception_cancels_siblings_and_clears_claims(tmp_path):
    adapter = _deepseek_adapter(tmp_path)
    tool_calls = _four_read_calls()
    running = 0

    class ExplodingRegistry:
        permission_durable_wait = False

        async def execute(self, tool_name, arguments, tool_call_id=""):
            nonlocal running
            running += 1
            try:
                if tool_call_id == "tc-1":
                    await asyncio.sleep(0.02)
                    raise RuntimeError("boom in registry")
                await asyncio.sleep(5.0)
                return ToolResult(tool_call_id=tool_call_id, output="never")
            finally:
                running -= 1

    adapter.tool_registry = ExplodingRegistry()
    messages: list[dict] = [{"role": "assistant", "tool_calls": tool_calls}]
    with pytest.raises(RuntimeError, match="boom in registry"):
        await adapter._run_tool_calls(tool_calls, messages, None)

    assert adapter._inflight_tool_bindings == {}
    assert running == 0


@pytest.mark.asyncio
async def test_real_git_quoted_diff_output_writes_file_and_serializes_with_text_replace(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True)
    tracked = repo / "app.py"
    tracked.write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    tracked.write_text("x = 2\n", encoding="utf-8")

    quoted_cmd = "git diff '--output=shared.py'"
    if sys.platform == "win32":
        run = subprocess.run(
            ["powershell", "-NoProfile", "-Command", quoted_cmd],
            cwd=repo,
            capture_output=True,
            text=True,
        )
    else:
        run = subprocess.run(["sh", "-c", quoted_cmd], cwd=repo, capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    written = repo / "shared.py"
    assert written.exists() and "+x = 2" in written.read_text(encoding="utf-8")
    assert _is_strictly_readonly_shell_command(quoted_cmd) is False

    # Concurrency acceptance: holding text_replace on shared.py must block quoted git diff --output
    # (both in-process and cross-process), while ordinary git status still overlaps.
    ctx = lambda: {"global_config": SimpleNamespace(bridge_home=tmp_path)}
    reg = SimpleNamespace(workspace_dir=repo, access_roots=[repo], _effective_audit_context=ctx)
    entered = asyncio.Event()

    async def run_quoted_diff():
        async with tool_resource(reg, "shell", {"command": quoted_cmd}):
            entered.set()

    async with tool_resource(reg, "text_replace", {"path": "shared.py", "find": "x = 2", "replace": "x = 3"}):
        # Ordinary read-only git status still overlaps with the independent edit.
        async with tool_resource(reg, "shell", {"command": "git status --short"}):
            pass
        task = asyncio.create_task(run_quoted_diff())
        await asyncio.sleep(0.08)
        assert not entered.is_set(), "quoted git diff --output must wait for text_replace lease"
    await asyncio.wait_for(task, timeout=2.0)
    assert entered.is_set()

    # Cross-process confirmation on Windows/OS file leases.
    ready = tmp_path / "tr_ready"
    release = tmp_path / "tr_release"
    proc = _spawn_lease_holder(
        bridge_home=tmp_path,
        workspace_dir=repo,
        access_roots=[repo],
        tool_name="text_replace",
        arguments={"path": "shared.py", "find": "x = 2", "replace": "x = 3"},
        ready_file=ready,
        release_file=release,
    )
    try:
        _wait_for_file(ready)
        entered_xp = asyncio.Event()

        async def run_quoted_diff_xp():
            async with tool_resource(reg, "shell", {"command": quoted_cmd}):
                entered_xp.set()

        t_xp = asyncio.create_task(run_quoted_diff_xp())
        await asyncio.sleep(0.15)
        assert not entered_xp.is_set(), "cross-process text_replace must block quoted git diff --output"
        release.write_text("go", encoding="utf-8")
        await asyncio.wait_for(t_xp, timeout=4.0)
        assert entered_xp.is_set()
    finally:
        release.write_text("go", encoding="utf-8")
        _out, err = proc.communicate(timeout=5)
    assert proc.returncode == 0, err.decode(errors="replace")


@pytest.mark.asyncio
async def test_parallel_readonly_batch_preserves_completed_result_on_sibling_exception_and_recovery(tmp_path):
    """Test A + Test E: A completes, B raises, C & D cancelled; A survives & is never replayed."""
    adapter = _deepseek_adapter(tmp_path)
    tool_calls = _four_read_calls()
    running = 0
    a_done = asyncio.Event()

    class RegistryA:
        permission_durable_wait = False

        async def execute(self, tool_name, arguments, tool_call_id=""):
            nonlocal running
            running += 1
            try:
                if tool_call_id == "tc-0":
                    res = ToolResult(tool_call_id=tool_call_id, output="saved:f0.py")
                    a_done.set()
                    return res
                if tool_call_id == "tc-1":
                    await a_done.wait()
                    await asyncio.sleep(0.02)
                    raise RuntimeError("tool B exploded")
                await asyncio.sleep(5.0)
                return ToolResult(tool_call_id=tool_call_id, output="never")
            finally:
                running -= 1

    adapter.tool_registry = RegistryA()
    messages: list[dict] = [{"role": "assistant", "tool_calls": tool_calls}]
    with pytest.raises(RuntimeError, match="tool B exploded"):
        await adapter._run_tool_calls(tool_calls, messages, None)

    assert adapter._inflight_tool_bindings == {}
    assert running == 0
    recorded = [m for m in messages if m.get("role") == "tool"]
    assert [(m["tool_call_id"], m["content"]) for m in recorded] == [("tc-0", "saved:f0.py")]

    # Recovery re-invocation: A reuses its preserved result, only B/C/D execute, zero duplicate messages.
    re_executed: list[str] = []

    class RecoveryRegistry:
        permission_durable_wait = False

        async def execute(self, tool_name, arguments, tool_call_id=""):
            re_executed.append(tool_call_id)
            return ToolResult(tool_call_id=tool_call_id, output=f"recovered:{arguments['path']}")

    adapter.tool_registry = RecoveryRegistry()
    await adapter._run_tool_calls(tool_calls, messages, None)

    assert re_executed == ["tc-1", "tc-2", "tc-3"]
    assert adapter._inflight_tool_bindings == {}
    final_tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert len(final_tool_msgs) == 4
    assert [m["tool_call_id"] for m in final_tool_msgs] == ["tc-0", "tc-1", "tc-2", "tc-3"]
    assert final_tool_msgs[0]["content"] == "saved:f0.py"


@pytest.mark.asyncio
async def test_parallel_readonly_batch_preserves_multiple_completed_results_in_original_order(tmp_path):
    """Test B + Test E: A and C complete, B fails, D cancelled; A and C survive in order."""
    adapter = _deepseek_adapter(tmp_path)
    tool_calls = _four_read_calls()
    a_done = asyncio.Event()
    c_done = asyncio.Event()
    running = 0

    class RegistryAC:
        permission_durable_wait = False

        async def execute(self, tool_name, arguments, tool_call_id=""):
            nonlocal running
            running += 1
            try:
                if tool_call_id == "tc-2":
                    res = ToolResult(tool_call_id=tool_call_id, output="saved:f2.py")
                    c_done.set()
                    return res
                if tool_call_id == "tc-0":
                    await c_done.wait()
                    res = ToolResult(tool_call_id=tool_call_id, output="saved:f0.py")
                    a_done.set()
                    return res
                if tool_call_id == "tc-1":
                    await a_done.wait()
                    await asyncio.sleep(0.02)
                    raise RuntimeError("tool B failed")
                await asyncio.sleep(5.0)
                return ToolResult(tool_call_id=tool_call_id, output="never")
            finally:
                running -= 1

    adapter.tool_registry = RegistryAC()
    messages: list[dict] = [{"role": "assistant", "tool_calls": tool_calls}]
    with pytest.raises(RuntimeError, match="tool B failed"):
        await adapter._run_tool_calls(tool_calls, messages, None)

    assert adapter._inflight_tool_bindings == {}
    assert running == 0
    recorded = [m for m in messages if m.get("role") == "tool"]
    # Even though C finished before A, recorded order must match original tool_calls order: A then C.
    assert [(m["tool_call_id"], m["content"]) for m in recorded] == [
        ("tc-0", "saved:f0.py"),
        ("tc-2", "saved:f2.py"),
    ]

    re_executed: list[str] = []

    class RecoveryRegistry:
        permission_durable_wait = False

        async def execute(self, tool_name, arguments, tool_call_id=""):
            re_executed.append(tool_call_id)
            return ToolResult(tool_call_id=tool_call_id, output=f"recovered:{arguments['path']}")

    adapter.tool_registry = RecoveryRegistry()
    await adapter._run_tool_calls(tool_calls, messages, None)
    assert re_executed == ["tc-1", "tc-3"]
    assert adapter._inflight_tool_bindings == {}
    final_ids = [m["tool_call_id"] for m in messages if m.get("role") == "tool"]
    assert len(final_ids) == 4 and set(final_ids) == {"tc-0", "tc-1", "tc-2", "tc-3"}


@pytest.mark.asyncio
async def test_parallel_readonly_batch_result_recording_exception_keeps_bindings_consistent(tmp_path, monkeypatch):
    """Test C: exception while translating result 2 leaves result 1 consistent and un-replayed."""
    from adapters.openrouter_api import _completed_tool_bindings

    adapter = _deepseek_adapter(tmp_path)
    tool_calls = _four_read_calls()

    class FastRegistry:
        permission_durable_wait = False

        async def execute(self, tool_name, arguments, tool_call_id=""):
            return ToolResult(tool_call_id=tool_call_id, output=f"ok:{arguments['path']}")

    adapter.tool_registry = FastRegistry()
    orig = adapter._tool_result_messages
    calls = 0

    def fail_on_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError("translation failure on second result")
        return orig(*args, **kwargs)

    monkeypatch.setattr(adapter, "_tool_result_messages", fail_on_second)
    messages: list[dict] = [{"role": "assistant", "tool_calls": tool_calls}]
    with pytest.raises(ValueError, match="translation failure on second result"):
        await adapter._run_tool_calls(tool_calls, messages, None)

    assert adapter._inflight_tool_bindings == {}
    bindings = _completed_tool_bindings(messages)
    assert set(bindings.keys()) == {"tc-0"}
    assert [(m["tool_call_id"], m["content"]) for m in messages if m.get("role") == "tool"] == [
        ("tc-0", "ok:f0.py")
    ]

    # Restore translator; re-invoking must NOT replay tc-0.
    monkeypatch.setattr(adapter, "_tool_result_messages", orig)
    re_executed: list[str] = []

    class RecoveryRegistry:
        permission_durable_wait = False

        async def execute(self, tool_name, arguments, tool_call_id=""):
            re_executed.append(tool_call_id)
            return ToolResult(tool_call_id=tool_call_id, output=f"rec:{arguments['path']}")

    adapter.tool_registry = RecoveryRegistry()
    await adapter._run_tool_calls(tool_calls, messages, None)
    assert "tc-0" not in re_executed
    assert re_executed == ["tc-1", "tc-2", "tc-3"]
    assert adapter._inflight_tool_bindings == {}
    final_ids = [m["tool_call_id"] for m in messages if m.get("role") == "tool"]
    assert len(final_ids) == 4 and len(set(final_ids)) == 4


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
