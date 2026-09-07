#!/usr/bin/env python3
"""Validate an interpreter against HASHI's authoritative Core policy."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--code-root",
        default=str(Path(__file__).resolve().parents[1]),
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--runtime-only",
        action="store_true",
        help="Check only the interpreter/ABI so a clean environment can be created.",
    )
    args = parser.parse_args(argv)
    code_root = Path(args.code_root).resolve()
    sys.path.insert(0, str(code_root))

    from orchestrator.runtime_contract import (
        RuntimeContractError,
        enforce_runtime_contract,
        load_runtime_policy,
    )

    try:
        fingerprint = enforce_runtime_contract(
            code_root,
            require_standard_dependencies=not args.runtime_only,
        )
    except RuntimeContractError as exc:
        print(f"HASHI Core runtime rejected: {exc}", file=sys.stderr)
        return 78
    if args.json:
        print(json.dumps(fingerprint.to_dict(), sort_keys=True))
    else:
        policy = load_runtime_policy(code_root)
        print(
            "HASHI Core runtime: ok · "
            f"{fingerprint.runtime_id} · ABI={fingerprint.platform_abi} · "
            f"python={policy.python_text} · policy={policy.requires_python}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
