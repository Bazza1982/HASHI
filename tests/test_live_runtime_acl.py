from __future__ import annotations

import ctypes
import os
from pathlib import Path
import subprocess
import sys

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
