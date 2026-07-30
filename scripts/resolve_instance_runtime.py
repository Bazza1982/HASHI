#!/usr/bin/env python3
"""Print one instance-scoped runtime value for launch/control scripts."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parent.parent
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))

from orchestrator.pathing import build_bridge_paths  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--code-root", type=Path, required=True)
    parser.add_argument("--bridge-home", type=Path)
    parser.add_argument(
        "--field",
        choices=("instance-id", "runtime-dir", "lock-path", "pid-path"),
        required=True,
    )
    args = parser.parse_args()
    paths = build_bridge_paths(args.code_root, bridge_home=args.bridge_home)
    values = {
        "instance-id": paths.instance_id,
        "runtime-dir": paths.lock_path.parent,
        "lock-path": paths.lock_path,
        "pid-path": paths.pid_path,
    }
    print(values[args.field])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
