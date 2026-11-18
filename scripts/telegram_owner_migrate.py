#!/usr/bin/env python3
"""Stage or finalize one Telegram owner account replacement."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
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
    stage.add_argument("--proof-mode", choices=("administrator_selected", "observed"),
                       default="administrator_selected")
    stage.add_argument("--proof-max-age-seconds", type=int, default=600)
    stage.add_argument("--revoke-all-delegates", action="store_true")
    finalize = subcommands.add_parser("finalize")
    finalize.add_argument("--owner", required=True)
    finalize.add_argument("--expected-previous-owner", required=True)
    finalize.add_argument("--expected-config-revision")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.operation == "stage":
        result = stage_telegram_owner_replacement(
            bridge_home=args.root,
            new_owner_id=args.new_owner,
            expected_current_owner_id=args.expected_current_owner,
            proof_agent_id=args.proof_agent,
            proof_mode=args.proof_mode,
            proof_max_age=timedelta(seconds=args.proof_max_age_seconds),
            revoke_all_delegates=args.revoke_all_delegates,
        )
    else:
        from orchestrator.workbench_auth import resolve_remote_runtime_sids

        root = Path(args.root).expanduser().resolve()
        from orchestrator.config_json import read_config_json

        expected_revision = args.expected_config_revision or read_config_json(root / "agents.json").revision
        result = finalize_telegram_owner_secret(
            bridge_home=root,
            owner_id=args.owner,
            expected_previous_owner_id=args.expected_previous_owner,
            expected_config_revision=expected_revision,
            private_full_control_sids=resolve_remote_runtime_sids(root),
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
