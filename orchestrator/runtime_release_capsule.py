"""Relocatable Portable role qualification owned by the platform installer.

Public capsules contain only relative file closures and observed identities.
Installation re-observes the unchanged bytes at their final location; absolute
qualification records remain external to the immutable interpreters.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
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


def _verify_sidecar_files(root, table):
    """Seal helper resources without granting Core/Functions runtime authority."""
    root = Path(root).absolute()
    if root.resolve() != root or not root.is_dir():
        raise RuntimeContractError("Sidecar resource path is redirected or unavailable")
    if not isinstance(table, dict) or not table:
        raise RuntimeContractError("Sidecar resource closure is empty")
    for relative, checksum in table.items():
        if (not isinstance(relative, str) or not relative or "\\" in relative
                or ":" in relative or relative.startswith("/")
                or any(part in {"", ".", ".."} for part in relative.split("/"))):
            raise RuntimeContractError("Invalid sidecar resource path")
        if not isinstance(checksum, str) or re.fullmatch(r"[0-9a-f]{64}", checksum) is None:
            raise RuntimeContractError("Invalid sidecar resource hash")
    observed = {}
    pending = [(root, "")]
    while pending:
        directory, prefix = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                metadata = entry.stat(follow_symlinks=False)
                if os.name == "nt" and metadata.st_ino == 0:
                    metadata = os.stat(entry.path, follow_symlinks=False)
                if (stat.S_ISLNK(metadata.st_mode)
                        or getattr(metadata, "st_file_attributes", 0) & 0x400):
                    raise RuntimeContractError("Sidecar resource path is redirected")
                relative = prefix + entry.name
                if stat.S_ISDIR(metadata.st_mode):
                    pending.append((Path(entry.path), relative + "/"))
                elif stat.S_ISREG(metadata.st_mode):
                    observed[relative] = (Path(entry.path), metadata)
                else:
                    raise RuntimeContractError("Sidecar resource contains a non-regular entry")
    if set(observed) != set(table):
        raise RuntimeContractError("Sidecar resource file closure changed")

    def identity(value):
        return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns

    for relative, (file, expected) in observed.items():
        digest = hashlib.sha256()
        with file.open("rb") as stream:
            before = os.fstat(stream.fileno())
            if identity(before) != identity(expected):
                raise RuntimeContractError("Sidecar resource changed during verification")
            while block := stream.read(1024 * 1024):
                digest.update(block)
            if identity(os.fstat(stream.fileno())) != identity(before):
                raise RuntimeContractError("Sidecar resource changed during verification")
        if digest.hexdigest() != table[relative]:
            raise RuntimeContractError(f"Sidecar resource hash mismatch: {relative}")


PAYLOAD_ROOTS = ("runtime/functions", "runtime/media", "runtime/bin")
MAX_PAYLOAD_BYTES = 4 * 1024 * 1024 * 1024


def _payload_files(value, relative):
    if relative == "runtime/functions":
        return value["roles"]["functions"]["files"]
    if relative in {"runtime/media", "runtime/bin"} and relative in value["sidecars"]:
        return value["sidecars"][relative]
    raise RuntimeContractError("Unexpected release payload root")


def _payload_member(name):
    if (not isinstance(name, str) or not name or "\\" in name or ":" in name
            or "\0" in name):
        raise RuntimeContractError("Unsafe release payload path")
    path = PurePosixPath(name)
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                *(f"LPT{i}" for i in range(1, 10))}
    if (path.is_absolute() or path.as_posix() != name
            or any(part in {"", ".", ".."} or part.endswith((".", " "))
                   or part.split(".", 1)[0].upper() in reserved for part in path.parts)):
        raise RuntimeContractError("Unsafe release payload path")
    return path


def _payload_infos(archive, table, record):
    infos = archive.infolist()
    names = [info.filename for info in infos]
    if len(names) != len(set(names)) or set(names) != set(table):
        raise RuntimeContractError("Release payload file closure changed")
    total = 0
    for info in infos:
        _payload_member(info.filename)
        mode = info.external_attr >> 16
        if (info.is_dir() or info.flag_bits & 1 or stat.S_ISLNK(mode)
                or (stat.S_IFMT(mode) not in {0, stat.S_IFREG})
                or info.external_attr & 0x400):
            raise RuntimeContractError("Release payload contains a redirected or non-regular member")
        total += info.file_size
    if total != record["uncompressed_bytes"] or total > MAX_PAYLOAD_BYTES:
        raise RuntimeContractError("Release payload declared size differs")
    return infos


def _verified_payload(image_root, relative, record, table):
    if (not isinstance(record, dict)
            or set(record) != {"archive", "sha256", "file_count", "uncompressed_bytes"}
            or record["archive"] != relative + ".payload.zip"
            or not isinstance(record["sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", record["sha256"]) is None
            or type(record["file_count"]) is not int or not 0 < record["file_count"] <= 100000
            or type(record["uncompressed_bytes"]) is not int
            or not 0 <= record["uncompressed_bytes"] <= MAX_PAYLOAD_BYTES
            or not isinstance(table, dict) or len(table) != record["file_count"]):
        raise RuntimeContractError("Release payload metadata is invalid")
    for name, checksum in table.items():
        _payload_member(name)
        if not isinstance(checksum, str) or re.fullmatch(r"[0-9a-f]{64}", checksum) is None:
            raise RuntimeContractError("Release payload file checksum is invalid")
        if relative == "runtime/functions" and (
            name.lower().endswith(".pth") or Path(name).name.lower() in {
                "sitecustomize.py", "sitecustomize.pyc", "usercustomize.py", "usercustomize.pyc"}):
            raise RuntimeContractError("Functions payload may not inject startup hooks")
    path = image_root / record["archive"]
    if path.resolve() != path or not path.is_file():
        raise RuntimeContractError("Release payload archive is redirected or missing")
    with path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != record["sha256"]:
            raise RuntimeContractError("Release payload archive checksum differs")
    with zipfile.ZipFile(path) as archive:
        for info in _payload_infos(archive, table, record):
            with archive.open(info) as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != table[info.filename]:
                    raise RuntimeContractError("Release payload content checksum differs")


def _verify_payload_root(image_root, relative, table, capsule_path):
    root = image_root / relative
    if root.resolve() != root:
        raise RuntimeContractError("Release payload destination is redirected")
    if relative == "runtime/functions":
        verify_runtime_files(root, table, manifest_path=capsule_path)
    else:
        _verify_sidecar_files(root, table)


def payload_cache_key(relative, table):
    payload = json.dumps([relative, table], sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest() + ".zip"


def pack_release_payloads(image_root, code_root, *, roots=PAYLOAD_ROOTS, cache_root=None):
    """Losslessly pack fresh verified files; never pack an installed runtime."""
    image_root = Path(image_root).absolute()
    if ((image_root / "runtime/hashi-runtime-release.json").exists()
            or (image_root / "runtime-qualifications").exists()
            or (image_root / ".hashi-local-install.json").exists()):
        raise RuntimeContractError("An installed runtime cannot be repacked")
    value = verify_capsule(image_root, code_root)
    if value["schema_version"] != 1 or not roots or len(set(roots)) != len(roots):
        raise RuntimeContractError("Only a fresh unpacked capsule can be packed")
    if set(roots) - set(PAYLOAD_ROOTS):
        raise RuntimeContractError("Unexpected release payload root")
    payloads = {}
    created = []
    try:
        for relative in roots:
            table = _payload_files(value, relative)
            target = image_root / (relative + ".payload.zip")
            cached = Path(cache_root) / payload_cache_key(relative, table) if cache_root else None
            if cached is not None and cached.is_file():
                with cached.open("rb") as source, target.open("xb") as output:
                    created.append(target)
                    shutil.copyfileobj(source, output)
            else:
                with zipfile.ZipFile(target, "x", compression=zipfile.ZIP_LZMA) as archive:
                    created.append(target)
                    for name in sorted(table):
                        archive.write(image_root / relative / name, name)
            with target.open("rb") as stream:
                checksum = hashlib.file_digest(stream, "sha256").hexdigest()
            with zipfile.ZipFile(target) as archive:
                total = sum(info.file_size for info in archive.infolist())
            record = {"archive": relative + ".payload.zip", "sha256": checksum,
                      "file_count": len(table), "uncompressed_bytes": total}
            _verified_payload(image_root, relative, record, table)
            _verify_payload_root(image_root, relative, table, image_root / CAPSULE_PATH)
            payloads[relative] = record
            if cached is not None and not cached.exists():
                cached.parent.mkdir(parents=True, exist_ok=True)
                temporary_cache = cached.with_name("." + cached.name + "." + uuid4().hex)
                try:
                    with target.open("rb") as source, temporary_cache.open("xb") as output:
                        shutil.copyfileobj(source, output)
                    try:
                        os.link(temporary_cache, cached)
                    except FileExistsError:
                        pass
                finally:
                    temporary_cache.unlink(missing_ok=True)
        value["schema_version"] = 2
        value["payloads"] = payloads
        capsule = image_root / CAPSULE_PATH
        temporary = capsule.with_name("." + capsule.name + "." + uuid4().hex)
        temporary.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        os.replace(temporary, capsule)
        for relative in roots:
            _verify_payload_root(image_root, relative, _payload_files(value, relative), capsule)
            shutil.rmtree(image_root / relative)
        verify_capsule(image_root, code_root)
        return value
    except BaseException:
        # Before publication only these newly created archives belong to us.
        if value.get("schema_version") == 1:
            for path in created:
                path.unlink(missing_ok=True)
        raise


def materialize_release_payloads(image_root, code_root):
    """Expand verified bytes only into fresh runtime roots before qualification."""
    image_root = Path(image_root).absolute()
    if ((image_root / "runtime/hashi-runtime-release.json").exists()
            or (image_root / "runtime-qualifications").exists()):
        raise RuntimeContractError("Qualified runtime trees must not be expanded or overwritten")
    value = verify_capsule(image_root, code_root)
    for relative, record in value.get("payloads", {}).items():
        table = _payload_files(value, relative)
        target = image_root / relative
        if target.exists():
            _verify_payload_root(image_root, relative, table, image_root / CAPSULE_PATH)
            continue
        temporary = Path(tempfile.mkdtemp(prefix="." + target.name + ".expanding-", dir=target.parent))
        try:
            with zipfile.ZipFile(image_root / record["archive"]) as archive:
                for info in _payload_infos(archive, table, record):
                    destination = temporary.joinpath(*_payload_member(info.filename).parts)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info) as source, destination.open("xb") as output:
                        digest = hashlib.sha256()
                        while block := source.read(1024 * 1024):
                            output.write(block)
                            digest.update(block)
                    if digest.hexdigest() != table[info.filename]:
                        raise RuntimeContractError("Expanded release payload checksum differs")
                    if os.name != "nt":
                        destination.chmod((info.external_attr >> 16) & 0o777)
            if relative == "runtime/functions":
                verify_runtime_files(temporary, table, manifest_path=image_root / CAPSULE_PATH)
            else:
                _verify_sidecar_files(temporary, table)
            os.rename(temporary, target)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    verify_capsule(image_root, code_root)
    return value


def _require_expanded_payloads(image_root, value):
    for relative in value.get("payloads", {}):
        if not (image_root / relative).is_dir():
            raise RuntimeContractError("Release payloads must be fully expanded before qualification")


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
    fields = {"schema_version", "core_source_digest", "roles", "function_manifest", "function_source", "sidecars"}
    version = value.get("schema_version") if isinstance(value, dict) else None
    if version == 2:
        fields.add("payloads")
    if (not isinstance(value, dict) or set(value) != fields
            or type(version) is not int or version not in {1, 2}
            or not isinstance(value["roles"], dict) or set(value["roles"]) != set(ROLE_ROOTS)):
        raise RuntimeContractError("Release capsule schema mismatch")
    payloads = value.get("payloads", {})
    if (not isinstance(payloads, dict) or (version == 2 and not payloads)
            or set(payloads) - set(PAYLOAD_ROOTS)):
        raise RuntimeContractError("Release capsule payload schema mismatch")
    for relative, record in payloads.items():
        _verified_payload(image_root, relative, record, _payload_files(value, relative))
    if value["core_source_digest"] != core_source_digest(code_root):
        raise RuntimeContractError("Release capsule protected source mismatch")
    for name, (relative, role) in ROLE_ROOTS.items():
        record = value["roles"][name]
        if (set(record) != {"root", "runtime_id", "fingerprint", "files"}
                or record["root"] != relative or record["fingerprint"]["role"] != role):
            raise RuntimeContractError("Release capsule role/root mismatch")
        if any(key in record["fingerprint"] for key in ("environment_prefix", "executable", "installation_digest")):
            raise RuntimeContractError("Public capsule contains host-specific runtime authority")
        root = image_root / relative
        if root.resolve() != root:
            raise RuntimeContractError("Release capsule runtime root is redirected")
        if root.exists() or relative not in payloads:
            verify_runtime_files(root, record["files"], manifest_path=path)
    if set(value["sidecars"]) - {"runtime/bin", "runtime/media"}:
        raise RuntimeContractError("Release capsule sidecar root mismatch")
    for relative, files in value["sidecars"].items():
        root = image_root / relative
        if root.resolve() != root:
            raise RuntimeContractError("Release capsule sidecar root is redirected")
        if root.exists() or relative not in payloads:
            _verify_sidecar_files(root, files)
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
    _require_expanded_payloads(image_root, value)
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
    _require_expanded_payloads(image_root, value)
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
