"""Deterministic compatibility membrane for model-authored stage envelopes."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, is_dataclass, replace
from functools import wraps
from typing import Any, Callable, Mapping, TypeVar

from .interfaces import StructuredOutputError
from .models import (
    ExecutionDisposition,
    ExecutionOutcome,
    ReplanningOutcome,
    StageResponse,
)

ParsedT = TypeVar("ParsedT")
MappingParser = Callable[[Mapping[str, Any]], ParsedT]

_JSON_OBJECT_FENCE = re.compile(
    r"```(?:json)?\s*(\{.*\})\s*```",
    flags=re.IGNORECASE | re.DOTALL,
)
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
    if isinstance(value, ReplanningOutcome):
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
    if not isinstance(value, Mapping):
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
    items: list[Mapping[str, Any]] = []
    seen: set[str] = set()

    fenced = _JSON_OBJECT_FENCE.fullmatch(value)
    if fenced is not None:
        value = fenced.group(1).strip()

    # Accept only a complete object, including one deterministic layer of
    # JSON-string encoding used by several OpenAI-compatible gateways.  Never
    # scan prose, logs, code examples, or quoted user content for an embedded
    # object: those objects are content, not stage-control authority.
    try:
        parsed: Any = json.loads(value, strict=strict)
        if isinstance(parsed, str):
            parsed = json.loads(parsed, strict=strict)
        _append_mapping(items, seen, parsed)
    except (json.JSONDecodeError, TypeError):
        pass
    return tuple(items)


def extract_json_objects(text: str) -> tuple[Mapping[str, Any], ...]:
    """Extract strict-JSON object candidates without guessing meaning."""

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
    strict_keys = {_mapping_key(data) for data in text_candidates}
    repaired_text_candidates = tuple(
        data
        for data in _extract_json_objects(response.text, strict=False)
        if _mapping_key(data) not in strict_keys
    )
    for index, data in enumerate(repaired_text_candidates):
        add(f"provider_json_control_char_repair:{index + 1}", data)
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

    # Immediate Response, primary Execution, and Finalisation are presentation
    # stages.  Their visible provider text is the result itself, including any
    # JSON, Markdown, code, logs, or examples the user requested.  Structured
    # provider data remains a fallback only when no visible text exists, and
    # hidden reasoning is never promoted into presentation authority.
    if plain_text_field:
        text = str(response.text or "").strip()
        if text:
            candidate = {plain_text_field: text}
            parsed = parser(candidate)
            return StructuredResolution(
                response=replace(response, data=candidate),
                parsed=parsed,
                source="provider_plain_text",
            )

        if bool(getattr(validator, "_allow_audio_only", False)) and any(
            isinstance(part, Mapping)
            and part.get("type") == "audio"
            and str(part.get("asset_id") or "").strip()
            for part in response.content
        ):
            return StructuredResolution(
                response=response,
                parsed="",
                source="provider_audio",
            )

        primary = (
            (_Candidate("provider_data", response.data),)
            if response.data
            else ()
        )
        resolved, rejected = _resolve_group(response, parser, primary)
        if resolved is not None:
            return resolved
        if rejected:
            detail = "; ".join(
                f"{source}: {error}" for source, error in rejected
            )
            raise StructuredOutputError(
                f"provider response has no compatible structured result ({detail})"
            )
        raise StructuredOutputError("provider returned an empty structured response")

    primary = _candidate_group(
        response,
        include_reasoning=False,
    )
    resolved, primary_rejected = _resolve_group(response, parser, primary)
    if resolved is not None:
        return resolved

    reasoning = _candidate_group(
        response,
        include_reasoning=True,
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


def _stage_parser(
    *, plain_text_field: str | None = None, allow_audio_only: bool = False
):
    def decorate(parser: MappingParser):
        @wraps(parser)
        def validate(response: StageResponse):
            return resolve_stage_response(response, validate).parsed

        setattr(validate, "_mapping_parser", parser)
        setattr(validate, "_plain_text_field", plain_text_field)
        setattr(validate, "_allow_audio_only", bool(allow_audio_only))
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


def parse_direct_message(response: StageResponse) -> str:
    """Accept any non-empty Direct result without imposing an output envelope."""

    # A Direct response is user-authored presentation, not a control object.
    # Preserve JSON, Markdown, code, and other requested output formats exactly
    # as text instead of interpreting a JSON-looking answer as stage metadata.
    text = str(response.text or "").strip()
    if not text:
        data = response.data if isinstance(response.data, Mapping) else {}
        text = _text_value(
            data.get("message"),
            data.get("response"),
            data.get("text"),
            data.get("final_message"),
            data.get("result"),
        )
    if not text:
        if any(
            isinstance(part, Mapping)
            and part.get("type") == "audio"
            and str(part.get("asset_id") or "").strip()
            for part in response.content
        ):
            return ""
        raise StructuredOutputError("Direct requires a non-empty final response")
    return text


def _validated_delegation_plan_fields(
    data: Mapping[str, Any],
    *,
    stage_label: str,
) -> tuple[list[Any], list[Any]]:
    """Validate one complete delegation schedule shared by Plan and Replan.

    ``parallel_groups`` is an ordered list of execution waves.  Assignment IDs
    within one wave may run concurrently.  An empty field retains the legacy
    high-volume default of one concurrent wave containing every assignment;
    when supplied it must schedule every assignment exactly once.  Keeping
    this contract deterministic prevents Runtime from inventing dependencies
    or concurrency beyond the planning authority's declared schedule.
    """

    sub_agents = data.get("sub_agents", [])
    if not isinstance(sub_agents, list):
        raise StructuredOutputError(f"{stage_label} sub_agents must be a list")
    seen_ids: set[str] = set()
    for index, raw in enumerate(sub_agents, start=1):
        if not isinstance(raw, Mapping):
            raise StructuredOutputError(
                f"{stage_label} sub-agent assignment {index} must be an object"
            )
        assignment_id = _text_value(raw.get("id"))
        task = _text_value(raw.get("task"))
        profile = _text_value(raw.get("profile"))
        tools = raw.get("tools")
        attachment_ids = raw.get("attachment_ids")
        allow_side_effects = raw.get("allow_side_effects")
        if not assignment_id or assignment_id in seen_ids:
            raise StructuredOutputError(
                f"{stage_label} sub-agent assignments require unique non-empty IDs"
            )
        seen_ids.add(assignment_id)
        if not task or not profile:
            raise StructuredOutputError(
                f"{stage_label} sub-agent assignments require task and profile"
            )
        if not isinstance(tools, list) or any(
            not isinstance(item, str) or not item.strip() for item in tools
        ):
            raise StructuredOutputError(
                f"{stage_label} sub-agent assignment tools must be a list of strings"
            )
        if attachment_ids is not None and (
            not isinstance(attachment_ids, list)
            or any(
                not isinstance(item, str) or not item.strip() for item in attachment_ids
            )
        ):
            raise StructuredOutputError(
                f"{stage_label} sub-agent attachment_ids must be a list of strings"
            )
        if not isinstance(allow_side_effects, bool):
            raise StructuredOutputError(
                f"{stage_label} sub-agent allow_side_effects must be a boolean"
            )

    parallel_groups = data.get("parallel_groups", [])
    if not isinstance(parallel_groups, list):
        raise StructuredOutputError(f"{stage_label} parallel_groups must be a list")
    scheduled_ids: set[str] = set()
    for index, raw_group in enumerate(parallel_groups, start=1):
        if (
            not isinstance(raw_group, list)
            or not raw_group
            or any(not isinstance(item, str) or not item.strip() for item in raw_group)
        ):
            raise StructuredOutputError(
                f"{stage_label} parallel group {index} must be a non-empty list "
                "of assignment IDs"
            )
        group_ids = [item.strip() for item in raw_group]
        if len(group_ids) != len(set(group_ids)):
            raise StructuredOutputError(
                f"{stage_label} parallel group {index} contains duplicate assignment IDs"
            )
        unknown = sorted(set(group_ids) - seen_ids)
        if unknown:
            raise StructuredOutputError(
                f"{stage_label} parallel group {index} references unknown sub-agent "
                f"assignment IDs: {', '.join(unknown)}"
            )
        repeated = sorted(set(group_ids) & scheduled_ids)
        if repeated:
            raise StructuredOutputError(
                f"{stage_label} sub-agent assignments may appear in only one parallel "
                f"group: {', '.join(repeated)}"
            )
        scheduled_ids.update(group_ids)
    if parallel_groups:
        omitted = sorted(seen_ids - scheduled_ids)
        if omitted:
            raise StructuredOutputError(
                f"{stage_label} parallel_groups must schedule every sub-agent "
                f"assignment when supplied: {', '.join(omitted)}"
            )
    return sub_agents, parallel_groups


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


# HASHI-Fingerprint: hfp_aa40eb91d83c4d479ca3d80af01b785f
