"""Typed failure parsing for ``codex exec --json``.

Codex's non-interactive JSONL stream is the authoritative protocol surface.
In particular, provider failures are normally carried by ``turn.failed`` (and
occasionally preceding ``error`` events), while stderr can be empty.  Keeping
this parser separate from process management makes the failure contract easy
to test and replace when Codex adds event fields.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

_STATUS_RE = re.compile(
    r"\b(?:unexpected\s+)?(?:http(?:\s+status)?(?:\s+code)?|status)"
    r"\s*[:=]?\s*(?P<status>[45]\d\d)\b",
    re.IGNORECASE,
)
_REQUEST_ID_RE = re.compile(
    r"\b(?:request[\s_-]*id)\s*[:=]\s*(?P<request_id>[A-Za-z0-9._:-]+)",
    re.IGNORECASE,
)
_RETRY_AFTER_RE = re.compile(
    r"\b(?:retry|try\s+again)\s+(?:after|in)\s+"
    r"(?P<value>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>milliseconds?|ms|seconds?|secs?|s|minutes?|mins?|m)?\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CodexFailure:
    """Provider-neutral failure fields extracted from a Codex CLI event."""

    message: str
    code: str
    retryable: bool
    http_status: int | None = None
    provider_request_id: str | None = None
    retry_after_s: float | None = None
    description: str = ""


def _json_mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return None
    try:
        decoded = json.loads(stripped)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, Mapping) else None


def _error_payload(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Descend through provider error wrappers, including JSON strings."""

    current = payload
    seen: set[int] = set()
    while id(current) not in seen:
        seen.add(id(current))
        nested = _json_mapping(current.get("error"))
        if nested is None:
            message_payload = _json_mapping(current.get("message"))
            if message_payload is None:
                break
            nested = message_payload
        current = nested
    return current


def _message_from(payload: Mapping[str, Any], fallback: str) -> str:
    current = payload
    best = ""
    seen: set[int] = set()
    while id(current) not in seen:
        seen.add(id(current))
        value = current.get("message")
        if value in (None, ""):
            value = current.get("detail")
        if value in (None, ""):
            value = current.get("error_description")
        if isinstance(value, str) and value.strip():
            decoded = _json_mapping(value)
            if decoded is not None:
                current = decoded
                continue
            best = value.strip()
        elif isinstance(value, Mapping):
            current = value
            continue
        nested_value = current.get("error")
        nested = _json_mapping(nested_value)
        if nested is not None:
            current = nested
            continue
        if not best and isinstance(nested_value, str) and nested_value.strip():
            best = nested_value.strip()
        break
    return best or str(fallback or "").strip() or "Codex CLI failed."


def _first_field(payload: Mapping[str, Any], *keys: str) -> Any:
    current = payload
    seen: set[int] = set()
    while id(current) not in seen:
        seen.add(id(current))
        for key in keys:
            value = current.get(key)
            if value not in (None, ""):
                return value
        nested = _json_mapping(current.get("error")) or _json_mapping(
            current.get("message")
        )
        if nested is None:
            break
        current = nested
    return None


def _http_status(payload: Mapping[str, Any], message: str) -> int | None:
    value = _first_field(payload, "status", "status_code", "http_status")
    try:
        status = int(value)
    except (TypeError, ValueError):
        match = _STATUS_RE.search(message)
        status = int(match.group("status")) if match else 0
    return status if 400 <= status <= 599 else None


def _request_id(payload: Mapping[str, Any], message: str) -> str | None:
    value = _first_field(
        payload,
        "request_id",
        "requestId",
        "x_request_id",
        "x-request-id",
    )
    if value not in (None, ""):
        return str(value).strip()[:200] or None
    match = _REQUEST_ID_RE.search(message)
    return match.group("request_id")[:200] if match else None


def _retry_after(payload: Mapping[str, Any], message: str) -> float | None:
    value = _first_field(
        payload,
        "retry_after_s",
        "retry_after",
        "retryAfter",
        "retry-after",
    )
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        match = _RETRY_AFTER_RE.search(message)
        if not match:
            return None
        parsed = float(match.group("value"))
        unit = (match.group("unit") or "seconds").casefold()
        if unit in {"millisecond", "milliseconds", "ms"}:
            parsed /= 1000
        elif unit in {"minute", "minutes", "min", "mins", "m"}:
            parsed *= 60
    return max(0.0, parsed)


def _provider_code(payload: Mapping[str, Any]) -> str:
    value = _first_field(payload, "code", "error_code")
    return str(value or "").strip().casefold()


def _classify(
    message: str,
    *,
    status: int | None,
    provider_code: str,
) -> tuple[str, bool, int | None, str]:
    lowered = message.casefold()
    signal = f"{provider_code} {lowered}"

    if any(
        token in signal
        for token in (
            "context_length_exceeded",
            "context window",
            "context capacity",
            "ran out of room",
            "too many tokens",
            "prompt is too long",
        )
    ):
        return (
            "CONTEXT_CAPACITY_REJECTED",
            False,
            status or 400,
            "Codex rejected the serialized request because it exceeded the model context capacity.",
        )
    if status == 401 or any(
        token in signal
        for token in (
            "unauthorized",
            "unauthorised",
            "authentication failed",
            "missing bearer",
            "invalid_api_key",
        )
    ):
        return (
            "PROVIDER_AUTHENTICATION_FAILED",
            False,
            status or 401,
            "Codex rejected the configured credentials.",
        )
    if any(
        token in signal
        for token in (
            "flagged for possible cybersecurity risk",
            "safety policy",
            "content policy",
            "policy violation",
        )
    ):
        return (
            "PROVIDER_SAFETY_REJECTED",
            False,
            status or 400,
            "Codex rejected the request under a provider safety policy.",
        )
    if status == 403 or any(
        token in signal for token in ("permission denied", "access denied", "forbidden")
    ):
        return (
            "PROVIDER_PERMISSION_DENIED",
            False,
            status or 403,
            "Codex denied access to the requested resource or model.",
        )
    if any(
        token in signal
        for token in (
            "usage limit",
            "insufficient_quota",
            "purchase more credits",
            "credit balance",
        )
    ):
        return (
            "PROVIDER_QUOTA_EXHAUSTED",
            True,
            status or 429,
            "The Codex account has exhausted its current usage allowance.",
        )
    if any(
        token in signal
        for token in (
            "selected model is at capacity",
            "model is at capacity",
            "currently experiencing high demand",
            "temporarily overloaded",
            "server is overloaded",
        )
    ):
        return (
            "PROVIDER_CAPACITY_UNAVAILABLE",
            True,
            status or 503,
            "The selected Codex model is temporarily at capacity.",
        )
    if status == 429 or any(
        token in signal
        for token in ("rate limit", "rate_limit", "too many requests")
    ):
        return (
            "PROVIDER_RATE_LIMITED",
            True,
            status or 429,
            "Codex rate-limited the request.",
        )
    if any(
        token in signal
        for token in (
            "model_not_found",
            "model is not supported",
            "model does not exist",
            "model is unavailable",
            "unknown model",
        )
    ):
        return (
            "PROVIDER_MODEL_UNAVAILABLE",
            False,
            status or 400,
            "The selected Codex model is unavailable for this account or route.",
        )
    if status == 408 or any(
        token in signal
        for token in ("request timed out", "request timeout", "timed out", "timeout")
    ):
        return (
            "PROVIDER_REQUEST_TIMEOUT",
            True,
            status or 504,
            "The Codex request timed out.",
        )
    if any(
        token in signal
        for token in (
            "certificate verify failed",
            "certificate error",
            "tls handshake",
            "ssl error",
        )
    ):
        return (
            "PROVIDER_TLS_ERROR",
            False,
            status or 502,
            "The Codex TLS certificate or trust configuration failed.",
        )
    if any(
        token in signal
        for token in (
            "stream disconnected before completion",
            "websocket protocol error",
            "websocket closed",
            "connection reset",
            "failed to lookup address",
            "dns",
        )
    ):
        return (
            "PROVIDER_INCOMPLETE_STREAM",
            True,
            status or 502,
            "The Codex response stream ended before completion.",
        )
    if any(
        token in signal
        for token in (
            "error sending request",
            "connection refused",
            "connection aborted",
            "connection reset",
            "network is unreachable",
            "temporary failure in name resolution",
        )
    ):
        return (
            "PROVIDER_CONNECTION_FAILED",
            True,
            status or 502,
            "A connection to Codex could not be established or was interrupted.",
        )
    if status is not None and 500 <= status <= 599:
        return (
            "PROVIDER_SERVER_ERROR",
            True,
            status,
            "Codex reported a temporary server failure.",
        )
    if status == 400 or any(
        token in signal
        for token in ("bad request", "invalid_request_error", "invalid request")
    ):
        return (
            "PROVIDER_BAD_REQUEST",
            False,
            status or 400,
            "Codex rejected the request as invalid.",
        )
    if status is not None and 400 <= status <= 499:
        return (
            "PROVIDER_BAD_REQUEST",
            False,
            status,
            f"Codex rejected the request with HTTP {status}.",
        )
    if any(
        token in signal
        for token in (
            "no such file or directory",
            "executable file not found",
            "permission denied while launching",
            "codex cli not accessible",
        )
    ):
        return (
            "PROVIDER_CONFIGURATION_ERROR",
            False,
            status or 503,
            "The local Codex CLI command could not be launched.",
        )
    return (
        "PROVIDER_UNKNOWN",
        False,
        status,
        "Codex failed for an unclassified technical reason.",
    )


def parse_codex_failure(
    event: Mapping[str, Any] | None = None,
    *,
    fallback_message: str = "",
) -> CodexFailure:
    """Parse one terminal or fallback Codex failure into HASHI's contract."""

    payload: Mapping[str, Any] = event if isinstance(event, Mapping) else {}
    authoritative = _error_payload(payload)
    message = _message_from(payload, fallback_message)
    status = _http_status(authoritative, message) or _http_status(payload, message)
    provider_code = _provider_code(authoritative) or _provider_code(payload)
    code, retryable, status, description = _classify(
        message,
        status=status,
        provider_code=provider_code,
    )
    retry_after_s = _retry_after(authoritative, message)
    if retry_after_s is None:
        retry_after_s = _retry_after(payload, message)
    if code == "PROVIDER_QUOTA_EXHAUSTED" and retry_after_s is None:
        # Absolute reset timestamps in account messages are timezone-ambiguous.
        # Do not fabricate a delay or burn an immediate automatic retry.
        retryable = False
    return CodexFailure(
        message=message,
        code=code,
        retryable=retryable,
        http_status=status,
        provider_request_id=(
            _request_id(authoritative, message) or _request_id(payload, message)
        ),
        retry_after_s=retry_after_s,
        description=description,
    )
