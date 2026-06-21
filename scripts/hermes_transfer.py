#!/usr/bin/env python3
"""Operator CLI for HASHI Hermes agent transfer packages."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from orchestrator.hermes_transfer import (
    HashiImportOptions,
    HermesImportOptions,
    MoveFinalizeOptions,
    export_hashi_agent,
    export_hermes_agent,
    finalize_hashi_to_hermes_move_source,
    finalize_hermes_to_hashi_move_source,
    import_hashi_agent,
    import_hermes_agent,
    plan_hashi_export,
    plan_hashi_import,
    plan_hermes_export,
    plan_hermes_import,
)


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        payload = _dispatch(args)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    print(json.dumps({"ok": True, **payload}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _dispatch(args: argparse.Namespace) -> dict[str, Any]:
    command = args.command
    if command == "plan-hashi-export":
        plan = plan_hashi_export(args.hashi_root, args.agent, args.output)
        return _plan_payload(command, plan)
    if command == "export-hashi":
        result = export_hashi_agent(args.hashi_root, args.agent, args.output)
        return {"command": command, "package_path": str(result.package_path), "manifest": result.manifest}
    if command == "plan-hashi-import":
        plan = plan_hashi_import(args.hashi_root, args.package, options=HashiImportOptions(target_agent_id=args.target_agent, overwrite=args.overwrite, enable=args.enable))
        return _plan_payload(command, plan)
    if command == "import-hashi":
        plan = import_hashi_agent(args.hashi_root, args.package, options=HashiImportOptions(target_agent_id=args.target_agent, overwrite=args.overwrite, enable=args.enable))
        return _plan_payload(command, plan)
    if command == "plan-hermes-export":
        plan = plan_hermes_export(args.profile_dir, args.bridge_home, args.agent, args.output)
        return _plan_payload(command, plan)
    if command == "export-hermes":
        result = export_hermes_agent(args.profile_dir, args.bridge_home, args.agent, args.output)
        return {"command": command, "package_path": str(result.package_path), "manifest": result.manifest}
    if command == "plan-hermes-import":
        plan = plan_hermes_import(args.profile_dir, args.bridge_home, args.package, options=HermesImportOptions(target_profile_name=args.target_profile, overwrite=args.overwrite))
        return _plan_payload(command, plan)
    if command == "import-hermes":
        plan = import_hermes_agent(args.profile_dir, args.bridge_home, args.package, options=HermesImportOptions(target_profile_name=args.target_profile, overwrite=args.overwrite))
        return _plan_payload(command, plan)
    if command == "finalize-move-source":
        options = MoveFinalizeOptions(target_verified=args.target_verified, package_id=args.package_id)
        if args.direction == "hashi-to-hermes":
            result = finalize_hashi_to_hermes_move_source(args.hashi_root, args.agent, options=options)
        else:
            result = finalize_hermes_to_hashi_move_source(args.profile_dir, args.bridge_home, args.profile, options=options)
        return {
            "command": command,
            "source_runtime": result.source_runtime,
            "source_id": result.source_id,
            "changed_paths": [str(path) for path in result.changed_paths],
            "warnings": result.warnings,
        }
    raise ValueError(f"unknown command: {command}")


def _plan_payload(command: str, plan: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "command": command,
        "dry_run_plan": plan.dry_run_report.to_dict(),
        "warnings": list(getattr(plan, "warnings", [])),
    }
    if hasattr(plan, "package_path"):
        payload["package_path"] = str(plan.package_path)
    if hasattr(plan, "target_agent_id"):
        payload["target_agent_id"] = plan.target_agent_id
    if hasattr(plan, "target_profile_name"):
        payload["target_profile_name"] = plan.target_profile_name
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("plan-hashi-export")
    _add_hashi_export_args(p)
    p = sub.add_parser("export-hashi")
    _add_hashi_export_args(p)

    p = sub.add_parser("plan-hashi-import")
    _add_hashi_import_args(p)
    p = sub.add_parser("import-hashi")
    _add_hashi_import_args(p)

    p = sub.add_parser("plan-hermes-export")
    _add_hermes_export_args(p)
    p = sub.add_parser("export-hermes")
    _add_hermes_export_args(p)

    p = sub.add_parser("plan-hermes-import")
    _add_hermes_import_args(p)
    p = sub.add_parser("import-hermes")
    _add_hermes_import_args(p)

    p = sub.add_parser("finalize-move-source")
    p.add_argument("--direction", choices=["hashi-to-hermes", "hermes-to-hashi"], required=True)
    p.add_argument("--target-verified", action="store_true", help="required safety gate before source disable")
    p.add_argument("--package-id")
    p.add_argument("--hashi-root", type=Path)
    p.add_argument("--agent")
    p.add_argument("--profile-dir", type=Path)
    p.add_argument("--bridge-home", type=Path)
    p.add_argument("--profile")
    return parser


def _add_hashi_export_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--hashi-root", type=Path, required=True)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--output", type=Path)


def _add_hashi_import_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--hashi-root", type=Path, required=True)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--target-agent")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--enable", action="store_true")


def _add_hermes_export_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--profile-dir", type=Path, required=True)
    parser.add_argument("--bridge-home", type=Path, required=True)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--output", type=Path)


def _add_hermes_import_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--profile-dir", type=Path, required=True)
    parser.add_argument("--bridge-home", type=Path, required=True)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--target-profile")
    parser.add_argument("--overwrite", action="store_true")


if __name__ == "__main__":
    raise SystemExit(main())
