from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

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


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL contract")
def test_windows_acl_rejects_real_writes_and_restores_disposable_target(tmp_path):
    root = tmp_path / "live-runtime"
    root.mkdir()
    child = root / "package.py"
    child.write_text("before\n", encoding="utf-8")
    sid = windows_current_sid()
    targets = (NativeProtectionTarget(root, recursive=True, kind="runtime"),)

    try:
        apply_windows_read_only(targets, runtime_sid=sid, lock_owner=False)
        assert child.read_text(encoding="utf-8") == "before\n"
        with pytest.raises(PermissionError):
            child.write_text("changed\n", encoding="utf-8")
        with pytest.raises(PermissionError):
            (root / "new.py").write_text("new\n", encoding="utf-8")
    finally:
        restore_windows_access(targets, runtime_sid=sid)

    child.write_text("restored\n", encoding="utf-8")
    assert child.read_text(encoding="utf-8") == "restored\n"


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
    ]

    plan = subprocess.run(
        [sys.executable, str(script), "plan", *common],
        capture_output=True,
        text=True,
    )
    assert plan.returncode == 0, plan.stderr
    assert not bridge_home.exists()

    try:
        applied = subprocess.run(
            [
                sys.executable,
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
            [sys.executable, str(script), "verify", *common],
            capture_output=True,
            text=True,
        )
        assert verified.returncode == 0, verified.stdout + verified.stderr
    finally:
        restored = subprocess.run(
            [
                sys.executable,
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
