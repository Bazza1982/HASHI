#!/usr/bin/env python3
"""Verify or qualify the complete copied Portable roles; never start services."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from orchestrator.runtime_release_capsule import (  # noqa: E402
    materialize_release_payloads, qualify_relocated_roles, verify_capsule, verify_installed_roles,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("verify-bundle", "expand-bundle", "qualify-installed", "verify-installed"))
    parser.add_argument("--image-root", type=Path, required=True)
    args = parser.parse_args(argv)
    image = args.image_root.absolute()
    code = image / "app/hashi"
    if code.resolve() != ROOT:
        raise ValueError("Portable qualifier must belong to the exact installed program")
    function = {"verify-bundle": verify_capsule,
                "expand-bundle": materialize_release_payloads,
                "qualify-installed": qualify_relocated_roles,
                "verify-installed": verify_installed_roles}[args.operation]
    result = function(image, code)
    print(json.dumps({"status": "qualified", "operation": args.operation,
                      "schema_version": result["schema_version"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
# HASHI-Fingerprint: hfp_dfc8190e23b04504a27f901e0f89b3ac
