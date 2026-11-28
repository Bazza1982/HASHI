"""Refuse npm packages whose allow-list drops required Function source.

``FUNCTION_SOURCE.json`` is captured from *every* tracked file at HEAD, while a
published tarball only contains what ``package.json``'s ``files`` allow-list
covers.  When the two disagree the package looks well formed to the publisher
but every installed instance fails at startup::

    FunctionGenerationError: Candidate function modules have no project source: ['hal.service']
    New Function generation was rejected; continuing startup with the last verified generation

HAL shipped in exactly that state between 2026-11-25 and 2026-11-28: the
contract listed the ``hal/`` package, the allow-list did not include it, so
instances silently ran an older generation.  These helpers let the prepack
step fail loudly instead of publishing an unusable artifact.
"""

from __future__ import annotations

import fnmatch
import json
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from orchestrator.function_contract import (
    FUNCTION_MODULE_PREFIXES,
    FUNCTION_SIDECAR_PREFIXES,
)


def _normalise(entry: str) -> str:
    return entry.replace("\\", "/").strip().lstrip("./")


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


def _entry_covers(entry: str, path: str) -> bool:
    pattern = _normalise(entry).rstrip("/")
    if not pattern:
        return False
    if fnmatch.fnmatchcase(path, pattern):
        return True
    return path.startswith(pattern + "/")


def published(path: str, allow_list: Sequence[str]) -> bool:
    """Apply npm ``files`` semantics to one relative path."""

    included = False
    for raw in allow_list:
        entry = _normalise(raw)
        if not entry:
            continue
        if entry.startswith("!"):
            if _entry_covers(entry[1:], path):
                return False
            continue
        if _entry_covers(entry, path):
            included = True
    return included


def package_root(relative: str) -> str:
    parts = _normalise(relative).split("/")
    return parts[0] if len(parts) > 1 else ""


def missing_published_modules(
    contract_files: Iterable[str],
    allow_list: Sequence[str],
) -> tuple[str, ...]:
    """Contract files required by Function prefixes that the tarball would omit."""

    missing = [
        relative
        for relative in sorted(contract_files)
        if is_function_source_path(relative) and not published(_normalise(relative), allow_list)
    ]
    return tuple(missing)


def describe_missing(missing: Sequence[str]) -> str:
    if not missing:
        return ""
    roots: dict[str, list[str]] = {}
    for relative in missing:
        roots.setdefault(package_root(relative) or relative, []).append(relative)
    lines = [
        "npm package allow-list omits Function source required by orchestrator/function_contract.py:",
    ]
    for root, files in sorted(roots.items()):
        sample = ", ".join(files[:3])
        if len(files) > 3:
            sample += f", … (+{len(files) - 3} more)"
        lines.append(f"  package '{root}': {len(files)} file(s) missing from the tarball — e.g. {sample}")
    additions = sorted({f'"{root}/"' for root in roots})
    lines.append(f'Add {", ".join(additions)} to package.json "files".')
    lines.append(
        'An installed instance would otherwise fail with "Candidate function modules have no project source" '
        "and silently keep running an older generation."
    )
    return "\n".join(lines)


def audit_package_tree(root: Path) -> dict:
    """Read ``package.json`` and ``FUNCTION_SOURCE.json`` from a checkout."""

    root = Path(root)
    manifest_path = root / "package.json"
    contract_path = root / "FUNCTION_SOURCE.json"
    if not manifest_path.is_file():
        return {"ok": False, "reason": f"missing {manifest_path}", "missing": ()}
    if not contract_path.is_file():
        return {"ok": False, "reason": f"missing {contract_path}", "missing": ()}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    allow_list = manifest.get("files")
    if not isinstance(allow_list, list):
        # No allow-list: npm publishes everything tracked, so nothing can be dropped.
        return {"ok": True, "reason": "", "missing": ()}
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    files: Mapping[str, object] = contract.get("files") or {}
    missing = missing_published_modules(files.keys(), allow_list)
    return {
        "ok": not missing,
        "reason": describe_missing(missing),
        "missing": missing,
        "roots": sorted({package_root(item) for item in missing}),
    }
# HASHI-Fingerprint: hfp_16435bdca8054b41a3c489cb0e89aa98
