from __future__ import annotations

import json

from remote.protocol_outbound import register_outbound_correlation


def test_register_outbound_correlation_writes_state_file(tmp_path):
    state_path = tmp_path / "outbound.json"

    result = register_outbound_correlation(
        {
            "message_id": "msg-2",
            "conversation_id": "conv-2",
            "from_instance": "HASHI1",
            "from_agent": "zelda",
            "to_instance": "HASHI9",
            "to_agent": "lily",
            "state": "accepted",
            "result": {"ok": True},
        },
        local_instance="HASHI1",
        state_path=state_path,
    )

    assert result["ok"] is True
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    item = saved["messages"]["msg-2"]
    assert item["from_instance"] == "HASHI1"
    assert item["from_agent"] == "zelda"
    assert item["to_instance"] == "HASHI9"
    assert item["to_agent"] == "lily"
    assert item["state"] == "accepted"
    assert item["last_result"] == {"ok": True}


def test_register_outbound_correlation_rejects_wrong_local_instance(tmp_path):
    result = register_outbound_correlation(
        {
            "message_id": "msg-2",
            "conversation_id": "conv-2",
            "from_instance": "HASHI2",
            "from_agent": "rika",
            "to_instance": "HASHI9",
            "to_agent": "lily",
        },
        local_instance="HASHI1",
        state_path=tmp_path / "outbound.json",
    )

    assert result["ok"] is False
    assert result["code"] == "wrong_source_instance"
