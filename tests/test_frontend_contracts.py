from __future__ import annotations

import pytest

from orchestrator.frontend_connector_registry import (
    canonical_connector_id,
    connector_registry_snapshot,
)
from orchestrator.frontend_contracts import (
    build_frontend_ingress_envelope,
    normalize_command_invocation,
    normalize_delivery_intent,
    normalize_delivery_receipt,
    normalize_frontend_ingress_envelope,
    normalize_media_group,
    normalize_relay_envelope,
    normalize_tool_interaction,
)


def _ingress(**overrides):
    value = {
        "type": "hashi.frontend-ingress",
        "version": 2,
        "scope": "current_message",
        "instance_id": "HASHI1",
        "connector": {"id": "backend_api", "endpoint_id": "ep_123"},
        "principal": {"kind": "human_or_client", "assurance": "runtime_observed"},
        "message": {
            "request_id": "req-1",
            "idempotency_digest": "sha256:" + "a" * 64,
        },
        "target": {"session_id": "ses_1", "agent_id": "lily"},
        "network_authentication": "not_applicable",
        "relay_chain": [],
    }
    value.update(overrides)
    return value


def test_ingress_envelope_is_versioned_and_does_not_elevate_declared_identity():
    normalized = normalize_frontend_ingress_envelope(_ingress())
    assert normalized["version"] == 2
    assert normalized["principal"]["assurance"] == "runtime_observed"

    declared = _ingress()
    declared["principal"]["assurance"] = "declared"
    assert normalize_frontend_ingress_envelope(declared)["principal"]["assurance"] == "declared"


def test_ingress_rejects_unknown_versions_and_malformed_identity():
    wrong_version = _ingress()
    wrong_version["version"] = 99
    with pytest.raises(ValueError, match="version"):
        normalize_frontend_ingress_envelope(wrong_version)

    forged = _ingress()
    forged["principal"]["assurance"] = "verified"
    with pytest.raises(ValueError, match="assurance"):
        normalize_frontend_ingress_envelope(forged)


def test_unknown_ingress_fields_are_ignored_for_forward_compatibility():
    value = _ingress()
    value["future_extension"] = {"untrusted": "not promoted"}
    normalized = normalize_frontend_ingress_envelope(value)
    assert "future_extension" not in normalized


def test_server_builds_opaque_endpoint_envelope_from_resolved_source_facts():
    envelope = build_frontend_ingress_envelope(
        source_id="api",
        ingress_transport="api_chat",
        surface="workbench",
        channel_key="private-chat-id",
        instance_id="HASHI1",
        principal={"kind": "human_or_client", "assurance": "declared"},
        network_authentication="not_applicable",
        relay_chain=[],
        request_id="req-2",
        idempotency_key="client-idempotency-key",
        session_id="ses_2",
        agent_id="lily",
    )
    assert envelope["connector"]["id"] == "backend_api"
    assert envelope["connector"]["endpoint_id"].startswith("ep_")
    assert "private-chat-id" not in str(envelope)
    assert envelope["message"]["idempotency_digest"].startswith("sha256:")


def test_registry_maps_legacy_names_to_connector_neutral_ids():
    assert canonical_connector_id("api", ingress_transport="api_chat") == "backend_api"
    assert canonical_connector_id("api", ingress_transport="session-api") == "session_api"
    assert canonical_connector_id("workbench", ingress_transport="api_chat") == "backend_api"
    registry = connector_registry_snapshot()
    identifiers = {item["id"] for item in registry["connectors"]}
    assert {"telegram", "tui", "backend_api", "session_api", "hchat", "remote", "exchange"} <= identifiers
    assert all("workbench" not in item["id"] for item in registry["connectors"])


def test_delivery_intent_allows_two_endpoints_of_the_same_connector():
    value = {
        "type": "hashi.delivery-intent",
        "version": 2,
        "scope": "run",
        "event_id": "evt-1",
        "session_id": "ses_1",
        "idempotency_digest": "sha256:" + "b" * 64,
        "destinations": [
            {
                "connector_id": "telegram",
                "endpoint_id": "telegram:one",
                "channel_key": "chat-1",
                "role": "primary",
                "content_modes": ["text", "media"],
                "retry_class": "idempotent",
            },
            {
                "connector_id": "telegram",
                "endpoint_id": "telegram:two",
                "channel_key": "chat-2",
                "role": "mirror",
                "content_modes": ["text"],
                "retry_class": "idempotent",
            },
        ],
    }
    normalized = normalize_delivery_intent(value)
    assert [item["endpoint_id"] for item in normalized["destinations"]] == [
        "telegram:one",
        "telegram:two",
    ]
    assert all("channel_key" not in item for item in normalized["destinations"])
    assert "chat-1" not in str(normalized)

    value["destinations"][1]["endpoint_id"] = "telegram:one"
    with pytest.raises(ValueError, match="unique"):
        normalize_delivery_intent(value)


def test_delivery_receipt_distinguishes_proof_from_ambiguous_outcome():
    delivered = normalize_delivery_receipt(
        {
            "type": "hashi.delivery-receipt",
            "version": 1,
            "event_id": "evt-1",
            "endpoint_id": "telegram:one",
            "status": "delivered",
            "proof": {"type": "transport_message_id", "value": "42"},
        }
    )
    assert delivered["status"] == "delivered"

    with pytest.raises(ValueError, match="proof"):
        normalize_delivery_receipt(
            {
                "type": "hashi.delivery-receipt",
                "version": 1,
                "event_id": "evt-1",
                "endpoint_id": "telegram:one",
                "status": "delivered",
            }
        )
    unknown = normalize_delivery_receipt(
        {
            "type": "hashi.delivery-receipt",
            "version": 1,
            "event_id": "evt-1",
            "endpoint_id": "telegram:one",
            "status": "unknown",
        }
    )
    assert unknown["status"] == "unknown"


def test_media_group_preserves_order_and_declares_retention():
    normalized = normalize_media_group(
        {
            "type": "hashi.media-group",
            "version": 1,
            "group_id": "grp-1",
            "retention_class": "session",
            "attachments": [
                {"attachment_id": "att-1", "ordinal": 0, "sha256": "a" * 64},
                {"attachment_id": "att-2", "ordinal": 1, "sha256": "b" * 64},
            ],
        }
    )
    assert [item["attachment_id"] for item in normalized["attachments"]] == [
        "att-1",
        "att-2",
    ]
    assert normalized["retention_class"] == "session"

    invalid = {
        "type": "hashi.media-group",
        "version": 1,
        "group_id": "grp-1",
        "retention_class": "session",
        "attachments": [
            {"attachment_id": "att-1", "ordinal": 1, "sha256": "a" * 64}
        ],
    }
    with pytest.raises(ValueError, match="ordinal"):
        normalize_media_group(invalid)


def test_commands_are_typed_idempotent_and_authorized_at_admission():
    normalized = normalize_command_invocation(
        {
            "type": "hashi.frontend-command",
            "version": 2,
            "invocation_id": "cmd-1",
            "request_id": "req-cmd-1",
            "connector_id": "telegram",
            "endpoint_id": "telegram:one",
            "session_id": "ses_1",
            "context_generation": 3,
            "command": "telegram",
            "issued_action_id": None,
            "revision": None,
            "arguments": ["off"],
            "actor_digest": "sha256:" + "d" * 64,
            "idempotency_digest": "sha256:" + "c" * 64,
            "authorization": {"decision": "allowed", "scope": "owner"},
        }
    )
    assert normalized["authorization"]["decision"] == "allowed"
    normalized_action = normalize_command_invocation(
        {
            **normalized,
            "issued_action_id": "_LyM8CVBdVJyvbz-OJAp1Jrb",
            "revision": 2,
        }
    )
    assert normalized_action["issued_action_id"] == "_LyM8CVBdVJyvbz-OJAp1Jrb"

    denied = {
        "type": "hashi.frontend-command",
        "version": 2,
        "invocation_id": "cmd-2",
        "request_id": "req-cmd-2",
        "connector_id": "telegram",
        "endpoint_id": "telegram:one",
        "session_id": "ses_1",
        "context_generation": 3,
        "command": "telegram",
        "issued_action_id": "action-1",
        "revision": 2,
        "arguments": ["off"],
        "actor_digest": "sha256:" + "d" * 64,
        "idempotency_digest": "sha256:" + "c" * 64,
        "authorization": {"decision": "denied", "scope": "owner"},
    }
    assert normalize_command_invocation(denied)["authorization"]["decision"] == "denied"


def test_relay_envelope_bounds_hops_and_preserves_typed_payload_reference():
    envelope = normalize_relay_envelope(
        {
            "type": "hashi.frontend-relay",
            "version": 1,
            "correlation_id": "corr-1",
            "origin_instance": "HASHI2",
            "target_instance": "HASHI1",
            "relay_chain": ["HASHI2"],
            "hop_limit": 4,
            "payload_ref": {"type": "hashi.frontend-ingress", "id": "msg-1"},
        }
    )
    assert envelope["payload_ref"]["type"] == "hashi.frontend-ingress"
    with pytest.raises(ValueError, match="hop"):
        normalize_relay_envelope({**envelope, "hop_limit": 99})


def test_browser_and_computer_tools_share_auditable_interaction_envelope():
    envelope = normalize_tool_interaction(
        {
            "type": "hashi.frontend-tool-interaction",
            "version": 1,
            "invocation_id": "tool-1",
            "request_id": "req-1",
            "session_id": "ses_1",
            "tool_class": "computer",
            "tool_name": "browser_click",
            "state": "completed",
            "authorization_scope": ["current_run"],
            "side_effects_possible": True,
            "artifact_refs": ["att_1"],
        }
    )
    assert envelope["side_effects_possible"] is True
    assert envelope["authorization_scope"] == ["current_run"]


def test_frontend_event_envelope_validates_durable_and_ephemeral_lanes():
    from orchestrator.frontend_contracts import normalize_frontend_event

    durable = normalize_frontend_event(
        {
            "type": "hashi.frontend-event",
            "version": 2,
            "event_id": "evt-10",
            "message_id": "msg-10",
            "session_id": "ses-1",
            "sequence": 5,
            "run_id": "run-1",
            "request_id": "req-1",
            "durability": "durable",
            "audience": "user",
            "visibility": "public",
            "semantic_kind": "final",
            "presentation_channel": "final",
            "content_blocks": [
                {"type": "text", "text": "Hello world", "format": "markdown"},
                {"type": "hint", "level": "info", "text": "Hint text"},
            ],
            "created_at": "2026-09-25T20:00:00Z",
        }
    )
    assert durable["durability"] == "durable"
    assert durable["message_id"] == "msg-10"
    assert durable["sequence"] == 5
    assert len(durable["content_blocks"]) == 2

    # Durable requires non-negative sequence
    with pytest.raises(ValueError, match="sequence"):
        normalize_frontend_event(
            {
                "type": "hashi.frontend-event",
                "version": 2,
                "event_id": "evt-11",
                "session_id": "ses-1",
                "sequence": None,
                "durability": "durable",
                "created_at": "2026-09-25T20:00:00Z",
            }
        )

    # Ephemeral allows sequence None but validates epoch
    ephemeral = normalize_frontend_event(
        {
            "type": "hashi.frontend-event",
            "version": 2,
            "event_id": "evt-12",
            "session_id": "ses-1",
            "sequence": None,
            "durability": "ephemeral",
            "epoch": 1,
            "ephemeral_sequence": 10,
            "semantic_kind": "commentary",
            "presentation_channel": "commentary",
            "content_blocks": [{"type": "text", "text": "Thinking..."}],
            "created_at": "2026-09-25T20:00:01Z",
        }
    )
    assert ephemeral["durability"] == "ephemeral"
    assert ephemeral["epoch"] == 1
    assert ephemeral["ephemeral_sequence"] == 10


def test_frontend_event_exposes_only_public_typed_error_codes():
    from orchestrator.frontend_contracts import normalize_frontend_event

    base = {
        "type": "hashi.frontend-event",
        "version": 2,
        "event_id": "evt-error",
        "session_id": "ses-1",
        "sequence": 6,
        "run_id": "run-1",
        "request_id": "req-1",
        "durability": "durable",
        "audience": "user",
        "visibility": "public",
        "interface_kind": "display",
        "semantic_kind": "error",
        "presentation_channel": "error",
        "content_blocks": [{"type": "text", "text": "Request failed"}],
        "created_at": "2026-10-03T03:56:08Z",
    }
    public = normalize_frontend_event(
        {**base, "error_code": "PROVIDER_AUTHENTICATION_FAILED"}
    )
    assert public["error_code"] == "PROVIDER_AUTHENTICATION_FAILED"

    unlisted = normalize_frontend_event(
        {**base, "event_id": "evt-private", "error_code": "RAW_PROVIDER_FAILURE"}
    )
    assert unlisted['error_code'] == 'RAW_PROVIDER_FAILURE'
    malformed = normalize_frontend_event({**base, 'error_code':'token=secret-canary'})
    assert 'error_code' not in malformed


def test_frontend_error_preserves_owner_recovery_decision_and_connection_code():
    import json
    from orchestrator.frontend_contracts import normalize_frontend_event
    failure = {'type':'hashi.public-failure', 'version':1, 'backend':'her-v3',
               'error_code':'PROVIDER_CONNECTION_FAILED', 'retry_action':'verify_results',
               'error_retryable':False, 'side_effects_possible':True,
               'text':'已保存完成的操作，请先核实成果。',
               'effects':{'confirmed_write_count':9, 'no_change_count':2, 'unverified_action_count':23}}
    event = normalize_frontend_event({'type':'hashi.frontend-event','version':2, 'event_id':'evt-connection',
        'session_id':'ses-1','run_id':'run-1','request_id':'req-1','sequence':6,'durability':'durable',
        'audience':'user','visibility':'public','interface_kind':'display','semantic_kind':'error',
        'presentation_channel':'error','error_code':'PROVIDER_CONNECTION_FAILED',
        'content_blocks':[{'type':'text','text':failure['text']}], 'public_failure':{**failure, 'api_key':'secret-canary'},
        'created_at':'2026-10-06T01:00:00Z'})
    assert event['error_code'] == 'PROVIDER_CONNECTION_FAILED'
    assert event['public_failure'] == failure
    assert 'secret-canary' not in json.dumps(event)


def test_content_component_normalization_supports_standard_components():
    from orchestrator.frontend_contracts import normalize_content_blocks

    blocks = [
        {"type": "text", "text": "Report", "format": "markdown"},
        {"type": "key_value", "items": [{"key": "status", "value": "healthy"}]},
        {"type": "table", "headers": ["A", "B"], "rows": [["1", "2"]]},
        {"type": "action", "action_id": "act-1", "label": "Approve", "style": "primary"},
        {"type": "hint", "level": "warning", "text": "Check credentials"},
        {"type": "media_ref", "group_id": "grp-1", "attachment_id": "att-1", "role": "voice_message"},
    ]
    normalized = normalize_content_blocks(blocks)
    assert len(normalized) == 6
    assert normalized[0]["type"] == "text"
    assert normalized[1]["items"][0]["key"] == "status"
    assert normalized[2]["headers"] == ["A", "B"]
    assert normalized[3]["style"] == "primary"
    assert normalized[4]["level"] == "warning"
    assert normalized[5]["role"] == "voice_message"


def test_standard_content_capacity_covers_large_connector_button_displays():
    from orchestrator.frontend_contracts import normalize_content_blocks

    blocks = [{"type": "text", "text": "Choose"}]
    blocks.extend(
        {
            "type": "action",
            "action_id": f"choice_{index}",
            "label": f"Choice {index}",
        }
        for index in range(100)
    )
    assert len(normalize_content_blocks(blocks)) == 101
    with pytest.raises(ValueError, match="maximum limit of 128"):
        normalize_content_blocks(blocks + blocks[:28])


def test_admission_receipt_normalization():
    from orchestrator.frontend_contracts import normalize_admission_receipt

    accepted = normalize_admission_receipt(
        {
            "type": "hashi.admission-receipt",
            "version": 1,
            "status": "accepted",
            "session_id": "ses-1",
            "run_id": "run-1",
            "request_id": "req-1",
            "idempotency_digest": "sha256:" + "e" * 64,
            "replayed": False,
        }
    )
    assert accepted["status"] == "accepted"
    assert accepted["replayed"] is False

    conflict = normalize_admission_receipt(
        {
            "type": "hashi.admission-receipt",
            "version": 1,
            "status": "conflict",
            "session_id": "ses-1",
            "run_id": None,
            "request_id": "req-1",
            "idempotency_digest": "sha256:" + "e" * 64,
            "replayed": False,
            "reason": "digest mismatch",
        }
    )
    assert conflict["status"] == "conflict"
    assert conflict["reason"] == "digest mismatch"


@pytest.mark.asyncio
async def test_t01_t12_dynamic_third_connector_registration_and_reference_adapter():
    from orchestrator.frontend_connector_registry import (
        ReferenceConnectorAdapter,
        canonical_connector_id,
        connector_registry_snapshot,
        get_connector_capabilities,
        register_connector,
        unregister_connector,
    )

    custom_id = "connector_three"
    register_connector(
        {
            "id": custom_id,
            "class": "custom_messaging",
            "ingress": ["message", "command"],
            "egress": ["text", "card"],
            "canonical_feed": "persistent_session_events",
            "runtime": {
                "endpoint_id": "connector_three:primary",
                "ready": True,
                "health": "online",
                "generation": "fc-test-generation-1",
            },
        }
    )
    try:
        # T01: Capabilities query and canonical id mapping
        assert canonical_connector_id(custom_id) == custom_id
        caps = get_connector_capabilities(custom_id)
        assert caps["connector_id"] == custom_id
        assert caps["protocol_version"] == 3
        assert "message" in caps["ingress"]
        assert caps["endpoint_registered"] is True
        assert caps["ready"] is True
        assert caps["health"] == "online"
        assert caps["generation"] == "fc-test-generation-1"

        unknown_endpoint = get_connector_capabilities(
            custom_id, endpoint_id="connector_three:unknown"
        )
        assert unknown_endpoint["endpoint_registered"] is False
        assert unknown_endpoint["ready"] is False
        assert unknown_endpoint["health"] == "unobserved"

        declared_only = get_connector_capabilities("telegram")
        assert declared_only["endpoint_registered"] is False
        assert declared_only["ready"] is False
        assert declared_only["health"] == "unobserved"

        with pytest.raises(ValueError, match="built-in connector"):
            register_connector(
                {
                    "id": "telegram",
                    "class": "custom_messaging",
                    "ingress": ["message"],
                    "egress": ["text"],
                }
            )

        snapshot = connector_registry_snapshot()
        ids = {c["id"] for c in snapshot["connectors"]}
        assert custom_id in ids
        assert "reference" in ids

        # T12: Reference adapter rendering and dispatch simulation
        adapter = ReferenceConnectorAdapter(connector_id=custom_id)
        test_event = {
            "type": "hashi.frontend-event",
            "version": 2,
            "event_id": "evt-test-1",
            "session_id": "ses-1",
            "sequence": 1,
            "durability": "durable",
            "content_blocks": [
                {"type": "text", "text": "Hello 3rd party"},
                {"type": "key_value", "items": [{"key": "env", "value": "prod"}]},
            ],
            "created_at": "2026-09-25T20:00:00Z",
        }
        rendered = adapter.render(test_event)
        assert "Hello 3rd party" in rendered
        assert "env: prod" in rendered

        # Simulate delivered
        receipt = await adapter.dispatch(test_event)
        assert receipt["status"] == "delivered"
        assert receipt["proof"]["type"] == "reference_ack"

        # Simulate timeout -> unknown
        adapter.simulate_mode = "timeout"
        receipt_timeout = await adapter.dispatch(test_event)
        assert receipt_timeout["status"] == "unknown"

        # Simulate rejected -> failed
        adapter.simulate_mode = "rejected"
        receipt_rejected = await adapter.dispatch(test_event)
        assert receipt_rejected["status"] == "failed"

        # Simulate disconnected -> ConnectionError
        adapter.simulate_mode = "disconnected"
        with pytest.raises(ConnectionError):
            await adapter.dispatch(test_event)
    finally:
        unregister_connector(custom_id)
        with pytest.raises(ValueError, match="unregistered frontend connector"):
            canonical_connector_id(custom_id)
