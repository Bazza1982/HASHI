from __future__ import annotations

from remote.protocol_router import validate_protocol_envelope


def _payload(**overrides):
    payload = {
        "message_type": "agent_message",
        "message_id": "msg-1",
        "conversation_id": "conv-1",
        "from_instance": "hashi2",
        "from_agent": "rika",
        "to_instance": "hashi1",
        "to_agent": "zelda",
        "body": {"text": "hello"},
        "ttl": 99,
        "hop_count": 0,
        "route_trace": ["hashi2"],
    }
    payload.update(overrides)
    return payload


def test_validate_protocol_envelope_clamps_ttl_and_normalizes_identity():
    result = validate_protocol_envelope(_payload(), local_instance="HASHI1", max_allowed_ttl=8)

    assert result.ok is True
    assert result.payload["ttl"] == 8
    assert result.payload["from_instance"] == "HASHI2"
    assert result.payload["from_agent"] == "rika"
    assert result.payload["to_instance"] == "HASHI1"
    assert result.payload["route_trace"] == ["HASHI2"]


def test_validate_protocol_envelope_rejects_route_loop():
    result = validate_protocol_envelope(
        _payload(route_trace=["HASHI2", "HASHI1"]),
        local_instance="HASHI1",
        max_allowed_ttl=8,
    )

    assert result.ok is False
    assert result.status_code == 409
    assert result.error["body"]["code"] == "loop_detected"


def test_validate_protocol_envelope_rejects_expired_hop_count():
    result = validate_protocol_envelope(_payload(ttl=2, hop_count=2), local_instance="HASHI1", max_allowed_ttl=8)

    assert result.ok is False
    assert result.status_code == 400
    assert result.error["body"]["code"] == "delivery_expired"


def test_validate_protocol_envelope_requires_reply_correlation():
    result = validate_protocol_envelope(
        _payload(message_type="agent_reply", in_reply_to=""),
        local_instance="HASHI1",
        max_allowed_ttl=8,
    )

    assert result.ok is False
    assert result.error["body"]["code"] == "invalid_reply"
