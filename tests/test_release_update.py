from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import hashi_instance_cli
from tools import release_update
from tools.release_update import (
    LocalReleaseAdapter,
    ReleaseUpdateError,
    complete_runtime_release,
    fast_forward_checkout,
    validate_release_payload,
)


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _source_repository(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-b", "main")
    _git(source, "config", "user.email", "release-test@example.invalid")
    _git(source, "config", "user.name", "Release Test")
    (source / "version.txt").write_text("one\n", encoding="utf-8")
    _git(source, "add", "version.txt")
    _git(source, "commit", "-m", "initial")
    deployed = tmp_path / "deployed"
    subprocess.run(
        ["git", "clone", str(source), str(deployed)],
        check=True,
        capture_output=True,
        text=True,
    )
    return source, deployed


def _matching_payload(commit: str) -> dict:
    provenance = {"commit": commit}
    return {
        "ok": True,
        "instance": {"id": "HASHI3"},
        "running": {
            "commit": commit,
            "functions": {"provenance": provenance},
            "workers": [
                {
                    "agent": "agent1",
                    "phase": "ACTIVE",
                    "accepting": True,
                    "provenance": provenance,
                },
                {
                    "agent": "agent2",
                    "phase": "ACTIVE",
                    "accepting": True,
                    "provenance": provenance,
                },
            ],
            "remote": {
                "status": "active",
                "provenance": provenance,
            },
        },
        "source": {
            "available": True,
            "commit": commit,
            "dirty": False,
        },
        "state": {"code": "running_matches_source", "reasons": []},
    }


def test_update_command_is_one_closed_release_operation():
    args = hashi_instance_cli._parser().parse_args(
        ["--instance", "alpha", "update", "--confirm", "alpha"]
    )

    assert args.command == "update"
    assert args.confirm == "alpha"
    assert args.remote == "origin"
    assert args.branch == "main"


def test_runtime_handoff_is_not_owned_by_the_core_restart_controller(
    tmp_path: Path,
):
    argv = release_update._runtime_release_argv(
        runtime_command=["python"],
        code_root=tmp_path,
        release_home=tmp_path,
        instance_id="HASHI3",
        api_port=18804,
        expected_commit="a" * 40,
        timeout=300,
    )

    assert "--bridge-home" not in argv
    assert "--release-home" in argv
    deploy_index = argv.index("deploy")
    args = release_update._parser().parse_args(argv[deploy_index:])
    assert args.bridge_home == tmp_path


def test_fast_forward_checkout_updates_exact_fetched_main(tmp_path: Path):
    source, deployed = _source_repository(tmp_path)
    (source / "version.txt").write_text("two\n", encoding="utf-8")
    _git(source, "add", "version.txt")
    _git(source, "commit", "-m", "second")
    target = _git(source, "rev-parse", "HEAD")

    result = fast_forward_checkout(deployed, remote="origin", branch="main")

    assert result["changed"] is True
    assert result["target_commit"] == target
    assert _git(deployed, "rev-parse", "HEAD") == target
    assert _git(deployed, "status", "--porcelain") == ""


def test_fast_forward_checkout_refuses_dirty_source(tmp_path: Path):
    _source, deployed = _source_repository(tmp_path)
    (deployed / "version.txt").write_text("local change\n", encoding="utf-8")

    with pytest.raises(ReleaseUpdateError, match="clean"):
        fast_forward_checkout(deployed, remote="origin", branch="main")


def test_version_gate_requires_every_component_to_match_target():
    target = "a" * 40
    payload = _matching_payload(target)

    assert (
        validate_release_payload(
            payload, expected_commit=target, expected_instance="HASHI3"
        )
        == []
    )

    payload["running"]["remote"]["provenance"] = {"commit": "b" * 40}
    issues = validate_release_payload(
        payload, expected_commit=target, expected_instance="HASHI3"
    )

    assert "remote_commit_mismatch" in issues


def test_version_gate_rejects_offline_remote_even_if_state_is_green():
    target = "a" * 40
    payload = _matching_payload(target)
    payload["running"]["remote"] = {
        "status": "offline",
        "provenance": None,
    }

    issues = validate_release_payload(
        payload, expected_commit=target, expected_instance="HASHI3"
    )

    assert "remote_not_active" in issues
    assert "remote_provenance_missing" in issues


def test_version_gate_requires_the_complete_preflight_worker_set():
    target = "a" * 40
    payload = _matching_payload(target)

    issues = validate_release_payload(
        payload,
        expected_commit=target,
        expected_instance="HASHI3",
        expected_agents=["agent1", "agent2", "agent3"],
    )

    assert "worker_set_mismatch" in issues


def test_runtime_release_restarts_core_then_remote_before_final_gate():
    target = "a" * 40

    class Adapter:
        def __init__(self):
            self.events = []

        def preflight(self):
            self.events.append("preflight")
            return {"remote": "ready"}

        def restart_core(self, expected_commit):
            self.events.append(("restart_core", expected_commit))
            return {"restart_id": "rst_test", "state": "completed"}

        def restart_remote(self):
            self.events.append("restart_remote")
            return {"controller": "reloaded"}

        def wait_for_version(self, expected_commit):
            self.events.append(("version_gate", expected_commit))
            return _matching_payload(expected_commit)

    adapter = Adapter()
    result = complete_runtime_release(
        adapter,
        expected_commit=target,
        expected_instance="HASHI3",
    )

    assert result["ok"] is True
    assert result["target_commit"] == target
    assert adapter.events == [
        "preflight",
        ("restart_core", target),
        "restart_remote",
        ("version_gate", target),
    ]


def test_runtime_release_fails_when_remote_restarts_old_code():
    target = "a" * 40

    class Adapter:
        def preflight(self):
            return {}

        def restart_core(self, _expected_commit):
            return {"restart_id": "rst_test", "state": "completed"}

        def restart_remote(self):
            return {}

        def wait_for_version(self, expected_commit):
            payload = _matching_payload(expected_commit)
            payload["running"]["remote"]["provenance"] = {"commit": "b" * 40}
            return payload

    with pytest.raises(ReleaseUpdateError, match="remote_commit_mismatch"):
        complete_runtime_release(
            Adapter(),
            expected_commit=target,
            expected_instance="HASHI3",
        )


def test_windows_remote_is_reregistered_before_it_is_restarted(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(release_update, "load_shared_token", lambda _root: "token")
    monkeypatch.setattr(release_update.shutil, "which", lambda _name: "powershell.exe")
    calls = []

    def run(argv, **options):
        calls.append((list(argv), options))
        return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(release_update, "_run", run)
    adapter = LocalReleaseAdapter(
        code_root=tmp_path,
        bridge_home=tmp_path,
        instance_id="HASHI3",
        api_port=18804,
        expected_commit="a" * 40,
        python_executable="python.exe",
        platform_name="nt",
    )

    result = adapter.restart_remote()

    assert result == {
        "controller": "windows_scheduled_task",
        "re_registered": True,
    }
    assert [call[0][7] for call in calls] == ["register", "restart"]
    assert all("-InstanceId" in call[0] for call in calls)
