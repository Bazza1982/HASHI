"""Installer-owned publication evidence for a Git-less Function source tree.

This is Functions/platform code. Git checkouts retain the existing clean-HEAD
gate. Packaged code may use only a fixed record beside the privately admitted
FR, never an Agent/config/environment-supplied manifest.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess

from orchestrator.runtime_contract import (
    FUNCTIONS_ROLE, RuntimeContractError, core_source_digest, current_process_runtime,
)

SNAPSHOT_NAME = "FUNCTION_SOURCE.json"
PUBLICATION_NAME = "hashi-function-publication.json"
_COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_HASH = re.compile(r"[0-9a-f]{64}")


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeContractError("Duplicate Function publication member")
        result[key] = value
    return result


def _read(path):
    path = Path(path)
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        raise RuntimeContractError("Function publication is redirected")
    payload = path.read_bytes()
    if len(payload) > 64 * 1024 * 1024:
        raise RuntimeContractError("Function publication is oversized")
    return json.loads(payload, object_pairs_hook=_unique)


def _relative(text):
    if not isinstance(text, str) or not text or "\\" in text or ":" in text or "\0" in text:
        raise RuntimeContractError("Unsafe Function publication path")
    value = PurePosixPath(text)
    if value.is_absolute() or ".." in value.parts or value.as_posix() != text:
        raise RuntimeContractError("Unsafe Function publication path")
    return value


def _entry(path):
    return {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "executable": bool(path.stat().st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))}


def _git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args],
                            capture_output=True, check=True, timeout=30)
    return result.stdout


def capture_git_source(root):
    root = Path(root).resolve()
    actual = Path(_git(root, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    if actual != root:
        raise RuntimeContractError("Function publication requires the exact Git source root")
    commit = _git(root, "rev-parse", "--verify", "HEAD").decode().strip().lower()
    if _COMMIT.fullmatch(commit) is None:
        raise RuntimeContractError("Function publication source commit is invalid")
    if _git(root, "diff", "--name-only", "-z", "HEAD", "--"):
        raise RuntimeContractError("Function publication refuses modified tracked source")
    files = {}
    for raw in _git(root, "ls-tree", "-r", "--name-only", "-z", "HEAD").split(b"\0"):
        if not raw:
            continue
        relative = raw.decode("utf-8")
        path = root.joinpath(*_relative(relative).parts)
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
            raise RuntimeContractError("Function publication source is missing or redirected")
        files[relative] = _entry(path)
    value = {"schema_version": 1, "source_commit": commit,
             "core_source_digest": core_source_digest(root), "files": files}
    validate_snapshot(value, root)
    return value


def validate_snapshot(value, code_root, *, required_paths=()):
    root = Path(code_root).resolve()
    if (not isinstance(value, dict)
            or set(value) != {"schema_version", "source_commit", "core_source_digest", "files"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or not isinstance(value["source_commit"], str)
            or _COMMIT.fullmatch(value["source_commit"]) is None
            or value["core_source_digest"] != core_source_digest(root)
            or not isinstance(value["files"], dict) or not value["files"]):
        raise RuntimeContractError("Function publication source schema/Core identity mismatch")
    for relative, record in value["files"].items():
        _relative(relative)
        if (not isinstance(record, dict) or set(record) != {"sha256", "executable"}
                or not isinstance(record["sha256"], str) or _HASH.fullmatch(record["sha256"]) is None
                or type(record["executable"]) is not bool):
            raise RuntimeContractError("Function publication file record mismatch")
    for relative in required_paths:
        _relative(relative)
        path = root / relative
        if (relative not in value["files"] or path.is_symlink()
                or not path.is_file() or not path.resolve().is_relative_to(root)
                or _entry(path)["sha256"] != value["files"][relative]["sha256"]):
            raise RuntimeContractError("Function publication source bytes/path mismatch: " + relative)
    return value


def read_package_snapshot(code_root):
    root = Path(code_root).resolve()
    value = validate_snapshot(_read(root / SNAPSHOT_NAME), root)
    # Missing non-shipped test files are not imported. Every present declared
    # source is checked; the requested FG additionally requires every member.
    present = [relative for relative in value["files"] if (root / relative).exists()]
    return validate_snapshot(value, root, required_paths=present)


def packaged_tree_snapshot(git_root, packaged_root):
    source = capture_git_source(git_root)
    root = Path(packaged_root).resolve()
    files = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative == SNAPSHOT_NAME:
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise RuntimeContractError("Packaged Function source is redirected")
        record = _entry(path)
        original = source["files"].get(relative)
        if original is not None and original["sha256"] != record["sha256"]:
            raise RuntimeContractError("Packaged tracked source differs from committed input: " + relative)
        if path.suffix == ".py" and original is None:
            raise RuntimeContractError("Packaged Python source was not committed: " + relative)
        files[relative] = record
    value = {**source, "files": files}
    validate_snapshot(value, root, required_paths=files)
    return value


def publish_source(code_root, runtime_parent, snapshot=None):
    root, runtime_parent = Path(code_root).resolve(), Path(runtime_parent).resolve()
    if snapshot is None:
        try:
            checkout = _git(root, "rev-parse", "--show-toplevel").decode().strip()
        except (OSError, subprocess.SubprocessError):
            checkout = ""
        snapshot = (capture_git_source(root) if checkout and Path(checkout).resolve() == root
                    else read_package_snapshot(root))
    validate_snapshot(snapshot, root)
    target = runtime_parent / PUBLICATION_NAME
    value = {"schema_version": 1, "code_root": str(root), "source": snapshot}
    with target.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True)
    return target


def verify_published_manifest(manifest, code_root):
    root = Path(code_root).resolve()
    bound = current_process_runtime()
    if bound is None or bound.fingerprint.role != FUNCTIONS_ROLE:
        raise RuntimeContractError("Git-less Functions require a privately admitted runtime binding")
    path = Path(bound.fingerprint.environment_prefix).parent / PUBLICATION_NAME
    value = _read(path)
    if (not isinstance(value, dict) or set(value) != {"schema_version", "code_root", "source"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["code_root"] != str(root)):
        raise RuntimeContractError("Function publication authority/source root mismatch")
    paths = [entry.relative_path for entry in (*manifest.entries, *manifest.assets)]
    source = validate_snapshot(value["source"], root, required_paths=paths)
    for entry in (*manifest.entries, *manifest.assets):
        expected = source["files"][entry.relative_path]
        if entry.sha256 != expected["sha256"]:
            raise RuntimeContractError("Candidate differs from installer-published Function bytes")
        if hasattr(entry, "executable") and entry.executable != expected["executable"]:
            raise RuntimeContractError("Candidate differs from published Function executable mode")
    return source["source_commit"]
