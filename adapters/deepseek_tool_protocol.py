"""Bounded DeepSeek DSML compatibility at the Model Provider boundary.

The public DeepSeek API promises OpenAI-style message.tool_calls. V3.2, V4,
and V4.1 nevertheless have documented model-native DSML encodings which can
occasionally leak into assistant text when the Provider parser misses them.
This module recognises only complete official dialects. Degraded markers are
classified for a native-channel repair request, never executed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from jsonschema import Draft7Validator
from jsonschema.exceptions import SchemaError


MAX_TEXT_TOOL_CHARACTERS = 256 * 1024
MAX_TOOL_CALLS = 32
MAX_TOOL_PARAMETERS = 128
MAX_TAG_HEADER_CHARACTERS = 1024


@dataclass(frozen=True)
class _Dialect:
    name: str
    calls: str
    invoke: str
    parameter: str


_DIALECTS = (
    _Dialect("v4.1", "｜DSML｜ calls", "｜DSML｜ invoke", "｜DSML｜ parameter"),
    _Dialect("v4", "｜DSML｜tool_calls", "｜DSML｜invoke", "｜DSML｜parameter"),
    _Dialect(
        "v3.2",
        "｜DSML｜function_calls",
        "｜DSML｜invoke",
        "｜DSML｜parameter",
    ),
)

_CANONICAL_STARTS = tuple(f"<{dialect.calls}>" for dialect in _DIALECTS)
_DEGRADED_STARTS = (
    "<function_calls",
    "<tool_calls",
    "<invoke",
    "<｜dsml｜",
    "<|dsml|",
    "<||dsml||",
    "<｜｜dsml｜｜",
)
_STREAM_CANDIDATE_STARTS = tuple(
    marker.casefold() for marker in (*_CANONICAL_STARTS, *_DEGRADED_STARTS)
)


@dataclass(frozen=True)
class DeepSeekTextToolInspection:
    status: str
    dialect: str = ""
    tool_calls: tuple[dict[str, Any], ...] = ()
    issue: str = ""
    candidate_tool_name: str = ""


class _DSMLParseError(ValueError):
    def __init__(self, issue: str, *, candidate_tool_name: str = "") -> None:
        super().__init__(issue)
        self.issue = issue
        self.candidate_tool_name = candidate_tool_name


def _skip_whitespace(text: str, index: int) -> int:
    while index < len(text) and text[index].isspace():
        index += 1
    return index


def _parse_attributes(fragment: str) -> dict[str, str]:
    attributes: dict[str, str] = {}
    index = 0
    while True:
        index = _skip_whitespace(fragment, index)
        if index == len(fragment):
            return attributes
        start = index
        while index < len(fragment) and (
            fragment[index].isalnum() or fragment[index] in {"_", "-", ":"}
        ):
            index += 1
        if index == start:
            raise _DSMLParseError("invalid_attribute_name")
        name = fragment[start:index]
        index = _skip_whitespace(fragment, index)
        if index >= len(fragment) or fragment[index] != "=":
            raise _DSMLParseError("attribute_missing_equals")
        index = _skip_whitespace(fragment, index + 1)
        if index >= len(fragment) or fragment[index] not in {'"', "'"}:
            raise _DSMLParseError("attribute_value_not_quoted")
        quote = fragment[index]
        value_start = index + 1
        value_end = fragment.find(quote, value_start)
        if value_end < 0:
            raise _DSMLParseError("attribute_quote_unclosed")
        if name in attributes:
            raise _DSMLParseError("duplicate_attribute")
        attributes[name] = fragment[value_start:value_end]
        index = value_end + 1


def _opening_tag(
    text: str,
    index: int,
    tag_name: str,
    required_attributes: frozenset[str],
) -> tuple[int, dict[str, str]]:
    prefix = f"<{tag_name}"
    if not text.startswith(prefix, index):
        raise _DSMLParseError("unexpected_opening_tag")
    header_end = text.find(
        ">",
        index + len(prefix),
        min(len(text), index + MAX_TAG_HEADER_CHARACTERS),
    )
    if header_end < 0:
        raise _DSMLParseError("tag_header_unclosed")
    attributes = _parse_attributes(text[index + len(prefix) : header_end])
    if frozenset(attributes) != required_attributes:
        raise _DSMLParseError("unexpected_attributes")
    return header_end + 1, attributes


def _offered_tools(
    tools: Sequence[Mapping[str, Any]],
) -> dict[str, tuple[str, str | None, Mapping[str, Any]]]:
    offered: dict[str, tuple[str, str | None, Mapping[str, Any]]] = {}
    for item in tools:
        if not isinstance(item, Mapping):
            continue
        function = item.get("function")
        if not isinstance(function, Mapping):
            continue
        name = str(function.get("name") or "").strip()
        if not name:
            continue
        namespace_value = item.get("namespace", function.get("namespace"))
        if isinstance(namespace_value, Mapping):
            namespace_value = namespace_value.get("name")
        namespace = str(namespace_value or "").strip() or None
        encoded_name = name
        if namespace and "::" not in encoded_name:
            encoded_name = f"{namespace}::{encoded_name}"
        schema = function.get("parameters")
        offered[encoded_name] = (
            name.partition("::")[2] if "::" in name else name,
            namespace or (name.partition("::")[0] if "::" in name else None),
            schema if isinstance(schema, Mapping) else {"type": "object"},
        )
    return offered


def _validate_arguments(
    arguments: Mapping[str, Any],
    schema: Mapping[str, Any],
    *,
    tool_name: str,
) -> None:
    try:
        Draft7Validator.check_schema(dict(schema))
        validator = Draft7Validator(dict(schema))
    except (SchemaError, RecursionError) as exc:
        raise _DSMLParseError(
            "advertised_tool_schema_invalid",
            candidate_tool_name=tool_name,
        ) from exc
    try:
        validation_error = next(validator.iter_errors(dict(arguments)), None)
    except RecursionError as exc:
        raise _DSMLParseError(
            "arguments_validation_too_deep",
            candidate_tool_name=tool_name,
        ) from exc
    if validation_error is not None:
        raise _DSMLParseError(
            "arguments_do_not_match_advertised_schema",
            candidate_tool_name=tool_name,
        )


def _parse_canonical_dsml(
    text: str,
    dialect: _Dialect,
    tools: Sequence[Mapping[str, Any]],
    *,
    response_id: str,
) -> tuple[dict[str, Any], ...]:
    offered = _offered_tools(tools)
    start_tag = f"<{dialect.calls}>"
    end_tag = f"</{dialect.calls}>"
    invoke_end = f"</{dialect.invoke}>"
    parameter_end = f"</{dialect.parameter}>"
    index = len(start_tag)
    calls: list[dict[str, Any]] = []
    text_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()

    while True:
        index = _skip_whitespace(text, index)
        if text.startswith(end_tag, index):
            index += len(end_tag)
            break
        if len(calls) >= MAX_TOOL_CALLS:
            raise _DSMLParseError("too_many_tool_calls")
        index, invoke_attributes = _opening_tag(
            text,
            index,
            dialect.invoke,
            frozenset({"name"}),
        )
        encoded_name = invoke_attributes["name"].strip()
        if not encoded_name or len(encoded_name) > 128:
            raise _DSMLParseError("invalid_tool_name")
        if encoded_name not in offered:
            raise _DSMLParseError(
                "tool_was_not_advertised",
                candidate_tool_name=encoded_name,
            )
        execution_name, namespace, schema = offered[encoded_name]
        arguments: dict[str, Any] = {}

        while True:
            index = _skip_whitespace(text, index)
            if text.startswith(invoke_end, index):
                index += len(invoke_end)
                break
            if len(arguments) >= MAX_TOOL_PARAMETERS:
                raise _DSMLParseError(
                    "too_many_tool_parameters",
                    candidate_tool_name=encoded_name,
                )
            index, parameter_attributes = _opening_tag(
                text,
                index,
                dialect.parameter,
                frozenset({"name", "string"}),
            )
            parameter_name = parameter_attributes["name"].strip()
            string_flag = parameter_attributes["string"]
            if not parameter_name or len(parameter_name) > 128:
                raise _DSMLParseError(
                    "invalid_parameter_name",
                    candidate_tool_name=encoded_name,
                )
            if parameter_name in arguments:
                raise _DSMLParseError(
                    "duplicate_parameter",
                    candidate_tool_name=encoded_name,
                )
            if string_flag not in {"true", "false"}:
                raise _DSMLParseError(
                    "invalid_string_flag",
                    candidate_tool_name=encoded_name,
                )
            value_end = text.find(parameter_end, index)
            if value_end < 0:
                raise _DSMLParseError(
                    "parameter_tag_unclosed",
                    candidate_tool_name=encoded_name,
                )
            raw_value = text[index:value_end]
            if string_flag == "true":
                value: Any = raw_value
            else:
                try:
                    value = json.loads(raw_value)
                except (json.JSONDecodeError, RecursionError) as exc:
                    raise _DSMLParseError(
                        "non_string_parameter_is_not_json",
                        candidate_tool_name=encoded_name,
                    ) from exc
            arguments[parameter_name] = value
            index = value_end + len(parameter_end)

        _validate_arguments(arguments, schema, tool_name=encoded_name)
        digest = hashlib.sha256(
            (
                f"{response_id}\0{dialect.name}\0{len(calls)}\0{text_sha256}"
            ).encode("utf-8")
        ).hexdigest()
        call: dict[str, Any] = {
            "id": f"call_dsml_{digest[:24]}",
            "type": "function",
            "function": {
                "name": execution_name,
                "arguments": json.dumps(
                    arguments,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        }
        if namespace:
            call["namespace"] = namespace
        calls.append(call)

    index = _skip_whitespace(text, index)
    if index != len(text):
        raise _DSMLParseError("unexpected_text_after_tool_calls")
    if not calls:
        raise _DSMLParseError("empty_tool_call_batch")
    return tuple(calls)


def _candidate_name(text: str) -> str:
    folded = text.casefold()
    marker = "invoke name="
    start = folded.find(marker, 0, min(len(text), 512))
    if start < 0:
        return ""
    index = start + len(marker)
    if index >= len(text) or text[index] not in {'"', "'"}:
        return ""
    quote = text[index]
    end = text.find(quote, index + 1, min(len(text), index + 131))
    return text[index + 1 : end].strip() if end >= 0 else ""


def inspect_deepseek_text_tool_calls(
    text: str,
    tools: Sequence[Mapping[str, Any]],
    *,
    response_id: str,
) -> DeepSeekTextToolInspection:
    """Classify complete native DSML or a narrow degraded tool-call marker."""

    if not text or not tools:
        return DeepSeekTextToolInspection("none")
    if len(text) > MAX_TEXT_TOOL_CHARACTERS:
        stripped_prefix = text[:512].lstrip().casefold()
        if any(
            stripped_prefix.startswith(item) for item in _STREAM_CANDIDATE_STARTS
        ):
            return DeepSeekTextToolInspection(
                "repair_required",
                dialect="oversized",
                issue="text_tool_call_exceeds_limit",
                candidate_tool_name=_candidate_name(text[:512]),
            )
        return DeepSeekTextToolInspection("none")

    stripped = text.strip()
    for dialect in _DIALECTS:
        if not stripped.startswith(f"<{dialect.calls}>"):
            continue
        try:
            calls = _parse_canonical_dsml(
                stripped,
                dialect,
                tools,
                response_id=response_id,
            )
        except _DSMLParseError as exc:
            return DeepSeekTextToolInspection(
                "repair_required",
                dialect=dialect.name,
                issue=exc.issue,
                candidate_tool_name=(
                    exc.candidate_tool_name or _candidate_name(stripped)
                ),
            )
        return DeepSeekTextToolInspection(
            "recovered",
            dialect=dialect.name,
            tool_calls=calls,
        )

    folded = stripped[:512].casefold()
    if any(folded.startswith(marker) for marker in _DEGRADED_STARTS):
        if folded.startswith("<function_calls"):
            dialect = "v3.2-degraded"
        elif folded.startswith("<|dsml|") or folded.startswith("<||dsml||"):
            dialect = "dsml-degraded"
        else:
            dialect = "unknown-degraded"
        return DeepSeekTextToolInspection(
            "repair_required",
            dialect=dialect,
            issue="noncanonical_text_tool_call",
            candidate_tool_name=_candidate_name(stripped),
        )
    if len(folded) >= 4 and any(
        marker.startswith(folded) for marker in _STREAM_CANDIDATE_STARTS
    ):
        return DeepSeekTextToolInspection(
            "repair_required",
            dialect="incomplete",
            issue="incomplete_text_tool_call_prefix",
        )
    return DeepSeekTextToolInspection("none")


class DeepSeekToolMarkupStreamGate:
    """Hold only a possible leading tool envelope until its prefix is known."""

    def __init__(self, *, enabled: bool) -> None:
        self._state = "pending" if enabled else "passthrough"
        self._buffer = ""

    @property
    def blocked(self) -> bool:
        return self._state == "blocked"

    def feed(self, delta: str) -> str:
        if not delta:
            return ""
        if self._state == "passthrough":
            return delta
        if self._state == "blocked":
            return ""
        self._buffer += delta
        probe = self._buffer.lstrip().casefold()
        if not probe:
            if len(self._buffer) > 1024:
                output, self._buffer = self._buffer, ""
                return output
            return ""
        if any(marker.startswith(probe) for marker in _STREAM_CANDIDATE_STARTS):
            return ""
        if any(probe.startswith(marker) for marker in _STREAM_CANDIDATE_STARTS):
            self._state = "blocked"
            return ""
        self._state = "passthrough"
        output, self._buffer = self._buffer, ""
        return output

    def finish(self) -> str:
        if self._state == "blocked":
            self._buffer = ""
            return ""
        probe = self._buffer.lstrip().casefold()
        if len(probe) >= 4 and any(
            marker.startswith(probe) or probe.startswith(marker)
            for marker in _STREAM_CANDIDATE_STARTS
        ):
            self._state = "blocked"
            self._buffer = ""
            return ""
        output, self._buffer = self._buffer, ""
        self._state = "passthrough"
        return output
