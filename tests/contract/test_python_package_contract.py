from __future__ import annotations

import json
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

import pytest

pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parents[2]


def _relative_members(names: list[str]) -> set[str]:
    members = set()
    for name in names:
        parts = PurePosixPath(name).parts
        if not parts:
            continue
        if parts[0].startswith("hashi_bridge-"):
            parts = parts[1:]
        if parts:
            members.add(PurePosixPath(*parts).as_posix())
    return members


def test_python_build_artifacts_match_the_nagare_flow_publication_boundary(
    tmp_path,
):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--no-isolation",
            "--outdir",
            str(tmp_path),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SetuptoolsDeprecationWarning" not in result.stdout + result.stderr

    [sdist] = tmp_path.glob("*.tar.gz")
    [wheel] = tmp_path.glob("*.whl")
    with tarfile.open(sdist, mode="r:gz") as archive:
        sdist_members = _relative_members(archive.getnames())
    with zipfile.ZipFile(wheel) as archive:
        wheel_members = set(archive.namelist())
        [entry_points_name] = [
            name for name in wheel_members if name.endswith(".dist-info/entry_points.txt")
        ]
        entry_points = archive.read(entry_points_name).decode("utf-8")

    required = {
        "nagare/__init__.py",
        "nagare/cli.py",
        "nagare/engine/runner.py",
        "flow/adapters/hashi.py",
        "flow/engine/flow_runner.py",
    }
    assert required <= sdist_members
    assert required <= wheel_members
    assert "[console_scripts]" in entry_points
    assert "nagare = nagare.cli:main" in entry_points

    forbidden_roots = {
        "docs",
        "exp",
        "packaging",
        "scripts",
        "skills",
        "superloops",
        "tests",
        "tools",
        "workbench",
        "workspaces",
    }
    sdist_leaks = {
        name
        for name in sdist_members
        if PurePosixPath(name).parts
        and PurePosixPath(name).parts[0] in forbidden_roots
    }
    wheel_leaks = {
        name
        for name in wheel_members
        if PurePosixPath(name).parts
        and PurePosixPath(name).parts[0] in forbidden_roots
    }
    assert sdist_leaks == set()
    assert wheel_leaks == set()

    install_dir = tmp_path / "installed"
    install = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--target",
            str(install_dir),
            str(wheel),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert install.returncode == 0, install.stdout + install.stderr

    workflow = tmp_path / "installed-smoke.yaml"
    workflow.write_text(
        """
workflow:
  id: installed-smoke
  name: Installed smoke
  version: 1.0.0
agents:
  orchestrator:
    id: flow-runner
  workers:
    - id: worker
      role: Deterministic smoke worker
      backend: callable
steps:
  - id: write
    name: Write
    agent: worker
    prompt: Write a smoke artifact.
    output:
      artifacts:
        - key: result
          path: result.txt
          type: text
""".strip()
        + "\n",
        encoding="utf-8",
    )
    runs_root = tmp_path / "installed-runs"
    output = tmp_path / "installed-result.json"
    bootstrap = """
import pathlib
import runpy
import sys

install_dir = pathlib.Path(sys.argv.pop(1)).resolve()
sys.path.insert(0, str(install_dir))

import nagare
import flow.engine.flow_runner as flow_runner

assert pathlib.Path(nagare.__file__).resolve().is_relative_to(install_dir)
assert pathlib.Path(flow_runner.__file__).resolve().is_relative_to(install_dir)
runpy.run_module("nagare.cli", run_name="__main__")
"""
    smoke = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            bootstrap,
            str(install_dir),
            "run",
            str(workflow),
            "--yes",
            "--silent",
            "--smoke-handler",
            "--runs-root",
            str(runs_root),
            "--repo-root",
            str(tmp_path),
            "--output",
            str(output),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert smoke.returncode == 0, smoke.stdout + smoke.stderr
    smoke_result = json.loads(output.read_text(encoding="utf-8"))
    assert smoke_result["success"] is True
    assert (runs_root / smoke_result["run_id"] / "state.json").is_file()
