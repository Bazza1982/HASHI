#!/usr/bin/env python3
"""Plan, apply, verify, or restore exact HASHI live-runtime permissions."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from orchestrator.config_json import (  # noqa: E402
    new_config_json,
    read_config_json,
    write_config_json,
)
from orchestrator.live_runtime_acl import (  # noqa: E402
    apply_posix_read_only,
    apply_windows_read_only,
    build_native_protection_targets,
    restore_posix_access,
    restore_windows_access,
    windows_current_sid,
)
from orchestrator.live_runtime_protection import (  # noqa: E402
    POLICY_RELATIVE_PATH,
    load_live_runtime_policy,
)


def _path(value: str) -> Path:
    path = Path(value).expanduser().resolve(strict=False)
    if path.parent == path:
        raise argparse.ArgumentTypeError("filesystem roots are not valid targets")
    return path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Protect only the configured live HASHI Core paths and interpreter; "
            "workspaces and development environments are not targets."
        )
    )
    parser.add_argument("action", choices=("plan", "apply", "verify", "restore"))
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--code-root", required=True, type=_path)
    parser.add_argument("--bridge-home", required=True, type=_path)
    parser.add_argument("--runtime-root", required=True, type=_path)
    parser.add_argument("--secrets-path", type=_path)
    parser.add_argument("--service-config", action="append", type=_path, default=[])
    parser.add_argument("--restart-secret", action="append", type=_path, default=[])
    parser.add_argument("--service-target", action="append", default=[])
    parser.add_argument(
        "--runtime-principal",
        help="Windows runtime SID, or POSIX runtime user",
    )
    parser.add_argument("--runtime-group", help="POSIX runtime group")
    parser.add_argument(
        "--lock-owner",
        action="store_true",
        help="Windows: set target owner to Administrators (requires elevation)",
    )
    parser.add_argument(
        "--immutable",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="POSIX: set the immutable bit on each top-level target",
    )
    parser.add_argument(
        "--confirm",
        help="Required for apply/restore: '<ACTION> LIVE RUNTIME <INSTANCE_ID>'",
    )
    return parser


def _policy_payload(args: argparse.Namespace) -> dict:
    write_paths = [*args.service_config, *args.restart_secret]
    read_paths = [*args.restart_secret]
    return {
        "schema": 1,
        "runtime_roots": [str(args.runtime_root)],
        "protected_write_paths": [str(path) for path in write_paths],
        "protected_read_paths": [str(path) for path in read_paths],
        "service_targets": sorted(
            {
                args.instance_id.strip(),
                *(str(value).strip() for value in args.service_target),
            }
            - {""}
        ),
    }


def _global_config(args: argparse.Namespace) -> SimpleNamespace:
    return SimpleNamespace(
        project_root=args.code_root,
        bridge_home=args.bridge_home,
        secrets_path=args.secrets_path,
        instance_id=args.instance_id,
    )


def _policy(args: argparse.Namespace, *, include_unpublished: bool):
    config = _global_config(args)
    policy = load_live_runtime_policy(config, runtime_prefix=args.runtime_root)
    if not include_unpublished:
        return policy
    if policy is None:
        raise ValueError("code-root and bridge-home are required")
    payload = _policy_payload(args)
    extension_write = tuple(Path(value).resolve() for value in payload["protected_write_paths"])
    extension_read = tuple(Path(value).resolve() for value in payload["protected_read_paths"])
    return type(policy)(
        code_root=policy.code_root,
        bridge_home=policy.bridge_home,
        runtime_roots=tuple(dict.fromkeys((*policy.runtime_roots, args.runtime_root))),
        protected_write_paths=tuple(
            dict.fromkeys(
                (*policy.protected_write_paths, *extension_write, policy.policy_path)
            )
        ),
        protected_read_paths=tuple(
            dict.fromkeys((*policy.protected_read_paths, *extension_read))
        ),
        service_targets=tuple(
            dict.fromkeys(
                value.casefold() for value in payload["service_targets"]
            )
        ),
        core_pid=policy.core_pid,
        policy_path=policy.policy_path,
        configuration_error=policy.configuration_error,
    )


def _publish_policy(args: argparse.Namespace) -> Path:
    path = args.bridge_home / POLICY_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    snapshot = read_config_json(path) if path.exists() else new_config_json(path)
    snapshot.clear()
    snapshot.update(_policy_payload(args))
    write_config_json(path, snapshot)
    return path


def _targets(args: argparse.Namespace, *, include_unpublished: bool):
    policy = _policy(args, include_unpublished=include_unpublished)
    if policy is None:
        raise ValueError("live runtime policy is unavailable")
    if include_unpublished and not policy.policy_path.exists():
        # Planning must remain read-only. The path is shown separately and is
        # added to the native target set only after publication during apply.
        protected = tuple(
            path for path in policy.protected_write_paths if path != policy.policy_path
        )
        policy = type(policy)(
            code_root=policy.code_root,
            bridge_home=policy.bridge_home,
            runtime_roots=policy.runtime_roots,
            protected_write_paths=protected,
            protected_read_paths=policy.protected_read_paths,
            service_targets=policy.service_targets,
            core_pid=policy.core_pid,
            policy_path=policy.policy_path,
            configuration_error=policy.configuration_error,
        )
        return build_native_protection_targets(policy), policy.policy_path
    return build_native_protection_targets(policy), None


def _confirmation(args: argparse.Namespace, action: str) -> None:
    expected = f"{action.upper()} LIVE RUNTIME {args.instance_id}"
    if args.confirm != expected:
        raise ValueError(f"confirmation must exactly match: {expected}")


def _runtime_identity(args: argparse.Namespace) -> tuple[str, str | None]:
    if os.name == "nt":
        return args.runtime_principal or windows_current_sid(), None
    if not args.runtime_principal or not args.runtime_group:
        raise ValueError("POSIX apply/verify/restore requires runtime principal and group")
    return args.runtime_principal, args.runtime_group


def _verify_windows(targets, sid: str) -> list[dict]:
    if windows_current_sid() != sid.upper():
        raise ValueError("Windows verification must run as the configured runtime SID")
    results = []
    for target in targets:
        path = target.path
        readable = os.access(path, os.R_OK)
        write_denied = False
        probe = None
        try:
            if path.is_dir():
                probe = path / f".hashi-write-probe-{uuid4().hex}"
                probe.write_bytes(b"")
            else:
                with path.open("r+b"):
                    pass
        except PermissionError:
            write_denied = True
        finally:
            if probe is not None:
                try:
                    probe.unlink()
                except FileNotFoundError:
                    pass
        results.append(
            {"path": str(path), "readable": readable, "write_denied": write_denied}
        )
    return results


def _verify_posix(targets, user: str) -> list[dict]:
    results = []
    for target in targets:
        path = target.path
        readable = subprocess.run(
            ["runuser", "-u", user, "--", "test", "-r", str(path)]
        ).returncode == 0
        write_denied = subprocess.run(
            ["runuser", "-u", user, "--", "test", "-w", str(path)]
        ).returncode != 0
        results.append(
            {"path": str(path), "readable": readable, "write_denied": write_denied}
        )
    return results


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.action == "plan":
        targets, pending_policy = _targets(args, include_unpublished=True)
        payload = {
            "action": "plan",
            "instance_id": args.instance_id,
            "platform": "windows" if os.name == "nt" else "posix",
            "targets": [
                {
                    "path": str(target.path),
                    "recursive": target.recursive,
                    "kind": target.kind,
                }
                for target in targets
            ],
            "policy_path": str(
                pending_policy or args.bridge_home / POLICY_RELATIVE_PATH
            ),
            "changes_applied": False,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    principal, group = _runtime_identity(args)
    if args.action == "apply":
        _confirmation(args, "protect")
        _publish_policy(args)
        targets, _pending = _targets(args, include_unpublished=False)
        if os.name == "nt":
            reports = apply_windows_read_only(
                targets,
                runtime_sid=principal,
                lock_owner=args.lock_owner,
            )
        else:
            reports = apply_posix_read_only(
                targets,
                runtime_user=principal,
                runtime_group=str(group),
                immutable=args.immutable,
            )
    elif args.action == "verify":
        targets, _pending = _targets(args, include_unpublished=False)
        reports = (
            _verify_windows(targets, principal)
            if os.name == "nt"
            else _verify_posix(targets, principal)
        )
        if not all(row["readable"] and row["write_denied"] for row in reports):
            print(json.dumps({"action": "verify", "reports": reports}, indent=2))
            return 1
    else:
        _confirmation(args, "restore")
        targets, _pending = _targets(args, include_unpublished=True)
        if os.name == "nt":
            reports = restore_windows_access(targets, runtime_sid=principal)
        else:
            reports = restore_posix_access(
                targets,
                runtime_user=principal,
                runtime_group=str(group),
                immutable=args.immutable,
            )
    print(
        json.dumps(
            {
                "action": args.action,
                "instance_id": args.instance_id,
                "reports": reports,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
