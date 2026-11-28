"""Prepack guard: npm's real tarball file list must ship every required Function package."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from orchestrator.npm_package_contract import audit_package_tree  # noqa: E402


def _read_pack_list(source: str) -> tuple[str, ...]:
    """File paths from a captured ``npm pack --dry-run --json`` payload."""

    if source == "-":
        payload = sys.stdin.read()
    else:
        payload = Path(source).read_text(encoding="utf-8")
    report = json.loads(payload)
    if isinstance(report, dict):
        report = [report]
    entries = report[0].get("files") if report else None
    if not isinstance(entries, list):
        raise SystemExit(f"{source}: no npm file list found")
    return tuple(item["path"] for item in entries if isinstance(item, dict) and item.get("path"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument(
        "--pack-list",
        help="reuse a captured `npm pack --dry-run --json` listing (use - for stdin) instead of running npm",
    )
    parser.add_argument("--timeout", type=float, default=300.0, help="seconds allowed for npm pack --dry-run")
    arguments = parser.parse_args(argv)
    packaged = _read_pack_list(arguments.pack_list) if arguments.pack_list else None
    report = audit_package_tree(Path(arguments.root), packaged_paths=packaged)
    if report["ok"]:
        count = report.get("packaged_count")
        suffix = f" ({count} files in the tarball)" if count is not None else ""
        print(f"npm tarball ships every Function module package required by the contract{suffix}.")
        return 0
    print(report["reason"] or "npm package Function source check failed.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
# HASHI-Fingerprint: hfp_17fc273bdacc453e9bad95309c5b41ca
