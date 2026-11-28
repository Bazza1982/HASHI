from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from orchestrator.npm_package_contract import (
    describe_missing,
    missing_published_modules,
)

ROOT = Path(__file__).resolve().parents[1]


def _tracked_files() -> list[str]:
    output = subprocess.run(
        ["git", "-C", str(ROOT), "ls-tree", "-r", "--name-only", "-z", "HEAD"],
        capture_output=True,
        check=True,
        timeout=120,
    ).stdout
    return [item for item in output.decode("utf-8").split("\0") if item]


def _manifest() -> dict:
    return json.loads((ROOT / "package.json").read_text(encoding="utf-8"))


def test_repository_allow_list_ships_every_function_module():
    assert missing_published_modules(_tracked_files(), _manifest()["files"]) == ()


def test_20261125_package_shape_is_rejected_with_real_sources():
    """Replay the incident: the contract listed hal/, the allow-list did not."""

    allow_list = [item for item in _manifest()["files"] if item != "hal/"]
    missing = missing_published_modules(_tracked_files(), allow_list)
    assert missing, "the pre-fix allow-list must be rejected"
    assert all(relative.startswith("hal/") for relative in missing)
    message = describe_missing(missing)
    assert "package 'hal'" in message
    assert '"hal/"' in message
    assert "no project source" in message


def test_missing_root_package_is_reported_with_readable_name():
    contract = ["hal/__init__.py", "hal/service.py", "hal/whiteboard.py", "orchestrator/bridge.py"]
    missing = missing_published_modules(contract, ["orchestrator/", "adapters/"])
    assert missing == ("hal/__init__.py", "hal/service.py", "hal/whiteboard.py")
    assert "package 'hal'" in describe_missing(missing)


def test_negated_package_is_reported_as_missing():
    contract = ["hal/service.py", "orchestrator/bridge.py"]
    assert missing_published_modules(contract, ["hal/", "orchestrator/", "!hal/**"]) == ("hal/service.py",)


def test_partial_package_listing_still_counts_as_published():
    contract = ["exp/__init__.py", "exp/loader.py"]
    assert missing_published_modules(contract, ["exp/__init__.py", "exp/loader.py"]) == ()


def test_sidecar_modules_are_not_required_in_the_tarball():
    contract = ["tools/windows_helper/runner.py", "tools/read_only.py"]
    assert missing_published_modules(contract, ["tools/read_only.py"]) == ()


def test_checker_cli_fails_on_a_dropped_package(tmp_path: Path):
    packaged = tmp_path / "checkout"
    packaged.mkdir()
    (packaged / "package.json").write_text(json.dumps({"files": ["orchestrator/"]}), encoding="utf-8")
    (packaged / "FUNCTION_SOURCE.json").write_text(
        json.dumps({"files": {"hal/service.py": {}}}), encoding="utf-8"
    )
    result = subprocess.run(
        [sys.executable, "-B", str(ROOT / "scripts" / "check_npm_package_files.py"), "--root", str(packaged)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 1
    assert "package 'hal'" in result.stdout
# HASHI-Fingerprint: hfp_b7af581e5cc44523bb9ca2c9cdd168d1
