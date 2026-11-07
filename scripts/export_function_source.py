#!/usr/bin/env python3
"""Export committed Function publication evidence without runtime adoption."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from orchestrator.function_source_publication import capture_git_source  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    source = capture_git_source(ROOT)
    args.output.write_text(json.dumps(source, sort_keys=True), encoding="utf-8")
    print(source["source_commit"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
# HASHI-Fingerprint: hfp_b5190af9f1294332a48908696b398117
