#!/usr/bin/env python3
"""Build new qualified role environments without altering or adopting active ones."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from orchestrator.runtime_installation_builder import (  # noqa: E402
    snapshot_reference, build_reference_roles,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    operations = parser.add_subparsers(dest="operation", required=True)
    capture = operations.add_parser("snapshot-reference")
    capture.add_argument("--base-root", type=Path, required=True)
    capture.add_argument("--functions-site", type=Path, required=True)
    capture.add_argument("--baseline-dependency-digest", required=True)
    capture.add_argument("--output", type=Path, required=True)
    build = operations.add_parser("build-reference")
    build.add_argument("--reference", type=Path, required=True)
    build.add_argument("--reference-digest", required=True)
    build.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.operation == "snapshot-reference":
        digest = snapshot_reference(ROOT, base_root=args.base_root,
                                    functions_site=args.functions_site, destination=args.output,
                                    expected_dependency_digest=args.baseline_dependency_digest)
        result = {"reference": str(args.output.resolve()), "digest": digest}
    else:
        result = build_reference_roles(ROOT, args.reference,
                                       expected_digest=args.reference_digest, destination=args.output)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
