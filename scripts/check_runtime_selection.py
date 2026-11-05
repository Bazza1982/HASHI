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


def check_function_imports(code_root, expected):
    from orchestrator.function_generation import (
        FUNCTION_GENERATION_ENTRYPOINTS, build_source_manifest,
        probe_candidate_payload, verify_manifest_source_commit,
    )
    from orchestrator.manager_registry import FUNCTION_MANAGER_SPECS

    manifest = build_source_manifest(
        [*FUNCTION_GENERATION_ENTRYPOINTS, *(spec.module for spec in FUNCTION_MANAGER_SPECS)],
        code_root=code_root)
    verify_manifest_source_commit(manifest, code_root=code_root)
    result = probe_candidate_payload({
        "code_root": str(code_root), "module_names": list(manifest.module_names),
        "expected_runtime": expected.to_dict(),
    })
    if not result.get("ok"):
        from orchestrator.runtime_contract import RuntimeContractError
        raise RuntimeContractError(
            "Installed Functions import qualification failed: " + str(result.get("error")))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code-root", type=Path, default=ROOT)
    parser.add_argument("--core-command", action="store_true")
    parser.add_argument("--import-functions", action="store_true")
    args = parser.parse_args(argv)
    selected = checked_selection(args.code_root)
    if args.import_functions:
        check_function_imports(args.code_root.resolve(), selected.functions.fingerprint)
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
