from __future__ import annotations

import sqlite3

import pytest

from orchestrator import runtime_session
from orchestrator.session_store import (
    IdempotencyConflict,
    SessionConflict,
    SessionNotFound,
    SessionStore,
    StaleFencingToken,
)


def _store(tmp_path) -> SessionStore:
    return SessionStore(tmp_path / "state" / "sessions.sqlite3", instance_id="HASHI1")


def test_runtime_without_bridge_root_keeps_session_db_in_its_workspace(tmp_path):
    runtime = type("Runtime", (), {})()
    runtime.name = "test-agent"
    runtime.workspace_dir = tmp_path / "workspace"
    runtime.global_config = type(
        "Config", (), {"authorized_id": 7, "instance_id": "TEST"}
    )()

    store = runtime_session.ensure_store(runtime)

    assert store.db_path == runtime.workspace_dir / "state" / "sessions.sqlite3"
    assert not (tmp_path / "state" / "sessions.sqlite3").exists()


def _complete(
    store: SessionStore,
    *,
    session_id: str,
    owner_id: str,
    request_id: str,
    key: str,
    text: str,
    answer: str,
):
    accepted = store.accept_run(
        session_id=session_id,
        owner_id=owner_id,
        agent_id="lily",
        request_id=request_id,
        text=text,
        source="test",
        idempotency_key=key,
    )
    assert store.mark_request_running(request_id, worker_id="test-worker") == 1
    run = store.finish_request(
        request_id,
        success=True,
        assistant_text=answer,
        assistant_source="test-backend",
    )
    assert run and run["state"] == "completed"
    return accepted


def test_default_session_is_permanent_and_channel_bindings_are_isolated(tmp_path):
    store = _store(tmp_path)
    owner = "user:7"
    default_a = store.ensure_default_session(owner_id=owner, agent_id="lily")
    default_b = store.ensure_default_session(owner_id=owner, agent_id="lily")
    other = store.create_session(owner_id=owner, agent_id="lily", title="Project B")

    assert default_a["session_id"] == default_b["session_id"]
    assert default_a["is_default"] is True

    store.bind_channel(
        owner_id=owner,
        agent_id="lily",
        surface="telegram",
        channel_key="chat-1",
        session_id=other["session_id"],
    )
    assert (
        store.resolve_session(
            owner_id=owner,
            agent_id="lily",
            surface="telegram",
            channel_key="chat-1",
        )["session_id"]
        == other["session_id"]
    )
    assert (
        store.resolve_session(
            owner_id=owner,
            agent_id="lily",
            surface="workbench",
            channel_key="window-2",
        )["session_id"]
        == default_a["session_id"]
    )

    with pytest.raises(SessionConflict):
        store.archive_session(default_a["session_id"])


def test_recent_context_never_crosses_session_or_fresh_generation(tmp_path):
    store = _store(tmp_path)
    owner = "user:7"
    first = store.ensure_default_session(owner_id=owner, agent_id="lily")
    second = store.create_session(owner_id=owner, agent_id="lily", title="Second")

    _complete(
        store,
        session_id=first["session_id"],
        owner_id=owner,
        request_id="req-first",
        key="key-first",
        text="FIRST SESSION SECRET",
        answer="first answer",
    )
    _complete(
        store,
        session_id=second["session_id"],
        owner_id=owner,
        request_id="req-second",
        key="key-second",
        text="SECOND SESSION SECRET",
        answer="second answer",
    )

    assert [
        row["user_text"] for row in store.recent_exchanges(first["session_id"])
    ] == ["FIRST SESSION SECRET"]
    assert [
        row["user_text"] for row in store.recent_exchanges(second["session_id"])
    ] == ["SECOND SESSION SECRET"]

    fresh = store.start_fresh_generation(first["session_id"])
    assert fresh["context_generation"] == 2
    assert store.recent_exchanges(first["session_id"]) == []
    assert "FIRST SESSION SECRET" in [
        message["text"] for message in store.messages(first["session_id"])
    ]


def test_last_user_activity_comes_from_all_agent_sessions(tmp_path):
    store = _store(tmp_path)
    owner = "user:7"
    default = store.ensure_default_session(owner_id=owner, agent_id="lily")
    other = store.create_session(owner_id=owner, agent_id="lily", title="Other")

    assert store.last_user_message_at(agent_id="lily", owner_id=owner) is None
    store.accept_run(
        session_id=other["session_id"],
        owner_id=owner,
        agent_id="lily",
        request_id="req-activity",
        text="active in another Session",
        source="test",
        idempotency_key="activity-key",
    )

    assert store.last_user_message_at(agent_id="lily", owner_id=owner)
    assert store.last_user_message_at(agent_id="other-agent", owner_id=owner) is None
    assert store.messages(default["session_id"]) == []


def test_accept_run_is_atomic_and_idempotency_is_digest_bound(tmp_path):
    store = _store(tmp_path)
    owner = "user:7"
    session = store.ensure_default_session(owner_id=owner, agent_id="lily")
    first = store.accept_run(
        session_id=session["session_id"],
        owner_id=owner,
        agent_id="lily",
        request_id="req-one",
        text="same request",
        source="test",
        idempotency_key="stable-key",
    )
    replay = store.accept_run(
        session_id=session["session_id"],
        owner_id=owner,
        agent_id="lily",
        request_id="req-two-never-used",
        text="same request",
        source="test",
        idempotency_key="stable-key",
    )

    assert replay.replayed is True
    assert replay.run_id == first.run_id
    assert replay.request_id == "req-one"
    assert len(store.messages(session["session_id"])) == 1

    with pytest.raises(IdempotencyConflict):
        store.accept_run(
            session_id=session["session_id"],
            owner_id=owner,
            agent_id="lily",
            request_id="req-three",
            text="different request",
            source="test",
            idempotency_key="stable-key",
        )


def test_terminal_commit_writes_message_event_projection_and_outbox_together(tmp_path):
    store = _store(tmp_path)
    owner = "user:7"
    session = store.ensure_default_session(owner_id=owner, agent_id="lily")
    accepted = _complete(
        store,
        session_id=session["session_id"],
        owner_id=owner,
        request_id="req-terminal",
        key="terminal-key",
        text="question",
        answer="answer",
    )

    snapshot = store.snapshot(session["session_id"], owner_id=owner)
    assert [message["role"] for message in snapshot["messages"]] == [
        "user",
        "assistant",
    ]
    assert snapshot["runs"][0]["state"] == "completed"
    assert snapshot["runs"][0]["run_id"] == accepted.run_id
    assert [event["kind"] for event in store.events(session["session_id"])] == [
        "session.created",
        "run.accepted",
        "run.started",
        "run.completed",
    ]
    with sqlite3.connect(store.db_path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM delivery_outbox").fetchone()[0]
            == 2
        )


def test_promotion_watermark_is_idempotent_and_archive_is_non_destructive(tmp_path):
    store = _store(tmp_path)
    owner = "user:7"
    session = store.create_session(owner_id=owner, agent_id="lily", title="Temporary")
    _complete(
        store,
        session_id=session["session_id"],
        owner_id=owner,
        request_id="req-promote",
        key="promote-key",
        text="remember all of this",
        answer="retained answer",
    )
    candidate = store.promotion_candidates(agent_id="lily")[0]

    assert store.record_promoted(agent_id="lily", candidate=candidate) is True
    assert store.record_promoted(agent_id="lily", candidate=candidate) is False
    assert store.promotion_candidates(agent_id="lily") == []

    archived = store.archive_session(session["session_id"], deleted=True)
    assert archived["status"] == "deleted"
    assert [message["text"] for message in store.messages(session["session_id"])] == [
        "remember all of this",
        "retained answer",
    ]
    assert store.promotion_status(agent_id="lily")["promoted_count"] == 1


def test_cancel_fences_late_worker_and_is_idempotent(tmp_path):
    store = _store(tmp_path)
    owner = "user:7"
    session = store.ensure_default_session(owner_id=owner, agent_id="lily")
    accepted = store.accept_run(
        session_id=session["session_id"],
        owner_id=owner,
        agent_id="lily",
        request_id="req-cancel",
        text="stop",
        source="test",
        idempotency_key="cancel-key",
    )
    token = store.mark_request_running(accepted.request_id, worker_id="worker")
    stopped = store.cancel_run(accepted.run_id, owner_id=owner)
    assert stopped["state"] == "stopped"
    assert stopped["fencing_token"] == token + 1
    assert store.cancel_run(accepted.run_id, owner_id=owner)["state"] == "stopped"
    assert (
        store.finish_request(
            accepted.request_id,
            success=True,
            assistant_text="late",
            fencing_token=token,
        )["state"]
        == "stopped"
    )


def test_attachment_owner_binding_and_approval_origin_fencing(tmp_path):
    store = _store(tmp_path)
    owner = "user:7"
    session = store.ensure_default_session(owner_id=owner, agent_id="lily")
    staged = store.stage_attachment(
        session_id=session["session_id"],
        owner_id=owner,
        filename="a.txt",
        media_type="text/plain",
        size_bytes=3,
        sha256="a" * 64,
    )
    assert (
        store.commit_attachment(
            session_id=session["session_id"],
            owner_id=owner,
            attachment_id=staged["attachment_id"],
        )["state"]
        == "committed"
    )
    with pytest.raises(SessionNotFound):
        store.commit_attachment(
            session_id=session["session_id"],
            owner_id="user:8",
            attachment_id=staged["attachment_id"],
        )

    accepted = store.accept_run(
        session_id=session["session_id"],
        owner_id=owner,
        agent_id="lily",
        request_id="req-approval",
        text="approve",
        source="test",
        idempotency_key="approval-key",
    )
    token = store.mark_request_running(accepted.request_id, worker_id="worker")
    approval = store.create_approval(
        run_id=accepted.run_id,
        owner_id=owner,
        fencing_token=token,
        scope={"tool": "write"},
    )
    assert (
        store.decide_approval(
            approval_id=approval["approval_id"], owner_id=owner, decision="approved"
        )["decision"]
        == "approved"
    )

    second = store.accept_run(
        session_id=session["session_id"],
        owner_id=owner,
        agent_id="lily",
        request_id="req-expired",
        text="expire",
        source="test",
        idempotency_key="expired-key",
    )
    token2 = store.mark_request_running(second.request_id, worker_id="worker")
    expired = store.create_approval(
        run_id=second.run_id,
        owner_id=owner,
        fencing_token=token2,
        scope={"tool": "write"},
    )
    store.cancel_run(second.run_id, owner_id=owner)
    with pytest.raises(StaleFencingToken):
        store.decide_approval(
            approval_id=expired["approval_id"], owner_id=owner, decision="approved"
        )


def test_backend_bindings_are_keyed_by_session_generation_and_backend(tmp_path):
    store = _store(tmp_path)
    session = store.ensure_default_session(owner_id="user:7", agent_id="lily")
    store.save_backend_binding(
        agent_id="lily",
        session_id=session["session_id"],
        context_generation=1,
        backend_id="codex-cli",
        backend_thread_id="thread-one",
    )

    assert (
        store.backend_binding(
            agent_id="lily",
            session_id=session["session_id"],
            context_generation=1,
            backend_id="codex-cli",
        )
        == "thread-one"
    )
    assert (
        store.backend_binding(
            agent_id="lily",
            session_id=session["session_id"],
            context_generation=2,
            backend_id="codex-cli",
        )
        is None
    )
    assert (
        store.backend_binding(
            agent_id="lily",
            session_id=session["session_id"],
            context_generation=1,
            backend_id="claude-cli",
        )
        is None
    )


def test_event_consumer_replays_until_monotonic_ack(tmp_path):
    store = _store(tmp_path)
    owner = "user:7"
    session = store.ensure_default_session(owner_id=owner, agent_id="lily")
    _complete(
        store,
        session_id=session["session_id"],
        owner_id=owner,
        request_id="req-events",
        key="events-key",
        text="question",
        answer="answer",
    )
    consumer = store.create_event_consumer(
        session_id=session["session_id"], owner_id=owner
    )

    first = store.poll_event_consumer(
        session_id=session["session_id"],
        owner_id=owner,
        consumer_id=consumer["consumer_id"],
    )
    replay = store.poll_event_consumer(
        session_id=session["session_id"],
        owner_id=owner,
        consumer_id=consumer["consumer_id"],
    )
    assert [row["event_id"] for row in replay["events"]] == [
        row["event_id"] for row in first["events"]
    ]

    issued = first["issued_through_sequence"]
    acknowledged = store.acknowledge_event_consumer(
        session_id=session["session_id"],
        owner_id=owner,
        consumer_id=consumer["consumer_id"],
        sequence=issued,
    )
    assert acknowledged["acknowledged_sequence"] == issued
    assert (
        store.poll_event_consumer(
            session_id=session["session_id"],
            owner_id=owner,
            consumer_id=consumer["consumer_id"],
        )["events"]
        == []
    )

    # A stale ACK is idempotent and cannot move the cursor backwards.
    stale = store.acknowledge_event_consumer(
        session_id=session["session_id"],
        owner_id=owner,
        consumer_id=consumer["consumer_id"],
        sequence=max(0, issued - 1),
    )
    assert stale["acknowledged_sequence"] == issued

    with pytest.raises(SessionConflict):
        store.acknowledge_event_consumer(
            session_id=session["session_id"],
            owner_id=owner,
            consumer_id=consumer["consumer_id"],
            sequence=issued + 1,
        )


def test_fresh_is_blocked_until_active_run_is_terminal(tmp_path):
    store = _store(tmp_path)
    owner = "user:7"
    session = store.ensure_default_session(owner_id=owner, agent_id="lily")
    store.accept_run(
        session_id=session["session_id"],
        owner_id=owner,
        agent_id="lily",
        request_id="req-active",
        text="still queued",
        source="test",
        idempotency_key="active-key",
    )

    with pytest.raises(SessionConflict):
        store.start_fresh_generation(session["session_id"])

    store.finish_request(
        "req-active", success=False, error_text="cancelled before execution"
    )
    assert (
        store.start_fresh_generation(session["session_id"])["context_generation"] == 2
    )


def test_restart_reconciliation_terminalizes_queued_and_running_runs_once(tmp_path):
    store = _store(tmp_path)
    owner = "user:7"
    session = store.ensure_default_session(owner_id=owner, agent_id="lily")
    queued = store.accept_run(
        session_id=session["session_id"],
        owner_id=owner,
        agent_id="lily",
        request_id="req-queued-restart",
        text="accepted before restart",
        source="test",
        idempotency_key="queued-restart",
    )
    running = store.accept_run(
        session_id=session["session_id"],
        owner_id=owner,
        agent_id="lily",
        request_id="req-running-restart",
        text="running during restart",
        source="test",
        idempotency_key="running-restart",
    )
    assert (
        store.mark_request_running(
            running.request_id, worker_id="worker-before-restart"
        )
        == 1
    )

    restarted = _store(tmp_path)
    reconciled = restarted.reconcile_incomplete_runs()

    assert {row["run_id"] for row in reconciled} == {
        queued.run_id,
        running.run_id,
    }
    for accepted, prior_state in ((queued, "queued"), (running, "running")):
        run = restarted.get_run(accepted.run_id, owner_id=owner)
        assert run["state"] == "interrupted"
        assert run["error_code"] == "runtime_restart_interrupted"
        events = restarted.events(session["session_id"], owner_id=owner)
        terminal = [
            event
            for event in events
            if event["run_id"] == accepted.run_id and event["kind"] == "run.interrupted"
        ]
        assert len(terminal) == 1
        assert terminal[0]["detail"]["prior_state"] == prior_state

    assert restarted.reconcile_incomplete_runs() == []
