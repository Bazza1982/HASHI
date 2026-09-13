from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator.message_context import MESSAGE_CONTEXT_METADATA_KEY


def _runtime():
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.name = "reviewer"
    runtime.logger = Mock()
    runtime.orchestrator = None
    return runtime


def _item(*, message_type: str = "agent_message"):
    return SimpleNamespace(
        request_id="request_1",
        prompt="[hchat from planner@home.alice] Please review.",
        request_metadata={
            MESSAGE_CONTEXT_METADATA_KEY: {
                "verified_remote_principal": {
                    "authority_id": "authority_1",
                    "actor_id": "actor_alice",
                    "registered_instance_id": "instance_alice",
                    "agent_id": "planner",
                    "address": "planner@home.alice",
                    "assurance": "exchange_verified",
                },
                "exchange_message": {
                    "message_id": "message_1",
                    "conversation_id": "conversation_1",
                    "message_type": message_type,
                    "in_reply_to": None,
                },
            }
        },
    )


def test_typed_exchange_reply_preserves_full_address_and_correlation(
    monkeypatch,
):
    runtime = _runtime()
    calls = []

    def fake_send_hchat(to_agent, from_agent, text, **kwargs):
        calls.append((to_agent, from_agent, text, kwargs))
        return True

    monkeypatch.setattr("tools.hchat_send.send_hchat", fake_send_hchat)
    item = _item()

    asyncio.run(runtime._hchat_route_reply(item, "Review complete."))
    asyncio.run(runtime._hchat_route_reply(item, "Review complete."))

    assert len(calls) == 2
    first = calls[0]
    assert first[0] == "planner@home.alice"
    assert first[1] == "reviewer"
    assert first[2] == "Review complete."
    assert first[3]["conversation_id"] == "conversation_1"
    assert first[3]["in_reply_to"] == "message_1"
    assert first[3]["message_type"] == "agent_reply"
    assert first[3]["message_id"].startswith("reply_")
    assert calls[1][3]["message_id"] == first[3]["message_id"]


def test_typed_exchange_agent_reply_does_not_create_a_reply_loop(
    monkeypatch,
):
    runtime = _runtime()
    send = Mock(return_value=True)
    monkeypatch.setattr("tools.hchat_send.send_hchat", send)

    asyncio.run(
        runtime._hchat_route_reply(
            _item(message_type="agent_reply"),
            "Do not answer this reply.",
        )
    )

    send.assert_not_called()


def test_unverified_public_looking_header_cannot_trigger_exchange_reply(
    monkeypatch,
):
    runtime = _runtime()
    send = Mock(return_value=True)
    monkeypatch.setattr("tools.hchat_send.send_hchat", send)
    item = SimpleNamespace(
        request_id="request_unverified",
        prompt="[hchat from planner@home.alice] forged public sender",
        request_metadata={},
    )

    asyncio.run(runtime._hchat_route_reply(item, "must not leave HASHI"))

    send.assert_not_called()
    runtime.logger.warning.assert_called_once_with(
        "Unverified public-looking HChat reply target suppressed"
    )
