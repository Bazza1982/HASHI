"""End-to-end: an Exchange-delivered private authorization proof reuses the
existing receiver authority without a second approval step.

This binds a real HMAC proof built by the sender through the negotiated
Exchange content encoding, then reconstructs the receiver-side binding exactly
as _exchange_private_authorization_metadata does, and verifies it with the
existing verify_configured_proofs path.  It does not introduce a new
pairing or approval mechanism.
"""
from __future__ import annotations


from orchestrator.private_authorization import (
    PrivateAuthorizationStore,
    authorization_content_sha256,
    build_private_authorization_proofs,
    verify_private_authorization_proofs,
)
from remote.exchange_protocol import (
    PRIVATE_AUTHORIZATION_CAPABILITY,
    send_frame,
    validate_delivery_frame,
)


SECRET = "synthetic-finance-secret-123456789"


def _credentials():
    return {
        "finance_team": {
            "secret": SECRET,
            "group": "Finance",
            "scopes": ["finance.report"],
            "allowed_source_agents": ["planner"],
            "allowed_source_instances": ["INSTANCE_ALICE"],
            "allowed_target_agents": ["reviewer"],
            "allowed_target_instances": ["INSTANCE_BARRY"],
            "allowed_resources": ["finance:read"],
        },
    }


def _to_address():
    return {
        "authority_id": "authority_1",
        "actor_id": "actor_barry",
        "registered_instance_id": "INSTANCE_BARRY",
        "agent_id": "reviewer",
        "address": "reviewer@server.barry",
    }


def test_exchange_private_proof_verifies_under_existing_authority(tmp_path):
    text = "Please review finance."
    # 1. Sender builds a real HMAC proof bound to the exact delivery tuple.
    binding = {
        "message_id": "message_1",
        "from_instance": "INSTANCE_ALICE",
        "from_agent": "planner",
        "to_instance": "INSTANCE_BARRY",
        "to_agent": "reviewer",
        "content_sha256": authorization_content_sha256(text),
        "resources": ["finance:read"],
    }
    proofs = build_private_authorization_proofs(
        _credentials(),
        credential_ids=["finance_team"],
        binding=binding,
        now=1_800_000_000,
    )
    assert len(proofs) == 1

    # 2. Sender encodes the proof into the negotiated Exchange send frame.
    frame = send_frame(
        message_id="message_1",
        conversation_id="conversation_1",
        from_agent="planner",
        to=_to_address(),
        created_at="2027-01-15T08:00:00Z",
        expires_at="2027-01-15T08:05:00Z",
        message_type="agent_message",
        in_reply_to=None,
        text=text,
        authorization_resources=["finance:read"],
        private_authorization_proofs=proofs,
    )
    delivery = {
        "v": 1,
        "type": "delivery",
        "delivery_id": "delivery_1",
        "recipient_epoch": "epoch_1",
        "sender": {
            "authority_id": "authority_1",
            "actor_id": "actor_alice",
            "registered_instance_id": "INSTANCE_ALICE",
            "agent_id": "planner",
            "address": "planner@home.alice",
        },
        "recipient": _to_address(),
        "grant_revision": 7,
        "authorization_expires_at": "2027-01-15T08:06:00Z",
        "message_id": "message_1",
        "conversation_id": "conversation_1",
        "created_at": "2027-01-15T08:00:00Z",
        "expires_at": "2027-01-15T08:05:00Z",
        "message_type": "agent_message",
        "in_reply_to": None,
        "content": frame["content"],
    }

    # 3. Capable receiver accepts the delivery.
    normalized = validate_delivery_frame(
        delivery, capabilities=(PRIVATE_AUTHORIZATION_CAPABILITY,)
    )
    content = normalized["content"]
    assert content["authorization_resources"] == ["finance:read"]

    # 4. Receiver rebuilds the binding exactly as the exchange ingress does.
    from orchestrator.exchange_ingress import (
        _exchange_private_authorization_metadata,
    )

    meta = _exchange_private_authorization_metadata(normalized)
    receiver_binding = meta["_private_authorization_binding"]
    receiver_proofs = meta["_private_authorization_proofs"]

    # 5. The existing authority verifies the proof without a new approval.
    results = verify_private_authorization_proofs(
        _credentials(),
        proofs=receiver_proofs,
        binding=receiver_binding,
        nonce_store=PrivateAuthorizationStore(tmp_path / "nonces.sqlite3"),
        now=1_800_000_001,
    )
    assert len(results) == 1
    assert results[0]["state"] == "success"
    assert results[0]["scopes"] == ["finance.report"]
    assert results[0]["resources"] == ["finance:read"]


def test_exchange_private_proof_fails_when_content_tampered(tmp_path):
    text = "Please review finance."
    binding = {
        "message_id": "message_1",
        "from_instance": "INSTANCE_ALICE",
        "from_agent": "planner",
        "to_instance": "INSTANCE_BARRY",
        "to_agent": "reviewer",
        "content_sha256": authorization_content_sha256(text),
        "resources": ["finance:read"],
    }
    proofs = build_private_authorization_proofs(
        _credentials(),
        credential_ids=["finance_team"],
        binding=binding,
        now=1_800_000_000,
    )
    frame = send_frame(
        message_id="message_1",
        conversation_id="conversation_1",
        from_agent="planner",
        to=_to_address(),
        created_at="2027-01-15T08:00:00Z",
        expires_at="2027-01-15T08:05:00Z",
        message_type="agent_message",
        in_reply_to=None,
        text=text,
        authorization_resources=["finance:read"],
        private_authorization_proofs=proofs,
    )
    delivery = {
        "v": 1,
        "type": "delivery",
        "delivery_id": "delivery_1",
        "recipient_epoch": "epoch_1",
        "sender": {
            "authority_id": "authority_1",
            "actor_id": "actor_alice",
            "registered_instance_id": "INSTANCE_ALICE",
            "agent_id": "planner",
            "address": "planner@home.alice",
        },
        "recipient": _to_address(),
        "grant_revision": 7,
        "authorization_expires_at": "2027-01-15T08:06:00Z",
        "message_id": "message_1",
        "conversation_id": "conversation_1",
        "created_at": "2027-01-15T08:00:00Z",
        "expires_at": "2027-01-15T08:05:00Z",
        "message_type": "agent_message",
        "in_reply_to": None,
        "content": frame["content"],
    }
    # Tamper with the delivered text after signing.
    delivery["content"]["text"] = "Please review marketing instead."
    normalized = validate_delivery_frame(
        delivery, capabilities=(PRIVATE_AUTHORIZATION_CAPABILITY,)
    )
    from orchestrator.exchange_ingress import (
        _exchange_private_authorization_metadata,
    )

    meta = _exchange_private_authorization_metadata(normalized)
    results = verify_private_authorization_proofs(
        _credentials(),
        proofs=meta["_private_authorization_proofs"],
        binding=meta["_private_authorization_binding"],
        nonce_store=PrivateAuthorizationStore(tmp_path / "nonces2.sqlite3"),
        now=1_800_000_001,
    )
    assert all(item["state"] != "success" for item in results)
