#!/usr/bin/env python3
"""
Best-effort pip-audit wrapper for security scans.

Purpose:
  - Keep daily patrol output deterministic.
  - Distinguish "no vulnerabilities found" from "audit timed out/failed".
  - Prefer requirement-file audit when available, fall back to environment audit.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def _run(cmd: list[str], timeout_s: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        cwd=str(Path(__file__).resolve().parents[1]),
    )


def _pick_requirements(project_root: Path) -> Path | None:
    for name in ("requirements.txt", "requirements-dev.txt"):
        candidate = project_root / name
        if candidate.exists():
            return candidate
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Best-effort pip-audit wrapper for HASHI patrols")
    parser.add_argument("--project-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--max-lines", type=int, default=20)
    args = parser.parse_args()

    project_root = Path(args.project_root).resolve()
    req = _pick_requirements(project_root)

    commands: list[list[str]] = []
    if req is not None:
        commands.append(
            [
                "pip-audit",
                "-r",
                str(req),
                "--desc",
                "--progress-spinner",
                "off",
            ]
        )
    commands.append(
        [
            "pip-audit",
            "--desc",
            "--progress-spinner",
            "off",
        ]
    )

    saw_timeout = False
    failures: list[str] = []

    for cmd in commands:
        try:
            result = _run(cmd, timeout_s=args.timeout)
        except subprocess.TimeoutExpired:
            saw_timeout = True
            failures.append(f"timed out: {' '.join(cmd)}")
            continue

        combined = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
        lines = combined.splitlines()

        if result.returncode in (0, 1):
            if lines:
                print("\n".join(lines[: args.max_lines]))
            else:
                print("pip-audit completed but produced no output")
            return result.returncode

        failures.append(f"exit {result.returncode}: {' '.join(cmd)}")

    if saw_timeout:
        print("pip-audit timed out before returning a usable result")
    else:
        print("pip-audit failed before returning a usable result")
    for item in failures[:3]:
        print(f"detail: {item}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
