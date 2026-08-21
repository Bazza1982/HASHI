from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator.her_rebuild import (
    BuildArtifact,
    FailureKind,
    HERRebuildError,
    RebuildStage,
    SourceFingerprint,
    ToolchainIdentity,
)
from orchestrator.her_rebuild_manager import (
    HERBuildLock,
    HERRebuildJobStore,
    HERRebuildManager,
)
from scripts import her_rebuild_dev


def _create(store: HERRebuildJobStore, *, fingerprint: str = "a" * 64):
    return store.create(
        source_fingerprint=fingerprint,
        target_agent="lily",
        actor_id="owner-1",
        origin={"channel": "telegram", "chat_id": "123"},
    )


def _advance_to_success(store: HERRebuildJobStore, job_id: str):
    states = [
        RebuildStage.SOURCE_PREFLIGHT,
        RebuildStage.WAITING_FOR_BUILD_LOCK,
        RebuildStage.BUILDING,
        RebuildStage.VERIFYING,
        RebuildStage.CANDIDATE_READY,
        RebuildStage.WAITING_FOR_AGENT_IDLE,
        RebuildStage.ACTIVATING,
        RebuildStage.REBOOT_REQUESTED,
        RebuildStage.ADOPTING,
        RebuildStage.POSTCHECK,
        RebuildStage.SUCCEEDED,
    ]
    record = None
    for state in states:
        record = store.transition(
            job_id,
            state,
            candidate_id="candidate-one"
            if state == RebuildStage.CANDIDATE_READY
            else None,
        )
    return record


def test_job_store_persists_full_state_machine(tmp_path: Path) -> None:
    store = HERRebuildJobStore(tmp_path / "jobs")
    created = _create(store)
    completed = _advance_to_success(store, created.job_id)

    assert completed is not None
    assert completed.state == RebuildStage.SUCCEEDED
    assert completed.is_terminal is True
    assert completed.candidate_id == "candidate-one"
    assert [item.state for item in completed.transitions] == [
        RebuildStage.ACCEPTED,
        RebuildStage.SOURCE_PREFLIGHT,
        RebuildStage.WAITING_FOR_BUILD_LOCK,
        RebuildStage.BUILDING,
        RebuildStage.VERIFYING,
        RebuildStage.CANDIDATE_READY,
        RebuildStage.WAITING_FOR_AGENT_IDLE,
        RebuildStage.ACTIVATING,
        RebuildStage.REBOOT_REQUESTED,
        RebuildStage.ADOPTING,
        RebuildStage.POSTCHECK,
        RebuildStage.SUCCEEDED,
    ]

    reloaded = HERRebuildJobStore(tmp_path / "jobs")
    assert reloaded.get(created.job_id) == completed
    assert reloaded.latest() == completed


def test_job_store_rejects_invalid_or_unclassified_failure_transition(
    tmp_path: Path,
) -> None:
    store = HERRebuildJobStore(tmp_path / "jobs")
    created = _create(store)
    with pytest.raises(ValueError, match="invalid HER rebuild transition"):
        store.transition(created.job_id, RebuildStage.BUILDING)
    with pytest.raises(ValueError, match="requires failure_kind"):
        store.transition(created.job_id, RebuildStage.FAILED)


def test_failure_persists_machine_readable_reason(tmp_path: Path) -> None:
    store = HERRebuildJobStore(tmp_path / "jobs")
    created = _create(store)
    failed = store.transition(
        created.job_id,
        RebuildStage.FAILED,
        detail="source absent",
        failure_kind=FailureKind.SOURCE_MISSING,
        error="Integrated HER source is missing.",
        details={"current_her_unchanged": True},
    )
    assert failed.failure_kind == FailureKind.SOURCE_MISSING
    assert failed.details == {"current_her_unchanged": True}
    assert store.get(created.job_id) == failed


def test_accept_or_join_deduplicates_only_identical_fingerprint(tmp_path: Path) -> None:
    store = HERRebuildJobStore(tmp_path / "jobs")
    first, joined = store.accept_or_join(
        source_fingerprint="a" * 64,
        target_agent="lily",
        actor_id="owner-1",
        origin={"chat_id": "123"},
    )
    assert joined is False

    same, joined = store.accept_or_join(
        source_fingerprint="a" * 64,
        target_agent="sunny",
        actor_id="owner-1",
        origin={"chat_id": "456"},
    )
    assert joined is True
    assert same.job_id == first.job_id
    assert len(same.requesters) == 2
    assert {item["target_agent"] for item in same.requesters} == {"lily", "sunny"}
    assert len({item["requester_id"] for item in same.requesters}) == 2

    with pytest.raises(HERRebuildError) as caught:
        store.accept_or_join(
            source_fingerprint="b" * 64,
            target_agent="lily",
            actor_id="owner-1",
            origin={"chat_id": "123"},
        )
    assert caught.value.failure_kind == FailureKind.BUILD_LOCK_BUSY


def test_terminal_notification_event_is_idempotent(tmp_path: Path) -> None:
    store = HERRebuildJobStore(tmp_path / "jobs")
    created = _create(store)
    completed = _advance_to_success(store, created.job_id)
    assert completed is not None

    first = store.mark_notification(
        created.job_id,
        event_id=f"{created.job_id}:terminal",
        delivered=False,
    )
    delivered = store.mark_notification(
        created.job_id,
        event_id=f"{created.job_id}:terminal",
        delivered=True,
    )
    retried = store.mark_notification(
        created.job_id,
        event_id=f"{created.job_id}:terminal",
        delivered=False,
    )
    assert first.terminal_notification_delivered is False
    assert delivered.terminal_notification_delivered is True
    assert retried.terminal_notification_delivered is True

    with pytest.raises(ValueError, match="immutable"):
        store.mark_notification(
            created.job_id,
            event_id="a-different-terminal-event",
            delivered=True,
        )


def test_joined_requesters_track_independent_terminal_delivery(tmp_path: Path) -> None:
    store = HERRebuildJobStore(tmp_path / "jobs")
    created, _ = store.accept_or_join(
        source_fingerprint="a" * 64,
        target_agent="lily",
        actor_id="owner-1",
        origin={"chat_id": "123"},
    )
    joined, is_joined = store.accept_or_join(
        source_fingerprint="a" * 64,
        target_agent="sunny",
        actor_id="owner-1",
        origin={"chat_id": "456"},
    )
    assert is_joined is True
    completed = _advance_to_success(store, created.job_id)
    assert completed is not None

    first_id = str(joined.requesters[0]["requester_id"])
    second_id = str(joined.requesters[1]["requester_id"])
    partial = store.mark_notification(
        created.job_id,
        requester_id=first_id,
        event_id=f"{created.job_id}:terminal:{first_id}",
        delivered=True,
    )
    assert partial.terminal_notification_delivered is False
    complete = store.mark_notification(
        created.job_id,
        requester_id=second_id,
        event_id=f"{created.job_id}:terminal:{second_id}",
        delivered=True,
    )
    assert complete.terminal_notification_delivered is True


def test_nonterminal_notification_is_rejected(tmp_path: Path) -> None:
    store = HERRebuildJobStore(tmp_path / "jobs")
    created = _create(store)
    with pytest.raises(ValueError, match="before terminal"):
        store.mark_notification(
            created.job_id,
            event_id="too-early",
            delivered=True,
        )


def test_recovery_marks_pre_activation_job_failed(tmp_path: Path) -> None:
    store = HERRebuildJobStore(tmp_path / "jobs")
    created = _create(store)
    store.transition(created.job_id, RebuildStage.SOURCE_PREFLIGHT)
    store.transition(created.job_id, RebuildStage.WAITING_FOR_BUILD_LOCK)
    store.transition(created.job_id, RebuildStage.BUILDING)

    recovered = store.recover_nonterminal()
    assert len(recovered) == 1
    assert recovered[0].state == RebuildStage.FAILED
    assert recovered[0].failure_kind == FailureKind.INTERNAL_ERROR
    assert recovered[0].details == {"interrupted": True}


def test_recovery_defers_interrupted_rollback_for_startup_reconciliation(
    tmp_path: Path,
) -> None:
    store = HERRebuildJobStore(tmp_path / "jobs")
    created = _create(store)
    for state in (
        RebuildStage.SOURCE_PREFLIGHT,
        RebuildStage.WAITING_FOR_BUILD_LOCK,
        RebuildStage.BUILDING,
        RebuildStage.VERIFYING,
        RebuildStage.CANDIDATE_READY,
        RebuildStage.WAITING_FOR_AGENT_IDLE,
        RebuildStage.ACTIVATING,
        RebuildStage.REBOOT_REQUESTED,
        RebuildStage.ROLLING_BACK,
    ):
        store.transition(created.job_id, state)
    recovered = store.recover_nonterminal()
    assert recovered == []
    assert store.get(created.job_id).state == RebuildStage.ROLLING_BACK


@pytest.mark.asyncio
async def test_offline_status_is_strictly_read_only_during_active_build(
    tmp_path: Path, capsys
) -> None:
    bridge_home = tmp_path / "home"
    store = HERRebuildJobStore(bridge_home / "state" / "her_rebuild" / "jobs")
    created = _create(store)
    store.transition(created.job_id, RebuildStage.SOURCE_PREFLIGHT)
    store.transition(created.job_id, RebuildStage.WAITING_FOR_BUILD_LOCK)
    store.transition(created.job_id, RebuildStage.BUILDING)
    job_path = store._job_path(created.job_id)
    before = job_path.read_bytes()

    exit_code = await her_rebuild_dev._run(
        SimpleNamespace(bridge_home=bridge_home, status="latest")
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["job_id"] == created.job_id
    assert payload["state"] == RebuildStage.BUILDING.value
    assert job_path.read_bytes() == before
    assert store.get(created.job_id).state == RebuildStage.BUILDING


@pytest.mark.asyncio
async def test_offline_status_does_not_create_state_when_no_job_exists(
    tmp_path: Path, capsys
) -> None:
    bridge_home = tmp_path / "absent-home"

    exit_code = await her_rebuild_dev._run(
        SimpleNamespace(bridge_home=bridge_home, status="latest")
    )

    assert exit_code == 1
    assert json.loads(capsys.readouterr().out) == {"status": "not_found"}
    assert not bridge_home.exists()


def test_second_manager_cannot_recover_a_live_managers_job(tmp_path: Path) -> None:
    code_root = tmp_path / "hashi"
    code_root.mkdir()
    kernel = SimpleNamespace(
        paths=SimpleNamespace(code_root=code_root, bridge_home=tmp_path / "home"),
        runtimes=[],
    )
    first = HERRebuildManager(kernel)
    created = first.jobs.create(
        source_fingerprint="a" * 64,
        target_agent="lily",
        actor_id="owner",
        origin={"chat_id": "1"},
    )
    first.jobs.transition(created.job_id, RebuildStage.SOURCE_PREFLIGHT)
    first.jobs.transition(created.job_id, RebuildStage.WAITING_FOR_BUILD_LOCK)
    first.jobs.transition(created.job_id, RebuildStage.BUILDING)

    try:
        with pytest.raises(HERRebuildError) as caught:
            HERRebuildManager(kernel)
        assert caught.value.failure_kind == FailureKind.BUILD_LOCK_BUSY
        assert first.jobs.get(created.job_id).state == RebuildStage.BUILDING
    finally:
        first.close()

    replacement = HERRebuildManager(kernel)
    try:
        recovered = replacement.jobs.get(created.job_id)
        assert recovered.state == RebuildStage.FAILED
        assert recovered.error == "kernel_restarted_during_rebuild"
    finally:
        replacement.close()


def test_manager_ownership_lock_excludes_a_second_process(tmp_path: Path) -> None:
    code_root = tmp_path / "hashi"
    bridge_home = tmp_path / "home"
    code_root.mkdir()
    kernel = SimpleNamespace(
        paths=SimpleNamespace(code_root=code_root, bridge_home=bridge_home),
        runtimes=[],
    )
    manager = HERRebuildManager(kernel)
    created = manager.jobs.create(
        source_fingerprint="a" * 64,
        target_agent="lily",
        actor_id="owner",
        origin={"chat_id": "1"},
    )
    manager.jobs.transition(created.job_id, RebuildStage.SOURCE_PREFLIGHT)
    manager.jobs.transition(created.job_id, RebuildStage.WAITING_FOR_BUILD_LOCK)
    manager.jobs.transition(created.job_id, RebuildStage.BUILDING)
    probe = f"""
from types import SimpleNamespace
from orchestrator.her_rebuild import HERRebuildError
from orchestrator.her_rebuild_manager import HERRebuildManager
kernel = SimpleNamespace(
    paths=SimpleNamespace(code_root={str(code_root)!r}, bridge_home={str(bridge_home)!r}),
    runtimes=[],
)
try:
    HERRebuildManager(kernel)
except HERRebuildError as exc:
    print(exc.failure_kind.value)
    raise SystemExit(0)
raise SystemExit(2)
"""

    try:
        completed = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=Path(__file__).resolve().parents[1],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert completed.returncode == 0, completed.stderr
        assert completed.stdout.strip() == FailureKind.BUILD_LOCK_BUSY.value
        assert manager.jobs.get(created.job_id).state == RebuildStage.BUILDING
    finally:
        manager.close()


def test_existing_stable_manager_hot_upgrade_preserves_live_state(
    tmp_path: Path,
) -> None:
    code_root = tmp_path / "hashi"
    code_root.mkdir()
    kernel = SimpleNamespace(
        paths=SimpleNamespace(code_root=code_root, bridge_home=tmp_path / "home"),
        runtimes=[],
    )
    manager = HERRebuildManager(kernel)
    manager._manager_lock.release()
    del manager._manager_lock
    legacy_class = type("LegacyHERRebuildManager", (), {})
    manager.__class__ = legacy_class
    created = manager.jobs.create(
        source_fingerprint="a" * 64,
        target_agent="lily",
        actor_id="owner",
        origin={"chat_id": "1"},
    )
    active_task = SimpleNamespace(done=lambda: False)
    manager._tasks[created.job_id] = active_task

    upgraded = HERRebuildManager.upgrade_existing(manager)
    owner_lock = upgraded._manager_lock
    upgraded_again = HERRebuildManager.upgrade_existing(upgraded)

    assert upgraded is manager
    assert upgraded.__class__ is HERRebuildManager
    assert upgraded.jobs.get(created.job_id).state == RebuildStage.ACCEPTED
    assert upgraded._tasks[created.job_id] is active_task
    assert owner_lock.acquired is True
    assert upgraded_again is upgraded
    assert upgraded_again._manager_lock is owner_lock

    upgraded._tasks.clear()
    upgraded.close()


def test_manager_reconciles_interrupted_selection_before_agent_startup(
    tmp_path: Path, monkeypatch
) -> None:
    code_root = tmp_path / "hashi"
    code_root.mkdir()
    kernel = SimpleNamespace(
        paths=SimpleNamespace(code_root=code_root, bridge_home=tmp_path / "home")
    )
    manager = HERRebuildManager(kernel)
    created = manager.jobs.create(
        source_fingerprint="a" * 64,
        target_agent="lily",
        actor_id="owner",
        origin={"chat_id": "1"},
    )
    for state in (
        RebuildStage.SOURCE_PREFLIGHT,
        RebuildStage.WAITING_FOR_BUILD_LOCK,
        RebuildStage.BUILDING,
        RebuildStage.VERIFYING,
        RebuildStage.CANDIDATE_READY,
        RebuildStage.WAITING_FOR_AGENT_IDLE,
        RebuildStage.ACTIVATING,
    ):
        manager.jobs.transition(created.job_id, state)
    monkeypatch.setattr(manager.selection, "restore_previous", lambda **_kwargs: None)

    reconciled = manager.reconcile_before_agent_startup()

    assert len(reconciled) == 1
    assert reconciled[0].state == RebuildStage.ROLLED_BACK
    assert reconciled[0].details["cold_start_reconciled"] is True


@pytest.mark.asyncio
async def test_submit_rejects_live_non_her_target_before_toolchain_probe(
    tmp_path: Path, monkeypatch
) -> None:
    code_root = tmp_path / "hashi"
    code_root.mkdir()
    runtime = SimpleNamespace(
        name="lily",
        config=SimpleNamespace(active_backend="codex-cli"),
    )
    kernel = SimpleNamespace(
        paths=SimpleNamespace(code_root=code_root, bridge_home=tmp_path / "home"),
        runtimes=[runtime],
    )
    manager = HERRebuildManager(kernel)
    probed = False

    async def unexpected_probe():
        nonlocal probed
        probed = True

    monkeypatch.setattr(
        "orchestrator.her_rebuild_manager.inspect_toolchain", unexpected_probe
    )

    with pytest.raises(HERRebuildError) as caught:
        await manager.submit(
            target_agent="lily",
            actor_id="owner",
            origin={"chat_id": "123"},
        )

    assert caught.value.failure_kind == FailureKind.REBOOT_REJECTED
    assert "switch that Agent to HER" in str(caught.value)
    assert probed is False


@pytest.mark.asyncio
async def test_manager_builds_verifies_adopts_and_notifies_transactionally(
    tmp_path: Path,
) -> None:
    code_root = tmp_path / "hashi"
    code_root.mkdir()
    backend = SimpleNamespace(
        _active_processes={}, _binary=None, _binary_resolution=None
    )
    runtime = SimpleNamespace(
        name="lily",
        startup_success=True,
        backend_ready=True,
        queue=asyncio.Queue(),
        is_generating=False,
        current_request_meta=None,
        _background_tasks=set(),
        backend=backend,
        send_long_message=AsyncMock(),
    )
    kernel = SimpleNamespace(
        paths=SimpleNamespace(code_root=code_root, bridge_home=tmp_path / "home"),
        runtimes=[runtime],
    )
    manager = HERRebuildManager(kernel, idle_timeout_seconds=0)
    fingerprint = SourceFingerprint(
        digest="a" * 64,
        git_head="b" * 40,
        dirty=False,
        file_count=1,
        source_bytes=1,
        target="x86_64-unknown-linux-gnu",
        profile="hashi-dev",
        features=(),
        cargo_version="cargo test",
        rustc_version="rustc test",
    )
    toolchain = ToolchainIdentity(
        cargo_path="cargo",
        cargo_version=fingerprint.cargo_version,
        rustc_path="rustc",
        rustc_version=fingerprint.rustc_version,
    )
    cargo_output = tmp_path / "cargo-output" / "claw"
    cargo_output.parent.mkdir()
    cargo_output.write_bytes(b"development-her")
    cargo_output.chmod(0o755)
    build_log = tmp_path / "build.log"
    build_log.write_text("ok\n", encoding="utf-8")
    artifact = BuildArtifact(
        job_id="placeholder",
        fingerprint=fingerprint,
        binary_path=cargo_output,
        build_log_path=build_log,
        build_started_at="2026-08-16T00:00:00+00:00",
        build_finished_at="2026-08-16T00:00:01+00:00",
        build_duration_seconds=1.0,
        cargo_argv=("cargo", "build"),
        diagnostics="",
        log_truncated=False,
    )

    async def fake_build(**kwargs):
        kwargs["on_process_started"](4242)
        kwargs["on_process_finished"]()
        return artifact

    async def fake_verify(binary_path, **_kwargs):
        return {"schema_version": 1, "result": "passed", "binary": str(binary_path)}

    manager.controller = SimpleNamespace(build=fake_build)
    manager.verifier = SimpleNamespace(verify=fake_verify)

    async def hot_restart(_request):
        candidate = manager.selection.active_candidate(target=fingerprint.target)
        backend._binary = Path(candidate.binary_path)
        backend._binary_resolution = SimpleNamespace(source="development-source-build")
        return True

    kernel.reboot_manager = SimpleNamespace(hot_restart=hot_restart)
    record = manager.jobs.create(
        source_fingerprint=fingerprint.digest,
        target_agent="lily",
        actor_id="owner",
        origin={"chat_id": "123"},
    )

    await manager._run(record.job_id, fingerprint=fingerprint, toolchain=toolchain)

    completed = manager.jobs.get(record.job_id)
    assert completed.state == RebuildStage.SUCCEEDED
    assert completed.terminal_notification_delivered is True
    assert manager.selection.read()["adoption_state"] == "adopted"
    runtime.send_long_message.assert_awaited_once()


def test_build_lock_excludes_a_second_owner_and_releases_cleanly(
    tmp_path: Path,
) -> None:
    path = tmp_path / "her-build.lock"
    first = HERBuildLock(path, source_fingerprint="a" * 64)
    second = HERBuildLock(path, source_fingerprint="a" * 64)

    first.acquire()
    assert first.acquired is True
    with pytest.raises(HERRebuildError) as caught:
        second.acquire()
    assert caught.value.failure_kind == FailureKind.BUILD_LOCK_BUSY

    first.release()
    second.acquire()
    assert second.acquired is True
    second.release()


def test_build_lock_tracks_cargo_pid_and_preserves_metadata(tmp_path: Path) -> None:
    path = tmp_path / "her-build.lock"
    lock = HERBuildLock(path, source_fingerprint="a" * 64)
    with lock:
        lock.set_cargo_pid(4242)
        assert lock.metadata()["cargo_pid"] == 4242
        assert lock.metadata()["source_fingerprint"] == "a" * 64
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["cargo_pid"] is None
    assert payload["released_at"]


def test_build_lock_does_not_steal_live_orphaned_cargo(tmp_path: Path) -> None:
    path = tmp_path / "her-build.lock"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "owner_pid": 111,
                "cargo_pid": 222,
                "source_fingerprint": "old",
            }
        ),
        encoding="utf-8",
    )
    lock = HERBuildLock(
        path,
        source_fingerprint="new",
        pid_probe=lambda pid: pid == 222,
    )
    with pytest.raises(HERRebuildError) as caught:
        lock.acquire()
    assert caught.value.failure_kind == FailureKind.STALE_LOCK_UNRECOVERABLE
    assert lock.acquired is False


def test_build_lock_recovers_stale_metadata_when_cargo_is_absent(
    tmp_path: Path,
) -> None:
    path = tmp_path / "her-build.lock"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "owner_pid": 111,
                "cargo_pid": 222,
                "source_fingerprint": "old",
            }
        ),
        encoding="utf-8",
    )
    lock = HERBuildLock(
        path,
        source_fingerprint="new",
        pid_probe=lambda _pid: False,
    )
    lock.acquire()
    assert lock.metadata()["source_fingerprint"] == "new"
    assert lock.metadata()["cargo_pid"] is None
    lock.release()
