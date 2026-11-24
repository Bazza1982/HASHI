from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from orchestrator import remote_lifecycle
from tools import release_update

TARGET = "a" * 40
OLD = "b" * 40


def _write_remote_tree(root: Path) -> None:
    for relative in release_update._REMOTE_REQUIRED_FILES:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# remote\n", encoding="utf-8")
    controller = root / "bin" / "hashi_remote_ctl.ps1"
    controller.parent.mkdir(parents=True, exist_ok=True)
    controller.write_text("# controller\n", encoding="utf-8")


def test_remote_precheck_marks_running_commit_for_restart(tmp_path, monkeypatch):
    root = tmp_path / "hashi"
    root.mkdir()
    _write_remote_tree(root)
    (root / "agents.json").write_text(json.dumps({"version": "test"}), encoding="utf-8")
    claim_path = root / "state" / "remote_runtime_claim.json"
    claim_path.parent.mkdir(parents=True, exist_ok=True)
    claim_path.write_text(
        json.dumps(
            {
                "pid": 4321,
                "code_root": str(root),
                "provenance": {"commit": OLD, "branch": "main"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        release_update,
        "_remote_import_precheck",
        lambda code_root: {"ok": True, "imports": ["remote.api.server"]},
    )
    monkeypatch.setattr(
        release_update,
        "_require_clean_checkout",
        lambda code_root, *, phase: TARGET,
    )

    state = release_update._remote_release_precheck(
        root, TARGET, remote="origin", branch="main"
    )

    assert state["status"] == "ready"
    assert state["needs_restart"] is True
    assert state["running_commit"] == OLD
    assert state["running_pid"] == 4321
    assert state["target_commit"] == TARGET
    assert state["controller"] == "bin/hashi_remote_ctl.ps1"
    assert state["precheck"]["ok"] is True


def test_remote_precheck_fast_forwards_independent_code_root(tmp_path, monkeypatch):
    root = tmp_path / "hashi"
    remote_root = tmp_path / "remote-code"
    root.mkdir()
    remote_root.mkdir()
    _write_remote_tree(root)
    _write_remote_tree(remote_root)
    (root / "agents.json").write_text(json.dumps({"version": "test"}), encoding="utf-8")
    claim_path = root / "state" / "remote_runtime_claim.json"
    claim_path.parent.mkdir(parents=True, exist_ok=True)
    claim_path.write_text(
        json.dumps(
            {
                "pid": 11,
                "code_root": str(remote_root),
                "provenance": {"commit": OLD},
            }
        ),
        encoding="utf-8",
    )
    calls: list[tuple[str, str]] = []

    def fast_forward(code_root, *, remote, branch):
        calls.append((remote, branch))
        return {"target_commit": TARGET}

    monkeypatch.setattr(release_update, "fast_forward_checkout", fast_forward)
    monkeypatch.setattr(
        release_update,
        "_require_clean_checkout",
        lambda code_root, *, phase: OLD if not calls else TARGET,
    )
    monkeypatch.setattr(
        release_update,
        "_remote_import_precheck",
        lambda code_root: {"ok": True, "imports": []},
    )

    state = release_update._remote_release_precheck(
        root, TARGET, remote="upstream", branch="release"
    )

    assert calls == [("upstream", "release")]
    assert state["code_root"] == str(remote_root)
    assert state["sync"]["action"] == "fast_forward"


def test_remote_precheck_fails_closed_when_source_incomplete(tmp_path, monkeypatch):
    root = tmp_path / "hashi"
    root.mkdir()
    (root / "agents.json").write_text(json.dumps({}), encoding="utf-8")
    monkeypatch.setattr(
        release_update, "_require_clean_checkout", lambda code_root, *, phase: TARGET
    )

    with pytest.raises(release_update.ReleaseUpdateError) as caught:
        release_update._remote_release_precheck(
            root, TARGET, remote="origin", branch="main"
        )

    assert caught.value.phase == "remote_prepare"
    assert "remote/api/server.py" in caught.value.details["missing"]


def _settings(root: Path) -> remote_lifecycle.RemoteLifecycleSettings:
    return remote_lifecycle.RemoteLifecycleSettings(
        root=root,
        enabled=True,
        supervised=True,
        disabled_path=root / "state" / "remote_disabled.json",
        port=8771,
        use_tls=False,
        backend="lan",
    )


def test_restart_remote_after_cutover_requires_new_pid_and_commit(
    tmp_path, monkeypatch
):
    settings = _settings(tmp_path)
    monkeypatch.setattr(remote_lifecycle, "load_settings", lambda root=None: settings)
    monkeypatch.setattr(remote_lifecycle, "read_disabled_state", lambda root=None: None)
    monkeypatch.setattr(
        remote_lifecycle, "read_runtime_claim", lambda root: {"pid": 100}
    )

    async def control(root, *, action, **kwargs):
        assert action == "restart"
        return {"ok": True, "action": "restarted"}

    monkeypatch.setattr(remote_lifecycle, "control_remote_supervisor", control)

    observations = iter(
        [
            {"pid": 100, "health": {"instance": {"runtime_claim": {"provenance": {"commit": OLD}}}}},
            {"pid": 200, "health": {"instance": {"runtime_claim": {"provenance": {"commit": TARGET}}}}},
        ]
    )

    async def find(settings_arg):
        return next(observations, None)

    monkeypatch.setattr(remote_lifecycle, "_find_owned_remote", find)

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(remote_lifecycle.asyncio, "sleep", no_sleep)

    result = asyncio.run(
        remote_lifecycle.restart_remote_after_cutover(expected_commit=TARGET)
    )

    assert result["ok"] is True
    assert result["status"] == "restarted"
    assert result["pid"] == 200
    assert result["previous_pid"] == 100
    assert result["source_commit"] == TARGET


def test_restart_remote_skips_when_it_was_never_running(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    monkeypatch.setattr(remote_lifecycle, "load_settings", lambda root=None: settings)
    monkeypatch.setattr(remote_lifecycle, "read_disabled_state", lambda root=None: None)
    monkeypatch.setattr(remote_lifecycle, "read_runtime_claim", lambda root: None)

    async def find(settings_arg):
        return None

    monkeypatch.setattr(remote_lifecycle, "_find_owned_remote", find)

    result = asyncio.run(remote_lifecycle.restart_remote_after_cutover())

    assert result == {"ok": True, "status": "skipped", "reason": "remote_not_running"}


def test_restart_remote_falls_back_and_reports_failure(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    monkeypatch.setattr(remote_lifecycle, "load_settings", lambda root=None: settings)
    monkeypatch.setattr(remote_lifecycle, "read_disabled_state", lambda root=None: None)
    monkeypatch.setattr(remote_lifecycle, "read_runtime_claim", lambda root: {"pid": 5})

    async def control(root, *, action, **kwargs):
        return {"ok": False, "action": "supervisor_unavailable"}

    async def stop(root, **kwargs):
        return {"ok": True, "action": "stopped"}

    async def start(root, **kwargs):
        return {"ok": False, "action": "remote_activation_failed"}

    monkeypatch.setattr(remote_lifecycle, "control_remote_supervisor", control)
    monkeypatch.setattr(remote_lifecycle, "stop_remote", stop)
    monkeypatch.setattr(remote_lifecycle, "ensure_remote_started", start)

    result = asyncio.run(remote_lifecycle.restart_remote_after_cutover())

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert result["reason"] == "remote_restart_failed"
    assert result["fallback"]["start"]["ok"] is False
# HASHI-Fingerprint: hfp_236650cd2c994da0a23e7522a1e3208f
