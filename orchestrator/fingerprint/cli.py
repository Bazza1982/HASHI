"""Local management and portable evidence CLI; never exposed as a model tool."""
from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
from types import SimpleNamespace


def configure_parser(subparsers):
    parser = subparsers.add_parser("fingerprint", help="Artifact provenance and exact-version evidence")
    actions = parser.add_subparsers(dest="fingerprint_action", required=True)
    status = actions.add_parser("status", help="Show configured policy and declared coverage")
    status.add_argument("--home", type=Path)
    status.add_argument("--config", type=Path)
    inspect = actions.add_parser("inspect", help="Inspect one file against portable evidence")
    inspect.add_argument("path", type=Path)
    inspect.add_argument("--root", type=Path)
    verify = actions.add_parser("verify", help="Check declared project artifact versions")
    verify.add_argument("path", type=Path)
    export = actions.add_parser("export", help="Export minimal unsigned evidence")
    export.add_argument("path", type=Path)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--include-content", action="store_true", help="Explicitly include selected artifact bytes")
    settings = actions.add_parser("set", help="Apply an authorized local ON/OFF choice")
    settings.add_argument("value", choices=("on", "off"))
    settings.add_argument("--home", type=Path)
    settings.add_argument("--config", type=Path)
    settings.add_argument("--actor", required=True)
    settings.add_argument("--expected-revision", required=True)
    repair = actions.add_parser("repair", help="Rebuild projections without replaying business tools")
    repair.add_argument("path", type=Path)
    repair.add_argument("--home", type=Path)
    repair.add_argument("--config", type=Path)
    repair.add_argument("--agent", required=True)
    repair.add_argument("--actor", required=True)
    parser.set_defaults(func=run)
    return parser


def _management(args):
    from orchestrator.config_admin import ConfigAdmin
    from orchestrator.pathing import build_bridge_paths
    from orchestrator.config_json import read_config_json
    from orchestrator.canonical_audit import CanonicalAuditStore

    code_root = Path(__file__).resolve().parents[2]
    paths = build_bridge_paths(code_root, getattr(args, "home", None))
    config_path = getattr(args, "config", None) or paths.config_path
    admin = ConfigAdmin(SimpleNamespace(config_path=config_path))
    raw = read_config_json(config_path)
    global_settings = raw.get("global", {})
    # Local management uses configured identity, never directory names.
    instance_id = str(global_settings.get("instance_id") or "HASHI")
    if args.fingerprint_action == "status":
        return admin, None
    audit = CanonicalAuditStore(paths.bridge_home, instance_id=instance_id,
        agent_id=getattr(args, "agent", "fingerprint-management"),
        config=global_settings.get("canonical_audit") or {})
    return admin, audit


def run(args) -> int:
    from .coverage import matrix
    from .models import FingerprintPolicy
    from .service import FingerprintAudit
    try:
        action = args.fingerprint_action
        if action == "status":
            admin, _audit = _management(args)
            snapshot = admin.read_fingerprint_policy()
            result = {"configured": "ON" if snapshot.policy.enabled else "OFF",
                      "config_revision": snapshot.revision, "policy_version": snapshot.policy.version,
                      "runtime_adoption": "not_verified", "coverage": matrix()}
        elif action == "set":
            admin, audit = _management(args)
            current = admin.read_fingerprint_policy()
            updated = FingerprintPolicy(enabled=args.value == "on", marking=current.policy.marking,
                                        export_content=current.policy.export_content)
            receipt = admin.write_fingerprint_policy(updated,
                expected_revision=args.expected_revision, actor_id=args.actor, audit=audit)
            result = {"configured": args.value.upper(), "config_revision": receipt.revision,
                      "applies_to": "future Run admissions; existing Runs retain their frozen policy"}
        elif action == "inspect":
            from .verify import inspect
            result = inspect(args.path, root=args.root)
        elif action == "verify":
            from .verify import verify
            result = verify(args.path)
        elif action == "export":
            from .export import export_bundle
            path = args.path.resolve()
            if path.is_dir():
                root, relative = path, None
            else:
                root = next((parent for parent in path.parents if (parent / ".hashi-fingerprint/manifest.json").exists()), path.parent)
                relative = path.relative_to(root).as_posix()
            result = export_bundle(root, args.output, relative=relative, include_content=args.include_content)
        else:
            from .recovery import repair
            admin, audit = _management(args)
            result = {"repairs": repair(FingerprintAudit(audit), args.path, authorization={
                "actor": args.actor, "purpose": "explicit local fingerprint projection repair", "allow_raw_audit": True})}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if action == "verify" and result["status"] != "consistent":
            return 1
        return 0
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        print(json.dumps({"status": "unavailable", "reason": str(exc)}, ensure_ascii=False))
        return 2


def main():
    import argparse
    parser = argparse.ArgumentParser(prog="hashi")
    configure_parser(parser.add_subparsers(dest="cmd", required=True))
    import sys
    return run(parser.parse_args(["fingerprint", *sys.argv[1:]]))
