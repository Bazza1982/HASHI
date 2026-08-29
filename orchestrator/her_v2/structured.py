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
    FinalisationOutcome,
    ReplanningOutcome,
    ReviewFinding,
    ReviewOutcome,
    StageResponse,
    StrategyDecision,
    TriageClassification,
    TriageDecision,
)

ParsedT = TypeVar("ParsedT")
MappingParser = Callable[[Mapping[str, Any]], ParsedT]

_MAX_SOURCE_CHARS = 200_000
_MAX_JSON_CANDIDATES = 8
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
    if isinstance(value, StrategyDecision):
        value = {
            "classification": value.classification.value,
            "real_goal": value.real_goal,
            "selected_strategy_cards": value.selected_strategy_cards,
            "relevant_habits": value.relevant_habits,
            "execution_brief": dict(value.execution_brief),
            "clarification": value.clarification,
        }
    elif isinstance(value, TriageDecision):
        value = {
            "classification": value.classification.value,
            "real_goal": value.real_goal,
            "relevant_habits": value.relevant_habits,
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


@_stage_parser(plain_text_field="message", allow_audio_only=True)
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


@_stage_parser()
def parse_triage(data: Mapping[str, Any]) -> TriageDecision:
    if "real_goal" not in data or "relevant_habits" not in data:
        raise StructuredOutputError(
            "Triage schema v2 requires real_goal and relevant_habits"
        )
    classification = _enum_value(
        TriageClassification,
        data.get("classification"),
    )
    real_goal = _text_value(data.get("real_goal"))
    raw_relevant_habits = data.get("relevant_habits")
    if not isinstance(raw_relevant_habits, list) or any(
        not isinstance(item, str) or not item.strip()
        for item in raw_relevant_habits
    ):
        raise StructuredOutputError(
            "Triage relevant_habits must be a list of non-empty strings"
        )
    relevant_habits = tuple(item.strip() for item in raw_relevant_habits)
    if len(set(relevant_habits)) != len(relevant_habits):
        raise StructuredOutputError("Triage relevant_habits must not contain duplicates")
    clarification = _text_value(data.get("clarification"))
    if (
        classification is TriageClassification.CONFIRMATION_REQUIRED
        and not clarification
    ):
        raise StructuredOutputError(
            "CONFIRMATION_REQUIRED requires a clarification question"
        )
    if classification is TriageClassification.CONFIRMATION_REQUIRED:
        if relevant_habits:
            raise StructuredOutputError(
                "CONFIRMATION_REQUIRED must not speculate about relevant Habits"
            )
    elif not real_goal:
        raise StructuredOutputError(
            "Triage schema v2 requires a non-empty real_goal for resolved requests"
        )
    return TriageDecision(classification, real_goal, relevant_habits, clarification)


@_stage_parser()
def parse_strategy(data: Mapping[str, Any]) -> StrategyDecision:
    """Validate the minimal Strategy schema-v3 envelope."""

    required = {
        "classification",
        "real_goal",
        "selected_strategy_cards",
        "relevant_habits",
        "execution_brief",
    }
    missing = sorted(required - set(data))
    if missing:
        raise StructuredOutputError(
            "Strategy schema v3 is missing required fields: " + ", ".join(missing)
        )

    classification = _enum_value(
        TriageClassification,
        data.get("classification"),
    )
    real_goal = _text_value(data.get("real_goal"))

    def string_list(field: str) -> tuple[str, ...]:
        raw = data.get(field)
        if not isinstance(raw, list) or any(
            not isinstance(item, str) or not item.strip() for item in raw
        ):
            raise StructuredOutputError(
                f"Strategy {field} must be a list of non-empty strings"
            )
        values = tuple(item.strip() for item in raw)
        if len(set(values)) != len(values):
            raise StructuredOutputError(
                f"Strategy {field} must not contain duplicates"
            )
        return values

    selected_cards = string_list("selected_strategy_cards")
    relevant_habits = string_list("relevant_habits")

    raw_brief = data.get("execution_brief")
    if not isinstance(raw_brief, Mapping):
        raise StructuredOutputError("Strategy execution_brief must be an object")
    brief_fields = {
        "strategy",
        "stages",
        "dependencies",
        "verification",
        "success_criteria",
        "replan_conditions",
    }
    if set(raw_brief) != brief_fields:
        missing_brief = sorted(brief_fields - set(raw_brief))
        extra_brief = sorted(set(raw_brief) - brief_fields)
        details = []
        if missing_brief:
            details.append("missing " + ", ".join(missing_brief))
        if extra_brief:
            details.append("unexpected " + ", ".join(extra_brief))
        raise StructuredOutputError(
            "Strategy execution_brief has invalid fields: " + "; ".join(details)
        )
    strategy = _text_value(raw_brief.get("strategy"))
    execution_brief: dict[str, Any] = {"strategy": strategy}
    for field in (
        "stages",
        "dependencies",
        "verification",
        "success_criteria",
        "replan_conditions",
    ):
        raw_values = raw_brief.get(field)
        if not isinstance(raw_values, list) or any(
            not isinstance(item, str) or not item.strip() for item in raw_values
        ):
            raise StructuredOutputError(
                f"Strategy execution_brief.{field} must be a list of non-empty strings"
            )
        execution_brief[field] = [item.strip() for item in raw_values]

    clarification = _text_value(data.get("clarification"))
    if classification is TriageClassification.CONFIRMATION_REQUIRED:
        if not clarification:
            raise StructuredOutputError(
                "CONFIRMATION_REQUIRED requires a clarification question"
            )
        if selected_cards or relevant_habits:
            raise StructuredOutputError(
                "CONFIRMATION_REQUIRED must not speculate about Cards or Habits"
            )
        if strategy or any(execution_brief[field] for field in brief_fields - {"strategy"}):
            raise StructuredOutputError(
                "CONFIRMATION_REQUIRED requires an empty execution_brief"
            )
    else:
        if not real_goal:
            raise StructuredOutputError(
                "Strategy schema v3 requires a non-empty real_goal for resolved requests"
            )
        if classification in {
            TriageClassification.SIMPLE_TASK,
            TriageClassification.COMPLEX_TASK,
            TriageClassification.HIGH_VOLUME_TASK,
        } and not strategy:
            raise StructuredOutputError(
                "work classifications require execution_brief.strategy"
            )

    return StrategyDecision(
        classification=classification,
        real_goal=real_goal,
        selected_strategy_cards=selected_cards,
        relevant_habits=relevant_habits,
        execution_brief=execution_brief,
        clarification=clarification,
    )


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
    sub_agents, parallel_groups = _validated_delegation_plan_fields(
        data,
        stage_label="Planning",
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

    sub_agents, parallel_groups = _validated_delegation_plan_fields(
        data,
        stage_label="Replanning",
    )
    plan: dict[str, Any] = {
        "plan": steps,
        "success_criteria": success_criteria,
        "parallel_groups": parallel_groups,
        "sub_agents": sub_agents,
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
    if "status" in data:
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


def validate_review_response(response: StageResponse) -> ReviewFinding:
    """Validate the three-state Review decision contract.

    Review tools remain independently recorded by the Tool Gateway, but the
    reviewer may also judge work that cannot be objectively tool-verified.
    Therefore a valid decision does not require receipt citations.
    """

    finding = parse_review(response)
    assert isinstance(finding, ReviewFinding)
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
