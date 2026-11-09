from __future__ import annotations

import ctypes
from ctypes import wintypes
from contextlib import nullcontext
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from uuid import uuid4

import pytest

import orchestrator.live_runtime_acl as live_runtime_acl
from orchestrator.live_runtime_acl import (
    NativeProtectionTarget,
    apply_posix_read_only,
    apply_windows_read_only,
    build_native_protection_targets,
    restore_posix_access,
    restore_windows_access,
    windows_current_sid,
)
from orchestrator.live_runtime_protection import LiveRuntimePolicy
from orchestrator.runtime_contract import CORE_SOURCE_PATHS


def _policy(tmp_path: Path) -> LiveRuntimePolicy:
    code_root = tmp_path / "source"
    bridge_home = tmp_path / "instance"
    runtime_root = code_root / ".venv"
    for relative in CORE_SOURCE_PATHS:
        path = code_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# protected\n", encoding="utf-8")
    runtime_root.mkdir(parents=True)
    (runtime_root / "pyvenv.cfg").write_text("home = test\n", encoding="utf-8")
    policy_path = bridge_home / "state" / "platform" / "live-runtime-protection.json"
    policy_path.parent.mkdir(parents=True)
    policy_path.write_text("{}\n", encoding="utf-8")
    service_config = bridge_home / "platform" / "service.xml"
    service_config.parent.mkdir(parents=True)
    service_config.write_text("<service />\n", encoding="utf-8")
    secret = bridge_home / "private" / "restart.key"
    secret.parent.mkdir(parents=True)
    secret.write_text("test-only\n", encoding="utf-8")
    return LiveRuntimePolicy(
        code_root=code_root,
        bridge_home=bridge_home,
        runtime_roots=(runtime_root,),
        protected_write_paths=tuple(
            [code_root / relative for relative in CORE_SOURCE_PATHS]
            + [policy_path, service_config, secret]
        ),
        protected_read_paths=(secret,),
        service_targets=("hashi-test",),
        core_pid=None,
        policy_path=policy_path,
    )


def _windows_token_is_elevated() -> bool:
    return os.name == "nt" and bool(ctypes.windll.shell32.IsUserAnAdmin())


class _StartupInfo(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("reserved", wintypes.LPWSTR),
        ("desktop", wintypes.LPWSTR),
        ("title", wintypes.LPWSTR),
        ("x", wintypes.DWORD),
        ("y", wintypes.DWORD),
        ("x_size", wintypes.DWORD),
        ("y_size", wintypes.DWORD),
        ("x_count_chars", wintypes.DWORD),
        ("y_count_chars", wintypes.DWORD),
        ("fill_attribute", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("show_window", wintypes.WORD),
        ("reserved2_size", wintypes.WORD),
        ("reserved2", ctypes.POINTER(ctypes.c_byte)),
        ("stdin", wintypes.HANDLE),
        ("stdout", wintypes.HANDLE),
        ("stderr", wintypes.HANDLE),
    ]


class _ProcessInformation(ctypes.Structure):
    _fields_ = [
        ("process", wintypes.HANDLE),
        ("thread", wintypes.HANDLE),
        ("process_id", wintypes.DWORD),
        ("thread_id", wintypes.DWORD),
    ]


def _launch_as_windows_identity(username, password, argv, *, cwd):
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    security.CreateProcessWithLogonW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD, wintypes.LPVOID,
        wintypes.LPCWSTR, ctypes.POINTER(_StartupInfo),
        ctypes.POINTER(_ProcessInformation),
    ]
    security.CreateProcessWithLogonW.restype = wintypes.BOOL
    startup = _StartupInfo()
    startup.cb = ctypes.sizeof(startup)
    process = _ProcessInformation()
    command = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
    if not security.CreateProcessWithLogonW(
        username, ".", password, 0, argv[0], command, 0x400, None,
        str(cwd), ctypes.byref(startup), ctypes.byref(process),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    ctypes.windll.kernel32.CloseHandle(process.thread)
    return process


def _wait_for_path(path: Path, process, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        if ctypes.windll.kernel32.WaitForSingleObject(process.process, 0) == 0:
            raise RuntimeError("disposable runtime identity exited before its receipt")
        time.sleep(0.05)
    raise TimeoutError(f"disposable runtime identity did not publish {path.name}")


def _close_test_process(process):
    kernel = ctypes.windll.kernel32
    try:
        if kernel.WaitForSingleObject(process.process, 5000) != 0:
            kernel.TerminateProcess(process.process, 1)
            kernel.WaitForSingleObject(process.process, 5000)
    finally:
        kernel.CloseHandle(process.process)


@pytest.mark.skipif(os.name != "nt", reason="Windows recovery identity contract")
def test_windows_strong_lock_rejects_same_runtime_and_recovery_identity(tmp_path, monkeypatch):
    root = tmp_path / "unsafe-shared-identity"
    root.mkdir()
    calls = []
    monkeypatch.setattr(live_runtime_acl, "_run_windows_acl", lambda argv: calls.append(argv))
    with pytest.raises(PermissionError, match="recovery|maintenance"):
        apply_windows_read_only(
            (NativeProtectionTarget(root, recursive=True, kind="runtime"),),
            runtime_sid=windows_current_sid(), lock_owner=True,
        )
    assert calls == []


@pytest.mark.skipif(os.name != "nt", reason="Windows non-mutating verification")
def test_windows_verify_never_creates_a_probe_in_the_installation(tmp_path, monkeypatch):
    from scripts.protect_live_runtime import _verify_windows
    root = tmp_path / "sealed"
    root.mkdir()
    attempted = []
    monkeypatch.setattr(Path, "write_bytes", lambda path, data: attempted.append(path))
    reports = _verify_windows(
        (NativeProtectionTarget(root, recursive=True, kind="runtime"),),
        windows_current_sid(),
    )
    assert attempted == []
    assert reports[0]["write_denied"] is False
    assert list(root.iterdir()) == []


def test_deployment_policy_retains_incomplete_runtime_facts(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import scripts.protect_live_runtime as deployment
    from dataclasses import replace
    policy = replace(_policy(tmp_path), protection_complete=False,
                     qualification_error="RuntimeContractError")
    monkeypatch.setattr(deployment, "load_live_runtime_policy", lambda *args, **kwargs: policy)
    args = SimpleNamespace(
        code_root=policy.code_root, bridge_home=policy.bridge_home,
        secrets_path=None, instance_id="TEST", runtime_root=policy.runtime_roots[0],
        service_config=[], restart_secret=[], service_target=[],
    )
    observed = deployment._policy(args, include_unpublished=True)
    assert observed.protection_complete is False
    assert observed.qualification_error == "RuntimeContractError"


def test_native_target_plan_is_exact_and_never_protects_repo_or_workzone(tmp_path):
    policy = _policy(tmp_path)
    workzone = tmp_path / "workzones" / "agent"
    workzone.mkdir(parents=True)

    targets = build_native_protection_targets(policy)
    rendered = {target.path for target in targets}

    assert policy.code_root not in rendered
    assert policy.bridge_home not in rendered
    assert workzone not in rendered
    assert set(policy.runtime_roots).issubset(rendered)
    assert set(policy.protected_write_paths).issubset(rendered)
    assert next(target for target in targets if target.path in policy.runtime_roots).recursive
    assert all(target.path.is_absolute() for target in targets)


def test_native_target_plan_accepts_exact_managed_python_prefix(
    tmp_path,
    monkeypatch,
):
    policy = _policy(tmp_path)
    runtime_root = tmp_path / "managed-python"
    executable = (
        runtime_root / "python.exe"
        if os.name == "nt"
        else runtime_root / "bin" / "python3"
    )
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"test-only")

    def report_prefix(argv, **_kwargs):
        assert Path(argv[0]) == executable
        return subprocess.CompletedProcess(argv, 0, f"{runtime_root}\n", "")

    monkeypatch.setattr(live_runtime_acl.subprocess, "run", report_prefix)
    policy = LiveRuntimePolicy(
        code_root=policy.code_root,
        bridge_home=policy.bridge_home,
        runtime_roots=(runtime_root,),
        protected_write_paths=policy.protected_write_paths,
        protected_read_paths=policy.protected_read_paths,
        service_targets=policy.service_targets,
        core_pid=policy.core_pid,
        policy_path=policy.policy_path,
    )

    targets = build_native_protection_targets(policy)

    assert any(target.path == runtime_root for target in targets)


@pytest.mark.platform
@pytest.mark.skipif(os.name != "nt", reason="Windows DACL contract")
def test_windows_acl_rejects_real_writes_and_restores_disposable_target(tmp_path):
    root = tmp_path / "live-runtime"
    root.mkdir()
    child = root / "package.py"
    child.write_text("before\n", encoding="utf-8")
    sid = windows_current_sid()
    targets = (NativeProtectionTarget(root, recursive=True, kind="runtime"),)
    backup = tmp_path / "acl-recovery.json"
    live_runtime_acl._run_windows_acl(["icacls.exe", str(child), "/grant:r", f"*{sid}:(F)", "/inheritance:r"])
    before = {path: live_runtime_acl._windows_get_sddl(path) for path in (tmp_path, root, child)}

    try:
        reports = apply_windows_read_only(targets, runtime_sid=sid, lock_owner=False,
                                         snapshot_path=backup, canary=True)
        assert reports[0]["assurance"] == "canary"
        assert child.read_text(encoding="utf-8") == "before\n"
        with pytest.raises(PermissionError):
            child.write_text("changed\n", encoding="utf-8")
        with pytest.raises(PermissionError):
            (root / "new.py").write_text("new\n", encoding="utf-8")
    finally:
        restore_windows_access(targets, runtime_sid=sid, snapshot_path=backup)

    assert {path: live_runtime_acl._windows_get_sddl(path) for path in before} == before
    child.write_text("restored\n", encoding="utf-8")
    assert child.read_text(encoding="utf-8") == "restored\n"


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL recovery contract")
def test_windows_acl_failed_apply_restores_exact_acl(tmp_path, monkeypatch):
    root = tmp_path / "failed-runtime"
    root.mkdir()
    child = root / "package.py"
    child.write_text("before", encoding="utf-8")
    sid = windows_current_sid()
    targets = (NativeProtectionTarget(root, recursive=True, kind="runtime"),)
    before = {path: live_runtime_acl._windows_get_sddl(path) for path in (tmp_path, root, child)}
    original = live_runtime_acl._run_windows_acl
    calls = []
    def fail_during_apply(command):
        calls.append(command)
        if len(calls) == 3:
            raise OSError("injected ACL update failure")
        return original(command)
    monkeypatch.setattr(live_runtime_acl, "_run_windows_acl", fail_during_apply)
    with pytest.raises(OSError, match="injected"):
        apply_windows_read_only(targets, runtime_sid=sid, lock_owner=False,
            snapshot_path=tmp_path / "failure-recovery.json", canary=True)
    assert {path: live_runtime_acl._windows_get_sddl(path) for path in before} == before
    child.write_text("after rollback", encoding="utf-8")


@pytest.mark.skipif(os.name != "nt", reason="Windows strong DACL contract")
def test_windows_strong_acl_denies_delete_for_distinct_runtime_identity(tmp_path, monkeypatch):
    anchor = tmp_path / "installer-anchor"
    root = anchor / "runtime"
    root.mkdir(parents=True)
    child = root / "package.py"
    child.write_text("protected", encoding="utf-8")
    runtime_sid = "S-1-5-21-111111111-222222222-333333333-4444"
    maintainer_sid = windows_current_sid()
    commands = []

    monkeypatch.setattr(
        live_runtime_acl,
        "windows_process_identity",
        lambda process_id=None: {"sid": runtime_sid, "pid": process_id},
    )
    monkeypatch.setattr(
        live_runtime_acl, "_windows_runtime_token", lambda process_id: nullcontext()
    )
    monkeypatch.setattr(live_runtime_acl, "_windows_can_open", lambda *args: False)
    monkeypatch.setattr(live_runtime_acl, "_run_windows_acl", lambda argv: commands.append(argv))

    reports = apply_windows_read_only(
        (NativeProtectionTarget(root, recursive=True, kind="runtime"),),
        runtime_sid=runtime_sid,
        maintenance_sid=maintainer_sid,
        lock_owner=True,
        runtime_pid=4242,
        protection_anchor=anchor,
        snapshot_path=tmp_path / "recovery" / "acl.json",
        canary=False,
    )

    assert reports[0]["assurance"] == "maintenance_owned"
    deny_commands = [command for command in commands if "/deny" in command]
    child_deny = next(command[-1] for command in deny_commands if command[1] == str(child))
    root_deny = next(command[-1] for command in deny_commands if command[1] == str(root))
    anchor_deny = next(command[-1] for command in deny_commands if command[1] == str(anchor))
    assert f"*{runtime_sid}:(D," in child_deny
    assert f"*{runtime_sid}:(D," in root_deny
    assert f"*{runtime_sid}:(D,DC," in anchor_deny


@pytest.mark.platform
@pytest.mark.skipif(os.name != "nt", reason="Windows independent identity contract")
def test_windows_strong_acl_blocks_real_identity_delete_rename_and_replace():
    if os.environ.get("HASHI_RUN_WINDOWS_IDENTITY_PROBE") != "1":
        pytest.skip("set HASHI_RUN_WINDOWS_IDENTITY_PROBE=1 for disposable account probe")
    if not _windows_token_is_elevated():
        pytest.skip("disposable account probe requires an elevated maintenance identity")

    from scripts.permissions.windows_test_identity import (
        DisposableTestIdentity,
        WindowsAccounts,
    )

    program_data = Path(os.environ["ProgramData"]).resolve()
    base = program_data / ("HashiAclProbe_" + uuid4().hex)
    anchor = base / "installer-anchor"
    root = anchor / "runtime"
    exchange = base / "exchange"
    recovery = base / "recovery"
    for path in (root, exchange, recovery):
        path.mkdir(parents=True, exist_ok=True)
    child = root / "package.py"
    child.write_text("protected", encoding="utf-8")
    worker = exchange / "acl-worker.ps1"
    ready, trigger, result = (
        exchange / "ready.json",
        exchange / "sealed.trigger",
        exchange / "result.json",
    )
    worker.write_text(
        r'''param(
  [Parameter(Mandatory=$true)][string]$Root,
  [Parameter(Mandatory=$true)][string]$Child,
  [Parameter(Mandatory=$true)][string]$Ready,
  [Parameter(Mandatory=$true)][string]$Trigger,
  [Parameter(Mandatory=$true)][string]$Result,
  [Parameter(Mandatory=$true)][string]$Exchange
)
$ErrorActionPreference = "Stop"
$baseline = Join-Path $Root "baseline.txt"
$baselineRenamed = Join-Path $Root "baseline-renamed.txt"
Set-Content -LiteralPath $baseline -Value "baseline" -NoNewline
Rename-Item -LiteralPath $baseline -NewName "baseline-renamed.txt"
Remove-Item -LiteralPath $baselineRenamed -Force
@{ pid = $PID; baseline_mutation = $true } | ConvertTo-Json -Compress | Set-Content -LiteralPath $Ready
$deadline = [DateTime]::UtcNow.AddSeconds(30)
while (-not (Test-Path -LiteralPath $Trigger)) {
  if ([DateTime]::UtcNow -ge $deadline) { throw "sealed trigger timeout" }
  Start-Sleep -Milliseconds 50
}
try {
$env:TEMP = $Exchange
$env:TMP = $Exchange
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class HashiAclNativeProbe {
  [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true, EntryPoint="DeleteFileW")]
  public static extern bool DeleteFile(string path);
  [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true, EntryPoint="MoveFileExW")]
  public static extern bool MoveFileEx(string source, string destination, int flags);
}
"@
function Convert-NativeResult([bool]$Succeeded, [int]$Code) {
  return @{ denied = ((-not $Succeeded) -and ($Code -eq 5)); code = $Code }
}
$replacement = Join-Path $Exchange "replacement.py"
Set-Content -LiteralPath $replacement -Value "replacement" -NoNewline
$renamed = Join-Path $Root "renamed.py"
$anchor = Split-Path -Parent $Root
$rootRenamed = Join-Path $anchor "runtime-renamed"
$deleteOk = [HashiAclNativeProbe]::DeleteFile($Child)
$deleteCode = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
$renameOk = [HashiAclNativeProbe]::MoveFileEx($Child, $renamed, 0)
$renameCode = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
$replaceOk = [HashiAclNativeProbe]::MoveFileEx($replacement, $Child, 1)
$replaceCode = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
$renameRootOk = [HashiAclNativeProbe]::MoveFileEx($Root, $rootRenamed, 0)
$renameRootCode = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
$operations = @{
  delete = Convert-NativeResult $deleteOk $deleteCode
  rename = Convert-NativeResult $renameOk $renameCode
  replace = Convert-NativeResult $replaceOk $replaceCode
  rename_root = Convert-NativeResult $renameRootOk $renameRootCode
}
@{
  pid = $PID
  operations = $operations
  child_exists = (Test-Path -LiteralPath $Child)
  renamed_exists = (Test-Path -LiteralPath $renamed)
  root_renamed_exists = (Test-Path -LiteralPath $rootRenamed)
} | ConvertTo-Json -Depth 5 -Compress | Set-Content -LiteralPath $Result
} catch {
  @{
    pid = $PID
    fatal = $_.Exception.ToString()
  } | ConvertTo-Json -Depth 5 -Compress | Set-Content -LiteralPath $Result
  exit 1
}
''',
        encoding="utf-8",
    )

    accounts = WindowsAccounts()
    identity = None
    cleanup_safe = False
    try:
        identity = DisposableTestIdentity(accounts, base / "identity.json")
        with identity:
            runtime_sid = identity.sid
            assert runtime_sid and identity.password
            maintainer_sid = windows_current_sid()
            powershell = Path(os.environ["SystemRoot"]) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
            live_runtime_acl._run_windows_acl(
                ["icacls.exe", str(base), "/grant:r", f"*{runtime_sid}:(RX)"]
            )
            live_runtime_acl._run_windows_acl(
                ["icacls.exe", str(exchange), "/grant:r", f"*{runtime_sid}:(OI)(CI)(F)"]
            )
            live_runtime_acl._run_windows_acl(
                ["icacls.exe", str(root), "/grant:r", f"*{runtime_sid}:(OI)(CI)(M)"]
            )
            # Strong sealing requires its installer anchor, anchor parent and
            # recovery directory to be outside the runtime identity's control.
            live_runtime_acl._run_windows_acl(
                ["icacls.exe", str(base), "/deny", f"*{runtime_sid}:(DC)"]
            )
            live_runtime_acl._run_windows_acl(
                ["icacls.exe", str(anchor), "/deny", f"*{runtime_sid}:(D,WD,AD,WEA,WA,WDAC,WO)"]
            )
            live_runtime_acl._run_windows_acl(
                ["icacls.exe", str(recovery), "/deny", f"*{runtime_sid}:(D,DC,WD,AD,WEA,WA,WDAC,WO)"]
            )

            argv = [
                str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-File", str(worker),
                "-Root", str(root), "-Child", str(child), "-Ready", str(ready),
                "-Trigger", str(trigger), "-Result", str(result),
                "-Exchange", str(exchange),
            ]
            process = _launch_as_windows_identity(
                identity.name, identity.password, argv, cwd=exchange,
            )
            sealed = False
            try:
                _wait_for_path(ready, process)
                baseline = json.loads(ready.read_text(encoding="utf-8-sig"))
                assert baseline == {"baseline_mutation": True, "pid": process.process_id}
                targets = (NativeProtectionTarget(root, recursive=True, kind="runtime"),)
                snapshot = recovery / "acl.json"
                apply_windows_read_only(
                    targets,
                    runtime_sid=runtime_sid,
                    maintenance_sid=maintainer_sid,
                    lock_owner=True,
                    runtime_pid=process.process_id,
                    protection_anchor=anchor,
                    snapshot_path=snapshot,
                    canary=False,
                )
                sealed = True
                trigger.write_text("run", encoding="utf-8")
                _wait_for_path(result, process)
                receipt = json.loads(result.read_text(encoding="utf-8-sig"))
                assert receipt["pid"] == process.process_id
                assert "fatal" not in receipt, receipt.get("fatal")
                assert all(item["denied"] for item in receipt["operations"].values()), json.dumps(
                    receipt, sort_keys=True,
                )
                assert receipt["child_exists"] is True
                assert receipt["renamed_exists"] is False
                assert receipt["root_renamed_exists"] is False
                assert child.read_text(encoding="utf-8") == "protected"
            finally:
                _close_test_process(process)
                if sealed:
                    restore_windows_access(
                        targets, runtime_sid=runtime_sid, snapshot_path=snapshot,
                    )
        cleanup_safe = accounts.info(identity.name) is None
        assert cleanup_safe, "disposable runtime identity cleanup was not confirmed"
    finally:
        if identity is not None and accounts.info(identity.name) is None:
            cleanup_safe = True
        if cleanup_safe:
            shutil.rmtree(base)


@pytest.mark.skipif(os.name != "nt", reason="Windows policy rollback contract")
@pytest.mark.parametrize("existing", [False, True])
def test_windows_deployment_acl_failure_rolls_back_policy(tmp_path, monkeypatch, existing):
    import scripts.protect_live_runtime as deployment
    from orchestrator.config_json import new_config_json, write_config_json

    code_root = tmp_path / "source"
    runtime_root = tmp_path / "runtime"
    bridge_home = tmp_path / "instance"
    code_root.mkdir()
    runtime_root.mkdir()
    policy_path = bridge_home / "state" / "platform" / "live-runtime-protection.json"
    before = None
    if existing:
        policy_path.parent.mkdir(parents=True)
        prior = new_config_json(policy_path, {"schema": 1, "service_targets": ["before"]})
        write_config_json(policy_path, prior)
        before = policy_path.read_bytes()

    monkeypatch.setattr(deployment, "_targets", lambda *args, **kwargs: ((), None))

    def fail_acl(*args, **kwargs):
        raise OSError("injected ACL failure after policy publication")

    monkeypatch.setattr(deployment, "apply_windows_read_only", fail_acl)
    with pytest.raises(OSError, match="injected ACL failure"):
        deployment.main([
            "apply",
            "--instance-id", "TEST",
            "--code-root", str(code_root),
            "--bridge-home", str(bridge_home),
            "--runtime-root", str(runtime_root),
            "--canary",
            "--acl-backup", str(tmp_path / "acl.json"),
            "--confirm", "PROTECT LIVE RUNTIME TEST",
        ])

    if before is None:
        assert not policy_path.exists()
    else:
        assert policy_path.read_bytes() == before


@pytest.mark.platform
@pytest.mark.skipif(os.name != "nt", reason="Windows deployment script contract")
def test_windows_deployment_script_plans_applies_verifies_and_restores(tmp_path):
    code_root = tmp_path / "source"
    runtime_root = code_root / ".venv"
    bridge_home = tmp_path / "instance"
    for relative in CORE_SOURCE_PATHS:
        path = code_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# protected\n", encoding="utf-8")
    runtime_root.mkdir(parents=True)
    (runtime_root / "pyvenv.cfg").write_text("home = test\n", encoding="utf-8")
    script = Path(__file__).resolve().parents[1] / "scripts" / "protect_live_runtime.py"
    common = [
        "--instance-id",
        "TEST",
        "--code-root",
        str(code_root),
        "--bridge-home",
        str(bridge_home),
        "--runtime-root",
        str(runtime_root),
        "--canary", "--acl-backup", str(tmp_path / "deployment-acl.json"),
    ]

    plan = subprocess.run(
        [sys.executable, "-B", str(script), "plan", *common],
        capture_output=True,
        text=True,
    )
    assert plan.returncode == 0, plan.stderr
    assert not bridge_home.exists()

    try:
        applied = subprocess.run(
            [
                sys.executable,
                "-B",
                str(script),
                "apply",
                *common,
                "--confirm",
                "PROTECT LIVE RUNTIME TEST",
            ],
            capture_output=True,
            text=True,
        )
        assert applied.returncode == 0, applied.stderr
        verified = subprocess.run(
            [sys.executable, "-B", str(script), "verify", *common],
            capture_output=True,
            text=True,
        )
        assert verified.returncode == 0, verified.stdout + verified.stderr
    finally:
        restored = subprocess.run(
            [
                sys.executable,
                "-B",
                str(script),
                "restore",
                *common,
                "--confirm",
                "RESTORE LIVE RUNTIME TEST",
            ],
            capture_output=True,
            text=True,
        )
        assert restored.returncode == 0, restored.stderr

    (code_root / "main.py").write_text("# restored\n", encoding="utf-8")


@pytest.mark.platform
@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership contract")
def test_posix_acl_rejects_real_writes_and_restores_disposable_target(tmp_path):
    if os.geteuid() != 0:
        pytest.skip("POSIX ACL contract requires a disposable root-run test")
    runtime_user = os.environ.get("HASHI_TEST_RUNTIME_USER", "").strip()
    runtime_group = os.environ.get("HASHI_TEST_RUNTIME_GROUP", "").strip()
    if not runtime_user or not runtime_group:
        pytest.skip("set HASHI_TEST_RUNTIME_USER and HASHI_TEST_RUNTIME_GROUP")
    tmp_path.parent.chmod(0o755)
    tmp_path.chmod(0o755)
    root = tmp_path / "live-runtime"
    root.mkdir()
    child = root / "package.py"
    child.write_text("before\n", encoding="utf-8")
    targets = (NativeProtectionTarget(root, recursive=True, kind="runtime"),)

    try:
        apply_posix_read_only(
            targets,
            runtime_user=runtime_user,
            runtime_group=runtime_group,
            immutable=True,
        )
        readable = subprocess.run(
            ["runuser", "-u", runtime_user, "--", "cat", str(child)],
            capture_output=True,
            text=True,
        )
        assert readable.returncode == 0
        assert readable.stdout == "before\n"
        denied = subprocess.run(
            [
                "runuser",
                "-u",
                runtime_user,
                "--",
                "sh",
                "-c",
                'printf changed > "$1"',
                "hashi-acl-test",
                str(child),
            ],
            capture_output=True,
            text=True,
        )
        assert denied.returncode != 0
        denied_create = subprocess.run(
            [
                "runuser",
                "-u",
                runtime_user,
                "--",
                "touch",
                str(root / "new.py"),
            ],
            capture_output=True,
            text=True,
        )
        assert denied_create.returncode != 0
    finally:
        restore_posix_access(
            targets,
            runtime_user=runtime_user,
            runtime_group=runtime_group,
            immutable=True,
        )

    restored = subprocess.run(
        [
            "runuser",
            "-u",
            runtime_user,
            "--",
            "sh",
            "-c",
            'printf restored > "$1"',
            "hashi-acl-test",
            str(child),
        ],
        capture_output=True,
        text=True,
    )
    assert restored.returncode == 0
    assert child.read_text(encoding="utf-8") == "restored"
# HASHI-Fingerprint: hfp_d06483aebc6d479a886ac6947daf2b5e
