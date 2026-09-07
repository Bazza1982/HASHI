#!/usr/bin/env python3
"""Fail when protected HASHI core files are changed without explicit approval."""

from __future__ import annotations

import argparse
import ast
import os
import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = "orchestrator/runtime_contract.py"


def _parse_manifest(source: str) -> tuple[str, ...]:
    # Read data, never execute code from the branch being checked.
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "CORE_SOURCE_PATHS"
            for target in node.targets
        ):
            paths = ast.literal_eval(node.value)
            if isinstance(paths, (tuple, list)) and all(isinstance(p, str) for p in paths):
                return tuple(paths)
    raise ValueError("CORE_SOURCE_PATHS must be a literal sequence of paths")


PROTECTED_CORE_PATHS = _parse_manifest(
    (REPOSITORY_ROOT / MANIFEST_PATH).read_text(encoding="utf-8")
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
    cmd = ["git", "diff", "--name-only", "--no-renames"]
    if args.cached:
        cmd.append("--cached")
    if args.base:
        cmd.extend([args.base, "--"])
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    changed = {
        line.strip().replace("\\", "/")
        for line in result.stdout.splitlines()
        if line.strip()
    }
    if not args.cached and not args.base:
        staged = subprocess.run(
            ["git", "diff", "--name-only", "--no-renames", "--cached"],
            check=True, capture_output=True, text=True,
        )
        changed.update(line.strip() for line in staged.stdout.splitlines() if line.strip())
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            check=True,
            capture_output=True,
            text=True,
        )
        changed.update(
            line.strip().replace("\\", "/")
            for line in untracked.stdout.splitlines()
            if line.strip()
        )
    return changed


def _protected_paths(root: Path, args: argparse.Namespace) -> set[str]:
    current = root / MANIFEST_PATH
    protected = set(
        _parse_manifest(current.read_text(encoding="utf-8"))
        if current.is_file() else PROTECTED_CORE_PATHS
    )
    # A candidate cannot erase protection by editing its own manifest. Keep
    # HEAD, the branch baseline and (for commits) the exact index view too.
    refs = ["HEAD"]
    if args.base:
        refs.append(args.base)
    if args.cached:
        refs.append("")
    for ref in dict.fromkeys(refs):
        result = subprocess.run(
            ["git", "show", f"{ref}:{MANIFEST_PATH}"],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            protected.update(_parse_manifest(result.stdout))
    return protected


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
    try:
        protected = sorted(changed & _protected_paths(root, args))
    except (ValueError, SyntaxError, OSError) as exc:
        print(f"protected core manifest: invalid: {exc}", file=sys.stderr)
        return 3

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
        "\nIf the current task already explicitly authorizes these Core edits, rerun "
        "with `--authorized` or a command-scoped HASHI_CORE_EDIT_AUTHORIZED=1. "
        "Otherwise obtain authorization before changing Core. Do not request it twice.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
