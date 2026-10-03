#!/usr/bin/env python3
"""Offline operator CLI for schema-5 history backfill into existing Agents."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from orchestrator.agent_move.history_backfill import (  # noqa: E402
    apply_history_backfill,
    dry_run_history_backfill,
    export_history_capsules,
    history_backfill_status,
    rollback_history_backfill,
)
from orchestrator.agent_move.package import AgentMoveError  # noqa: E402


def _manifest(path: str) -> dict:
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise AgentMoveError("history backfill manifest must be a JSON object")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("export", "dry-run", "apply", "status", "rollback")
    )
    parser.add_argument("--source-root")
    parser.add_argument("--source-instance")
    parser.add_argument("--target-root")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--capsule-dir")
    parser.add_argument("--expected-plan-digest")
    parser.add_argument("--backup-dir")
    args = parser.parse_args(argv)
    try:
        manifest = _manifest(args.manifest)
        if args.command == "export":
            if not args.source_root or not args.source_instance or not args.capsule_dir:
                parser.error(
                    "export requires --source-root, --source-instance, and --capsule-dir"
                )
            result = export_history_capsules(
                args.source_root,
                args.source_instance,
                manifest,
                args.capsule_dir,
            )
        elif args.command == "dry-run":
            if not args.target_root or not args.capsule_dir:
                parser.error("dry-run requires --target-root and --capsule-dir")
            result = dry_run_history_backfill(
                args.target_root, manifest, capsule_dir=args.capsule_dir
            )
        elif args.command == "apply":
            if (
                not args.target_root
                or not args.capsule_dir
                or not args.expected_plan_digest
                or not args.backup_dir
            ):
                parser.error(
                    "apply requires --target-root, --capsule-dir, "
                    "--expected-plan-digest, and --backup-dir"
                )
            result = apply_history_backfill(
                args.target_root,
                manifest,
                capsule_dir=args.capsule_dir,
                expected_plan_digest=args.expected_plan_digest,
                backup_dir=args.backup_dir,
            )
        elif args.command == "status":
            if not args.target_root:
                parser.error("status requires --target-root")
            result = history_backfill_status(args.target_root, manifest)
        else:
            if not args.target_root:
                parser.error("rollback requires --target-root")
            result = rollback_history_backfill(args.target_root, manifest)
    except (AgentMoveError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
