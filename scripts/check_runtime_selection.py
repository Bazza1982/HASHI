#!/usr/bin/env python3
"""Validate the installer-owned CR/FR pair from its qualified Core anchor."""
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
    # Independent FR replacements do not acquire a second Core selector.
    # The fixed qualified Core can manage its pair; the original co-located
    # FR remains supported only while it is the exact selected installation.
    own = (selected.core
           if Path(sys.prefix).resolve() == Path(selected.core.fingerprint.environment_prefix).resolve()
           else selected.functions)
    bind_process_runtime(own, code_root=code_root)
    compare_runtime_fingerprints(
        own.fingerprint, enforce_runtime_contract(code_root, role=own.fingerprint.role))
    return selected


def check_function_imports(code_root, expected, *, selection=None):
    from orchestrator.function_generation import (
        FUNCTION_GENERATION_ENTRYPOINTS, build_source_manifest,
        probe_candidate_payload, verify_manifest_source_commit,
    )
    from orchestrator.manager_registry import FUNCTION_MANAGER_SPECS

    manifest = build_source_manifest(
        [*FUNCTION_GENERATION_ENTRYPOINTS, *(spec.module for spec in FUNCTION_MANAGER_SPECS)],
        code_root=code_root)
    verify_manifest_source_commit(manifest, code_root=code_root)
    payload = {
        "code_root": str(code_root), "module_names": list(manifest.module_names),
        "expected_runtime": expected.to_dict(),
    }
    if selection is None:
        result = probe_candidate_payload(payload)
    else:
        # Keep Functions imports out of the dependency-free Core. Reuse the
        # existing qualified child, private channel and PID/nonce admission.
        from orchestrator.runtime_process import run_protected_call
        payload.update(
            runtime=selection.functions.fingerprint.to_dict(),
            core_runtime=selection.core.fingerprint.to_dict(),
            probe_entrypoint=json.loads(
                (code_root / "runtime-entry.json").read_text(encoding="utf-8"))["candidate_probe"],
        )
        result = run_protected_call(
            payload, purpose="candidate-probe", runtime=selection.functions)

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
        check_function_imports(args.code_root.resolve(), selected.functions.fingerprint,
                               selection=selected)
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
