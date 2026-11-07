"""Qualify the intended cold-restart destination through the installed Core selector."""

from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path


def qualified_restart_runtime(code_root: Path) -> dict | None:
    """Return the admitted Functions contract, or retain a legacy contract.

    Qualification runs in the installer-selected isolated Core, never in the
    Remote's Python environment. A failed v5 selection must not stop a live
    instance or fall back to its old coupled virtual environment.
    """
    metadata = code_root / "pyproject.toml"
    if not metadata.is_file():
        return None
    policy = tomllib.loads(metadata.read_text(encoding="utf-8"))
    roles = policy.get("tool", {}).get("hashi", {}).get("runtime", {})
    if "core-host-lock" not in roles:
        return None

    from scripts.hashi_instance_cli import _select_core_runtime

    command = _select_core_runtime(code_root)
    result = subprocess.run(
        [*command, str(code_root / "scripts/check_runtime_selection.py"),
         "--code-root", str(code_root)],
        cwd=code_root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=180,
        check=False,
    )
    if result.returncode:
        raise ValueError("Cold-restart target failed installer runtime qualification")
    selected = json.loads(result.stdout)
    functions = selected.get("functions")
    if not isinstance(functions, dict) or functions.get("role") != "functions":
        raise ValueError("Cold-restart target has no qualified Functions contract")
    return functions
# HASHI-Fingerprint: hfp_0043f76a7f3f47dc8a4689a986ff67af
