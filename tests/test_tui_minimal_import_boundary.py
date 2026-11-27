"""Iron rule: the TUI rescue hatch must never drag in server-side packages.

The TUI is the only interface a broken installation ships with. If importing it
pulls python-telegram-bot (and through it cryptography, httpx, click), a single
missing server package turns the rescue hatch into a stack trace. These tests
import ``tui.app`` in a clean subprocess and assert that no server dependency
appears in ``sys.modules``.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Packages the TUI must never require. Playwright/greenlet/pyee are the three
# that broke windows\start_tui.bat; the rest arrive through the server stack.
FORBIDDEN = (
    "playwright",
    "greenlet",
    "pyee",
    "telegram",
    "cryptography",
    "openai",
    "anthropic",
    "numpy",
    "uvicorn",
    "fastapi",
    "httpx",
    "click",
)

_PROBE = """
import json, sys
{statement}  # noqa: F401
leaked = [name for name in {forbidden} if name in sys.modules]
print(json.dumps(leaked))
"""


def _probe(statement: str) -> list[str]:
    code = _PROBE.format(statement=statement, forbidden=repr(tuple(FORBIDDEN)))
    completed = subprocess.run(
        [sys.executable, "-B", "-c", code],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert completed.returncode == 0, completed.stderr[-4000:]
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_tui_app_imports_without_server_packages():
    leaked = _probe("import tui.app")
    assert leaked == [], f"tui.app pulled in server-side packages: {leaked}"


def test_tui_entry_point_imports_without_starting_the_app():
    completed = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            "import importlib.util, sys;"
            "spec = importlib.util.spec_from_file_location('tui_entry', %r);"
            "mod = importlib.util.module_from_spec(spec);"
            "spec.loader.exec_module(mod);"
            "assert callable(mod.main);"
            "print('ok')" % str(ROOT / "tui.py"),
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert completed.returncode == 0, completed.stderr[-4000:]
    assert "ok" in completed.stdout


def test_rescue_cli_imports_only_the_standard_library():
    leaked = _probe("import tui.rescue_cli")
    assert leaked == [], f"tui.rescue_cli must stay dependency free: {leaked}"


def test_launchers_do_not_check_the_server_contract_themselves():
    """TUI launchers pick an interpreter and start; the server is /doctor's job.

    ``check_runtime_contract.py`` checks the Functions role by default, so
    running it here - with the TUI interpreter - would report a healthy
    Core/Functions pair as drift and blame the TUI for a server problem.
    """

    launchers = (
        ROOT / "windows" / "start_tui.bat",
        ROOT / "windows" / "TUI_onboarding.bat",
        ROOT / "mac" / "start_tui.command",
        ROOT / "mac" / "tui_onboarding.command",
    )
    for launcher in launchers:
        text = launcher.read_text(encoding="utf-8", errors="replace")
        assert "resolve_tui_python.py" in text, launcher
        assert "check_runtime_contract" not in text, launcher
        assert "HASHI_TUI_RUNTIME_STATUS" not in text, launcher
    windows_launcher = (ROOT / "windows" / "start_tui.bat").read_text(
        encoding="utf-8", errors="replace"
    )
    # The old fatal gate: run the server contract and bail out with 78.
    assert "violates the HASHI Core runtime contract" not in windows_launcher
    assert "exit /b 78" not in windows_launcher
    # The misleading wording that blamed the TUI for a Playwright mismatch.
    assert "Rebuild this installation" not in windows_launcher
# HASHI-Fingerprint: hfp_27478b3498f64cc191d80e8cbecfafdb
