#!/usr/bin/env python3
"""Resolve a Python interpreter that can actually run the TUI.

The TUI is the rescue hatch: it must start even when the Core/Functions server
runtime is broken, incomplete, or mid-upgrade. It therefore only needs the
three TUI packages (``textual``, ``rich``, ``aiohttp``) - never the full server
dependency contract.

Candidates are tried in this order, and the first one that satisfies the TUI
requirement wins:

1. the installer-qualified Functions runtime
2. the Portable private ``<root>/python``
3. the developer ``<root>/.venv``
4. a Python 3 on ``PATH``

Everything here is standard library only, so this script runs under a broken
virtual environment as well.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

TUI_REQUIREMENT = "import textual, rich, aiohttp"
DEFAULT_PROBE_TIMEOUT = 25

# Human-readable labels, kept stable so launchers and tests can match on them.
LABEL_FUNCTIONS = "installer-functions-runtime"
LABEL_PORTABLE = "portable-python"
LABEL_VENV = "developer-venv"
LABEL_PATH = "path-python3"


def _functions_runtime(code_root: Path) -> str | None:
    """Return the installer-qualified Functions interpreter, if declared."""

    try:
        from scripts.hashi_instance_cli import _select_core_runtime
    except Exception:  # pragma: no cover - only on a broken install tree
        return None
    try:
        _core, functions = _select_core_runtime(code_root, with_functions=True)
    except Exception:
        return None
    candidate = str(functions[0]) if functions else ""
    return candidate or None


def _portable_python(code_root: Path) -> str | None:
    for relative in ("python/python.exe", "python/bin/python3"):
        candidate = code_root / relative
        if candidate.is_file():
            return str(candidate)
    return None


def _venv_python(code_root: Path) -> str | None:
    for relative in (".venv/Scripts/python.exe", ".venv/bin/python3"):
        candidate = code_root / relative
        if candidate.is_file():
            return str(candidate)
    return None


def _path_python() -> str | None:
    for name in ("python3", "python"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _candidates(code_root: Path) -> list[tuple[str, str]]:
    ordered: list[tuple[str, str]] = []
    for label, resolver in (
        (LABEL_FUNCTIONS, lambda: _functions_runtime(code_root)),
        (LABEL_PORTABLE, lambda: _portable_python(code_root)),
        (LABEL_VENV, lambda: _venv_python(code_root)),
        (LABEL_PATH, _path_python),
    ):
        try:
            resolved = resolver()
        except Exception:
            resolved = None
        if resolved:
            ordered.append((label, resolved))
    # De-duplicate while preserving order.
    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for label, executable in ordered:
        key = str(Path(executable))
        if key in seen:
            continue
        seen.add(key)
        unique.append((label, executable))
    return unique


def tui_requirement_met(executable: str, timeout: int = DEFAULT_PROBE_TIMEOUT) -> bool:
    """True when *executable* can import the three TUI packages."""

    try:
        completed = subprocess.run(
            [executable, "-B", "-c", TUI_REQUIREMENT],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def resolve(code_root: Path = ROOT, *, probe=None) -> tuple[str | None, str]:
    """Return ``(executable, label)`` for the first working TUI interpreter."""

    check = probe if probe is not None else tui_requirement_met
    candidates = _candidates(code_root)
    for label, executable in candidates:
        if check(executable):
            return executable, label
    return None, ""


def _explain(code_root: Path) -> str:
    lines = [
        "No Python interpreter with the TUI packages was found.",
        "The TUI needs only: textual, rich, aiohttp.",
        "This is a TUI startup problem, not a Core/Functions failure.",
        "Tried:",
    ]
    for label, executable in _candidates(code_root):
        lines.append(f"  - {label}: {executable}")
    lines.append(
        "Install the TUI packages into one of those interpreters, "
        "or start the stdlib rescue shell with: python tui.py --rescue"
    )
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code-root", type=Path, default=ROOT)
    parser.add_argument(
        "--report",
        action="store_true",
        help="print every candidate and its probe result as JSON",
    )
    args = parser.parse_args(argv)
    root = args.code_root.resolve()

    if args.report:
        payload = [
            {"label": label, "executable": executable, "usable": tui_requirement_met(executable)}
            for label, executable in _candidates(root)
        ]
        print(json.dumps(payload, indent=2))
        return 0

    executable, label = resolve(root)
    if not executable:
        print(_explain(root), file=sys.stderr)
        return 78
    print(label, file=sys.stderr)
    print(executable)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
# HASHI-Fingerprint: hfp_7dbed70462444aa5bfda39a2d396ff92
