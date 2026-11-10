"""Derive tool write-denial targets from admitted runtime/installer facts.

This adds denials only. It never selects an executable, publishes a release,
changes a runtime or grants an Agent path authority.
"""
from __future__ import annotations

from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path

from orchestrator.runtime_contract import (
    CORE_HOST_ROLE, FUNCTIONS_ROLE, RuntimeContractError, RuntimeFingerprint,
    compare_runtime_fingerprints, current_process_runtime,
    qualified_runtime_from_reference,
)


_MAX_SELECTOR_BYTES = 64 * 1024


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


def _selector_stamp(path: Path):
    """Return a bounded content observation for denial-cache invalidation."""
    try:
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            return ("redirected",)
        with path.open("rb") as stream:
            payload = stream.read(_MAX_SELECTOR_BYTES + 1)
            opened = os.fstat(stream.fileno())
        current = path.stat()
    except OSError:
        # Observation failure does not discard an admitted boundary.
        return ("unavailable",)
    opened_identity = (
        opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns
    )
    current_identity = (
        current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns
    )
    if opened_identity != current_identity:
        return ("unstable",)
    content = (
        "oversize"
        if len(payload) > _MAX_SELECTOR_BYTES
        else "sha256:" + hashlib.sha256(payload).hexdigest()
    )
    return (current.st_mtime_ns, current.st_size, content)


def protection_context_key(core_runtime=None):
    """Bounded observation key; no instance/Agent-selected runtime path input."""
    core = _core(core_runtime)
    own = current_process_runtime()
    reference = (str(own.manifest_path), own.manifest_digest) if own else None
    stamp = None
    if core is not None:
        stamp = _selector_stamp(_selector(core))
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
    with path.open("rb") as stream:
        payload = stream.read(_MAX_SELECTOR_BYTES + 1)
    if len(payload) > _MAX_SELECTOR_BYTES:
        raise RuntimeContractError("Installer selection exceeds its size limit")
    value = json.loads(payload, object_pairs_hook=_unique_pairs)
    if (not isinstance(value, dict) or set(value) != {"schema_version", "core", "functions"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1):
        raise RuntimeContractError("Invalid installer selection for runtime protection")
    return value


def bound_protection_targets(code_root, *, core_runtime=None):
    """Recover denial targets from the private, already-admitted process binding.

    This never qualifies an installation or authorizes a new process. A manifest
    must still match its admission digest before its base path is trusted.
    """
    import hashlib
    core = _core(core_runtime)
    own = current_process_runtime()
    roots, files = set(), set()
    if core is not None:
        roots.add(Path(core.environment_prefix))
        files.add(_selector(core))
    if own is not None:
        payload = own.manifest_path.read_bytes()
        if "sha256:" + hashlib.sha256(payload).hexdigest() != own.manifest_digest:
            raise RuntimeContractError("Bound runtime protection manifest changed")
        record = json.loads(payload, object_pairs_hook=_unique_pairs)
        roots.update((Path(own.fingerprint.environment_prefix), Path(record["base_root"])))
        files.update((own.manifest_path,
                      Path(own.fingerprint.environment_prefix).parent / "hashi-function-publication.json",
                      Path(code_root) / "FUNCTION_SOURCE.json"))
    # A Core fingerprint alone does not pin its external manifest or base.
    # Partial targets are useful denials, never a complete execution boundary.
    return tuple(sorted(roots)), tuple(sorted(files)), False


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

        files.add(Path(runtime.fingerprint.environment_prefix).parent / "hashi-function-publication.json")
        files.add(code_root / "FUNCTION_SOURCE.json")
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
