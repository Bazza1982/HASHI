"""Qualify the intended cold-restart destination through the installed Core selector."""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path


def selected_restart_python(code_root: Path) -> str:
    """Resolve the cheap installer-selected Functions launcher.

    This deliberately reads only the active installer selector used by the
    normal launcher.  It does not qualify installed bytes; the later
    ``qualified_restart_runtime`` boundary remains mandatory before Core is
    stopped.  Keeping the original launcher path matters on Windows because a
    venv process reports the shared base interpreter through ``sys.executable``.
    """
    metadata = code_root / "pyproject.toml"
    if not metadata.is_file():
        return sys.executable
    policy = tomllib.loads(metadata.read_text(encoding="utf-8"))
    roles = policy.get("tool", {}).get("hashi", {}).get("runtime", {})
    if "core-host-lock" not in roles:
        return sys.executable

    from scripts.hashi_instance_cli import _prepared_role_candidates

    for command in _prepared_role_candidates(code_root):
        if len(command) != 1:
            continue
        candidate = Path(command[0])
        if candidate.is_absolute() and candidate.is_file():
            return str(candidate)
    raise ValueError("Cold-restart target has no installer-selected Functions launcher")


def restart_runtime_data_root() -> str:
    """Return the installation catalogue root used by the runtime selector."""
    from tools.instance_registry import default_data_root

    selected = default_data_root().resolve()
    if not selected.is_absolute():
        raise ValueError("Cold-restart runtime data root must be absolute")
    return str(selected)


def qualified_restart_runtime(
    code_root: Path,
    *,
    expected_functions_python: str | None = None,
) -> dict | None:
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

    command, functions_command = _select_core_runtime(code_root, with_functions=True)
    if expected_functions_python is not None:
        expected = Path(expected_functions_python).resolve()
        selected = Path(functions_command[0]).resolve()
        if selected != expected:
            raise ValueError(
                "Cold-restart Functions launcher changed after fixed-task precheck"
            )
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
# HASHI-Fingerprint: hfp_1a2351975c8b44248c31a4a2d54fe34c
