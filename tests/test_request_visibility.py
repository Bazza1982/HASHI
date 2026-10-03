from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from orchestrator import runtime_session, runtime_transfer
from orchestrator import runtime_cross_session, runtime_delivery_order
from orchestrator.frontend_delivery import RUN_DELIVERY_ROUTE_METADATA_KEY
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator.session_store import SessionStore


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source",
    [
        "protocol:message",
        "protocol:reply",
        "hchat-reply:peer",
        "api",
        "handoff",
        "browser:headless",
        "tui",
    ],
)
async def test_legacy_hidden_request_is_admitted_as_visible(tmp_path, monkeypatch, source):
    runtime = object.__new__(FlexibleAgentRuntime)
    runtime.name = "visibility"
    runtime.global_config = SimpleNamespace(project_root=None)
    runtime.next_request_id = lambda: "req-visible"
    runtime.session_store = SimpleNamespace(session_workspace=lambda *args: tmp_path)
    runtime.message_logger = Mock()
    runtime.request_activity = Mock()
    runtime.queue = asyncio.Queue()
    monkeypatch.setattr(runtime_session, "accept_request", lambda *args, **kwargs: (
        {"session_id": "session-visible", "context_generation": 1},
        SimpleNamespace(
            replayed=False,
            run_id="run-visible",
            message_id="message-visible",
            request_id="req-visible",
        ),
        "user:123", "workbench", "default",
    ))
    monkeypatch.setattr(runtime_session, "resolve_request_session", lambda *args, **kwargs: (
        {"session_id": "session-visible", "context_generation": 1},
        "user:123", "workbench", "default",
    ))
    monkeypatch.setattr(
        runtime_session, "session_workzone_state", lambda *args, **kwargs: {}
    )

    result = await runtime.enqueue_request(
        123, "Visible work", source, "Visible work",
        deliver_to_telegram=False, silent=True,
    )

    item = runtime.queue.get_nowait()
    assert result == item.request_id == "req-visible"
    assert item.deliver_to_telegram is True
    assert item.silent is False
    assert item.chat_id == 123
    assert item.session_id == "session-visible"
    expected_source = {
        "protocol:message": "hchat",
        "protocol:reply": "hchat",
        "hchat-reply:peer": "hchat",
        "api": "api",
        "handoff": "telegram",
        "browser:headless": "telegram",
        "tui": "tui",
    }[source]
    assert item.request_metadata["message_context_snapshot"]["message_source"][
        "id"
    ] == expected_source


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["bridge-transfer:trf-red", "bridge-fork:frk-red"])
async def test_transfer_bridge_request_uses_internal_connector(
    tmp_path, monkeypatch, source
):
    runtime = object.__new__(FlexibleAgentRuntime)
    runtime.name = "target"
    runtime.global_config = SimpleNamespace(project_root=None, instance_id="HASHI3")
    runtime.next_request_id = lambda: "req-transfer"
    runtime.session_store = SimpleNamespace(session_workspace=lambda *args: tmp_path)
    runtime.message_logger = Mock()
    runtime.request_activity = Mock()
    runtime.queue = asyncio.Queue()
    runtime._transfer_state = {
        "status": "accepted",
        "transfer_id": "trf-existing-source-fence",
        "target_agent": "target",
        "target_instance": "HASHI3",
    }
    monkeypatch.setattr(
        runtime_session,
        "accept_request",
        lambda *args, **kwargs: (
            {"session_id": "session-target", "context_generation": 1},
            SimpleNamespace(
                replayed=False,
                run_id="run-transfer",
                message_id="message-transfer",
                request_id="req-transfer",
            ),
            "user:123",
            "bridge",
            "default",
        ),
    )
    monkeypatch.setattr(
        runtime_session,
        "resolve_request_session",
        lambda *args, **kwargs: (
            {"session_id": "session-target", "context_generation": 1},
            "user:123",
            "bridge",
            "default",
        ),
    )
    monkeypatch.setattr(runtime_session, "session_workzone_state", lambda *args, **kwargs: {})

    result = await runtime.enqueue_request(
        123,
        "Continue transferred work",
        source,
        "Transfer",
        deliver_to_telegram=True,
    )

    item = runtime.queue.get_nowait()
    assert result == item.request_id == "req-transfer"
    context = item.request_metadata["message_context_snapshot"]
    assert context["message_source"]["id"] == "hashi.internal"
    assert context["frontend_ingress"]["connector"]["id"] == "internal"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "transfer_state",
    [
        {
            "status": "accepted",
            "transfer_id": "trf-worker-accepted",
            "target_agent": "target",
            "target_instance": "HASHI3",
        },
        {
            "status": "pending",
            "outcome_unknown": True,
            "transfer_id": "trf-worker-unknown",
            "target_agent": "target",
            "target_instance": "HASHI3",
        },
    ],
)
async def test_worker_nonvoice_ingress_rechecks_transfer_fence_before_run(
    tmp_path, monkeypatch, transfer_state
):
    runtime = object.__new__(FlexibleAgentRuntime)
    runtime.name = "source"
    runtime.global_config = SimpleNamespace(project_root=None, instance_id="HASHI2")
    runtime.next_request_id = lambda: "req-fenced"
    runtime.session_store = SimpleNamespace(session_workspace=lambda *args: tmp_path)
    runtime.message_logger = Mock()
    runtime.request_activity = Mock()
    runtime.queue = asyncio.Queue()
    runtime._persist_transfer_state = Mock()
    runtime._transfer_state = {
        "status": "pending",
        "transfer_id": transfer_state["transfer_id"],
        "target_agent": transfer_state["target_agent"],
        "target_instance": transfer_state["target_instance"],
    }
    accept_request = Mock(side_effect=AssertionError("Run writer must not execute"))

    if transfer_state["status"] == "accepted":
        runtime_transfer.record_transfer_accepted(
            runtime,
            transfer_id=transfer_state["transfer_id"],
            target_status="accepted",
        )
    else:
        runtime_transfer.record_transfer_outcome_unknown(
            runtime,
            transfer_id=transfer_state["transfer_id"],
            error=RuntimeError("signed receiver outcome was not observed"),
        )

    monkeypatch.setattr(runtime_session, "accept_request", accept_request)
    monkeypatch.setattr(
        runtime_session,
        "resolve_request_session",
        lambda *args, **kwargs: (
            {"session_id": "session-fenced", "context_generation": 1},
            "user:123",
            "workbench",
            "default",
        ),
    )
    monkeypatch.setattr(
        runtime_session, "session_workzone_state", lambda *args, **kwargs: {}
    )

    with pytest.raises(runtime_transfer.TransferRedirectRequired) as captured:
        await runtime.enqueue_request(
            123,
            "must not create a Run",
            "session-api",
            "fenced request",
            request_metadata={"voice_origin": True},
        )

    assert captured.value.redirect["transfer_id"] == transfer_state["transfer_id"]
    accept_request.assert_not_called()
    assert runtime.queue.empty()


@pytest.mark.asyncio
async def test_worker_nonvoice_ingress_preserves_complete_pending_transfer(tmp_path, monkeypatch):
    runtime = object.__new__(FlexibleAgentRuntime)
    runtime.name = "source"
    runtime.global_config = SimpleNamespace(project_root=None, instance_id="HASHI2")
    runtime.next_request_id = lambda: "req-pending"
    runtime.session_store = SimpleNamespace(session_workspace=lambda *args: tmp_path)
    runtime.message_logger = Mock()
    runtime.request_activity = Mock()
    runtime.queue = asyncio.Queue()
    runtime._transfer_state = {
        "status": "pending",
        "transfer_id": "trf-worker-pending",
        "target_agent": "target",
        "target_instance": "HASHI3",
    }
    monkeypatch.setattr(
        runtime_session,
        "accept_request",
        lambda *args, **kwargs: (
            {"session_id": "session-pending", "context_generation": 1},
            SimpleNamespace(
                replayed=False,
                run_id="run-pending",
                message_id="message-pending",
                request_id="req-pending",
            ),
            "user:123",
            "workbench",
            "default",
        ),
    )
    monkeypatch.setattr(
        runtime_session,
        "resolve_request_session",
        lambda *args, **kwargs: (
            {"session_id": "session-pending", "context_generation": 1},
            "user:123",
            "workbench",
            "default",
        ),
    )
    monkeypatch.setattr(
        runtime_session, "session_workzone_state", lambda *args, **kwargs: {}
    )

    request_id = await runtime.enqueue_request(
        123,
        "admitted before transfer outcome",
        "session-api",
        "pending request",
    )

    assert request_id == "req-pending"
    assert runtime.queue.get_nowait().request_id == "req-pending"


@pytest.mark.asyncio
async def test_typed_tui_run_policy_cannot_disable_central_telegram_mirroring(
    tmp_path,
    monkeypatch,
):
    from orchestrator.frontend_delivery import tui_request_metadata

    runtime = object.__new__(FlexibleAgentRuntime)
    runtime.name = "visibility"
    runtime.global_config = SimpleNamespace(project_root=None)
    runtime.next_request_id = lambda: "req-tui"
    runtime.session_store = SimpleNamespace(session_workspace=lambda *args: tmp_path)
    runtime.message_logger = Mock()
    runtime.request_activity = Mock()
    runtime.queue = asyncio.Queue()
    monkeypatch.setattr(runtime_session, "accept_request", lambda *args, **kwargs: (
        {"session_id": "session-shared", "context_generation": 1},
        SimpleNamespace(
            replayed=False,
            run_id="run-tui",
            message_id="message-tui",
            request_id="req-tui",
        ),
        "user:123", "workbench", "default",
    ))
    monkeypatch.setattr(runtime_session, "resolve_request_session", lambda *args, **kwargs: (
        {"session_id": "session-shared", "context_generation": 1},
        "user:123", "workbench", "default",
    ))
    monkeypatch.setattr(runtime_session, "session_workzone_state", lambda *args, **kwargs: {})
    metadata = {
        "session_surface": "workbench",
        "session_channel_key": "default",
        **tui_request_metadata(telegram_mirror=False, client_id="tui-window-1"),
    }

    result = await runtime.enqueue_request(
        123,
        "Visible in the canonical TUI Conversation",
        "tui",
        "TUI work",
        deliver_to_telegram=True,
        silent=True,
        request_metadata=metadata,
    )

    item = runtime.queue.get_nowait()
    assert result == item.request_id == "req-tui"
    assert item.deliver_to_telegram is True
    assert item.silent is False
    assert item.session_id == "session-shared"
    assert item.request_metadata["frontend_delivery_policy"]["scope"] == "run"
    context = item.request_metadata["message_context_snapshot"]
    assert context["message_source"]["id"] == "tui"
    assert context["output_destination"]["telegram_mirror"] is True
    assert context["output_destination"]["surface"] == "tui"
    assert context["output_destination"]["mirrors"] == ["telegram"]
    assert context["output_destination"]["automatic"] is True


@pytest.mark.asyncio
async def test_validated_live_voice_origin_survives_canonical_context_rebuild(
    tmp_path,
    monkeypatch,
):
    runtime = object.__new__(FlexibleAgentRuntime)
    runtime.name = "visibility"
    runtime.global_config = SimpleNamespace(project_root=None)
    runtime.next_request_id = lambda: "req-live-origin"
    origin = {
        "call_id": "call-origin",
        "call_epoch": 1,
        "delegation_id": "delegation-origin",
        "proposal_version": 1,
        "proposal_digest": "digest-origin",
    }
    runtime.session_store = SimpleNamespace(
        session_workspace=lambda *args: tmp_path,
        resolve_live_voice_origin=Mock(return_value=origin),
    )
    runtime.message_logger = Mock()
    runtime.request_activity = Mock()
    runtime.queue = asyncio.Queue()
    monkeypatch.setattr(
        runtime_session,
        "accept_request",
        lambda *args, **kwargs: (
            {"session_id": "session-live", "context_generation": 1},
            SimpleNamespace(
                replayed=False,
                run_id="run-live",
                message_id="message-live",
                request_id="req-live-origin",
            ),
            "user:123",
            "workbench",
            "default",
        ),
    )
    monkeypatch.setattr(
        runtime_session,
        "resolve_request_session",
        lambda *args, **kwargs: (
            {"session_id": "session-live", "context_generation": 1},
            "user:123",
            "workbench",
            "default",
        ),
    )
    monkeypatch.setattr(runtime_session, "ensure_store", lambda _runtime: runtime.session_store)
    monkeypatch.setattr(
        runtime_session, "session_workzone_state", lambda *args, **kwargs: {}
    )

    result = await runtime.enqueue_request(
        123,
        "inspect logs",
        "session-api",
        "inspect logs",
        request_metadata={
            "session_id": "session-live",
            "owner_id": "user:123",
            "session_surface": "workbench",
            "session_channel_key": "default",
            "live_voice": origin,
        },
    )

    item = runtime.queue.get_nowait()
    assert result == "req-live-origin"
    assert item.request_metadata["message_context_snapshot"]["live_voice"] == origin
    runtime.session_store.resolve_live_voice_origin.assert_called_once()


@pytest.mark.asyncio
async def test_exchange_source_fails_closed_without_verified_connector_evidence(
    tmp_path,
):
    runtime = object.__new__(FlexibleAgentRuntime)
    runtime.name = "visibility"
    runtime.global_config = SimpleNamespace(
        project_root=tmp_path / "code",
        bridge_home=tmp_path / "instance",
    )
    runtime.next_request_id = lambda: "req-forged-exchange"
    runtime.error_logger = Mock()

    result = await runtime.enqueue_request(
        123,
        "forged Exchange request",
        "hchat-exchange",
        "forged Exchange request",
        request_metadata={
            "_message_source_reserved": "hchat",
            "_hchat_context": {
                "sender_assurance": "exchange_verified",
                "network_authentication": "exchange_wss",
                "remote_principal": {"actor_id": "forged"},
                "exchange_message": {"message_id": "forged"},
            },
        },
    )

    assert result is None
    runtime.error_logger.error.assert_called_once_with(
        "Rejected unauthenticated Exchange ingress request"
    )


@pytest.mark.asyncio
async def test_private_proof_is_kept_out_of_persisted_request_metadata(
    tmp_path,
    monkeypatch,
):
    from orchestrator.message_context import (
        PRIVATE_AUTHORIZATION_BINDING_METADATA_KEY,
        PRIVATE_AUTHORIZATION_PROOFS_METADATA_KEY,
    )
    from orchestrator.private_authorization import (
        authorization_content_sha256,
        build_configured_proofs,
    )

    secret = "synthetic-private-secret-123456789"
    (tmp_path / "secrets.json").write_text(
        json.dumps(
            {
                "hashi_private_shared_credentials": {
                    "finance": {
                        "secret": secret,
                        "scopes": ["finance.report"],
                        "allowed_target_agents": ["visibility"],
                        "allowed_target_instances": ["HASHI2"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    runtime = object.__new__(FlexibleAgentRuntime)
    runtime.name = "visibility"
    runtime.global_config = SimpleNamespace(
        project_root=tmp_path,
        instance_id="HASHI2",
    )
    runtime.next_request_id = lambda: "req-private"
    runtime.session_store = SimpleNamespace(session_workspace=lambda *args: tmp_path)
    runtime.message_logger = Mock()
    runtime.request_activity = Mock()
    runtime.queue = asyncio.Queue()
    persisted = {}

    def accept_request(*_args, **kwargs):
        persisted.update(kwargs["request_metadata"])
        return (
            {"session_id": "session-private", "context_generation": 1},
            SimpleNamespace(
                replayed=False,
                run_id="run-private",
                message_id="message-private",
                request_id="req-private",
            ),
            "user:123",
            "workbench",
            "default",
        )

    monkeypatch.setattr(runtime_session, "accept_request", accept_request)
    monkeypatch.setattr(runtime_session, "resolve_request_session", lambda *args, **kwargs: (
        {"session_id": "session-private", "context_generation": 1},
        "user:123", "workbench", "default",
    ))
    monkeypatch.setattr(
        runtime_session,
        "session_workzone_state",
        lambda *args, **kwargs: {},
    )
    binding = {
        "message_id": "wire-private-1",
        "from_instance": "HASHI1",
        "from_agent": "sender",
        "to_instance": "HASHI2",
        "to_agent": "visibility",
        "content_sha256": authorization_content_sha256("private request"),
        "resources": [],
    }
    proofs = build_configured_proofs(
        tmp_path,
        credential_ids=["finance"],
        binding=binding,
    )

    await runtime.enqueue_request(
        0,
        "private request",
        "hchat",
        "private request",
        request_metadata={
            PRIVATE_AUTHORIZATION_PROOFS_METADATA_KEY: proofs,
            PRIVATE_AUTHORIZATION_BINDING_METADATA_KEY: binding,
        },
    )

    item = runtime.queue.get_nowait()
    assert PRIVATE_AUTHORIZATION_PROOFS_METADATA_KEY not in persisted
    assert PRIVATE_AUTHORIZATION_BINDING_METADATA_KEY not in persisted
    assert PRIVATE_AUTHORIZATION_PROOFS_METADATA_KEY not in item.request_metadata
    assert item.private_authorization_evidence[
        PRIVATE_AUTHORIZATION_PROOFS_METADATA_KEY
    ] == proofs
    assert item.request_metadata["message_context_snapshot"][
        "private_authorization_state"
    ] == "success"
    assert secret not in repr(item)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "chat_id", "request_metadata", "source_id", "sender", "surface", "mirrors", "telegram"),
    [
        (
            "background-job-event",
            123,
            None,
            "hashi.internal",
            "system",
            "telegram",
            [],
            True,
        ),
        (
            "whatsapp",
            0,
            {
                "session_surface": "whatsapp",
                "session_channel_key": "61400000000@s.whatsapp.net",
            },
            "whatsapp",
            "human_or_client",
            "whatsapp",
            ["telegram"],
            True,
        ),
    ],
)
async def test_pao_persists_the_same_route_projected_into_pcm(
    tmp_path,
    monkeypatch,
    source,
    chat_id,
    request_metadata,
    source_id,
    sender,
    surface,
    mirrors,
    telegram,
):
    runtime = object.__new__(FlexibleAgentRuntime)
    runtime.name = "visibility"
    runtime.global_config = SimpleNamespace(
        project_root=tmp_path,
        bridge_home=tmp_path,
        instance_id="HASHI2",
        authorized_id=123,
    )
    runtime.next_request_id = lambda: f"req-{source_id}"
    runtime.session_store = SessionStore(
        tmp_path / "state" / "sessions.sqlite3", instance_id="HASHI2"
    )
    runtime.message_logger = Mock()
    runtime.request_activity = Mock()
    runtime.queue = asyncio.Queue()
    if source == "background-job-event":
        runtime._transfer_state = {
            "status": "accepted",
            "transfer_id": "trf-automation-unchanged",
            "target_agent": "target",
            "target_instance": "HASHI3",
        }
    monkeypatch.setattr(
        runtime_session, "session_workzone_state", lambda *args, **kwargs: {}
    )
    monkeypatch.setattr(
        runtime_delivery_order, "register_turn", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        runtime_cross_session, "capture_reply_target", lambda *args, **kwargs: None
    )

    await runtime.enqueue_request(
        chat_id,
        "admission route test",
        source,
        "route test",
        deliver_to_telegram=False,
        request_metadata=request_metadata,
    )

    item = runtime.queue.get_nowait()
    context = item.request_metadata["message_context_snapshot"]
    run = runtime.session_store.get_run_by_request(item.request_id)
    assert context["message_source"]["id"] == source_id
    assert context["sender"]["kind"] == sender
    assert context["output_destination"]["surface"] == surface
    assert context["output_destination"]["mirrors"] == mirrors
    assert context["output_destination"]["automatic"] is True
    assert item.deliver_to_telegram is telegram
    assert run["message_context"] == context
    assert run["delivery_route"] == item.request_metadata[
        RUN_DELIVERY_ROUTE_METADATA_KEY
    ]
    assert item.request_metadata[RUN_DELIVERY_ROUTE_METADATA_KEY]["primary"][
        "surface"
    ] == surface


@pytest.mark.asyncio
async def test_pao_uses_central_mirror_switches_for_tui_admission(tmp_path, monkeypatch):
    from orchestrator.connector_delivery_preferences import set_connector_preference
    from orchestrator.frontend_delivery import tui_request_metadata

    runtime = object.__new__(FlexibleAgentRuntime)
    runtime.name = "visibility"
    runtime.global_config = SimpleNamespace(
        project_root=tmp_path, bridge_home=tmp_path,
        instance_id="HASHI2", authorized_id=123,
        whatsapp={"allowed_numbers": ["+61400111222"]},
    )
    runtime.next_request_id = lambda: "req-central-mirrors"
    runtime.session_store = SessionStore(
        tmp_path / "state" / "sessions.sqlite3", instance_id="HASHI2"
    )
    runtime.message_logger = Mock()
    runtime.request_activity = Mock()
    runtime.queue = asyncio.Queue()
    monkeypatch.setattr(runtime_session, "session_workzone_state", lambda *args, **kwargs: {})
    monkeypatch.setattr(runtime_delivery_order, "register_turn", lambda *args, **kwargs: None)
    monkeypatch.setattr(runtime_cross_session, "capture_reply_target", lambda *args, **kwargs: None)
    set_connector_preference(tmp_path, "user:123", "telegram", "mirror", False)
    set_connector_preference(tmp_path, "user:123", "whatsapp", "mirror", True)

    await runtime.enqueue_request(
        123, "central switch", "tui", "route test", deliver_to_telegram=True,
        request_metadata={
            "session_surface": "workbench", "session_channel_key": "default",
            **tui_request_metadata(telegram_mirror=True, client_id="tui-a"),
        },
    )
    item = runtime.queue.get_nowait()
    route = item.request_metadata[RUN_DELIVERY_ROUTE_METADATA_KEY]
    assert item.deliver_to_telegram is False
    assert route["primary"]["surface"] == "tui"
    assert route["mirrors"] == [
        {"surface": "whatsapp", "channel_key": "61400111222@s.whatsapp.net"}
    ]
    assert item.request_metadata["message_context_snapshot"]["output_destination"]["mirrors"] == ["whatsapp"]

    runtime.next_request_id = lambda: "req-central-internal"
    await runtime.enqueue_request(
        123, "scheduled status", "background-job-event", "route test",
        request_metadata=None,
    )
    internal = runtime.queue.get_nowait()
    assert internal.deliver_to_telegram is False
    assert internal.request_metadata[RUN_DELIVERY_ROUTE_METADATA_KEY]["primary"] is None


@pytest.mark.asyncio
async def test_idempotent_frontend_retry_reuses_the_original_pcm_request_identity(
    tmp_path,
    monkeypatch,
):
    runtime = object.__new__(FlexibleAgentRuntime)
    runtime.name = "visibility"
    runtime.global_config = SimpleNamespace(
        project_root=tmp_path,
        bridge_home=tmp_path,
        instance_id="HASHI2",
        authorized_id=123,
    )
    runtime.next_request_id = Mock(side_effect=["req-original", "req-retry"])
    runtime.session_store = SessionStore(
        tmp_path / "state" / "sessions.sqlite3", instance_id="HASHI2"
    )
    runtime.message_logger = Mock()
    runtime.request_activity = Mock()
    runtime.queue = asyncio.Queue()
    monkeypatch.setattr(
        runtime_session, "session_workzone_state", lambda *args, **kwargs: {}
    )
    monkeypatch.setattr(
        runtime_delivery_order, "register_turn", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        runtime_cross_session, "capture_reply_target", lambda *args, **kwargs: None
    )

    first = await runtime.enqueue_request(
        0,
        "one frontend action",
        "session-api",
        "one frontend action",
        idempotency_key="stable-frontend-action",
    )
    replay = await runtime.enqueue_request(
        0,
        "one frontend action",
        "session-api",
        "one frontend action",
        idempotency_key="stable-frontend-action",
    )

    assert first == replay == "req-original"
    assert runtime.queue.qsize() == 1
