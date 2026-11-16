"""Product-neutral immutable-code verification and child import routing."""

from __future__ import annotations

import hashlib
import importlib.abc
import importlib.util
import re
import threading
import time
from pathlib import Path, PurePosixPath

from orchestrator.runtime_contract import CORE_SOURCE_PATHS


_verification_lock = threading.Lock()
_verification_stats = {
    "full_scans": 0,
    "identity_checks": 0,
    "files_hashed": 0,
    "bytes_hashed": 0,
    "duration_ms": 0.0,
}


def artifact_verification_stats(*, reset: bool = False) -> dict:
    """Return process-local scan evidence for startup/restart diagnostics."""
    with _verification_lock:
        value = dict(_verification_stats)
        if reset:
            for key in _verification_stats:
                _verification_stats[key] = 0.0 if key == "duration_ms" else 0
        return value


def _record_artifact_verification(**changes) -> None:
    with _verification_lock:
        for key, value in changes.items():
            _verification_stats[key] += value


def manifest_digest(entries, assets=()) -> str:
    digest = hashlib.sha256()
    for entry in entries:
        digest.update(b"module\0")
        digest.update(entry["module"].encode("utf-8") + b"\0")
        digest.update(entry["relative_path"].encode("utf-8") + b"\0")
        digest.update(entry["sha256"].encode("ascii") + b"\n")
    for asset in assets:
        digest.update(b"asset\0")
        digest.update(asset["relative_path"].encode("utf-8") + b"\0")
        digest.update(asset["sha256"].encode("ascii"))
        digest.update(b"\0x\n" if asset["executable"] else b"\0-\n")
    return "sha256:" + digest.hexdigest()


def _validated_manifest_entries(manifest: dict):
    entries = manifest["entries"]
    assets = manifest.get("assets", [])
    if manifest_digest(entries, assets) != manifest["generation_id"]:
        raise ValueError("artifact manifest digest mismatch")
    seen, modules, validated = set(), set(), []
    for entry in [*entries, *assets]:
        relative = entry["relative_path"]
        path = PurePosixPath(relative)
        if (
            not relative
            or path.is_absolute()
            or ".." in path.parts
            or "\\" in relative
            or ":" in relative
            or path.as_posix() != relative
            or relative in seen
            or relative in CORE_SOURCE_PATHS
        ):
            raise ValueError(f"invalid artifact path: {relative}")
        seen.add(relative)
        if "module" in entry:
            module = entry["module"]
            valid_paths = {
                module.replace(".", "/") + suffix for suffix in (".py", "/__init__.py")
            }
            if (
                not re.fullmatch(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)*", module)
                or module in modules
                or relative not in valid_paths
            ):
                raise ValueError(f"invalid artifact module: {module}")
            modules.add(module)
        validated.append(entry)
    return validated


def _validated_artifact_entries(root: Path, manifest: dict):
    original = Path(root)
    if (
        not original.is_absolute()
        or original.is_symlink()
        or getattr(original, "is_junction", lambda: False)()
        or not original.is_dir()
        or original.resolve() != original.absolute()
    ):
        raise ValueError("artifact root is unavailable or redirected")
    root = original.resolve()
    validated = []
    for entry in _validated_manifest_entries(manifest):
        relative = entry["relative_path"]
        target = root / relative
        if (target.is_symlink() or not target.is_file()
                or not target.resolve().is_relative_to(root)):
            raise ValueError(f"artifact path escapes root: {relative}")
        if entry.get("executable") and not target.stat().st_mode & 0o100:
            raise ValueError(f"artifact executable bit lost: {relative}")
        validated.append((entry, target))
    return validated


def validate_artifact_identity(root: Path, manifest: dict) -> None:
    """Validate a qualified generation receipt without touching every artifact.

    The parent already performed ``verify_artifact`` before product execution.
    Routine children receive this manifest over the private nonce-bound channel,
    so admission rechecks its canonical identity and the unredirected root only.
    It deliberately does not claim that the previously verified disk bytes are
    immutable against a later local writer.
    """
    original = Path(root)
    if (
        not original.is_absolute()
        or original.is_symlink()
        or getattr(original, "is_junction", lambda: False)()
        or not original.is_dir()
        or original.resolve() != original.absolute()
    ):
        raise ValueError("artifact root is unavailable or redirected")
    _validated_manifest_entries(manifest)
    _record_artifact_verification(identity_checks=1)


def verify_artifact(root: Path, manifest: dict) -> None:
    """Full byte verification used once at generation qualification/adoption."""
    started = time.perf_counter()
    files_hashed = 0
    bytes_hashed = 0
    try:
        for entry, target in _validated_artifact_entries(root, manifest):
            content = target.read_bytes()
            files_hashed += 1
            bytes_hashed += len(content)
            if hashlib.sha256(content).hexdigest() != entry["sha256"]:
                raise ValueError(f"artifact bytes changed: {entry['relative_path']}")
    finally:
        _record_artifact_verification(
            full_scans=1,
            files_hashed=files_hashed,
            bytes_hashed=bytes_hashed,
            duration_ms=(time.perf_counter() - started) * 1000,
        )


class GenerationModuleFinder(importlib.abc.MetaPathFinder):
    """Exact manifest routing; unlisted project modules fail closed.

    The Core process never installs this finder or imports an entrypoint.
    A freshly spawned child installs it only after runtime and byte checks.
    """

    def __init__(self, *, generation_root: Path, code_root: Path, manifest: dict):
        self.generation_root = Path(generation_root).resolve()
        self.code_root = Path(code_root).resolve()
        self.entries = {entry["module"]: entry for entry in manifest["entries"]}

    def find_spec(self, fullname, path=None, target=None):
        entry = self.entries.get(fullname)
        if entry is not None:
            source = self.generation_root / entry["relative_path"]
            locations = None
            if entry["relative_path"].endswith("/__init__.py"):
                locations = [
                    str(source.parent),
                    str(self.code_root / Path(*fullname.split("."))),
                ]
            return importlib.util.spec_from_file_location(
                fullname, source, submodule_search_locations=locations
            )
        relative = fullname.replace(".", "/") + ".py"
        if relative in CORE_SOURCE_PATHS:
            return importlib.util.spec_from_file_location(
                fullname, self.code_root / relative
            )
        # Namespace packages are containers, not an escape to mutable Python.
        if any(name.startswith(fullname + ".") for name in self.entries):
            return importlib.util.spec_from_loader(
                fullname, loader=None, is_package=True
            )
        if (self.code_root / relative).is_file() or (
            self.code_root / fullname.replace(".", "/") / "__init__.py"
        ).is_file():
            raise ImportError(
                f"project module is outside the qualified generation: {fullname}"
            )
        return None
