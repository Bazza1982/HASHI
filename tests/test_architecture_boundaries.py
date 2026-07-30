from __future__ import annotations

from pathlib import Path

from orchestrator import runtime_defaults
from remote import port_selection


ROOT = Path(__file__).resolve().parent.parent
ACTIVE_RUNTIME_MAX_LINES = 8059
PRIVATE_PATH_MARKERS = (
    "/home/lily",
    "/mnt/c/Users/thene",
    "C:/Users/thene",
)


def _runtime_python_sources() -> list[Path]:
    sources = [
        path
        for package in ("orchestrator", "remote", "browser_gateway")
        for path in (ROOT / package).rglob("*.py")
        if "legacy" not in path.parts
    ]
    sources.extend(
        [
            ROOT / "tools" / "hchat_send.py",
            ROOT / "tools" / "news_deliver_to_sunny.py",
            ROOT / "tools" / "onedrive_memo_gen.py",
        ]
    )
    return sources


def test_clean_ci_dependencies_cover_runtime_imports_and_async_tests():
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    project_config = (ROOT / "pyproject.toml").read_text(encoding="utf-8").lower()

    for dependency in ("pyyaml", "pytest-asyncio"):
        assert dependency in requirements
    assert "pyyaml" in project_config
    assert "pytest-asyncio" in project_config


def test_active_runtime_size_ratchet():
    runtime_path = ROOT / "orchestrator" / "flexible_agent_runtime.py"

    assert len(runtime_path.read_text(encoding="utf-8").splitlines()) <= ACTIVE_RUNTIME_MAX_LINES


def test_runtime_and_adapters_do_not_embed_private_machine_paths():
    violations = []
    for path in _runtime_python_sources():
        text = path.read_text(encoding="utf-8")
        for marker in PRIVATE_PATH_MARKERS:
            if marker in text:
                violations.append(f"{path.relative_to(ROOT)}: {marker}")

    assert violations == []


def test_launch_and_control_surfaces_do_not_use_repository_global_process_files():
    surfaces = [
        ROOT / "main.py",
        ROOT / "__main__.py",
        *sorted((ROOT / "bin").glob("*.sh")),
        *sorted((ROOT / "bin").glob("*.bat")),
        *sorted((ROOT / "bin").glob("*.ps1")),
        *sorted((ROOT / "mac").glob("*.command")),
        *sorted((ROOT / "windows").glob("*.bat")),
        ROOT / "onboarding" / "onboarding_main.py",
    ]
    forbidden = (".bridge_u_f.pid", ".bridge_u_f.lock")
    violations = []
    for path in surfaces:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for marker in forbidden:
            if marker in text:
                violations.append(f"{path.relative_to(ROOT)}: {marker}")

    assert violations == []


def test_process_controls_require_exact_instance_home_ownership():
    required_markers = {
        ROOT / "bin" / "bridge-u.sh": (
            "resolve_instance_runtime.py",
            "cmdline_matches_bridge_home",
            "--bridge-home",
        ),
        ROOT / "bin" / "kill-sessions.sh": (
            "resolve_instance_runtime.py",
            "cmdline_matches_bridge_home",
            "--bridge-home",
        ),
        ROOT / "bin" / "bridge-u.bat": (
            "resolve_instance_runtime.py",
            "--bridge-home",
        ),
        ROOT / "bin" / "bridge_ctl.ps1": (
            "resolve_instance_runtime.py",
            "Test-BridgeCommandLine",
            "--bridge-home",
        ),
    }
    violations = []
    for path, markers in required_markers.items():
        text = path.read_text(encoding="utf-8", errors="replace")
        for marker in markers:
            if marker not in text:
                violations.append(f"{path.relative_to(ROOT)}: missing {marker}")

    onboarding = (ROOT / "onboarding" / "onboarding_main.py").read_text(encoding="utf-8")
    for marker in ("taskkill", "pkill", "process_iter", "kill -9"):
        if marker in onboarding:
            violations.append(f"onboarding/onboarding_main.py: owns process control via {marker}")

    windows_wrapper = (ROOT / "bin" / "kill_bridge_u_f_sessions.bat").read_text(
        encoding="utf-8",
        errors="replace",
    ).lower()
    for marker in ("taskkill", "wmic process", "get-nettcpconnection"):
        if marker in windows_wrapper:
            violations.append(f"bin/kill_bridge_u_f_sessions.bat: bypasses instance controller via {marker}")

    assert violations == []


def test_remote_port_compatibility_facade_derives_from_runtime_defaults():
    assert port_selection.DEFAULT_PORT == runtime_defaults.DEFAULT_HASHI_REMOTE_PORT
    assert (
        runtime_defaults.DEFAULT_WORKBENCH_URL
        == f"http://127.0.0.1:{runtime_defaults.DEFAULT_WORKBENCH_PORT}"
    )
