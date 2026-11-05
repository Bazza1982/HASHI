"""Offline Windows operator for a stopped, explicitly named permission installation.

Not registered in Agent tools or either public permission RPC endpoint.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import ctypes
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import time

from orchestrator.config_json import new_config_json, read_config_json, write_config_json
from orchestrator.permissions.authority import PermissionAuthority, PermissionDenied, RevisionConflict
from orchestrator.permissions.windows_security import InstallationLock, require_private_path


def _archive(private, ledger, *, recovery_id, expected_revision, reason, sid):
    root = private / "maintenance"
    root.mkdir(exist_ok=True)
    require_private_path(root, trusted_sids=(sid,))
    destination = root / recovery_id
    identity = {"recovery_id": recovery_id, "source_revision": expected_revision, "reason": reason}
    if destination.exists():
        require_private_path(destination, trusted_sids=(sid,))
        require_private_path(destination / "archive.json", trusted_sids=(sid,))
        manifest = read_config_json(destination / "archive.json")
        if any(manifest.get(key) != value for key, value in identity.items()):
            raise RevisionConflict("recovery archive belongs to different inputs")
        for name in ("ledger.sqlite", "witness.sqlite"):
            item = destination / name
            require_private_path(item, trusted_sids=(sid,))
            if sha256(item.read_bytes()).hexdigest() != manifest["sha256"][name]:
                raise PermissionDenied("recovery archive changed")
        return destination
    if PermissionAuthority.inspect_ledger(ledger)["revision"] != expected_revision:
        raise RevisionConflict("ledger changed before archive")
    destination.mkdir()
    require_private_path(destination, trusted_sids=(sid,))
    hashes = {}
    for source, name in ((ledger, "ledger.sqlite"), (ledger.with_name(ledger.name + ".continuity"), "witness.sqlite")):
        target = destination / name
        with closing(sqlite3.connect(source.absolute().as_uri() + "?mode=ro", uri=True)) as src:
            with closing(sqlite3.connect(target)) as dst:
                src.backup(dst)
        require_private_path(target, trusted_sids=(sid,))
        hashes[name] = sha256(target.read_bytes()).hexdigest()
    write_config_json(destination / "archive.json", new_config_json(
        destination / "archive.json", {"version": 1, **identity, "sha256": hashes},
    ), private_full_control_sids=(sid,), private_include_writer=False)
    require_private_path(destination / "archive.json", trusted_sids=(sid,))
    return destination


def maintain(installation_path, *, instance_id, expected_revision=None,
             recovery_id=None, reason=None):
    # Native admission precedes reading any private installation or ledger.
    import os
    if os.name != "nt":
        raise PermissionDenied("maintenance requires the native Windows operator")
    check = ctypes.WinDLL("shell32", use_last_error=True).IsUserAnAdmin
    check.argtypes, check.restype = [], ctypes.c_int
    if not check():
        raise PermissionDenied("an elevated trusted installation operator is required")
    installation_path = Path(installation_path)
    if not installation_path.is_absolute():
        raise ValueError("an absolute installation descriptor is required")
    installation = read_config_json(installation_path)
    if installation.get("version") != 1 or installation.get("instance_id") != instance_id:
        raise PermissionDenied("explicit instance does not match the installation")
    sid = installation["service_sid"]
    private = Path(installation["private_root"])
    require_private_path(installation_path, trusted_sids=(sid,))
    require_private_path(private, trusted_sids=(sid,))
    # Same kernel ownership lock as normal service startup, before ledger open.
    ownership = InstallationLock(private)
    try:
        ledger = private / "permissions.sqlite"
        for item in (ledger, ledger.with_name(ledger.name + ".continuity")):
            require_private_path(item, trusted_sids=(sid,))
            if item.stat().st_nlink != 1:
                raise PermissionDenied("linked ledgers cannot be maintained")
        if expected_revision is None:
            if recovery_id is not None or reason is not None:
                raise ValueError("inspection does not accept recovery inputs")
            observation = PermissionAuthority.inspect_ledger(ledger)
            if any(value != instance_id for value in observation["instance_ids"]):
                raise PermissionDenied("ledger belongs to another installation")
            return observation
        # Validate identifiers before using one as a protected archive component.
        import re
        if (not isinstance(recovery_id, str) or re.fullmatch(r"[0-9a-f]{32}", recovery_id) is None
                or not isinstance(expected_revision, str)
                or re.fullmatch(r"[0-9a-f]{64}", expected_revision) is None
                or not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500):
            raise ValueError("explicit recovery identity, revision and reason required")
        observation = PermissionAuthority.inspect_ledger(ledger)
        if any(value != instance_id for value in observation["instance_ids"]):
            raise PermissionDenied("ledger belongs to another installation")
        archive = _archive(private, ledger, recovery_id=recovery_id,
                           expected_revision=expected_revision, reason=reason, sid=sid)
        result = PermissionAuthority.reconcile_ledger(
            ledger, expected_revision=expected_revision, recovery_id=recovery_id,
            reason=reason, observed_at=time.time(), instance_id=instance_id,
        )
        return {**result, "archive": str(archive)}
    finally:
        ownership.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inspect", "reconcile"))
    parser.add_argument("--installation", required=True, type=Path)
    parser.add_argument("--instance", required=True)
    parser.add_argument("--expected-revision")
    parser.add_argument("--recovery-id")
    parser.add_argument("--reason")
    args = parser.parse_args(argv)
    if args.action == "reconcile" and any(value is None for value in (
            args.expected_revision, args.recovery_id, args.reason)):
        parser.error("reconcile requires the inspected revision, recovery ID and reason")
    if args.action == "inspect" and any(value is not None for value in (
            args.expected_revision, args.recovery_id, args.reason)):
        parser.error("inspect cannot contain recovery arguments")
    result = maintain(args.installation, instance_id=args.instance,
                      expected_revision=args.expected_revision,
                      recovery_id=args.recovery_id, reason=args.reason)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
# HASHI-Fingerprint: hfp_69a5c2cbd9654ae2b1f2d4f6a626baf9
