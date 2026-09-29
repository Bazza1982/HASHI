"""GPT-Live adapter primitives for a replaceable Frontend Function.

Not OpenAI Realtime. No Core dependency, SDK installation, shared key in UI,
raw-audio persistence, autonomous backend tools, or arbitrary provider URL.
The application owns start attempts, close recovery, leases and metering.
"""
from __future__ import annotations
from contextlib import asynccontextmanager
from collections.abc import Callable, Mapping, Sequence
from typing import Any
import json
import logging
from urllib.parse import quote
from orchestrator.phone_catalog import OPENAI_LIVE_VOICES
from orchestrator.pcm_voice_projection import (
    MAX_LIVE_INPUT_MESSAGES,
    MAX_LIVE_INPUT_TOKENS,
    MAX_LIVE_INSTRUCTION_TOKENS,
)
from tools.token_tracker import estimate_tokens
from .protocol import LiveVoiceError, identifier

CREATE_URL = "https://api.openai.com/v1/live/sessions"
ATTACH_ROOT = "wss://api.openai.com/v1/live/sessions"
COUNT_INPUT_URL = "https://api.openai.com/v1/responses/input_tokens"
MAX_FRAME_BYTES = 262144
_LOGGER = logging.getLogger(__name__)


async def _response_bytes(response: Any, *, limit: int) -> bytes:
    chunks: list[bytes] = []
    length = 0
    async for chunk in response.content.iter_chunked(16384):
        length += len(chunk)
        if length > limit:
            raise LiveVoiceError("live_provider_response_limit", 502)
        chunks.append(chunk)
    return b"".join(chunks)


def _safe_provider_field(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "unknown"
    cleaned = "".join(
        character
        for character in raw[:160]
        if character.isalnum() or character in "._:/-"
    )
    return cleaned or "unknown"


def _log_provider_rejection(response: Any, body: bytes) -> None:
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        payload = {}
    error = payload.get("error") if isinstance(payload, Mapping) else None
    error = error if isinstance(error, Mapping) else {}
    headers = getattr(response, "headers", {})
    request_id = headers.get("x-request-id") if isinstance(headers, Mapping) else ""
    _LOGGER.warning(
        "GPT-Live session creation rejected status=%s request_id=%s error_type=%s error_code=%s error_param=%s",
        int(getattr(response, "status", 0) or 0),
        _safe_provider_field(request_id),
        _safe_provider_field(error.get("type")),
        _safe_provider_field(error.get("code")),
        _safe_provider_field(error.get("param")),
    )


def _normalise_input_messages(
    input_messages: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    normalized_input: list[dict[str, Any]] = []
    for raw in input_messages or ():
        if not isinstance(raw, Mapping) or raw.get("type") != "message":
            raise LiveVoiceError("live_input_invalid")
        role = str(raw.get("role") or "")
        if role not in {"developer", "user", "assistant"}:
            raise LiveVoiceError("live_input_invalid")
        content = raw.get("content")
        if (
            not isinstance(content, Sequence)
            or isinstance(content, (str, bytes))
            or len(content) != 1
            or not isinstance(content[0], Mapping)
        ):
            raise LiveVoiceError("live_input_invalid")
        part = content[0]
        expected_types = {"output_text", "text"} if role == "assistant" else {"input_text"}
        text = part.get("text")
        if part.get("type") not in expected_types or not isinstance(text, str) or not text:
            raise LiveVoiceError("live_input_invalid")
        normalized_input.append(
            {
                "type": "message",
                "role": role,
                "content": [{"type": str(part["type"]), "text": text}],
            }
        )
    if len(normalized_input) > MAX_LIVE_INPUT_MESSAGES:
        raise LiveVoiceError("live_input_limit")
    return normalized_input


def session_request(
    sdp: str,
    instructions: str,
    model: str = "gpt-live-1",
    voice: str = "marin",
    input_messages: Sequence[Mapping[str, Any]] | None = None,
    token_count: Callable[[str], int] = estimate_tokens,
    input_token_count_exact: int | None = None,
) -> dict[str, Any]:
    if not isinstance(sdp, str) or not sdp.startswith("v=0") or len(sdp.encode("utf-8")) > 65536:
        raise LiveVoiceError("live_sdp_invalid")
    if (
        not isinstance(instructions, str)
        or not instructions
        or token_count(instructions) > MAX_LIVE_INSTRUCTION_TOKENS
    ):
        raise LiveVoiceError("live_instructions_invalid")
    if model != "gpt-live-1":  # Extend only with a separately qualified configured model.
        raise LiveVoiceError("live_model_unqualified")
    if voice not in OPENAI_LIVE_VOICES:
        raise LiveVoiceError("live_voice_unqualified")
    normalized_input = _normalise_input_messages(input_messages)
    if input_token_count_exact is not None and (
        isinstance(input_token_count_exact, bool)
        or not isinstance(input_token_count_exact, int)
        or input_token_count_exact < 0
        or input_token_count_exact > MAX_LIVE_INPUT_TOKENS
    ):
        raise LiveVoiceError("live_input_limit")
    return {"session": {"model": model, "instructions": instructions, "input": normalized_input, "store": False,
                        "audio": {"output": {"voice": voice}},
                        "delegation": {"type": "client"}},
            "transport": {"type": "webrtc", "sdp": sdp}}


async def _provider_input_token_count(
    http: Any,
    *,
    key: str,
    model: str,
    input_messages: Sequence[Mapping[str, Any]],
) -> int:
    if not key:
        raise LiveVoiceError("live_credential_unavailable", 503)
    if not input_messages:
        return 0
    try:
        async with http.post(
            COUNT_INPUT_URL,
            json={"model": model, "input": list(input_messages)},
            headers={"Authorization": f"Bearer {key}"},
            allow_redirects=False,
        ) as response:
            body = await _response_bytes(response, limit=65536)
            if response.status != 200:
                _log_provider_rejection(response, body)
                raise LiveVoiceError("live_provider_count_failed", 502)
            payload = json.loads(body)
    except LiveVoiceError:
        raise
    except Exception as exc:
        raise LiveVoiceError("live_provider_count_failed", 502) from exc
    count = payload.get("input_tokens") if isinstance(payload, Mapping) else None
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise LiveVoiceError("live_provider_count_invalid", 502)
    return count


async def fit_live_session_input(
    http: Any,
    *,
    key: str,
    model: str,
    input_messages: Sequence[Mapping[str, Any]],
    required_message_count: int,
    history_unit_message_counts: Sequence[int],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Fit startup input using the provider's authoritative token counter.

    Mandatory PCM context is never clipped. Conversation history is represented
    as chronological complete units; only the oldest whole units may yield.
    """

    if model != "gpt-live-1":
        raise LiveVoiceError("live_model_unqualified")
    normalized = _normalise_input_messages(input_messages)
    if (
        isinstance(required_message_count, bool)
        or not isinstance(required_message_count, int)
        or not 0 <= required_message_count <= len(normalized)
    ):
        raise LiveVoiceError("live_input_plan_invalid", 503)
    unit_counts = list(history_unit_message_counts)
    if any(
        isinstance(count, bool) or not isinstance(count, int) or count < 1
        for count in unit_counts
    ):
        raise LiveVoiceError("live_input_plan_invalid", 503)
    if required_message_count + sum(unit_counts) != len(normalized):
        raise LiveVoiceError("live_input_plan_invalid", 503)

    required = normalized[:required_message_count]
    unit_offsets: list[tuple[int, int]] = []
    offset = required_message_count
    for count in unit_counts:
        unit_offsets.append((offset, offset + count))
        offset += count

    def candidate(newest_units: int) -> list[dict[str, Any]]:
        if newest_units <= 0:
            return list(required)
        start = unit_offsets[len(unit_offsets) - newest_units][0]
        return [*required, *normalized[start:]]

    counts: dict[int, int] = {}

    async def exact_count(newest_units: int) -> int:
        if newest_units not in counts:
            counts[newest_units] = await _provider_input_token_count(
                http,
                key=key,
                model=model,
                input_messages=candidate(newest_units),
            )
        return counts[newest_units]

    total_units = len(unit_counts)
    full_count = await exact_count(total_units)
    if full_count <= MAX_LIVE_INPUT_TOKENS:
        return candidate(total_units), {
            "input_tokens_exact": full_count,
            "provider_tokens_limit": MAX_LIVE_INPUT_TOKENS,
            "history_included_units": total_units,
            "history_omitted_units": 0,
            "provider_count_requests": len(counts),
        }

    required_count = await exact_count(0)
    if required_count > MAX_LIVE_INPUT_TOKENS:
        raise LiveVoiceError("live_input_mandatory_limit", 503)

    low, high, best = 1, total_units, 0
    while low <= high:
        midpoint = (low + high) // 2
        if await exact_count(midpoint) <= MAX_LIVE_INPUT_TOKENS:
            best = midpoint
            low = midpoint + 1
        else:
            high = midpoint - 1
    if total_units and best == 0:
        raise LiveVoiceError("live_input_newest_unit_limit", 503)
    fitted = candidate(best)
    return fitted, {
        "input_tokens_exact": counts[best],
        "provider_tokens_limit": MAX_LIVE_INPUT_TOKENS,
        "history_included_units": best,
        "history_omitted_units": total_units - best,
        "provider_count_requests": len(counts),
    }


def append_update(kind: str, content: str, delegation_id: str | None, event_id: str,
                  token_count: Callable[[str], int]) -> dict[str, Any]:
    if kind not in {"commentary", "thinking", "instructions"} or not isinstance(content, str) or not content:
        raise LiveVoiceError("live_update_invalid")
    if delegation_id is not None:
        identifier(delegation_id)
    count = token_count(content)
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 500:
        raise LiveVoiceError("live_update_token_limit")
    return {"type": f"session.{kind}.append", "content": content,
            "delegation_id": delegation_id, "event_id": identifier(event_id)}


@asynccontextmanager
async def provider_http_session():
    import aiohttp  # Functions only; reuse the installed qualified dependency.
    trace = aiohttp.TraceConfig()
    async def reject_redirect(_session, _context, _params):
        raise LiveVoiceError("live_provider_redirect", 502)
    trace.on_request_redirect.append(reject_redirect)
    timeout = aiohttp.ClientTimeout(total=25, connect=10, sock_read=15)
    async with aiohttp.ClientSession(timeout=timeout, trace_configs=[trace], trust_env=False) as session:
        yield session


async def create_provider_session(http: Any, *, key: str, request: Mapping[str, Any]) -> dict[str, str]:
    # On transport failure the application must record an unknown creation outcome.
    # Do not retry automatically; provider idempotency is not assumed here.
    if not key:
        raise LiveVoiceError("live_credential_unavailable", 503)
    try:
        async with http.post(CREATE_URL, json=dict(request), headers={"Authorization": f"Bearer {key}"}, allow_redirects=False) as response:
            if response.status != 200 and response.status != 201:
                body = await _response_bytes(response, limit=65536)
                _log_provider_rejection(response, body)
                raise LiveVoiceError("live_provider_create_failed", 502)
            value = json.loads(await _response_bytes(response, limit=1048576))
    except LiveVoiceError:
        raise
    except Exception as exc:
        raise LiveVoiceError("live_provider_create_unknown", 502) from exc
    provider_id = identifier(value.get("session", {}).get("id"))
    answer = value.get("transport", {}).get("sdp")
    if not isinstance(answer, str) or not answer.startswith("v=0") or len(answer.encode()) > 65536:
        raise LiveVoiceError("live_provider_answer_invalid", 502)
    return {"provider_session_id": provider_id, "sdp_answer": answer}


async def attach_provider(http: Any, *, key: str, provider_session_id: str):
    # Call only with provider_http_session() or an equivalently redirect-rejecting client.
    if not key:
        raise LiveVoiceError("live_credential_unavailable", 503)
    target = f"{ATTACH_ROOT}/{quote(identifier(provider_session_id), safe='')}/attach"
    return await http.ws_connect(target, headers={"Authorization": f"Bearer {key}"},
                                 heartbeat=20, max_msg_size=MAX_FRAME_BYTES)


def safe_sideband_event(raw: str) -> dict[str, Any] | None:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_FRAME_BYTES:
        raise LiveVoiceError("live_provider_event_limit", 502)
    try:
        value = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise LiveVoiceError("live_provider_event_invalid", 502) from exc
    if not isinstance(value, dict):
        raise LiveVoiceError("live_provider_event_invalid", 502)
    if value.get("type") in {"session.input_audio.append", "session.output_audio.delta"}:
        return None
    if value.get("type") not in {
        "session.started", "session.closed", "session.input_transcript.delta",
        "session.output_transcript.delta", "session.delegation.created",
        "session.commentary.appended", "session.thinking.appended",
        "session.instructions.appended",
        "session.input_audio.muted", "session.input_audio.unmuted", "error",
    }:
        return None
    # Internal only. The application must normalize/redact before browser projection/logging.
    return value
