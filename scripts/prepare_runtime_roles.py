#!/usr/bin/env python3
"""Materialize a fresh standard role pair from an explicitly installed wheel seed."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from orchestrator.runtime_contract import load_runtime_policy, locked_standard_dependencies  # noqa: E402
from orchestrator.runtime_installation_builder import (  # noqa: E402
    _metadata, build_reference_roles, snapshot_reference,
)
from scripts.compile_functions_locks import pinned_versions  # noqa: E402


def prepare_roles(site, output, reference, *, base_root=None, extra_lock=None):
    site, output, reference = Path(site).resolve(), Path(output).resolve(), Path(reference).resolve()
    installed = _metadata(site)
    standard = locked_standard_dependencies(ROOT, load_runtime_policy(ROOT))
    if any(installed.get(name) != value for name, value in standard.items()):
        raise ValueError("Fresh seed does not contain the exact canonical Functions packages")
    if extra_lock is None:
        if installed != standard:
            raise ValueError("Standard role seed contains undeclared additional packages")
    else:
        text = Path(extra_lock).read_text(encoding="utf-8")
        frozen = pinned_versions(text)
        if "--hash=sha256:" not in text or any(frozen.get(name) != value for name, value in installed.items()):
            raise ValueError("Additional Functions packages lack the exact hashed build lock")
    digest = "sha256:" + hashlib.sha256(
        "\n".join(f"{k}=={v}" for k, v in sorted(installed.items())).encode()).hexdigest()
    pin = snapshot_reference(
        ROOT, base_root=Path(base_root or sys.base_prefix), functions_site=site,
        destination=reference, expected_dependency_digest=digest)
    result = build_reference_roles(ROOT, reference, expected_digest=pin, destination=output,
                                   functions_wheel_lock=extra_lock)
    from orchestrator.function_source_publication import publish_source
    publish_source(ROOT, Path(result["selection"]).parent)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--functions-site", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--base-root", type=Path)
    parser.add_argument("--extra-lock", type=Path)
    args = parser.parse_args(argv)
    print(json.dumps(prepare_roles(
        args.functions_site, args.output, args.reference,
        base_root=args.base_root, extra_lock=args.extra_lock), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
