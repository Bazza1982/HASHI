from __future__ import annotations

import pytest

from orchestrator.hchat_origin_binding import (
    HChatOriginConflict,
    bind_outbound_origin,
    find_outbound_origin,
    resolve_reply_origin,
)
from orchestrator.session_store import SessionStore


def test_outbound_origin_is_durable_idempotent_and_run_owned(tmp_path):
    store = SessionStore(tmp_path / "sessions.sqlite3", instance_id="HASHI1")
    owner = "user:7"
    origin = store.create_session(owner_id=owner, agent_id="lily")
    other = store.create_session(owner_id=owner, agent_id="lily")
    accepted = store.accept_run(
        session_id=origin["session_id"], owner_id=owner, agent_id="lily",
        request_id="req-source", text="send", source="test", idempotency_key="request-key",
    )
    fencing_token = store.mark_request_running(accepted.request_id, worker_id="worker")
    args = dict(
        message_id="hchat-source", owner_id=owner, agent_id="lily", session_id=origin["session_id"],
        run_id=accepted.run_id, request_id=accepted.request_id, context_generation=1,
        fencing_token=fencing_token, target="rain@HASHI2",
    )
    recorded = bind_outbound_origin(store, **args)
    assert bind_outbound_origin(store, **args) == recorded
    with pytest.raises(HChatOriginConflict):
        bind_outbound_origin(store, **{**args, "target": "other@HASHI2"})
    with pytest.raises(HChatOriginConflict):
        bind_outbound_origin(store, **{**args, "message_id": "forged", "owner_id": "user:8"})

    store.bind_primary_session(owner_id=owner, agent_id="lily", session_id=other["session_id"])
    reopened = SessionStore(tmp_path / "sessions.sqlite3", instance_id="HASHI1")
    assert find_outbound_origin(reopened, "hchat-source")["session_id"] == origin["session_id"]
    assert find_outbound_origin(reopened, "unknown") is None
    route = {"conversation_id": "conv-1", "from_instance": "HASHI1", "from_agent": "lily",
             "to_instance": "HASHI2", "to_agent": "rain"}
    reply = dict(correlation=route, in_reply_to="hchat-source", conversation_id="conv-1",
                 from_instance="HASHI2", from_agent="rain", to_instance="HASHI1", to_agent="lily")
    assert resolve_reply_origin(reopened, **reply)["session_id"] == origin["session_id"]
    with pytest.raises(HChatOriginConflict):
        resolve_reply_origin(reopened, **{**reply, "from_agent": "other"})
    with pytest.raises(HChatOriginConflict):
        resolve_reply_origin(reopened, **{**reply, "correlation": {**route, "to_instance": "HASHI9"}})

    store.archive_session(origin["session_id"], deleted=True)
    with pytest.raises(HChatOriginConflict):
        find_outbound_origin(reopened, "hchat-source")


def test_group_origins_freeze_recipients_and_preserve_confirmed_or_uncertain_attempts(tmp_path):
    from orchestrator.hchat_group_origin import HChatGroupOrigin
    from orchestrator.hchat_submit_result import HChatSubmission

    path = tmp_path / "sessions.sqlite3"
    store = SessionStore(path, instance_id="HASHI1")
    session = store.create_session(owner_id="user:7", agent_id="lily")
    run = store.accept_run(
        session_id=session["session_id"], owner_id="user:7", agent_id="lily",
        request_id="req-group", text="send", source="test", idempotency_key="group-run",
    )
    fence = store.mark_request_running(run.request_id, worker_id="worker")
    identity = dict(
        parent_message_id="group-message", owner_id="user:7", agent_id="lily",
        session_id=session["session_id"], run_id=run.run_id, request_id=run.request_id,
        context_generation=1, fencing_token=fence, target="@reviewers", text="please review",
    )
    group = HChatGroupOrigin(store, **identity)
    assert group.prepare(None) is None
    attachment_manifest = [{"filename": "report.txt", "size_bytes": 6, "sha256": "a" * 64}]
    snapshot = group.prepare(
        ["akane@HASHI1", "rain@HASHI9", "other@HASHI3"],
        attachment_manifest=attachment_manifest,
    )
    assert [item["recipient"] for item in snapshot] == ["akane@HASHI1", "rain@HASHI9", "other@HASHI3"]
    assert len({item["message_id"] for item in snapshot}) == 3
    for item in snapshot:
        origin = find_outbound_origin(store, item["message_id"])
        assert origin["session_id"] == session["session_id"]
        assert origin["target"] == item["recipient"].casefold()
        assert origin["originated_at"] == item["originated_at"]
    rain = snapshot[1]
    route = {
        "conversation_id": "conv-rain", "from_instance": "HASHI1", "from_agent": "lily",
        "to_instance": "HASHI9", "to_agent": "rain",
    }
    reply = dict(
        correlation=route, in_reply_to=rain["message_id"], conversation_id="conv-rain",
        from_instance="HASHI9", from_agent="rain", to_instance="HASHI1", to_agent="lily",
    )
    assert resolve_reply_origin(store, **reply)["session_id"] == session["session_id"]
    with pytest.raises(HChatOriginConflict):
        resolve_reply_origin(store, **{**reply, "from_agent": "other"})

    assert group.begin("akane@HASHI1") is None
    group.finish("akane@HASHI1", HChatSubmission(True, "queued", True))
    assert group.begin("rain@HASHI9") is None  # the process may stop after dispatch
    assert group.begin("other@HASHI3") is None
    group.finish("other@HASHI3", HChatSubmission(False, "failed", False))

    reopened = HChatGroupOrigin(SessionStore(path, instance_id="HASHI1"), **identity)
    assert reopened.prepare(["changed@HASHI8"], attachment_manifest=attachment_manifest) == snapshot
    with pytest.raises(HChatOriginConflict):
        reopened.prepare(None, attachment_manifest=[{
            "filename": "report.txt", "size_bytes": 6, "sha256": "b" * 64,
        }])
    accepted = reopened.begin("akane@HASHI1")
    assert accepted.handled and accepted.accepted is True and accepted.status == "queued"
    uncertain = reopened.begin("rain@HASHI9")
    assert not uncertain.handled and uncertain.accepted is None and uncertain.status == "unconfirmed"
    assert reopened.begin("other@HASHI3") is None  # a known rejection can be retried

    with pytest.raises(HChatOriginConflict):
        HChatGroupOrigin(store, **{**identity, "text": "changed content"}).prepare(None)
    with pytest.raises(HChatOriginConflict):
        HChatGroupOrigin(store, **{**identity, "owner_id": "user:8"}).prepare(None)
    with pytest.raises(HChatOriginConflict):
        HChatGroupOrigin(store, **{**identity, "fencing_token": fence + 1}).prepare(None)
    with pytest.raises(HChatOriginConflict):
        reopened.begin("unknown@HASHI9")

def test_group_recipient_concurrent_claims_never_dispatch_twice(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from orchestrator.hchat_group_origin import HChatGroupOrigin

    path = tmp_path / "sessions.sqlite3"
    store = SessionStore(path, instance_id="HASHI1")
    session = store.create_session(owner_id="user:7", agent_id="lily")
    run = store.accept_run(
        session_id=session["session_id"], owner_id="user:7", agent_id="lily",
        request_id="req-concurrent", text="send", source="test", idempotency_key="group-concurrent",
    )
    fence = store.mark_request_running(run.request_id, worker_id="worker")
    identity = dict(
        parent_message_id="group-concurrent", owner_id="user:7", agent_id="lily",
        session_id=session["session_id"], run_id=run.run_id, request_id=run.request_id,
        context_generation=1, fencing_token=fence, target="@reviewers", text="please review",
    )
    first = HChatGroupOrigin(store, **identity)
    second = HChatGroupOrigin(SessionStore(path, instance_id="HASHI1"), **identity)
    first.prepare(["rain@HASHI9"])
    barrier = Barrier(2)
    def claim(group):
        barrier.wait()
        return group.begin("rain@HASHI9")
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(claim, (first, second)))
    assert outcomes.count(None) == 1
    [unconfirmed] = [value for value in outcomes if value is not None]
    assert unconfirmed.status == "unconfirmed" and unconfirmed.accepted is None
