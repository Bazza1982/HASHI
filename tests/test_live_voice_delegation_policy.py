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


def test_existing_result_recall_is_scoped_and_never_creates_a_run():
    request = {"route": "recall", "complete": True, "reply_needed": False,
               "reply": "", "actions": [], "result_ids": ["msg-news"]}
    decision = parse_decision(request, known_action_ids=set(),
                              known_result_ids={"msg-news"})
    assert decision.result_ids == ("msg-news",)
    assert decision.actions == ()
    with pytest.raises(LiveVoiceError, match="live_semantic_target_invalid"):
        parse_decision({**request, "result_ids": ["msg-foreign"]},
                       known_action_ids=set(), known_result_ids={"msg-news"})
    continued = parse_decision({**request, "result_continuation": True},
                               known_action_ids=set(), known_result_ids={"msg-news"},
                               known_result_next_offsets={"msg-news": 12000})
    assert continued.result_continuation is True
    with pytest.raises(LiveVoiceError, match="live_semantic_target_invalid"):
        parse_decision({**request, "result_continuation": True},
                       known_action_ids=set(), known_result_ids={"msg-news"})


@pytest.mark.asyncio
async def test_completed_result_index_survives_exact_provider_fitting(
    monkeypatch,
):
    weights = {"MANDATORY": 5_000, "BACKGROUND": 1_000, "OLD": 2_500,
               "LATEST": 1_000}

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
        input_messages=[item("MANDATORY"), item("BACKGROUND"), item("OLD"), item("LATEST")],
        required_message_count=2,
        history_unit_message_counts=[1, 1],
    )

    assert [entry["content"][0]["text"] for entry in fitted] == ["MANDATORY", "BACKGROUND", "LATEST"]
    assert audit["history_included_units"] == 1
    assert audit["history_omitted_units"] == 1
