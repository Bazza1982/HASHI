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


async def test_hashideploy_force_is_exact_and_manual_only(tmp_path, monkeypatch):
    from orchestrator import ui_language
    from orchestrator.commands import hashideploy

    calls = []
    messages = []

    async def reply(_update, text, **kwargs):
        messages.append(text)

    runtime = SimpleNamespace(
        global_config=SimpleNamespace(project_root=tmp_path, bridge_home=tmp_path, instance_id="TEST"),
        _is_authorized_user=lambda user_id: True,
        _reply_text=reply,
    )
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1))
    def prepare(**kwargs):
        calls.append(kwargs)
        result = _ready("a" * 40)
        if kwargs.get("force"):
            result = {
                **result,
                "forced": True,
                "force": {"stash_commit": "d" * 40, "stashed_dirty_count": 2,
                          "backup_ref": "refs/hashideploy-backups/x"},
                "source": {"forced": True, "previous_branch": "main",
                           "stashed_dirty_count": 2},
            }
        return result

    monkeypatch.setattr(hashideploy, "run_staged_source_release", prepare)

    context = SimpleNamespace(args=["force", "--restart"])
    await hashideploy.hashideploy_command(runtime, update, context)
    assert calls == []
    assert messages[-1] == ui_language.tr("hashideploy.usage")

    context = SimpleNamespace(args=["force"], source_channel="hal")
    await hashideploy.hashideploy_command(runtime, update, context)
    assert calls == []
    assert messages[-1] == ui_language.tr("hashideploy.force.not_automated")

    context = SimpleNamespace(args=["force"], source_channel="workbench_api")
    await hashideploy.hashideploy_command(runtime, update, context)
    assert len(calls) == 1
    assert calls[0]["force"] is True
    assert "force mode" in messages[-1].casefold()

    context = SimpleNamespace(args=[])
    await hashideploy.hashideploy_command(runtime, update, context)
    assert calls[-1]["force"] is False

    runtime._is_authorized_user = lambda user_id: False
    context = SimpleNamespace(args=["force"], source_channel="workbench_api")
    await hashideploy.hashideploy_command(runtime, update, context)
    assert len(calls) == 2
    assert messages[-1] == ui_language.tr("hashideploy.denied")


FAILURE_CLASSES = [
    ("instance_source", "source_preflight", False,
     {"failure": {"code": "instance_source", "source_changed": False,
                  "stage": "pre_change", "root": "/srv/hashi"}},
     "部署未开始"),
    ("local_changes", "source_preflight", False,
     {"failure": {"code": "local_changes", "source_changed": False,
                  "stage": "pre_change", "dirty_count": 4,
                  "modified_count": 3, "untracked_count": 1,
                  "modified_paths": ["a.py", "b.py", "c.py"],
                  "untracked_paths": ["scratch.txt"]}},
     "未提交修改"),
    ("untracked_only", "source_preflight", False,
     {"failure": {"code": "untracked_only", "source_changed": False,
                  "stage": "pre_change", "dirty_count": 2,
                  "modified_count": 0, "untracked_count": 2,
                  "untracked_paths": ["a.log", "b.tmp"]}},
     "未纳入版本控制"),
    ("fetch_failed", "source_fetch", False,
     {"failure": {"code": "fetch_failed", "source_changed": False,
                  "stage": "pre_change", "branch": "main",
                  "previous_commit": "a" * 40},
      "status": "fatal: unable to access remote"},
     "获取 main 失败"),
    ("not_fast_forward", "source_fast_forward", False,
     {"failure": {"code": "not_fast_forward", "source_changed": False,
                  "stage": "pre_change", "previous_branch": "topic/local",
                  "previous_commit": "a" * 40, "target_commit": "b" * 40}},
     "无法快进"),
    ("protected_gate", "protected_core_gate", True,
     {"failure": {"code": "protected_gate", "source_changed": True,
                  "stage": "post_change", "target_commit": "b" * 40}},
     "发布门禁未通过"),
    ("runtime_unavailable", "stage_runtime", True,
     {"failure": {"code": "runtime_unavailable", "source_changed": True,
                  "stage": "post_change", "target_commit": "b" * 40}},
     "当前运行时无法运行"),
    ("commit_mismatch", "source_verify", True,
     {"failure": {"code": "commit_mismatch", "source_changed": True,
                  "stage": "post_change", "target_commit": "b" * 40,
                  "actual_commit": "c" * 40}},
     "源码校验不一致"),
    ("receipt_failed", "stage_receipt", True,
     {"failure": {"code": "receipt_failed", "source_changed": True,
                  "stage": "post_change", "target_commit": "b" * 40}},
     "就绪记录未写入"),
    ("stash_failed", "source_force_stash", False,
     {"failure": {"code": "stash_failed", "source_changed": False,
                  "stage": "pre_change", "previous_commit": "a" * 40,
                  "target_commit": "b" * 40, "stash_commit": "d" * 40,
                  "backup_ref": "refs/hashideploy-backups/x"}},
     "本地改动已保留"),
]


@pytest.mark.parametrize(
    "code,phase,changed,details,marker", FAILURE_CLASSES,
    ids=[row[0] for row in FAILURE_CLASSES],
)
def test_hashideploy_failures_are_readable_and_state_aware(code, phase, changed, details, marker):
    from orchestrator import ui_language
    from orchestrator.commands import hashideploy
    from tools.release_update import ReleaseUpdateError

    with ui_language.language_scope(SimpleNamespace(), locale="zh-CN"):
        text = hashideploy.render_failure(ReleaseUpdateError(phase, "boom", details=details))

    assert marker in text
    for raw in ("source_preflight", "source_fetch", "source_verify",
                "stage_runtime", "stage_receipt", "source_force_stash"):
        assert raw not in text
    assert "?? " not in text
    if changed:
        assert "已更新到" in text
    else:
        assert "源码：未改动" in text
    if code == "stash_failed":
        assert "已暂存为" in text
        assert "旧 HEAD 已保留" in text


def test_hashideploy_prepared_mentions_the_branch_it_left(tmp_path):
    from orchestrator import ui_language
    from orchestrator.commands import hashideploy

    with ui_language.language_scope(SimpleNamespace(), locale="zh-CN"):
        text = hashideploy._success_text({
            "instance_id": "HASHI3",
            "target_commit": "b" * 40,
            "source": {"switched_to_branch": True, "previous_branch": "fix/cleanup"},
        })

    assert "fix/cleanup" in text
    assert "自动切回 main" in text


def test_hashideploy_force_success_names_the_preserved_target_branch(tmp_path):
    from orchestrator import ui_language
    from orchestrator.commands import hashideploy

    with ui_language.language_scope(SimpleNamespace(), locale="zh-CN"):
        text = hashideploy._success_text({
            "instance_id": "HASHI3",
            "target_commit": "b" * 40,
            "forced": True,
            "source": {"forced": True, "branch": "main"},
            "force": {
                "previous_branch": "fix/cleanup",
                "previous_commit": "a" * 40,
                "target_branch_backup_ref":
                    "refs/hashideploy-backups/x-target-aaaaaaaaaaaa",
            },
        })

    assert "原 <code>main</code> 分支的独有提交已保留" in text
    assert "refs/hashideploy-backups/x-target-aaaaaaaaaaaa" in text
# HASHI-Fingerprint: hfp_f8e7636eba1e4afdad419dc0741295ca


def test_staged_receipt_records_force_evidence(tmp_path, monkeypatch):
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
        source_meta={
            "forced": True, "previous_branch": "main",
            "previous_commit": "c" * 40, "stash_commit": "d" * 40,
            "backup_ref": "refs/hashideploy-backups/x",
            "target_branch_commit": "e" * 40,
            "target_branch_backup_ref": "refs/hashideploy-backups/x-target-eeeeeeeeeeee",
            "stashed_dirty_count": 2, "stashed_paths": ["a.py"],
        },
    )

    receipt = json.loads(Path(result["receipt_path"]).read_text(encoding="utf-8"))
    assert receipt["forced"] is True
    assert receipt["force"]["stash_commit"] == "d" * 40
    assert receipt["force"]["backup_ref"] == "refs/hashideploy-backups/x"
    assert receipt["force"]["previous_branch"] == "main"
    assert receipt["force"]["previous_commit"] == "c" * 40
    assert receipt["force"]["stashed_dirty_count"] == 2
    assert receipt["force"]["target_branch_commit"] == "e" * 40
    assert (
        receipt["force"]["target_branch_backup_ref"]
        == "refs/hashideploy-backups/x-target-eeeeeeeeeeee"
    )
