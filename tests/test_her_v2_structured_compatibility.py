from __future__ import annotations

import pytest

from orchestrator.her_v2.interfaces import StructuredOutputError
from orchestrator.her_v2.models import (
    ExecutionDisposition,
    ReviewOutcome,
    Stage,
    StageResponse,
    ToolEvidenceReceipt,
    ToolReceiptStatus,
    TriageClassification,
    VerificationOutcome,
)
from orchestrator.her_v2.structured import (
    parse_execution,
    parse_finalisation,
    parse_immediate,
    parse_plan,
    parse_triage,
    resolve_stage_response,
    validate_review_response,
    validate_verification_response,
)


def _receipt(
    evidence_ref,
    *,
    stage,
    invocation="invocation-1",
    attempt=1,
    tool_name="workspace_inspect",
    status=ToolReceiptStatus.SUCCESS,
    completed=True,
    details=None,
):
    return ToolEvidenceReceipt(
        evidence_ref=evidence_ref,
        stage=stage,
        invocation_id=invocation,
        attempt=attempt,
        tool_call_id=evidence_ref,
        tool_name=tool_name,
        status=status,
        read_only=True,
        completed=completed,
        output_sha256=f"sha256-{evidence_ref}",
        details=details or {},
    )


def _snapshot(ref, *, stage, digest="stable", **kwargs):
    return _receipt(
        ref,
        stage=stage,
        details={"operation": "snapshot", "snapshot_sha256": digest},
        **kwargs,
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


def test_review_pass_requires_exact_completed_current_tool_receipts():
    receipts = (
        _snapshot("before", stage=Stage.REVIEW),
        _receipt(
            "inspection",
            stage=Stage.REVIEW,
            details={"operation": "diff", "exit_code": 0},
        ),
        _snapshot("after", stage=Stage.REVIEW),
    )
    finding = validate_review_response(
        StageResponse(
            data={
                "outcome": "PASS",
                "summary": "The current diff satisfies the plan.",
                "evidence_refs": ["inspection"],
            },
            provider_attempt=1,
            tool_receipts=receipts,
        )
    )

    assert finding.outcome is ReviewOutcome.PASS
    assert finding.evidence_refs == ("inspection",)


@pytest.mark.parametrize(
    ("data", "receipts", "error"),
    [
        (
            {
                "outcome": "PASS",
                "summary": "Paper-only pass.",
                "evidence_refs": [],
            },
            (),
            "requires current tool evidence",
        ),
        (
            {
                "outcome": "PASS",
                "summary": "Fabricated reference.",
                "evidence_refs": ["fabricated"],
            },
            (
                _snapshot("before", stage=Stage.REVIEW),
                _snapshot("after", stage=Stage.REVIEW),
            ),
            "unknown or stale",
        ),
        (
            {
                "outcome": "PASS",
                "summary": "A failed call was misreported as proof.",
                "evidence_refs": ["inspection"],
            },
            (
                _snapshot("before", stage=Stage.REVIEW),
                _receipt(
                    "inspection",
                    stage=Stage.REVIEW,
                    status=ToolReceiptStatus.FAILED,
                ),
                _snapshot("after", stage=Stage.REVIEW),
            ),
            "cannot support a passing Review",
        ),
        (
            {
                "outcome": "FAIL",
                "summary": "An incomplete start was called evidence.",
                "evidence_refs": ["inspection"],
            },
            (
                _snapshot("before", stage=Stage.REVIEW),
                _receipt(
                    "inspection",
                    stage=Stage.REVIEW,
                    status=ToolReceiptStatus.FAILED,
                    completed=False,
                ),
                _snapshot("after", stage=Stage.REVIEW),
            ),
            "did not complete",
        ),
        (
            {
                "outcome": "PASS",
                "summary": "One receipt was counted twice.",
                "evidence_refs": ["inspection", "inspection"],
            },
            (
                _snapshot("before", stage=Stage.REVIEW),
                _receipt("inspection", stage=Stage.REVIEW),
                _snapshot("after", stage=Stage.REVIEW),
            ),
            "duplicate tool evidence",
        ),
        (
            {
                "outcome": "PASS",
                "summary": "Only the boundary snapshots were cited.",
                "evidence_refs": ["before", "after"],
            },
            (
                _snapshot("before", stage=Stage.REVIEW),
                _snapshot("after", stage=Stage.REVIEW),
            ),
            "substantive evidence",
        ),
    ],
)
def test_review_rejects_paper_fabricated_failed_or_incomplete_pass_evidence(
    data, receipts, error
):
    with pytest.raises(StructuredOutputError, match=error):
        validate_review_response(
            StageResponse(data=data, provider_attempt=1, tool_receipts=receipts)
        )


def test_review_rejects_stale_invocation_and_workspace_drift():
    mixed = (
        _snapshot("before", stage=Stage.REVIEW, invocation="old"),
        _receipt("inspection", stage=Stage.REVIEW, invocation="current"),
        _snapshot("after", stage=Stage.REVIEW, invocation="current"),
    )
    data = {
        "outcome": "FAIL",
        "summary": "A concrete issue was observed.",
        "evidence_refs": ["inspection"],
    }
    with pytest.raises(StructuredOutputError, match="multiple invocations"):
        validate_review_response(
            StageResponse(data=data, provider_attempt=1, tool_receipts=mixed)
        )

    drifted = (
        _snapshot("before", stage=Stage.REVIEW, digest="one"),
        _receipt("inspection", stage=Stage.REVIEW),
        _snapshot("after", stage=Stage.REVIEW, digest="two"),
    )
    with pytest.raises(StructuredOutputError, match="drifted"):
        validate_review_response(
            StageResponse(data=data, provider_attempt=1, tool_receipts=drifted)
        )


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


def _isolated_verification_response(*, result="VERIFIED", status=None, exit_code=0):
    receipt_status = status or (
        ToolReceiptStatus.SUCCESS
        if exit_code == 0
        else ToolReceiptStatus.FAILED
    )
    return StageResponse(
        data={
            "outcome": result,
            "summary": "The isolated core recipe was assessed.",
            "checks": [
                {
                    "claim": "The core recipe passes",
                    "verifiability": "VERIFIABLE",
                    "result": result,
                    "method": "isolated_test",
                    "evidence_refs": ["test-run"],
                    "observed": f"exit code {exit_code}",
                }
            ],
            "evidence_refs": ["test-run"],
        },
        provider_attempt=1,
        tool_receipts=(
            _snapshot("before", stage=Stage.VERIFICATION),
            _receipt(
                "test-run",
                stage=Stage.VERIFICATION,
                tool_name="verification_run",
                status=receipt_status,
                details={
                    "operation": "run",
                    "recipe": "pytest_core",
                    "exit_code": exit_code,
                    "isolated": True,
                },
            ),
            _snapshot("after", stage=Stage.VERIFICATION),
        ),
    )


def test_assured_verification_binds_success_and_failure_to_real_run_receipts():
    verified = validate_verification_response(_isolated_verification_response())
    failed = validate_verification_response(
        _isolated_verification_response(result="FAILED", exit_code=1)
    )

    assert verified.outcome is VerificationOutcome.VERIFIED
    assert failed.outcome is VerificationOutcome.FAILED


def test_assured_verification_rejects_false_success_and_cross_stage_receipts():
    with pytest.raises(StructuredOutputError, match="successful current receipts"):
        validate_verification_response(
            _isolated_verification_response(result="VERIFIED", exit_code=1)
        )

    response = _isolated_verification_response()
    stale = tuple(
        ToolEvidenceReceipt(
            **{
                **receipt.__dict__,
                "stage": Stage.REVIEW if receipt.evidence_ref == "test-run" else receipt.stage,
            }
        )
        for receipt in response.tool_receipts
    )
    with pytest.raises(StructuredOutputError, match="another stage"):
        validate_verification_response(
            StageResponse(**{**response.__dict__, "tool_receipts": stale})
        )


def test_assured_verification_rejects_an_invented_verification_method():
    response = _isolated_verification_response()
    data = dict(response.data)
    data["checks"] = [
        {**dict(response.data["checks"][0]), "method": "trust_the_model"}
    ]

    with pytest.raises(StructuredOutputError, match="unsupported verification method"):
        validate_verification_response(
            StageResponse(**{**response.__dict__, "data": data})
        )


def test_not_ai_verifiable_is_reported_without_invented_tool_evidence():
    finding = validate_verification_response(
        StageResponse(
            data={
                "outcome": "NOT_AI_VERIFIABLE",
                "summary": "A human must judge the physical result.",
                "checks": [
                    {
                        "claim": "The physical installation is comfortable",
                        "verifiability": "NOT_AI_VERIFIABLE",
                        "result": "NOT_AI_VERIFIABLE",
                        "method": "visual_inspection",
                        "evidence_refs": [],
                        "observed": "No physical sensor is available.",
                    }
                ],
            }
        )
    )

    assert finding.outcome is VerificationOutcome.NOT_AI_VERIFIABLE
