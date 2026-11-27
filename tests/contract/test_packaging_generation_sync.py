"""Guardrail: package.json['files'] stays synced with function generation entrypoints and assets."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from orchestrator.function_generation import (
    FUNCTION_GENERATION_ENTRYPOINTS,
    REQUIRED_FUNCTION_ASSETS,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_JSON = REPO_ROOT / "package.json"

# Generated at pack/build time rather than checked into source control.
BUILD_TIME_FILES = frozenset(
    {
        "BUILD_INFO.json",
        "FUNCTION_SOURCE.json",
        "runtime-entry.json",
    }
)


def _package_files() -> list[str]:
    payload = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
    files = payload.get("files")
    assert isinstance(files, list) and files, "package.json must declare a non-empty files list"
    return [str(entry).replace("\\", "/") for entry in files]


def _is_covered_by_package_files(rel_path: str, package_files: list[str]) -> bool:
    normalized = rel_path.replace("\\", "/").lstrip("./")
    for entry in package_files:
        clean = entry.rstrip("/")
        if entry.endswith("/"):
            if normalized == clean or normalized.startswith(f"{clean}/"):
                return True
        elif normalized == clean:
            return True
    return False


def test_every_function_generation_entrypoint_package_is_packaged_and_importable():
    package_files = _package_files()
    missing_from_package: list[str] = []
    missing_spec: list[str] = []

    for module_name in FUNCTION_GENERATION_ENTRYPOINTS:
        top_package = module_name.split(".", 1)[0]
        rel_pkg_dir = f"{top_package}/"
        rel_module_py = module_name.replace(".", "/") + ".py"
        if not (
            _is_covered_by_package_files(rel_pkg_dir, package_files)
            or _is_covered_by_package_files(rel_module_py, package_files)
        ):
            missing_from_package.append(module_name)
        if importlib.util.find_spec(module_name) is None:
            missing_spec.append(module_name)

    assert not missing_from_package, (
        f"FUNCTION_GENERATION_ENTRYPOINTS missing from package.json['files']: {missing_from_package}"
    )
    assert not missing_spec, (
        f"FUNCTION_GENERATION_ENTRYPOINTS not resolvable via importlib: {missing_spec}"
    )


def test_every_required_function_asset_exists_and_is_packaged():
    package_files = _package_files()
    missing_on_disk: list[str] = []
    missing_from_package: list[str] = []

    for rel_asset in REQUIRED_FUNCTION_ASSETS:
        if not (REPO_ROOT / rel_asset).is_file():
            missing_on_disk.append(rel_asset)
        if not _is_covered_by_package_files(rel_asset, package_files):
            missing_from_package.append(rel_asset)

    assert not missing_on_disk, f"REQUIRED_FUNCTION_ASSETS missing on disk: {missing_on_disk}"
    assert not missing_from_package, (
        f"REQUIRED_FUNCTION_ASSETS missing from package.json['files']: {missing_from_package}"
    )


def test_package_files_ships_hal_and_has_no_stale_missing_directories():
    package_files = _package_files()
    assert "hal/" in package_files, "hal/ runtime must be included in package.json['files']"
    assert "native/" not in package_files, "removed native/ directory must not remain in package.json['files']"

    missing_entries = []
    for entry in package_files:
        if entry.startswith("!") or entry in BUILD_TIME_FILES or "*" in entry:
            continue
        target = REPO_ROOT / entry.rstrip("/")
        if not target.exists():
            missing_entries.append(entry)

    assert not missing_entries, (
        f"package.json['files'] references non-existent paths: {missing_entries}"
    )
