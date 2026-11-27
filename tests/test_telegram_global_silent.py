"""Instance-wide Telegram safety silence: /telegram silent global."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from orchestrator import telegram_silent, ui_language
from orchestrator.commands import telegram as telegram_command_module
from orchestrator.config_json import write_config_json
from orchestrator.connector_delivery_preferences import (
    instance_telegram_mirror_enabled,
    instance_telegram_silenced,
    state_path,
)
from orchestrator.frontend_delivery import (
    RUN_DELIVERY_ROUTE_METADATA_KEY,
    RUN_DELIVERY_ROUTE_TYPE,
    RUN_DELIVERY_ROUTE_VERSION,
    telegram_delivery_for_admission,
    telegram_mirror_allowed_for_run,
)
from orchestrator import runtime_delivery
from orchestrator import telegram_update_ack


def _write_instance(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    config_path = root / "agents.json"
    write_config_json(
        config_path,
        {
            "global": {"authorized_id": 7},
            "agents": [
                {
                    "name": "alpha",
                    "workspace_dir": "workspaces/alpha",
                    "type": "flex",
                    "telegram_mirror_policy": "always_on",
                    "is_active": True,
                },
                {
                    "name": "beta",
                    "workspace_dir": "workspaces/beta",
                    "type": "flex",
                    "is_active": True,
                },
            ],
        },
    )
    return config_path


def _runtime(root: Path, config_path: Path, sent: list[str]) -> NS:
    async def reply(update, text, *, parse_mode=None, reply_markup=None):
        sent.append(text)

    return NS(
        name="alpha",
        global_config=NS(
            authorized_id=7,
            bridge_home=root,
            config_path=str(config_path),
            instance_id="SILENT_TEST",
        ),
        _is_authorized_user=lambda actor: actor == 7,
        _reply_text=reply,
    )


def _update():
    return NS(
        update_id=11,
        effective_user=NS(id=7),
        effective_chat=NS(id=7),
        _hashi_session_owner_id="user:123",
    )


async def _run(runtime: NS, args: list[str]) -> None:
    with ui_language.language_scope(None, locale="en"):
        await telegram_command_module.telegram_command(
            runtime, _update(), NS(args=args)
        )


@pytest.fixture(autouse=True)
def _clear_counters():
    telegram_silent.reset_counters()
    yield
    telegram_silent.reset_counters()


@pytest.mark.asyncio
async def test_silent_drops_every_inbound_update_and_still_acknowledges(
    tmp_path, monkeypatch
):
    config_path = _write_instance(tmp_path)
    runtime = _runtime(tmp_path, config_path, [])
    handled: list[object] = []

    async def fake_process(runtime_, update):
        handled.append(update)

    monkeypatch.setattr(
        "orchestrator.telegram_actor_scope.scoped_access_enabled", lambda runtime_: True
    )
    monkeypatch.setattr(
        "orchestrator.telegram_actor_scope.process_telegram_update", fake_process
    )

    await telegram_update_ack.dispatch_update(runtime, _update())
    assert len(handled) == 1
    handled.clear()

    await _run(runtime, ["silent", "global"])
    assert instance_telegram_silenced(tmp_path) is True

    # Dropping is explicit: returning normally acknowledges the update so the
    # Bot offset advances instead of building a backlog.
    await telegram_update_ack.dispatch_update(runtime, _update())
    await telegram_update_ack.dispatch_update(runtime, _update())
    assert handled == []
    assert telegram_silent.dropped_since_start(runtime) == 2
    assert "2" in (await _status(runtime))

    await _run(runtime, ["silent", "off", "global"])
    assert instance_telegram_silenced(tmp_path) is False
    await telegram_update_ack.dispatch_update(runtime, _update())
    assert len(handled) == 1


async def _status(runtime: NS) -> str:
    with ui_language.language_scope(None, locale="en"):
        return telegram_command_module._global_status_text(runtime)


@pytest.mark.asyncio
async def test_silent_suppresses_mirror_even_for_always_on_agents(tmp_path):
    config_path = _write_instance(tmp_path)
    runtime = _runtime(tmp_path, config_path, [])
    admission = dict(
        request_metadata={"owner_id": "user:123"},
        state_root=tmp_path,
        agent_id="alpha",
        agent_config_path=config_path,
    )
    assert telegram_delivery_for_admission(source="workbench", **admission) is True

    await _run(runtime, ["silent", "global"])

    assert telegram_delivery_for_admission(source="workbench", **admission) is False
    # Telegram-origin traffic is a primary route, never a mirror; the silence
    # itself removes it at the ingest point, not here.
    assert telegram_delivery_for_admission(source="telegram", **admission) is True
    assert instance_telegram_mirror_enabled(tmp_path) is True


@pytest.mark.asyncio
async def test_silent_stops_a_pending_mirror_of_a_frozen_run(tmp_path):
    config_path = _write_instance(tmp_path)
    runtime = _runtime(tmp_path, config_path, [])
    route = {
        "type": RUN_DELIVERY_ROUTE_TYPE,
        "version": RUN_DELIVERY_ROUTE_VERSION,
        "scope": "run",
        "primary": {"surface": "workbench", "channel_key": "user:123"},
        "mirrors": [{"surface": "telegram", "channel_key": "user:123"}],
        "automatic": True,
    }
    runtime._request_meta_by_id = {
        "req-silent": {
            "owner_id": "user:123",
            "request_metadata": {RUN_DELIVERY_ROUTE_METADATA_KEY: route},
        }
    }
    assert telegram_mirror_allowed_for_run(runtime, "req-silent") is True

    await _run(runtime, ["silent", "global"])

    assert telegram_mirror_allowed_for_run(runtime, "req-silent") is False


@pytest.mark.asyncio
async def test_silent_suppresses_outbound_delivery_at_send_time(tmp_path):
    config_path = _write_instance(tmp_path)
    runtime = _runtime(tmp_path, config_path, [])

    await _run(runtime, ["silent", "global"])

    duration, chunks = await runtime_delivery.send_long_message(
        runtime, chat_id=7, text="reply that must not leave", request_id="req-1"
    )
    assert (duration, chunks) == (0.0, 0)

    # The receipt announcing the change is the single exempt message.
    with telegram_silent.last_notice():
        assert telegram_silent.suppressed(runtime) is False
    assert telegram_silent.suppressed(runtime) is True


@pytest.mark.asyncio
async def test_on_global_clears_both_switches_and_off_global_keeps_silence(
    tmp_path,
):
    config_path = _write_instance(tmp_path)
    sent: list[str] = []
    runtime = _runtime(tmp_path, config_path, sent)

    await _run(runtime, ["off", "global"])
    await _run(runtime, ["silent", "global"])
    assert instance_telegram_silenced(tmp_path) is True

    # "off global" is the mirror switch only: it must not lift a safety cut.
    await _run(runtime, ["off", "global"])
    assert instance_telegram_silenced(tmp_path) is True
    assert instance_telegram_mirror_enabled(tmp_path) is False

    await _run(runtime, ["on", "global"])
    assert instance_telegram_silenced(tmp_path) is False
    assert instance_telegram_mirror_enabled(tmp_path) is True
    assert sent[-1].count("Safety silence") == 1


@pytest.mark.asyncio
async def test_silent_is_instance_wide_only(tmp_path):
    config_path = _write_instance(tmp_path)
    sent: list[str] = []
    runtime = _runtime(tmp_path, config_path, sent)

    await _run(runtime, ["silent"])
    assert instance_telegram_silenced(tmp_path) is False
    assert "silent global" in sent[-1]


@pytest.mark.asyncio
async def test_silence_is_scoped_to_one_instance(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first_config = _write_instance(first)
    second_config = _write_instance(second)
    sent: list[str] = []
    first_runtime = _runtime(first, first_config, sent)
    second_runtime = _runtime(second, second_config, sent)

    await _run(first_runtime, ["silent", "global"])

    assert instance_telegram_silenced(first) is True
    assert instance_telegram_silenced(second) is False
    admission = dict(
        request_metadata={"owner_id": "user:123"},
        state_root=second,
        agent_id="alpha",
        agent_config_path=second_config,
    )
    assert telegram_delivery_for_admission(source="workbench", **admission) is True


@pytest.mark.asyncio
async def test_silence_survives_a_restart(tmp_path):
    config_path = _write_instance(tmp_path)
    await _run(_runtime(tmp_path, config_path, []), ["silent", "global"])

    # A new runtime over the same instance root reads the persisted switch.
    restarted = _runtime(tmp_path, config_path, [])
    assert telegram_silent.silenced(restarted) is True
    assert instance_telegram_silenced(tmp_path) is True


@pytest.mark.asyncio
async def test_damaged_preference_document_does_not_silence_the_instance(
    tmp_path,
):
    config_path = _write_instance(tmp_path)
    sent: list[str] = []
    runtime = _runtime(tmp_path, config_path, sent)
    target = state_path(tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{not json", encoding="utf-8")

    # Reads fail open: a damaged store must not silence the instance by itself.
    assert telegram_silent.silenced(runtime) is False

    await _run(runtime, ["silent", "global"])
    assert instance_telegram_silenced(tmp_path) is False
    assert "could not be saved" in sent[-1]
    assert target.read_text(encoding="utf-8") == "{not json"


class _Logger:
    def info(self, *_args, **_kwargs) -> None:
        return None

    warning = error = debug = info


def _send_runtime(root: Path, config_path: Path, on_send=None) -> NS:
    """Runtime with a recording Telegram transport for real send-path tests."""

    sent: list[str] = []

    class _Bot:
        async def send_message(self, **kwargs):
            sent.append(str(kwargs.get("text")))
            if on_send is not None:
                on_send(len(sent))
            return NS(message_id=len(sent))

        async def send_chat_action(self, **_kwargs):
            sent.append("typing")
            return True

        async def edit_message_text(self, **_kwargs):
            sent.append("edit")
            return True

        async def delete_message(self, **_kwargs):
            sent.append("delete")
            return True

    runtime = NS(
        name="alpha",
        global_config=NS(
            bridge_home=root,
            config_path=str(config_path),
            instance_id="SILENT_TEST",
        ),
        config=NS(extra={}, active_backend="hashi-api"),
        app=NS(bot=_Bot()),
        telegram_connected=True,
        telegram_logger=_Logger(),
        error_logger=_Logger(),
        logger=_Logger(),
        workspace_dir=root,
        session_dir=root,
        _notify_enabled=False,
    )
    runtime.sent = sent
    return runtime


@pytest.mark.asyncio
async def test_silent_blocks_the_mirror_gate_before_any_route_short_circuit(
    tmp_path,
):
    config_path = _write_instance(tmp_path)
    runtime = _runtime(tmp_path, config_path, [])
    primary = {
        "type": RUN_DELIVERY_ROUTE_TYPE,
        "version": RUN_DELIVERY_ROUTE_VERSION,
        "scope": "run",
        "primary": {"surface": "telegram", "channel_key": "7"},
        "mirrors": [],
        "automatic": True,
    }
    runtime._request_meta_by_id = {
        "req-primary": {
            "owner_id": "user:123",
            "request_metadata": {RUN_DELIVERY_ROUTE_METADATA_KEY: primary},
        },
        "req-no-route": {"owner_id": "user:123", "request_metadata": {}},
    }
    # A Telegram primary and a Run admitted without a frozen route are mirror
    # decisions, not exemptions: both answer True before the cut.
    assert telegram_mirror_allowed_for_run(runtime, "req-primary") is True
    assert telegram_mirror_allowed_for_run(runtime, "req-no-route") is True

    await _run(runtime, ["silent", "global"])

    assert telegram_mirror_allowed_for_run(runtime, "req-primary") is False
    assert telegram_mirror_allowed_for_run(runtime, "req-no-route") is False


@pytest.mark.asyncio
async def test_send_text_stops_at_the_send_site(tmp_path):
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime

    config_path = _write_instance(tmp_path)
    runtime = _send_runtime(tmp_path, config_path)
    await _run(runtime, ["silent", "global"])

    # An explicit notification purpose bypasses the mirror policy, so only the
    # send-site guard can stop it.
    result = await FlexibleAgentRuntime._send_text(
        runtime, 7, "must not leave", _purpose="explicit-notification"
    )

    assert result is None
    assert runtime.sent == []


@pytest.mark.asyncio
async def test_typing_loop_makes_no_telegram_call_while_silenced(tmp_path, monkeypatch):
    import asyncio

    from orchestrator import frontend_delivery

    config_path = _write_instance(tmp_path)
    runtime = _send_runtime(tmp_path, config_path)
    await _run(runtime, ["silent", "global"])
    # The mirror policy is not a safety gate: hold it open so the loop must
    # stop on its own suppression check rather than borrowing that result.
    monkeypatch.setattr(
        frontend_delivery, "telegram_mirror_allowed_for_run", lambda *_a, **_k: True
    )

    await asyncio.wait_for(
        runtime_delivery.typing_loop(
            runtime, 7, asyncio.Event(), request_id="req-typing"
        ),
        timeout=2.0,
    )

    assert runtime.sent == []


@pytest.mark.asyncio
async def test_chunks_after_the_cut_is_armed_are_not_sent(tmp_path, monkeypatch):
    from orchestrator.connector_delivery_preferences import set_instance_preference

    config_path = _write_instance(tmp_path)

    def arm(_count: int) -> None:
        # The cut lands between the first chunk and the rest of the message.
        set_instance_preference(tmp_path, "telegram", "silent", True)

    runtime = _send_runtime(tmp_path, config_path, on_send=arm)

    async def not_blocked(*_args, **_kwargs):
        return False

    monkeypatch.setattr(
        runtime_delivery.telegram_delivery_failover, "handle_blocked_send", not_blocked
    )
    text = "第一段内容。" * 900  # 5400 characters: at least two chunks

    _duration, chunks = await runtime_delivery.send_long_message(
        runtime,
        chat_id=7,
        text=text,
        request_id="req-chunks",
        # Explicit notification bypasses the mirror policy; the per-chunk gate
        # is the only thing that can stop the rest of a started message.
        purpose="explicit-notification",
    )

    assert len(runtime.sent) == 1
    assert chunks == 1


@pytest.mark.asyncio
async def test_media_batch_is_suppressed_without_reaching_the_transport(tmp_path):
    config_path = _write_instance(tmp_path)
    runtime = _send_runtime(tmp_path, config_path)
    await _run(runtime, ["silent", "global"])
    event = {
        "content_blocks": [
            {"type": "media_ref", "attachment_id": "att-1", "role": "image"},
            {"type": "media_ref", "attachment_id": "att-2", "role": "document"},
        ]
    }
    outcome: dict[str, object] = {"status": "requested"}

    ids, count = await runtime_delivery._send_standard_event_media_to_telegram(
        runtime,
        chat_id=7,
        store=None,
        claim={"detail": {"message_id": "msg-1"}},
        owner_id="user:123",
        event=event,
        request_id="req-media",
        purpose="response",
        delivery_mode="final_delivery",
        outbox_outcome=outcome,
    )

    assert (ids, count) == ([], 0)
    assert outcome["status"] == "suppressed"
    assert runtime.sent == []


@pytest.mark.asyncio
async def test_media_stops_mid_batch_when_the_cut_is_armed(tmp_path):
    from orchestrator.connector_delivery_preferences import set_instance_preference

    config_path = _write_instance(tmp_path)
    image = tmp_path / "pic.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")

    class _Store:
        def visible_message_attachment(
            self, _session_id, *, owner_id, message_id, attachment_id
        ):
            return {
                "local_ref": str(image),
                "mime_type": "image/png",
                "role": "image",
            }

    sent: list[str] = []

    class _Bot:
        async def send_photo(self, **_kwargs):
            sent.append("photo")
            # The cut lands after the first attachment of a started batch.
            set_instance_preference(tmp_path, "telegram", "silent", True)
            return NS(message_id=len(sent))

        async def send_document(self, **_kwargs):
            sent.append("document")
            return NS(message_id=len(sent))

    runtime = NS(
        name="alpha",
        global_config=NS(bridge_home=tmp_path, config_path=str(config_path)),
        app=NS(bot=_Bot()),
        telegram_connected=True,
        telegram_logger=_Logger(),
        error_logger=_Logger(),
        logger=_Logger(),
        media_dir=tmp_path,
    )
    event = {
        "content_blocks": [
            {"type": "media_ref", "attachment_id": "a1", "role": "image"},
            {"type": "media_ref", "attachment_id": "a2", "role": "image"},
        ]
    }

    _ids, count = await runtime_delivery._send_standard_event_media_to_telegram(
        runtime,
        chat_id=7,
        store=_Store(),
        claim={"session_id": "s1", "detail": {"message_id": "m1"}},
        owner_id="user:123",
        event=event,
        request_id="req-media-batch",
        purpose="response",
        delivery_mode="final_delivery",
    )

    assert sent == ["photo"]
    assert count == 1


@pytest.mark.asyncio
async def test_media_cut_armed_during_attachment_preparation_stops_the_send(tmp_path):
    from orchestrator.connector_delivery_preferences import set_instance_preference

    config_path = _write_instance(tmp_path)
    image = tmp_path / "pic.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    sent: list[str] = []

    class _Store:
        def visible_message_attachment(
            self, _session_id, *, owner_id, message_id, attachment_id
        ):
            # The lookup is asynchronous in production: the cut lands after the
            # batch entry check and before the transport call.
            set_instance_preference(tmp_path, "telegram", "silent", True)
            return {
                "local_ref": str(image),
                "mime_type": "image/png",
                "role": "image",
            }

    class _Bot:
        async def send_photo(self, **_kwargs):
            sent.append("photo")
            return NS(message_id=1)

    runtime = NS(
        name="alpha",
        global_config=NS(bridge_home=tmp_path, config_path=str(config_path)),
        app=NS(bot=_Bot()),
        telegram_connected=True,
        telegram_logger=_Logger(),
        error_logger=_Logger(),
        logger=_Logger(),
        media_dir=tmp_path,
    )
    event = {
        "content_blocks": [{"type": "media_ref", "attachment_id": "a1", "role": "image"}]
    }
    outcome: dict[str, object] = {"status": "requested"}

    ids, count = await runtime_delivery._send_standard_event_media_to_telegram(
        runtime,
        chat_id=7,
        store=_Store(),
        claim={"session_id": "s1", "detail": {"message_id": "m1"}},
        owner_id="user:123",
        event=event,
        request_id="req-media-prep",
        # Explicit notification bypasses the mirror policy, so only the
        # pre-submit recheck can stop this attachment.
        purpose="explicit-notification",
        delivery_mode="final_delivery",
        outbox_outcome=outcome,
    )

    assert sent == []
    assert (ids, count) == ([], 0)
    assert outcome["status"] == "suppressed"


@pytest.mark.asyncio
async def test_partly_delivered_media_batch_is_never_reported_complete(
    tmp_path, monkeypatch
):
    from datetime import datetime, timezone
    from unittest.mock import AsyncMock

    import orchestrator.frontend_connector_registry as connector
    import orchestrator.frontend_dispatch as dispatch
    import orchestrator.frontend_run_questions as questions
    import orchestrator.frontend_telegram_replay as replay
    import orchestrator.telegram_actor_scope as actor_scope
    from orchestrator.connector_delivery_preferences import set_instance_preference

    config_path = _write_instance(tmp_path)
    image = tmp_path / "image.png"
    image.write_bytes(b"image")
    sent: list[str] = []
    completions: list[dict] = []
    receipts: list[dict] = []

    class _Bot:
        async def send_photo(self, **_kwargs):
            sent.append("photo")
            # The cut lands right after the first attachment of the batch.
            set_instance_preference(tmp_path, "telegram", "silent", True)
            return NS(message_id=len(sent))

    runtime = NS(
        name="alpha",
        global_config=NS(
            bridge_home=tmp_path,
            config_path=str(config_path),
            instance_id="SILENT_TEST",
        ),
        config=NS(extra={}),
        app=NS(bot=_Bot()),
        telegram_connected=True,
        logger=_Logger(),
        telegram_logger=_Logger(),
        error_logger=_Logger(),
        media_dir=tmp_path,
    )
    store = NS(
        visible_message_attachment=lambda *_a, **_k: {
            "local_ref": str(image),
            "mime_type": "image/png",
            "role": "image",
        },
        complete_delivery_outbox=lambda **entry: completions.append(entry),
    )
    event = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "content_blocks": [
            {"type": "media_ref", "attachment_id": "a1", "role": "image"},
            {"type": "media_ref", "attachment_id": "a2", "role": "image"},
        ],
    }
    monkeypatch.setattr(
        runtime_delivery.runtime_delivery_order, "wait_for_turn", AsyncMock()
    )
    monkeypatch.setattr(
        runtime_delivery.telegram_delivery_failover,
        "handle_blocked_send",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(connector, "require_connector_event", lambda *_a: None)
    monkeypatch.setattr(dispatch, "project_claimed_frontend_event", lambda *_a, **_k: event)
    monkeypatch.setattr(questions, "question_identity", lambda _event: None)
    monkeypatch.setattr(
        actor_scope, "delivery_allowed", lambda *_a, **_k: True
    )
    monkeypatch.setattr(
        replay, "telegram_replay_decision", lambda *_a, **_k: NS(state="send", is_replay=False)
    )
    monkeypatch.setattr(
        runtime_delivery, "_render_standard_event_for_telegram", lambda _event: ("", None)
    )
    monkeypatch.setattr(
        runtime_delivery, "telegram_reply_markup_for_event", lambda *_a: None
    )
    monkeypatch.setattr(
        runtime_delivery,
        "_record_standard_telegram_receipt",
        lambda *_a, **kw: receipts.append(kw),
    )
    claim = {
        "outbox_id": "ob",
        "lease_token": "lease",
        "session_id": "s",
        "event_id": "event",
        "endpoint_id": "ep",
        "detail": {"message_id": "m"},
    }

    await runtime_delivery.dispatch_claimed_telegram_event(
        runtime,
        chat_id=7,
        store=store,
        claim=claim,
        frontend_owner_id="user:123",
        request_id="req-partial",
        purpose="response",
    )

    # Only the first attachment left, and the outbox never says complete.
    assert sent == ["photo"]
    assert completions and completions[-1]["status"] == "unknown"
    assert completions[-1]["error_code"] == "telegram_silenced"
    assert receipts[-1]["status"] == "unknown"
    assert receipts[-1]["message_ids"] == ["1"]


@pytest.mark.asyncio
async def test_escalating_placeholder_loop_exits_while_silenced(tmp_path):
    import asyncio

    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime

    config_path = _write_instance(tmp_path)
    runtime = _send_runtime(tmp_path, config_path)
    await _run(runtime, ["silent", "global"])

    await asyncio.wait_for(
        FlexibleAgentRuntime._escalating_placeholder_loop(
            runtime, 7, NS(message_id=99), "req-placeholder", asyncio.Event()
        ),
        timeout=2.0,
    )

    assert runtime.sent == []


@pytest.mark.asyncio
async def test_background_status_delivery_is_suppressed(tmp_path):
    import asyncio

    from orchestrator import runtime_background_status

    config_path = _write_instance(tmp_path)
    runtime = _send_runtime(tmp_path, config_path)
    await _run(runtime, ["silent", "global"])

    generation = asyncio.create_task(asyncio.sleep(0))
    await runtime_background_status._deliver(
        runtime, NS(request_id="req-bg", chat_id=7), generation, NS(message_id=5)
    )
    await generation

    assert runtime.sent == []


@pytest.mark.asyncio
async def test_question_cards_are_not_edited_while_silenced(tmp_path):
    from orchestrator import frontend_run_questions

    config_path = _write_instance(tmp_path)
    runtime = _send_runtime(tmp_path, config_path)
    await _run(runtime, ["silent", "global"])

    # A store is never touched: the cut returns before any Telegram call.
    await frontend_run_questions.refresh_telegram_question_renditions(runtime, None)

    assert runtime.sent == []


@pytest.mark.asyncio
async def test_status_text_does_not_promise_conversations_during_silence(tmp_path):
    config_path = _write_instance(tmp_path)
    runtime = _runtime(tmp_path, config_path, [])

    await _run(runtime, ["silent", "global"])
    status = await _status(runtime)

    assert "Safety silence: on (nothing in or out)" in status
    assert "Only mirroring changes" not in status
    assert "nothing is received" in status


@pytest.mark.asyncio
async def test_silent_status_reports_state_and_scope(tmp_path):
    config_path = _write_instance(tmp_path)
    sent: list[str] = []
    runtime = _runtime(tmp_path, config_path, sent)

    await _run(runtime, ["silent", "global"])
    await _run(runtime, ["global"])

    status = sent[-1]
    assert "Safety silence: on (nothing in or out)" in status
    assert "every Telegram account" in status
    assert "Instance-wide mirror" in status
    stored = json.loads(state_path(tmp_path).read_text(encoding="utf-8"))
    assert stored["owners"]["__instance__"]["connectors"]["telegram"]["silent"] is True
# HASHI-Fingerprint: hfp_b24bfb63aa6e4792987fcf2e2d12df65
