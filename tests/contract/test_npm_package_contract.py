from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parents[2]


def test_npm_tarball_contains_runtime_closure_without_local_state() -> None:
    npm = shutil.which("npm")
    assert npm is not None, "npm is required to verify the npm publication boundary"

    result = subprocess.run(
        [npm, "pack", "--dry-run", "--json"],
        cwd=ROOT,
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
        "main.py",
        "pyproject.toml",
        "THIRD_PARTY_NOTICES.md",
        "constraints/standard-py312.lock",
        "adapters/base.py",
        "browser_gateway/__init__.py",
        "flow/flow_cli.py",
        "flow/workflows/library/book_translation.yaml",
        "locales/runtime/en.json",
        "nagare/cli.py",
        "orchestrator/config.py",
        "remote/__init__.py",
        "scripts/check_runtime_contract.py",
        "tools/builtins.py",
        "tools/bin/usecomputer",
        "transports/__init__.py",
        "tui/__init__.py",
    } <= packaged

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
