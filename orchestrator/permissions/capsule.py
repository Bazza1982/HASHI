"""Qualified self-contained permission runtime; never use editable/site imports.

The manifest digest is pinned in the operator-owned installation document.
Ordinary Agent requests cannot choose a manifest, executable or import root.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
from zipfile import ZipFile

from orchestrator.permissions.windows_security import require_private_path


def _relative(value):
    if (not isinstance(value, str) or not value or "\\" in value
            or PurePosixPath(value).is_absolute() or ":" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))):
        raise ValueError("invalid capsule-relative path")
    return value


def _ordinary(path):
    stat = path.lstat()
    if (path.is_symlink() or getattr(path, "is_junction", lambda: False)()
            or getattr(stat, "st_file_attributes", 0) & 0x400):
        raise PermissionError("capsules cannot contain reparse points")
    if path.is_file() and stat.st_nlink != 1:
        raise PermissionError("capsules cannot contain hard-linked files")
    if not path.is_file() and not path.is_dir():
        raise PermissionError("capsules contain only ordinary files/directories")
    return stat


@dataclass(frozen=True)
class RuntimeCapsule:
    root: Path
    manifest: dict
    digest: str

    @property
    def executable(self):
        return self.root / self.manifest["executable"]

    @property
    def host_entry(self):
        return self.root / "app/orchestrator/permissions/restricted_her_host.py"

    @property
    def environment(self):
        # In particular, no PYTHONPATH, user site, proxy, auth, token or
        # inherited Provider credentials are copied into an execution host.
        system_root = os.environ.get("SystemRoot")
        if not system_root:
            raise PermissionError("the native Windows system root is unavailable")
        return {"SystemRoot": system_root, "WINDIR": system_root,
                "PATH": str(self.executable.parent) + os.pathsep + str(Path(system_root) / "System32")}

    def file_digest(self, path):
        return self.manifest["files"][Path(path).relative_to(self.root).as_posix()]["sha256"]


def qualify_capsule(manifest_path, expected_digest, *, trusted_sids, qualifier=require_private_path):
    source = Path(manifest_path)
    if not source.is_absolute() or source.name != "capsule.json":
        raise ValueError("an absolute fixed capsule manifest is required")
    root = source.parent
    _ordinary(root)
    _ordinary(source)
    qualifier(root, trusted_sids=trusted_sids, read_only_public=True)
    qualifier(source, trusted_sids=trusted_sids, read_only_public=True)
    raw = source.read_bytes()
    if sha256(raw).hexdigest() != expected_digest:
        raise PermissionError("the pinned runtime manifest changed")
    manifest = json.loads(raw)
    if (not isinstance(manifest, dict) or set(manifest) != {
            "version", "source_commit", "python_version", "executable", "files",
        } or manifest["version"] != 1 or not isinstance(manifest["files"], dict)
            or not manifest["files"]):
        raise ValueError("invalid sealed runtime manifest")
    executable = _relative(manifest["executable"])
    if (not executable.startswith("python/") or not executable.endswith("/python.exe")
            or executable not in manifest["files"]
            or "app/orchestrator/permissions/restricted_her_host.py" not in manifest["files"]):
        raise ValueError("the fixed runtime entrypoints are missing")
    expected, observed = set(manifest["files"]), set()
    for name, fact in manifest["files"].items():
        _relative(name)
        if not isinstance(fact, dict) or set(fact) != {"sha256", "size"}:
            raise ValueError("invalid capsule file evidence")
        if type(fact["size"]) is not int or fact["size"] < 0:
            raise ValueError("invalid capsule file size")
    # Check directory ACLs too: an untrusted DELETE_CHILD grant on a parent
    # could replace an individually read-only import file.
    for path in root.rglob("*"):
        stat = _ordinary(path)
        qualifier(path, trusted_sids=trusted_sids, read_only_public=True)
        if path == source or path.is_dir():
            continue
        relative = path.relative_to(root).as_posix()
        observed.add(relative)
        fact = manifest["files"].get(relative)
        if (fact is None or stat.st_size != fact["size"]
                or sha256(path.read_bytes()).hexdigest() != fact["sha256"]):
            raise PermissionError("sealed runtime content changed")
    if observed != expected:
        raise PermissionError("sealed runtime content is incomplete")
    for path in (root / "python").glob("*._pth"):
        lines = path.read_text(encoding="utf-8").splitlines()
        if any("import " in line or (line and not line.startswith("#") and line not in {
            "DLLs", "Lib", "Lib/site-packages", "../app",
        }) for line in lines):
            raise PermissionError("the isolated import configuration changed")
        break
    else:
        raise PermissionError("a sealed isolated Python import configuration is required")
    return RuntimeCapsule(root, manifest, expected_digest)


def prepare_capsule(source_root, destination, *, runtime_root, site_packages):
    """Build a reviewable artifact only; never install accounts/ACLs or start it.

    The source is exactly the committed Git tree. The supplied Python/dependency
    directories must be the already qualified build environment, not a package
    install performed in a running Core interpreter.
    """
    source, target = Path(source_root), Path(destination)
    runtime, dependencies = Path(runtime_root), Path(site_packages)
    if (not all(path.is_absolute() for path in (source, target, runtime, dependencies))
            or target.exists() or not (runtime / "python.exe").is_file()
            or not dependencies.is_dir()):
        raise ValueError("explicit source/runtime/dependencies and a new artifact directory are required")
    def git(*arguments):
        result = subprocess.run(["git", "-C", str(source), *arguments], capture_output=True, check=True)
        return result.stdout
    commit = git("rev-parse", "HEAD").decode().strip()
    if git("status", "--porcelain", "--untracked-files=no"):
        raise ValueError("the capsule source must be committed before packaging")
    target.mkdir(parents=True)
    try:
        archive = target / "source.zip"
        archive.write_bytes(git("archive", "--format=zip", commit))
        with ZipFile(archive) as zipped:
            for entry in zipped.infolist():
                name = entry.filename.rstrip("/")
                _relative(name)
                if entry.external_attr >> 16 & 0o170000 == 0o120000:
                    raise ValueError("source archives cannot include symlinks")
            zipped.extractall(target / "app")
        archive.unlink()
        # Keep a complete standard library and actual qualified dependencies.
        # Exclude mutable caches and any base interpreter's unrelated packages.
        def ignored(directory, names):
            return [name for name in names if name in {
                "__pycache__", "site-packages", ".git", ".venv",
            } or name.endswith((".pyc", ".pyo"))]
        shutil.copytree(runtime, target / "python", ignore=ignored)
        shutil.copytree(dependencies, target / "python/Lib/site-packages",
                        ignore=lambda directory, names: [name for name in names if
                            name == "__pycache__" or name.endswith((".pyc", ".pyo", ".pth"))])
        for path in (target / "python").glob("*._pth"):
            path.unlink()
        # The builder interpreter version must match the supplied distribution.
        # An installed service additionally observes its actual runtime version.
        pth = target / "python" / f"python{sys.version_info.major}{sys.version_info.minor}._pth"
        pth.write_text("DLLs\nLib\nLib/site-packages\n../app\n", encoding="utf-8")
        files = {}
        for path in target.rglob("*"):
            stat = _ordinary(path)
            if path.is_file():
                files[path.relative_to(target).as_posix()] = {
                    "sha256": sha256(path.read_bytes()).hexdigest(), "size": stat.st_size,
                }
        manifest = {"version": 1, "source_commit": commit,
                    "python_version": list(sys.version_info[:3]),
                    "executable": "python/python.exe", "files": files}
        encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        (target / "capsule.json").write_bytes(encoded)
        return {"manifest": str(target / "capsule.json"), "sha256": sha256(encoded).hexdigest(),
                "source_commit": commit}
    except BaseException:
        # Failed artifacts are deliberately not installed or silently repaired.
        # Keep their evidence for the operator, and never overwrite a target.
        raise
