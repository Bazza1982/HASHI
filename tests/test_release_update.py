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
    force_align_checkout,
    release_failure,
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


def test_version_gate_accepts_requalified_commit_for_reused_function_bytes():
    target = "a" * 40
    artifact_commit = "b" * 40
    payload = _matching_payload(target)
    payload["running"]["functions"]["source_commit"] = target
    payload["running"]["functions"]["provenance"] = {"commit": artifact_commit}
    for worker in payload["running"]["workers"]:
        worker["source_commit"] = target
        worker["provenance"] = {"commit": artifact_commit}

    assert (
        validate_release_payload(
            payload,
            expected_commit=target,
            expected_instance="HASHI3",
        )
        == []
    )


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
# HASHI-Fingerprint: hfp_c24fb81627c64ef9bb853b13e8536902


def test_fast_forward_checkout_returns_a_feature_checkout_to_main(tmp_path: Path):
    source, deployed = _source_repository(tmp_path)
    _git(deployed, "checkout", "-b", "fix/cleanup")
    (source / "version.txt").write_text("two\n", encoding="utf-8")
    _git(source, "add", "version.txt")
    _git(source, "commit", "-m", "second")
    target = _git(source, "rev-parse", "HEAD")

    result = fast_forward_checkout(deployed, remote="origin", branch="main")

    assert result["previous_branch"] == "fix/cleanup"
    assert result["switched_to_branch"] is True
    assert result["changed"] is True
    assert _git(deployed, "rev-parse", "HEAD") == target
    assert _git(deployed, "rev-parse", "--abbrev-ref", "HEAD") == "main"


def test_fast_forward_checkout_names_the_diverged_branch_and_keeps_it(tmp_path: Path):
    source, deployed = _source_repository(tmp_path)
    _git(deployed, "checkout", "-b", "topic/local")
    (deployed / "local.txt").write_text("mine\n", encoding="utf-8")
    _git(deployed, "add", "local.txt")
    _git(deployed, "commit", "-m", "local commit")
    local = _git(deployed, "rev-parse", "HEAD")
    (source / "version.txt").write_text("two\n", encoding="utf-8")
    _git(source, "add", "version.txt")
    _git(source, "commit", "-m", "second")
    target = _git(source, "rev-parse", "HEAD")

    with pytest.raises(ReleaseUpdateError) as raised:
        fast_forward_checkout(deployed, remote="origin", branch="main")

    failure = release_failure(raised.value)
    assert failure["code"] == "not_fast_forward"
    assert failure["source_changed"] is False
    assert failure["previous_branch"] == "topic/local"
    assert failure["previous_commit"] == local
    assert failure["target_commit"] == target
    assert _git(deployed, "rev-parse", "HEAD") == local


def test_force_align_checkout_stashes_local_work_and_backs_up_main(tmp_path: Path):
    source, deployed = _source_repository(tmp_path)
    (deployed / "local.txt").write_text("committed only here\n", encoding="utf-8")
    _git(deployed, "add", "local.txt")
    _git(deployed, "commit", "-m", "local only")
    old = _git(deployed, "rev-parse", "HEAD")
    (deployed / "version.txt").write_text("local edit\n", encoding="utf-8")
    (deployed / "scratch.txt").write_text("untracked\n", encoding="utf-8")
    (source / "version.txt").write_text("two\n", encoding="utf-8")
    _git(source, "add", "version.txt")
    _git(source, "commit", "-m", "second")
    target = _git(source, "rev-parse", "HEAD")

    result = force_align_checkout(deployed, remote="origin", branch="main")

    assert result["forced"] is True
    assert result["previous_branch"] == "main"
    assert result["old_commit"] == old
    assert _git(deployed, "rev-parse", "HEAD") == target
    assert _git(deployed, "status", "--porcelain") == ""
    assert result["stashed_dirty_count"] == 2
    assert result["stash_commit"]
    assert result["backup_ref"].startswith("refs/hashideploy-backups/")
    assert _git(deployed, "rev-parse", result["backup_ref"]) == old
    assert result["stash_message"] in _git(deployed, "stash", "list")
    assert "local edit" in _git(deployed, "show", "stash@{0}:version.txt")


def test_force_align_checkout_keeps_a_feature_branch_pointer(tmp_path: Path):
    source, deployed = _source_repository(tmp_path)
    _git(deployed, "checkout", "-b", "wip/experiment")
    (deployed / "experiment.txt").write_text("unmerged work\n", encoding="utf-8")
    _git(deployed, "add", "experiment.txt")
    _git(deployed, "commit", "-m", "unmerged experiment")
    old = _git(deployed, "rev-parse", "HEAD")
    (deployed / "scratch.txt").write_text("untracked\n", encoding="utf-8")
    (source / "version.txt").write_text("two\n", encoding="utf-8")
    _git(source, "add", "version.txt")
    _git(source, "commit", "-m", "second")
    target = _git(source, "rev-parse", "HEAD")

    result = force_align_checkout(deployed, remote="origin", branch="main")

    assert result["previous_branch"] == "wip/experiment"
    assert _git(deployed, "rev-parse", result["backup_ref"]) == old
    assert _git(deployed, "rev-parse", "wip/experiment") == old
    assert _git(deployed, "rev-parse", "HEAD") == target


def test_force_align_checkout_leaves_everything_untouched_when_fetch_fails(tmp_path: Path):
    _source, deployed = _source_repository(tmp_path)
    (deployed / "version.txt").write_text("local edit\n", encoding="utf-8")
    old = _git(deployed, "rev-parse", "HEAD")
    _git(deployed, "remote", "set-url", "origin", str(tmp_path / "missing-remote"))

    with pytest.raises(ReleaseUpdateError) as raised:
        force_align_checkout(deployed, remote="origin", branch="main")

    failure = release_failure(raised.value)
    assert failure["code"] == "fetch_failed"
    assert failure["source_changed"] is False
    assert _git(deployed, "rev-parse", "HEAD") == old
    assert "local edit" in (deployed / "version.txt").read_text(encoding="utf-8")
    assert _git(deployed, "stash", "list") == ""


def test_force_align_checkout_does_not_stash_when_clean(tmp_path: Path):
    source, deployed = _source_repository(tmp_path)
    (source / "version.txt").write_text("two\n", encoding="utf-8")
    _git(source, "add", "version.txt")
    _git(source, "commit", "-m", "second")

    result = force_align_checkout(deployed, remote="origin", branch="main")

    assert result["stash_commit"] is None
    assert result["stashed_dirty_count"] == 0
    assert result["backup_ref"] is None


def test_source_metadata_rejects_paths_and_non_objects():
    with pytest.raises(ReleaseUpdateError):
        release_update._normalize_source_meta("forced")
    with pytest.raises(ReleaseUpdateError):
        release_update._normalize_source_meta(
            {"forced": True, "previous_branch": "main\nrm -rf /"}
        )
    with pytest.raises(ReleaseUpdateError):
        release_update._normalize_source_meta({"forced": True, "stash_commit": "not-a-commit"})
    assert release_update._normalize_source_meta(None) == {}
    assert release_update._normalize_source_meta({"forced": False, "stash_commit": "a" * 40}) == {}


def test_clean_checkout_reports_untracked_and_tracked_changes_separately(tmp_path: Path):
    _source, deployed = _source_repository(tmp_path)
    (deployed / "scratch.txt").write_text("untracked\n", encoding="utf-8")

    with pytest.raises(ReleaseUpdateError) as untracked:
        release_update._require_clean_checkout(deployed, phase="source_preflight")
    failure = release_failure(untracked.value)
    assert failure["code"] == "untracked_only"
    assert failure["untracked_count"] == 1
    assert failure["modified_count"] == 0
    assert failure["untracked_paths"] == ["scratch.txt"]

    (deployed / "version.txt").write_text("changed\n", encoding="utf-8")
    with pytest.raises(ReleaseUpdateError) as mixed:
        release_update._require_clean_checkout(deployed, phase="source_preflight")
    failure = release_failure(mixed.value)
    assert failure["code"] == "local_changes"
    assert failure["modified_count"] == 1
    assert failure["untracked_count"] == 1


def test_force_align_checkout_leaves_ignored_files_in_place(tmp_path: Path):
    source, deployed = _source_repository(tmp_path)
    (source / ".gitignore").write_text("*.log\n", encoding="utf-8")
    _git(source, "add", ".gitignore")
    _git(source, "commit", "-m", "ignore logs")
    _git(deployed, "fetch", "origin", "main")
    _git(deployed, "merge", "--ff-only", "FETCH_HEAD")
    (deployed / "debug.log").write_text("keep me\n", encoding="utf-8")
    (deployed / "scratch.txt").write_text("stash me\n", encoding="utf-8")
    (source / "version.txt").write_text("two\n", encoding="utf-8")
    _git(source, "add", "version.txt")
    _git(source, "commit", "-m", "second")

    result = force_align_checkout(deployed, remote="origin", branch="main")

    assert result["stashed_dirty_count"] == 1
    assert (deployed / "debug.log").read_text(encoding="utf-8") == "keep me\n"


def test_force_align_checkout_aborts_when_a_locked_file_survives_the_stash(
    tmp_path: Path, monkeypatch
):
    source, deployed = _source_repository(tmp_path)
    (deployed / "scratch.txt").write_text("locked\n", encoding="utf-8")
    old = _git(deployed, "rev-parse", "HEAD")
    (source / "version.txt").write_text("two\n", encoding="utf-8")
    _git(source, "add", "version.txt")
    _git(source, "commit", "-m", "second")
    real_git = release_update._git

    def fake_git(root, *args, **kwargs):
        if args[:2] == ("status", "--porcelain") and kwargs.get("phase") == "source_force_stash":
            return subprocess.CompletedProcess(["git"], 0, "?? locked.sqlite\n", "")
        return real_git(root, *args, **kwargs)

    monkeypatch.setattr(release_update, "_git", fake_git)

    with pytest.raises(ReleaseUpdateError) as raised:
        force_align_checkout(deployed, remote="origin", branch="main")

    failure = release_failure(raised.value)
    assert failure["code"] == "stash_incomplete"
    assert failure["dirty_paths"] == ["locked.sqlite"]
    assert failure["stash_commit"]
    assert _git(deployed, "rev-parse", "HEAD") == old
    assert _git(deployed, "stash", "list") != ""


def test_dirty_summary_keeps_the_first_path_intact_after_strip():
    summary = release_update._dirty_summary("M README.md\n?? scratch.txt")
    assert summary["modified_paths"] == ["README.md"]
    assert summary["untracked_paths"] == ["scratch.txt"]

    summary = release_update._dirty_summary(" M README.md\n?? scratch.txt")
    assert summary["modified_paths"] == ["README.md"]
    assert summary["untracked_paths"] == ["scratch.txt"]

    summary = release_update._dirty_summary("MM docs/a b.md")
    assert summary["modified_paths"] == ["docs/a b.md"]
