#!/usr/bin/env python3
"""Fail when protected HASHI core files are changed without explicit approval."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


PROTECTED_CORE_PATHS = (
    "main.py",
    "orchestrator/kernel.py",
    "orchestrator/reboot_manager.py",
    "orchestrator/instance_lock.py",
    "orchestrator/startup_manager.py",
    "orchestrator/shutdown_manager.py",
    "remote/protocol_manager.py",
    "remote/peer/base.py",
)


def _repo_root() -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(result.stdout.strip())


def _changed_files(args: argparse.Namespace) -> set[str]:
    cmd = ["git", "diff", "--name-only"]
    if args.cached:
        cmd.append("--cached")
    if args.base:
        cmd.extend([args.base, "--"])
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return {line.strip().replace("\\", "/") for line in result.stdout.splitlines() if line.strip()}


def _is_authorized(args: argparse.Namespace) -> bool:
    return args.authorized or os.environ.get("HASHI_CORE_EDIT_AUTHORIZED") == "1"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cached", action="store_true", help="check staged changes")
    parser.add_argument("--base", help="optional git base/ref to diff against")
    parser.add_argument(
        "--authorized",
        action="store_true",
        help="acknowledge explicit user authorization for protected core edits",
    )
    args = parser.parse_args(argv)

    root = _repo_root()
    os.chdir(root)
    changed = _changed_files(args)
    protected = sorted(path for path in changed if path in PROTECTED_CORE_PATHS)

    if not protected:
        print("protected core check: ok")
        return 0

    if _is_authorized(args):
        print("protected core check: authorized")
        for path in protected:
            print(f"- {path}")
        return 0

    print("protected core check: blocked", file=sys.stderr)
    print("Protected HASHI core files changed without explicit authorization:", file=sys.stderr)
    for path in protected:
        print(f"- {path}", file=sys.stderr)
    print(
        "\nAsk the user for explicit core-edit authorization, then rerun with "
        "`--authorized` or HASHI_CORE_EDIT_AUTHORIZED=1.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
