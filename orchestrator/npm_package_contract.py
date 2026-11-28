"""Refuse npm packages whose tarball omits required Function source.

``FUNCTION_SOURCE.json`` is captured from *every* tracked file at HEAD, while a
published tarball only contains what npm actually selects.  When the two
disagree the package looks well formed to the publisher but every installed
instance fails at startup::

    FunctionGenerationError: Candidate function modules have no project source: ['hal.service']
    New Function generation was rejected; continuing startup with the last verified generation

HAL shipped in exactly that state between 2026-11-25 and 2026-11-28: the
contract listed the ``hal/`` package, the tarball did not carry it, so instances
silently ran an older generation.

The authority for "what is in the tarball" is npm itself.  Re-implementing the
``files`` allow-list in Python drifted from real behaviour in both directions —
a nested ``.npmignore`` inside an allow-listed directory could drop a module
without the guard noticing, while wildcard entries such as ``hal/**/service.py``
were reported as missing when npm did ship them.  These helpers therefore ask
npm (``npm pack --dry-run --json``) for the exact file list and compare it with
the Function source the contract requires.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from orchestrator.function_contract import (
    FUNCTION_MODULE_PREFIXES,
    FUNCTION_SIDECAR_PREFIXES,
)

# Set on the nested ``npm pack`` that computes the file list.  ``--ignore-scripts``
# already stops it from running prepack again; this flag turns any future
# recursion into a loud failure instead of a hang.
PACK_LIST_CHILD_ENV = "HASHI_NPM_PACK_LIST_CHILD"

_DEFAULT_TIMEOUT_S = 300.0


def _normalise(entry: str) -> str:
    path = str(entry).replace("\\", "/").strip()
    while path.startswith("./"):
        path = path[2:]
    return path


def module_name_for_path(relative: str) -> str:
    """Map ``hal/service.py`` to ``hal.service`` and ``hal/__init__.py`` to ``hal``."""

    path = _normalise(relative)
    for suffix in (".py", ".pyi"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    parts = [part for part in path.split("/") if part]
    if parts and parts[-1] == "__init__":
        parts.pop()
    if parts and parts[0] == "src":
        parts.pop(0)
    return ".".join(parts)


def is_function_source_path(relative: str) -> bool:
    """True when a contract file is Python source of a Function module package."""

    if not _normalise(relative).endswith((".py", ".pyi")):
        return False
    name = module_name_for_path(relative)
    if not name:
        return False
    if any(name == prefix.rstrip(".") or name.startswith(prefix) for prefix in FUNCTION_SIDECAR_PREFIXES):
        return False
    return any(name == prefix.rstrip(".") or name.startswith(prefix) for prefix in FUNCTION_MODULE_PREFIXES)


def package_root(relative: str) -> str:
    parts = _normalise(relative).split("/")
    return parts[0] if len(parts) > 1 else ""


def required_function_files(contract_files: Iterable[str]) -> tuple[str, ...]:
    """Contract files that a tarball must carry for Function generation to work."""

    return tuple(sorted({_normalise(relative) for relative in contract_files if is_function_source_path(relative)}))


def missing_from_pack_list(
    contract_files: Iterable[str],
    packaged_paths: Iterable[str],
) -> tuple[str, ...]:
    """Required Function source files that npm's real file list does not carry."""

    packaged = {_normalise(path) for path in packaged_paths}
    return tuple(path for path in required_function_files(contract_files) if path not in packaged)


def _pack_list_paths(stdout: str) -> tuple[str, ...]:
    payload = (stdout or "").strip()
    if not payload:
        raise RuntimeError("npm pack --dry-run produced no JSON output")
    try:
        report = json.loads(payload)
    except json.JSONDecodeError:
        # npm may prefix notices on stdout when a lifecycle hook writes to it.
        start = payload.find("[")
        if start < 0:
            raise RuntimeError(f"npm pack --dry-run did not return JSON: {payload[:200]}")
        report = json.loads(payload[start:])
    if isinstance(report, Mapping):
        report = [report]
    entries = report[0].get("files") if report else None
    if not isinstance(entries, list):
        raise RuntimeError("npm pack --dry-run returned no file list")
    paths = [
        _normalise(item["path"])
        for item in entries
        if isinstance(item, Mapping) and isinstance(item.get("path"), str)
    ]
    if not paths:
        raise RuntimeError("npm pack --dry-run returned an empty file list")
    return tuple(paths)


def npm_pack_files(root: Path, *, timeout: float = _DEFAULT_TIMEOUT_S) -> tuple[str, ...]:
    """Ask npm which files the tarball would actually contain.

    ``--ignore-scripts`` keeps the nested invocation from re-entering prepack;
    ``--dry-run`` guarantees nothing is written to disk.
    """

    if os.environ.get(PACK_LIST_CHILD_ENV) == "1":
        raise RuntimeError(
            "refusing to run a nested npm pack while computing the packaging file list"
        )
    npm = shutil.which("npm")
    if not npm:
        raise RuntimeError("npm was not found on PATH; cannot verify the published file list")
    arguments = ["pack", "--dry-run", "--json", "--ignore-scripts"]
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.lower().startswith(("npm_config_", "npm_lifecycle_", "npm_package_"))
    }
    environment[PACK_LIST_CHILD_ENV] = "1"
    if os.name == "nt":
        # CreateProcess cannot launch npm.cmd directly; go through the shell.
        command: object = f'"{npm}" ' + " ".join(arguments)
        result = subprocess.run(
            command,
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=environment,
            shell=True,
        )
    else:
        result = subprocess.run(
            [npm, *arguments],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=environment,
        )
    if result.returncode != 0:
        detail = [line for line in (result.stderr or result.stdout or "").splitlines() if line.strip()]
        tail = " | ".join(detail[-3:]) if detail else "no output"
        raise RuntimeError(f"npm pack --dry-run failed (exit {result.returncode}): {tail}")
    return _pack_list_paths(result.stdout)


def describe_missing(missing: Sequence[str]) -> str:
    if not missing:
        return ""
    roots: dict[str, list[str]] = {}
    for relative in missing:
        roots.setdefault(package_root(relative) or relative, []).append(relative)
    lines = [
        "npm tarball omits Function source required by orchestrator/function_contract.py:",
    ]
    for root, files in sorted(roots.items()):
        sample = ", ".join(files[:3])
        if len(files) > 3:
            sample += f", … (+{len(files) - 3} more)"
        lines.append(f"  package '{root}': {len(files)} file(s) missing — e.g. {sample}")
    lines.append(
        "Checked against npm's own file list (npm pack --dry-run --json --ignore-scripts), not a re-implementation of"
        ' package.json "files".'
    )
    additions = sorted({f'"{root}/"' for root in roots})
    lines.append(
        f'Add {", ".join(additions)} to package.json "files", or remove the ignore rule '
        "(a nested .npmignore/.gitignore entry or a \"!…\" pattern) that drops it."
    )
    lines.append(
        'An installed instance would otherwise fail with "Candidate function modules have no project source" '
        "and silently keep running an older generation."
    )
    return "\n".join(lines)


def audit_package_tree(root: Path, packaged_paths: Iterable[str] | None = None) -> dict:
    """Compare the Function contract with npm's real tarball file list.

    ``packaged_paths`` lets callers (tests, CI) supply a previously captured
    ``npm pack --dry-run --json`` listing instead of invoking npm again.
    """

    root = Path(root)
    manifest_path = root / "package.json"
    contract_path = root / "FUNCTION_SOURCE.json"
    if not manifest_path.is_file():
        return {"ok": False, "reason": f"missing {manifest_path}", "missing": ()}
    if not contract_path.is_file():
        return {"ok": False, "reason": f"missing {contract_path}", "missing": ()}
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    files: Mapping[str, object] = contract.get("files") or {}
    if packaged_paths is None:
        try:
            packaged_paths = npm_pack_files(root)
        except (RuntimeError, OSError, subprocess.SubprocessError) as error:
            return {"ok": False, "reason": f"cannot read npm's file list: {error}", "missing": ()}
    packaged = tuple(packaged_paths)
    missing = missing_from_pack_list(files.keys(), packaged)
    return {
        "ok": not missing,
        "reason": describe_missing(missing),
        "missing": missing,
        "roots": sorted({package_root(item) for item in missing}),
        "packaged_count": len(packaged),
    }
# HASHI-Fingerprint: hfp_16435bdca8054b41a3c489cb0e89aa98
