from __future__ import annotations
import json
import pytest
from orchestrator.frontend_live_voice.delegation_policy import decision_shape, parse_decision
from orchestrator.frontend_live_voice.protocol import LiveVoiceError
from orchestrator.frontend_live_voice.openai_live import fit_live_session_input


def test_structural_decision_cannot_invent_a_target_or_hide_missing_question():
    with pytest.raises(LiveVoiceError):
        parse_decision({"route": "act", "complete": True, "reply": "", "actions": [
            {"kind": "cancel", "request": "Cancel it", "relation": "cancel", "target_action_id": "foreign"}]},
            known_action_ids={"owned"})
    with pytest.raises(LiveVoiceError):
        parse_decision({"route": "clarify", "complete": True, "reply": "", "actions": []}, known_action_ids=set())


def test_malformed_contract_diagnostics_are_bounded_and_never_include_prose():
    secret = "private utterance and arbitrary model output"
    raw = {"route": {secret: []}, "complete": False, "reply": secret,
           secret: secret, "actions": [{"kind": [secret], "relation": {secret: True},
           "request": secret, "target_action_id": secret}] * 10}
    shape = decision_shape(raw)
    assert shape["route"] == "invalid"
    assert shape["actions_count"] == 10
    assert len(shape["action_shapes"]) == 4
    assert shape["action_shapes"][0]["kind"] == "invalid"
    assert shape["action_shapes"][0]["relation"] == "invalid"
    assert shape["reply_characters"] == len(secret)
    assert secret not in json.dumps(shape)
    with pytest.raises(LiveVoiceError, match="live_semantic_result_invalid"):
        parse_decision({**raw, "route": "act", "actions": raw["actions"][:1]}, known_action_ids=set())


def test_progress_preference_is_typed_without_changing_action_intent():
    base = {"route": "act", "complete": True, "reply": "", "actions": [
        {"kind": "query", "request": "Check the existing news", "relation": "new", "target_action_id": None}]}
    off = parse_decision({**base, "progress_preference": "off"}, known_action_ids=set())
    assert off.progress_preference == "off"
    assert off.actions[0].request == "Check the existing news"
    assert parse_decision(base, known_action_ids=set()).progress_preference == "unchanged"
    with pytest.raises(LiveVoiceError, match="live_semantic_result_invalid"):
        parse_decision({**base, "progress_preference": ["off"]}, known_action_ids=set())


@pytest.mark.asyncio
async def test_optional_background_reference_yields_before_mandatory_context(
    monkeypatch,
):
    weights = {"MANDATORY": 7_800, "BACKGROUND": 1_000}

    async def fake_count(
        _http,
        *,
        key,
        model,
        input_messages,
    ):
        del key, model
        return sum(
            weights[part["text"]]
            for item in input_messages
            for part in item["content"]
        )

    monkeypatch.setattr(
        "orchestrator.frontend_live_voice.openai_live._provider_input_token_count",
        fake_count,
    )

    def item(text):
        return {
            "type": "message",
            "role": "developer",
            "content": [{"type": "input_text", "text": text}],
        }

    fitted, audit = await fit_live_session_input(
        object(),
        key="test-key",
        model="gpt-live-1",
        input_messages=[item("MANDATORY"), item("BACKGROUND")],
        required_message_count=1,
        history_unit_message_counts=[1],
        optional_prefix_unit_count=1,
    )

    assert [entry["content"][0]["text"] for entry in fitted] == ["MANDATORY"]
    assert audit["history_included_units"] == 0
    assert audit["history_omitted_units"] == 1
