#!/usr/bin/env python3
"""Precompile a HASHI local acceleration cache with machine-readable progress."""

from __future__ import annotations

import argparse
import py_compile
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("roots", nargs="+", type=Path)
    args = parser.parse_args()

    sources = sorted(
        path
        for root in args.roots
        if root.is_dir()
        for path in root.rglob("*.py")
        if path.is_file()
    )
    total = len(sources)
    interval = max(1, total // 200)
    failures: list[tuple[Path, str]] = []
    print(f"HASHI_COMPILE_PROGRESS 0 {total}", flush=True)
    for index, source in enumerate(sources, 1):
        try:
            py_compile.compile(str(source), doraise=True, optimize=0)
        except (OSError, py_compile.PyCompileError) as exc:
            failures.append((source, str(exc)))
        if index == total or index % interval == 0:
            print(f"HASHI_COMPILE_PROGRESS {index} {total}", flush=True)

    if failures:
        for source, error in failures[:20]:
            print(f"bytecode compile failed: {source}: {error}", file=sys.stderr)
        if len(failures) > 20:
            print(
                f"... and {len(failures) - 20} additional compile failures",
                file=sys.stderr,
            )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
