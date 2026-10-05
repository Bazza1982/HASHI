from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _node(script, *args, environment=None):
    node = shutil.which("node")
    assert node, "Node is required for the installed deployment contract"
    result = subprocess.run(
        [node, "-e", script, *map(str, args)], cwd=ROOT,
        env=environment, capture_output=True, text=True, check=False, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return result


_PREPARATION = r"""
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const cp = require('child_process');
const root = process.argv[1];
const versionRoot = process.argv[2];
const fault = process.argv[3];
const calls = [];
cp.spawnSync = (command, args, options) => {
  calls.push({command, args});
  if (!args.some(x => String(x).endsWith('provision_transcription_runtime.py'))) {
    return {status: 0};
  }
  if (args.includes('--check')) return {status: fault === 'check' ? 1 : 0};
  if (fault === 'install') return {status: 1};
  const runtime = args[args.indexOf('--runtime-dir') + 1];
  const home = args[args.indexOf('--bridge-home') + 1];
  const python = path.join(runtime, process.platform === 'win32' ? 'Scripts' : 'bin',
    process.platform === 'win32' ? 'python.exe' : 'python');
  fs.mkdirSync(path.dirname(python), {recursive: true});
  fs.writeFileSync(python, 'isolated');
  const config = path.join(home, 'state', 'platform', 'transcription.json');
  fs.mkdirSync(path.dirname(config), {recursive: true});
  fs.writeFileSync(config, JSON.stringify({
    schema_version: 1, python, runtime_dir: runtime,
    lock_sha256: fault === 'receipt' ? 'wrong' : crypto.createHash('sha256').update(
      fs.readFileSync(path.join(root, 'constraints', 'transcription-py312.lock'))
    ).digest('hex'),
    python_version: '3.12.13',
    packages: {'faster-whisper':'1.2.1', ctranslate2:'4.7.1', av:'17.0.0'},
  }));
  return {status: 0};
};
const setup = require(path.join(root, 'postinstall.js'));
const output = setup.prepareTranscription({command:'/approved/base-python',prefix:[]}, versionRoot);
console.log('RESULT=' + JSON.stringify({output, calls,
  pointer: fs.existsSync(path.join(versionRoot, 'transcription-active.json'))
    ? JSON.parse(fs.readFileSync(path.join(versionRoot, 'transcription-active.json'))) : null,
}));
"""


def _result(result):
    return json.loads(next(line[7:] for line in result.stdout.splitlines()
                           if line.startswith("RESULT=")))


def test_default_npm_preparation_runs_locked_sidecar_then_checks_before_publish(tmp_path):
    version_root = tmp_path / "runtimes" / "version"
    data = _result(_node(_PREPARATION, ROOT, version_root, "none"))
    assert data["output"] is True
    assert len(data["calls"]) == 2
    install, check = data["calls"]
    assert install["command"] == check["command"] == "/approved/base-python"
    assert "--check" not in install["args"] and "--check" in check["args"]
    for call in data["calls"]:
        assert "--runtime-dir" in call["args"] and "--bridge-home" in call["args"]
        assert not {"pip", "install", "-m"}.intersection(call["args"])
    pointer = data["pointer"]
    assert Path(pointer["python"]).is_file()
    assert pointer["lock_sha256"] == hashlib.sha256(
        (ROOT / "constraints/transcription-py312.lock").read_bytes()).hexdigest()
    assert Path(pointer["runtime_dir"]).parent == version_root
    assert not (tmp_path / "instances").exists()


@pytest.mark.parametrize("fault", ["install", "check", "receipt"])
def test_failed_sidecar_preparation_preserves_previous_selection(tmp_path, fault):
    version_root = tmp_path / "runtimes" / "version"
    version_root.mkdir(parents=True)
    previous = {"python": "previous-private-runtime"}
    (version_root / "transcription-active.json").write_text(json.dumps(previous))
    data = _result(_node(_PREPARATION, ROOT, version_root, fault))
    assert data["output"] is False
    assert data["pointer"] == previous
    assert not list(version_root.glob("transcription-*/bin/python"))


def test_explicit_transcription_optout_never_installs_or_publishes(tmp_path):
    env = os.environ.copy()
    env["HASHI_POSTINSTALL_NO_TRANSCRIPTION"] = "1"
    root = tmp_path / "runtimes"
    data = _result(_node(_PREPARATION, ROOT, root, "none", environment=env))
    assert data["output"] is False and data["calls"] == []
    assert data["pointer"] is None and not root.exists()


def test_npm_default_main_calls_sidecar_for_already_prepared_core(tmp_path):
    version = json.loads((ROOT / "package.json").read_text())["version"]
    data_root = tmp_path / "data"
    version_root = data_root / "runtimes" / version
    python = version_root / "build-ready" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("fake prepared core")
    (version_root / "active.json").write_text(json.dumps({
        "schema_version": 1, "program_version": version, "python": str(python)}))
    script = _PREPARATION[:_PREPARATION.index("const setup =")] + r"""
process.env.HASHI_DATA_ROOT = process.argv[4];
process.env.HASHI_POSTINSTALL_NO_PRIVACY = '1';
const setup = require(path.join(root, 'postinstall.js'));
const output = setup.main();
console.log('RESULT=' + JSON.stringify({output, calls,
  pointer: fs.existsSync(path.join(versionRoot, 'transcription-active.json'))}));
"""
    data = _result(_node(script, ROOT, version_root, "none", data_root))
    assert data["output"] == 0 and data["pointer"] is True
    assert len([c for c in data["calls"] if any(
        str(a).endswith("provision_transcription_runtime.py") for a in c["args"])]) == 2


@pytest.mark.parametrize("fault", ["none", "outside", "version", "lock", "symlink"])
def test_transcription_pointer_requires_owned_generation_version_and_lock(tmp_path, fault):
    version = json.loads((ROOT / "package.json").read_text())["version"]
    version_root = tmp_path / "runtimes" / version
    runtime = version_root / "transcription-ready"
    if fault == "symlink":
        outside = tmp_path / "outside"
        outside.mkdir()
        version_root.mkdir(parents=True)
        runtime.symlink_to(outside, target_is_directory=True)
    python = runtime / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python.parent.mkdir(parents=True)
    python.write_bytes(b"isolated")
    pointer = {
        "schema_version": 1, "program_version": version, "python": str(python),
        "runtime_dir": str(runtime),
        "lock_sha256": hashlib.sha256((ROOT / "constraints/transcription-py312.lock").read_bytes()).hexdigest(),
    }
    if fault == "outside":
        pointer["python"] = str(tmp_path / "foreign" / "python")
    elif fault == "version":
        pointer["program_version"] = "future-unadopted"
    elif fault == "lock":
        pointer["lock_sha256"] = "0" * 64
    (version_root / "transcription-active.json").write_text(json.dumps(pointer))
    script = r"""
const setup = require(process.argv[1]);
console.log('RESULT=' + JSON.stringify({python: setup.preparedTranscriptionPython(process.argv[2], process.argv[3])}));
"""
    selected = _result(_node(script, ROOT / "transcription-runtime.js", version_root, version))["python"]
    assert selected == (str(python) if fault == "none" else "")


def test_npm_cli_passes_verified_sidecar_to_selected_instance_process(tmp_path):
    script = r"""
const cp = require('child_process');
let selected;
cp.spawnSync = () => ({status:0});
cp.spawn = (command,args,options) => {
  selected = {command,args,env: options.env};
  return {on:()=>{},kill:()=>{}};
};
process.env.HASHI_TRANSCRIPTION_PYTHON = process.argv[2];
const cli = require(process.argv[1]);
cli.run(['status','--instance','alpha','--json']);
console.log('RESULT=' + JSON.stringify({python: selected.env.HASHI_TRANSCRIPTION_PYTHON,
  args:selected.args}));
"""
    python = tmp_path / "isolated-transcription-python"
    data = _result(_node(script, ROOT / "cli.js", python))
    assert data["python"] == str(python)
    assert data["args"][-4:] == ["--instance", "alpha", "--json", "status"]


@pytest.mark.parametrize("platform", ["native", "wsl"])
def test_task_deployment_prepares_and_checks_selected_sidecar_before_registration(platform):
    installer = (ROOT / f"packaging/windows/install-{platform}-hashi-user-runtime.ps1").read_text()
    assert "provision_transcription_runtime.py" in installer
    assert "--bridge-home" in installer and "--check" in installer
    assert "$SkipTranscription" in installer
    provision = installer.index("if (-not $SkipTranscription)")
    # WhatIf remains side-effect free; registration cannot claim success before
    # the selected instance's actual isolated dependency probe succeeded.
    assert installer.index("$PSCmdlet.ShouldProcess(") < provision
    assert provision < installer.index("Register-ScheduledTask")
    assert "Transcription runtime preparation failed" in installer



def test_npm_cli_propagates_owned_default_pointer_without_explicit_override(tmp_path):
    version = json.loads((ROOT / "package.json").read_text())["version"]
    data_root = tmp_path / "data"
    runtime = data_root / "runtimes" / version / "transcription-ready"
    python = runtime / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python.parent.mkdir(parents=True)
    python.write_bytes(b"isolated")
    (runtime.parent / "transcription-active.json").write_text(json.dumps({
        "schema_version": 1, "program_version": version, "python": str(python),
        "runtime_dir": str(runtime),
        "lock_sha256": hashlib.sha256((ROOT / "constraints/transcription-py312.lock").read_bytes()).hexdigest(),
    }))
    script = r"""
const cp = require('child_process');
let selected;
cp.spawnSync = () => ({status:0});
cp.spawn = (command,args,options) => {
  selected = options.env;
  return {on:()=>{},kill:()=>{}};
};
delete process.env.HASHI_TRANSCRIPTION_PYTHON;
process.env.HASHI_DATA_ROOT = process.argv[2];
const cli = require(process.argv[1]);
cli.run(['status','--instance','alpha','--json']);
console.log('RESULT=' + JSON.stringify({python:selected.HASHI_TRANSCRIPTION_PYTHON}));
"""
    data = _result(_node(script, ROOT / "cli.js", data_root))
    assert data["python"] == str(python)


def test_already_prepared_sidecar_is_probed_without_a_second_install(tmp_path):
    version = json.loads((ROOT / "package.json").read_text())["version"]
    version_root = tmp_path / "runtimes" / version
    first = _result(_node(_PREPARATION, ROOT, version_root, "none"))
    pointer = first["pointer"]
    second = _result(_node(_PREPARATION, ROOT, version_root, "none"))
    assert second["output"] is True and second["pointer"] == pointer
    assert len(second["calls"]) == 1
    assert "--check" in second["calls"][0]["args"]


def test_fresh_npm_core_generation_also_prepares_sidecar(tmp_path):
    version = json.loads((ROOT / "package.json").read_text())["version"]
    data_root = tmp_path / "data"
    version_root = data_root / "runtimes" / version
    script = _PREPARATION[:_PREPARATION.index("const setup =")] + r"""
const previousSpawn = cp.spawnSync;
cp.spawnSync = (command, args, options) => {
  if (args[0] === '-m' && args[1] === 'venv') {
    const runtime = args[2];
    const python = path.join(runtime, process.platform === 'win32' ? 'Scripts' : 'bin',
      process.platform === 'win32' ? 'python.exe' : 'python');
    fs.mkdirSync(path.dirname(python), {recursive:true});
    fs.writeFileSync(python, 'prepared core');
  }
  return previousSpawn(command, args, options);
};
process.env.HASHI_DATA_ROOT = process.argv[4];
process.env.HASHI_POSTINSTALL_NO_PRIVACY = '1';
const setup = require(path.join(root, 'postinstall.js'));
const output = setup.main();
console.log('RESULT=' + JSON.stringify({output,calls,
  pointer: JSON.parse(fs.readFileSync(path.join(versionRoot, 'transcription-active.json'))),
  core: JSON.parse(fs.readFileSync(path.join(versionRoot, 'active.json')))}));
"""
    data = _result(_node(script, ROOT, version_root, "none", data_root))
    assert data["output"] == 0
    assert data["core"]["python"] != data["pointer"]["python"]
    native_installs = [c for c in data["calls"] if "pip" in c["args"]]
    assert len(native_installs) == 1
    assert native_installs[0]["args"][-1].endswith("standard-py312.lock")
    sidecar = [c for c in data["calls"] if any(
        str(a).endswith("provision_transcription_runtime.py") for a in c["args"])]
    assert len(sidecar) == 2
