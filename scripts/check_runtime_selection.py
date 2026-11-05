#!/usr/bin/env python3
"""Validate the installer-owned CR/FR pair anchored to this management FR."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from orchestrator.runtime_contract import (  # noqa: E402
    bind_process_runtime, compare_runtime_fingerprints, enforce_runtime_contract,
    load_core_runtime_selection,
)


def checked_selection(code_root):
    code_root = Path(code_root).resolve()
    selected = load_core_runtime_selection(code_root)
    # A bootstrap/system/Agent-supplied Python cannot impersonate the selected FR.
    bind_process_runtime(selected.functions, code_root=code_root)
    compare_runtime_fingerprints(
        selected.functions.fingerprint, enforce_runtime_contract(code_root))
    return selected


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code-root", type=Path, default=ROOT)
    parser.add_argument("--core-command", action="store_true")
    args = parser.parse_args(argv)
    selected = checked_selection(args.code_root)
    result = ([selected.core.fingerprint.executable, "-I", "-B"]
              if args.core_command else {
                  "core": selected.core.fingerprint.to_dict(),
                  "functions": selected.functions.fingerprint.to_dict(),
                  "CR_ID": selected.core.runtime_id, "FR_ID": selected.functions.runtime_id})
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
# HASHI-Fingerprint: hfp_4f8ff4087269435f9f2ef3b6de88e4e1
