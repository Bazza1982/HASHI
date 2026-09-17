#!/usr/bin/env python3
"""Plan, apply, verify, or revoke one narrow Windows restart-service ACE."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from orchestrator.config_json import read_config_json  # noqa: E402
from orchestrator.live_runtime_protection import POLICY_RELATIVE_PATH  # noqa: E402
from orchestrator.windows_service_acl import (  # noqa: E402
    grant_windows_service_restart,
    read_windows_service_sddl,
    revoke_windows_service_restart,
    windows_service_has_restart_access,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Grant only service start/stop to the configured Remote principal."
    )
    parser.add_argument("action", choices=("plan", "apply", "verify", "restore"))
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--bridge-home", type=Path, required=True)
    parser.add_argument("--service-name", required=True)
    parser.add_argument("--runtime-principal", required=True)
    parser.add_argument("--confirm")
    return parser


def _configured_target(args: argparse.Namespace) -> str:
    path = args.bridge_home.resolve() / POLICY_RELATIVE_PATH
    payload = read_config_json(path)
    targets = payload.get("service_targets")
    if not isinstance(targets, list):
        raise ValueError("live runtime policy has no service_targets list")
    matches = {
        str(value).strip()
        for value in targets
        if str(value).strip().casefold() == args.service_name.strip().casefold()
    }
    if len(matches) != 1:
        raise ValueError("service name is not an exact configured service target")
    return next(iter(matches))


def _confirmation(args: argparse.Namespace, verb: str) -> None:
    expected = f"{verb} RESTART SERVICE {args.instance_id}"
    if args.confirm != expected:
        raise ValueError(f"confirmation must exactly match: {expected}")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    service_name = _configured_target(args)
    configured = windows_service_has_restart_access(
        read_windows_service_sddl(service_name),
        args.runtime_principal,
    )
    if args.action == "plan":
        result = {
            "action": "plan",
            "instance_id": args.instance_id,
            "service_name": service_name,
            "restart_access_configured": configured,
            "changes_applied": False,
        }
    elif args.action == "verify":
        result = {
            "action": "verify",
            "instance_id": args.instance_id,
            "service_name": service_name,
            "restart_access_configured": configured,
        }
        if not configured:
            print(json.dumps(result, indent=2))
            return 1
    elif args.action == "apply":
        _confirmation(args, "GRANT")
        result = {
            "action": "apply",
            "instance_id": args.instance_id,
            **grant_windows_service_restart(
                service_name,
                runtime_sid=args.runtime_principal,
            ),
        }
    else:
        _confirmation(args, "REVOKE")
        result = {
            "action": "restore",
            "instance_id": args.instance_id,
            **revoke_windows_service_restart(
                service_name,
                runtime_sid=args.runtime_principal,
            ),
        }
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
