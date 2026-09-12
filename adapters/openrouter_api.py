from __future__ import annotations
import asyncio
import base64
import binascii
import hashlib
import io
import json
import logging
import os
import threading
import time
import wave
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import count
from pathlib import Path
from typing import Any, Callable, Mapping, Optional
from uuid import uuid4

import httpx

from adapters.base import BaseBackend, BackendCapabilities, BackendResponse
from adapters.stream_events import (
    DELIVERY_USER_COMMENTARY,
    KIND_FILE_EDIT,
    KIND_FILE_READ,
    KIND_SHELL_EXEC,
    KIND_TEXT_DELTA,
    KIND_THINKING,
    KIND_TOOL_END,
    KIND_TOOL_START,
    StreamCallback,
    StreamEvent,
)
from orchestrator.enterprise.policy import evaluate_governance_policy
from orchestrator.audio_assets import (
    AudioAssetStore,
    DEFAULT_RETENTION_SECONDS,
    asset_root_from_global_config,
    normalize_audio_format,
    validate_audio_signature,
)
from orchestrator.pcm import load_pcm_document
from orchestrator.multimodal_contract import (
    InputCapability,
    MultimodalContractError,
    attachment_manifest,
    canonical_request_content,
    materialize_openai_user_content,
    media_failure_code,
    native_attachment_reference_aliases,
    normalize_request_content,
    request_content_has_media,
    request_content_is_voice_origin,
    routing_decisions_payload,
    validate_authorized_media_references,
)


INVALID_TOOL_CALL_REPAIR_LIMIT = 3
_PROVIDER_FORENSIC_WRITE_LOCK = threading.Lock()


class ProviderProtocolForensicError(RuntimeError):
    """A mandatory private Provider-protocol record could not be persisted."""


HASHI_COMPACTION_CAPABILITIES = {
    "prompt_isolation": True,
    "tool_disablement": True,
    # OpenRouter aggregates heterogeneous models; an exact Agent grant must
    # opt the selected model into semantic compaction.
    "semantic_reasoning": False,
    "local_or_slow": False,
}
HASHI_MODEL_CAPACITY_PROFILES: dict[str, dict[str, Any]] = {}

_STABLE_CONTEXT_CAPACITY_CODES = frozenset(
    {
        "context_length_exceeded",
        "context_window_exceeded",
        "maximum_context_length_exceeded",
        "prompt_too_long",
    }
)

_STABLE_MODALITY_UNSUPPORTED_CODES = frozenset(
    {
        "modality_unsupported",
        "provider_modality_unsupported",
        "unsupported_modality",
        "unsupported_media",
        "unsupported_media_type",
        "image_input_not_supported",
    }
)

_REASONING_DISABLED_VALUES = frozenset({"off", "none", "false", "0", "disabled"})
_REASONING_EFFORT_VALUES = frozenset(
    {"minimal", "low", "medium", "high", "xhigh", "max"}
)
_MEDIA_FALLBACK_TOOL_NAMES = frozenset({"media_read", "vision_inspect"})


class ProviderCallObserverError(RuntimeError):
    """A durable per-request observer failed after a real Provider call.

    Adapters must let this escape unchanged instead of converting it into a
    Provider failure response or retrying the already completed request.
    """


ProviderCallObserver = Callable[[Mapping[str, Any]], None]


def _argument_string_values(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, Mapping):
        values: set[str] = set()
        for item in value.values():
            values.update(_argument_string_values(item))
        return values
    if isinstance(value, (list, tuple, set, frozenset)):
        values = set()
        for item in value:
            values.update(_argument_string_values(item))
        return values
    return set()


def _references_native_attachment(
    arguments: Mapping[str, Any],
    *,
    attachment_ids: set[str],
    local_refs: set[str],
) -> bool:
    for raw in _argument_string_values(arguments):
        value = str(raw or "").strip()
        if (
            value in local_refs
            or Path(value).name in local_refs
            or value in attachment_ids
        ):
            return True
        try:
            resolved_value = str(
                Path(value).expanduser().resolve(strict=False)
            )
        except (OSError, RuntimeError, ValueError):
            resolved_value = ""
        if resolved_value and resolved_value in local_refs:
            return True
        if any(value.endswith(f":{attachment_id}") for attachment_id in attachment_ids):
            return True
    return False


def _stable_provider_error_code(response: httpx.Response | None) -> str:
    """Return only provider-owned stable codes; never infer capacity from HTTP 400."""

    if response is None:
        return ""
    try:
        payload = response.json()
    except Exception:
        return ""
    candidates: list[Any] = []
    if isinstance(payload, Mapping):
        error = payload.get("error")
        if isinstance(error, Mapping):
            candidates.extend((error.get("code"), error.get("type")))
            metadata = error.get("metadata")
            if isinstance(metadata, Mapping):
                candidates.extend((metadata.get("code"), metadata.get("type")))
        candidates.extend((payload.get("code"), payload.get("type")))
    for candidate in candidates:
        normalized = str(candidate or "").strip().lower()
        if normalized in _STABLE_CONTEXT_CAPACITY_CODES:
            return "CONTEXT_CAPACITY_REJECTED"
        if normalized in _STABLE_MODALITY_UNSUPPORTED_CODES:
            return "PROVIDER_MODALITY_UNSUPPORTED"
    return ""


def _stream_error_exception(
    payload: Mapping[str, Any],
    *,
    request: httpx.Request,
    provider_activity_observed: bool,
) -> httpx.HTTPStatusError | None:
    """Convert an OpenAI-compatible SSE error event into a typed HTTP error.

    Streaming endpoints have already committed HTTP 200 before a backend can
    fail.  Preserve the event payload on a synthetic response so the normal
    stable provider-code handling remains identical to non-streaming calls.
    """

    raw_error = payload.get("error")
    if raw_error in (None, ""):
        return None
    error = raw_error if isinstance(raw_error, Mapping) else {}
    raw_status = error.get("status", payload.get("status", 502))
    try:
        status = int(raw_status)
    except (TypeError, ValueError):
        status = 502
    if status < 400 or status > 599:
        status = 502
    message = str(error.get("message") or raw_error or "provider stream error")
    response = httpx.Response(status, request=request, json=dict(payload))
    exception = httpx.HTTPStatusError(
        message,
        request=request,
        response=response,
    )
    metadata = error.get("metadata")
    reported_activity = (
        bool(metadata.get("provider_activity"))
        if isinstance(metadata, Mapping)
        else False
    )
    setattr(
        exception,
        "provider_activity_observed",
        bool(provider_activity_observed or reported_activity),
    )
    return exception


_UNSET_FINISH_REASON = object()


@dataclass
class _APIResult:
    """Internal intermediate result from a single API call."""
    text: str
    tool_calls: Optional[list]   # None = no tool calls, just text
    finish_reason: str | None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    thinking_tokens: int = 0
    cost_usd: float | None = None
    reasoning_content: str = ""
    structured_data: dict[str, Any] | None = None
    audio_bytes: bytes = b""
    audio_transcript: str = ""
    # Provider-reported prompt-cache accounting.  ``None`` means the
    # provider did not report the field; zero remains a real observation.
    prompt_cache_hit_tokens: int | None = None
    prompt_cache_miss_tokens: int | None = None
    # Preserve Provider protocol truth separately from HASHI's normalized
    # decision.  In particular, a missing or explicit-null finish_reason must
    # never be rewritten as a Provider-owned ``stop``.
    raw_finish_reason: Any = field(default=_UNSET_FINISH_REASON, repr=False)
    finish_reason_present: bool | None = None
    finish_reason_source: str = ""
    provider_response_id: str = ""
    transport_request_id: str = ""
    transport_complete: bool = True
    transport_state: str = "complete_response"
    stream_done: bool | None = None
    stream_eof: bool = False
    stream_truncated: bool = False
    reasoning_state: str = ""
    # Complete request/response evidence is retained only long enough to write
    # a private local forensic record when the Provider tool protocol is bad.
    # It must never be copied into ordinary audit, stream, or user metadata.
    wire_evidence: dict[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        # Test doubles and older compatible adapters construct _APIResult with
        # only the original three positional fields.  Infer provider truth for
        # those callers while allowing parsers to state missing/null exactly.
        if self.raw_finish_reason is _UNSET_FINISH_REASON:
            self.raw_finish_reason = self.finish_reason
        if self.finish_reason_present is None:
            self.finish_reason_present = self.finish_reason is not None
        if not self.finish_reason_source:
            if not self.finish_reason_present:
                self.finish_reason_source = "missing"
            elif self.raw_finish_reason is None:
                self.finish_reason_source = "provider_null"
            else:
                self.finish_reason_source = "provider"
        if self.finish_reason is not None:
            normalized = str(self.finish_reason).strip()
            self.finish_reason = normalized or None
        if not self.reasoning_state:
            self.reasoning_state = (
                "available" if str(self.reasoning_content or "") else "unavailable"
            )


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _stop_parameter_summary(value: Any) -> dict[str, Any]:
    """Describe request-side stop values without logging prompt fragments."""

    if value is None:
        return {"configured": False, "count": 0, "values": []}
    values = list(value) if isinstance(value, (list, tuple)) else [value]
    summaries = []
    for item in values:
        encoded = str(item).encode("utf-8")
        summaries.append(
            {
                "length": len(str(item)),
                "sha256": hashlib.sha256(encoded).hexdigest(),
            }
        )
    return {"configured": True, "count": len(values), "values": summaries}


def _effective_protocol_parameters(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return only non-secret parameters that influence wire semantics."""

    result: dict[str, Any] = {
        "stream": bool(payload.get("stream", False)),
        "tools_available": bool(payload.get("tools")),
        "stop": _stop_parameter_summary(payload.get("stop")),
    }
    for key in (
        "tool_choice",
        "parallel_tool_calls",
        "max_tokens",
        "max_completion_tokens",
        "temperature",
        "top_p",
        "modalities",
        "reasoning_effort",
    ):
        if key in payload:
            value = payload.get(key)
            if key == "tool_choice" and isinstance(value, Mapping):
                function = value.get("function")
                result[key] = {
                    "type": str(value.get("type") or ""),
                    "function_name": (
                        str(function.get("name") or "")
                        if isinstance(function, Mapping)
                        else ""
                    ),
                }
            else:
                result[key] = value
    for key in ("reasoning", "thinking", "response_format", "stream_options"):
        value = payload.get(key)
        if isinstance(value, Mapping):
            # These protocol controls contain no prompt/tool bodies.  Keep
            # their scalar values but exclude any unexpected nested content.
            result[key] = {
                str(name): item
                for name, item in value.items()
                if isinstance(item, (str, int, float, bool, type(None)))
            }
    return result


def _tool_call_protocol_summary(
    tool_calls: Any,
) -> tuple[list[dict[str, Any]], bool]:
    """Validate complete structured tool requests before any side effect."""

    if tool_calls in (None, []):
        return [], True
    if not isinstance(tool_calls, list):
        return [
            {
                "id": "",
                "name": "",
                "complete": False,
                "arguments_state": "invalid_tool_call_container",
            }
        ], False
    summaries: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    all_complete = True
    for call in tool_calls:
        call_id = ""
        name = ""
        arguments_state = "missing"
        complete = False
        if isinstance(call, Mapping):
            call_id = str(call.get("id") or "").strip()
            function = call.get("function")
            if isinstance(function, Mapping):
                name = str(function.get("name") or "").strip()
                raw_arguments = function.get("arguments")
                if isinstance(raw_arguments, str):
                    try:
                        decoded = json.loads(raw_arguments or "{}")
                    except json.JSONDecodeError:
                        arguments_state = "invalid_json"
                    else:
                        arguments_state = (
                            "valid_object"
                            if isinstance(decoded, Mapping)
                            else "non_object_json"
                        )
                elif isinstance(raw_arguments, Mapping):
                    arguments_state = "valid_object"
                elif raw_arguments is None:
                    arguments_state = "missing"
                else:
                    arguments_state = "invalid_type"
                complete = bool(
                    call_id
                    and name
                    and arguments_state == "valid_object"
                    and str(call.get("type") or "function") == "function"
                    and call_id not in seen_ids
                )
        if call_id:
            if call_id in seen_ids:
                arguments_state = "duplicate_call_id"
                complete = False
            seen_ids.add(call_id)
        summaries.append(
            {
                "id": call_id,
                "name": name,
                "complete": complete,
                "arguments_state": arguments_state,
            }
        )
        all_complete = all_complete and complete
    return summaries, all_complete


def _tool_call_forensic_details(
    tool_calls: Any,
    *,
    argument_fragments: list[Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return untruncated parser and assembly evidence for every tool call.

    This structure is private forensic material.  Callers must never attach it
    to normal BackendResponse metadata or user-visible errors.
    """

    calls = tool_calls if isinstance(tool_calls, list) else [tool_calls]
    fragments = [
        dict(item)
        for item in (argument_fragments or [])
        if isinstance(item, Mapping)
    ]
    details: list[dict[str, Any]] = []
    for position, call in enumerate(calls):
        call_mapping = dict(call) if isinstance(call, Mapping) else {}
        function = call_mapping.get("function")
        function_mapping = dict(function) if isinstance(function, Mapping) else {}
        index = call_mapping.get("index", position)
        raw_arguments = function_mapping.get("arguments")
        parser_input = (
            raw_arguments
            if isinstance(raw_arguments, str)
            else (
                json.dumps(raw_arguments, ensure_ascii=False, separators=(",", ":"))
                if raw_arguments is not None
                else ""
            )
        )
        matching_fragments = [
            item
            for item in fragments
            if item.get("index", 0) == index
        ]
        matching_fragments.sort(key=lambda item: int(item.get("arrival") or 0))
        provider_arguments = "".join(
            str(item.get("arguments_fragment") or "")
            for item in matching_fragments
        )
        has_provider_fragments = bool(matching_fragments)
        assembly_matches = (
            provider_arguments == parser_input if has_provider_fragments else True
        )

        parser_error: dict[str, Any] | None = None
        parsed_type = ""
        arguments_state = "missing"
        if isinstance(raw_arguments, str):
            try:
                parsed = json.loads(parser_input or "{}")
            except json.JSONDecodeError as exc:
                arguments_state = "invalid_json"
                parser_error = {
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "reason": exc.msg,
                    "character_position": int(exc.pos),
                    "byte_position": len(parser_input[: exc.pos].encode("utf-8")),
                    "line": int(exc.lineno),
                    "column": int(exc.colno),
                }
            else:
                parsed_type = type(parsed).__name__
                arguments_state = (
                    "valid_object" if isinstance(parsed, Mapping) else "non_object_json"
                )
                if not isinstance(parsed, Mapping):
                    parser_error = {
                        "type": "NonObjectJSON",
                        "message": (
                            "tool arguments decoded successfully but did not produce "
                            "a JSON object"
                        ),
                        "reason": "decoded_value_is_not_object",
                        "character_position": None,
                        "byte_position": None,
                        "line": None,
                        "column": None,
                    }
        elif isinstance(raw_arguments, Mapping):
            parsed_type = type(raw_arguments).__name__
            arguments_state = "valid_object"
        elif raw_arguments is None:
            parser_error = {
                "type": "MissingToolArguments",
                "message": "tool arguments are missing",
                "reason": "missing",
                "character_position": None,
                "byte_position": None,
                "line": None,
                "column": None,
            }
        else:
            arguments_state = "invalid_type"
            parser_error = {
                "type": "InvalidToolArgumentsType",
                "message": f"tool arguments have unsupported type {type(raw_arguments).__name__}",
                "reason": "invalid_type",
                "character_position": None,
                "byte_position": None,
                "line": None,
                "column": None,
            }

        if not assembly_matches:
            attribution = "hashi_assembly_mismatch"
        elif arguments_state == "invalid_json":
            attribution = "provider_invalid_json"
        elif arguments_state == "valid_object":
            attribution = "valid"
        else:
            attribution = "provider_invalid_tool_arguments"
        details.append(
            {
                "position": position,
                "index": index,
                "id": str(call_mapping.get("id") or ""),
                "type": str(call_mapping.get("type") or "function"),
                "name": str(function_mapping.get("name") or ""),
                "argument_fragments": matching_fragments,
                "provider_arguments_from_fragments": provider_arguments,
                "parser_input": parser_input,
                "parser_input_type": type(raw_arguments).__name__,
                "parser_input_characters": len(parser_input),
                "parser_input_bytes": len(parser_input.encode("utf-8")),
                "parsed_type": parsed_type,
                "arguments_state": arguments_state,
                "parser_error": parser_error,
                "assembly_matches_provider_fragments": assembly_matches,
                "attribution": attribution,
            }
        )
    return details


def _wire_body_evidence(raw: bytes) -> dict[str, Any]:
    """Preserve exact wire bytes without truncation or redaction."""

    payload = bytes(raw)
    try:
        body = payload.decode("utf-8")
        encoding = "utf-8"
    except UnicodeDecodeError:
        body = base64.b64encode(payload).decode("ascii")
        encoding = "base64"
    return {
        "body": body,
        "encoding": encoding,
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _request_wire_evidence(
    payload: Mapping[str, Any],
    request: Any = None,
) -> dict[str, Any]:
    try:
        raw = bytes(request.content) if request is not None else b""
    except (AttributeError, RuntimeError, TypeError, ValueError):
        raw = b""
    if not raw:
        raw = json.dumps(
            dict(payload),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    return _wire_body_evidence(raw)


def _response_wire_evidence(response: Any) -> dict[str, Any]:
    try:
        raw = bytes(response.content)
    except (AttributeError, RuntimeError, TypeError, ValueError):
        raw = b""
    return _wire_body_evidence(raw)


def _invalid_tool_repair_prompt(
    tool_details: list[Mapping[str, Any]],
    *,
    repair_number: int,
    completed_tool_calls: list[Mapping[str, Any]],
) -> str:
    issues: list[str] = []
    for detail in tool_details:
        if str(detail.get("arguments_state") or "") == "valid_object":
            continue
        parser_error = detail.get("parser_error")
        error = dict(parser_error) if isinstance(parser_error, Mapping) else {}
        issues.append(
            "tool index={index}, id={id}, name={name}: {kind} at character "
            "{position}: {reason}; exact arguments={arguments}".format(
                index=detail.get("index"),
                id=detail.get("id") or "<missing>",
                name=detail.get("name") or "<missing>",
                kind=error.get("type") or detail.get("arguments_state") or "invalid",
                position=(
                    error.get("character_position")
                    if error.get("character_position") is not None
                    else "n/a"
                ),
                reason=error.get("reason") or error.get("message") or "invalid arguments",
                arguments=json.dumps(
                    detail.get("parser_input"), ensure_ascii=False
                ),
            )
        )
    completed = ", ".join(
        f"{item.get('id') or '<missing>'}:{item.get('name') or '<missing>'}"
        for item in completed_tool_calls
    ) or "none"
    return (
        f"HASHI tool-call repair request {repair_number}/{INVALID_TOOL_CALL_REPAIR_LIMIT}. "
        "The preceding Provider response was rejected before any tool in that "
        "batch executed. Correct the same intended tool call(s) and emit a complete "
        "tool-call batch whose arguments are valid JSON objects. Do not restart the "
        "task and do not repeat previously completed tool calls. "
        f"Previously completed tool calls: {completed}. "
        f"Exact parse issue(s): {' | '.join(issues)}"
    )


def _invalid_tool_user_error(
    tool_details: list[Mapping[str, Any]],
    *,
    provider_request_id: str,
    forensic_path: Path,
) -> str:
    invalid = next(
        (
            detail
            for detail in tool_details
            if str(detail.get("arguments_state") or "") != "valid_object"
        ),
        {},
    )
    parser_error = invalid.get("parser_error")
    error = dict(parser_error) if isinstance(parser_error, Mapping) else {}
    name = str(invalid.get("name") or "<missing>")
    reason = str(error.get("reason") or error.get("message") or "invalid JSON")
    position = error.get("character_position")
    position_text = f" at character {position}" if position is not None else ""
    return (
        "PROVIDER_INVALID_TOOL_CALLS: Provider tool "
        f"{name} still had invalid JSON after 3 repair attempts "
        f"({reason}{position_text}). No malformed tool was executed. "
        f"Provider request ID: {provider_request_id or 'unavailable'}. "
        f"Complete private local diagnostic: {forensic_path}"
    )


def _provider_response_decision(
    result: _APIResult,
    *,
    tool_registry_available: bool,
) -> dict[str, Any]:
    """Classify one complete Provider response before executing tools."""

    tool_summaries, tools_complete = _tool_call_protocol_summary(result.tool_calls)
    has_tools = bool(tool_summaries)
    raw = result.raw_finish_reason
    raw_text = str(raw).strip() if raw is not None else ""
    normalized = raw_text.casefold()
    if not bool(result.finish_reason_present) or not raw_text:
        normalized = "missing_finish_reason"

    base = {
        "normalized_finish_reason": normalized,
        "finish_reason_source": str(result.finish_reason_source or "missing"),
        "tool_calls": tool_summaries,
        "tool_calls_complete": tools_complete,
        "tool_call_count_received": len(tool_summaries),
        "execute_tools": False,
        "success": False,
        "error_code": "",
        "error_retryable": False,
        "decision": "",
        "decision_reason": "",
    }
    if not result.transport_complete:
        return {
            **base,
            "decision": "reject_incomplete_transport",
            "decision_reason": str(result.transport_state or "transport_incomplete"),
            "error_code": "PROVIDER_INCOMPLETE_STREAM",
            "error_retryable": True,
        }
    if has_tools and not tools_complete:
        return {
            **base,
            "decision": "reject_invalid_tool_calls",
            "decision_reason": "tool_calls_incomplete_or_invalid",
            "error_code": "PROVIDER_INVALID_TOOL_CALLS",
        }
    if normalized in {"stop", "completed"}:
        if has_tools:
            return {
                **base,
                "decision": "protocol_conflict",
                "decision_reason": f"{normalized}_with_tool_calls",
                "error_code": "PROVIDER_FINISH_REASON_CONFLICT",
            }
        return {
            **base,
            "decision": "complete",
            "decision_reason": f"provider_{normalized}",
            "success": True,
        }
    if normalized in {"tool_calls", "function_call"}:
        if not has_tools:
            return {
                **base,
                "decision": "protocol_conflict",
                "decision_reason": "tool_finish_without_tool_calls",
                "error_code": "PROVIDER_FINISH_REASON_CONFLICT",
            }
        if not tool_registry_available:
            return {
                **base,
                "decision": "reject_tools_unavailable",
                "decision_reason": "tool_registry_unavailable",
                "error_code": "PROVIDER_TOOL_EXECUTION_UNAVAILABLE",
            }
        return {
            **base,
            "decision": "execute_tools",
            "decision_reason": "complete_structured_tool_calls",
            "execute_tools": True,
        }
    if normalized == "missing_finish_reason":
        return {
            **base,
            "decision": "reject_missing_finish_reason",
            "decision_reason": "provider_finish_reason_missing_or_null",
            "error_code": "PROVIDER_MISSING_FINISH_REASON",
        }
    if normalized in {"length", "max_tokens"}:
        return {
            **base,
            "decision": "reject_truncated_output",
            "decision_reason": normalized,
            "error_code": "PROVIDER_OUTPUT_TRUNCATED",
        }
    if normalized in {"content_filter", "safety"}:
        return {
            **base,
            "decision": "reject_filtered_output",
            "decision_reason": normalized,
            "error_code": "PROVIDER_OUTPUT_FILTERED",
        }
    if normalized in {"insufficient_system_resource", "resource_exhausted"}:
        return {
            **base,
            "decision": "reject_resource_failure",
            "decision_reason": normalized,
            "error_code": "PROVIDER_CAPACITY_UNAVAILABLE",
            "error_retryable": True,
        }
    return {
        **base,
        "decision": "reject_unknown_finish_reason",
        "decision_reason": normalized or "unknown",
        "error_code": "PROVIDER_UNKNOWN_FINISH_REASON",
    }


def _response_protocol_record(
    result: _APIResult,
    decision: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "raw_finish_reason_present": bool(result.finish_reason_present),
        "raw_finish_reason": result.raw_finish_reason,
        "normalized_finish_reason": decision.get("normalized_finish_reason"),
        "finish_reason_source": decision.get("finish_reason_source"),
        "transport_complete": bool(result.transport_complete),
        "transport_state": str(result.transport_state or ""),
        "stream_done": result.stream_done,
        "stream_eof": bool(result.stream_eof),
        "stream_truncated": bool(result.stream_truncated),
        "provider_response_id": str(result.provider_response_id or ""),
        "transport_request_id": str(result.transport_request_id or ""),
        "text_provided": bool(str(result.text or "")),
        "text_length": len(str(result.text or "")),
        "reasoning_availability": str(result.reasoning_state or "unavailable"),
        "reasoning_length": len(str(result.reasoning_content or "")),
        "structured_data_provided": isinstance(result.structured_data, Mapping),
        "tool_calls": list(decision.get("tool_calls") or []),
        "tool_calls_complete": bool(decision.get("tool_calls_complete", True)),
        "tool_call_count_received": int(
            decision.get("tool_call_count_received") or 0
        ),
        "decision": str(decision.get("decision") or ""),
        "decision_reason": str(decision.get("decision_reason") or ""),
        "decision_success": bool(decision.get("success")),
    }


_PROVIDER_PROTOCOL_ERROR_MESSAGES = {
    "PROVIDER_FINISH_REASON_CONFLICT": (
        "The Provider response contained conflicting finish and tool-call signals."
    ),
    "PROVIDER_MISSING_FINISH_REASON": (
        "The Provider response did not contain a terminal finish reason."
    ),
    "PROVIDER_OUTPUT_TRUNCATED": (
        "The Provider response ended because its output was truncated."
    ),
    "PROVIDER_OUTPUT_FILTERED": (
        "The Provider response was stopped by a content or safety filter."
    ),
    "PROVIDER_CAPACITY_UNAVAILABLE": (
        "The Provider could not complete the response because resources were unavailable."
    ),
    "PROVIDER_INVALID_TOOL_CALLS": (
        "The Provider returned incomplete or invalid structured tool calls."
    ),
    "PROVIDER_TOOL_EXECUTION_UNAVAILABLE": (
        "The Provider requested tools that are unavailable for this request."
    ),
    "PROVIDER_UNKNOWN_FINISH_REASON": (
        "The Provider returned an unsupported finish reason."
    ),
    "PROVIDER_INCOMPLETE_STREAM": (
        "The Provider response stream ended before a complete decision could be made."
    ),
}


def _provider_protocol_error_message(error_code: str) -> str:
    return _PROVIDER_PROTOCOL_ERROR_MESSAGES.get(
        str(error_code or ""),
        "The Provider response could not be accepted safely.",
    )


def _annotate_stream_exception(
    error: BaseException,
    state: Mapping[str, Any],
) -> BaseException:
    snapshot = dict(state)
    snapshot.update(
        {
            "transport_complete": False,
            "transport_state": (
                "cancelled"
                if isinstance(error, asyncio.CancelledError)
                else "stream_interrupted"
            ),
            "stream_done": False,
            "stream_eof": False,
            "stream_truncated": True,
        }
    )
    setattr(error, "hashi_provider_protocol", snapshot)
    return error


async def _iter_provider_stream_lines(
    response: Any,
    state: Mapping[str, Any],
):
    """Attach already observed protocol facts if the wire iterator aborts."""

    try:
        async for line in response.aiter_lines():
            yield line
    except BaseException as exc:
        _annotate_stream_exception(exc, state)
        raise


def _usage_thinking_tokens(usage: Mapping[str, Any]) -> int:
    details = usage.get("completion_tokens_details")
    if isinstance(details, Mapping):
        value = details.get("reasoning_tokens")
        if value is not None:
            return int(value or 0)
    return int(usage.get("thinking_tokens") or 0)


def _usage_cost_usd(usage: Mapping[str, Any]) -> float | None:
    value = usage.get("cost")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_usage_token_count(value: Any) -> int | None:
    """Normalize an optional provider token counter without inventing zero."""

    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _retry_after_seconds(response: httpx.Response | None) -> float | None:
    if response is None:
        return None
    raw = str(response.headers.get("retry-after") or "").strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def _provider_request_id(response: httpx.Response | None) -> str:
    if response is None:
        return ""
    headers = getattr(response, "headers", {}) or {}
    for name in (
        "x-hashi-gateway-request-id",
        "x-request-id",
        "request-id",
        "cf-ray",
        "x-amzn-requestid",
    ):
        value = str(headers.get(name) or "").strip()
        if value:
            return value
    return ""


_SENSITIVE_HTTP_HEADER_NAMES = frozenset(
    {
        "authorization",
        "cookie",
        "proxy-authorization",
        "set-cookie",
        "x-api-key",
    }
)


def _diagnostic_headers(headers: Mapping[str, Any]) -> dict[str, str]:
    return {
        str(name): (
            "[REDACTED]"
            if str(name).strip().casefold() in _SENSITIVE_HTTP_HEADER_NAMES
            else str(value)
        )
        for name, value in headers.items()
    }


def _diagnostic_body(payload: bytes) -> dict[str, Any]:
    raw = bytes(payload)
    try:
        body = raw.decode("utf-8")
        encoding = "utf-8"
    except UnicodeDecodeError:
        body = base64.b64encode(raw).decode("ascii")
        encoding = "base64"
    return {
        "body": body,
        "body_encoding": encoding,
        "body_bytes": len(raw),
        "body_sha256": hashlib.sha256(raw).hexdigest(),
    }


async def _read_http_error_body(response: Any) -> None:
    """Read an HTTP error while its stream is open and retain read state.

    ``httpx`` deliberately raises ``ResponseNotRead`` when a streaming
    response is inspected before ``aread``.  Treating that exception as an
    empty body destroys the Provider's actual diagnostic.  Adapters call this
    helper before ``raise_for_status``; diagnostic projection can then
    distinguish a genuinely empty response from an unread or failed read.
    """

    try:
        status = int(getattr(response, "status_code", 0) or 0)
    except (TypeError, ValueError):
        status = 0
    if status < 400:
        return

    declared_length: int | None = None
    headers = getattr(response, "headers", {}) or {}
    try:
        raw_length = str(headers.get("content-length") or "").strip()
        if raw_length:
            declared_length = max(0, int(raw_length))
    except (AttributeError, TypeError, ValueError):
        declared_length = None
    setattr(response, "hashi_declared_body_bytes", declared_length)

    aread = getattr(response, "aread", None)
    if not callable(aread):
        # Small protocol test doubles may expose only status/json. Real httpx
        # streaming responses always provide aread; preserve uncertainty here
        # instead of fabricating an empty body.
        setattr(response, "hashi_body_read_state", "not_read")
        return
    try:
        body = bytes(await aread())
    except BaseException as exc:
        setattr(response, "hashi_body_read_state", "read_failed")
        setattr(response, "hashi_body_read_error", type(exc).__name__)
        if not hasattr(exc, "response"):
            try:
                setattr(exc, "response", response)
            except (AttributeError, TypeError):
                pass
        raise
    setattr(response, "hashi_observed_body", body)
    if not body:
        state = "empty"
    elif declared_length is not None and len(body) < declared_length:
        state = "partial"
    else:
        state = "complete"
    setattr(response, "hashi_body_read_state", state)


def _provider_http_failure_diagnostics(error: Exception) -> dict[str, Any]:
    """Preserve the complete HTTP request/response evidence for local audit."""

    diagnostics: dict[str, Any] = {}
    try:
        response = getattr(error, "response", None)
    except RuntimeError:
        response = None
    try:
        request = getattr(error, "request", None)
    except RuntimeError:
        request = None
    if not isinstance(request, httpx.Request) and isinstance(response, httpx.Response):
        try:
            request = response.request
        except RuntimeError:
            request = None

    if isinstance(request, httpx.Request):
        try:
            request_body = bytes(request.content)
        except (httpx.RequestNotRead, TypeError, ValueError):
            request_body = None
        request_record = {
            "method": str(request.method),
            "url": str(request.url),
            "headers": _diagnostic_headers(request.headers),
        }
        if request_body is None:
            request_record["body_state"] = "not_read"
        else:
            request_record.update(_diagnostic_body(request_body))
            request_record["body_state"] = (
                "empty" if not request_body else "complete"
            )
        diagnostics["request"] = request_record

    if isinstance(response, httpx.Response):
        response_body = getattr(response, "hashi_observed_body", None)
        if response_body is None:
            try:
                response_body = bytes(response.content)
            except (httpx.ResponseNotRead, TypeError, ValueError):
                response_body = None
        response_record = {
            "status": int(response.status_code),
            "headers": _diagnostic_headers(response.headers),
        }
        body_state = str(
            getattr(response, "hashi_body_read_state", "") or ""
        )
        if response_body is None:
            response_record["body_state"] = body_state or "not_read"
        else:
            response_record.update(_diagnostic_body(response_body))
            response_record["body_state"] = body_state or (
                "empty" if not response_body else "complete"
            )
        declared_length = getattr(response, "hashi_declared_body_bytes", None)
        if declared_length is not None:
            response_record["declared_body_bytes"] = int(declared_length)
        read_error = str(
            getattr(response, "hashi_body_read_error", "") or ""
        )
        if read_error:
            response_record["body_read_error"] = read_error
        diagnostics["response"] = response_record

    audit_refs = getattr(error, "hashi_transport_audit_refs", ())
    if isinstance(audit_refs, (list, tuple)):
        diagnostics["transport_audit_refs"] = [
            str(item) for item in audit_refs if str(item).strip()
        ]
    return diagnostics


def _backend_failure_response(
    error: Exception,
    *,
    duration_ms: float,
    tool_call_count: int = 0,
    tool_loop_count: int = 0,
) -> BackendResponse:
    """Convert OpenAI-compatible transport errors into a typed response."""

    response = getattr(error, "response", None)
    status = (
        int(response.status_code)
        if isinstance(response, httpx.Response)
        else None
    )
    retryable = False
    code = "PROVIDER_UNKNOWN"
    description = "The provider request failed for an unknown technical reason."

    if isinstance(error, ProviderProtocolForensicError):
        code = "AUDIT_PERSISTENCE_FAILURE"
        description = (
            "HASHI stopped because the mandatory private Provider protocol "
            "forensic record could not be persisted."
        )
    elif isinstance(error, MultimodalContractError):
        code = error.code
        description = str(error)
    elif status is not None:
        stable_code = _stable_provider_error_code(response)
        if stable_code == "CONTEXT_CAPACITY_REJECTED":
            code = stable_code
            description = "The provider rejected the serialized request because it exceeds the model context capacity."
        elif stable_code == "PROVIDER_MODALITY_UNSUPPORTED":
            code = stable_code
            description = "The provider explicitly rejected the requested input modality."
        elif status == 400:
            code = "PROVIDER_BAD_REQUEST"
            description = "The provider rejected the request as invalid."
        elif status == 401:
            code = "PROVIDER_AUTHENTICATION_FAILED"
            description = "The provider rejected the configured credentials."
        elif status == 403:
            code = "PROVIDER_PERMISSION_DENIED"
            description = "The provider denied access to this model or request."
        elif status == 408:
            code = "PROVIDER_REQUEST_TIMEOUT"
            description = "The provider timed out while handling the request."
            retryable = True
        elif status == 429:
            code = "PROVIDER_RATE_LIMITED"
            description = "The provider rate-limited the request."
            retryable = True
        elif 500 <= status <= 599:
            code = "PROVIDER_SERVER_ERROR"
            description = "The provider reported a temporary server failure."
            retryable = True
        elif 400 <= status <= 499:
            code = "PROVIDER_BAD_REQUEST"
            description = f"The provider rejected the request with HTTP {status}."
    elif isinstance(error, httpx.TimeoutException):
        code = "PROVIDER_REQUEST_TIMEOUT"
        description = "The provider connection or response timed out."
        retryable = True
    elif isinstance(error, httpx.RemoteProtocolError):
        code = "PROVIDER_INCOMPLETE_STREAM"
        description = "The provider response stream ended before completion."
        retryable = True
    elif isinstance(error, (httpx.InvalidURL, httpx.UnsupportedProtocol)):
        code = "PROVIDER_CONFIGURATION_ERROR"
        description = "The configured provider URL or protocol is invalid."
    elif isinstance(error, httpx.ConnectError):
        lowered = str(error).casefold()
        if any(token in lowered for token in ("certificate", "ssl", "tls")):
            code = "PROVIDER_TLS_ERROR"
            description = "The provider TLS certificate or trust configuration failed."
        else:
            code = "PROVIDER_CONNECTION_FAILED"
            description = "A connection to the provider could not be established."
            retryable = True
    elif isinstance(error, httpx.NetworkError):
        code = "PROVIDER_CONNECTION_FAILED"
        description = "The provider connection was interrupted."
        retryable = True
    elif isinstance(error, (json.JSONDecodeError, UnicodeDecodeError)):
        code = "PROVIDER_INCOMPLETE_STREAM"
        description = "The provider returned an incomplete or invalid response body."
        retryable = True

    side_effects_possible = bool(tool_call_count)
    if side_effects_possible:
        # The runtime performs the final replay-safety decision using actual
        # tool activity.  Preserve uncertainty rather than claiming safety.
        retryable = bool(retryable)
    return BackendResponse(
        text="",
        duration_ms=duration_ms,
        error=str(error),
        is_success=False,
        tool_call_count=int(tool_call_count),
        tool_loop_count=int(tool_loop_count),
        error_code=code,
        error_retryable=retryable,
        http_status=status,
        provider_request_id=_provider_request_id(
            response if isinstance(response, httpx.Response) else None
        )
        or None,
        retry_after_s=_retry_after_seconds(
            response if isinstance(response, httpx.Response) else None
        ),
        side_effects_possible=side_effects_possible,
        stream_metadata={
            "provider_failure_description": description,
            "provider_activity_observed": bool(
                getattr(error, "provider_activity_observed", False)
            ),
            "provider_http_failure": _provider_http_failure_diagnostics(error),
        },
    )


def _transient_provider_call_error(error: Exception) -> bool:
    """Return whether one unfinished provider HTTP call may be retried in place."""

    response = getattr(error, "response", None)
    status = (
        int(response.status_code)
        if isinstance(response, httpx.Response)
        else None
    )
    if status in {408, 429} or (status is not None and 500 <= status <= 599):
        return True
    if isinstance(error, httpx.ConnectError) and any(
        token in str(error).casefold() for token in ("certificate", "ssl", "tls")
    ):
        return False
    return isinstance(
        error,
        (
            httpx.TimeoutException,
            httpx.RemoteProtocolError,
            httpx.ConnectError,
            httpx.NetworkError,
            json.JSONDecodeError,
            UnicodeDecodeError,
        ),
    )


def _provider_call_retry_delay(
    error: Exception,
    *,
    default_s: float,
    maximum_s: float,
) -> float:
    response = getattr(error, "response", None)
    retry_after = _retry_after_seconds(
        response if isinstance(response, httpx.Response) else None
    )
    delay = default_s if retry_after is None else retry_after
    return max(0.0, min(float(maximum_s), float(delay)))


def _assistant_content_text(content: Any) -> str:
    """Normalize common OpenAI-compatible content shapes into assistant text."""

    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, Mapping):
        for key in ("text", "output_text", "content"):
            if key in content:
                return _assistant_content_text(content.get(key))
        return json.dumps(dict(content), ensure_ascii=False)
    if isinstance(content, list):
        return "".join(_assistant_content_text(item) for item in content)
    return str(content)


def _message_structured_data(message: Mapping[str, Any]) -> dict[str, Any] | None:
    """Preserve an API-native parsed object without granting it authority."""

    for key in ("parsed", "structured_output"):
        value = message.get(key)
        if isinstance(value, Mapping):
            return dict(value)
    content = message.get("content")
    if isinstance(content, Mapping):
        return dict(content)
    return None


def _decode_provider_audio(value: Any) -> bytes:
    if not value:
        return b""
    if not isinstance(value, str):
        raise MultimodalContractError(
            "provider audio data must be Base64 text",
            code="INVALID_PROVIDER_AUDIO_OUTPUT",
        )
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise MultimodalContractError(
            "provider returned invalid Base64 audio",
            code="INVALID_PROVIDER_AUDIO_OUTPUT",
        ) from exc


def _pcm16_to_wav(
    payload: bytes,
    *,
    sample_rate_hz: int = 24_000,
    channels: int = 1,
) -> bytes:
    """Wrap provider PCM16 output in a terminal-safe WAV container."""

    if len(payload) % 2:
        raise MultimodalContractError(
            "provider returned an incomplete PCM16 sample",
            code="INVALID_PROVIDER_AUDIO_OUTPUT",
        )
    if sample_rate_hz <= 0 or channels <= 0:
        raise MultimodalContractError(
            "provider PCM16 output parameters are invalid",
            code="AUDIO_OUTPUT_CAPABILITY_INCOMPLETE",
        )
    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(channels)
        target.setsampwidth(2)
        target.setframerate(sample_rate_hz)
        target.writeframes(payload)
    return output.getvalue()


def _append_provider_transcript(current: str, delta: Any) -> str:
    text = str(delta or "")
    if not text:
        return current
    # Providers may send either incremental fragments or a cumulative value.
    if text.startswith(current):
        return text
    if current.endswith(text):
        return current
    return current + text


def _tool_target_path(arguments: dict) -> str | None:
    for key in ("path", "file_path", "target_path"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _file_resource(arguments: dict) -> str:
    path = _tool_target_path(arguments)
    return f"file:{path}" if path else "file:*"


class OpenRouterAdapter(BaseBackend):
    # OpenRouter aggregates providers with different replay guarantees.  A
    # concrete compatible adapter may opt into narrowly scoped HTTP-call
    # recovery without replaying completed tool loops.
    TRANSIENT_PROVIDER_CALL_RETRIES = INVALID_TOOL_CALL_REPAIR_LIMIT
    TRANSIENT_PROVIDER_CALL_RETRY_DELAY_S = 1.0
    TRANSIENT_PROVIDER_CALL_RETRY_MAX_DELAY_S = 5.0

    def _define_capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            supports_sessions=False,
            supports_files=False,
            supports_tool_use=True,
            supports_thinking_stream=True,
            supports_headless_mode=True,
            supports_progress_stream=True,
            supports_tool_stream=True,
            supports_answer_stream=True,
            continuation_mode="reconstructed",
            tool_request_mode="native",
            recovery_mode="reconstruct_safe",
            reasoning_transport="visible_optional",
        )

    def __init__(self, agent_config, global_config, api_key: str = None):
        super().__init__(agent_config, global_config, api_key)
        self.logger = logging.getLogger(f"Backend.OpenRouter.{self.config.name}")
        self.client = None
        self.sys_prompt = "You are a helpful AI assistant."
        # ``None`` preserves the provider/model default. Explicit False must be
        # distinguishable because some OpenRouter models default reasoning on.
        self.reasoning_enabled: bool | None = None
        self.tool_registry = None   # Injected by FlexibleBackendManager if tools configured
        self._audio_asset_store: AudioAssetStore | None = None
        self._provider_call_observer: ProviderCallObserver | None = None
        self._provider_invocation_context: dict[str, Any] = {}
        self._active_provider_wire_context: dict[str, Any] = {}

    def _provider_evidence_url(self) -> str:
        """Return the effective non-secret HTTP endpoint for wire evidence."""

        return self._chat_completions_url()

    def _record_provider_wire_evidence(
        self,
        event: str,
        *,
        request_id: str,
        call_serial: int,
        payload: Mapping[str, Any],
    ) -> str:
        """Persist complete Provider wire evidence through the PAO store.

        A running HASHI Worker always owns a canonical audit store outside the
        mutable Agent workspace. Direct adapter use (diagnostics and focused
        tests) falls back to one private, append-only local evidence file. The
        fallback is deliberately not rotated: it must not become a seven-day
        replacement for the canonical original.
        """

        record = {
            "format": "hashi-provider-wire-v1",
            "event": str(event),
            "recorded_at": _utc_timestamp(),
            "instance_id": str(
                getattr(self.global_config, "instance_id", "")
                or getattr(self.global_config, "name", "")
                or ""
            ),
            "agent_id": str(getattr(self.config, "name", "") or ""),
            "adapter": type(self).__name__,
            "provider": str(
                getattr(self.config, "engine", "") or "openrouter-api"
            ),
            "model": str(getattr(self.config, "model", "") or ""),
            "hashi_request_id": str(request_id or ""),
            "call_serial": max(1, int(call_serial)),
            "runtime_generation_id": str(
                getattr(self.config, "runtime_generation_id", "")
                or getattr(self.config, "generation_id", "")
                or ""
            ),
            **dict(getattr(self, "_provider_invocation_context", {}) or {}),
            "payload": dict(payload),
        }
        runtime = getattr(self.config, "_hashi_runtime", None)
        canonical = getattr(runtime, "canonical_audit", None)
        if canonical is not None and callable(getattr(canonical, "record", None)):
            try:
                event_id = canonical.record(
                    "provider_wire_evidence",
                    record,
                    request_id=str(request_id or ""),
                    provenance={
                        "adapter": type(self).__name__,
                        "provider": record["provider"],
                        "model": record["model"],
                    },
                )
            except Exception as exc:
                raise ProviderProtocolForensicError(
                    "canonical Provider wire evidence could not be persisted"
                ) from exc
            return f"canonical-audit:{event_id}"

        workspace = Path(self.config.workspace_dir).expanduser().resolve()
        forensic_root = workspace / "logs" / "provider_protocol_forensics"
        path = forensic_root / "provider-wire.jsonl"
        record_id = "provider-wire-" + uuid4().hex
        encoded = json.dumps(
            {"event_id": record_id, **record},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ) + "\n"
        try:
            from tools.private_files import protect_private_file

            forensic_root.mkdir(parents=True, exist_ok=True)
            if os.name == "nt":
                protect_private_file(forensic_root)
            else:
                forensic_root.chmod(0o700)
            descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                protect_private_file(path)
                with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
                    descriptor = -1
                    with _PROVIDER_FORENSIC_WRITE_LOCK:
                        handle.write(encoded)
                        handle.flush()
                        os.fsync(handle.fileno())
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
        except OSError as exc:
            raise ProviderProtocolForensicError(
                f"private Provider wire evidence persistence failed: {exc}"
            ) from exc
        return f"provider-wire:{path}:{record_id}"

    def set_provider_call_observer(
        self,
        observer: ProviderCallObserver | None,
    ) -> None:
        """Install a synchronous durable observer for physical HTTP calls."""

        self._provider_call_observer = observer

    def set_provider_invocation_context(self, context: Mapping[str, Any]) -> None:
        """Bind HER/PAO identifiers to private protocol-forensic records."""

        self._provider_invocation_context = {
            str(key): value
            for key, value in dict(context or {}).items()
            if value not in (None, "")
        }

    def _write_invalid_tool_call_forensic(
        self,
        *,
        path: Path | None,
        request_id: str,
        call_serial: int,
        payload: Mapping[str, Any],
        result: _APIResult,
        provider_call_record: Mapping[str, Any],
        repair_response_number: int,
        next_repair_number: int | None,
        incident_number: int,
    ) -> tuple[Path, list[dict[str, Any]]]:
        """Durably append one complete bad-tool response to a private log."""

        workspace = Path(self.config.workspace_dir).expanduser().resolve()
        forensic_root = workspace / "logs" / "provider_protocol_forensics"
        if path is None:
            identity = hashlib.sha256(
                f"{request_id}|{incident_number}|{uuid4().hex}".encode("utf-8")
            ).hexdigest()[:24]
            path = forensic_root / f"invalid-tool-calls-{identity}.jsonl"

        wire = (
            dict(result.wire_evidence)
            if isinstance(result.wire_evidence, Mapping)
            else {}
        )
        raw_provider = wire or {
            "transport": "test_double",
            "request": _request_wire_evidence(payload),
            "response": {
                "provider_response_id": str(result.provider_response_id or ""),
                "transport_request_id": str(result.transport_request_id or ""),
                "finish_reason": result.raw_finish_reason,
                "text": result.text,
                "tool_calls": result.tool_calls,
            },
            "sse_events": [],
            "tool_call_fragments": [],
            "assembly_snapshots": [],
        }
        raw_fragments = raw_provider.get("tool_call_fragments")
        tool_details = _tool_call_forensic_details(
            result.tool_calls,
            argument_fragments=(
                list(raw_fragments) if isinstance(raw_fragments, list) else []
            ),
        )
        provider_request_id = str(
            result.provider_response_id
            or result.transport_request_id
            or provider_call_record.get("provider_request_id")
            or ""
        )
        record = {
            "format": "hashi-provider-tool-forensic-v1",
            "retention": "canonical_or_indefinite_local",
            "recorded_at": _utc_timestamp(),
            "hashi": {
                "request_id": str(request_id or ""),
                "call_serial": max(1, int(call_serial)),
                "agent_id": str(getattr(self.config, "name", "") or ""),
                "instance_id": str(
                    getattr(self.global_config, "instance_id", "")
                    or getattr(self.global_config, "name", "")
                    or ""
                ),
                "runtime_generation_id": str(
                    getattr(self.config, "runtime_generation_id", "")
                    or getattr(self.config, "generation_id", "")
                    or ""
                ),
                **dict(self._provider_invocation_context),
            },
            "provider": {
                "adapter": type(self).__name__,
                "provider": str(
                    getattr(self.config, "engine", "") or "openrouter-api"
                ),
                "model": str(getattr(self.config, "model", "") or ""),
                "provider_request_id": provider_request_id,
                "provider_response_id": str(result.provider_response_id or ""),
                "transport_request_id": str(result.transport_request_id or ""),
                "finish_reason_present": bool(result.finish_reason_present),
                "raw_finish_reason": result.raw_finish_reason,
                "normalized_finish_reason": str(result.finish_reason or ""),
            },
            "repair": {
                "incident": max(1, int(incident_number)),
                "response_to_repair_request": (
                    f"{repair_response_number}/{INVALID_TOOL_CALL_REPAIR_LIMIT}"
                    if repair_response_number
                    else None
                ),
                "next_request": (
                    f"{next_repair_number}/{INVALID_TOOL_CALL_REPAIR_LIMIT}"
                    if next_repair_number is not None
                    else None
                ),
                "status": "retrying" if next_repair_number is not None else "exhausted",
            },
            "request_started_at": provider_call_record.get("request_started_at"),
            "response_observed_at": provider_call_record.get("response_observed_at"),
            "raw_provider": raw_provider,
            "tool_calls": tool_details,
            "normalization": {
                "tool_protocol_summary": _tool_call_protocol_summary(
                    result.tool_calls
                )[0],
                "transport_complete": bool(result.transport_complete),
                "transport_state": str(result.transport_state or ""),
            },
        }
        encoded = json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ) + "\n"
        try:
            from tools.private_files import protect_private_file
            forensic_root.mkdir(parents=True, exist_ok=True)
            if os.name == 'nt':
                protect_private_file(forensic_root)
            else:
                forensic_root.chmod(0o700)
            flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
            descriptor = os.open(path, flags, 0o600)
            try:
                protect_private_file(path)
                with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
                    descriptor = -1
                    with _PROVIDER_FORENSIC_WRITE_LOCK:
                        handle.write(encoded)
                        handle.flush()
                        os.fsync(handle.fileno())
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
        except OSError as exc:
            raise ProviderProtocolForensicError(
                f"private Provider protocol forensic persistence failed: {exc}"
            ) from exc
        return path, tool_details

    def _provider_call_record(
        self,
        *,
        request_id: str,
        serial: int,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Create and immediately publish one immutable physical-call fact."""

        record = dict(payload)
        record.setdefault("protocol_record_version", 1)
        record.setdefault("call_serial", max(1, int(serial)))
        record.setdefault("hashi_request_id", str(request_id or ""))
        record.setdefault("agent_id", str(getattr(self.config, "name", "") or ""))
        record.setdefault(
            "instance_id",
            str(
                getattr(self.global_config, "instance_id", "")
                or getattr(self.global_config, "name", "")
                or ""
            ),
        )
        record.setdefault(
            "provider", str(getattr(self.config, "engine", "") or "openrouter-api")
        )
        record.setdefault("model", str(getattr(self.config, "model", "") or ""))
        record.setdefault(
            "runtime_generation_id",
            str(
                getattr(self.config, "runtime_generation_id", "")
                or getattr(self.config, "generation_id", "")
                or ""
            ),
        )
        record.setdefault(
            "provider_request_id",
            "hashi-provider:"
            + hashlib.sha256(
                "|".join(
                    (
                        type(self).__name__,
                        str(getattr(self.config, "model", "") or ""),
                        str(request_id or ""),
                        str(max(1, int(serial))),
                    )
                ).encode("utf-8")
            ).hexdigest(),
        )
        observer = getattr(self, "_provider_call_observer", None)
        if observer is not None:
            try:
                observer(record)
            except ProviderCallObserverError:
                raise
            except Exception as exc:
                raise ProviderCallObserverError(
                    "physical Provider request could not be durably observed"
                ) from exc
        return record

    def set_reasoning_enabled(self, enabled: bool | None) -> None:
        self.reasoning_enabled = None if enabled is None else bool(enabled)

    def _native_audio_output_profile(
        self, request_content: Mapping[str, Any] | None
    ) -> dict[str, Any] | None:
        """Resolve exact, explicitly configured audio output or fail closed."""

        if not request_content_is_voice_origin(request_content):
            return None
        extra = dict(getattr(self.config, "extra", {}) or {})
        if bool(extra.get("_native_audio_output_disabled")):
            return None
        if "audio" not in self.capabilities.output_modalities:
            return None
        if self.capabilities.api_surface != "chat_completions":
            raise MultimodalContractError(
                "native audio output requires a chat_completions capability",
                code="AUDIO_OUTPUT_CAPABILITY_INCOMPLETE",
            )
        if self.capabilities.output_streaming not in {
            "sse",
            "openai_sse",
            "server_sent_events",
        }:
            raise MultimodalContractError(
                "native audio output requires an explicitly verified SSE stream",
                code="AUDIO_OUTPUT_CAPABILITY_INCOMPLETE",
            )
        configured_formats = self.capabilities.output_formats.get("audio", ())
        configured_format = str(extra.get("native_audio_format") or "").strip()
        audio_format = (
            (
                "pcm16"
                if configured_format.casefold() == "pcm16"
                else normalize_audio_format(configured_format)
            )
            if configured_format
            else next(
                (
                    candidate
                    for candidate in (
                        "pcm16",
                        "wav",
                        "mp3",
                        "ogg",
                        "opus",
                        "webm",
                        "flac",
                        "m4a",
                        "mp4",
                    )
                    if candidate in configured_formats
                ),
                "",
            )
        )
        if not configured_formats or audio_format not in configured_formats:
            raise MultimodalContractError(
                "native audio output format is not explicitly supported",
                code="AUDIO_OUTPUT_CAPABILITY_INCOMPLETE",
            )
        voice = str(extra.get("native_audio_voice") or "").strip().casefold()
        if not voice and self.capabilities.supported_voices:
            voice = self.capabilities.supported_voices[0]
        if not voice:
            raise MultimodalContractError(
                "native audio output requires a configured voice",
                code="AUDIO_OUTPUT_CAPABILITY_INCOMPLETE",
            )
        if (
            self.capabilities.supported_voices
            and voice not in self.capabilities.supported_voices
        ):
            raise MultimodalContractError(
                "native audio voice is not explicitly supported",
                code="AUDIO_OUTPUT_CAPABILITY_INCOMPLETE",
            )
        raw_retention = extra.get(
            "native_audio_retention_seconds", DEFAULT_RETENTION_SECONDS
        )
        retention_indefinite = str(raw_retention).strip().casefold() in {
            "indefinite",
            "forever",
        }
        retention_seconds = (
            DEFAULT_RETENTION_SECONDS
            if retention_indefinite
            else int(raw_retention)
        )
        asset_format = "wav" if audio_format == "pcm16" else audio_format
        return {
            "voice": voice,
            "format": audio_format,
            "mime_type": {
                "wav": "audio/wav",
                "mp3": "audio/mpeg",
                "ogg": "audio/ogg",
                "opus": "audio/ogg",
                "webm": "audio/webm",
                "flac": "audio/flac",
                "m4a": "audio/mp4",
                "mp4": "audio/mp4",
            }[asset_format],
            "asset_format": asset_format,
            "pcm_sample_rate_hz": int(
                extra.get("native_audio_pcm_sample_rate_hz", 24_000)
            ),
            "pcm_channels": int(extra.get("native_audio_pcm_channels", 1)),
            "tools": bool(extra.get("audio_model_tools", False)),
            "retention_seconds": retention_seconds,
            "retention_indefinite": retention_indefinite,
        }

    def _native_audio_asset_store_instance(self) -> AudioAssetStore:
        if self._audio_asset_store is None:
            self._audio_asset_store = AudioAssetStore(
                asset_root_from_global_config(self.global_config)
            )
        return self._audio_asset_store

    def _reasoning_payload(self) -> dict[str, Any] | None:
        extra = dict(getattr(self.config, "extra", None) or {})
        configured = extra.get("provider_reasoning")
        if configured is None:
            configured = extra.get("reasoning_effort")
        normalized = (
            str(configured).strip().casefold() if configured is not None else ""
        )
        if normalized in _REASONING_DISABLED_VALUES:
            return {"enabled": False}
        if normalized in _REASONING_EFFORT_VALUES:
            return {
                "enabled": True,
                "effort": normalized,
                "exclude": False,
            }
        if configured is not None or self.reasoning_enabled is True:
            return {"enabled": True, "exclude": False}
        if self.reasoning_enabled is False:
            return {"enabled": False}
        return None

    def _ensure_client(self):
        if self.client is None or getattr(self.client, "is_closed", False):
            self.client = httpx.AsyncClient(timeout=float(self.PROCESS_TIMEOUT_SEC))

    def _summarize_reasoning_detail(self, detail) -> str:
        if not isinstance(detail, dict):
            return ""
        detail_type = str(detail.get("type") or "").strip()
        if detail_type == "reasoning.text":
            return str(detail.get("text") or "").strip()
        if detail_type == "reasoning.summary":
            return str(detail.get("summary") or "").strip()
        if detail_type == "reasoning.encrypted":
            return "[Encrypted reasoning]"
        return (
            str(detail.get("text") or "").strip()
            or str(detail.get("summary") or "").strip()
        )

    def _reasoning_detail_delta(self, detail) -> str:
        if not isinstance(detail, dict):
            return ""
        detail_type = str(detail.get("type") or "").strip()
        if detail_type == "reasoning.encrypted":
            return ""
        if detail_type == "reasoning.summary":
            return str(detail.get("summary") or "")
        return str(detail.get("text") or detail.get("summary") or "")

    async def initialize(self) -> bool:
        self.config.workspace_dir.mkdir(parents=True, exist_ok=True)
        if not self.api_key:
            self.logger.error("No OpenRouter API key provided in secrets.json")
            return False
        self._ensure_client()

        try:
            if self.config.system_md and Path(self.config.system_md).exists():
                self.sys_prompt = load_pcm_document(
                    self.config.system_md,
                    workspace_dir=self.config.workspace_dir,
                ).system
        except Exception as e:
            self.logger.warning(f"Could not read system_md: {e}")

        self.logger.info("OpenRouter adapter initialized in stateless mode.")
        return True

    async def handle_new_session(self) -> bool:
        self.logger.info("OpenRouter backend is stateless. /new acknowledged.")
        return True

    async def get_key_info(self) -> dict | None:
        try:
            self._ensure_client()
            response = await self.client.get(
                "https://openrouter.ai/api/v1/key",
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.logger.error(f"Failed to fetch OpenRouter key info: {e}")
            return None

    # ------------------------------------------------------------------
    # Payload builder
    # ------------------------------------------------------------------

    # Default tiers for OpenRouter — None means send all allowed tools.
    # Subclasses (e.g. OllamaAdapter) override with smaller defaults.
    DEFAULT_TOOL_TIERS: list[str] | None = None

    def _request_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "HTTP-Referer": "https://github.com/Bazza1982/HASHI",
            "X-Title": "Bridge-U Orchestrator",
        }

    def _chat_completions_url(self) -> str:
        """Return the concrete provider endpoint for this adapter."""

        return self.global_config.openrouter_url

    def _augment_assistant_tool_message(
        self,
        assistant_msg: dict[str, Any],
        result: _APIResult,
    ) -> None:
        del assistant_msg, result

    def _media_fallback_modalities(self) -> frozenset[str]:
        registry = getattr(self, "tool_registry", None)
        is_allowed = getattr(registry, "is_allowed", None)
        if not callable(is_allowed):
            return frozenset()
        modalities: set[str] = set()
        if is_allowed("media_read"):
            modalities.update({"image", "audio", "video", "document"})
        if is_allowed("vision_inspect"):
            modalities.add("image")
        if is_allowed("file_read"):
            modalities.add("document")
        return frozenset(modalities)

    def _initial_messages(
        self,
        prompt: str,
        request_content: Mapping[str, Any] | None,
    ) -> tuple[list[dict[str, Any]], tuple[dict[str, Any], ...]]:
        normalized = normalize_request_content(request_content)
        if normalized is None or not request_content_has_media(normalized):
            return (
                [
                    {"role": "system", "content": self.sys_prompt},
                    {"role": "user", "content": prompt},
                ],
                (),
            )
        capability = self.resolve_input_capability()
        validate_authorized_media_references(
            normalized,
            authorized_roots=self.authorized_media_roots(),
        )
        content, decisions = materialize_openai_user_content(
            prompt,
            normalized,
            capability,
            authorized_roots=self.authorized_media_roots(),
            fallback_modalities=self._media_fallback_modalities(),
        )
        unsupported = [item for item in decisions if item.route == "unsupported"]
        if unsupported:
            first = unsupported[0]
            error_code = media_failure_code(first.reason)
            raise MultimodalContractError(
                f"{capability.provider}/{capability.model} cannot consume "
                f"{first.modality} attachment {first.attachment_id!r} and no "
                "authorized local fallback is available",
                code=error_code,
                attachment_id=first.attachment_id,
            )
        return (
            [
                {"role": "system", "content": self.sys_prompt},
                {"role": "user", "content": content},
            ],
            routing_decisions_payload(decisions),
        )

    async def _prepare_audio_input_formats(
        self,
        request_content: Mapping[str, Any] | None,
    ) -> tuple[dict[str, Any] | None, tuple[Path, ...], tuple[dict[str, Any], ...]]:
        """Normalize audio once at the provider boundary when explicitly required."""

        normalized = normalize_request_content(request_content)
        declared_formats = getattr(
            getattr(self, "capabilities", None), "input_formats", {}
        ) or {}
        allowed_formats = tuple(
            str(item or "").strip().casefold()
            for item in declared_formats.get("audio", ())
            if str(item or "").strip()
        )
        if normalized is None or not allowed_formats:
            return normalized, (), ()
        validate_authorized_media_references(
            normalized,
            authorized_roots=self.authorized_media_roots(),
        )
        target_format = next(
            (
                candidate
                for candidate in ("wav", "mp3", "flac", "ogg", "opus", "webm", "m4a", "mp4")
                if candidate in allowed_formats
            ),
            "",
        )
        root = Path(
            getattr(self.global_config, "base_media_dir", None)
            or self.config.workspace_dir
        ).resolve()
        derivative_root = root / "native_audio_derivatives"
        derivative_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        prepared_parts: list[dict[str, Any]] = []
        derivatives: list[Path] = []
        records: list[dict[str, Any]] = []
        mime_types = {
            "wav": "audio/wav",
            "mp3": "audio/mpeg",
            "flac": "audio/flac",
            "ogg": "audio/ogg",
            "opus": "audio/ogg",
            "webm": "audio/webm",
            "m4a": "audio/mp4",
            "mp4": "audio/mp4",
        }
        try:
            for raw_part in normalized["parts"]:
                part = dict(raw_part)
                if part.get("type") != "media" or part.get("modality") != "audio":
                    prepared_parts.append(part)
                    continue
                source_format = normalize_audio_format(
                    Path(str(part.get("filename") or "")).suffix,
                    mime_type=str(part.get("mime_type") or ""),
                )
                if source_format in allowed_formats:
                    prepared_parts.append(part)
                    continue
                if not target_format:
                    raise MultimodalContractError(
                        "the declared audio input formats have no supported normalizer",
                        code="AUDIO_INPUT_FORMAT_UNSUPPORTED",
                        attachment_id=str(part.get("attachment_id") or ""),
                    )
                source_path = Path(str(part["local_ref"])).resolve(strict=True)
                target = derivative_root / f"audio_{uuid4().hex}.{target_format}"
                command = [
                    str(
                        dict(getattr(self.config, "extra", {}) or {}).get(
                            "ffmpeg_cmd", "ffmpeg"
                        )
                    ),
                    "-y",
                    "-i",
                    str(source_path),
                    "-vn",
                    "-ac",
                    "1",
                ]
                if target_format == "wav":
                    command.extend(["-ar", "16000", "-c:a", "pcm_s16le"])
                elif target_format == "mp3":
                    command.extend(["-c:a", "libmp3lame"])
                elif target_format == "flac":
                    command.extend(["-c:a", "flac"])
                elif target_format in {"ogg", "opus", "webm"}:
                    command.extend(["-c:a", "libopus"])
                else:
                    command.extend(["-c:a", "aac"])
                command.append(str(target))
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _stdout, stderr = await process.communicate()
                if process.returncode != 0 or not target.exists():
                    reason = stderr.decode("utf-8", errors="replace").strip()
                    raise MultimodalContractError(
                        "audio input normalization failed"
                        + (f": {reason[-400:]}" if reason else ""),
                        code="AUDIO_INPUT_NORMALIZATION_FAILED",
                        attachment_id=str(part.get("attachment_id") or ""),
                    )
                payload = target.read_bytes()
                validate_audio_signature(payload, target_format)
                derivatives.append(target)
                part.update(
                    {
                        "filename": target.name,
                        "mime_type": mime_types[target_format],
                        "local_ref": str(target),
                        "size_bytes": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    }
                )
                prepared_parts.append(part)
                records.append(
                    {
                        "attachment_id": str(part.get("attachment_id") or ""),
                        "source_format": source_format,
                        "provider_format": target_format,
                    }
                )
            return (
                canonical_request_content(prepared_parts),
                tuple(derivatives),
                tuple(records),
            )
        except Exception:
            for derivative in derivatives:
                derivative.unlink(missing_ok=True)
            raise

    def _can_replay_typed_media_fallback(
        self,
        error: Exception,
        *,
        media_routing: tuple[dict[str, Any], ...],
        fallback_attempted: bool,
        provider_call_count: int,
        tool_call_count: int,
    ) -> bool:
        if (
            fallback_attempted
            or provider_call_count
            or tool_call_count
            or bool(getattr(error, "provider_activity_observed", False))
        ):
            return False
        response = getattr(error, "response", None)
        if not isinstance(response, httpx.Response):
            return False
        if _stable_provider_error_code(response) != "PROVIDER_MODALITY_UNSUPPORTED":
            return False
        if not media_routing or not any(
            str(item.get("route") or "") == "native" for item in media_routing
        ):
            return False
        fallback_modalities = self._media_fallback_modalities()
        return bool(fallback_modalities) and all(
            str(item.get("modality") or "") in fallback_modalities
            for item in media_routing
        )

    def _typed_media_fallback_messages(
        self,
        prompt: str,
        request_content: Mapping[str, Any] | None,
    ) -> list[dict[str, Any]]:
        capability = self.resolve_input_capability()
        content, _decisions = materialize_openai_user_content(
            prompt,
            request_content,
            InputCapability(
                provider=capability.provider,
                model=capability.model,
                input_modalities=frozenset({"text"}),
                input_transports={},
                limits=capability.limits,
                privacy_eligible=capability.privacy_eligible,
                source="typed_local_fallback",
            ),
            authorized_roots=self.authorized_media_roots(),
            fallback_modalities=self._media_fallback_modalities(),
        )
        return [
            {"role": "system", "content": self.sys_prompt},
            {"role": "user", "content": content},
        ]

    def _enable_request_local_media_fallback(
        self,
        attachment_ids: set[str],
    ) -> None:
        enable = getattr(
            getattr(self, "tool_registry", None),
            "enable_local_media_fallback",
            None,
        )
        if callable(enable):
            enable(set(attachment_ids))

    @staticmethod
    def _typed_media_fallback_routing(
        media_routing: tuple[dict[str, Any], ...],
    ) -> tuple[dict[str, Any], ...]:
        return tuple(
            {
                **dict(item),
                "route": "local_fallback",
                "reason": "provider_typed_modality_unsupported",
                "transport": None,
            }
            for item in media_routing
        )

    def _build_payload(
        self,
        messages: list[dict],
        use_streaming: bool = False,
        tool_tiers: list[str] | None = ...,
        *,
        excluded_tool_names: frozenset[str] = frozenset(),
        audio_output: Mapping[str, Any] | None = None,
        allow_tools: bool = True,
    ) -> dict:
        payload: dict = {
            "model": self.config.model,
            "messages": messages,
        }
        reasoning = self._reasoning_payload()
        if reasoning is not None:
            payload["reasoning"] = reasoning
        if use_streaming:
            payload["stream"] = True
            payload["stream_options"] = {"include_usage": True}
        if audio_output is not None:
            payload["modalities"] = ["text", "audio"]
            payload["audio"] = {
                "voice": str(audio_output["voice"]),
                "format": str(audio_output["format"]),
            }
        if allow_tools and self.tool_registry:
            tiers = self.DEFAULT_TOOL_TIERS if tool_tiers is ... else tool_tiers
            tool_defs = self.tool_registry.get_tool_definitions(tiers=tiers)
            if excluded_tool_names:
                tool_defs = [
                    item
                    for item in tool_defs
                    if str((item.get("function") or {}).get("name") or "")
                    not in excluded_tool_names
                ]
            if tool_defs:
                payload["tools"] = tool_defs
        return payload

    # ------------------------------------------------------------------
    # Stream event helper
    # ------------------------------------------------------------------

    async def _emit(
        self,
        on_stream_event: StreamCallback,
        kind: str,
        summary: str,
        tool_name: str = "",
        file_path: str = "",
        metadata: Mapping[str, Any] | None = None,
        delivery_class: str = "",
    ) -> None:
        if on_stream_event is None:
            return
        try:
            await on_stream_event(
                StreamEvent(
                    kind=kind,
                    summary=summary,
                    tool_name=tool_name,
                    file_path=file_path,
                    metadata=dict(metadata or {}),
                    delivery_class=delivery_class,
                )
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Tool execution with stream events
    # ------------------------------------------------------------------

    async def _run_tool_calls(
        self,
        tool_calls: list[dict],
        messages: list[dict],
        on_stream_event: StreamCallback,
        *,
        native_attachment_ids: set[str] | None = None,
        native_local_refs: set[str] | None = None,
        all_media_native: bool = False,
        provider_call_context: Mapping[str, Any] | None = None,
    ) -> None:
        """Execute all tool_calls and append tool result messages to `messages`."""
        for tc in tool_calls:
            fn = tc.get("function", {})
            tool_name = fn.get("name", "unknown")
            tc_id = tc.get("id", "")
            raw_args = fn.get("arguments", "{}")
            correlation = {
                **dict(provider_call_context or {}),
                "tool_call_id": str(tc_id or ""),
                "tool_name": str(tool_name or ""),
            }

            # Determine stream event kind
            if tool_name in {"bash", "shell"}:
                evt_kind = KIND_SHELL_EXEC
            elif tool_name == "file_read":
                evt_kind = KIND_FILE_READ
            elif tool_name == "file_write":
                evt_kind = KIND_FILE_EDIT
            else:
                evt_kind = KIND_TOOL_START

            # Parse arguments
            try:
                arguments = (
                    dict(raw_args)
                    if isinstance(raw_args, Mapping)
                    else (json.loads(raw_args) if raw_args else {})
                )
            except json.JSONDecodeError as e:
                result_text = f"Error: could not parse tool arguments: {e}"
                await self._emit(
                    on_stream_event,
                    evt_kind,
                    f"{tool_name}: {raw_args[:120]}",
                    tool_name=tool_name,
                    metadata=correlation,
                )
                await self._emit(on_stream_event, KIND_TOOL_END,
                                 f"{tool_name}: argument parse error", tool_name=tool_name,
                                 metadata={**correlation, "is_error": True})
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": result_text,
                })
                continue

            event_metadata: dict[str, Any] = {}
            event_path = ""
            if isinstance(arguments, Mapping):
                if tool_name in {"bash", "shell"}:
                    command = arguments.get("command") or arguments.get("cmd")
                    if command:
                        event_metadata["command"] = str(command)
                elif tool_name in {"file_read", "file_write"}:
                    path = arguments.get("path") or arguments.get("file_path")
                    if path:
                        event_path = str(path)
                        event_metadata["file_paths"] = (event_path,)
            # One canonical start event per operation.  The specialised kind
            # already carries tool-start semantics; a preceding generic
            # KIND_TOOL_START would make deterministic activity counters lie.
            await self._emit(
                on_stream_event,
                evt_kind,
                f"{tool_name}: {raw_args[:120]}",
                tool_name=tool_name,
                file_path=event_path,
                metadata={**event_metadata, **correlation},
            )

            if tool_name in _MEDIA_FALLBACK_TOOL_NAMES and (
                all_media_native
                or _references_native_attachment(
                    arguments,
                    attachment_ids=set(native_attachment_ids or ()),
                    local_refs=set(native_local_refs or ()),
                )
            ):
                result_text = (
                    "Error: this attachment was already supplied through the native "
                    "media route; duplicate fallback processing is blocked."
                )
                await self._emit(
                    on_stream_event,
                    KIND_TOOL_END,
                    f"{tool_name}: duplicate media fallback blocked",
                    tool_name=tool_name,
                    metadata={**correlation, "blocked": True},
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": result_text,
                    }
                )
                continue

            policy = self._evaluate_tool_policy(tool_name, arguments)
            if not policy.allowed:
                result_text = self._blocked_tool_result_text(tool_name, policy)
                denial_recorder = getattr(
                    self.tool_registry, "record_policy_denial", None
                )
                denial_details = {}
                if callable(denial_recorder):
                    denial_result = await denial_recorder(
                        tool_name,
                        arguments,
                        tc_id,
                        output=result_text,
                        decision=policy.decision.value,
                    )
                    result_text = denial_result.output
                    denial_details = dict(denial_result.details or {})
                await self._emit(on_stream_event, KIND_TOOL_END,
                                 f"{tool_name}: blocked by policy", tool_name=tool_name,
                                 metadata={
                                     **correlation,
                                     "blocked": True,
                                     "tool_result_details": denial_details,
                                 })
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": result_text,
                })
                continue

            # Execute
            try:
                result = await self.tool_registry.execute(
                    tool_name, arguments, tool_call_id=tc_id
                )
            except asyncio.CancelledError as exc:
                details = dict(getattr(exc, "hashi_tool_details", {}) or {})
                await self._emit(
                    on_stream_event,
                    KIND_TOOL_END,
                    f"{tool_name}: cancelled after cleanup",
                    tool_name=tool_name,
                    metadata={
                        **correlation,
                        "is_error": True,
                        "tool_result_details": details,
                    },
                )
                raise

            output_preview = result.output[:100].replace("\n", " ")
            await self._emit(on_stream_event, KIND_TOOL_END,
                             f"{tool_name}: {output_preview}", tool_name=tool_name,
                             metadata={
                                 **correlation,
                                 "is_error": bool(getattr(result, "is_error", False)),
                                 "tool_result_details": dict(result.details or {})
                             })

            messages.append({
                "role": "tool",
                "tool_call_id": tc_id,
                "content": result.output,
            })

    def _evaluate_tool_policy(self, tool_name: str, arguments: dict):
        action, resource = self._tool_policy_action_resource(tool_name, arguments)
        return evaluate_governance_policy(
            action,
            {
                "global_config": self.global_config,
                "agent_id": getattr(self.config, "name", None),
                "backend": getattr(self.config, "engine", None),
                "tool_name": tool_name,
                "tool_arguments": arguments,
                "resource": resource,
                "target_path": _tool_target_path(arguments),
            },
        )

    def _tool_policy_action_resource(self, tool_name: str, arguments: dict) -> tuple[str, str]:
        normalized = (tool_name or "").strip().lower()
        if normalized in {"bash", "shell"}:
            if normalized == "bash":
                selected_shell = "bash"
            else:
                from orchestrator.process_execution import default_shell_name

                selected_shell = str(
                    arguments.get("shell") or default_shell_name()
                ).strip().casefold()
            return "shell.execute", f"shell:{selected_shell}"
        if normalized == "file_write":
            return "file.write", _file_resource(arguments)
        if normalized == "file_read":
            return "file.read", _file_resource(arguments)
        return "tool.execute", f"tool:{normalized or 'unknown'}"

    def _blocked_tool_result_text(self, tool_name: str, policy) -> str:
        if policy.decision.value == "approval_required":
            return f"Error: tool call requires approval by enterprise policy: {tool_name}"
        return f"Error: tool call blocked by enterprise policy: {tool_name}"

    # ------------------------------------------------------------------
    # Non-streaming single API call
    # ------------------------------------------------------------------

    async def _call_api_once(
        self,
        payload: dict,
        headers: dict,
        on_stream_event: StreamCallback,
    ) -> _APIResult:
        response = await self.client.post(
            self._chat_completions_url(),
            json=payload,
            headers=headers,
        )
        response.raise_for_status()
        try:
            response_request = response.request
        except (AttributeError, RuntimeError):
            response_request = None
        wire_evidence = {
            "transport": "json",
            "request": _request_wire_evidence(payload, response_request),
            "raw_response": _response_wire_evidence(response),
            "sse_events": [],
            "tool_call_fragments": [],
            "assembly_snapshots": [],
        }
        data = response.json()
        choices = data.get("choices") or []
        if not choices:
            return _APIResult(
                text="",
                tool_calls=None,
                finish_reason=None,
                raw_finish_reason=None,
                finish_reason_present=False,
                finish_reason_source="missing",
                provider_response_id=str(data.get("id") or ""),
                transport_request_id=_provider_request_id(response),
                transport_state="complete_response_no_choices",
                wire_evidence=wire_evidence,
            )

        choice = choices[0]
        message = choice.get("message") or {}
        finish_reason_present = "finish_reason" in choice
        raw_finish_reason = choice.get("finish_reason")
        finish_reason = (
            str(raw_finish_reason).strip() or None
            if raw_finish_reason is not None
            else None
        )
        ai_text = _assistant_content_text(message.get("content"))
        audio = message.get("audio")
        audio_bytes = b""
        audio_transcript = ""
        if isinstance(audio, Mapping):
            audio_bytes = _decode_provider_audio(audio.get("data"))
            audio_transcript = str(audio.get("transcript") or "")
            if audio_transcript:
                ai_text = audio_transcript

        # Preserve only reasoning that the Provider actually exposed. Encrypted
        # reasoning is marked as such and never reconstructed.
        reasoning_fragments: list[str] = []
        encrypted_reasoning = False
        reasoning_text = str(message.get("reasoning") or "").strip()
        if reasoning_text:
            reasoning_fragments.append(reasoning_text)
            if on_stream_event is not None:
                await on_stream_event(
                    StreamEvent(
                        kind=KIND_THINKING,
                        summary=reasoning_text[:400],
                        raw_delta=reasoning_text,
                    )
                )
        for detail in message.get("reasoning_details") or []:
            if (
                isinstance(detail, Mapping)
                and str(detail.get("type") or "") == "reasoning.encrypted"
            ):
                encrypted_reasoning = True
            raw_detail = self._reasoning_detail_delta(detail)
            if raw_detail:
                reasoning_fragments.append(raw_detail)
            snippet = self._summarize_reasoning_detail(detail)
            if snippet and on_stream_event is not None:
                await on_stream_event(
                    StreamEvent(
                        kind=KIND_THINKING,
                        summary=snippet[:400],
                        raw_delta=raw_detail,
                    )
                )

        tool_calls = message.get("tool_calls") or None

        # Extract real token usage from API response
        usage = data.get("usage") or {}
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        thinking_tokens = _usage_thinking_tokens(usage)

        reasoning_content = "".join(reasoning_fragments)
        return _APIResult(
            text=ai_text, tool_calls=tool_calls, finish_reason=finish_reason,
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            thinking_tokens=thinking_tokens,
            cost_usd=_usage_cost_usd(usage),
            reasoning_content=reasoning_content,
            structured_data=_message_structured_data(message),
            audio_bytes=audio_bytes,
            audio_transcript=audio_transcript,
            raw_finish_reason=raw_finish_reason,
            finish_reason_present=finish_reason_present,
            finish_reason_source=(
                "provider"
                if finish_reason is not None
                else ("provider_null" if finish_reason_present else "missing")
            ),
            provider_response_id=str(data.get("id") or ""),
            transport_request_id=_provider_request_id(response),
            reasoning_state=(
                "available"
                if reasoning_content
                else ("encrypted" if encrypted_reasoning else "unavailable")
            ),
            wire_evidence=wire_evidence,
        )

    # ------------------------------------------------------------------
    # Streaming single API call (accumulates tool_calls deltas)
    # ------------------------------------------------------------------

    async def _stream_api_once(
        self,
        payload: dict,
        headers: dict,
        on_stream_event: StreamCallback,
    ) -> _APIResult:
        text_chunks: list[str] = []
        # tool_calls_acc: dict[int, dict] indexed by tool call index
        tool_calls_acc: dict[int, dict] = {}
        finish_reason = ""
        finish_reason_present = False
        finish_reason_field_seen = False
        raw_finish_reason: Any = None
        stream_usage: dict = {}  # usage from final streaming chunk
        saw_done = False
        provider_activity_observed = False
        provider_response_id = ""
        transport_request_id = ""
        reasoning_chunks: list[str] = []
        encrypted_reasoning = False
        audio_chunks: list[str] = []
        audio_transcript = ""
        audio_encoded_size = 0
        wire_evidence: dict[str, Any] = {
            "transport": "sse",
            "request": _request_wire_evidence(payload),
            "sse_events": [],
            "tool_call_fragments": [],
            "assembly_snapshots": [],
        }
        protocol_state: dict[str, Any] = {
            "raw_finish_reason_present": False,
            "raw_finish_reason": None,
            "finish_reason_source": "missing",
            "normalized_finish_reason": "incomplete",
            "provider_response_id": "",
            "transport_request_id": "",
            "text_provided": False,
            "text_length": 0,
            "reasoning_availability": "unavailable",
            "reasoning_length": 0,
            "tool_calls": [],
        }
        protocol_state["wire_evidence"] = wire_evidence

        async with self.client.stream(
            "POST",
            self._chat_completions_url(),
            json=payload,
            headers=headers,
        ) as response:
            await _read_http_error_body(response)
            response.raise_for_status()
            transport_request_id = _provider_request_id(response)
            protocol_state["transport_request_id"] = transport_request_id
            try:
                stream_request = response.request
            except (AttributeError, RuntimeError):
                # Minimal OpenAI-compatible clients and deterministic test
                # doubles may omit httpx's response.request metadata.  Error
                # events still need a concrete request for HTTPStatusError.
                stream_request = httpx.Request(
                    "POST", self._chat_completions_url()
                )
            wire_evidence["request"] = _request_wire_evidence(
                payload, stream_request
            )

            async for line in _iter_provider_stream_lines(response, protocol_state):
                self._touch_activity()
                event_arrival = len(wire_evidence["sse_events"]) + 1
                wire_evidence["sse_events"].append(
                    {"arrival": event_arrival, "raw_line": line}
                )

                if not line.startswith("data: "):
                    continue
                data_str = line[6:].strip()
                if data_str == "[DONE]":
                    saw_done = True
                    break

                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError as exc:
                    error = httpx.RemoteProtocolError(
                        "provider stream contained invalid JSON data"
                    )
                    raise _annotate_stream_exception(error, protocol_state) from exc
                if not isinstance(data, Mapping):
                    continue
                if data.get("id"):
                    provider_response_id = str(data.get("id"))
                    protocol_state["provider_response_id"] = provider_response_id

                stream_error = _stream_error_exception(
                    data,
                    request=stream_request,
                    provider_activity_observed=provider_activity_observed,
                )
                if stream_error is not None:
                    raise _annotate_stream_exception(
                        stream_error, protocol_state
                    )

                # Capture usage from streaming chunks (sent in final chunk)
                if data.get("usage"):
                    stream_usage = data["usage"]
                    provider_activity_observed = True

                choices = data.get("choices", [])
                if not choices:
                    continue

                choice = choices[0]
                delta = choice.get("delta", {})
                if "finish_reason" in choice:
                    finish_reason_field_seen = True
                    raw_finish_reason = choice.get("finish_reason")
                    if raw_finish_reason is not None and str(
                        raw_finish_reason
                    ).strip():
                        finish_reason = str(raw_finish_reason).strip()
                        finish_reason_present = True
                    protocol_state.update(
                        {
                            "raw_finish_reason_present": bool(
                                finish_reason_present
                            ),
                            "raw_finish_reason": (
                                finish_reason
                                if finish_reason_present
                                else raw_finish_reason
                            ),
                            "finish_reason_source": (
                                "provider"
                                if finish_reason_present
                                else "provider_null"
                            ),
                            "normalized_finish_reason": (
                                finish_reason.casefold()
                                if finish_reason_present
                                else "missing_finish_reason"
                            ),
                        }
                    )

                # Text content
                content = delta.get("content", "")
                reasoning_text = str(delta.get("reasoning") or "")
                reasoning_details = delta.get("reasoning_details") or []
                tool_call_deltas = delta.get("tool_calls") or []
                audio_delta = delta.get("audio")
                if (
                    content
                    or reasoning_text
                    or reasoning_details
                    or tool_call_deltas
                    or audio_delta
                    or finish_reason
                ):
                    provider_activity_observed = True

                if reasoning_text and on_stream_event:
                    reasoning_chunks.append(reasoning_text)
                    protocol_state["reasoning_availability"] = "available"
                    protocol_state["reasoning_length"] += len(reasoning_text)
                    await on_stream_event(
                        StreamEvent(
                            kind=KIND_THINKING,
                            summary=reasoning_text[:400],
                            raw_delta=reasoning_text,
                        )
                    )
                elif reasoning_text:
                    reasoning_chunks.append(reasoning_text)
                    protocol_state["reasoning_availability"] = "available"
                    protocol_state["reasoning_length"] += len(reasoning_text)
                elif reasoning_details:
                    for detail in reasoning_details:
                        if (
                            isinstance(detail, Mapping)
                            and str(detail.get("type") or "")
                            == "reasoning.encrypted"
                        ):
                            encrypted_reasoning = True
                            if not reasoning_chunks:
                                protocol_state["reasoning_availability"] = (
                                    "encrypted"
                                )
                            continue
                        raw_delta = self._reasoning_detail_delta(detail)
                        if raw_delta:
                            reasoning_chunks.append(raw_delta)
                            protocol_state["reasoning_availability"] = "available"
                            protocol_state["reasoning_length"] += len(raw_delta)
                            if on_stream_event:
                                await on_stream_event(
                                    StreamEvent(
                                        kind=KIND_THINKING,
                                        summary=raw_delta[:400],
                                        raw_delta=raw_delta,
                                    )
                                )
                            continue
                        snippet = self._summarize_reasoning_detail(detail)
                        if snippet and on_stream_event:
                            await on_stream_event(
                                StreamEvent(
                                    kind=KIND_THINKING,
                                    summary=snippet[:400],
                                )
                            )

                if content:
                    text_chunks.append(content)
                    protocol_state["text_provided"] = True
                    protocol_state["text_length"] += len(str(content))
                    if on_stream_event:
                        await on_stream_event(
                            StreamEvent(kind=KIND_TEXT_DELTA, summary=content)
                        )

                if isinstance(audio_delta, Mapping):
                    raw_chunk = audio_delta.get("data")
                    if raw_chunk:
                        if not isinstance(raw_chunk, str):
                            raise MultimodalContractError(
                                "provider audio data must be Base64 text",
                                code="INVALID_PROVIDER_AUDIO_OUTPUT",
                            )
                        audio_encoded_size += len(raw_chunk)
                        if audio_encoded_size > 90 * 1024 * 1024:
                            raise MultimodalContractError(
                                "provider audio output exceeds the 64 MiB limit",
                                code="AUDIO_OUTPUT_LIMIT_EXCEEDED",
                            )
                        audio_chunks.append(raw_chunk)
                    previous_transcript = audio_transcript
                    audio_transcript = _append_provider_transcript(
                        audio_transcript, audio_delta.get("transcript")
                    )
                    transcript_delta = audio_transcript[len(previous_transcript) :]
                    if transcript_delta and on_stream_event:
                        await on_stream_event(
                            StreamEvent(
                                kind=KIND_TEXT_DELTA,
                                summary=transcript_delta,
                            )
                        )

                # Accumulate tool_calls deltas
                for tc_delta in tool_call_deltas:
                    idx = tc_delta.get("index", 0)
                    if idx not in tool_calls_acc:
                        tool_calls_acc[idx] = {
                            "id": tc_delta.get("id", ""),
                            "type": tc_delta.get("type", "function"),
                            "function": {"name": "", "arguments": ""},
                        }
                    acc = tool_calls_acc[idx]
                    if tc_delta.get("id"):
                        acc["id"] = tc_delta["id"]
                    fn_delta = tc_delta.get("function", {})
                    wire_evidence["tool_call_fragments"].append(
                        {
                            "arrival": len(wire_evidence["tool_call_fragments"]) + 1,
                            "sse_event_arrival": event_arrival,
                            "index": idx,
                            "id_fragment": tc_delta.get("id", ""),
                            "type_fragment": tc_delta.get("type", ""),
                            "name_fragment": fn_delta.get("name", ""),
                            "arguments_fragment": fn_delta.get("arguments", ""),
                        }
                    )
                    if fn_delta.get("name"):
                        acc["function"]["name"] += fn_delta["name"]
                    if fn_delta.get("arguments"):
                        acc["function"]["arguments"] += fn_delta["arguments"]
                    wire_evidence["assembly_snapshots"].append(
                        {
                            "arrival": len(wire_evidence["assembly_snapshots"]) + 1,
                            "after_fragment": len(wire_evidence["tool_call_fragments"]),
                            "index": idx,
                            "assembled": json.loads(
                                json.dumps(acc, ensure_ascii=False)
                            ),
                        }
                    )
                if tool_calls_acc:
                    protocol_state["tool_calls"] = _tool_call_protocol_summary(
                        list(tool_calls_acc.values())
                    )[0]

        if not saw_done and not finish_reason:
            error = httpx.RemoteProtocolError(
                "provider stream ended without a completion marker"
            )
            raise _annotate_stream_exception(error, protocol_state)
        full_text = audio_transcript or "".join(text_chunks)
        tool_calls = list(tool_calls_acc.values()) if tool_calls_acc else None
        audio_bytes = b""
        if audio_chunks:
            try:
                audio_bytes = _decode_provider_audio("".join(audio_chunks))
            except MultimodalContractError:
                # Compatible providers either split one Base64 stream at
                # arbitrary boundaries or pad each SSE chunk independently.
                audio_bytes = b"".join(
                    _decode_provider_audio(chunk) for chunk in audio_chunks
                )
            if len(audio_bytes) > 64 * 1024 * 1024:
                raise MultimodalContractError(
                    "provider audio output exceeds the 64 MiB limit",
                    code="AUDIO_OUTPUT_LIMIT_EXCEEDED",
                )
        return _APIResult(
            text=full_text,
            tool_calls=tool_calls,
            finish_reason=finish_reason or None,
            prompt_tokens=stream_usage.get("prompt_tokens", 0),
            completion_tokens=stream_usage.get("completion_tokens", 0),
            thinking_tokens=_usage_thinking_tokens(stream_usage),
            cost_usd=_usage_cost_usd(stream_usage),
            reasoning_content="".join(reasoning_chunks),
            audio_bytes=audio_bytes,
            audio_transcript=audio_transcript,
            raw_finish_reason=(
                finish_reason if finish_reason_present else raw_finish_reason
            ),
            finish_reason_present=finish_reason_present,
            finish_reason_source=(
                "provider"
                if finish_reason_present
                else ("provider_null" if finish_reason_field_seen else "missing")
            ),
            provider_response_id=provider_response_id,
            transport_request_id=transport_request_id,
            transport_complete=True,
            transport_state=(
                "done_marker" if saw_done else "eof_after_finish_reason"
            ),
            stream_done=saw_done,
            stream_eof=not saw_done,
            reasoning_state=(
                "available"
                if reasoning_chunks
                else ("encrypted" if encrypted_reasoning else "unavailable")
            ),
            wire_evidence=wire_evidence,
        )

    # ------------------------------------------------------------------
    # Main generate_response with tool loop
    # ------------------------------------------------------------------

    async def generate_response(
        self,
        prompt: str,
        request_id: str,
        is_retry: bool = False,
        silent: bool = False,
        on_stream_event: StreamCallback = None,
        request_content: Mapping[str, Any] | None = None,
    ) -> BackendResponse:
        started = time.perf_counter()
        self._ensure_client()

        audio_output = None
        use_streaming = on_stream_event is not None

        headers = self._request_headers()

        last_text = ""
        last_structured_data = None
        # Accumulate token usage across all tool loops
        total_prompt = 0
        total_completion = 0
        total_thinking = 0
        total_cost_usd = 0.0
        provider_call_count = 0
        provider_cost_complete = True
        provider_calls: list[dict[str, Any]] = []
        provider_transport_retry_count = 0
        provider_attempt_serial = 0
        total_tool_calls = 0
        tool_loop_count = 0
        media_routing: tuple[dict[str, Any], ...] = ()
        media_fallback_attempted = False
        last_audio_bytes = b""
        last_audio_transcript = ""
        input_derivatives: tuple[Path, ...] = ()
        input_normalization: tuple[dict[str, Any], ...] = ()
        terminal_decision: dict[str, Any] | None = None
        last_provider_call_record: dict[str, Any] | None = None
        completed_tool_calls: list[dict[str, str]] = []
        repair_attempt_for_incident = 0
        repair_incident = 0
        recovery_attempts_used = 0
        active_forensic_path: Path | None = None
        last_forensic_path: Path | None = None
        total_tool_repair_requests = 0
        total_local_recovery_requests = 0

        try:
            self._touch_activity()
            audio_output = self._native_audio_output_profile(request_content)
            use_streaming = use_streaming or audio_output is not None
            (
                provider_request_content,
                input_derivatives,
                input_normalization,
            ) = await self._prepare_audio_input_formats(request_content)
            messages, media_routing = self._initial_messages(
                prompt, provider_request_content
            )
            self._last_media_routing = media_routing
            normalized_request_content = normalize_request_content(
                provider_request_content
            )
            manifest = attachment_manifest(normalized_request_content)
            native_attachment_ids = {
                str(item.get("attachment_id") or "")
                for item in media_routing
                if str(item.get("route") or "") == "native"
            }
            native_local_refs = native_attachment_reference_aliases(
                manifest,
                native_attachment_ids,
            )
            all_media_native = bool(media_routing) and all(
                str(item.get("route") or "") == "native" for item in media_routing
            )

            for loop_idx in count():
                provider_call_retry_count = 0
                next_provider_recovery_kind = "none"
                while True:
                    payload = self._build_payload(
                        messages,
                        use_streaming=use_streaming,
                        excluded_tool_names=(
                            _MEDIA_FALLBACK_TOOL_NAMES
                            if all_media_native
                            else frozenset()
                        ),
                        audio_output=audio_output,
                        allow_tools=bool(
                            audio_output is None or audio_output.get("tools")
                        ),
                    )
                    provider_call_emitted_text = False
                    effective_parameters = _effective_protocol_parameters(payload)

                    async def _capture_provider_call(event: StreamEvent) -> None:
                        nonlocal provider_call_emitted_text
                        if event.kind == KIND_TEXT_DELTA and (
                            event.raw_delta or event.summary
                        ):
                            provider_call_emitted_text = True
                        if on_stream_event is not None:
                            await on_stream_event(event)

                    call_stream_callback = (
                        _capture_provider_call
                        if on_stream_event is not None
                        else None
                    )
                    provider_attempt_serial += 1
                    provider_call_started = time.perf_counter()
                    provider_call_started_at = _utc_timestamp()
                    provider_attempt = provider_call_retry_count + 1
                    provider_recovery_kind = next_provider_recovery_kind
                    self._active_provider_wire_context = {
                        "request_id": str(request_id or ""),
                        "call_serial": provider_attempt_serial,
                    }
                    provider_wire_refs = [
                        self._record_provider_wire_evidence(
                            "request_prepared",
                            request_id=request_id,
                            call_serial=provider_attempt_serial,
                            payload={
                                "method": "POST",
                                "url": self._provider_evidence_url(),
                                "headers": _diagnostic_headers(headers),
                                "streaming": bool(use_streaming),
                                "body": payload,
                                "body_sha256": hashlib.sha256(
                                    json.dumps(
                                        payload,
                                        ensure_ascii=False,
                                        sort_keys=True,
                                        separators=(",", ":"),
                                        default=str,
                                    ).encode("utf-8")
                                ).hexdigest(),
                            },
                        )
                    ]
                    try:
                        if use_streaming:
                            result = await self._stream_api_once(
                                payload,
                                headers,
                                call_stream_callback,
                            )
                        else:
                            result = await self._call_api_once(
                                payload,
                                headers,
                                call_stream_callback,
                            )
                    except asyncio.CancelledError as exc:
                        provider_wire_refs.append(
                            self._record_provider_wire_evidence(
                                "request_cancelled",
                                request_id=request_id,
                                call_serial=provider_attempt_serial,
                                payload={
                                    "partial_protocol": dict(
                                        getattr(
                                            exc, "hashi_provider_protocol", {}
                                        )
                                        or {}
                                    )
                                },
                            )
                        )
                        partial_protocol = dict(
                            getattr(exc, "hashi_provider_protocol", {}) or {}
                        )
                        partial_protocol.pop("wire_evidence", None)
                        provider_calls.append(
                            self._provider_call_record(
                                request_id=request_id,
                                serial=provider_attempt_serial,
                                payload={
                                    "input": 0,
                                    "output": 0,
                                    "thinking": 0,
                                    "token_source": "unknown",
                                    "thinking_in_output": False,
                                    "cost_usd": None,
                                    "prompt_cache_hit_tokens": None,
                                    "prompt_cache_miss_tokens": None,
                                    "provider_call_latency_ms": round(
                                        (time.perf_counter() - provider_call_started)
                                        * 1000,
                                        3,
                                    ),
                                    "attempt": provider_attempt,
                                    "retry_count": provider_call_retry_count,
                                    "recovery_kind": provider_recovery_kind,
                                    "status": "cancelled",
                                    "request_started_at": provider_call_started_at,
                                    "response_observed_at": _utc_timestamp(),
                                    "effective_parameters": effective_parameters,
                                    "transport_complete": False,
                                    "transport_state": "cancelled",
                                    "stream_done": False if use_streaming else None,
                                    "stream_eof": False,
                                    "stream_truncated": bool(use_streaming),
                                    "raw_finish_reason_present": False,
                                    "raw_finish_reason": None,
                                    "finish_reason_source": "missing",
                                    "normalized_finish_reason": "cancelled",
                                    "reasoning_availability": "unknown",
                                    "decision": "cancel",
                                    "decision_reason": "request_cancelled",
                                    "decision_success": False,
                                    "provider_wire_evidence_refs": list(
                                        provider_wire_refs
                                    ),
                                    **partial_protocol,
                                },
                            )
                        )
                        raise
                    except Exception as exc:
                        if not isinstance(exc, ProviderProtocolForensicError):
                            provider_wire_refs.append(
                                self._record_provider_wire_evidence(
                                    "request_failed",
                                    request_id=request_id,
                                    call_serial=provider_attempt_serial,
                                    payload={
                                        "error_type": type(exc).__name__,
                                        "error": str(exc),
                                        "http": _provider_http_failure_diagnostics(
                                            exc
                                        ),
                                        "partial_protocol": dict(
                                            getattr(
                                                exc,
                                                "hashi_provider_protocol",
                                                {},
                                            )
                                            or {}
                                        ),
                                    },
                                )
                            )
                        can_media_fallback = self._can_replay_typed_media_fallback(
                            exc,
                            media_routing=media_routing,
                            fallback_attempted=media_fallback_attempted,
                            provider_call_count=provider_call_count,
                            tool_call_count=total_tool_calls,
                        )
                        retry_limit = min(
                            INVALID_TOOL_CALL_REPAIR_LIMIT,
                            max(0, int(self.TRANSIENT_PROVIDER_CALL_RETRIES)),
                        )
                        can_transport_retry = bool(
                            not can_media_fallback
                            and recovery_attempts_used < retry_limit
                            and not provider_call_emitted_text
                            and _transient_provider_call_error(exc)
                        )
                        partial_protocol = dict(
                            getattr(exc, "hashi_provider_protocol", {}) or {}
                        )
                        partial_protocol.pop("wire_evidence", None)
                        failure_decision = (
                            "retry_with_typed_media_fallback"
                            if can_media_fallback
                            else (
                                "retry_unfinished_provider_call"
                                if can_transport_retry
                                else "return_provider_failure"
                            )
                        )
                        provider_calls.append(
                            self._provider_call_record(
                                request_id=request_id,
                                serial=provider_attempt_serial,
                                payload={
                                    "input": 0,
                                    "output": 0,
                                    "thinking": 0,
                                    "token_source": "unknown",
                                    "thinking_in_output": False,
                                    "cost_usd": None,
                                    "prompt_cache_hit_tokens": None,
                                    "prompt_cache_miss_tokens": None,
                                    "provider_call_latency_ms": round(
                                        (time.perf_counter() - provider_call_started)
                                        * 1000,
                                        3,
                                    ),
                                    "attempt": provider_attempt,
                                    "retry_count": provider_call_retry_count,
                                    "recovery_kind": provider_recovery_kind,
                                    "status": (
                                        "failed_after_partial_response"
                                        if partial_protocol
                                        or provider_call_emitted_text
                                        or bool(
                                            getattr(
                                                exc,
                                                "provider_activity_observed",
                                                False,
                                            )
                                        )
                                        else "failed_without_receipt"
                                    ),
                                    "request_started_at": provider_call_started_at,
                                    "response_observed_at": _utc_timestamp(),
                                    "effective_parameters": effective_parameters,
                                    "transport_complete": False,
                                    "transport_state": "provider_call_failed",
                                    "stream_done": False if use_streaming else None,
                                    "stream_eof": False,
                                    "stream_truncated": bool(use_streaming),
                                    "raw_finish_reason_present": False,
                                    "raw_finish_reason": None,
                                    "finish_reason_source": "missing",
                                    "normalized_finish_reason": "incomplete",
                                    "reasoning_availability": "unknown",
                                    "decision": failure_decision,
                                    "decision_reason": type(exc).__name__,
                                    "decision_success": False,
                                    "provider_wire_evidence_refs": list(
                                        provider_wire_refs
                                    ),
                                    **partial_protocol,
                                },
                            )
                        )
                        if can_media_fallback:
                            media_fallback_attempted = True
                            self._enable_request_local_media_fallback(
                                native_attachment_ids
                            )
                            messages = self._typed_media_fallback_messages(
                                prompt,
                                provider_request_content,
                            )
                            media_routing = self._typed_media_fallback_routing(
                                media_routing
                            )
                            self._last_media_routing = media_routing
                            native_attachment_ids = set()
                            native_local_refs = set()
                            all_media_native = False
                            next_provider_recovery_kind = "typed_media_fallback"
                            continue
                        if can_transport_retry:
                            provider_call_retry_count += 1
                            recovery_attempts_used += 1
                            total_local_recovery_requests += 1
                            provider_transport_retry_count += 1
                            next_provider_recovery_kind = (
                                "provider_transport_retry"
                            )
                            await asyncio.sleep(
                                _provider_call_retry_delay(
                                    exc,
                                    default_s=(
                                        self.TRANSIENT_PROVIDER_CALL_RETRY_DELAY_S
                                    ),
                                    maximum_s=(
                                        self.TRANSIENT_PROVIDER_CALL_RETRY_MAX_DELAY_S
                                    ),
                                )
                            )
                            continue
                        raise
                    provider_call_latency_ms = round(
                        (time.perf_counter() - provider_call_started) * 1000,
                        3,
                    )
                    provider_wire_refs.append(
                        self._record_provider_wire_evidence(
                            "response_received",
                            request_id=request_id,
                            call_serial=provider_attempt_serial,
                            payload={
                                "wire": dict(result.wire_evidence or {}),
                                "provider_response_id": str(
                                    result.provider_response_id or ""
                                ),
                                "transport_request_id": str(
                                    result.transport_request_id or ""
                                ),
                                "transport_state": str(
                                    result.transport_state or ""
                                ),
                            },
                        )
                    )
                    break

                # Accumulate usage from each API call
                total_prompt += result.prompt_tokens
                total_completion += result.completion_tokens
                total_thinking += result.thinking_tokens
                provider_call_count += 1
                decision = _provider_response_decision(
                    result,
                    tool_registry_available=self.tool_registry is not None,
                )
                invalid_tool_response = (
                    str(decision.get("error_code") or "")
                    == "PROVIDER_INVALID_TOOL_CALLS"
                )
                if invalid_tool_response and active_forensic_path is None:
                    repair_incident += 1
                next_repair_number = (
                    recovery_attempts_used + 1
                    if invalid_tool_response
                    and recovery_attempts_used < INVALID_TOOL_CALL_REPAIR_LIMIT
                    else None
                )
                repair_record: dict[str, Any] = {}
                if invalid_tool_response:
                    repair_record = {
                        "incident": repair_incident,
                        "response_to_request": (
                            f"{repair_attempt_for_incident}/{INVALID_TOOL_CALL_REPAIR_LIMIT}"
                            if repair_attempt_for_incident
                            else None
                        ),
                        "next_request": (
                            f"{next_repair_number}/{INVALID_TOOL_CALL_REPAIR_LIMIT}"
                            if next_repair_number is not None
                            else None
                        ),
                        "status": (
                            "retrying"
                            if next_repair_number is not None
                            else "exhausted"
                        ),
                    }
                elif repair_attempt_for_incident:
                    repair_record = {
                        "incident": repair_incident,
                        "response_to_request": (
                            f"{repair_attempt_for_incident}/{INVALID_TOOL_CALL_REPAIR_LIMIT}"
                        ),
                        "next_request": None,
                        "status": "repaired",
                    }
                last_provider_call_record = self._provider_call_record(
                    request_id=request_id,
                    serial=provider_attempt_serial,
                    payload={
                        "input": int(result.prompt_tokens or 0),
                        "output": int(result.completion_tokens or 0),
                        "thinking": int(result.thinking_tokens or 0),
                        "token_source": "provider",
                        # OpenRouter reasoning_tokens is a detail within
                        # completion_tokens, not an additional token bucket.
                        "thinking_in_output": True,
                        "cost_usd": result.cost_usd,
                        "prompt_cache_hit_tokens": _optional_usage_token_count(
                            getattr(result, "prompt_cache_hit_tokens", None)
                        ),
                        "prompt_cache_miss_tokens": _optional_usage_token_count(
                            getattr(result, "prompt_cache_miss_tokens", None)
                        ),
                        # One physical call only; retries have their own row.
                        "provider_call_latency_ms": provider_call_latency_ms,
                        "attempt": provider_attempt,
                        "retry_count": provider_call_retry_count,
                        "recovery_kind": provider_recovery_kind,
                        "status": "completed",
                        "provider_wire_evidence_refs": list(
                            provider_wire_refs
                        ),
                        "request_started_at": provider_call_started_at,
                        "response_observed_at": _utc_timestamp(),
                        "effective_parameters": effective_parameters,
                        **_response_protocol_record(result, decision),
                        **(
                            {"tool_call_repair": repair_record}
                            if repair_record
                            else {}
                        ),
                    },
                )
                provider_calls.append(last_provider_call_record)
                if result.cost_usd is None:
                    provider_cost_complete = False
                else:
                    total_cost_usd += result.cost_usd

                last_text = result.text
                last_structured_data = result.structured_data
                last_audio_bytes = result.audio_bytes
                last_audio_transcript = result.audio_transcript

                if invalid_tool_response:
                    active_forensic_path, tool_details = (
                        self._write_invalid_tool_call_forensic(
                            path=active_forensic_path,
                            request_id=request_id,
                            call_serial=provider_attempt_serial,
                            payload=payload,
                            result=result,
                            provider_call_record=last_provider_call_record,
                            repair_response_number=repair_attempt_for_incident,
                            next_repair_number=next_repair_number,
                            incident_number=repair_incident,
                        )
                    )
                    last_forensic_path = active_forensic_path
                    if next_repair_number is not None:
                        rejected_assistant = {
                            "role": "assistant",
                            "content": json.dumps(
                                {
                                    "hashi_rejected_tool_call_batch": result.tool_calls,
                                    "reason": "invalid tool arguments JSON",
                                    "executed": False,
                                },
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        }
                        # A synthetic repair turn is still the assistant turn
                        # returned by the Provider. Provider-specific required
                        # fields (notably DeepSeek reasoning_content) must use
                        # the same augmentation path as a valid tool turn.
                        self._augment_assistant_tool_message(
                            rejected_assistant, result
                        )
                        messages.append(rejected_assistant)
                        messages.append(
                            {
                                "role": "system",
                                "content": _invalid_tool_repair_prompt(
                                    tool_details,
                                    repair_number=next_repair_number,
                                    completed_tool_calls=completed_tool_calls,
                                ),
                            }
                        )
                        repair_attempt_for_incident = next_repair_number
                        recovery_attempts_used = next_repair_number
                        total_tool_repair_requests += 1
                        total_local_recovery_requests += 1
                        continue

                    provider_request_id = str(
                        result.provider_response_id
                        or result.transport_request_id
                        or last_provider_call_record.get("provider_request_id")
                        or ""
                    )
                    terminal_decision = {
                        **dict(decision),
                        "repair_attempts_exhausted": INVALID_TOOL_CALL_REPAIR_LIMIT,
                        "invalid_tool_names": [
                            str(item.get("name") or "<missing>")
                            for item in tool_details
                            if str(item.get("arguments_state") or "")
                            != "valid_object"
                        ],
                        "provider_request_id": provider_request_id,
                        "provider_protocol_forensic_path": str(
                            active_forensic_path
                        ),
                    }
                    last_text = ""
                    break

                if repair_attempt_for_incident:
                    repair_attempt_for_incident = 0
                    active_forensic_path = None
                recovery_attempts_used = 0

                # The protocol decision is persisted synchronously above. No
                # tool side effect may occur before that durable boundary.
                if not bool(decision.get("execute_tools")):
                    terminal_decision = dict(decision)
                    break

                tool_loop_count += 1
                total_tool_calls += len(result.tool_calls)
                self.logger.debug(
                    f"Tool loop {loop_idx + 1}: {len(result.tool_calls)} tool call(s)"
                )

                # Append assistant message (with tool_calls) to conversation
                assistant_msg: dict = {"role": "assistant"}
                if result.text:
                    assistant_msg["content"] = result.text
                self._augment_assistant_tool_message(assistant_msg, result)
                assistant_msg["tool_calls"] = result.tool_calls
                messages.append(assistant_msg)

                # Execute tools, append results
                await self._run_tool_calls(
                    result.tool_calls,
                    messages,
                    on_stream_event,
                    native_attachment_ids=native_attachment_ids,
                    native_local_refs=native_local_refs,
                    all_media_native=all_media_native,
                    provider_call_context={
                        "provider_call_serial": provider_attempt_serial,
                        "provider_request_id": last_provider_call_record.get(
                            "provider_request_id", ""
                        ),
                    },
                )
                completed_tool_calls.extend(
                    {
                        "id": str(call.get("id") or ""),
                        "name": str((call.get("function") or {}).get("name") or ""),
                    }
                    for call in result.tool_calls
                    if isinstance(call, Mapping)
                )

            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            from adapters.base import TokenUsage
            usage = TokenUsage(
                input_tokens=total_prompt,
                output_tokens=total_completion,
                thinking_tokens=total_thinking,
            ) if (total_prompt or total_completion) else None
            protocol_success = bool(
                terminal_decision and terminal_decision.get("success")
            )
            protocol_error_code = str(
                (terminal_decision or {}).get("error_code") or ""
            )
            protocol_error = (
                _provider_protocol_error_message(protocol_error_code)
                if not protocol_success
                else None
            )
            if (
                protocol_error_code == "PROVIDER_INVALID_TOOL_CALLS"
                and last_forensic_path is not None
            ):
                protocol_error = _invalid_tool_user_error(
                    tool_details,
                    provider_request_id=str(
                        (terminal_decision or {}).get("provider_request_id") or ""
                    ),
                    forensic_path=last_forensic_path,
                )
            if protocol_success and audio_output is not None and not last_audio_bytes:
                raise MultimodalContractError(
                    "provider completed a native voice request without audio output",
                    code="PROVIDER_AUDIO_OUTPUT_MISSING",
                )
            output_content: tuple[Mapping[str, Any], ...] = ()
            native_audio_metadata: dict[str, Any] | None = None
            if protocol_success and last_audio_bytes:
                if audio_output is None:
                    raise MultimodalContractError(
                        "provider returned audio without an authorized audio output profile",
                        code="UNEXPECTED_PROVIDER_AUDIO_OUTPUT",
                    )
                asset_payload = last_audio_bytes
                if str(audio_output["format"]) == "pcm16":
                    asset_payload = _pcm16_to_wav(
                        asset_payload,
                        sample_rate_hz=int(audio_output["pcm_sample_rate_hz"]),
                        channels=int(audio_output["pcm_channels"]),
                    )
                asset = self._native_audio_asset_store_instance().create(
                    asset_payload,
                    owner_id="",
                    session_id="",
                    direction="output",
                    mime_type=str(audio_output["mime_type"]),
                    audio_format=str(audio_output["asset_format"]),
                    retention_seconds=int(audio_output["retention_seconds"]),
                    retention_indefinite=bool(
                        audio_output["retention_indefinite"]
                    ),
                    correlation={
                        "request_id": str(
                            dict(getattr(self.config, "extra", {}) or {}).get(
                                "_native_audio_claim_request_id"
                            )
                            or request_id
                        ),
                        "provider": "openrouter-api",
                        "model": str(self.config.model),
                    },
                )
                transcript = str(last_audio_transcript or last_text or "").strip()
                parts: list[Mapping[str, Any]] = []
                if transcript:
                    parts.append(
                        {
                            "type": "text",
                            "text": transcript,
                            "provenance": "provider_audio_transcript",
                        }
                    )
                    last_text = transcript
                else:
                    await self._emit(
                        on_stream_event,
                        "voice_warning",
                        (
                            "The native voice reply has no text transcript; "
                            "audio is still available."
                        ),
                        metadata={
                            "warning_code": "provider_output_transcript_unavailable"
                        },
                        delivery_class=DELIVERY_USER_COMMENTARY,
                    )
                parts.append(
                    {
                        "type": "audio",
                        "asset_id": asset["asset_id"],
                        "mime_type": asset["mime_type"],
                        "format": asset["format"],
                        "duration_ms": asset.get("duration_ms"),
                        "size_bytes": asset["size_bytes"],
                        "sha256": asset["sha256"],
                        "retention_expires_at": asset.get(
                            "retention_expires_at"
                        ),
                        "retention_indefinite": asset[
                            "retention_indefinite"
                        ],
                    }
                )
                output_content = tuple(parts)
                native_audio_metadata = {
                    "asset_id": asset["asset_id"],
                    "provider": "openrouter-api",
                    "model": str(self.config.model),
                    "voice": str(audio_output["voice"]),
                    "provider_format": str(audio_output["format"]),
                    "format": str(audio_output["asset_format"]),
                    "tools_enabled": bool(audio_output["tools"]),
                    "claimed": False,
                }
            return BackendResponse(
                text=last_text,
                duration_ms=duration_ms,
                structured_data=last_structured_data,
                is_success=protocol_success,
                error=protocol_error,
                error_code=protocol_error_code or None,
                error_retryable=(
                    bool((terminal_decision or {}).get("error_retryable"))
                    if not protocol_success
                    else None
                ),
                stop_reason=str(
                    (terminal_decision or {}).get("normalized_finish_reason")
                    or "unknown"
                ),
                usage=usage,
                cost_usd=(
                    round(total_cost_usd, 12)
                    if provider_call_count and provider_cost_complete
                    else None
                ),
                stream_metadata={
                    "provider_failure_description": (
                        protocol_error if not protocol_success else None
                    ),
                    "meter": {"provider_calls": provider_calls},
                    "provider_transport_retry_count": (
                        provider_transport_retry_count
                    ),
                    "provider_tool_repair_count": total_tool_repair_requests,
                    "provider_local_recovery_count": (
                        total_local_recovery_requests
                    ),
                    "provider_local_recovery_limit": (
                        INVALID_TOOL_CALL_REPAIR_LIMIT
                    ),
                    "provider_protocol_forensic_path": (
                        str(last_forensic_path) if last_forensic_path else None
                    ),
                    "multimodal_routing": list(media_routing),
                    "multimodal_fallback_attempted": media_fallback_attempted,
                    "native_audio": native_audio_metadata,
                    "audio_input_normalization": list(input_normalization),
                    "provider_protocol": dict(terminal_decision or {}),
                },
                tool_call_count=total_tool_calls,
                tool_loop_count=tool_loop_count,
                content=output_content,
                provider_request_id=(
                    str(
                        (last_provider_call_record or {}).get(
                            "provider_response_id"
                        )
                        or (last_provider_call_record or {}).get(
                            "transport_request_id"
                        )
                        or (last_provider_call_record or {}).get(
                            "provider_request_id"
                        )
                        or ""
                    )
                    or None
                ),
                side_effects_possible=bool(
                    not protocol_success and total_tool_calls
                ),
            )

        except asyncio.CancelledError:
            self.logger.warning(f"Request cancelled for {request_id}")
            raise
        except ProviderCallObserverError:
            raise
        except Exception as e:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            failure = _backend_failure_response(
                e,
                duration_ms=duration_ms,
                tool_call_count=total_tool_calls,
                tool_loop_count=tool_loop_count,
            )
            metadata = dict(failure.stream_metadata or {})
            metadata["provider_transport_retry_count"] = (
                provider_transport_retry_count
            )
            metadata["provider_local_recovery_count"] = (
                total_local_recovery_requests
            )
            metadata["provider_local_recovery_limit"] = (
                INVALID_TOOL_CALL_REPAIR_LIMIT
            )
            if recovery_attempts_used >= INVALID_TOOL_CALL_REPAIR_LIMIT:
                # The Adapter already consumed the one recovery budget for
                # this unfinished interaction. Do not let HER allocate a new
                # outer-stage budget merely because the final typed failure is
                # normally transient.
                failure.error_retryable = False
                metadata["provider_local_recovery_exhausted"] = True
            metadata["meter"] = {"provider_calls": provider_calls}
            metadata["multimodal_routing"] = list(media_routing)
            metadata["multimodal_fallback_attempted"] = media_fallback_attempted
            if isinstance(e, MultimodalContractError) and e.attachment_id:
                metadata["attachment_id"] = e.attachment_id
            failure.stream_metadata = metadata
            return failure
        finally:
            for derivative in input_derivatives:
                derivative.unlink(missing_ok=True)

    async def shutdown(self):
        if self.client is not None and not getattr(self.client, "is_closed", False):
            await self.client.aclose()
        self.client = None
