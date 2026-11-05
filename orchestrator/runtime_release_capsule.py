"""Relocatable Portable role qualification owned by the platform installer.

Public capsules contain only relative file closures and observed identities.
Installation re-observes the unchanged bytes at their final location; absolute
qualification records remain external to the immutable interpreters.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

from orchestrator.runtime_contract import (
    CORE_HOST_ROLE, FUNCTIONS_ROLE, RuntimeContractError,
    compare_runtime_boundary, core_source_digest, qualified_runtime_from_reference,
    runtime_installation_reference, verify_runtime_files,
)
from orchestrator.runtime_installation_builder import (
    _write_profile, file_table, qualify_environment,
)

ROLE_ROOTS = {"core": ("runtime/core", CORE_HOST_ROLE),
              "functions": ("runtime/functions", FUNCTIONS_ROLE)}
CAPSULE_PATH = "runtime/role-capsule.json"


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeContractError("Duplicate release capsule member")
        result[key] = value
    return result


def _public_fingerprint(runtime):
    return {name: value for name, value in runtime.to_dict().items()
            if name not in {"environment_prefix", "executable", "installation_digest"}}


def create_capsule(image_root, code_root, qualification_root, *, function_manifest,
                   function_source=None, sidecar_roots=()):
    image_root, code_root = Path(image_root).absolute(), Path(code_root).resolve()
    qualification_root = Path(qualification_root).resolve()
    if qualification_root.is_relative_to(image_root) or qualification_root.exists():
        raise ValueError("Build qualifications must be new and outside the public image")
    target = image_root / CAPSULE_PATH
    if target.exists():
        raise ValueError("Release capsule already exists")
    qualification_root.mkdir(parents=True)
    roles = {}
    observed = {}
    for name, (relative, role) in ROLE_ROOTS.items():
        environment = image_root / relative
        _write_profile(code_root, environment, role)
        qualified = qualify_environment(
            code_root, environment, environment, qualification_root / (name + ".json"),
            role, standalone=True)
        observed[name] = qualified
        roles[name] = {"root": relative, "runtime_id": qualified.runtime_id,
                       "fingerprint": _public_fingerprint(qualified.fingerprint),
                       "files": file_table(environment)}
    core, functions = observed["core"], observed["functions"]
    compare_runtime_boundary(core.fingerprint, functions.fingerprint,
                             expected_functions=functions.fingerprint)
    sidecars = {}
    for relative in sidecar_roots:
        if relative not in {"runtime/bin", "runtime/media"}:
            raise ValueError("Unexpected sidecar authority root")
        sidecars[relative] = file_table(image_root / relative)
    value = {"schema_version": 1, "core_source_digest": core_source_digest(code_root),
             "roles": roles, "function_manifest": function_manifest,
             "function_source": function_source, "sidecars": sidecars}
    target.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    verify_capsule(image_root, code_root)
    return value


def verify_capsule(image_root, code_root):
    image_root, code_root = Path(image_root).absolute(), Path(code_root).resolve()
    if image_root.resolve() != image_root:
        raise RuntimeContractError("Portable installation root is redirected")
    path = image_root / CAPSULE_PATH
    if path.is_symlink() or path.stat().st_size > 32 * 1024 * 1024:
        raise RuntimeContractError("Release capsule is redirected or oversized")
    value = json.loads(path.read_bytes(), object_pairs_hook=_unique)
    if (set(value) != {"schema_version", "core_source_digest", "roles", "function_manifest", "function_source", "sidecars"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or set(value["roles"]) != set(ROLE_ROOTS)):
        raise RuntimeContractError("Release capsule schema mismatch")
    if value["core_source_digest"] != core_source_digest(code_root):
        raise RuntimeContractError("Release capsule protected source mismatch")
    for name, (relative, role) in ROLE_ROOTS.items():
        record = value["roles"][name]
        if (set(record) != {"root", "runtime_id", "fingerprint", "files"}
                or record["root"] != relative or record["fingerprint"]["role"] != role):
            raise RuntimeContractError("Release capsule role/root mismatch")
        if any(key in record["fingerprint"] for key in ("environment_prefix", "executable", "installation_digest")):
            raise RuntimeContractError("Public capsule contains host-specific runtime authority")
        verify_runtime_files(image_root / relative, record["files"], manifest_path=path)
    if set(value["sidecars"]) - {"runtime/bin", "runtime/media"}:
        raise RuntimeContractError("Release capsule sidecar root mismatch")
    for relative, files in value["sidecars"].items():
        verify_runtime_files(image_root / relative, files, manifest_path=path)
    from orchestrator.function_generation import SourceManifest, verify_source_manifest
    manifest = SourceManifest.from_mapping(value["function_manifest"])
    verify_source_manifest(manifest, code_root=code_root)
    from orchestrator.function_source_publication import validate_snapshot
    source = value["function_source"]
    if source is None:
        if (code_root / "orchestrator/runtime_app.py").is_file():
            raise RuntimeContractError("A complete Portable product requires committed source publication")
    else:
        validate_snapshot(source, code_root, required_paths=[
            entry.relative_path for entry in (*manifest.entries, *manifest.assets)])
    return value


def qualify_relocated_roles(image_root, code_root):
    image_root, code_root = Path(image_root).absolute(), Path(code_root).resolve()
    value = verify_capsule(image_root, code_root)
    selector = image_root / "runtime/hashi-runtime-release.json"
    if selector.exists() or selector.is_symlink():
        raise ValueError("An installed selector already exists; verify it instead of overwriting")
    qualification_root = image_root / "runtime-qualifications" / uuid4().hex
    selection = {"schema_version": 1}
    observed = {}
    for name, (relative, role) in ROLE_ROOTS.items():
        environment = image_root / relative
        qualified = qualify_environment(
            code_root, environment, environment, qualification_root / (name + ".json"),
            role, standalone=True)
        expected = value["roles"][name]
        if (qualified.runtime_id != expected["runtime_id"]
                or _public_fingerprint(qualified.fingerprint) != expected["fingerprint"]):
            raise RuntimeContractError("Relocated runtime differs from built role qualification")
        selection[name] = runtime_installation_reference(qualified)
        observed[name] = qualified
    compare_runtime_boundary(observed["core"].fingerprint, observed["functions"].fingerprint,
                             expected_functions=observed["functions"].fingerprint)
    if value["function_source"] is not None:
        from orchestrator.function_source_publication import publish_source
        publish_source(code_root, image_root / "runtime", value["function_source"])
    temporary = selector.with_name(".hashi-runtime-release." + uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(selection, sort_keys=True), encoding="utf-8")
    try:
        # Fresh installation only: an appearing selector must never be replaced.
        os.link(temporary, selector)
    finally:
        temporary.unlink(missing_ok=True)
    return selection


def verify_installed_roles(image_root, code_root):
    image_root, code_root = Path(image_root).absolute(), Path(code_root).resolve()
    value = verify_capsule(image_root, code_root)
    path = image_root / "runtime/hashi-runtime-release.json"
    selection = json.loads(path.read_bytes(), object_pairs_hook=_unique)
    if set(selection) != {"schema_version", "core", "functions"} or selection["schema_version"] != 1:
        raise RuntimeContractError("Installed selector schema mismatch")
    for name, (relative, role) in ROLE_ROOTS.items():
        qualified = qualified_runtime_from_reference(selection[name], code_root=code_root, role=role)
        if (Path(qualified.fingerprint.environment_prefix).resolve() != image_root / relative
                or qualified.runtime_id != value["roles"][name]["runtime_id"]):
            raise RuntimeContractError("Installed role/capsule identity mismatch")
    return selection
# HASHI-Fingerprint: hfp_241b7feedb9a4a4586f635ffa3d6f4d0
