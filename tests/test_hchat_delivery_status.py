from __future__ import annotations

import pytest

from orchestrator.hchat_delivery_status import (
    default_hchat_conversation_id,
    decode_delivery_status,
    delivery_status_display_text,
    delivery_status_matches_outbound_correlation,
    delivery_status_message_id,
    encode_delivery_status,
)


def test_default_conversation_id_is_stable_per_logical_message():
    assert default_hchat_conversation_id("message-1") == (
        default_hchat_conversation_id("message-1")
    )
    assert default_hchat_conversation_id("message-1") != (
        default_hchat_conversation_id("message-2")
    )


def test_delivery_status_must_reverse_the_exact_original_route():
    correlation = {
        "conversation_id": "conversation-1",
        "from_instance": "HASHI3",
        "from_agent": "zelda",
        "to_instance": "HASHI2",
        "to_agent": "rain",
    }

    assert delivery_status_matches_outbound_correlation(
        correlation,
        conversation_id="conversation-1",
        from_instance="HASHI2",
        from_agent="rain",
        to_instance="HASHI3",
        to_agent="zelda",
    )
    assert not delivery_status_matches_outbound_correlation(
        correlation,
        conversation_id="conversation-1",
        from_instance="HASHI2",
        from_agent="mallory",
        to_instance="HASHI3",
        to_agent="zelda",
    )


def test_delivery_status_envelope_is_stable_and_content_free():
    encoded = encode_delivery_status(
        original_message_id="msg-original-1",
        state="confirmation_expired",
        reason="confirmation_timeout_15m",
    )

    assert "private command body" not in encoded
    assert decode_delivery_status(encoded) == {
        "original_message_id": "msg-original-1",
        "state": "confirmation_expired",
        "reason": "confirmation_timeout_15m",
    }
    assert delivery_status_message_id(
        "msg-original-1", "confirmation_expired"
    ) == delivery_status_message_id(
        "msg-original-1", "confirmation_expired"
    )


def test_delivery_status_reserved_prefix_fails_closed_when_malformed():
    with pytest.raises(ValueError, match="invalid HChat delivery status"):
        decode_delivery_status("[hashi hchat delivery status v1] not-json")


def test_delivery_status_display_is_truthful_about_non_execution():
    text = delivery_status_display_text(
        {
            "original_message_id": "msg-original-2",
            "state": "discarded",
        }
    )

    assert "discarded" in text
    assert "not executed" in text
    assert "msg-original-2" in text
