"""Build a committed permission-runtime artifact; never install or start it."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from orchestrator.permissions.capsule import prepare_capsule


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Committed qualified HASHI checkout")
    parser.add_argument("--runtime", type=Path, required=True, help="Matching base Windows Python distribution")
    parser.add_argument("--site-packages", type=Path, required=True, help="Already qualified dependency directory")
    parser.add_argument("--output", type=Path, required=True, help="New artifact directory; never overwritten")
    args = parser.parse_args(argv)
    result = prepare_capsule(
        args.source.resolve(), args.output.resolve(),
        runtime_root=args.runtime.resolve(), site_packages=args.site_packages.resolve(),
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
