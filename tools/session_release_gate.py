#!/usr/bin/env python3
"""Build and verify source-locked Persistent Session v1 qualification artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


def build_package(root: Path, output: Path, *, rollback_sha: str) -> dict[str, Any]:
    revision = _git(root, "rev-parse", "HEAD")
    if _git(root, "status", "--porcelain"):
        raise RuntimeError("release package requires a clean worktree")
    rollback = _git(root, "rev-parse", f"{rollback_sha}^{{commit}}")
    files = _git(root, "ls-files").splitlines()
    manifest = {
        "format": "hashi-persistent-session-release-v1",
        "hashi_revision": revision,
        "rollback_revision": rollback,
        "capabilities": {
            "session": "1.0",
            "event": "1.0",
            "control": "1.0",
            "attachment": "1.0",
            "approval": "1.0",
            "fencing": "1.0",
        },
        "files": [{"path": name, "sha256": _sha256(root / name)} for name in files],
    }
    with tempfile.TemporaryDirectory() as temporary:
        stage = Path(temporary)
        _write_json(stage / "manifest.json", manifest)
        source = stage / "source.tar"
        with tarfile.open(source, "w", format=tarfile.PAX_FORMAT) as archive:
            for name in files:
                info = archive.gettarinfo(str(root / name), arcname=name)
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mtime = 0
                with (root / name).open("rb") as stream:
                    archive.addfile(info, stream)
        manifest["source_archive_sha256"] = _sha256(source)
        _write_json(stage / "manifest.json", manifest)
        with tarfile.open(output, "w:gz", format=tarfile.PAX_FORMAT) as archive:
            for name in ("manifest.json", "source.tar"):
                archive.add(stage / name, arcname=name)
    return {**manifest, "package_sha256": _sha256(output)}


def verify_package(
    package: Path, *, public_key: Path | None = None, signature: Path | None = None
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as temporary:
        stage = Path(temporary)
        with tarfile.open(package, "r:gz") as archive:
            names = set(archive.getnames())
            if names != {"manifest.json", "source.tar"}:
                raise RuntimeError("unexpected release package members")
            archive.extractall(stage, filter="data")
        manifest = json.loads((stage / "manifest.json").read_text(encoding="utf-8"))
        if _sha256(stage / "source.tar") != manifest["source_archive_sha256"]:
            raise RuntimeError("source archive hash mismatch")
        with tarfile.open(stage / "source.tar", "r:") as source:
            members = {row.name: row for row in source.getmembers()}
            for expected in manifest["files"]:
                member = members.get(expected["path"])
                stream = source.extractfile(member) if member else None
                if (
                    stream is None
                    or hashlib.sha256(stream.read()).hexdigest() != expected["sha256"]
                ):
                    raise RuntimeError(f"artifact hash mismatch: {expected['path']}")
    signature_verified = False
    if public_key or signature:
        if not public_key or not signature:
            raise RuntimeError("public key and detached signature are both required")
        subprocess.run(
            [
                "openssl",
                "dgst",
                "-sha256",
                "-verify",
                str(public_key),
                "-signature",
                str(signature),
                str(package),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        signature_verified = True
    return {
        "ok": True,
        "package_sha256": _sha256(package),
        "manifest": manifest,
        "signature_verified": signature_verified,
    }


def qualification_receipt(capture: Path, output: Path) -> dict[str, Any]:
    data = json.loads(capture.read_text(encoding="utf-8"))
    required = (
        "hashi_revision",
        "client_revision",
        "client_profile",
        "deployment_lock_sha256",
        "compatibility_record",
        "session_id",
        "run_id",
        "request_id",
        "event_consumer_id",
        "acknowledged_sequence",
        "provider_envelope_sha256",
        "history_messages",
        "required_history_messages",
        "current_request_occurrences",
        "cross_session_sentinel_occurrences",
        "terminal_state",
    )
    missing = [name for name in required if data.get(name) in (None, "")]
    if missing:
        raise RuntimeError("qualification capture is incomplete: " + ", ".join(missing))
    if (
        int(data["history_messages"]) < int(data["required_history_messages"])
        or int(data["current_request_occurrences"]) != 1
    ):
        raise RuntimeError("history-size/current-request qualification failed")
    if (
        int(data["cross_session_sentinel_occurrences"]) != 0
        or data["terminal_state"] != "completed"
    ):
        raise RuntimeError("isolation/terminal qualification failed")
    receipt = {
        "format": "hashi-qualify-receipt-v1",
        "result": "passed",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "capture": data,
        "capture_sha256": _sha256(capture),
    }
    _write_json(output, receipt)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("--root", type=Path, default=Path.cwd())
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--rollback-sha", required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("--package", type=Path, required=True)
    verify.add_argument("--public-key", type=Path)
    verify.add_argument("--signature", type=Path)
    lane = sub.add_parser("qualify")
    lane.add_argument("--capture", type=Path, required=True)
    lane.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = (
        build_package(
            args.root.resolve(), args.output.resolve(), rollback_sha=args.rollback_sha
        )
        if args.command == "build"
        else (
            verify_package(
                args.package.resolve(),
                public_key=args.public_key,
                signature=args.signature,
            )
            if args.command == "verify"
            else qualification_receipt(args.capture.resolve(), args.output.resolve())
        )
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
