#!/usr/bin/env python3
"""Prepare and check the isolated HERV3 Privacy Level 2 model runtime."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = ROOT / "requirements-privacy.txt"
SIDECAR = ROOT / "tools" / "privacy_filter_sidecar.py"
EXPECTED = {
    "presidio-analyzer": "2.2.364",
    "spacy": "3.8.16",
    "en-core-web-sm": "3.8.0",
}
CANARY = "Contact Jordan Lee at jordan@example.com. Add 50 and 50."


class PrivacyRuntimeError(RuntimeError):
    """The separate detector environment is unavailable or incomplete."""


def runtime_python(runtime_dir: Path) -> Path:
    return runtime_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def checked_target(value: Path) -> Path:
    target = value.expanduser().resolve()
    active = Path(sys.prefix).resolve()
    if target == active or target in active.parents or active in target.parents:
        raise PrivacyRuntimeError("privacy runtime must not overlap the active Python environment")
    return target


def run(
    command: list[str], *, timeout: int, action: str,
    input_text: str | None = None,
) -> str:
    try:
        result = subprocess.run(
            command, input=input_text, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PrivacyRuntimeError(f"privacy {action} could not complete") from exc
    if result.returncode:
        # Library errors can contain supplied text or local details. Do not echo them.
        raise PrivacyRuntimeError(f"privacy {action} failed")
    return result.stdout


def check(python: Path) -> dict[str, str]:
    if not python.is_file():
        raise PrivacyRuntimeError("privacy runtime Python is missing")
    version_code = (
        "import importlib.metadata as m,json; "
        "print(json.dumps({n:m.version(n) for n in "
        "('presidio-analyzer','spacy','en-core-web-sm')}))"
    )
    try:
        versions = json.loads(run(
            [str(python), "-I", "-c", version_code], timeout=30,
            action="package inventory",
        ))
    except (ValueError, KeyError) as exc:
        raise PrivacyRuntimeError("privacy runtime package inventory is invalid") from exc
    if versions != EXPECTED:
        raise PrivacyRuntimeError("privacy runtime package versions do not match the profile")
    try:
        payload = json.loads(run(
            [str(python), str(SIDECAR)], timeout=90, action="detector readiness check",
            input_text=json.dumps({"texts": [CANARY]}),
        ))
        matches = payload["matches"][0]
        found = {CANARY[item["start"]:item["end"]] for item in matches}
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise PrivacyRuntimeError("privacy detector returned an invalid result") from exc
    if payload.get("ok") is not True or not {"Jordan Lee", "jordan@example.com"} <= found:
        raise PrivacyRuntimeError("privacy detector failed its synthetic readiness check")
    return versions


def provision(target: Path, base_python: Path) -> dict[str, str]:
    target = checked_target(target)
    python = runtime_python(target)
    if target.exists():
        versions = check(python)
        return {"status": "ready", "python": str(python), "packages": versions}
    if not REQUIREMENTS.is_file() or not SIDECAR.is_file():
        raise PrivacyRuntimeError("privacy deployment inputs are missing")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        run([str(base_python), "-m", "venv", str(target)],
            timeout=180, action="environment creation")
        run([
            str(python), "-m", "pip", "install", "--disable-pip-version-check",
            "--no-input", "-r", str(REQUIREMENTS),
        ], timeout=900, action="dependency installation")
        run([str(python), "-m", "pip", "check"],
            timeout=60, action="dependency compatibility check")
        versions = check(python)
    except Exception:
        # Only a target absent before this call is removed. Never touch an
        # existing environment that another HASHI process might be using.
        if target.is_dir():
            shutil.rmtree(target)
        raise
    return {"status": "ready", "python": str(python), "packages": versions}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, default=ROOT / ".venv-privacy")
    parser.add_argument("--base-python", type=Path, default=Path(sys.executable))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        target = checked_target(args.runtime_dir)
        receipt = (
            {"status": "ready", "python": str(runtime_python(target)),
             "packages": check(runtime_python(target))}
            if args.check else provision(target, args.base_python)
        )
    except (OSError, ValueError, PrivacyRuntimeError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
