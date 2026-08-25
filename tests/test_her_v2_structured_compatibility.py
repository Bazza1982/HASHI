from __future__ import annotations

import pytest

from orchestrator.her_v2.interfaces import StructuredOutputError
from orchestrator.her_v2.models import (
    ExecutionDisposition,
    ReviewOutcome,
    StageResponse,
    TriageClassification,
)
from orchestrator.her_v2.structured import (
    parse_direct_message,
    parse_execution,
    parse_finalisation,
    parse_immediate,
    parse_plan,
    parse_triage,
    resolve_stage_response,
    validate_review_response,
)


def test_direct_preserves_json_looking_user_output_as_natural_language():
    text = '{"report":{"status":"complete"}}'

    assert parse_direct_message(StageResponse(text=text)) == text
    assert parse_direct_message(
        StageResponse(data={"message": "Please provide the account ID."})
    ) == "Please provide the account ID."


@pytest.mark.parametrize(
    ("response", "expected_source", "expected_classification"),
    [
        (
            StageResponse(
                text=(
                    '{"classification":"SIMPLE_TASK","real_goal":"inspect",'
                    '"relevant_habits":[],"clarification":null}'
                ),
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
                    '{"classification":"COMPLEX_TASK","real_goal":"inspect",'
                    '"relevant_habits":[],"clarification":null}'
                ),
            ),
            "reasoning_recovery",
            TriageClassification.COMPLEX_TASK,
        ),
        (
            StageResponse(
                text=(
                    '{"response":{"classification":"HIGH_VOLUME_TASK",'
                    '"real_goal":"process all items","relevant_habits":[],'
                    '"clarification":null}}'
                )
            ),
            "provider_text",
            TriageClassification.HIGH_VOLUME_TASK,
        ),
        (
            StageResponse(
                text=(
                    '"{\\"classification\\":\\"DIRECT_RESPONSE\\",'
                    '\\"real_goal\\":\\"answer\\",'
                    '\\"relevant_habits\\":[],\\"clarification\\":null}"'
                )
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


def test_triage_schema_v2_rejects_the_retired_goal_only_shape():
    with pytest.raises(StructuredOutputError, match="requires real_goal"):
        parse_triage(
            StageResponse(
                data={
                    "classification": "SIMPLE_TASK",
                    "goal": "Inspect the target",
                }
            )
        )


def test_triage_schema_v2_preserves_resolved_goal_and_selected_habits():
    decision = parse_triage(
        StageResponse(
            data={
                "classification": "COMPLEX_TASK",
                "real_goal": "Inspect and repair the target",
                "relevant_habits": ["[inspect-first] Inspect current state."],
                "clarification": None,
            }
        )
    )

    assert decision.real_goal == "Inspect and repair the target"
    assert decision.relevant_habits == (
        "[inspect-first] Inspect current state.",
    )


def test_compatible_field_shapes_normalise_without_silent_data_damage():
    plan = parse_plan(
        StageResponse(
            text="",
            data={
                "steps": ["inspect", "verify"],
                "success_criteria": ["Inspection and verification complete"],
            },
        )
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


def test_planning_requires_steps_and_success_criteria() -> None:
    with pytest.raises(StructuredOutputError, match="success_criteria"):
        parse_plan(StageResponse(data={"plan": ["inspect"]}))

    with pytest.raises(StructuredOutputError, match="non-empty list"):
        parse_plan(
            StageResponse(
                data={"plan": [""], "success_criteria": ["Inspection complete"]}
            )
        )


def test_plan_b_finalisation_parses_canonical_result_and_message():
    result = parse_finalisation(
        StageResponse(
            data={
                "execution_result": {
                    "disposition": "COMPLETED",
                    "summary": "Completed and verified.",
                    "work_performed": ["Updated the target."],
                    "verification": ["The focused test passed."],
                    "evidence_refs": ["receipt:42"],
                    "remaining_work": [],
                },
                "final_message": "Captain, the work is complete.",
            }
        )
    )

    assert result.execution_result is not None
    assert result.execution_result.disposition is ExecutionDisposition.COMPLETED
    assert result.execution_result.work_performed == ("Updated the target.",)
    assert result.execution_result.verification == ("The focused test passed.",)
    assert result.execution_result.evidence_refs == ("receipt:42",)
    assert result.final_message == "Captain, the work is complete."
    assert result.execution_result_present is True


def test_finalisation_accepts_natural_language_without_json_envelope():
    result = parse_finalisation(
        StageResponse(text="Captain, the final result is ready.")
    )

    assert result.execution_result is None
    assert result.execution_result_present is False
    assert result.final_message == "Captain, the final result is ready."


def test_plan_b_finalisation_accepts_null_only_as_runtime_error_input():
    result = parse_finalisation(
        StageResponse(
            data={
                "execution_result": None,
                "final_message": "The Execution output was unusable.",
            }
        )
    )

    assert result.execution_result is None
    assert result.execution_result_present is True


@pytest.mark.parametrize("disposition", ["ERROR", "REPLAN_REQUIRED", "ABANDONED"])
def test_execution_rejects_non_execution_dispositions(disposition):
    with pytest.raises(StructuredOutputError):
        parse_execution(
            StageResponse(
                data={
                    "disposition": disposition,
                    "summary": "This disposition is not available to Execution.",
                }
            )
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
        data={
            "classification": "SIMPLE_TASK",
            "real_goal": "inspect one item",
            "relevant_habits": [],
            "clarification": None,
        },
        text=(
            '{"classification":"COMPLEX_TASK","real_goal":"inspect all items",'
            '"relevant_habits":[],"clarification":null}'
        ),
    )

    with pytest.raises(StructuredOutputError, match="conflicting valid"):
        parse_triage(response)


def test_conflicting_triage_real_goals_remain_a_hard_error():
    with pytest.raises(StructuredOutputError, match="conflicting valid"):
        resolve_stage_response(
            StageResponse(
                data={
                    "classification": "SIMPLE_TASK",
                    "real_goal": "short wording",
                    "relevant_habits": [],
                    "clarification": None,
                },
                text=(
                    '{"classification":"SIMPLE_TASK",'
                    '"real_goal":"a different authoritative wording",'
                    '"relevant_habits":[],"clarification":null}'
                ),
            ),
            parse_triage,
        )


def test_reasoning_is_not_used_when_a_formal_carrier_is_valid():
    response = StageResponse(
        text="",
        data={
            "classification": "SIMPLE_TASK",
            "real_goal": "inspect",
            "relevant_habits": [],
            "clarification": None,
        },
        reasoning_trace=(
            '{"classification":"COMPLEX_TASK","real_goal":"other",'
            '"relevant_habits":[],"clarification":null}'
        ),
    )

    resolution = resolve_stage_response(response, parse_triage)

    assert resolution.source == "provider_data"
    assert resolution.parsed.classification is TriageClassification.SIMPLE_TASK


def test_unstructured_triage_prose_is_not_guessed_into_authority():
    with pytest.raises(StructuredOutputError, match="no valid JSON"):
        parse_triage(StageResponse(text="This looks like a simple task."))


def test_review_pass_uses_the_new_three_field_contract_without_mandatory_tools():
    finding = validate_review_response(
        StageResponse(
            data={
                "status": "PASS",
                "reason": "The completed result satisfies the request.",
                "conditions": None,
            },
        )
    )

    assert finding.outcome is ReviewOutcome.PASS
    assert finding.summary == "The completed result satisfies the request."
    assert finding.findings == ()


@pytest.mark.parametrize(
    ("data", "error"),
    [
        (
            {
                "status": "CONDITIONAL_PASS",
                "reason": "The subjective result cannot be objectively proven.",
                "conditions": None,
            },
            "requires non-empty conditions",
        ),
        (
            {
                "status": "PASS",
                "reason": "The result passes.",
                "conditions": "Unexpected limitation.",
            },
            "requires conditions to be null",
        ),
        (
            {
                "status": "FAIL",
                "reason": "The result fails.",
                "conditions": None,
                "extra": "not allowed",
            },
            "unexpected fields",
        ),
        (
            {
                "status": "INCONCLUSIVE",
                "reason": "Not part of the model contract.",
                "conditions": None,
            },
            "must be PASS, CONDITIONAL_PASS, or FAIL",
        ),
    ],
)
def test_review_rejects_invalid_three_field_decisions(data, error):
    with pytest.raises(StructuredOutputError, match=error):
        validate_review_response(StageResponse(data=data))


def test_review_conditional_pass_preserves_conditions_for_finalisation():
    finding = validate_review_response(
        StageResponse(
            data={
                "status": "CONDITIONAL_PASS",
                "reason": "The result substantially satisfies the request.",
                "conditions": "Visual quality remains subjective.",
            }
        )
    )

    assert finding.outcome is ReviewOutcome.CONDITIONAL_PASS
    assert finding.findings == ("Visual quality remains subjective.",)


def test_review_technical_unavailability_is_not_a_conditional_pass():
    finding = validate_review_response(
        StageResponse(
            data={
                "outcome": "UNAVAILABLE",
                "summary": "The isolated inspector could not start.",
            }
        )
    )

    assert finding.outcome is ReviewOutcome.UNAVAILABLE
