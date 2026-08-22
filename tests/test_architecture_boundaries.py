from __future__ import annotations

from pathlib import Path

from orchestrator import runtime_defaults
from remote import port_selection


ROOT = Path(__file__).resolve().parent.parent
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


def test_legacy_fixed_runtime_cannot_reenter_active_architecture():
    retired_paths = [
        ROOT / "orchestrator" / "legacy" / "bridge_agent_runtime.py",
        ROOT / "orchestrator" / "agent_runtime.py",
    ]
    assert [path for path in retired_paths if path.exists()] == []

    forbidden_imports = (
        "orchestrator.legacy.bridge_agent_runtime",
        "BridgeAgentRuntime",
        "HASHI_ENABLE_LEGACY_FIXED_RUNTIME",
    )
    violations = []
    for package in ("orchestrator", "adapters", "onboarding", "tui"):
        for path in (ROOT / package).rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="replace")
            for marker in forbidden_imports:
                if marker in text:
                    violations.append(f"{path.relative_to(ROOT)}: {marker}")

    assert violations == []


def test_retired_her_v1_and_openclaw_surfaces_cannot_reenter_runtime():
    retired_paths = [
        ROOT / "adapters" / "her.py",
        ROOT / "adapters" / "her_ultra.py",
        ROOT / "adapters" / "claw_cli.py",
        ROOT / "orchestrator" / "runtime_turn_context.py",
        ROOT / "scripts" / "import_openclaw.py",
        ROOT / "scripts" / "her_runtime_probe.py",
        ROOT / "scripts" / "verify_her_certification.py",
        ROOT / "orchestrator" / "her_rebuild.py",
        ROOT / "orchestrator" / "her_rebuild_manager.py",
        ROOT / "scripts" / "her_rebuild_dev.py",
        ROOT / "native" / "her",
        ROOT / "hashi_assets" / "her",
        ROOT / "tools" / "her_debug",
        ROOT / "superloops" / "templates" / "her_debug",
    ]
    assert [path for path in retired_paths if path.exists()] == []

    forbidden_imports = (
        "adapters.her import",
        "adapters.her_ultra import",
        "adapters.claw_cli import",
        "orchestrator.runtime_turn_context import",
        "ClawCLIAdapter",
        "HERUltraAdapter",
        "discover_claw_binary",
        "build_claw_env",
    )
    violations = []
    for package in ("orchestrator", "adapters", "onboarding", "tui", "scripts", "tools"):
        for path in (ROOT / package).rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="replace")
            for marker in forbidden_imports:
                if marker in text:
                    violations.append(f"{path.relative_to(ROOT)}: {marker}")

    assert violations == []
