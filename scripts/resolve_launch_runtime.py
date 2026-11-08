#!/usr/bin/env python3
"""Resolve installer-qualified launch interpreters; never prepare or start them."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.hashi_instance_cli import _select_core_runtime  # noqa: E402
from tools.instance_registry import InstanceRegistryError  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code-root", type=Path, default=ROOT)
    parser.add_argument("--field", choices=("core-executable", "functions-executable"), required=True)
    args = parser.parse_args(argv)
    try:
        core, functions = _select_core_runtime(args.code_root.resolve(), with_functions=True)
        selected = core[0] if args.field == "core-executable" else functions[0]
        if not Path(selected).is_absolute() or not Path(selected).is_file():
            raise InstanceRegistryError("The installed runtime path is unavailable")
    except InstanceRegistryError as exc:
        print(f"HASHI runtime setup is incomplete: {exc}", file=sys.stderr)
        return 78
    print(selected)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
# HASHI-Fingerprint: hfp_aa5305b7a4bf4b36b7102b0963222f96
