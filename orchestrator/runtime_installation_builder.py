"""Platform installer for fresh, separately sealed Core and Functions roles.

This module is never imported by Core. The reference migration reads an exact
captured CPython tree and the existing approved Function packages; it never
runs pip, updates a source environment, selects an active release or installs a
service. Publication/adoption remains an explicit operator action.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from orchestrator.runtime_contract import (
    CORE_HOST_ROLE, FUNCTIONS_ROLE, load_runtime_policy, validate_standard_dependencies,
    load_qualified_runtime, verify_runtime_files, RuntimeContractError,
)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def file_table(root):
    root = Path(root).resolve()
    return {path.relative_to(root).as_posix(): sha256(path)
            for path in sorted(root.rglob("*")) if path.is_file()}


def _metadata(site):
    result = {}
    for item in importlib.metadata.distributions(path=[str(site)]):
        name = re.sub(r"[-_.]+", "-", str(item.metadata["Name"])).lower()
        if not name or name in result:
            raise ValueError("Ambiguous reference distribution metadata")
        result[name] = str(item.version)
    return result


def snapshot_reference(code_root, *, base_root, functions_site, destination,
                       expected_dependency_digest):
    """Capture only explicitly named interpreter/library roots, never user state."""
    code_root, base_root = Path(code_root).resolve(), Path(base_root).resolve()
    functions_site, destination = Path(functions_site).resolve(), Path(destination)
    if destination.exists():
        raise ValueError("Reference snapshot destination already exists")
    destination = destination.resolve()
    if any(destination.is_relative_to(root) for root in (base_root, functions_site)):
        raise ValueError("Reference record must be outside its source runtimes")
    for parent in destination.parents:
        if ((parent / "pyvenv.cfg").is_file()
                or ((parent / "python.exe").is_file() and (parent / "Lib").is_dir())):
            raise ValueError("Reference record cannot modify an existing Python environment")
    packages = _metadata(functions_site)
    digest = "sha256:" + hashlib.sha256(
        "\n".join(f"{k}=={v}" for k, v in sorted(packages.items())).encode()).hexdigest()
    if digest != expected_dependency_digest:
        raise ValueError("Reference dependency metadata does not match the approved baseline")
    validate_standard_dependencies(code_root, load_runtime_policy(code_root), installed=packages)
    value = {"schema_version": 1, "base_root": str(base_root),
             "base_files": file_table(base_root), "functions_site": str(functions_site),
             "functions_files": file_table(functions_site),
             "dependency_digest": digest, "distributions": packages}
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    return "sha256:" + sha256(destination)


def _write_profile(code_root, environment, role):
    policy = load_runtime_policy(code_root, role=role)
    profile = Path(environment) / "runtime-policy"
    profile.mkdir()
    values = {"implementation": policy.implementation, "python": policy.python_text,
              "standard-lock": policy.standard_lock, "portable-build-date": policy.portable_build_date,
              "core-api": policy.core_api, "function-api": policy.function_api,
              "worker-model": policy.worker_model, "worker-protocol": policy.worker_protocol,
              "generation-schema": policy.generation_schema}
    if role == CORE_HOST_ROLE:
        values["core-host-lock"] = policy.standard_lock
    text = "[tool.hashi.runtime]\n" + "\n".join(
        f"{key} = {json.dumps(value)}" for key, value in values.items()) + "\n"
    (profile / "pyproject.toml").write_text(text, encoding="utf-8")
    lock = profile / policy.standard_lock
    lock.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(code_root) / policy.standard_lock, lock)


def observe_fingerprint(code_root, executable, role):
    # Code/path arguments carry no credentials or prompts. Only the verified
    # newly built interpreter executes; -I/-B prevents ambient paths and writes.
    script = (
        "import json,sys;from pathlib import Path;"
        "root=Path(sys.argv[1]).resolve();sys.path.insert(0,str(root));"
        "from orchestrator.runtime_contract import enforce_runtime_contract;"
        "print(json.dumps(enforce_runtime_contract(root,role=sys.argv[2]).to_dict()))")
    result = subprocess.run([str(executable), "-I", "-B", "-c", script, str(code_root), role],
                            capture_output=True, text=True, encoding="utf-8",
                            timeout=60, check=False)
    if result.returncode != 0:
        raise RuntimeContractError(
            f"New {role} interpreter rejected observation (exit {result.returncode}): "
            + result.stderr[-4000:]
        )
    return json.loads(result.stdout)


def qualify_environment(code_root, environment, base_root, manifest_path, role):
    """Write a new external qualification record; never mutate sealed role files."""
    environment, base_root = Path(environment).resolve(), Path(base_root).resolve()
    manifest_path = Path(manifest_path).resolve()
    if manifest_path.exists() or manifest_path.is_relative_to(environment) or manifest_path.is_relative_to(base_root):
        raise ValueError("Qualification must be new and outside immutable runtime files")
    executable = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    runtime = observe_fingerprint(code_root, executable, role)
    value = {"schema_version": 1, "role": role, "runtime": runtime,
             "base_root": str(base_root), "base_files": file_table(base_root),
             "environment_files": file_table(environment)}
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    digest = "sha256:" + sha256(manifest_path)
    qualified = load_qualified_runtime(manifest_path, expected_digest=digest, code_root=Path(code_root), role=role)
    return qualified


def _copy_runtime_tree(source, destination, *, base=False):
    source, destination = Path(source), Path(destination)
    def ignore(directory, names):
        excluded = {name for name in names if name == "__pycache__"}
        if base and Path(directory) == source / ("Lib" if os.name == "nt" else "lib/python3.12"):
            excluded.add("site-packages")
        if not base:
            # uv's environment bootstrap is not an application dependency.
            excluded.update(name for name in names if name in {"_virtualenv.py", "_virtualenv.pth"})
        return excluded
    shutil.copytree(source, destination, ignore=ignore)
    for file in destination.rglob("*"):
        if file.is_symlink() or getattr(file, "is_junction", lambda: False)():
            raise ValueError("Copied runtime contains a redirected path")
        if file.suffix.lower() == ".pth" or file.name.lower() in {"sitecustomize.py", "usercustomize.py"}:
            raise ValueError("Unexpected reference startup injection")
    return destination


def build_reference_roles(code_root, reference_path, *, expected_digest, destination):
    """Materialize a fresh pair with original exact packages and independent seals."""
    code_root, reference_path = Path(code_root).resolve(), Path(reference_path).resolve()
    destination = Path(destination).resolve()
    if destination.exists():
        raise ValueError("Runtime destination already exists; never modify an existing environment")
    for parent in destination.parents:
        if ((parent / "pyvenv.cfg").is_file()
                or ((parent / "python.exe").is_file() and (parent / "Lib").is_dir())):
            raise ValueError("Runtime destination cannot modify an existing Python environment")
    if "sha256:" + sha256(reference_path) != expected_digest:
        raise ValueError("Reference snapshot digest mismatch")
    reference = json.loads(reference_path.read_bytes())
    if set(reference) != {"schema_version", "base_root", "base_files", "functions_site",
                          "functions_files", "dependency_digest", "distributions"} or type(reference["schema_version"]) is not int or reference["schema_version"] != 1:
        raise ValueError("Reference snapshot schema mismatch")
    base_source = Path(reference["base_root"])
    functions_source = Path(reference["functions_site"])
    if not base_source.is_absolute() or not functions_source.is_absolute():
        raise ValueError("Reference paths must be absolute")
    if any(destination.is_relative_to(p) or p.is_relative_to(destination) for p in (base_source, functions_source)):
        raise ValueError("Destination overlaps a reference installation")
    verify_runtime_files(base_source, reference["base_files"], manifest_path=reference_path)
    verify_runtime_files(functions_source, reference["functions_files"], manifest_path=reference_path)
    if _metadata(functions_source) != reference["distributions"]:
        raise ValueError("Reference packages changed after capture")
    validate_standard_dependencies(code_root, load_runtime_policy(code_root), installed=reference["distributions"])
    destination.mkdir(parents=True)
    base = _copy_runtime_tree(base_source, destination / "base", base=True)
    base_python = base / ("python.exe" if os.name == "nt" else "bin/python3")
    environments = {}
    for role, name in ((CORE_HOST_ROLE, "core"), (FUNCTIONS_ROLE, "functions")):
        environment = destination / "runtimes" / name
        subprocess.run([str(base_python), "-I", "-B", "-m", "venv", "--without-pip",
                        "--copies", str(environment)], check=True, capture_output=True, timeout=60)
        if role == FUNCTIONS_ROLE:
            site = environment / ("Lib/site-packages" if os.name == "nt" else "lib/python3.12/site-packages")
            # venv --without-pip makes an empty site directory; replace that
            # newly created empty directory, never a pre-existing environment.
            site.rmdir()
            _copy_runtime_tree(functions_source, site)
        _write_profile(code_root, environment, role)
        environments[role] = qualify_environment(
            code_root, environment, base, destination / "qualifications" / (name + ".json"), role)
    # Re-read the captured source closures: inconsistent copies never qualify.
    verify_runtime_files(base_source, reference["base_files"], manifest_path=reference_path)
    verify_runtime_files(functions_source, reference["functions_files"], manifest_path=reference_path)
    result = {"schema_version": 1}
    for key, role in (("core", CORE_HOST_ROLE), ("functions", FUNCTIONS_ROLE)):
        qualified = environments[role]
        result[key] = {"path": str(qualified.manifest_path), "digest": qualified.manifest_digest}
    selection = destination / "runtimes/hashi-runtime-release.json"
    selection.write_text(json.dumps(result, sort_keys=True), encoding="utf-8")
    return {"destination": str(destination), "selection": str(selection),
            "core": environments[CORE_HOST_ROLE].fingerprint.to_dict(),
            "functions": environments[FUNCTIONS_ROLE].fingerprint.to_dict(),
            "reference_digest": expected_digest}
