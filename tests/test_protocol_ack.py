from __future__ import annotations

import json

from remote.protocol_ack import record_protocol_ack


def test_record_protocol_ack_writes_state_file(tmp_path):
    state_path = tmp_path / "ack.json"

    result = record_protocol_ack(
        {
            "message_id": "msg-1",
            "conversation_id": "conv-1",
            "from_instance": "HASHI2",
            "to_instance": "HASHI1",
            "state": "delivered_to_local_queue",
            "details": {"request_id": "req-1"},
        },
        local_instance="HASHI1",
        state_path=state_path,
    )

    assert result == {"ok": True, "message_id": "msg-1", "state": "delivered_to_local_queue"}
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    item = saved["messages"]["msg-1"]
    assert item["conversation_id"] == "conv-1"
    assert item["from_instance"] == "HASHI2"
    assert item["to_instance"] == "HASHI1"
    assert item["details"] == {"request_id": "req-1"}


def test_record_protocol_ack_rejects_wrong_local_instance(tmp_path):
    result = record_protocol_ack(
        {
            "message_id": "msg-1",
            "conversation_id": "conv-1",
            "from_instance": "HASHI2",
            "to_instance": "HASHI9",
        },
        local_instance="HASHI1",
        state_path=tmp_path / "ack.json",
    )

    assert result["ok"] is False
    assert result["code"] == "wrong_target_instance"
