from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import hashi_instance_cli
from tools import release_update
from tests.test_release_update import _git, _source_repository


@pytest.mark.parametrize("command", ["update", "deploy"])
def test_source_preparation_has_an_explicit_cli_mode(command):
    argv = ["--instance", "alpha", command, "--confirm", "alpha"]
    if command == "update":
        argv.append("--stage-only")
    args = hashi_instance_cli._parser().parse_args(argv)
    assert args.stage_only is True
    assert args.confirm == "alpha"


def _ready(commit, instance="TEST"):
    return {
        "ok": True,
        "state": "ready_to_reboot",
        "message": "Ready to reboot",
        "target_commit": commit,
        "instance_id": instance,
        "restarts_performed": False,
        "running_verified": False,
    }


def test_stage_fetches_source_without_runtime_preflight_or_restart(tmp_path, monkeypatch):
    source, deployed = _source_repository(tmp_path)
    gate = source / "scripts" / "check_protected_core_changes.py"
    gate.parent.mkdir()
    gate.write_text("pass\n", encoding="utf-8")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "candidate")
    target = _git(source, "rev-parse", "HEAD")
    calls = []
    hook = deployed / ".git" / "hooks" / "post-merge"
    hook.write_text('#!/bin/sh\nprintf ran > "$0.ran"\n', encoding="utf-8")
    hook.chmod(0o755)
    real_run = release_update._run

    def run(argv, **kwargs):
        calls.append((list(argv), kwargs["phase"]))
        if kwargs["phase"] == "stage_qualification":
            return subprocess.CompletedProcess(argv, 0, json.dumps(_ready(target)), "")
        return real_run(argv, **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail("stage-only attempted the live runtime/restart path")

    monkeypatch.setattr(release_update, "_run", run)
    monkeypatch.setattr(release_update, "LocalReleaseAdapter", forbidden)
    monkeypatch.setattr(release_update, "complete_runtime_release", forbidden)
    result = release_update.run_source_release(
        code_root=deployed, bridge_home=tmp_path / "home", instance_id="TEST",
        api_port=1, runtime_command=[sys.executable, "-B"], stage_only=True,
    )
    assert result["state"] == "ready_to_reboot"
    assert result["running_verified"] is False
    assert result["source"]["target_commit"] == target
    assert _git(deployed, "rev-parse", "HEAD") == target
    handoff = next(argv for argv, phase in calls if phase == "stage_qualification")
    assert handoff[handoff.index("tools.release_update") + 1] == "stage"
    assert not any(phase == "runtime_release" for _, phase in calls)
    assert not hook.with_name("post-merge.ran").exists()
    again = release_update.run_source_release(
        code_root=deployed, bridge_home=tmp_path / "home", instance_id="TEST",
        api_port=1, runtime_command=[sys.executable, "-B"], stage_only=True,
    )
    assert again["source"]["changed"] is False


def test_failed_stage_reports_the_source_left_on_disk_and_never_claims_ready(tmp_path, monkeypatch):
    source, deployed = _source_repository(tmp_path)
    gate = source / "scripts" / "check_protected_core_changes.py"
    gate.parent.mkdir()
    gate.write_text("pass\n", encoding="utf-8")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "candidate")
    target = _git(source, "rev-parse", "HEAD")
    old = _git(deployed, "rev-parse", "HEAD")
    real_run = release_update._run

    def run(argv, **kwargs):
        if kwargs["phase"] == "stage_qualification":
            return subprocess.CompletedProcess(
                argv, 1, json.dumps({"ok": False, "phase": "stage_runtime",
                                    "error": "qualified runtime pair is missing"}), ""
            )
        return real_run(argv, **kwargs)

    monkeypatch.setattr(release_update, "_run", run)
    with pytest.raises(release_update.ReleaseUpdateError) as caught:
        release_update.run_source_release(
            code_root=deployed, bridge_home=tmp_path / "home", instance_id="TEST",
            api_port=1, runtime_command=[sys.executable, "-B"], stage_only=True,
        )
    payload = caught.value.to_payload()
    assert payload["phase"] == "stage_runtime"
    assert payload["details"]["source"]["old_commit"] == old
    assert payload["details"]["source"]["target_commit"] == target
    assert payload["details"]["restarts_performed"] is False
    assert _git(deployed, "rev-parse", "HEAD") == target


def test_stage_publication_requires_exact_qualification_and_is_not_an_active_pointer(tmp_path, monkeypatch):
    _source, deployed = _source_repository(tmp_path)
    target = _git(deployed, "rev-parse", "HEAD")
    home = tmp_path / "home"
    monkeypatch.setattr(
        release_update, "_qualify_staged_functions",
        lambda *_args: {"source_commit": target, "generation_id": "sha256:" + "a" * 64},
        raising=False,
    )
    result = release_update.prepare_staged_release(
        code_root=deployed, bridge_home=home, instance_id="TEST", expected_commit=target,
    )
    receipt = Path(result["receipt_path"])
    assert json.loads(receipt.read_text(encoding="utf-8"))["state"] == "ready_to_reboot"
    assert receipt.is_relative_to(home / "state" / "source_deployments")
    assert not (home / "state" / "qualified_function_generation.json").exists()

    monkeypatch.setattr(
        release_update, "_qualify_staged_functions",
        lambda *_args: {"source_commit": "b" * 40}, raising=False,
    )
    with pytest.raises(release_update.ReleaseUpdateError, match="commit"):
        release_update.prepare_staged_release(
            code_root=deployed, bridge_home=home, instance_id="TEST", expected_commit=target,
        )
    assert list(home.rglob("ready.json")) == [receipt]


def test_hashideploy_is_owner_only_and_uses_the_fixed_no_restart_path(tmp_path, monkeypatch):
    import asyncio

    asyncio.run(_hashideploy_scenario(tmp_path, monkeypatch))


async def _hashideploy_scenario(tmp_path, monkeypatch):
    from orchestrator.commands import hashideploy
    messages = []
    calls = []

    async def reply(_update, text, **kwargs):
        messages.append(text)

    runtime = SimpleNamespace(
        global_config=SimpleNamespace(project_root=tmp_path / "source",
                                      bridge_home=tmp_path / "home", instance_id="TEST"),
        _is_authorized_user=lambda user_id: user_id == 1,
        _reply_text=reply,
    )
    context = SimpleNamespace(args=[])
    update = SimpleNamespace(effective_user=SimpleNamespace(id=2))
    monkeypatch.setattr(
        hashideploy, "run_staged_source_release",
        lambda **kwargs: calls.append(kwargs) or _ready("a" * 40),
    )
    await hashideploy.hashideploy_command(runtime, update, context)
    assert calls == []
    update.effective_user.id = 1
    await hashideploy.hashideploy_command(runtime, update, context)
    assert len(calls) == 1
    assert calls[0]["code_root"] == tmp_path / "source"
    assert calls[0]["runtime_command"] == [sys.executable, "-B"]
    assert "functions update is ready" in messages[-1].casefold()
    context.args = ["--restart"]
    await hashideploy.hashideploy_command(runtime, update, context)
    assert len(calls) == 1

def test_cli_stage_ignores_live_busy_state_but_full_update_keeps_it(tmp_path, monkeypatch, capsys):
    record = {"name": "alpha", "instance_id": "ALPHA", "source_kind": "git",
              "managed": False, "bridge_home": str(tmp_path / "home")}
    root = tmp_path / "source"
    registry = SimpleNamespace(records=lambda: [record], resolved_code_root=lambda _record: root)
    monkeypatch.setattr(hashi_instance_cli, "InstanceRegistry", lambda **kwargs: registry)
    monkeypatch.setattr(hashi_instance_cli, "_selected_record", lambda *args, **kwargs: (record, "explicit"))
    monkeypatch.setattr(hashi_instance_cli, "configured_identity", lambda *_args: ("ALPHA", 1, 2))
    monkeypatch.setattr(hashi_instance_cli, "_select_runtime", lambda *args, **kwargs: [sys.executable, "-B"])
    calls = []

    def unexpected_probe(*args):
        pytest.fail("source preparation must not require live readiness/idle state")

    monkeypatch.setattr(hashi_instance_cli, "inspect_instance", unexpected_probe)
    monkeypatch.setattr(release_update, "run_source_release",
                        lambda **kwargs: calls.append(kwargs) or _ready("a" * 40, "ALPHA"))
    assert hashi_instance_cli._main(
        ["--json", "--instance", "alpha", "deploy", "--confirm", "alpha"]
    ) == 0
    assert calls[0]["stage_only"] is True
    assert json.loads(capsys.readouterr().out)["state"] == "ready_to_reboot"
    monkeypatch.setattr(hashi_instance_cli, "inspect_instance",
                        lambda *_args: {"running": True, "healthy": True, "ready": True,
                                        "activity_verified": True, "busy": True})
    assert hashi_instance_cli._main(
        ["--instance", "alpha", "update", "--confirm", "alpha"]
    ) == hashi_instance_cli.EXIT_BUSY
    assert len(calls) == 1


def test_stage_rejects_an_old_coordinator_without_a_staging_receipt(tmp_path, monkeypatch):
    _source, deployed = _source_repository(tmp_path)
    monkeypatch.setattr(release_update, "_verify_release_source", lambda *_args: None)
    real_run = release_update._run

    def run(argv, **kwargs):
        if kwargs["phase"] == "stage_qualification":
            return subprocess.CompletedProcess(argv, 2, "", "invalid choice: stage")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(release_update, "_run", run)
    with pytest.raises(release_update.ReleaseUpdateError, match="may not support stage-only"):
        release_update.run_staged_source_release(
            code_root=deployed, bridge_home=tmp_path / "home", instance_id="TEST",
            runtime_command=[sys.executable, "-B"],
        )


async def test_hashideploy_cancellation_keeps_preparation_busy_until_receipt(tmp_path, monkeypatch):
    import asyncio
    import threading

    from orchestrator import ui_language
    from orchestrator.commands import hashideploy

    source, deployed = _source_repository(tmp_path)
    gate = source / "scripts" / "check_protected_core_changes.py"
    gate.parent.mkdir()
    gate.write_text("pass\n", encoding="utf-8")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "candidate")
    target = _git(source, "rev-parse", "HEAD")
    home = tmp_path / "home"
    started = asyncio.Event()
    finish = threading.Event()
    loop = asyncio.get_running_loop()
    calls = []
    messages = asyncio.Queue()
    real_run = release_update._run
    real_prepare = hashideploy.run_staged_source_release

    def run(argv, **kwargs):
        if kwargs["phase"] != "stage_qualification":
            return real_run(argv, **kwargs)
        loop.call_soon_threadsafe(started.set)
        if not finish.wait(15):
            raise TimeoutError("test did not release source preparation")
        result = release_update.prepare_staged_release(
            code_root=deployed, bridge_home=home, instance_id="TEST", expected_commit=target,
        )
        return subprocess.CompletedProcess(argv, 0, json.dumps(result), "")

    def prepare(**kwargs):
        calls.append(kwargs)
        return real_prepare(**kwargs)

    async def reply(_update, text, **kwargs):
        messages.put_nowait(text)

    monkeypatch.setattr(release_update, "_run", run)
    monkeypatch.setattr(
        release_update, "_qualify_staged_functions",
        lambda *_args: {"source_commit": target, "generation_id": "sha256:" + "a" * 64},
    )
    monkeypatch.setattr(hashideploy, "run_staged_source_release", prepare)
    runtime = SimpleNamespace(
        global_config=SimpleNamespace(project_root=deployed, bridge_home=home, instance_id="TEST"),
        _is_authorized_user=lambda user_id: user_id == 1,
        _reply_text=reply,
    )
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1))
    context = SimpleNamespace(args=[])
    command = asyncio.create_task(hashideploy.hashideploy_command(runtime, update, context))
    retry = None
    preparation = None
    try:
        await asyncio.wait_for(started.wait(), 10)
        await messages.get()  # Initial preparation notice.
        preparation = runtime._hashideploy_inflight
        command.cancel()
        with pytest.raises(asyncio.CancelledError):
            await command
        retry = asyncio.create_task(hashideploy.hashideploy_command(runtime, update, context))
        assert await asyncio.wait_for(messages.get(), 5) == ui_language.tr("hashideploy.busy")
        await retry
        assert len(calls) == 1
    finally:
        finish.set()
        pending = [command]
        if retry is not None:
            pending.append(retry)
        if isinstance(preparation, asyncio.Task):
            pending.append(preparation)
        await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), 15)

    # Cancellation dropped the notification, but real Git and durable readiness agree.
    receipts = list((home / "state" / "source_deployments").glob("*/ready.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
    assert receipt["target_commit"] == _git(deployed, "rev-parse", "HEAD") == target
    assert receipt["state"] == "ready_to_reboot"
    assert receipt["restarts_performed"] is False
    assert receipt["running_verified"] is False
    await hashideploy.hashideploy_command(runtime, update, context)
    assert len(calls) == 2
    messages.get_nowait()  # New preparation notice.
    assert "functions update is ready" in messages.get_nowait().casefold()
