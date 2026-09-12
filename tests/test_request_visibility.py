from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from orchestrator import runtime_session
from orchestrator import runtime_cross_session, runtime_delivery_order
from orchestrator.frontend_delivery import RUN_DELIVERY_ROUTE_METADATA_KEY
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator.session_store import SessionStore


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["protocol:message", "protocol:reply", "api", "handoff", "tui"])
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
        SimpleNamespace(replayed=False, run_id="run-visible", message_id="message-visible"),
        "user:123", "workbench", "default",
    ))
    monkeypatch.setattr(runtime_session, "resolve_request_session", lambda *args, **kwargs: (
        {"session_id": "session-visible", "context_generation": 1},
        "user:123", "workbench", "default",
    ))
    monkeypatch.setattr(runtime_session, "session_workzone_state", lambda *args, **kwargs: {})

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
        "api": "api",
        "handoff": "telegram",
        "tui": "tui",
    }[source]
    assert item.request_metadata["message_context_snapshot"]["message_source"][
        "id"
    ] == expected_source


@pytest.mark.asyncio
async def test_typed_tui_run_policy_can_disable_only_telegram_mirroring(
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
        SimpleNamespace(replayed=False, run_id="run-tui", message_id="message-tui"),
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
    assert item.deliver_to_telegram is False
    assert item.silent is False
    assert item.session_id == "session-shared"
    assert item.request_metadata["frontend_delivery_policy"]["scope"] == "run"
    context = item.request_metadata["message_context_snapshot"]
    assert context["message_source"]["id"] == "tui"
    assert context["output_destination"]["telegram_mirror"] is False
    assert context["output_destination"]["surface"] == "tui"
    assert context["output_destination"]["mirrors"] == []
    assert context["output_destination"]["automatic"] is True


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
            [],
            False,
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
