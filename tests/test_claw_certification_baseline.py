from __future__ import annotations

import json
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_claw_certification_baseline_matches_packaged_manifest():
    manifest = _load(PROJECT_ROOT / "hashi_assets" / "claw" / "manifest.json")
    baseline = _load(PROJECT_ROOT / "hashi_assets" / "claw" / "certification_baseline.json")

    assert baseline["runtime_version"] == manifest["version"]
    assert baseline["upstream_commit"] == manifest["upstream_commit"]
    assert baseline["source_commit"] == manifest["source_commit"]


def test_claw_certification_exceptions_are_exact_and_non_expanding():
    baseline = _load(PROJECT_ROOT / "hashi_assets" / "claw" / "certification_baseline.json")
    rust_failures = baseline["rust_workspace"]["expected_upstream_failures"]
    clippy_command = baseline["clippy"]["command"]
    clippy_diagnostics = baseline["clippy"]["expected_upstream_diagnostics"]

    assert [item["test"] for item in rust_failures] == [
        "direct_resume_safe_slash_commands_route_to_local_json_actions_831"
    ]
    assert clippy_command == ["cargo", "clippy", "--workspace", "--lib", "--", "-D", "warnings"]
    assert len(clippy_diagnostics) == 6
    assert len(
        {
            (item["package"], item["path"], item["line"], item["lint"])
            for item in clippy_diagnostics
        }
    ) == 6
