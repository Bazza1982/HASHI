#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
import argparse
from pathlib import Path


PROJECT_ROOT = Path("/home/lily/projects/hashi")
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "consolidate_memory.py"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run local HASHI memory consolidation and embedding refresh."
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=["run"],
        help="Action to perform. Defaults to run.",
    )
    parser.parse_args(argv)

    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--embed"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
    )
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()

    if proc.returncode != 0:
        print("Memory consolidation failed.")
        if stdout:
            print(stdout)
        if stderr:
            print("\nstderr:\n" + stderr)
        return proc.returncode

    print("Memory consolidation completed successfully.")
    print("Execution mode: local-only action skill (no OpenRouter, no HASHI API relay)")
    if stdout:
        print()
        print(stdout)
    if stderr:
        print("\nstderr:\n" + stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
