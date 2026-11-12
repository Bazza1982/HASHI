"""FC catch-up policy: original-time deadlines, durable outcomes and no stale noise."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from orchestrator import telegram_delivery_failover
from orchestrator.frontend_incremental_delivery import (
    dispatch_pending_telegram_deliverables,
)
from orchestrator.session_store import SessionStore
from tests.test_telegram_connector_transport import setup_delivery


def queued(tmp_path, kind):
    route, store, owner, session, accepted, runtime = setup_delivery(tmp_path)
    runtime.global_config.ui_language = "zh-CN"
    if kind in {"final", "failure", "steer"}:
        store.finish_request(
            accepted.request_id,
            success=kind == "final",
            assistant_text="The durable report" if kind == "final" else None,
            error_text="Interrupted by user_steer"
            if kind == "steer"
            else "Provider unavailable",
        )
        event = next(
            e
            for e in store.events(session["session_id"], owner_id=owner)
            if e["kind"] in {"run.completed", "run.failed"}
        )
    else:
        message = store.append_presentation_message(
            session_id=session["session_id"],
            owner_id=owner,
            agent_id="agent1",
            role="assistant",
            text="Restart complete"
            if kind == "command_result"
            else "Starting / progress",
            source="frontend.runtime_notice",
            idempotency_key="notice-" + kind,
            presentation_channel="notification",
            history_eligible=False,
            message_context={"delivery_recovery": {"category": kind}},
            outbox=True,
            delivery_route=route,
        )
        event = next(
            e
            for e in store.events(session["session_id"], owner_id=owner)
            if e["event_id"] == message["delivery_event_id"]
        )
    return store, owner, session, accepted, runtime, event


def age_event(store, event, seconds):
    original = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    stamp = original.isoformat()
    with store._connection() as db:
        db.execute(
            "UPDATE run_events SET created_at=? WHERE event_id=?",
            (stamp, event["event_id"]),
        )
    return original


def task(store, event):
    with store._connection() as db:
        return dict(
            db.execute(
                "SELECT * FROM connector_delivery_tasks WHERE event_id=? AND connector_id='telegram'",
                (event["event_id"],),
            ).fetchone()
        )


@pytest.fixture(autouse=True)
def isolated_transport(monkeypatch):
    monkeypatch.setattr(
        telegram_delivery_failover, "handle_blocked_send", AsyncMock(return_value=False)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,seconds,expected",
    [
        ("final", 7190, "completed"),
        ("final", 7201, "expired"),
        ("failure", 590, "completed"),
        ("failure", 601, "expired"),
        ("command_result", 590, "completed"),
        ("command_result", 601, "expired"),
        ("steer", 1, "suppressed"),
        ("transient", 1, "suppressed"),
    ],
)
async def test_recovery_uses_original_event_deadline_and_keeps_fc_history(
    tmp_path, kind, seconds, expected
):
    store, owner, session, accepted, runtime, event = queued(tmp_path, kind)
    original = age_event(store, event, seconds)
    # Recovery reopens persisted state; task creation/reconnect is newer than the Event.
    runtime.session_store = SessionStore(store.db_path, instance_id=store.instance_id)
    await dispatch_pending_telegram_deliverables(runtime)
    actual = task(store, event)
    assert actual["state"] == expected
    assert bool(runtime.app.bot.messages) == (expected == "completed")
    if expected == "completed":
        text = runtime.app.bot.messages[0]["text"]
        assert "补发" in text
        assert original.strftime("%Y-%m-%d") in text
        assert original.strftime("%H:%M:%S") in text
        assert "UTC" in text
    else:
        assert actual["delivered_at"] is None
        assert actual["last_error_code"].startswith("telegram_replay_")
    assert await dispatch_pending_telegram_deliverables(runtime) == 0
    preserved = next(
        e
        for e in store.events(session["session_id"], owner_id=owner)
        if e["event_id"] == event["event_id"]
    )
    assert preserved["created_at"] == original.isoformat()
    # FC history remains authoritative; no new assistant Message is made for catch-up.
    with store._connection() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM run_events WHERE event_id=?", (event["event_id"],)
            ).fetchone()[0]
            == 1
        )
        assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_expiry_does_not_touch_primary_delivery_or_unknown_telegram_outcome(
    tmp_path,
):
    store, owner, session, accepted, runtime, event = queued(tmp_path, "final")
    age_event(store, event, 7201)
    with store._connection() as db:
        db.execute(
            "UPDATE connector_delivery_tasks SET state='unknown' "
            "WHERE connector_id='telegram' AND event_id=?",
            (event["event_id"],),
        )
    await dispatch_pending_telegram_deliverables(runtime)
    assert task(store, event)["state"] == "unknown"
    assert runtime.app.bot.messages == []
    with store._connection() as db:
        assert (
            db.execute(
                "SELECT state FROM connector_delivery_tasks "
                "WHERE connector_id='backend_api' AND event_id=?",
                (event["event_id"],),
            ).fetchone()[0]
            == "pending"
        )


@pytest.mark.asyncio
async def test_resolved_question_is_not_replayed_even_when_young(tmp_path):
    from orchestrator.run_questions import RunQuestions

    route, store, owner, session, accepted, runtime = setup_delivery(tmp_path)
    questions = RunQuestions(store)
    question = questions.create(
        request_id=accepted.request_id,
        agent_id="agent1",
        payload={
            "question": "Proceed with blue?",
            "options": [{"id": "blue", "label": "Blue"}],
            "allow_free_text": True,
            "idempotency_key": "question",
        },
    )
    questions.answer(
        session_id=session["session_id"],
        owner_id=owner,
        question_id=question["question_id"],
        payload={"idempotency_key": "answer", "option_id": "blue"},
    )
    event = next(
        e
        for e in store.events(session["session_id"], owner_id=owner)
        if e["kind"] == "run.question.created"
    )
    await dispatch_pending_telegram_deliverables(runtime)
    assert runtime.app.bot.messages == []
    assert task(store, event)["state"] == "suppressed"


@pytest.mark.asyncio
async def test_newer_fc_result_explicitly_supersedes_old_output(tmp_path):
    import json

    store, owner, session, accepted, runtime, event = queued(tmp_path, "final")
    newer = store.append_presentation_message(
        session_id=session["session_id"],
        owner_id=owner,
        agent_id="agent1",
        role="assistant",
        text="Corrected report",
        source="frontend.result",
        idempotency_key="corrected-result",
        presentation_channel="final",
        outbox=False,
    )
    with store._connection() as db:
        detail = dict(event["detail"], superseded_by=newer["delivery_event_id"])
        db.execute(
            "UPDATE run_events SET detail_json=? WHERE event_id=?",
            (json.dumps(detail), event["event_id"]),
        )
    await dispatch_pending_telegram_deliverables(runtime)
    assert runtime.app.bot.messages == []
    assert task(store, event)["state"] == "suppressed"


# HASHI-Fingerprint: hfp_8f935ba2798b4522a46b83d55b655c00


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,ttl", [("final", 7200), ("failure", 600), ("command_result", 600)]
)
async def test_exact_expiry_boundary_is_closed(tmp_path, kind, ttl):
    from orchestrator.frontend_dispatch import project_claimed_frontend_event
    from orchestrator.frontend_telegram_replay import telegram_replay_decision

    store, owner, session, accepted, runtime, event = queued(tmp_path, kind)
    original = datetime.fromisoformat(event["created_at"].replace("Z", "+00:00"))
    claim = store.claim_delivery_outbox(
        session_id=session["session_id"],
        owner_id=owner,
        worker_id="boundary",
        event_id=event["event_id"],
        connector_id="telegram",
    )[0]
    standard = project_claimed_frontend_event(
        store, claim, session_id=session["session_id"], owner_id=owner
    )

    def decision(at):
        return telegram_replay_decision(
            store, claim, standard, owner_id=owner, agent_id="agent1", chat_id=7, now=at
        )

    assert decision(original + timedelta(seconds=ttl, microseconds=-1)).state == "send"
    assert decision(original + timedelta(seconds=ttl)).state == "expired"


@pytest.mark.asyncio
@pytest.mark.parametrize("close", ["own_deadline", "cancel", "run_ended"])
async def test_question_cannot_outlive_its_real_validity(tmp_path, close):
    from orchestrator.run_questions import RunQuestions

    route, store, owner, session, accepted, runtime = setup_delivery(tmp_path)
    questions = RunQuestions(store)
    question = questions.create(
        request_id=accepted.request_id,
        agent_id="agent1",
        payload={
            "question": "Choose blue?",
            "options": [{"id": "blue", "label": "Blue"}],
            "idempotency_key": "q",
        },
    )
    if close == "own_deadline":
        with store._connection() as db:
            db.execute(
                "UPDATE run_questions SET expires_epoch=? WHERE question_id=?",
                (datetime.now(timezone.utc).timestamp() - 1, question["question_id"]),
            )
    elif close == "cancel":
        questions.cancel(
            request_id=accepted.request_id,
            agent_id="agent1",
            question_id=question["question_id"],
        )
    else:
        store.finish_request(
            accepted.request_id, success=True, assistant_text="Already finished"
        )
    await dispatch_pending_telegram_deliverables(runtime)
    assert not any("Choose blue?" in m["text"] for m in runtime.app.bot.messages)
    event = next(
        e
        for e in store.events(session["session_id"], owner_id=owner)
        if e["kind"] == "run.question.created"
    )
    assert task(store, event)["state"] in {"suppressed", "expired"}


@pytest.mark.asyncio
@pytest.mark.parametrize("seconds,delivered", [(7190, True), (7201, False)])
async def test_file_catch_up_preserves_asset_but_expires_its_telegram_task(
    tmp_path, seconds, delivered
):
    import json
    from tests.test_frontend_attachment_tool import _registry

    route, store, owner, session, accepted, runtime = setup_delivery(tmp_path)
    runtime.global_config.ui_language = "zh-CN"
    report = tmp_path / "report.txt"
    report.write_bytes(b"the finished report")
    registry = _registry(
        tmp_path,
        store,
        owner,
        session,
        allowed_tools=["frontend_publish_deliverable"],
        runtime=None,
    )
    result = await registry.execute(
        "frontend_publish_deliverable",
        {"publication_id": "file-result", "attachments": [{"path": str(report)}]},
        tool_call_id="publish",
    )
    assert not result.is_error, result.output
    published = json.loads(result.output)
    event = next(
        e
        for e in store.events(session["session_id"], owner_id=owner)
        if e["event_id"] == published["event_id"]
    )
    original = age_event(store, event, seconds)
    await dispatch_pending_telegram_deliverables(runtime)
    assert bool(runtime.app.bot.media) == delivered
    assert task(store, event)["state"] == ("completed" if delivered else "expired")
    assert report.read_bytes() == b"the finished report"
    publication = store.run_deliverable_publication(
        request_id=accepted.request_id,
        session_id=session["session_id"],
        owner_id=owner,
        agent_id="agent1",
        publication_id="file-result",
    )
    telegram = next(
        d for d in publication["deliveries"] if d["connector_id"] == "telegram"
    )
    assert telegram["state"] == ("delivered" if delivered else "skipped")
    assert store.get_message(
        published["message_id"], session_id=session["session_id"], owner_id=owner
    )
    if delivered:
        assert runtime.app.bot.media[0]["payload"] == b"the finished report"
        assert "补发" in runtime.app.bot.messages[0]["text"]
        assert original.strftime("%H:%M:%S") in runtime.app.bot.messages[0]["text"]


@pytest.mark.asyncio
async def test_normal_immediate_final_is_not_relabelled_as_catch_up(tmp_path):
    from orchestrator.runtime_delivery import send_long_message

    store, owner, session, accepted, runtime, event = queued(tmp_path, "final")
    await send_long_message(
        runtime,
        chat_id=7,
        text="caller copy",
        request_id=accepted.request_id,
        frontend_outbox=True,
    )
    assert len(runtime.app.bot.messages) == 1
    assert runtime.app.bot.messages[0]["text"] == "The durable report"


@pytest.mark.asyncio
@pytest.mark.parametrize("seconds,expected", [(590, "completed"), (601, "expired")])
async def test_native_notice_sender_uses_same_deadline_and_original_timestamp(
    tmp_path, seconds, expected
):
    from types import SimpleNamespace
    from orchestrator.frontend_notice_delivery import deliver_notice
    from orchestrator.runtime_session import record_kernel_presentation_notice

    route, store, owner, session, accepted, runtime = setup_delivery(tmp_path)
    runtime.global_config.ui_language = "zh-CN"
    kernel = SimpleNamespace(
        global_cfg=runtime.global_config,
        session_store=store,
        _runtime_map=lambda: {"agent1": runtime},
    )
    origin = dict(
        surface="telegram",
        chat_id=7,
        owner_id=owner,
        session_id=session["session_id"],
        context_generation=1,
        delivery_route=route,
    )
    message = record_kernel_presentation_notice(
        kernel,
        agent_id="agent1",
        origin=origin,
        text="Restart complete",
        idempotency_key="reboot:one:final",
    )
    event = next(
        e
        for e in store.events(session["session_id"], owner_id=owner)
        if e["event_id"] == message["delivery_event_id"]
    )
    original = age_event(store, event, seconds)
    sent = []

    async def send_notice(*args, **kwargs):
        sent.append(kwargs["render_text"]("agent1", None))
        return {"sent": True, "message_id": 501}

    await deliver_notice(
        kernel,
        agent_id="agent1",
        origin=origin,
        text="Restart complete",
        publication_id="reboot:one:final",
        send_notice=send_notice,
        recovery=True,
    )
    assert task(store, event)["state"] == expected
    assert bool(sent) == (expected == "completed")
    if sent:
        assert "补发" in sent[0] and original.strftime("%H:%M:%S") in sent[0]


@pytest.mark.asyncio
async def test_direct_error_retry_keeps_catch_up_label_instead_of_reformatting_it_away(
    tmp_path,
):
    from orchestrator.runtime_delivery import send_long_message

    store, owner, session, accepted, runtime, event = queued(tmp_path, "failure")
    original = age_event(store, event, 60)
    claim = store.claim_delivery_outbox(
        session_id=session["session_id"],
        owner_id=owner,
        worker_id="proved-offline",
        event_id=event["event_id"],
        connector_id="telegram",
    )[0]
    store.complete_delivery_outbox(
        outbox_id=claim["outbox_id"],
        lease_token=claim["lease_token"],
        status="retry",
        retry_safe=True,
        error_code="telegram_disconnected",
    )
    await send_long_message(
        runtime,
        chat_id=7,
        text="caller copy",
        request_id=accepted.request_id,
        purpose="error",
        frontend_outbox=True,
    )
    text = runtime.app.bot.messages[0]["text"]
    assert "补发" in text and original.strftime("%H:%M:%S") in text
    assert "Provider unavailable" in text


@pytest.mark.asyncio
async def test_late_first_publication_uses_operation_time_not_reconnection_time(
    tmp_path,
):
    from types import SimpleNamespace
    from orchestrator.frontend_notice_delivery import deliver_notice

    route, store, owner, session, accepted, runtime = setup_delivery(tmp_path)
    kernel = SimpleNamespace(
        global_cfg=runtime.global_config,
        session_store=store,
        _runtime_map=lambda: {"agent1": runtime},
    )
    origin = dict(
        surface="telegram",
        chat_id=7,
        owner_id=owner,
        session_id=session["session_id"],
        context_generation=1,
        delivery_route=route,
    )
    send_notice = AsyncMock()
    result = await deliver_notice(
        kernel,
        agent_id="agent1",
        origin=origin,
        text="Old restart complete",
        publication_id="reboot:late:first",
        send_notice=send_notice,
        recovery=True,
        original_at=(datetime.now(timezone.utc) - timedelta(seconds=601)).timestamp(),
    )
    send_notice.assert_not_awaited()
    assert result["delivery_state"] == "expired" and not result["sent"]
    event = next(
        e
        for e in store.events(session["session_id"], owner_id=owner)
        if e["event_id"] == result["delivery_event_id"]
    )
    assert task(store, event)["state"] == "expired"


def test_replay_label_has_localized_notice_and_unambiguous_original_time():
    from orchestrator.frontend_telegram_replay import ReplayDecision

    at = datetime(2026, 11, 12, 6, 3, 4, tzinfo=timezone.utc)
    decision = ReplayDecision("send", "eligible", at)
    assert "补发" in decision.label(locale="zh-CN")
    assert "Re-delivered" in decision.label(locale="en")
    assert all(
        "2026-11-12 06:03:04 UTC" in decision.label(locale=locale)
        for locale in ("en", "zh-CN")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("before_connection", [False, True])
async def test_healthy_queue_delivery_is_not_mislabelled_as_catch_up(
    tmp_path, before_connection
):
    store, owner, session, accepted, runtime, event = queued(tmp_path, "final")
    original = datetime.fromisoformat(event["created_at"].replace("Z", "+00:00"))
    runtime._telegram_delivery_connected_at = (
        original + timedelta(seconds=1)
        if before_connection
        else original - timedelta(seconds=1)
    ).timestamp()
    await dispatch_pending_telegram_deliverables(runtime)
    text = runtime.app.bot.messages[0]["text"]
    assert ("补发" in text) == before_connection
    assert "The durable report" in text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,seconds,expected",
    [
        ("final", 7200, "expired"),
        ("failure", 600, "expired"),
        ("final", 7199, "completed"),
        ("failure", 599, "completed"),
    ],
)
async def test_direct_first_claim_checks_original_deadline_after_offline_reopen(
    tmp_path, monkeypatch, kind, seconds, expected
):
    from orchestrator import frontend_telegram_replay
    from orchestrator.runtime_delivery import send_long_message

    store, owner, session, accepted, runtime, event = queued(tmp_path, kind)
    runtime.telegram_connected = False
    # This is the normal direct Run delivery entry, with no recovery flag.
    assert await send_long_message(
        runtime,
        chat_id=7,
        text="caller copy",
        request_id=accepted.request_id,
        frontend_outbox=True,
    ) == (0.0, 0)
    assert task(store, event)["attempt_count"] == 0
    original = age_event(store, event, 0)
    observed = original + timedelta(seconds=seconds)
    monkeypatch.setattr(frontend_telegram_replay, "utc_now", lambda: observed)
    runtime.session_store = SessionStore(store.db_path, instance_id=store.instance_id)
    runtime.telegram_connected = True
    runtime._telegram_delivery_connected_at = observed.timestamp()
    await send_long_message(
        runtime,
        chat_id=7,
        text="caller copy",
        request_id=accepted.request_id,
        frontend_outbox=True,
    )
    result = task(store, event)
    assert result["attempt_count"] == 1
    assert result["state"] == expected
    assert bool(runtime.app.bot.messages) == (expected == "completed")
    if expected == "expired":
        assert result["delivered_at"] is None
        assert result["last_error_code"] == "telegram_replay_expired"
    else:
        text = runtime.app.bot.messages[0]["text"]
        assert "补发" in text and original.strftime("%H:%M:%S") in text
        assert "caller copy" not in text
    with store._connection() as db:
        assert (
            db.execute(
                "SELECT state FROM connector_delivery_tasks "
                "WHERE connector_id='backend_api' AND event_id=?",
                (event["event_id"],),
            ).fetchone()[0]
            == "pending"
        )
        assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    preserved = next(
        e
        for e in store.events(session["session_id"], owner_id=owner)
        if e["event_id"] == event["event_id"]
    )
    assert preserved["created_at"] == original.isoformat()
    count = len(runtime.app.bot.messages)
    assert await dispatch_pending_telegram_deliverables(runtime) == 0
    assert len(runtime.app.bot.messages) == count


@pytest.mark.asyncio
async def test_first_explicit_publication_claim_cannot_bypass_expiry(
    tmp_path, monkeypatch
):
    from orchestrator import frontend_telegram_replay
    from orchestrator.runtime_delivery import send_long_message

    route, store, owner, session, accepted, runtime = setup_delivery(tmp_path)
    runtime.telegram_connected = False
    kwargs = dict(
        chat_id=7,
        text="Original diagnostic",
        request_id=accepted.request_id,
        purpose="error",
    )
    await send_long_message(runtime, **kwargs)
    event = next(
        e
        for e in store.events(session["session_id"], owner_id=owner)
        if e["kind"] == "frontend.message.recorded"
        and e["detail"]["source"] == "telegram.long-message"
    )
    assert task(store, event)["attempt_count"] == 0
    original = age_event(store, event, 0)
    observed = original + timedelta(seconds=600)
    monkeypatch.setattr(frontend_telegram_replay, "utc_now", lambda: observed)
    runtime.telegram_connected = True
    runtime._telegram_delivery_connected_at = observed.timestamp()
    await send_long_message(runtime, **kwargs)
    result = task(store, event)
    assert result["attempt_count"] == 1 and result["state"] == "expired"
    assert runtime.app.bot.messages == []
    assert store.get_run(accepted.run_id)["state"] == "running"


@pytest.mark.asyncio
async def test_first_direct_file_claim_without_recovery_hint_still_expires(
    tmp_path, monkeypatch
):
    import json
    from orchestrator import frontend_telegram_replay
    from orchestrator.frontend_incremental_delivery import (
        dispatch_incremental_telegram_event,
    )
    from tests.test_frontend_attachment_tool import _registry

    route, store, owner, session, accepted, runtime = setup_delivery(tmp_path)
    report = tmp_path / "report.txt"
    report.write_bytes(b"preserved report")
    registry = _registry(
        tmp_path,
        store,
        owner,
        session,
        allowed_tools=["frontend_publish_deliverable"],
        runtime=None,
    )
    result = await registry.execute(
        "frontend_publish_deliverable",
        {"publication_id": "delayed-file", "attachments": [{"path": str(report)}]},
        tool_call_id="publish",
    )
    assert not result.is_error, result.output
    publication = json.loads(result.output)
    event = next(
        e
        for e in store.events(session["session_id"], owner_id=owner)
        if e["event_id"] == publication["event_id"]
    )
    original = age_event(store, event, 0)
    observed = original + timedelta(seconds=7200)
    monkeypatch.setattr(frontend_telegram_replay, "utc_now", lambda: observed)
    runtime._telegram_delivery_connected_at = observed.timestamp()
    await dispatch_incremental_telegram_event(
        store,
        runtime,
        session_id=session["session_id"],
        owner_id=owner,
        request_id=accepted.request_id,
        event_id=event["event_id"],
        delivery_route=route,
    )
    result = task(store, event)
    assert result["attempt_count"] == 1 and result["state"] == "expired"
    assert runtime.app.bot.messages == [] and runtime.app.bot.media == []
    assert report.read_bytes() == b"preserved report"


@pytest.mark.asyncio
async def test_first_direct_start_notice_has_no_unbounded_initial_exception(tmp_path):
    from types import SimpleNamespace
    from orchestrator.frontend_notice_delivery import deliver_notice

    route, store, owner, session, accepted, runtime = setup_delivery(tmp_path)
    kernel = SimpleNamespace(
        global_cfg=runtime.global_config,
        session_store=store,
        _runtime_map=lambda: {"agent1": runtime},
    )
    origin = dict(
        surface="telegram",
        chat_id=7,
        owner_id=owner,
        session_id=session["session_id"],
        context_generation=1,
        delivery_route=route,
    )
    send_notice = AsyncMock()
    result = await deliver_notice(
        kernel,
        agent_id="agent1",
        origin=origin,
        text="Starting",
        publication_id="reboot:delayed:starting",
        send_notice=send_notice,
        recovery_category="transient",
        recovery=False,
        original_at=(datetime.now(timezone.utc) - timedelta(seconds=601)).timestamp(),
    )
    send_notice.assert_not_awaited()
    assert result["delivery_state"] == "expired" and not result["sent"]
