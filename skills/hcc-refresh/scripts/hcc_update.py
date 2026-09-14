from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from orchestrator.hcc import inspect_hcc_entry, replace_hcc_entry


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    inspect_p = sub.add_parser("inspect")
    inspect_p.add_argument("workspace")
    inspect_p.add_argument("entry")
    replace_p = sub.add_parser("replace")
    replace_p.add_argument("workspace")
    replace_p.add_argument("entry")
    replace_p.add_argument("--file")
    replace_p.add_argument("--expected-digest")
    args = parser.parse_args()

    if args.command == "inspect":
        print(json.dumps(inspect_hcc_entry(args.workspace, args.entry), ensure_ascii=False))
        return 0

    content = Path(args.file).read_text(encoding="utf-8") if args.file else sys.stdin.read()
    digest = replace_hcc_entry(
        args.workspace,
        args.entry,
        content,
        expected_digest=args.expected_digest,
    )
    print(json.dumps({"ok": True, "digest": digest}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
