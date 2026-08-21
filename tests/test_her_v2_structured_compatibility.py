from __future__ import annotations

import pytest

from orchestrator.her_v2.interfaces import StructuredOutputError
from orchestrator.her_v2.models import (
    ExecutionDisposition,
    StageResponse,
    TriageClassification,
)
from orchestrator.her_v2.structured import (
    parse_execution,
    parse_immediate,
    parse_plan,
    parse_report,
    parse_triage,
    resolve_stage_response,
)


@pytest.mark.parametrize(
    ("response", "expected_source", "expected_classification"),
    [
        (
            StageResponse(
                text='{"classification":"SIMPLE_TASK"}',
                data={"provider_note": "formal field was incomplete"},
            ),
            "provider_text",
            TriageClassification.SIMPLE_TASK,
        ),
        (
            StageResponse(
                text="",
                reasoning_trace=(
                    "classification follows "
                    '{"classification":"COMPLEX_TASK","goal":"inspect"}'
                ),
            ),
            "reasoning_recovery",
            TriageClassification.COMPLEX_TASK,
        ),
        (
            StageResponse(
                text='{"response":{"classification":"HIGH_VOLUME_TASK"}}'
            ),
            "provider_text",
            TriageClassification.HIGH_VOLUME_TASK,
        ),
        (
            StageResponse(
                text='"{\\"classification\\":\\"DIRECT_RESPONSE\\"}"'
            ),
            "provider_text",
            TriageClassification.DIRECT_RESPONSE,
        ),
    ],
)
def test_registered_carriers_recover_one_unambiguous_triage_result(
    response,
    expected_source,
    expected_classification,
):
    resolution = resolve_stage_response(response, parse_triage)

    assert resolution.source == expected_source
    assert resolution.parsed.classification is expected_classification


def test_compatible_field_shapes_normalise_without_silent_data_damage():
    plan = parse_plan(
        StageResponse(text="", data={"steps": ["inspect", "verify"]})
    )
    execution = parse_execution(
        StageResponse(
            text="",
            data={
                "status": "partial success",
                "result": "Completed with one limitation.",
                "evidence_refs": "tool:42",
                "limitations": "Remote verification was unavailable.",
            }
        )
    )

    assert plan["plan"] == ["inspect", "verify"]
    assert execution.disposition is ExecutionDisposition.COMPLETED_WITH_LIMITATIONS
    assert execution.evidence_refs == ("tool:42",)
    assert execution.limitations == ("Remote verification was unavailable.",)


def test_plain_user_facing_report_does_not_require_an_internal_json_wrapper():
    assert parse_report(StageResponse(text="The requested work is complete.")) == (
        "The requested work is complete."
    )


@pytest.mark.parametrize(
    ("text", "expected", "source"),
    [
        (
            "A plain acknowledgement.",
            "A plain acknowledgement.",
            "provider_plain_text",
        ),
        (
            '{"message":"First line\\n\\nSecond line."}',
            "First line\n\nSecond line.",
            "provider_text",
        ),
        (
            '{"message":"First line\n\nSecond line."}',
            "First line\n\nSecond line.",
            "provider_json_control_char_repair",
        ),
        (
            '{"message":"Visible even though the envelope was truncated."',
            '{"message":"Visible even though the envelope was truncated."',
            "provider_plain_text",
        ),
    ],
)
def test_immediate_presentation_compatibility_preserves_visible_content(
    text,
    expected,
    source,
):
    resolution = resolve_stage_response(StageResponse(text=text), parse_immediate)

    assert resolution.parsed == expected
    assert resolution.source == source


def test_registered_wrapper_does_not_turn_a_nested_object_into_display_text():
    assert parse_immediate(
        StageResponse(text='{"response":{"message":"Hello."}}')
    ) == "Hello."


def test_conflicting_valid_carriers_remain_a_hard_error():
    response = StageResponse(
        data={"classification": "SIMPLE_TASK"},
        text='{"classification":"COMPLEX_TASK"}',
    )

    with pytest.raises(StructuredOutputError, match="conflicting valid"):
        parse_triage(response)


def test_non_authoritative_triage_interpretations_do_not_create_false_conflict():
    resolution = resolve_stage_response(
        StageResponse(
            data={"classification": "SIMPLE_TASK", "goal": "short wording"},
            text=(
                '{"classification":"SIMPLE_TASK",'
                '"goal":"a different but non-authoritative wording"}'
            ),
        ),
        parse_triage,
    )

    assert resolution.parsed.classification is TriageClassification.SIMPLE_TASK


def test_reasoning_is_not_used_when_a_formal_carrier_is_valid():
    response = StageResponse(
        text="",
        data={"classification": "SIMPLE_TASK"},
        reasoning_trace='{"classification":"COMPLEX_TASK"}',
    )

    resolution = resolve_stage_response(response, parse_triage)

    assert resolution.source == "provider_data"
    assert resolution.parsed.classification is TriageClassification.SIMPLE_TASK


def test_unstructured_triage_prose_is_not_guessed_into_authority():
    with pytest.raises(StructuredOutputError, match="no valid JSON"):
        parse_triage(StageResponse(text="This looks like a simple task."))
