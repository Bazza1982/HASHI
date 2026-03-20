#!/usr/bin/env python3
"""Check whether a workspace has successfully upgraded to native vector storage."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from orchestrator.bridge_memory import BridgeMemoryStore


def _fmt_pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _overall_summary(status: dict) -> str:
    overall = status["overall_status"]
    if overall == "fully_upgraded":
        return "PASS: native vector storage is enabled and fully backfilled."
    if overall == "partially_upgraded":
        return "WARN: native vector storage is enabled, but backfill is incomplete."
    if overall == "upgrade_available_not_enabled":
        return "WARN: model and sqlite-vec look available, but native vector storage is not enabled."
    return "WARN: workspace is still using legacy fallback retrieval."


def _print_human(status: dict):
    print(_overall_summary(status))
    print(f"Workspace DB: {status['db_path']}")
    print("")
    print("Runtime")
    print(f"  encoder_ready: {status['encoder_ready']}")
    print(f"  encoder_dim: {status['encoder_dim']}")
    print(f"  sqlite_vec_supported: {status['sqlite_vec_supported']}")
    print(f"  vec_enabled: {status['vec_enabled']}")
    print(f"  vec_dim: {status['vec_dim']}")
    if status.get("encoder_error"):
        print(f"  encoder_error: {status['encoder_error']}")
    if status.get("vec_reason"):
        print(f"  vec_reason: {status['vec_reason']}")
    print("")
    print("Tables")
    for table_name in ("memory_vec", "turns_vec"):
        table = status["tables"][table_name]
        print(f"  {table_name}: exists={table['exists']} dim={table['dim']}")
    print("")
    print("Counts")
    print(
        f"  memories: {status['counts']['memory_vec']}/{status['counts']['memories']} "
        f"({_fmt_pct(status['coverage']['memories'])})"
    )
    print(
        f"  turns: {status['counts']['turns_vec']}/{status['counts']['turns']} "
        f"({_fmt_pct(status['coverage']['turns'])})"
    )


def check_workspace(workspace_dir: Path) -> dict:
    db_path = workspace_dir / "bridge_memory.sqlite"
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")
    store = BridgeMemoryStore(workspace_dir)
    return store.get_vector_status()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "workspace",
        help="Workspace directory containing bridge_memory.sqlite",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of human-readable text",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero unless native vectors are enabled and fully backfilled",
    )
    args = parser.parse_args()

    try:
        status = check_workspace(Path(args.workspace).expanduser().resolve())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
    if args.json:
        print(json.dumps(status, indent=2, ensure_ascii=False))
    else:
        _print_human(status)

    if args.strict and status["overall_status"] != "fully_upgraded":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
