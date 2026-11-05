"""Derive tool write-denial targets from admitted runtime/installer facts.

This adds denials only. It never selects an executable, publishes a release,
changes a runtime or grants an Agent path authority.
"""
from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path

from orchestrator.runtime_contract import (
    CORE_HOST_ROLE, FUNCTIONS_ROLE, RuntimeContractError, RuntimeFingerprint,
    compare_runtime_fingerprints, current_process_runtime,
    qualified_runtime_from_reference,
)


def _core(value):
    if value is None:
        return None
    if not isinstance(value, RuntimeFingerprint) or value.role != CORE_HOST_ROLE:
        raise RuntimeContractError("Runtime protection requires an admitted Core-role receipt")
    return value


def _selector(core):
    prefix = Path(core.environment_prefix)
    if not prefix.is_absolute():
        raise RuntimeContractError("Admitted Core prefix must be absolute")
    return prefix.parent / "hashi-runtime-release.json"


def protection_context_key(core_runtime=None):
    """Cheap observation key; no instance/Agent-selected runtime path input."""
    core = _core(core_runtime)
    own = current_process_runtime()
    reference = (str(own.manifest_path), own.manifest_digest) if own else None
    stamp = None
    if core is not None:
        path = _selector(core)
        metadata = path.stat()
        stamp = (metadata.st_mtime_ns, metadata.st_size)
    return (core, reference, stamp)


def _unique_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise RuntimeContractError("Duplicate installer selection field")
        value[key] = item
    return value


def _selection(path):
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        raise RuntimeContractError("Installer selection is redirected")
    payload = path.read_bytes()
    if len(payload) > 64 * 1024:
        raise RuntimeContractError("Installer selection exceeds its size limit")
    value = json.loads(payload, object_pairs_hook=_unique_pairs)
    if (not isinstance(value, dict) or set(value) != {"schema_version", "core", "functions"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1):
        raise RuntimeContractError("Invalid installer selection for runtime protection")
    return value


def admitted_protection_targets(code_root, *, core_runtime=None):
    """Protect bound old FR as well as the installer-selected pair during handoff."""
    core, own_reference, stamp = protection_context_key(core_runtime)
    return _targets(Path(code_root).resolve(), core, own_reference, stamp)


@lru_cache(maxsize=32)
def _targets(code_root, core, own_reference, selector_stamp):
    roots, files = set(), set()
    def include(runtime):
        # The loader already verified every environment/base byte, role and
        # package identity. Read its pinned manifest for the exact sealed base.
        payload = runtime.manifest_path.read_bytes()
        import hashlib
        if "sha256:" + hashlib.sha256(payload).hexdigest() != runtime.manifest_digest:
            raise RuntimeContractError("Admitted runtime protection manifest changed")
        record = json.loads(payload, object_pairs_hook=_unique_pairs)
        roots.add(Path(runtime.fingerprint.environment_prefix))
        roots.add(Path(record["base_root"]))
        files.add(runtime.manifest_path)

    if own_reference is not None:
        own = qualified_runtime_from_reference(
            {"path": own_reference[0], "digest": own_reference[1]},
            code_root=code_root, role=FUNCTIONS_ROLE)
        include(own)
    if core is not None:
        selector = _selector(core)
        selected = _selection(selector)
        installed_core = qualified_runtime_from_reference(
            selected["core"], code_root=code_root, role=CORE_HOST_ROLE)
        compare_runtime_fingerprints(core, installed_core.fingerprint)
        installed_functions = qualified_runtime_from_reference(
            selected["functions"], code_root=code_root, role=FUNCTIONS_ROLE)
        include(installed_core)
        include(installed_functions)
        files.add(selector)
        # Include own_reference independently: an old Worker may remain alive
        # while the installer selection already names the new immutable FR.
    return tuple(sorted(roots)), tuple(sorted(files))
# HASHI-Fingerprint: hfp_810fcbbf43394c98b78228a7916002c7
