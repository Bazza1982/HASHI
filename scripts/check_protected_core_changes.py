#!/usr/bin/env python3
"""Fail when protected HASHI core files are changed without explicit approval."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


PROTECTED_CORE_PATHS = (
    "__main__.py",
    "main.py",
    "orchestrator/config.py",
    "orchestrator/instance_lock.py",
    "orchestrator/pathing.py",
    "orchestrator/manager_registry.py",
    "orchestrator/hot_reload.py",
    "orchestrator/reboot_manager.py",
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


def _missing_manifest_paths(root: Path) -> list[str]:
    return sorted(path for path in PROTECTED_CORE_PATHS if not (root / path).is_file())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cached", action="store_true", help="check staged changes")
    parser.add_argument("--base", help="optional git base/ref to diff against")
    parser.add_argument(
        "--authorized",
        action="store_true",
        help="acknowledge explicit user authorization for protected core edits",
    )
    parser.add_argument(
        "--validate-manifest",
        action="store_true",
        help="fail if a protected path does not exist",
    )
    args = parser.parse_args(argv)

    root = _repo_root()
    os.chdir(root)
    if args.validate_manifest:
        missing = _missing_manifest_paths(root)
        if missing:
            print("protected core manifest: invalid", file=sys.stderr)
            for path in missing:
                print(f"- missing: {path}", file=sys.stderr)
            return 3
        print("protected core manifest: ok")
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
