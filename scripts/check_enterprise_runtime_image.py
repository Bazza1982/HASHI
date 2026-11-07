#!/usr/bin/env python3
"""Image-local strict role/publication probe; no account, service or provider use."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from orchestrator.runtime_contract import (  # noqa: E402
    CORE_HOST_ROLE, bind_process_runtime, enforce_runtime_contract,
    load_core_runtime_selection,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=("core-host", "functions"), required=True)
    parser.add_argument("--require-module", choices=("kubernetes",))
    args = parser.parse_args(argv)
    selected = load_core_runtime_selection(ROOT)
    installation = selected.core if args.role == CORE_HOST_ROLE else selected.functions
    bind_process_runtime(installation, code_root=ROOT)
    observed = enforce_runtime_contract(ROOT, role=args.role)
    if args.role == CORE_HOST_ROLE:
        assert observed.dependency_digest == "sha256:" + __import__("hashlib").sha256(b"").hexdigest()
        assert "aiohttp" not in sys.modules
    else:
        from scripts.check_runtime_selection import check_function_imports
        check_function_imports(ROOT, observed)
        if args.require_module:
            __import__(args.require_module)
        from orchestrator.function_generation import (
            FUNCTION_GENERATION_ENTRYPOINTS, build_source_manifest, verify_manifest_source_commit)
        from orchestrator.manager_registry import FUNCTION_MANAGER_SPECS
        manifest = build_source_manifest(
            [*FUNCTION_GENERATION_ENTRYPOINTS, *(spec.module for spec in FUNCTION_MANAGER_SPECS)],
            code_root=ROOT)
        commit = verify_manifest_source_commit(manifest, code_root=ROOT)
        assert commit
    print(json.dumps({"pid": os.getpid(), "role": args.role,
                      "runtime_id": installation.runtime_id,
                      "core_source_digest": observed.core_source_digest,
                      "services_started": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
# HASHI-Fingerprint: hfp_a3f30396fc864a08847f0a9209e7197e
