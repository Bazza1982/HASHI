#!/usr/bin/env python3
"""Operator-only prepare/health/activate for an independent Office generation.

No install-on-demand. Install the complete hash lock into a NEW environment
first. Activation changes only the explicitly named platform configuration.
"""
from __future__ import annotations
import argparse
import asyncio
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.office.runtime import OfficeRuntime, qualify_runtime
from orchestrator.config_json import read_config_json, new_config_json, write_config_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--python", type=Path, required=True)
    prepare.add_argument("--destination", type=Path, required=True)
    prepare.add_argument("--converter", type=Path)
    for name in ("health", "activate"):
        command = commands.add_parser(name)
        command.add_argument("--manifest", type=Path, required=True)
        command.add_argument("--state-root", type=Path, required=True)
        command.add_argument("--work-root", type=Path, required=True)
        if name == "activate":
            command.add_argument("--config", type=Path, required=True)
            command.add_argument("--minimum-free-bytes", type=int, default=2*1024**3)
    args = parser.parse_args()
    if args.operation == "prepare":
        manifest = qualify_runtime(args.python, args.destination, converter=args.converter)
        print(json.dumps({"status":"qualified","manifest":str(manifest)}))
        return 0
    options = {"manifest":str(args.manifest), "state_root":str(args.state_root), "work_root":str(args.work_root)}
    runtime = OfficeRuntime(options)
    health = asyncio.run(runtime.invoke("health", {}, args.state_root / "health" / uuid4().hex))
    if health.get("status") != "ok":
        raise RuntimeError("Office health failed")
    if args.operation == "health":
        print(json.dumps(health, ensure_ascii=False))
        return 0
    if not args.config.is_absolute():
        raise ValueError("Activation config path must be absolute")
    document = read_config_json(args.config) if args.config.exists() else new_config_json(args.config)
    old = dict(document)
    document.clear()
    document.update(options, minimum_free_bytes=args.minimum_free_bytes)
    revision = write_config_json(args.config, document)
    print(json.dumps({"status":"activated","config":str(args.config),"revision":revision,
                      "previous":old,"restart_performed":False}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
