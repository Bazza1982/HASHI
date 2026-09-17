from __future__ import annotations

import json
import subprocess

import pytest

from scripts import provision_transcription_runtime as provisioner


def test_runtime_target_cannot_be_the_active_hashi_environment(tmp_path):
    active_prefix = tmp_path / "core-venv"

    with pytest.raises(provisioner.ProvisioningError, match="Core environment"):
        provisioner.validate_runtime_target(active_prefix, active_prefix=active_prefix)

    with pytest.raises(provisioner.ProvisioningError, match="Core environment"):
        provisioner.validate_runtime_target(
            active_prefix / "speech",
            active_prefix=active_prefix,
        )


def test_runtime_python_path_does_not_dereference_a_venv_symlink(tmp_path):
    runtime_dir = tmp_path / "speech"
    python = provisioner.runtime_python_path(runtime_dir)
    python.parent.mkdir(parents=True)
    base_python = tmp_path / "base-python"
    base_python.write_bytes(b"python")
    try:
        python.symlink_to(base_python)
    except OSError:
        pytest.skip("this platform does not permit an unprivileged symlink")

    assert provisioner._absolute_path(python) == python.absolute()
    assert provisioner._absolute_path(python) != python.resolve()


def test_transcription_lock_must_pin_every_probed_distribution(tmp_path):
    lock = tmp_path / "transcription.lock"
    lock.write_text("faster-whisper==1.2.1\n", encoding="utf-8")

    with pytest.raises(provisioner.ProvisioningError, match="ctranslate2"):
        provisioner._parse_lock_versions(lock)


def test_provisioner_installs_and_probes_only_the_isolated_runtime(tmp_path, monkeypatch):
    bridge_home = tmp_path / "instance"
    runtime_dir = bridge_home / "state" / "runtimes" / "transcription" / "test"
    base_python = tmp_path / "python312" / "python"
    base_python.parent.mkdir(parents=True)
    base_python.write_bytes(b"python")
    lock = tmp_path / "transcription.lock"
    lock.write_text(
        "faster-whisper==1.2.1\nctranslate2==4.7.1\nav==17.0.0\n",
        encoding="utf-8",
    )
    commands: list[list[str]] = []

    def run(command, **kwargs):
        command = [str(item) for item in command]
        commands.append(command)
        if command[1:3] == ["-m", "venv"]:
            isolated_python = provisioner.runtime_python_path(runtime_dir)
            isolated_python.parent.mkdir(parents=True, exist_ok=True)
            isolated_python.write_bytes(b"isolated-python")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if "--probe" in command:
            payload = {
                "status": "ok",
                "python": "3.12.13",
                "packages": {
                    "faster-whisper": "1.2.1",
                    "ctranslate2": "4.7.1",
                    "av": "17.0.0",
                },
            }
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=provisioner.TRANSCRIPTION_RESULT_PREFIX + json.dumps(payload),
                stderr="",
            )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(provisioner.subprocess, "run", run)

    receipt = provisioner.provision_runtime(
        bridge_home=bridge_home,
        runtime_dir=runtime_dir,
        base_python=base_python,
        lock_path=lock,
    )

    isolated_python = provisioner.runtime_python_path(runtime_dir).resolve()
    assert commands[0] == [str(base_python.resolve()), "-m", "venv", str(runtime_dir.resolve())]
    assert commands[1][:4] == [
        str(isolated_python),
        "-m",
        "pip",
        "install",
    ]
    assert commands[2][0] == str(isolated_python)
    assert commands[2][1] == "-I"
    assert "--probe" in commands[2]
    assert receipt["python"] == str(isolated_python)

    config_path = bridge_home / "state" / "platform" / "transcription.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["python"] == str(isolated_python)
    assert config["lock_sha256"] == provisioner.sha256_file(lock)


def test_failed_probe_does_not_publish_platform_config(tmp_path, monkeypatch):
    bridge_home = tmp_path / "instance"
    runtime_dir = bridge_home / "state" / "runtimes" / "transcription" / "test"
    isolated_python = provisioner.runtime_python_path(runtime_dir)
    isolated_python.parent.mkdir(parents=True)
    isolated_python.write_bytes(b"isolated-python")
    base_python = tmp_path / "python"
    base_python.write_bytes(b"python")
    lock = tmp_path / "transcription.lock"
    lock.write_text(
        "faster-whisper==1.2.1\nctranslate2==4.7.1\nav==17.0.0\n",
        encoding="utf-8",
    )

    def run(command, **kwargs):
        if "--probe" in [str(item) for item in command]:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="missing av")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(provisioner.subprocess, "run", run)

    with pytest.raises(provisioner.ProvisioningError, match="probe"):
        provisioner.provision_runtime(
            bridge_home=bridge_home,
            runtime_dir=runtime_dir,
            base_python=base_python,
            lock_path=lock,
        )

    assert not (bridge_home / "state" / "platform" / "transcription.json").exists()
