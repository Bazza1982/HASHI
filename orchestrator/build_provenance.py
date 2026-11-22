"""Immutable, non-sensitive build provenance for HASHI function artifacts.

The running-version view must never infer a process identity from the current
checkout.  Builders call :func:`capture_build_provenance` once and persist the
returned allow-listed fields beside the immutable artifact.  Query paths use
``inspect_source_checkout`` separately to describe what is on disk now.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROVENANCE_SCHEMA_VERSION = 1
_MAX_METADATA_BYTES = 1024 * 1024


def _utc_iso(value: datetime | None = None) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        if not path.is_file() or path.stat().st_size > _MAX_METADATA_BYTES:
            return None
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _build_info(code_root: Path) -> dict[str, Any] | None:
    """Read only bounded, known portable/package metadata locations."""

    root = code_root.resolve()
    candidates = (root / "BUILD_INFO.json", root.parent / "BUILD_INFO.json")
    if root.parent.name.casefold() == "app":
        candidates += (root.parent.parent / "BUILD_INFO.json",)
    for candidate in candidates:
        value = _read_json(candidate)
        if value is not None:
            return value
    return None


def _package_json(code_root: Path) -> dict[str, Any] | None:
    return _read_json(code_root / "package.json")


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def product_version(code_root: Path | str) -> str:
    """Return the formal product version without copying a UI literal."""

    root = Path(code_root).resolve()
    pyproject = root / "pyproject.toml"
    try:
        if pyproject.is_file() and pyproject.stat().st_size <= _MAX_METADATA_BYTES:
            payload = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            value = str(((payload.get("project") or {}).get("version") or "")).strip()
            if value:
                return value
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, AttributeError):
        pass
    build_info = _build_info(root) or {}
    value = str(
        build_info.get("product_version")
        or (build_info.get("provenance") or {}).get("product_version")
        or ""
    ).strip()
    if value:
        return value
    package = _package_json(root) or {}
    return str(package.get("version") or "development").strip() or "development"


def _run_git(
    root: Path,
    *args: str,
    text: bool = True,
) -> subprocess.CompletedProcess[Any]:
    env = dict(os.environ)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=text,
        encoding="utf-8" if text else None,
        errors="replace" if text else None,
        timeout=5,
        env=env,
    )


def _git_text(root: Path, *args: str) -> str | None:
    try:
        result = _run_git(root, *args)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return str(result.stdout or "").strip()


def _git_source(root: Path) -> dict[str, Any] | None:
    checkout_root = _git_text(root, "rev-parse", "--show-toplevel")
    if not checkout_root or Path(checkout_root).resolve() != root.resolve():
        return None
    commit = _git_text(root, "rev-parse", "--verify", "HEAD")
    if not commit:
        return None
    branch = _git_text(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    tag = _git_text(root, "describe", "--tags", "--exact-match", "HEAD")
    commit_time = _git_text(root, "show", "-s", "--format=%cI", "HEAD")
    status = _git_text(root, "status", "--porcelain=v1", "--untracked-files=normal")
    rows = status.splitlines() if status else []
    return {
        "commit": commit,
        "branch": branch or None,
        "tag": tag or None,
        "commit_time": commit_time or None,
        "dirty": bool(rows) if status is not None else None,
        "change_count": len(rows) if status is not None else None,
    }


def _packaged_source(root: Path) -> tuple[str, dict[str, Any]]:
    build_info = _build_info(root) or {}
    nested = build_info.get("provenance")
    provenance = nested if isinstance(nested, dict) else build_info
    package = _package_json(root) or {}
    channel = str(
        provenance.get("release_channel")
        or ("portable" if build_info else "npm" if package else "packaged")
    ).strip()
    return channel or "packaged", {
        "commit": str(
            provenance.get("commit")
            or provenance.get("source_commit")
            or provenance.get("hashi_revision")
            or package.get("gitHead")
            or ""
        ).strip()
        or None,
        "branch": str(
            provenance.get("branch") or provenance.get("source_branch") or ""
        ).strip()
        or None,
        "tag": str(
            provenance.get("tag") or provenance.get("release_tag") or ""
        ).strip()
        or None,
        "commit_time": str(
            provenance.get("commit_time")
            or provenance.get("source_commit_time")
            or ""
        ).strip()
        or None,
        "build_time": str(
            provenance.get("build_time")
            or provenance.get("built_at_utc")
            or ""
        ).strip()
        or None,
        "dirty": (
            provenance.get("dirty_at_build")
            if isinstance(provenance.get("dirty_at_build"), bool)
            else None
        ),
        "change_count": (
            _nonnegative_int(provenance["change_count_at_build"])
            if provenance.get("change_count_at_build") is not None
            else None
        ),
    }


def capture_build_provenance(
    code_root: Path | str,
    *,
    artifact_kind: str,
    generation_id: str | None = None,
    release_channel: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Capture one immutable, path-free build identity."""

    root = Path(code_root).resolve()
    source = _git_source(root)
    if source is None and ((root / ".git").exists() or (root / ".git").is_symlink()):
        # An inaccessible checkout is not a packaged installation. Do not
        # use potentially stale package metadata as its build identity.
        channel, source = "git", {}
    elif source is None:
        channel, source = _packaged_source(root)
    else:
        channel = "git"
    build_time = str(source.get("build_time") or _utc_iso(now))
    payload: dict[str, Any] = {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "product_version": product_version(root),
        "artifact_kind": str(artifact_kind),
        "release_channel": str(release_channel or channel),
        "commit": source.get("commit"),
        "branch": source.get("branch"),
        "tag": source.get("tag"),
        "commit_time": source.get("commit_time"),
        "build_time": build_time,
        "dirty_at_build": source.get("dirty"),
        "change_count_at_build": source.get("change_count"),
        "generation_id": str(generation_id) if generation_id else None,
        "verifiable": bool(source.get("commit")),
    }
    identity = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    payload["build_id"] = f"sha256:{hashlib.sha256(identity).hexdigest()}"
    return payload


def inspect_source_checkout(
    code_root: Path | str,
    *,
    running_commit: str | None = None,
) -> dict[str, Any]:
    """Describe the checkout now and compare commits without fetching."""

    root = Path(code_root).resolve()
    source = _git_source(root)
    if source is None and ((root / ".git").exists() or (root / ".git").is_symlink()):
        return {
            "available": True,
            "release_channel": "git",
            "branch": None,
            "commit": None,
            "commit_time": None,
            "dirty": None,
            "change_count": None,
            "relation": "unknown",
            "reason": "source_status_unavailable",
        }
    if source is None:
        channel, packaged = _packaged_source(root)
        return {
            "available": False,
            "release_channel": channel,
            "branch": None,
            "commit": packaged.get("commit"),
            "commit_time": packaged.get("commit_time"),
            "dirty": packaged.get("dirty"),
            "change_count": packaged.get("change_count"),
            "relation": "unavailable",
            "reason": "packaged_install",
        }

    current = str(source.get("commit") or "")
    running = str(running_commit or "").strip()
    relation = "unknown"
    if running and current == running:
        relation = "same"
    elif running:
        try:
            source_result = _run_git(
                root, "merge-base", "--is-ancestor", running, current
            ).returncode
            running_result = _run_git(
                root, "merge-base", "--is-ancestor", current, running
            ).returncode
        except (OSError, subprocess.SubprocessError):
            source_result = running_result = None
        if source_result == 0:
            relation = "source_ahead"
        elif running_result == 0:
            relation = "running_ahead"
        elif source_result == 1 and running_result == 1:
            relation = "diverged"
    return {
        "available": True,
        "release_channel": "git",
        "branch": source.get("branch"),
        "tag": source.get("tag"),
        "commit": current or None,
        "commit_time": source.get("commit_time"),
        "dirty": source.get("dirty"),
        "change_count": source.get("change_count"),
        "relation": relation,
        "reason": None,
    }
# HASHI-Fingerprint: hfp_8e5784438033432dafd9eba3c6dd9404
