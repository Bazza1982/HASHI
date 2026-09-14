#!/usr/bin/env python3
"""Thin CLI for HCC refresh jobs; no network access and no duplicate state store."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Resolve from this skill installation, never from a caller-controlled cwd.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from orchestrator.hcc import inspect_hcc_entry, replace_hcc_entry  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, help="Current Agent workspace (not a session Workzone)")
    sub = parser.add_subparsers(dest="command", required=True)
    inspect_parser = sub.add_parser("inspect", help="Return only the selected entry revision")
    inspect_parser.add_argument("entry")
    replace_parser = sub.add_parser("replace", help="Publish a complete, successful observation")
    replace_parser.add_argument("entry")
    replace_parser.add_argument("--expected", required=True, help="Digest from inspect, or literal absent")
    replace_parser.add_argument("--file", type=Path, help="UTF-8 body file; defaults to stdin")
    args = parser.parse_args(argv)
    bound = os.environ.get("BRIDGE_WORKSPACE_DIR")
    workspace = args.workspace or (Path(bound) if bound else None)
    if workspace is None:
        parser.error("--workspace or BRIDGE_WORKSPACE_DIR is required; cwd is not an identity")
    workspace = workspace.expanduser().resolve()
    if bound and workspace != Path(bound).expanduser().resolve():
        parser.error("--workspace must match the bound Agent workspace")
    try:
        if args.command == "inspect":
            result = inspect_hcc_entry(workspace, args.entry)
        else:
            content = args.file.read_bytes().decode("utf-8") if args.file else sys.stdin.read()
            doc = replace_hcc_entry(
                workspace, args.entry, content,
                expected_entry_sha256=None if args.expected == "absent" else args.expected,
            )
            result = {"entry": args.entry, "updated": True, "pcm_sha256": doc.content_sha256}
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        # Never emit source body or arbitrary exception text to command logs.
        print(json.dumps({"error": type(exc).__name__, "updated": False}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
