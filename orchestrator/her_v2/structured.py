"""Bounded deterministic normalisation for model-authored stage envelopes."""

from __future__ import annotations

import json
from typing import Any, Mapping, TypeVar

from .interfaces import StructuredOutputError
from .models import (
    ExecutionDisposition,
    ExecutionOutcome,
    ReviewFinding,
    ReviewOutcome,
    StageResponse,
    TriageClassification,
    TriageDecision,
)


EnumT = TypeVar("EnumT")


def extract_json_object(text: str) -> Mapping[str, Any]:
    value = str(text or "").strip()
    if not value:
        raise StructuredOutputError("provider returned an empty structured response")
    try:
        parsed = json.loads(value)
        if isinstance(parsed, Mapping):
            return parsed
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for index, character in enumerate(value):
        if character != "{":
            continue
        try:
            parsed, _end = decoder.raw_decode(value[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, Mapping):
            return parsed
    raise StructuredOutputError("provider response contains no valid JSON object")


def response_data(response: StageResponse) -> Mapping[str, Any]:
    return response.data if response.data else extract_json_object(response.text)


def _enum_value(enum_type, raw: Any, *, aliases: Mapping[str, str] | None = None):
    normalized = str(raw or "").strip().upper().replace("-", "_").replace(" ", "_")
    if aliases:
        normalized = aliases.get(normalized, normalized)
    try:
        return enum_type(normalized)
    except ValueError as exc:
        raise StructuredOutputError(
            f"invalid {enum_type.__name__} value: {raw!r}"
        ) from exc


def parse_immediate(response: StageResponse) -> str:
    data = response_data(response)
    text = str(data.get("message") or data.get("response") or "").strip()
    if not text:
        raise StructuredOutputError("Immediate Response requires a message")
    return text


def parse_triage(response: StageResponse) -> TriageDecision:
    data = response_data(response)
    classification = _enum_value(
        TriageClassification,
        data.get("classification"),
        aliases={"DIRECT": "DIRECT_RESPONSE", "CONFIRM": "CONFIRMATION_REQUIRED"},
    )
    goal = str(data.get("goal") or "").strip()
    if not goal:
        raise StructuredOutputError("Triage requires an explicit goal")
    clarification = str(data.get("clarification") or "").strip()
    if (
        classification is TriageClassification.CONFIRMATION_REQUIRED
        and not clarification
    ):
        raise StructuredOutputError(
            "CONFIRMATION_REQUIRED requires a clarification question"
        )
    return TriageDecision(classification, goal, clarification)


def parse_plan(response: StageResponse) -> Mapping[str, Any]:
    data = response_data(response)
    plan = data.get("plan")
    if not isinstance(plan, (list, str)) or not plan:
        raise StructuredOutputError("Planning requires a non-empty plan")
    return data


def parse_execution(response: StageResponse) -> ExecutionOutcome:
    data = response_data(response)
    disposition = _enum_value(
        ExecutionDisposition,
        data.get("disposition") or data.get("status"),
        aliases={
            "SUCCESS": "COMPLETED",
            "SUCCEEDED": "COMPLETED",
            "LIMITED": "COMPLETED_WITH_LIMITATIONS",
            "UNSUCCESSFUL": "FAILED",
            "NEEDS_REPLAN": "REPLAN_REQUIRED",
        },
    )
    summary = str(data.get("summary") or data.get("result") or "").strip()
    if not summary:
        raise StructuredOutputError("Execution requires a truthful result summary")
    evidence = tuple(str(item) for item in data.get("evidence_refs") or () if str(item))
    limitations = tuple(str(item) for item in data.get("limitations") or () if str(item))
    reason = str(data.get("replan_reason") or "").strip()
    if disposition is ExecutionDisposition.REPLAN_REQUIRED and not reason:
        raise StructuredOutputError("REPLAN_REQUIRED requires a concrete reason")
    return ExecutionOutcome(disposition, summary, evidence, limitations, reason)


def parse_review(response: StageResponse) -> ReviewFinding:
    data = response_data(response)
    outcome = _enum_value(ReviewOutcome, data.get("outcome"))
    summary = str(data.get("summary") or "").strip()
    if not summary:
        raise StructuredOutputError("Review requires a summary")
    findings = tuple(str(item) for item in data.get("findings") or () if str(item))
    return ReviewFinding(outcome, summary, findings)


def parse_report(response: StageResponse) -> str:
    data = response_data(response)
    report = str(data.get("report") or data.get("message") or "").strip()
    if not report:
        raise StructuredOutputError("Finalisation requires a user-facing report")
    return report
