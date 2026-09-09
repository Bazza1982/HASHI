from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parents[2]


def test_npm_manifest_keeps_program_and_instance_data_lifecycles_separate() -> None:
    manifest = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))

    assert manifest["name"] == "hashi-bridge"
    assert manifest["bin"]["hashi"] == "./cli.js"
    assert manifest["bin"]["hashi-onboard"] == "./onboard-cli.js"
    assert not {
        "preuninstall",
        "uninstall",
        "postuninstall",
    }.intersection(manifest.get("scripts", {}))
    assert "scripts/hashi_instance_cli.py" in manifest["files"]


def test_npm_tarball_contains_runtime_closure_without_local_state(tmp_path) -> None:
    npm = shutil.which("npm")
    assert npm is not None, "npm is required to verify the npm publication boundary"

    result = subprocess.run(
        [npm, "pack", str(ROOT), "--dry-run", "--json"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert len(payload) == 1
    packaged = {item["path"] for item in payload[0]["files"]}

    assert {
        "cli.js",
        "BUILD_INFO.json",
        "main.py",
        "pyproject.toml",
        "runtime-entry.json",
        "THIRD_PARTY_NOTICES.md",
        "constraints/standard-py312.lock",
        "docs/INSTALL.md",
        "adapters/base.py",
        "browser_gateway/__init__.py",
        "flow/flow_cli.py",
        "flow/workflows/library/book_translation.yaml",
        "locales/runtime/en.json",
        "nagare/cli.py",
        "orchestrator/config.py",
        "remote/__init__.py",
        "scripts/check_runtime_contract.py",
        "scripts/hashi_instance_cli.py",
        "scripts/npm-build-provenance.js",
        "tools/builtins.py",
        "tools/instance_registry.py",
        "tools/bin/usecomputer",
        "transports/__init__.py",
        "tui/__init__.py",
    } <= packaged

    build_info = json.loads(
        (ROOT / "BUILD_INFO.json").read_text(encoding="utf-8")
    )
    provenance = build_info["provenance"]
    assert provenance["release_channel"] == "npm"
    assert provenance["product_version"] == "4.0.0a2"
    assert provenance["build_id"].startswith("sha256:")
    assert not any(
        key in provenance for key in ("code_root", "path", "remote_url", "username")
    )

    assert not any(
        name.startswith(
            (
                "skills/agent-audit/",
                "skills/hermes-memory-import/",
                "skills/library-pick/",
                "skills/memory-consolidation/",
            )
        )
        for name in packaged
    )

    forbidden_parts = {
        ".env",
        "__pycache__",
        "node_modules",
        "data",
        "dist",
        "loops",
        "recordings",
        "workbench",
    }
    offenders = {
        name
        for name in packaged
        if forbidden_parts.intersection(Path(name).parts)
        or name.endswith((".bak", ".backup", ".orig", ".pyc", ".rej", ".log", ".pid"))
    }
    assert offenders == set()

    assert not any(
        name.startswith(
            (
                "flow/evaluation_kb/improvements/",
                "flow/evaluation_kb/workflow_scores/",
                "flow/evaluation_kb/workflow_versions/",
            )
        )
        for name in packaged
    )

    operator_markers = (
        b"/home/" + b"lily/",
        b"/mnt/c/Users/" + b"thene/",
        b"C:/Users/" + b"thene/",
        b"C:\\\\Users\\\\" + b"thene\\\\",
        b"10.255." + b"255.254",
    )
    text_suffixes = {
        ".bat",
        ".cjs",
        ".cmd",
        ".js",
        ".json",
        ".md",
        ".ps1",
        ".py",
        ".sh",
        ".toml",
        ".yaml",
        ".yml",
    }
    local_path_leaks = {
        name
        for name in packaged
        if Path(name).suffix.lower() in text_suffixes
        and (source := ROOT / name).is_file()
        and any(marker.lower() in source.read_bytes().lower() for marker in operator_markers)
    }
    assert local_path_leaks == set()
