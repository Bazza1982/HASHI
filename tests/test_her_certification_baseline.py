from __future__ import annotations

import hashlib
import json
import os
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


def test_active_her_identity_docs_match_packaged_manifest():
    root = PROJECT_ROOT / "hashi_assets" / "her"
    manifest = _load(root / "manifest.json")
    evidence = _load(root / manifest["certification_evidence"])
    identity_values = {
        manifest["version"],
        manifest["source_commit"],
        evidence["source"]["certified_tag"],
        *(row["sha256"] for row in manifest["binaries"].values()),
    }

    for relative_path in (
        Path("packaging/her/README.md"),
        Path("docs/HER_BACKEND_CONTRACT.md"),
    ):
        contents = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
        for value in identity_values:
            assert value in contents, (
                f"{relative_path} is missing active HER identity {value}"
            )


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
        if platform_key.startswith("linux"):
            assert os.access(binary, os.X_OK), platform_key


def test_her_certification_requires_all_workspace_tests_to_pass():
    baseline = _load(PROJECT_ROOT / "hashi_assets" / "her" / "certification_baseline.json")
    rust_workspace = baseline["rust_workspace"]
    hashi_integration = baseline["hashi_integration"]
    clippy_command = baseline["clippy"]["command"]
    clippy_diagnostics = baseline["clippy"]["expected_upstream_diagnostics"]

    assert rust_workspace == {"command": ["cargo", "test", "--workspace"]}
    assert hashi_integration == {
        "command": ["{python}", "-m", "pytest", "-q", "tests", "veritas"]
    }
    assert clippy_command == [
        "cargo",
        "clippy",
        "--workspace",
        "--all-targets",
        "--",
        "-D",
        "warnings",
    ]
    diagnostic_keys = {
        (item["package"], item["path"], item["line"], item["lint"])
        for item in clippy_diagnostics
    }
    assert len(clippy_diagnostics) == len(diagnostic_keys) == 40
    assert {item["package"] for item in clippy_diagnostics} == {"api", "runtime"}
    assert {item["lint"] for item in clippy_diagnostics} == {
        "needless_borrows_for_generic_args",
        "result_large_err",
    }


def test_active_her_release_has_closed_supply_chain_evidence():
    root = PROJECT_ROOT / "hashi_assets" / "her"
    manifest = _load(root / "manifest.json")
    evidence_path = (root / manifest["certification_evidence"]).resolve()
    bundle_path = (root / manifest["source_bundle"]["path"]).resolve()
    evidence = _load(evidence_path)

    assert evidence_path.is_relative_to(root.resolve())
    assert evidence["certification_status"] == "candidate_complete"
    assert evidence["release_version"] == manifest["version"]
    assert evidence["source"]["source_commit"] == manifest["source_commit"]
    assert evidence["source"]["upstream_commit"] == manifest["upstream_commit"]
    assert evidence["source"]["source_branch"] == manifest["source_branch"]
    assert evidence["source"]["bundle"] == manifest["source_bundle"] | {
        "verify_status": "passed",
        "complete_history": True,
    }
    assert bundle_path.is_relative_to(root.resolve())
    assert bundle_path.stat().st_size == manifest["source_bundle"]["size_bytes"]
    assert hashlib.sha256(bundle_path.read_bytes()).hexdigest() == manifest["source_bundle"]["sha256"]

    assert set(evidence["artifacts"]) == set(manifest["binaries"])
    for platform_key, row in manifest["binaries"].items():
        artifact = evidence["artifacts"][platform_key]
        assert artifact["path"] == row["path"]
        assert artifact["sha256"] == row["sha256"]
        assert artifact["rust_target_triple"] == row["rust_target_triple"]
        assert artifact["embedded_source_commit"] == manifest["source_commit"]
        assert artifact["embedded_source_branch"] == manifest["source_branch"]
        assert artifact["provenance_dirty"] is False
