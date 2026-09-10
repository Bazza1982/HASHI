from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def node() -> str:
    executable = shutil.which("node")
    assert executable is not None, "Node.js is required for the npm CLI contract"
    return executable


def test_help_is_available_without_python_and_does_not_restore_workbench(node):
    result = subprocess.run(
        [node, str(ROOT / "cli.js"), "help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "instance" in result.stdout
    assert "ui" in result.stdout
    assert "hashi workbench" not in result.stdout.casefold()


def test_node_wrapper_accepts_global_selector_after_command(node):
    script = """
const cli = require(process.argv[1]);
process.stdout.write(JSON.stringify(cli.normalizeGlobalArguments([
  'status', '--instance', 'alpha', '--json'
])));
"""
    result = subprocess.run(
        [node, "-e", script, str(ROOT / "cli.js")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        "--instance",
        "alpha",
        "--json",
        "status",
    ]


def test_node_wrapper_rejects_the_other_os_runtime_path(node):
    script = r"""
const cli = require(process.argv[1]);
process.stdout.write(JSON.stringify({
  windows: cli.sameEnvironmentCommand('C:\\Python312\\python.exe'),
  wsl: cli.sameEnvironmentCommand('\\\\wsl.localhost\\Ubuntu-22.04\\usr\\bin\\python3'),
  posix: cli.sameEnvironmentCommand('/usr/bin/python3.12'),
}));
"""
    result = subprocess.run(
        [node, "-e", script, str(ROOT / "cli.js")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    values = json.loads(result.stdout)
    if os.name == "nt":
        assert values["windows"] is True
        assert values["wsl"] is False
    else:
        assert values["windows"] is False
        assert values["posix"] is True


def test_postinstall_disabled_path_reports_incomplete_without_creating_data(
    node, tmp_path
):
    data_root = tmp_path / "data"
    environment = os.environ.copy()
    environment.update(
        {
            "HASHI_DATA_ROOT": str(data_root),
            "HASHI_POSTINSTALL_NO_PREPARE": "1",
        }
    )

    result = subprocess.run(
        [node, str(ROOT / "postinstall.js")],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "runtime setup is incomplete" in result.stderr
    assert "runtime is ready" not in (result.stdout + result.stderr)
    assert not data_root.exists()


def test_postinstall_pointer_failure_preserves_previous_runtime_selection(
    node, tmp_path
):
    target = tmp_path / "active.json"
    target.write_text('{"python":"previous"}\n', encoding="utf-8")
    script = """
const fs = require('fs');
const setup = require(process.argv[1]);
const target = process.argv[2];
const cyclic = {};
cyclic.self = cyclic;
try { setup.atomicJson(target, cyclic); } catch (_) { /* expected */ }
process.stdout.write(fs.readFileSync(target, 'utf8'));
"""

    result = subprocess.run(
        [node, "-e", script, str(ROOT / "postinstall.js"), str(target)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"python": "previous"}


def test_postinstall_rename_failure_restores_previous_runtime_selection(node, tmp_path):
    target = tmp_path / "active.json"
    target.write_text('{"python":"previous"}\n', encoding="utf-8")
    script = """
const fs = require('fs');
const setup = require(process.argv[1]);
const target = process.argv[2];
const realRename = fs.renameSync;
let calls = 0;
fs.renameSync = (source, destination) => {
  calls += 1;
  if (calls === 1 || calls === 3) {
    const error = new Error('synthetic rename failure');
    error.code = 'EPERM';
    throw error;
  }
  return realRename(source, destination);
};
try { setup.atomicJson(target, {python: 'replacement'}); } catch (_) { /* expected */ }
fs.renameSync = realRename;
process.stdout.write(fs.readFileSync(target, 'utf8'));
"""

    result = subprocess.run(
        [node, "-e", script, str(ROOT / "postinstall.js"), str(target)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"python": "previous"}


def test_prepared_runtime_pointer_accepts_only_this_version_directory(node, tmp_path):
    version = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["version"]
    version_root = tmp_path / "runtimes" / version
    inside = version_root / "build-owned" / (
        "python.exe" if os.name == "nt" else "python"
    )
    outside = tmp_path / ("python.exe" if os.name == "nt" else "python")
    inside.parent.mkdir(parents=True)
    inside.write_bytes(b"")
    outside.write_bytes(b"")
    script = """
const setup = require(process.argv[1]);
const root = process.argv[2];
const version = process.argv[3];
const inside = process.argv[4];
const outside = process.argv[5];
const payload = (python) => ({schema_version: 1, program_version: version, python});
process.stdout.write(JSON.stringify({
  inside: setup.preparedRuntimeCandidate(root, payload(inside)),
  outside: setup.preparedRuntimeCandidate(root, payload(outside)),
  wrongVersion: setup.preparedRuntimeCandidate(
    root,
    {schema_version: 1, program_version: 'different', python: inside},
  ),
}));
"""

    result = subprocess.run(
        [
            node,
            "-e",
            script,
            str(ROOT / "postinstall.js"),
            str(version_root),
            version,
            str(inside),
            str(outside),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "inside": str(inside.resolve()),
        "outside": "",
        "wrongVersion": "",
    }


def test_postinstall_cleanup_is_bounded_to_its_exact_version_directory(
    node, tmp_path
):
    version_root = tmp_path / "runtimes" / "test-version"
    build = version_root / "build-owned"
    outside = tmp_path / "outside"
    build.mkdir(parents=True)
    outside.mkdir()
    (build / "partial.txt").write_text("partial", encoding="utf-8")
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    script = """
const setup = require(process.argv[1]);
setup.safeRemoveBuild(process.argv[2], process.argv[4]);
setup.safeRemoveBuild(process.argv[2], process.argv[3]);
"""

    result = subprocess.run(
        [
            node,
            "-e",
            script,
            str(ROOT / "postinstall.js"),
            str(version_root),
            str(build),
            str(outside),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not build.exists()
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_postinstall_rejects_instance_data_inside_program_directory(node, tmp_path):
    program = tmp_path / "program"
    nested = program / "instance-data"
    outside = tmp_path / "instance-data"
    script = """
const setup = require(process.argv[1]);
process.stdout.write(JSON.stringify({
  nested: setup.isInsideOrEqual(process.argv[2], process.argv[3]),
  outside: setup.isInsideOrEqual(process.argv[2], process.argv[4]),
}));
"""

    result = subprocess.run(
        [
            node,
            "-e",
            script,
            str(ROOT / "postinstall.js"),
            str(program),
            str(nested),
            str(outside),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"nested": True, "outside": False}


def test_node_to_python_instance_list_is_read_only(node, tmp_path):
    environment = os.environ.copy()
    environment.update(
        {
            "HASHI_PYTHON": sys.executable,
            "HASHI_REGISTRY_ROOT": str(tmp_path / "registry"),
            "HASHI_DATA_ROOT": str(tmp_path / "data"),
        }
    )

    result = subprocess.run(
        [node, str(ROOT / "cli.js"), "instance", "list", "--json"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["data"] == {"instances": []}
    assert not (tmp_path / "registry").exists()
    assert not (tmp_path / "data").exists()
