#!/usr/bin/env python3
"""Build and verify an integrated HER development candidate without activation."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

CODE_ROOT = Path(__file__).resolve().parents[1]
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))

from orchestrator.her_rebuild import RebuildStage  # noqa: E402
from orchestrator.her_rebuild_manager import (  # noqa: E402
    HERRebuildJobStore,
    HERRebuildManager,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build and verify HASHI's integrated HER Rust source. The offline "
            "entry never writes an active development selection or restarts an Agent."
        )
    )
    parser.add_argument(
        "--bridge-home",
        type=Path,
        default=CODE_ROOT,
        help="State/candidate root (default: HASHI code root)",
    )
    parser.add_argument(
        "--status",
        nargs="?",
        const="latest",
        metavar="JOB_ID",
        help="Print the latest or named offline rebuild record without building",
    )
    return parser


def _render(record) -> dict:
    return {
        "job_id": record.job_id,
        "state": record.state.value,
        "source_fingerprint": record.source_fingerprint,
        "candidate_id": record.candidate_id,
        "failure_kind": record.failure_kind.value if record.failure_kind else None,
        "error": record.error,
        "details": dict(record.details or {}),
    }


async def _run(arguments: argparse.Namespace) -> int:
    bridge_home = arguments.bridge_home.expanduser().resolve()
    if arguments.status:
        jobs = HERRebuildJobStore(
            bridge_home / "state" / "her_rebuild" / "jobs", create_root=False
        )
        record = (
            jobs.latest()
            if arguments.status == "latest"
            else jobs.get(arguments.status)
        )
        if record is None:
            print(json.dumps({"status": "not_found"}, sort_keys=True))
            return 1
        print(json.dumps(_render(record), indent=2, sort_keys=True))
        return 0

    kernel = SimpleNamespace(
        paths=SimpleNamespace(code_root=CODE_ROOT, bridge_home=bridge_home),
        runtimes=[],
    )
    manager = HERRebuildManager(kernel, idle_timeout_seconds=0)
    try:
        record, joined = await manager.submit(
            target_agent="offline-verification",
            actor_id="local-operator",
            origin={"channel": "local-offline"},
        )
        completed = await manager.wait(record.job_id)
        payload = _render(completed)
        payload["joined_existing_build"] = joined
        payload["offline_only"] = True
        print(json.dumps(payload, indent=2, sort_keys=True))
        return (
            0
            if completed.state
            in {RebuildStage.ACTIVATION_DEFERRED, RebuildStage.SUCCEEDED}
            else 1
        )
    finally:
        manager.close()


def main() -> int:
    return asyncio.run(_run(_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
