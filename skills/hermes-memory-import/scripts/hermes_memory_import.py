#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path("/home/lily/projects/hashi")
SCRIPT_PATH = PROJECT_ROOT / "workspaces" / "lily" / "scripts" / "hermes_memory_import.py"


def run_step(label: str, args: list[str]) -> int:
    cmd = [sys.executable, str(SCRIPT_PATH), "--root", str(PROJECT_ROOT), *args]
    print(f"[hermes-memory-automation] step={label}")
    print(f"[hermes-memory-automation] command={' '.join(cmd)}")
    proc = subprocess.run(
        cmd,
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    print(f"[hermes-memory-automation] step={label} returncode={proc.returncode}")
    if stdout:
        print(stdout)
    if stderr:
        print("\nstderr:\n" + stderr)
    return proc.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the existing Hermes memory import sidecar as a HASHI Jobs automation."
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=["run", "check"],
        help="Use 'check' for path validation only, or 'run' to check then import.",
    )
    args = parser.parse_args(argv)

    print("[hermes-memory-automation] mode=local-jobs-automation")
    print(f"[hermes-memory-automation] project_root={PROJECT_ROOT}")
    print(f"[hermes-memory-automation] script_path={SCRIPT_PATH}")
    print("[hermes-memory-automation] writes=consolidated_memory.sqlite,logs/hermes_memory_import.jsonl")
    print("[hermes-memory-automation] no_core_edits=True no_obsidian_vault_writes=True")

    if not SCRIPT_PATH.exists():
        print(f"[hermes-memory-automation] ERROR missing_script={SCRIPT_PATH}")
        return 2

    check_code = run_step("check", ["--check"])
    if check_code != 0:
        print("[hermes-memory-automation] success=False failed_step=check")
        return check_code

    if args.command == "check":
        print("[hermes-memory-automation] success=True check_only=True")
        return 0

    import_code = run_step("import", ["--import"])
    if import_code != 0:
        print("[hermes-memory-automation] success=False failed_step=import")
        return import_code

    print("[hermes-memory-automation] success=True")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
