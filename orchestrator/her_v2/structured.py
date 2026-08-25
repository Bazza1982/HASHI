"""Deterministic compatibility membrane for model-authored stage envelopes."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, is_dataclass, replace
from functools import wraps
from typing import Any, Callable, Mapping, TypeVar

from .interfaces import StructuredOutputError
from .models import (
    ExecutionDisposition,
    ExecutionOutcome,
    FinalisationOutcome,
    ReplanningOutcome,
    ReviewFinding,
    ReviewOutcome,
    Stage,
    StageResponse,
    ToolEvidenceReceipt,
    TriageClassification,
    TriageDecision,
    Verifiability,
    VerificationCheck,
    VerificationFinding,
    VerificationOutcome,
)

ParsedT = TypeVar("ParsedT")
MappingParser = Callable[[Mapping[str, Any]], ParsedT]

_MAX_SOURCE_CHARS = 200_000
_MAX_JSON_CANDIDATES = 8
_REGISTERED_WRAPPERS = frozenset(
    {"data", "output", "parsed", "response", "result", "structured_output"}
)
_WRAPPER_METADATA = frozenset(
    {"id", "model", "provider", "status", "stop_reason", "type", "usage"}
)


@dataclass(frozen=True)
class StructuredResolution:
    """One semantically validated result and its audited compatibility source."""

    response: StageResponse
    parsed: Any
    source: str
    rejected_candidates: tuple[tuple[str, str], ...] = ()

    @property
    def recovered(self) -> bool:
        return self.source in {
            "provider_json_control_char_repair",
            "provider_plain_text",
            "reasoning_recovery",
        } or bool(self.rejected_candidates)


@dataclass(frozen=True)
class _Candidate:
    source: str
    data: Mapping[str, Any]


def _mapping_key(value: Mapping[str, Any]) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return repr(dict(value))


def _semantic_key(value: Any) -> str:
    if isinstance(value, TriageDecision):
        # Goal interpretation is audit evidence, not turn authority.
        value = {
            "classification": value.classification.value,
            "clarification": value.clarification,
        }
    elif isinstance(value, ReplanningOutcome):
        value = {
            key: item for key, item in asdict(value).items() if key != "commentary"
        }
    elif isinstance(value, Mapping):
        # Planning commentary and provider transport metadata do not alter the
        # binding plan semantics.
        value = {
            key: item
            for key, item in value.items()
            if str(key) != "commentary" and str(key) not in _WRAPPER_METADATA
        }
    elif is_dataclass(value):
        value = asdict(value)
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return repr(value)


def _append_mapping(
    items: list[Mapping[str, Any]],
    seen: set[str],
    value: Any,
) -> None:
    if not isinstance(value, Mapping) or len(items) >= _MAX_JSON_CANDIDATES:
        return
    key = _mapping_key(value)
    if key in seen:
        return
    seen.add(key)
    items.append(value)

    # Only unwrap explicitly registered provider envelope keys.  Other nested
    # objects may be evidence or user data and must never acquire authority.
    non_metadata = {str(key) for key in value} - _WRAPPER_METADATA
    if len(non_metadata) != 1:
        return
    wrapper = next(iter(non_metadata))
    if wrapper in _REGISTERED_WRAPPERS:
        _append_mapping(items, seen, value.get(wrapper))


def _extract_json_objects(
    text: str,
    *,
    strict: bool,
) -> tuple[Mapping[str, Any], ...]:
    value = str(text or "").strip()
    if not value:
        return ()
    if len(value) > _MAX_SOURCE_CHARS:
        value = value[:_MAX_SOURCE_CHARS]

    items: list[Mapping[str, Any]] = []
    seen: set[str] = set()

    # First accept a complete object, including one deterministic layer of
    # JSON-string encoding used by several OpenAI-compatible gateways.
    try:
        parsed: Any = json.loads(value, strict=strict)
        if isinstance(parsed, str):
            parsed = json.loads(parsed, strict=strict)
        _append_mapping(items, seen, parsed)
    except (json.JSONDecodeError, TypeError):
        pass

    decoder = json.JSONDecoder(strict=strict)
    for index, character in enumerate(value):
        if len(items) >= _MAX_JSON_CANDIDATES:
            break
        if character != "{":
            continue
        try:
            parsed, _end = decoder.raw_decode(value[index:])
        except json.JSONDecodeError:
            continue
        _append_mapping(items, seen, parsed)
    return tuple(items)


def extract_json_objects(text: str) -> tuple[Mapping[str, Any], ...]:
    """Extract bounded strict-JSON object candidates without guessing meaning."""

    return _extract_json_objects(text, strict=True)


def extract_json_object(text: str) -> Mapping[str, Any]:
    """Backwards-compatible first-object helper used outside stage validation."""

    value = str(text or "").strip()
    if not value:
        raise StructuredOutputError("provider returned an empty structured response")
    candidates = extract_json_objects(value)
    if candidates:
        return candidates[0]
    raise StructuredOutputError("provider response contains no valid JSON object")


def response_data(response: StageResponse) -> Mapping[str, Any]:
    """Return the first transport-level mapping for non-authoritative consumers."""

    if response.data:
        return response.data
    candidates = extract_json_objects(response.text)
    if candidates:
        return candidates[0]
    raise StructuredOutputError("provider response contains no valid JSON object")


def _candidate_group(
    response: StageResponse,
    *,
    include_reasoning: bool,
    plain_text_field: str | None,
) -> tuple[_Candidate, ...]:
    candidates: list[_Candidate] = []
    seen: set[tuple[str, str]] = set()

    def add(source: str, data: Mapping[str, Any]) -> None:
        key = (source.split(":", 1)[0], _mapping_key(data))
        if key in seen:
            return
        seen.add(key)
        candidates.append(_Candidate(source, data))

    if include_reasoning:
        for index, data in enumerate(
            extract_json_objects(response.reasoning_trace or "")
        ):
            add(f"reasoning_recovery:{index + 1}", data)
        return tuple(candidates)

    if response.data:
        add("provider_data", response.data)
    text_candidates = extract_json_objects(response.text)
    for index, data in enumerate(text_candidates):
        add(f"provider_text:{index + 1}", data)
    text = str(response.text or "").strip()
    strict_keys = {_mapping_key(data) for data in text_candidates}
    repaired_text_candidates = tuple(
        data
        for data in _extract_json_objects(response.text, strict=False)
        if _mapping_key(data) not in strict_keys
    )
    for index, data in enumerate(repaired_text_candidates):
        add(f"provider_json_control_char_repair:{index + 1}", data)
    if (
        plain_text_field
        and text
        and not text_candidates
        and not repaired_text_candidates
    ):
        add("provider_plain_text", {plain_text_field: text})
    return tuple(candidates)


def _resolve_group(
    response: StageResponse,
    parser: MappingParser,
    candidates: tuple[_Candidate, ...],
) -> tuple[StructuredResolution | None, tuple[tuple[str, str], ...]]:
    valid: dict[str, list[tuple[_Candidate, Any]]] = {}
    rejected: list[tuple[str, str]] = []
    for candidate in candidates:
        try:
            parsed = parser(candidate.data)
        except StructuredOutputError as exc:
            rejected.append((candidate.source, str(exc)))
            continue
        valid.setdefault(_semantic_key(parsed), []).append((candidate, parsed))

    if len(valid) > 1:
        sources = ", ".join(
            item.source for group in valid.values() for item, _parsed in group
        )
        raise StructuredOutputError(
            f"conflicting valid structured candidates from: {sources}"
        )
    if not valid:
        return None, tuple(rejected)

    group = next(iter(valid.values()))
    candidate, parsed = group[0]
    effective = replace(response, data=candidate.data)
    return (
        StructuredResolution(
            response=effective,
            parsed=parsed,
            source=candidate.source.split(":", 1)[0],
            rejected_candidates=tuple(rejected),
        ),
        tuple(rejected),
    )


def resolve_stage_response(
    response: StageResponse,
    validator: Callable[[StageResponse], ParsedT],
) -> StructuredResolution:
    """Validate all compatible carriers while rejecting semantic ambiguity.

    Formal provider data and assistant text are considered first.  Provider
    reasoning is a last-resort control-envelope carrier only when no formal
    carrier validates; it is never exposed as the user-facing response.
    """

    parser = getattr(validator, "_mapping_parser", None)
    if not callable(parser):
        # Non-registered validators retain their strict historical contract.
        return StructuredResolution(response, validator(response), "provider_response")
    plain_text_field = getattr(validator, "_plain_text_field", None)

    primary = _candidate_group(
        response,
        include_reasoning=False,
        plain_text_field=plain_text_field,
    )
    resolved, primary_rejected = _resolve_group(response, parser, primary)
    if resolved is not None:
        return resolved

    reasoning = _candidate_group(
        response,
        include_reasoning=True,
        plain_text_field=None,
    )
    resolved, reasoning_rejected = _resolve_group(response, parser, reasoning)
    if resolved is not None:
        return replace(
            resolved,
            rejected_candidates=primary_rejected + resolved.rejected_candidates,
        )

    rejected = primary_rejected + reasoning_rejected
    if rejected:
        detail = "; ".join(f"{source}: {error}" for source, error in rejected)
        raise StructuredOutputError(
            f"provider response has no compatible structured result ({detail})"
        )
    if (
        not str(response.text or "").strip()
        and not str(response.reasoning_trace or "").strip()
        and not response.data
    ):
        raise StructuredOutputError("provider returned an empty structured response")
    raise StructuredOutputError("provider response contains no valid JSON object")


def _stage_parser(*, plain_text_field: str | None = None):
    def decorate(parser: MappingParser):
        @wraps(parser)
        def validate(response: StageResponse):
            return resolve_stage_response(response, validate).parsed

        setattr(validate, "_mapping_parser", parser)
        setattr(validate, "_plain_text_field", plain_text_field)
        return validate

    return decorate


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


def _string_items(raw: Any, *, field: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        value = raw.strip()
        return (value,) if value else ()
    if not isinstance(raw, (list, tuple)):
        raise StructuredOutputError(f"{field} must be a string or list of strings")
    if any(not isinstance(item, str) for item in raw):
        raise StructuredOutputError(f"{field} must contain only strings")
    return tuple(item.strip() for item in raw if item.strip())


def _text_value(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


@_stage_parser(plain_text_field="message")
def parse_immediate(data: Mapping[str, Any]) -> str:
    text = _text_value(data.get("message"), data.get("response"))
    if not text:
        raise StructuredOutputError("Immediate Response requires a message")
    return text


@_stage_parser(plain_text_field="message")
def parse_execution_message(data: Mapping[str, Any]) -> str:
    """Accept the primary Execution agent's natural-language response."""

    text = _text_value(
        data.get("message"),
        data.get("response"),
        data.get("text"),
        data.get("final_message"),
        # Rolling compatibility for scripted providers that still return the
        # old Execution envelope.  This does not restore JSON as a requirement.
        data.get("summary"),
        data.get("result"),
    )
    if not text:
        raise StructuredOutputError("Execution requires a non-empty response")
    return text


@_stage_parser()
def parse_triage(data: Mapping[str, Any]) -> TriageDecision:
    classification = _enum_value(
        TriageClassification,
        data.get("classification") or data.get("task_type") or data.get("route"),
        aliases={
            "CHAT": "DIRECT_RESPONSE",
            "DIRECT": "DIRECT_RESPONSE",
            "DIRECT_ANSWER": "DIRECT_RESPONSE",
            "SIMPLE": "SIMPLE_TASK",
            "COMPLEX": "COMPLEX_TASK",
            "HIGH_VOLUME": "HIGH_VOLUME_TASK",
            "CONFIRM": "CONFIRMATION_REQUIRED",
            "NEEDS_CONFIRMATION": "CONFIRMATION_REQUIRED",
        },
    )
    # This interpretation is useful audit evidence, but the original user
    # request remains the sole authoritative goal throughout the turn.
    goal = _text_value(data.get("goal"), data.get("interpreted_goal"))
    clarification = _text_value(data.get("clarification"), data.get("question"))
    if (
        classification is TriageClassification.CONFIRMATION_REQUIRED
        and not clarification
    ):
        raise StructuredOutputError(
            "CONFIRMATION_REQUIRED requires a clarification question"
        )
    return TriageDecision(classification, goal, clarification)


@_stage_parser()
def parse_plan(data: Mapping[str, Any]) -> Mapping[str, Any]:
    plan = data.get("plan")
    if plan is None:
        plan = data.get("steps")
    if (
        not isinstance(plan, list)
        or not plan
        or any(not isinstance(item, str) or not item.strip() for item in plan)
    ):
        raise StructuredOutputError(
            "Planning plan must be a non-empty list of non-empty strings"
        )
    success_criteria = data.get("success_criteria")
    if (
        not isinstance(success_criteria, list)
        or not success_criteria
        or any(
            not isinstance(item, str) or not item.strip() for item in success_criteria
        )
    ):
        raise StructuredOutputError(
            "Planning success_criteria must be a non-empty list of non-empty strings"
        )
    parallel_groups = data.get("parallel_groups", [])
    if not isinstance(parallel_groups, list):
        raise StructuredOutputError("Planning parallel_groups must be a list")
    sub_agents = data.get("sub_agents", [])
    if not isinstance(sub_agents, list):
        raise StructuredOutputError("Planning sub_agents must be a list")
    seen_ids: set[str] = set()
    for index, raw in enumerate(sub_agents, start=1):
        if not isinstance(raw, Mapping):
            raise StructuredOutputError(
                f"Planning sub-agent assignment {index} must be an object"
            )
        assignment_id = _text_value(raw.get("id"))
        task = _text_value(raw.get("task"))
        profile = _text_value(raw.get("profile"))
        tools = raw.get("tools")
        attachment_ids = raw.get("attachment_ids")
        allow_side_effects = raw.get("allow_side_effects")
        if not assignment_id or assignment_id in seen_ids:
            raise StructuredOutputError(
                "Planning sub-agent assignments require unique non-empty IDs"
            )
        seen_ids.add(assignment_id)
        if not task or not profile:
            raise StructuredOutputError(
                "Planning sub-agent assignments require task and profile"
            )
        if not isinstance(tools, list) or any(
            not isinstance(item, str) or not item.strip() for item in tools
        ):
            raise StructuredOutputError(
                "Planning sub-agent assignment tools must be a list of strings"
            )
        if attachment_ids is not None and (
            not isinstance(attachment_ids, list)
            or any(
                not isinstance(item, str) or not item.strip() for item in attachment_ids
            )
        ):
            raise StructuredOutputError(
                "Planning sub-agent attachment_ids must be a list of strings"
            )
        if not isinstance(allow_side_effects, bool):
            raise StructuredOutputError(
                "Planning sub-agent allow_side_effects must be a boolean"
            )
    commentary = data.get("commentary")
    if commentary is not None and not isinstance(commentary, str):
        raise StructuredOutputError("Planning commentary must be a string")
    normalized = {
        "plan": plan,
        "success_criteria": success_criteria,
        "parallel_groups": parallel_groups,
        "sub_agents": sub_agents,
    }
    if isinstance(commentary, str) and commentary.strip():
        normalized["commentary"] = commentary.strip()
    return normalized


@_stage_parser()
def parse_replanning(data: Mapping[str, Any]) -> ReplanningOutcome:
    """Validate the three compulsory Replanning calibration answers.

    Commentary deliberately remains recoverable presentation data: an absent
    or malformed value is normalised to empty so Runtime can build the required
    deterministic message from the validated control fields.
    """

    forbidden = {
        "authoritative_user_goal",
        "authority",
        "classification",
        "goal",
        "permissions",
        "user_goal",
    }
    supplied_forbidden = sorted(
        key
        for key in forbidden
        if key in data and data.get(key) not in (None, "", [], {})
    )
    if supplied_forbidden:
        raise StructuredOutputError(
            "Replanning cannot replace goal, classification, authority, or "
            f"permissions: {', '.join(supplied_forbidden)}"
        )

    steps = data.get("plan")
    if steps is None:
        steps = data.get("steps")
    if not isinstance(steps, (list, str)) or not steps:
        raise StructuredOutputError("Replanning requires a non-empty plan")
    if isinstance(steps, list) and (
        not steps
        or any(not isinstance(item, str) or not item.strip() for item in steps)
    ):
        raise StructuredOutputError(
            "Replanning plan must contain only non-empty strings"
        )
    if isinstance(steps, str) and not steps.strip():
        raise StructuredOutputError("Replanning requires a non-empty plan")

    success_criteria = data.get("success_criteria")
    if not isinstance(success_criteria, (list, str)) or not success_criteria:
        raise StructuredOutputError("Replanning requires non-empty success_criteria")
    if isinstance(success_criteria, list) and (
        not success_criteria
        or any(
            not isinstance(item, str) or not item.strip() for item in success_criteria
        )
    ):
        raise StructuredOutputError(
            "Replanning success_criteria must contain only non-empty strings"
        )
    if isinstance(success_criteria, str) and not success_criteria.strip():
        raise StructuredOutputError("Replanning requires non-empty success_criteria")

    raw_percent = data.get("completion_percent")
    if isinstance(raw_percent, bool) or not isinstance(raw_percent, int):
        raise StructuredOutputError(
            "Replanning completion_percent must be an integer from 0 through 100"
        )
    if not 0 <= raw_percent <= 100:
        raise StructuredOutputError(
            "Replanning completion_percent must be from 0 through 100"
        )
    completion_basis = _text_value(data.get("completion_basis"))
    if not completion_basis:
        raise StructuredOutputError(
            "Replanning requires an evidence-based completion_basis"
        )

    raw_plan_changed = data.get("plan_changed")
    if not isinstance(raw_plan_changed, bool):
        raise StructuredOutputError("Replanning plan_changed must be a boolean")
    change_reason = _text_value(data.get("change_reason"), data.get("changed_because"))
    if raw_plan_changed and not change_reason:
        raise StructuredOutputError(
            "A changed Replanning plan requires a concrete change_reason"
        )
    if not raw_plan_changed and change_reason:
        raise StructuredOutputError(
            "An unchanged Replanning plan must not invent a change_reason"
        )

    next_step = _text_value(data.get("next_step"))
    if not next_step:
        raise StructuredOutputError("Replanning requires a concrete next_step")

    plan: dict[str, Any] = {
        "plan": steps,
        "success_criteria": success_criteria,
        "parallel_groups": data.get("parallel_groups", []),
        "sub_agents": data.get("sub_agents", []),
    }

    raw_commentary = data.get("commentary")
    commentary = (
        raw_commentary.strip()
        if isinstance(raw_commentary, str) and raw_commentary.strip()
        else ""
    )
    return ReplanningOutcome(
        plan=plan,
        completion_percent=raw_percent,
        completion_basis=completion_basis,
        plan_changed=raw_plan_changed,
        change_reason=change_reason,
        next_step=next_step,
        commentary=commentary,
    )


def _parse_execution_data(data: Mapping[str, Any]) -> ExecutionOutcome:
    disposition = _enum_value(
        ExecutionDisposition,
        data.get("disposition") or data.get("status"),
        aliases={
            "DONE": "COMPLETED",
            "SUCCESS": "COMPLETED",
            "SUCCEEDED": "COMPLETED",
            "LIMITED": "COMPLETED_WITH_LIMITATIONS",
            "PARTIAL": "COMPLETED_WITH_LIMITATIONS",
            "PARTIAL_SUCCESS": "COMPLETED_WITH_LIMITATIONS",
            "UNSUCCESSFUL": "FAILED",
            "BLOCKED_ON_USER": "USER_INPUT_REQUIRED",
            "INPUT_REQUIRED": "USER_INPUT_REQUIRED",
            "NEEDS_USER_INPUT": "USER_INPUT_REQUIRED",
            "PENDING_USER_INPUT": "USER_INPUT_REQUIRED",
        },
    )
    summary = _text_value(data.get("summary"), data.get("result"))
    if not summary:
        raise StructuredOutputError("Execution requires a truthful result summary")
    evidence = _string_items(data.get("evidence_refs"), field="evidence_refs")
    limitations = _string_items(data.get("limitations"), field="limitations")
    work_performed = _string_items(data.get("work_performed"), field="work_performed")
    verification = _string_items(data.get("verification"), field="verification")
    remaining_work = _string_items(data.get("remaining_work"), field="remaining_work")
    clarification = _text_value(data.get("clarification"), data.get("question"))
    if disposition is ExecutionDisposition.USER_INPUT_REQUIRED and not clarification:
        raise StructuredOutputError(
            "USER_INPUT_REQUIRED requires a clarification question"
        )
    return ExecutionOutcome(
        disposition,
        summary,
        evidence,
        limitations,
        clarification,
        work_performed,
        verification,
        remaining_work,
    )


@_stage_parser()
def parse_execution(data: Mapping[str, Any]) -> ExecutionOutcome:
    return _parse_execution_data(data)


@_stage_parser()
def parse_review(data: Mapping[str, Any]) -> ReviewFinding:
    if any(key in data for key in ("status", "reason", "conditions")):
        expected = {"status", "reason", "conditions"}
        missing = expected - set(data)
        unexpected = set(data) - expected
        if missing:
            raise StructuredOutputError(
                f"Review response is missing fields: {sorted(missing)}"
            )
        if unexpected:
            raise StructuredOutputError(
                f"Review response contains unexpected fields: {sorted(unexpected)}"
            )
        outcome = _enum_value(ReviewOutcome, data.get("status"))
        if outcome not in {
            ReviewOutcome.PASS,
            ReviewOutcome.CONDITIONAL_PASS,
            ReviewOutcome.FAIL,
        }:
            raise StructuredOutputError(
                "Review status must be PASS, CONDITIONAL_PASS, or FAIL"
            )
        reason = _text_value(data.get("reason"))
        if not reason:
            raise StructuredOutputError("Review requires a reason")
        raw_conditions = data.get("conditions")
        if outcome is ReviewOutcome.CONDITIONAL_PASS:
            if not isinstance(raw_conditions, str) or not raw_conditions.strip():
                raise StructuredOutputError(
                    "CONDITIONAL_PASS requires non-empty conditions"
                )
            conditions = (raw_conditions.strip(),)
        else:
            if raw_conditions is not None:
                raise StructuredOutputError(
                    f"{outcome.value} requires conditions to be null"
                )
            conditions = ()
        return ReviewFinding(outcome, reason, conditions, ())

    # Rolling compatibility for in-flight scripted providers using the prior
    # internal Review envelope. New model prompts expose only the contract above.
    outcome = _enum_value(
        ReviewOutcome,
        data.get("outcome"),
        aliases={
            "CONDITIONAL": "CONDITIONAL_PASS",
            "PASSED": "PASS",
            "UNKNOWN": "INCONCLUSIVE",
            "BLOCKED": "UNAVAILABLE",
        },
    )
    summary = _text_value(data.get("summary"), data.get("reason"))
    if not summary:
        raise StructuredOutputError("Review requires a summary")
    findings = _string_items(data.get("findings"), field="findings")
    evidence = _string_items(data.get("evidence_refs"), field="evidence_refs")
    return ReviewFinding(outcome, summary, findings, evidence)


def _strict_bool(raw: Any, *, field: str, default: bool) -> bool:
    if raw is None:
        return default
    if not isinstance(raw, bool):
        raise StructuredOutputError(f"{field} must be a boolean")
    return raw


def _parse_verification_check(raw: Any, *, index: int) -> VerificationCheck:
    if not isinstance(raw, Mapping):
        raise StructuredOutputError(f"checks[{index}] must be an object")
    claim = _text_value(raw.get("claim"))
    if not claim:
        raise StructuredOutputError(f"checks[{index}] requires a claim")
    verifiability = _enum_value(
        Verifiability,
        raw.get("verifiability"),
        aliases={
            "PARTIAL": "PARTIALLY_VERIFIABLE",
            "NOT_VERIFIABLE": "NOT_AI_VERIFIABLE",
            "BLOCKED": "UNAVAILABLE",
        },
    )
    result = _enum_value(
        VerificationOutcome,
        raw.get("result") or raw.get("status"),
        aliases={
            "PASS": "VERIFIED",
            "PARTIAL": "PARTIALLY_VERIFIED",
            "FAIL": "FAILED",
            "BLOCKED": "UNAVAILABLE",
            "UNKNOWN": "INCONCLUSIVE",
        },
    )
    method = _text_value(raw.get("method"))
    if not method:
        raise StructuredOutputError(f"checks[{index}] requires a method")
    evidence_refs = _string_items(
        raw.get("evidence_refs"), field=f"checks[{index}].evidence_refs"
    )
    observed = _text_value(raw.get("observed"))
    return VerificationCheck(
        claim=claim,
        verifiability=verifiability,
        result=result,
        method=method,
        evidence_refs=evidence_refs,
        observed=observed,
        required=_strict_bool(
            raw.get("required"), field=f"checks[{index}].required", default=True
        ),
    )


@_stage_parser()
def parse_verification(data: Mapping[str, Any]) -> VerificationFinding:
    outcome = _enum_value(
        VerificationOutcome,
        data.get("outcome") or data.get("status"),
        aliases={
            "PASS": "VERIFIED",
            "PARTIAL": "PARTIALLY_VERIFIED",
            "FAIL": "FAILED",
            "BLOCKED": "UNAVAILABLE",
            "UNKNOWN": "INCONCLUSIVE",
        },
    )
    summary = _text_value(data.get("summary"), data.get("reason"))
    if not summary:
        raise StructuredOutputError("Verification requires a summary")
    raw_checks = data.get("checks")
    if not isinstance(raw_checks, list) or not raw_checks:
        raise StructuredOutputError("Verification requires a non-empty checks list")
    checks = tuple(
        _parse_verification_check(item, index=index)
        for index, item in enumerate(raw_checks)
    )
    evidence_refs = _string_items(data.get("evidence_refs"), field="evidence_refs")
    limitations = _string_items(data.get("limitations"), field="limitations")
    return VerificationFinding(
        outcome=outcome,
        summary=summary,
        checks=checks,
        evidence_refs=evidence_refs,
        limitations=limitations,
    )


def _receipt_index(
    response: StageResponse, *, expected_stage: Stage
) -> dict[str, ToolEvidenceReceipt]:
    receipts = tuple(response.tool_receipts)
    if any(receipt.stage is not expected_stage for receipt in receipts):
        raise StructuredOutputError(
            f"{expected_stage.value} received evidence from another stage"
        )
    if any(receipt.attempt != response.provider_attempt for receipt in receipts):
        raise StructuredOutputError(
            f"{expected_stage.value} received evidence from another provider attempt"
        )
    invocations = {receipt.invocation_id for receipt in receipts}
    if len(invocations) > 1:
        raise StructuredOutputError(
            f"{expected_stage.value} mixed evidence from multiple invocations"
        )
    result = {receipt.evidence_ref: receipt for receipt in receipts}
    if len(result) != len(receipts):
        raise StructuredOutputError("tool evidence references must be unique")
    return result


def _validated_receipts(
    response: StageResponse,
    refs: tuple[str, ...],
    *,
    field: str,
    expected_stage: Stage,
) -> tuple[ToolEvidenceReceipt, ...]:
    known = _receipt_index(response, expected_stage=expected_stage)
    if len(set(refs)) != len(refs):
        raise StructuredOutputError(f"{field} contains duplicate tool evidence")
    unknown = [ref for ref in refs if ref not in known]
    if unknown:
        raise StructuredOutputError(
            f"{field} references unknown or stale tool evidence: {', '.join(unknown)}"
        )
    receipts = tuple(known[ref] for ref in refs)
    if any(not receipt.completed for receipt in receipts):
        raise StructuredOutputError(f"{field} cites a tool call that did not complete")
    return receipts


def _snapshot_receipts(
    response: StageResponse,
) -> tuple[ToolEvidenceReceipt, ToolEvidenceReceipt] | None:
    receipts = tuple(response.tool_receipts)
    if len(receipts) < 2:
        return None
    first, last = receipts[0], receipts[-1]
    if (
        first.tool_name != "workspace_inspect"
        or last.tool_name != "workspace_inspect"
        or first.details.get("operation") != "snapshot"
        or last.details.get("operation") != "snapshot"
        or not first.successful
        or not last.successful
    ):
        return None
    return first, last


def _require_stable_snapshot(response: StageResponse) -> None:
    pair = _snapshot_receipts(response)
    if pair is None:
        raise StructuredOutputError(
            "evidence-backed assessment must begin and end with successful "
            "workspace_inspect snapshot calls"
        )
    before, after = pair
    before_digest = str(before.details.get("snapshot_sha256") or "")
    after_digest = str(after.details.get("snapshot_sha256") or "")
    if not before_digest or not after_digest:
        raise StructuredOutputError("workspace snapshot receipts require digests")
    if before_digest != after_digest:
        raise StructuredOutputError(
            "the reviewed workspace drifted during assessment; outcome must be INCONCLUSIVE"
        )


def validate_review_response(response: StageResponse) -> ReviewFinding:
    """Validate the three-state Review decision contract.

    Review tools remain independently recorded by the Tool Gateway, but the
    reviewer may also judge work that cannot be objectively tool-verified.
    Therefore a valid decision does not require receipt citations.
    """

    finding = parse_review(response)
    assert isinstance(finding, ReviewFinding)
    return finding


_METHOD_TOOLS: Mapping[str, frozenset[str]] = {
    "workspace_test": frozenset({"verification_run"}),
    "workspace_snapshot": frozenset({"workspace_inspect"}),
    "workspace_status": frozenset({"workspace_inspect"}),
    "workspace_diff": frozenset({"workspace_inspect"}),
    "workspace_search": frozenset({"workspace_inspect"}),
    "file_hash": frozenset({"workspace_inspect"}),
    "artifact_inspection": frozenset(
        {"workspace_inspect", "file_read", "file_list", "media_read"}
    ),
    "process_health": frozenset({"process_list"}),
    "read_only_api": frozenset({"web_fetch", "hashi_scheduler_status"}),
    "visual_inspection": frozenset(
        {"media_read", "vision_inspect", "browser_screenshot", "desktop_screenshot"}
    ),
}


def _successful_workspace_test_receipt(receipt: ToolEvidenceReceipt) -> bool:
    details = receipt.details
    timeout_policy = details.get("timeout_policy")
    workspace_access = details.get("workspace_access")
    if not isinstance(timeout_policy, Mapping) or not isinstance(
        workspace_access, Mapping
    ):
        return False
    try:
        exit_code = int(details.get("exit_code", 1))
        effective_timeout_s = float(timeout_policy.get("effective_timeout_s", 0))
        execution_floor_s = float(timeout_policy.get("execution_floor_s", 0))
        receipt_timeout_s = float(details.get("timeout_s", 0))
    except (TypeError, ValueError):
        return False
    return bool(
        receipt.tool_name == "verification_run"
        and details.get("operation") == "run"
        and exit_code == 0
        and details.get("execution_scope") == "workspace"
        and details.get("workspace_copied") is False
        and details.get("process_isolated") is False
        and details.get("process_authority") == "inherited"
        and details.get("identity_policy") == "inherited"
        and details.get("filesystem_policy") == "inherited"
        and details.get("environment_policy") == "inherited"
        and details.get("network_policy") == "inherited"
        and details.get("home_policy") == "inherited"
        and workspace_access.get("read") is True
        and workspace_access.get("write") is True
        and workspace_access.get("execute") is True
        and effective_timeout_s > 0
        and receipt_timeout_s == effective_timeout_s
        and effective_timeout_s >= execution_floor_s
    )


def _validate_verification_check(
    response: StageResponse,
    check: VerificationCheck,
) -> None:
    receipts = _validated_receipts(
        response,
        check.evidence_refs,
        field=f"Verification evidence for {check.claim!r}",
        expected_stage=Stage.VERIFICATION,
    )
    allowed_tools = _METHOD_TOOLS.get(check.method)
    if allowed_tools is None:
        raise StructuredOutputError(
            f"unsupported verification method: {check.method!r}"
        )
    if check.result in {
        VerificationOutcome.VERIFIED,
        VerificationOutcome.PARTIALLY_VERIFIED,
    }:
        if check.verifiability not in {
            Verifiability.VERIFIABLE,
            Verifiability.PARTIALLY_VERIFIABLE,
        }:
            raise StructuredOutputError(
                "verified checks must be classified as verifiable"
            )
        if not receipts or any(not receipt.successful for receipt in receipts):
            raise StructuredOutputError(
                "VERIFIED and PARTIALLY_VERIFIED checks require successful current receipts"
            )
        if not any(receipt.tool_name in allowed_tools for receipt in receipts):
            raise StructuredOutputError(
                f"verification method {check.method!r} lacks a matching tool receipt"
            )
        if check.method == "workspace_test" and not any(
            _successful_workspace_test_receipt(receipt) for receipt in receipts
        ):
            raise StructuredOutputError(
                "workspace_test verification requires a successful direct-workspace "
                "verification_run receipt with inherited authority and a runtime-derived "
                "timeout"
            )
    elif check.result is VerificationOutcome.FAILED:
        if check.verifiability not in {
            Verifiability.VERIFIABLE,
            Verifiability.PARTIALLY_VERIFIABLE,
        }:
            raise StructuredOutputError(
                "FAILED checks must be classified as verifiable"
            )
        if not receipts:
            raise StructuredOutputError(
                "FAILED checks require concrete current evidence"
            )
    elif check.result is VerificationOutcome.NOT_AI_VERIFIABLE:
        if check.verifiability is not Verifiability.NOT_AI_VERIFIABLE:
            raise StructuredOutputError(
                "NOT_AI_VERIFIABLE results require matching verifiability"
            )
    elif check.result is VerificationOutcome.UNAVAILABLE:
        if check.verifiability is not Verifiability.UNAVAILABLE:
            raise StructuredOutputError(
                "UNAVAILABLE results require matching verifiability"
            )
        if any(not receipt.successful for receipt in receipts):
            raise StructuredOutputError(
                "failed tools can support only FAILED or INCONCLUSIVE checks"
            )


def validate_verification_response(response: StageResponse) -> VerificationFinding:
    """Bind every verification claim to completed current-invocation receipts."""

    finding = parse_verification(response)
    assert isinstance(finding, VerificationFinding)
    _receipt_index(response, expected_stage=Stage.VERIFICATION)
    for check in finding.checks:
        _validate_verification_check(response, check)
    if finding.evidence_refs:
        overall_receipts = _validated_receipts(
            response,
            finding.evidence_refs,
            field="Verification evidence_refs",
            expected_stage=Stage.VERIFICATION,
        )
        if finding.outcome is VerificationOutcome.UNAVAILABLE and any(
            not receipt.successful for receipt in overall_receipts
        ):
            raise StructuredOutputError(
                "failed tools can support only FAILED or INCONCLUSIVE Verification outcomes"
            )

    required = tuple(check for check in finding.checks if check.required)
    results = {check.result for check in required}
    if finding.outcome is VerificationOutcome.VERIFIED:
        if not required or results != {VerificationOutcome.VERIFIED}:
            raise StructuredOutputError(
                "overall VERIFIED requires every required check to be VERIFIED"
            )
    elif finding.outcome is VerificationOutcome.PARTIALLY_VERIFIED:
        if not results.intersection(
            {VerificationOutcome.VERIFIED, VerificationOutcome.PARTIALLY_VERIFIED}
        ) or results <= {VerificationOutcome.VERIFIED}:
            raise StructuredOutputError(
                "PARTIALLY_VERIFIED requires both verified evidence and a stated gap"
            )
    elif finding.outcome is VerificationOutcome.FAILED:
        if VerificationOutcome.FAILED not in results:
            raise StructuredOutputError(
                "overall FAILED requires a failed required check"
            )
    elif finding.outcome is VerificationOutcome.NOT_AI_VERIFIABLE:
        if not required or results != {VerificationOutcome.NOT_AI_VERIFIABLE}:
            raise StructuredOutputError(
                "overall NOT_AI_VERIFIABLE requires every required check to match"
            )
    elif finding.outcome is VerificationOutcome.UNAVAILABLE:
        if VerificationOutcome.UNAVAILABLE not in results:
            raise StructuredOutputError(
                "overall UNAVAILABLE requires an unavailable required check"
            )
    elif finding.outcome is VerificationOutcome.INCONCLUSIVE:
        if VerificationOutcome.INCONCLUSIVE not in results:
            raise StructuredOutputError(
                "overall INCONCLUSIVE requires an inconclusive required check"
            )

    if finding.outcome in {
        VerificationOutcome.VERIFIED,
        VerificationOutcome.PARTIALLY_VERIFIED,
        VerificationOutcome.FAILED,
    }:
        _require_stable_snapshot(response)
    return finding


@_stage_parser(plain_text_field="final_message")
def parse_finalisation(data: Mapping[str, Any]) -> FinalisationOutcome:
    """Accept Finalisation's natural-language response.

    The former JSON envelope remains readable during a rolling upgrade, but
    Finalisation no longer has to produce or classify an execution result.
    """

    execution_result_present = "execution_result" in data
    raw_execution = data.get("execution_result")
    execution: ExecutionOutcome | None
    if raw_execution is None:
        execution = None
    elif isinstance(raw_execution, Mapping):
        execution = _parse_execution_data(raw_execution)
    else:
        raise StructuredOutputError(
            "Finalisation execution_result must be an object or null"
        )

    final_message = _text_value(
        data.get("final_message"),
        data.get("report"),
        data.get("message"),
        data.get("response"),
    )
    if not final_message:
        raise StructuredOutputError(
            "Finalisation requires a Persona-rendered final_message"
        )
    return FinalisationOutcome(
        execution_result=execution,
        final_message=final_message,
        execution_result_present=execution_result_present,
    )
