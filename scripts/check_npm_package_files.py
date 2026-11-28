"""Prepack guard: the npm allow-list must ship every required Function package."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from orchestrator.npm_package_contract import audit_package_tree  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(ROOT))
    arguments = parser.parse_args(argv)
    report = audit_package_tree(Path(arguments.root))
    if report["ok"]:
        print("npm package allow-list covers every Function module package.")
        return 0
    print(report["reason"] or "npm package allow-list check failed.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
# HASHI-Fingerprint: hfp_17fc273bdacc453e9bad95309c5b41ca
