#!/usr/bin/env python3
"""Stage or finalize one runtime-proven Telegram owner replacement."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from orchestrator.telegram_owner import (  # noqa: E402
    finalize_telegram_owner_secret,
    stage_telegram_owner_replacement,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    subcommands = parser.add_subparsers(dest="operation", required=True)
    stage = subcommands.add_parser("stage")
    stage.add_argument("--new-owner", required=True)
    stage.add_argument("--expected-current-owner", required=True)
    stage.add_argument("--proof-agent", default="hashiko")
    finalize = subcommands.add_parser("finalize")
    finalize.add_argument("--owner", required=True)
    finalize.add_argument("--expected-previous-owner", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.operation == "stage":
        result = stage_telegram_owner_replacement(
            bridge_home=args.root,
            new_owner_id=args.new_owner,
            expected_current_owner_id=args.expected_current_owner,
            proof_agent_id=args.proof_agent,
        )
    else:
        from orchestrator.workbench_auth import _windows_remote_runtime_sids

        root = Path(args.root).expanduser().resolve()
        result = finalize_telegram_owner_secret(
            bridge_home=root,
            owner_id=args.owner,
            expected_previous_owner_id=args.expected_previous_owner,
            private_full_control_sids=_windows_remote_runtime_sids(root),
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
