from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.her_rebuild import FailureKind, HERRebuildError, RebuildStage
from orchestrator.her_rebuild_manager import (
    HERBuildLock,
    HERRebuildJobStore,
)


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
            candidate_id="candidate-one" if state == RebuildStage.CANDIDATE_READY else None,
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


def test_recovery_marks_interrupted_rollback_as_manual_failure(tmp_path: Path) -> None:
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
    assert len(recovered) == 1
    assert recovered[0].state == RebuildStage.ROLLBACK_FAILED
    assert recovered[0].details["manual_reconciliation_required"] is True


def test_build_lock_excludes_a_second_owner_and_releases_cleanly(tmp_path: Path) -> None:
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
