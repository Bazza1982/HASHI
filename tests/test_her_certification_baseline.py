from __future__ import annotations

import hashlib
import json
from pathlib import Path

from adapters.her import HER_VERSION


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_her_certification_baseline_matches_packaged_manifest():
    manifest = _load(PROJECT_ROOT / "hashi_assets" / "her" / "manifest.json")
    baseline = _load(PROJECT_ROOT / "hashi_assets" / "her" / "certification_baseline.json")

    assert baseline["runtime_version"] == manifest["version"]
    assert baseline["upstream_commit"] == manifest["upstream_commit"]
    assert baseline["source_commit"] == manifest["source_commit"]
    assert HER_VERSION == manifest["version"]


def test_packaged_her_binaries_match_manifest_digests():
    root = PROJECT_ROOT / "hashi_assets" / "her"
    manifest = _load(root / "manifest.json")
    assert set(manifest["binaries"]) == {"linux-x86_64", "windows-x86_64"}

    for platform_key, binary_metadata in manifest["binaries"].items():
        binary = (root / binary_metadata["path"]).resolve()

        assert binary.is_relative_to(root.resolve()), platform_key
        assert binary.is_file(), platform_key
        assert binary.name == binary_metadata["binary_name"], platform_key
        assert (
            hashlib.sha256(binary.read_bytes()).hexdigest()
            == binary_metadata["sha256"]
        ), platform_key


def test_her_certification_requires_all_workspace_tests_to_pass():
    baseline = _load(PROJECT_ROOT / "hashi_assets" / "her" / "certification_baseline.json")
    rust_workspace = baseline["rust_workspace"]
    clippy_command = baseline["clippy"]["command"]
    clippy_diagnostics = baseline["clippy"]["expected_upstream_diagnostics"]

    assert rust_workspace == {"command": ["cargo", "test", "--workspace"]}
    assert clippy_command == [
        "cargo",
        "clippy",
        "--workspace",
        "--all-targets",
        "--",
        "-D",
        "warnings",
    ]
    assert clippy_diagnostics == []
