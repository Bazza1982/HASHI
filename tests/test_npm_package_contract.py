from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator.npm_package_contract import (
    PACK_LIST_CHILD_ENV,
    describe_missing,
    missing_from_pack_list,
    npm_pack_files,
)

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "check_npm_package_files.py"
NPM = shutil.which("npm")


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


def _run_cli(package: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-B", str(CLI), "--root", str(package), *extra],
        capture_output=True,
        text=True,
        timeout=300,
    )


def _fixture_package(root: Path) -> Path:
    package = root / "pkg"
    (package / "hal").mkdir(parents=True)
    (package / "package.json").write_text(
        json.dumps({"name": "hashi-pack-guard-fixture", "version": "0.0.0", "files": ["hal/"]}),
        encoding="utf-8",
    )
    (package / "hal" / "__init__.py").write_text("", encoding="utf-8")
    (package / "hal" / "service.py").write_text("VALUE = 1\n", encoding="utf-8")
    (package / "FUNCTION_SOURCE.json").write_text(
        json.dumps({"files": {"hal/__init__.py": {}, "hal/service.py": {}}}),
        encoding="utf-8",
    )
    return package


def test_package_json_still_allow_lists_hal_package():
    """The 2026-11-25 incident: contract listed hal/, the allow-list did not."""

    assert "hal/" in _manifest()["files"]


def test_20261125_package_shape_is_rejected_with_real_sources():
    tracked = _tracked_files()
    packaged = [item for item in tracked if not item.startswith("hal/")]
    missing = missing_from_pack_list(tracked, packaged)
    assert missing, "a tarball without hal/ must be rejected"
    assert all(relative.startswith("hal/") for relative in missing)
    message = describe_missing(missing)
    assert "package 'hal'" in message
    assert '"hal/"' in message
    assert "no project source" in message


def test_missing_files_are_reported_with_readable_name():
    contract = ["hal/__init__.py", "hal/service.py", "hal/whiteboard.py", "orchestrator/bridge.py"]
    missing = missing_from_pack_list(contract, ["orchestrator/bridge.py"])
    assert missing == ("hal/__init__.py", "hal/service.py", "hal/whiteboard.py")
    assert "package 'hal'" in describe_missing(missing)


def test_sidecar_modules_are_not_required_in_the_tarball():
    contract = ["tools/windows_helper/runner.py", "tools/read_only.py"]
    assert missing_from_pack_list(contract, ["tools/read_only.py"]) == ()


def test_nested_pack_is_refused_instead_of_recursing(monkeypatch):
    monkeypatch.setenv(PACK_LIST_CHILD_ENV, "1")
    with pytest.raises(RuntimeError, match="nested npm pack"):
        npm_pack_files(ROOT)


def test_checker_cli_uses_a_captured_pack_list(tmp_path: Path):
    package = _fixture_package(tmp_path)
    complete = tmp_path / "complete.json"
    complete.write_text(
        json.dumps([{"files": [{"path": "hal/__init__.py"}, {"path": "hal/service.py"}]}]),
        encoding="utf-8",
    )
    passed = _run_cli(package, "--pack-list", str(complete))
    assert passed.returncode == 0, passed.stdout + passed.stderr

    dropped = tmp_path / "dropped.json"
    dropped.write_text(json.dumps([{"files": [{"path": "hal/__init__.py"}]}]), encoding="utf-8")
    failed = _run_cli(package, "--pack-list", str(dropped))
    assert failed.returncode == 1
    assert "package 'hal'" in failed.stdout
    assert "hal/service.py" in failed.stdout


@pytest.mark.skipif(NPM is None, reason="npm is required to compute the real tarball file list")
def test_real_npm_tarball_list_is_the_authority(tmp_path: Path):
    """A nested .npmignore drops a module that package.json's files entry allows."""

    package = _fixture_package(tmp_path)
    complete = _run_cli(package)
    assert complete.returncode == 0, complete.stdout + complete.stderr
    assert "ships every Function module package" in complete.stdout

    (package / "hal" / ".npmignore").write_text("service.py\n", encoding="utf-8")
    real_list = npm_pack_files(package)
    assert "hal/service.py" not in real_list, "npm must be the one deciding what is packed"

    dropped = _run_cli(package)
    assert dropped.returncode == 1
    assert "package 'hal'" in dropped.stdout
    assert "hal/service.py" in dropped.stdout

    # The opposite deviation: npm's own wildcard handling includes hal/service.py
    # for hal/**/service.py, so a list comparison must not report it missing.
    (package / "hal" / ".npmignore").unlink()
    (package / "package.json").write_text(
        json.dumps(
            {
                "name": "hashi-pack-guard-fixture",
                "version": "0.0.0",
                "files": ["hal/__init__.py", "hal/**/service.py"],
            }
        ),
        encoding="utf-8",
    )
    wildcard = _run_cli(package)
    assert wildcard.returncode == 0, wildcard.stdout + wildcard.stderr
# HASHI-Fingerprint: hfp_b7af581e5cc44523bb9ca2c9cdd168d1
