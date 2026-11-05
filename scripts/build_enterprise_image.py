#!/usr/bin/env python3
"""Build the enterprise image from a committed, secret-free source export."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from orchestrator.function_source_publication import (  # noqa: E402
    SNAPSHOT_NAME, capture_git_source, validate_snapshot,
)


def export_context(destination):
    destination = Path(destination).resolve()
    if destination.exists():
        raise ValueError("Container context must be a new directory")
    snapshot = capture_git_source(ROOT)
    destination.mkdir(parents=True)
    for relative in snapshot["files"]:
        source, target = ROOT / relative, destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        shutil.copymode(source, target)
    validate_snapshot(snapshot, destination, required_paths=snapshot["files"])
    (destination / SNAPSHOT_NAME).write_text(json.dumps(snapshot, sort_keys=True), encoding="utf-8")
    return snapshot


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--extras", default="")
    parser.add_argument("--context-only", type=Path)
    args = parser.parse_args(argv)
    if args.context_only is not None:
        print(export_context(args.context_only)["source_commit"])
        return 0
    with tempfile.TemporaryDirectory(prefix="hashi-enterprise-source-") as temporary:
        context = Path(temporary) / "context"
        source = export_context(context)
        subprocess.run([
            "docker", "build", "-f", str(context / "Dockerfile.enterprise"),
            "--tag", args.tag, "--build-arg", "HASHI_ENTERPRISE_EXTRAS=" + args.extras,
            "--label", "org.opencontainers.image.revision=" + source["source_commit"],
            str(context)], check=True, timeout=3600)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
