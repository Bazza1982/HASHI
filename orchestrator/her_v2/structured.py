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
    ReviewFinding,
    ReviewOutcome,
    StageResponse,
    TriageClassification,
    TriageDecision,
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
        for index, data in enumerate(extract_json_objects(response.reasoning_trace or "")):
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
    if not str(response.text or "").strip() and not str(
        response.reasoning_trace or ""
    ).strip() and not response.data:
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
    if not isinstance(plan, (list, str)) or not plan:
        raise StructuredOutputError("Planning requires a non-empty plan")
    if "plan" in data:
        return data
    normalized = dict(data)
    normalized["plan"] = plan
    return normalized


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
    work_performed = _string_items(
        data.get("work_performed"), field="work_performed"
    )
    verification = _string_items(data.get("verification"), field="verification")
    remaining_work = _string_items(
        data.get("remaining_work"), field="remaining_work"
    )
    clarification = _text_value(data.get("clarification"), data.get("question"))
    if (
        disposition is ExecutionDisposition.USER_INPUT_REQUIRED
        and not clarification
    ):
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
    outcome = _enum_value(
        ReviewOutcome,
        data.get("outcome") or data.get("status"),
        aliases={"CONDITIONAL": "CONDITIONAL_PASS", "PASSED": "PASS"},
    )
    summary = _text_value(data.get("summary"), data.get("reason"))
    if not summary:
        raise StructuredOutputError("Review requires a summary")
    findings = _string_items(data.get("findings"), field="findings")
    return ReviewFinding(outcome, summary, findings)


@_stage_parser()
def parse_finalisation(data: Mapping[str, Any]) -> FinalisationOutcome:
    """Parse the one-call Plan B ledger result and Persona-rendered message.

    A report-only object remains readable during a rolling upgrade, but it has
    no authority to synthesize an Execution result when the original Execution
    JSON was invalid.
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
    if not execution_result_present and "report" not in data:
        raise StructuredOutputError(
            "Finalisation requires execution_result (object or null)"
        )
    return FinalisationOutcome(
        execution_result=execution,
        final_message=final_message,
        execution_result_present=execution_result_present,
    )
