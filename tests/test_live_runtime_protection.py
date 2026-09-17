from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from orchestrator.live_runtime_protection import load_live_runtime_policy
from tools.registry import ToolRegistry


def _registry(tmp_path: Path) -> tuple[ToolRegistry, dict[str, Path]]:
    bridge_home = tmp_path / "instance"
    code_root = bridge_home / "source"
    runtime_root = bridge_home / ".venv"
    workspace = tmp_path / "workzones" / "agent"
    secrets_path = bridge_home / "secrets.json"
    for path in (code_root / "orchestrator", runtime_root, workspace):
        path.mkdir(parents=True, exist_ok=True)
    secrets_path.write_text("{}", encoding="utf-8")
    (bridge_home / "state" / "instance").mkdir(parents=True)
    (bridge_home / "state" / "instance" / "process.pid").write_text(
        "4242\n", encoding="utf-8"
    )
    global_config = SimpleNamespace(
        project_root=code_root,
        bridge_home=bridge_home,
        secrets_path=secrets_path,
        instance_id="HASHI3",
    )
    registry = ToolRegistry(
        allowed_tools=[
            "file_read",
            "file_write",
            "apply_patch",
            "shell",
            "background_job_start",
            "verification_run",
            "process_kill",
        ],
        access_root=tmp_path,
        access_roots=[tmp_path],
        workspace_dir=workspace,
        secrets={},
        audit_context={
            "global_config": global_config,
            "live_runtime_prefix": str(runtime_root),
        },
    )
    return registry, {
        "bridge_home": bridge_home,
        "code_root": code_root,
        "runtime_root": runtime_root,
        "workspace": workspace,
        "secrets_path": secrets_path,
    }


def _reason(result) -> str:
    assert result is not None
    assert result.is_error is True
    return str((result.details or {}).get("reason") or "")


def test_live_runtime_gate_denies_core_and_secret_but_allows_workzone(tmp_path):
    registry, paths = _registry(tmp_path)

    denied = registry.evaluate_admission(
        "file_write",
        {"path": str(paths["code_root"] / "main.py"), "content": "changed"},
    )
    assert _reason(denied) == "live_runtime_protection"

    denied = registry.evaluate_admission(
        "file_read", {"path": str(paths["secrets_path"])}
    )
    assert _reason(denied) == "live_runtime_protection"

    denied = registry.evaluate_admission(
        "file_write",
        {
            "path": str(paths["runtime_root"] / "Lib" / "site-packages" / "x.py"),
            "content": "changed",
        },
    )
    assert _reason(denied) == "live_runtime_protection"

    allowed = registry.evaluate_admission(
        "file_write",
        {"path": str(paths["workspace"] / "notes.md"), "content": "ok"},
    )
    assert allowed is None


def test_live_runtime_gate_allows_read_only_core_inspection_but_denies_shell_write(
    tmp_path,
):
    registry, paths = _registry(tmp_path)
    core_path = paths["code_root"] / "main.py"

    assert (
        registry.evaluate_admission(
            "shell", {"command": f'git diff -- "{core_path}"'}
        )
        is None
    )
    denied = registry.evaluate_admission(
        "shell", {"command": f'Set-Content -LiteralPath "{core_path}" -Value changed'}
    )
    assert _reason(denied) == "live_runtime_protection"


def test_live_runtime_gate_denies_live_install_but_allows_explicit_dev_environment(
    tmp_path,
):
    registry, paths = _registry(tmp_path)
    live_python = paths["runtime_root"] / "Scripts" / "python.exe"
    dev_python = paths["workspace"] / ".venv-dev" / "Scripts" / "python.exe"

    for command in (
        "python -m pip install example",
        "pip install example",
        f'"{live_python}" -m pip install example',
    ):
        denied = registry.evaluate_admission("shell", {"command": command})
        assert _reason(denied) == "live_runtime_protection"

    assert (
        registry.evaluate_admission(
            "shell", {"command": f'"{dev_python}" -m pip install example'}
        )
        is None
    )
    denied = registry.evaluate_admission(
        "verification_run",
        {"argv": [str(live_python), "-m", "pip", "install", "example"]},
    )
    assert _reason(denied) == "live_runtime_protection"


def test_live_runtime_gate_blocks_raw_current_instance_control_only(tmp_path):
    registry, _paths = _registry(tmp_path)

    denied = registry.evaluate_admission(
        "shell", {"command": "Restart-Service -Name HASHI3"}
    )
    assert _reason(denied) == "live_runtime_protection"
    assert (
        registry.evaluate_admission(
            "shell", {"command": "Get-Service -Name HASHI3"}
        )
        is None
    )
    assert (
        registry.evaluate_admission(
            "shell", {"command": "Restart-Service -Name PostgreSQL"}
        )
        is None
    )

    denied = registry.evaluate_admission("process_kill", {"pid": 4242})
    assert _reason(denied) == "live_runtime_protection"
    assert registry.evaluate_admission("process_kill", {"pid": 4343}) is None


def test_live_runtime_policy_extends_exact_paths_without_widening_workzone(tmp_path):
    registry, paths = _registry(tmp_path)
    policy_path = (
        paths["bridge_home"]
        / "state"
        / "platform"
        / "live-runtime-protection.json"
    )
    service_config = paths["bridge_home"] / "platform" / "hashi3-service.xml"
    restart_key = paths["bridge_home"] / "private" / "restart.key"
    policy_path.parent.mkdir(parents=True)
    policy_path.write_text(
        json.dumps(
            {
                "schema": 1,
                "protected_write_paths": [str(service_config)],
                "protected_read_paths": [str(restart_key)],
                "service_targets": ["HASHI3-Lifeline"],
            }
        ),
        encoding="utf-8",
    )

    denied = registry.evaluate_admission(
        "file_write", {"path": str(service_config), "content": "changed"}
    )
    assert _reason(denied) == "live_runtime_protection"
    denied = registry.evaluate_admission(
        "file_read", {"path": str(restart_key)}
    )
    assert _reason(denied) == "live_runtime_protection"
    denied = registry.evaluate_admission(
        "shell", {"command": "sc.exe stop HASHI3-Lifeline"}
    )
    assert _reason(denied) == "live_runtime_protection"
    denied = registry.evaluate_admission(
        "file_write", {"path": str(policy_path), "content": "{}"}
    )
    assert _reason(denied) == "live_runtime_protection"
    assert (
        registry.evaluate_admission(
            "file_write",
            {"path": str(paths["workspace"] / "result.json"), "content": "{}"},
        )
        is None
    )


def test_malformed_optional_policy_does_not_disable_derived_core_protection(tmp_path):
    registry, paths = _registry(tmp_path)
    policy_path = (
        paths["bridge_home"]
        / "state"
        / "platform"
        / "live-runtime-protection.json"
    )
    policy_path.parent.mkdir(parents=True)
    policy_path.write_text(
        json.dumps({"schema": 1, "protected_write_paths": ["relative/path"]}),
        encoding="utf-8",
    )

    policy = load_live_runtime_policy(
        registry.audit_context["global_config"],
        runtime_prefix=registry.audit_context["live_runtime_prefix"],
    )
    assert policy is not None
    assert policy.configuration_error
    denied = registry.evaluate_admission(
        "file_write",
        {"path": str(paths["code_root"] / "main.py"), "content": "changed"},
    )
    assert _reason(denied) == "live_runtime_protection"
    assert (
        registry.evaluate_admission(
            "file_write",
            {"path": str(paths["workspace"] / "safe.txt"), "content": "ok"},
        )
        is None
    )
