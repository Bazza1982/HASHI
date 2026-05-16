from __future__ import annotations

import re

import pytest

from orchestrator.hchat_delivery import (
    HChatDraft,
    HChatDraftParseError,
    deliver_hchat_draft,
    draft_parse_error_text,
    hchat_delivery_log_fields,
    hchat_draft_parsed_log_fields,
    parse_hchat_draft,
    validate_hchat_target_format,
)
from tools.hchat_send import _load_json_object_with_salvage


def test_parse_hchat_draft_valid_json_object():
    draft = parse_hchat_draft(
        """
        {
          "target": "ying",
          "message": "Bridge is healthy.",
          "user_report": "I sent ying the bridge status."
        }
        """
    )

    assert draft == HChatDraft(
        target="ying",
        message="Bridge is healthy.",
        user_report="I sent ying the bridge status.",
    )


def test_parse_hchat_draft_accepts_fenced_json():
    draft = parse_hchat_draft(
        """
        ```json
        {"target": "rika@HASHI2", "message": "Please check remote routing."}
        ```
        """
    )

    assert draft.target == "rika@HASHI2"
    assert draft.message == "Please check remote routing."
    assert draft.user_report is None


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ('{"message": "hello"}', 'missing required field "target"'),
        ('{"target": "ying"}', 'missing required field "message"'),
        ("not json", "invalid JSON"),
        (
            '/home/lily/projects/hashi/.venv/bin/python3 tools/hchat_send.py --to ying --text "hello"',
            "draft looks like a shell command",
        ),
        (
            '{"target": "ying", "message": "python3 tools/hchat_send.py --to akane --text hi"}',
            "message looks like a shell command",
        ),
    ],
)
def test_parse_hchat_draft_rejects_malformed_fixtures(raw, reason):
    with pytest.raises(HChatDraftParseError) as exc_info:
        parse_hchat_draft(raw)

    assert reason in str(exc_info.value)
    assert draft_parse_error_text(exc_info.value).startswith("[hchat] Draft parse error:")
    assert draft_parse_error_text(exc_info.value).endswith("Message not sent.")


@pytest.mark.parametrize("target", ["ying", "rika@HASHI2", "@staff", "all", "hashi-agent_1@MSI-BOX"])
def test_validate_hchat_target_format_accepts_known_shapes(target):
    assert validate_hchat_target_format(target) == target


@pytest.mark.parametrize("target", ["", "two words", "agent@BAD INSTANCE", "agent;rm", "agent/other"])
def test_validate_hchat_target_format_rejects_invalid_shapes(target):
    with pytest.raises(HChatDraftParseError):
        validate_hchat_target_format(target)


def test_draft_parsed_log_fields_are_separate_from_delivery_payload():
    draft = HChatDraft(target="ying", message="hello", user_report="sent")

    assert hchat_draft_parsed_log_fields(draft) == {
        "hchat_draft_parsed": {
            "target": "ying",
            "message": "hello",
            "user_report": "sent",
        }
    }


def test_deliver_hchat_draft_delegates_routing_to_send_hchat():
    calls = []

    def fake_sender(to_agent, from_agent, text, **kwargs):
        calls.append((to_agent, from_agent, text, kwargs))
        return True

    draft = HChatDraft(target="rika@HASHI2", message="check the route", user_report="sent")

    result = deliver_hchat_draft(draft, from_agent="zelda", sender=fake_sender, attempt_id="attempt-1")

    assert calls == [("rika@HASHI2", "zelda", "check the route", {})]
    assert result.success is True
    assert result.delivery_status == "delivered"
    assert result.attempt_id == "attempt-1"
    assert result.retry_count == 0
    assert result.user_report == "sent"


def test_deliver_hchat_draft_generates_attempt_id_and_structured_failure():
    def failing_sender(to_agent, from_agent, text, **kwargs):
        raise RuntimeError("offline")

    result = deliver_hchat_draft(HChatDraft(target="ying", message="hello"), from_agent="zelda", sender=failing_sender)

    assert result.success is False
    assert result.delivery_status == "failed"
    assert result.error == "RuntimeError: offline"
    assert result.retry_count == 0
    assert re.match(r"^[0-9a-f-]{36}$", result.attempt_id)

    fields = hchat_delivery_log_fields(result)
    assert fields["hchat_target"] == "ying"
    assert fields["hchat_delivery_attempt_id"] == result.attempt_id
    assert fields["hchat_payload_final"] == "hello"
    assert fields["hchat_delivery_status"] == "failed"
    assert fields["hchat_delivery_error"] == "RuntimeError: offline"
    assert fields["hchat_retry_count"] == 0


def test_load_json_object_with_salvage_accepts_trailing_garbage(tmp_path):
    path = tmp_path / "instances.json"
    path.write_text('{"instances": {"hashi1": {"instance_id": "HASHI1"}}} trailing-bytes', encoding="utf-8")

    data = _load_json_object_with_salvage(path)

    assert data == {"instances": {"hashi1": {"instance_id": "HASHI1"}}}
