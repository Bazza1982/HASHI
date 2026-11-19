from __future__ import annotations

import copy
import json

import pytest

from remote.exchange_protocol import (
    AUTHORIZED_ROUTES_CAPABILITY,
    CAPABILITIES,
    PRIVATE_AUTHORIZATION_CAPABILITY,
    ExchangeProtocolError,
    decode_server_frame,
    delivery_payload_digest,
    send_frame,
    routes_frame,
    utc_timestamp,
    validate_delivery_deadline,
    validate_delivery_frame,
)


def _address(*, actor: str, instance: str, agent: str, address: str):
    return {
        "authority_id": "authority_1",
        "actor_id": actor,
        "registered_instance_id": instance,
        "agent_id": agent,
        "address": address,
    }


def _delivery(now: float = 1_800_000_000.0):
    return {
        "v": 1,
        "type": "delivery",
        "delivery_id": "delivery_1",
        "recipient_epoch": "epoch_1",
        "sender": _address(
            actor="actor_alice",
            instance="instance_alice",
            agent="planner",
            address="planner@home.alice",
        ),
        "recipient": _address(
            actor="actor_barry",
            instance="instance_barry",
            agent="reviewer",
            address="reviewer@server.barry",
        ),
        "grant_revision": 7,
        "authorization_expires_at": utc_timestamp(now + 60),
        "message_id": "message_1",
        "conversation_id": "conversation_1",
        "created_at": utc_timestamp(now),
        "expires_at": utc_timestamp(now + 300),
        "message_type": "agent_message",
        "in_reply_to": None,
        "content": {
            "format": "hchat",
            "text": "Please review.",
            "authorization_message_id": None,
            "authorization_resources": [],
            "private_authorization_proofs": [],
        },
    }


def test_server_welcome_is_strictly_validated():
    frame = {
        "v": 1,
        "type": "welcome",
        "connection_id": "connection_1",
        "epoch": "epoch_1",
        "authority_id": "authority_1",
        "actor_id": "actor_example",
        "registered_instance_id": "instance_example",
        "instance_address": "node.example",
        "capabilities": list(CAPABILITIES),
        "limits": {
            "max_message_bytes": 65536,
            "max_published_agents": 100,
        },
        "heartbeat_seconds": 25,
        "lease_expires_at": "2027-01-15T08:00:00Z",
    }

    assert decode_server_frame(json.dumps(frame))["instance_address"] == (
        "node.example"
    )
    frame["authority_id"] = "bad.authority"
    with pytest.raises(ExchangeProtocolError):
        decode_server_frame(json.dumps(frame))


def test_legacy_server_without_optional_directory_remains_compatible():
    frame = {
        "v": 1,
        "type": "welcome",
        "connection_id": "connection_1",
        "epoch": "epoch_1",
        "authority_id": "authority_1",
        "actor_id": "actor_legacy",
        "registered_instance_id": "instance_legacy",
        "instance_address": "legacy.example",
        "capabilities": ["hchat_v1", "receipt_v1", "agent_reply_v1", "public_address_v1"],
        "limits": {
            "max_message_bytes": 65536,
            "max_published_agents": 100,
        },
        "heartbeat_seconds": 25,
        "lease_expires_at": "2027-01-15T08:00:00Z",
    }

    decoded = decode_server_frame(json.dumps(frame))
    assert AUTHORIZED_ROUTES_CAPABILITY not in decoded["capabilities"]
    assert "message_freshness_v1" not in decoded["capabilities"]
    assert "message_freshness_v1" in CAPABILITIES
    frame["capabilities"].remove("receipt_v1")
    with pytest.raises(ExchangeProtocolError, match="UNSUPPORTED_CAPABILITY"):
        decode_server_frame(json.dumps(frame))


def test_authorized_routes_snapshot_is_strict_and_bounded():
    frame = {
        "v": 1,
        "type": "authorized_routes",
        "request_id": "routes_1",
        "grant_revision": 11,
        "refreshed_at": "2027-01-15T08:00:00Z",
        "routes": [{
            "to": _address(
                actor="actor_alice",
                instance="instance_alice",
                agent="planner",
                address="planner@home.example",
            ),
            "message_kinds": ["agent_message", "agent_reply"],
            "available": True,
        }],
    }

    decoded = decode_server_frame(json.dumps(frame))
    assert decoded["routes"][0]["to"]["address"] == "planner@home.example"
    assert decoded["routes"][0]["available"] is True
    assert routes_frame(request_id="routes_1") == {
        "v": 1, "type": "routes", "request_id": "routes_1"
    }

    frame["routes"][0]["reason"] = "private-policy-detail"
    with pytest.raises(ExchangeProtocolError):
        decode_server_frame(json.dumps(frame))


def test_duplicate_json_key_is_rejected():
    with pytest.raises(ExchangeProtocolError):
        decode_server_frame(
            '{"v":1,"v":1,"type":"receipt","message_id":"m1",'
            '"status":"accepted"}'
        )


def test_delivery_rejects_non_negotiated_private_proof():
    delivery = _delivery()
    delivery["content"]["private_authorization_proofs"] = [
        {"credential_id": "finance"}
    ]

    with pytest.raises(
        ExchangeProtocolError, match="UNSUPPORTED_CAPABILITY"
    ):
        validate_delivery_frame(delivery)


def test_delivery_dedup_digest_ignores_retry_epoch_but_not_body():
    first = _delivery()
    retry = copy.deepcopy(first)
    retry["delivery_id"] = "delivery_2"
    retry["recipient_epoch"] = "epoch_2"
    retry["grant_revision"] = 8
    retry["authorization_expires_at"] = utc_timestamp(1_800_000_050)

    assert delivery_payload_digest(first) == delivery_payload_digest(retry)
    retry["content"]["text"] = "Changed body"
    assert delivery_payload_digest(first) != delivery_payload_digest(retry)


def test_delivery_wall_clock_expiry_is_not_remote_hop_ttl():
    delivery = _delivery()
    validate_delivery_deadline(delivery, now=1_800_000_001)
    with pytest.raises(ExchangeProtocolError, match="MESSAGE_EXPIRED"):
        validate_delivery_deadline(delivery, now=1_800_000_301)
    assert "ttl" not in delivery
    assert "hop_count" not in delivery


def test_reply_frame_preserves_conversation_and_original_request():
    frame = send_frame(
        message_id="reply_1",
        conversation_id="conversation_1",
        from_agent="reviewer",
        to=_address(
            actor="actor_alice",
            instance="instance_alice",
            agent="planner",
            address="planner@home.alice",
        ),
        created_at="2027-01-15T08:00:00Z",
        expires_at="2027-01-15T08:05:00Z",
        message_type="agent_reply",
        in_reply_to="message_1",
        text="Reviewed.",
    )

    assert frame["conversation_id"] == "conversation_1"
    assert frame["in_reply_to"] == "message_1"
    assert frame["message_type"] == "agent_reply"



def _proof(*, credential_id: str = "finance") -> dict:
    return {
        "type": "hashi.private-authorization-proof",
        "version": 1,
        "credential_id": credential_id,
        "issued_at": 1_800_000_000,
        "expires_at": 1_800_000_090,
        "nonce": "nonce_1",
        "digest": "a" * 64,
        "binding": {
            "message_id": "message_1",
            "from_instance": "instance_alice",
            "from_agent": "planner",
            "to_instance": "instance_barry",
            "to_agent": "reviewer",
            "content_sha256": "b" * 64,
            "resources": ["finance:read"],
        },
    }


def _private_delivery(proofs=None, resources=None):
    delivery = _delivery()
    delivery["content"]["authorization_resources"] = list(resources or [])
    delivery["content"]["private_authorization_proofs"] = list(proofs or [])
    return delivery


def test_delivery_accepts_private_proof_when_capability_negotiated():
    delivery = _private_delivery(
        proofs=[_proof()], resources=["finance:read"]
    )
    normalized = validate_delivery_frame(
        delivery, capabilities=(PRIVATE_AUTHORIZATION_CAPABILITY,)
    )
    assert normalized["content"]["authorization_resources"] == ["finance:read"]
    assert normalized["content"]["private_authorization_proofs"][0][
        "credential_id"
    ] == "finance"


def test_delivery_rejects_private_proof_without_capability_still():
    delivery = _private_delivery(proofs=[_proof()])
    with pytest.raises(ExchangeProtocolError, match="UNSUPPORTED_CAPABILITY"):
        validate_delivery_frame(delivery)


def test_private_proof_structural_validation_rejects_bad_digest():
    proof = _proof()
    proof["digest"] = "not-hex"
    delivery = _private_delivery(proofs=[proof])
    with pytest.raises(ExchangeProtocolError, match="INVALID_MESSAGE"):
        validate_delivery_frame(
            delivery, capabilities=(PRIVATE_AUTHORIZATION_CAPABILITY,)
        )


def test_private_proof_budget_and_resource_budget_are_enforced():
    proof = _proof()
    many = _private_delivery(
        proofs=[dict(proof) for _ in range(17)],
        resources=["finance:read"],
    )
    with pytest.raises(ExchangeProtocolError, match="PROOF_BUDGET_EXCEEDED"):
        validate_delivery_frame(
            many, capabilities=(PRIVATE_AUTHORIZATION_CAPABILITY,)
        )
    too_many_resources = _private_delivery(
        proofs=[proof],
        resources=[f"res:{i}" for i in range(65)],
    )
    with pytest.raises(ExchangeProtocolError, match="RESOURCE_BUDGET_EXCEEDED"):
        validate_delivery_frame(
            too_many_resources,
            capabilities=(PRIVATE_AUTHORIZATION_CAPABILITY,),
        )


def test_private_delivery_digest_covers_proofs_despite_capability():
    first = _private_delivery(proofs=[_proof()])
    second = _private_delivery(proofs=[_proof(credential_id="other")])
    # digest covers full content regardless of negotiated capability
    assert delivery_payload_digest(first) != delivery_payload_digest(second)


def test_capabilities_advertises_private_authorization():
    assert PRIVATE_AUTHORIZATION_CAPABILITY in CAPABILITIES
    # but a legacy peer without it is still accepted
    assert set(["hchat_v1", "receipt_v1", "agent_reply_v1", "public_address_v1"]) <= set(CAPABILITIES)
