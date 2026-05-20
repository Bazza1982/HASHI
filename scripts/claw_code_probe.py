#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from adapters.claw_cli import (
    ClawError,
    find_claw_binary,
    run_claw_doctor,
    run_claw_state,
    run_claw_status,
    run_claw_version,
)


def _emit(payload: dict, *, pretty: bool) -> None:
    print(json.dumps(payload, indent=2 if pretty else None, sort_keys=pretty))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe a configured Claw Code binary.")
    parser.add_argument("--binary", help="Path to the claw executable. Falls back to CLAW_BINARY/CLAW_BIN/PATH.")
    parser.add_argument("--cwd", default=".", help="Workspace directory to run diagnostics in.")
    parser.add_argument("--timeout", type=float, default=30.0, help="Timeout in seconds per command.")
    parser.add_argument(
        "--check",
        choices=("version", "doctor", "status", "state", "all"),
        default="all",
        help="Diagnostic command to run.",
    )
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")
    args = parser.parse_args(argv)

    cwd = Path(args.cwd).expanduser().resolve()
    try:
        binary = find_claw_binary(args.binary)
        checks = ["version", "doctor", "status", "state"] if args.check == "all" else [args.check]
        results = {}
        ok = True
        for check in checks:
            try:
                if check == "version":
                    data = run_claw_version(cwd, binary_path=binary, timeout_s=args.timeout)
                elif check == "doctor":
                    data = run_claw_doctor(cwd, binary_path=binary, timeout_s=args.timeout)
                elif check == "status":
                    data = run_claw_status(cwd, binary_path=binary, timeout_s=args.timeout)
                elif check == "state":
                    data = run_claw_state(cwd, binary_path=binary, timeout_s=args.timeout)
                else:
                    raise AssertionError(f"unhandled check: {check}")
                results[check] = {"ok": True, "data": data}
            except ClawError as exc:
                ok = False
                results[check] = {
                    "ok": False,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
        _emit({"ok": ok, "binary": str(binary), "cwd": str(cwd), "checks": results}, pretty=args.pretty)
        return 0 if ok else 1
    except ClawError as exc:
        _emit({"ok": False, "error_type": type(exc).__name__, "error": str(exc), "cwd": str(cwd)}, pretty=args.pretty)
        return 1


if __name__ == "__main__":
    sys.exit(main())
