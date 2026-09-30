"""GPT-Live adapter primitives for a replaceable Frontend Function.

Not OpenAI Realtime. No Core dependency, SDK installation, shared key in UI,
raw-audio persistence, autonomous backend tools, or arbitrary provider URL.
The application owns start attempts, close recovery, leases and metering.
"""
from __future__ import annotations
from contextlib import asynccontextmanager
from collections.abc import Callable, Mapping, Sequence
from typing import Any
import array
import base64
import binascii
import json
import logging
import math
import sys
from urllib.parse import quote
from orchestrator.phone_catalog import OPENAI_LIVE_VOICES
from .openai_limits import (
    MAX_LIVE_INPUT_MESSAGES,
    MAX_LIVE_INPUT_TOKENS,
    MAX_LIVE_INSTRUCTION_TOKENS,
)
from tools.token_tracker import estimate_tokens
from .protocol import LiveVoiceError, identifier
from .provider import ProviderCapabilities

CREATE_URL = "https://api.openai.com/v1/live/sessions"
ATTACH_ROOT = "wss://api.openai.com/v1/live/sessions"
COUNT_INPUT_URL = "https://api.openai.com/v1/responses/input_tokens"
MAX_FRAME_BYTES = 262144
_LOGGER = logging.getLogger(__name__)


def _reflected_output_activity(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Reduce GPT-Live sideband PCM16LE mono 24 kHz to non-content evidence.

    Digital silence permits at most one quantization bit. This is deliberately
    not a speech classifier: quiet/unknown audio must never authorize a retry.
    """
    start, end = candidate.get("start_ms"), candidate.get("end_ms")
    result = {"type": "output.generated", "activity": "unknown", "completion": "unknown",
              "start_ms": start, "end_ms": end}
    if (any(isinstance(value, bool) or not isinstance(value, (int, float))
            or value < 0 or value > 2**53 - 1 or not math.isfinite(value) for value in (start, end))
            or end <= start):
        return result
    encoded = candidate.get("delta")
    if not isinstance(encoded, str) or not encoded:
        return result
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return result
    if not raw or len(raw) % 2 or abs(len(raw) / 48 - (end - start)) > 1 / 24:
        return result
    samples = array.array("h")
    samples.frombytes(raw)
    if sys.byteorder != "little":
        samples.byteswap()
    result["activity"] = "silent" if all(-1 <= sample <= 1 for sample in samples) else "non_silent"
    return result


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
    *, enforce_message_limit: bool = True,
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
    if enforce_message_limit and len(normalized_input) > MAX_LIVE_INPUT_MESSAGES:
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
    optional_prefix_unit_count: int = 0,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Fit startup input using the provider's authoritative token counter.

    Mandatory PCM context is never clipped. Conversation history is represented
    as chronological complete units; only the oldest whole units may yield.
    """

    if model != "gpt-live-1":
        raise LiveVoiceError("live_model_unqualified")
    normalized = _normalise_input_messages(input_messages, enforce_message_limit=False)
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
    if (
        isinstance(optional_prefix_unit_count, bool)
        or not isinstance(optional_prefix_unit_count, int)
        or not 0 <= optional_prefix_unit_count <= len(unit_counts)
    ):
        raise LiveVoiceError("live_input_plan_invalid", 503)

    message_omitted_units = 0
    had_conversation = len(unit_counts) > optional_prefix_unit_count
    while len(normalized) > MAX_LIVE_INPUT_MESSAGES and unit_counts:
        count = unit_counts.pop(0)
        del normalized[required_message_count:required_message_count + count]
        optional_prefix_unit_count = max(0, optional_prefix_unit_count - 1)
        message_omitted_units += 1
    if len(normalized) > MAX_LIVE_INPUT_MESSAGES:
        raise LiveVoiceError("live_input_mandatory_limit", 503)
    if had_conversation and not unit_counts:
        raise LiveVoiceError("live_input_newest_unit_limit", 503)

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
            "history_omitted_units": message_omitted_units,
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
    if total_units > optional_prefix_unit_count and best == 0:
        raise LiveVoiceError("live_input_newest_unit_limit", 503)
    fitted = candidate(best)
    return fitted, {
        "input_tokens_exact": counts.get(best, required_count),
        "provider_tokens_limit": MAX_LIVE_INPUT_TOKENS,
        "history_included_units": best,
        "history_omitted_units": total_units - best + message_omitted_units,
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


class OpenAILiveProvider:
    """The only live-qualified Phone provider; no PAO state or policy lives here."""

    provider_id = "openai"
    capabilities = ProviderCapabilities(
        version="openai-live-v1", max_input_messages=MAX_LIVE_INPUT_MESSAGES,
        max_instruction_tokens=MAX_LIVE_INSTRUCTION_TOKENS,
        max_input_tokens=MAX_LIVE_INPUT_TOKENS, max_session_seconds=1800,
    )

    def credential(self, secrets: Mapping[str, Any]) -> str:
        import os
        return str(secrets.get("openai_api_key") or os.environ.get("OPENAI_API_KEY", "")).strip()

    def validate_selection(self, model: str, voice: str) -> None:
        if model != "gpt-live-1":
            raise LiveVoiceError("live_model_unqualified", 503)
        if voice not in OPENAI_LIVE_VOICES:
            raise LiveVoiceError("live_voice_unqualified", 503)

    def media_descriptor(self) -> dict[str, Any]:
        return {"transport": "webrtc", "protocol": self.capabilities.version,
                "data_channel": "oai-events", "version": 1}

    def encode_history(self, messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        result = []
        for item in messages:
            role = item.get("role")
            text = item.get("text")
            if text is None:  # Read the previous PCM projection during adoption.
                content = item.get("content")
                if isinstance(content, list) and len(content) == 1 and isinstance(content[0], Mapping):
                    text = content[0].get("text")
            if role not in {"developer", "user", "assistant"} or not isinstance(text, str) or not text:
                raise LiveVoiceError("live_input_invalid", 503)
            result.append({"type": "message", "role": role, "content": [
                {"type": "output_text" if role == "assistant" else "input_text", "text": text}
            ]})
        return result

    def validate_session(self, instructions, model, voice, messages) -> None:
        self.validate_selection(model, voice)
        session_request("v=0", instructions, model, voice, self.encode_history(messages))

    def http_session(self):
        return provider_http_session()

    async def fit_input(self, http, **kwargs):
        kwargs["input_messages"] = self.encode_history(kwargs["input_messages"])
        return await fit_live_session_input(http, **kwargs)

    async def create(self, http, *, key, **kwargs):
        kwargs["input_messages"] = self.encode_history(kwargs["input_messages"])
        return await create_provider_session(http, key=key, request=session_request(**kwargs))

    async def attach(self, http, *, key, provider_session_id):
        return await attach_provider(http, key=key, provider_session_id=provider_session_id)

    def normalize_event(self, raw: str) -> dict[str, Any] | None:
        # Reflected audio is reduced to observation metadata. No bytes escape the adapter.
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_FRAME_BYTES:
            raise LiveVoiceError("live_provider_event_limit", 502)
        try:
            candidate = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise LiveVoiceError("live_provider_event_invalid", 502) from exc
        if isinstance(candidate, dict) and candidate.get("type") == "session.output_audio.delta":
            return _reflected_output_activity(candidate)
        event = safe_sideband_event(raw)
        if event is None:
            return None
        mapping = {
            "session.started": "provider.ready", "session.closed": "provider.closed",
            "session.input_transcript.delta": "conversation.user.delta",
            "session.output_transcript.delta": "conversation.assistant.delta",
            "session.delegation.created": "action.proposed",
            "session.commentary.appended": "update.accepted",
            "session.thinking.appended": "update.accepted",
            "session.instructions.appended": "update.accepted",
            "session.input_audio.muted": "control.accepted",
            "session.input_audio.unmuted": "control.accepted", "error": "provider.error",
        }
        kind = mapping.get(event.get("type"))
        if kind is None:
            return None
        result = {key: event[key] for key in (
            "event_id", "client_event_id", "delta", "start_ms", "end_ms", "offset_ms"
        ) if key in event}
        result["type"] = kind
        if kind == "action.proposed":
            delegation = event.get("delegation")
            if not isinstance(delegation, Mapping) or delegation.get("target") != "client":
                return None
            result["delegation"] = {"target": "client", "id": delegation.get("id")}
        if kind == "provider.error":
            error = event.get("error")
            error = error if isinstance(error, Mapping) else {}
            result["client_event_id"] = result.get("client_event_id") or error.get("client_event_id") or error.get("event_id")
            result["error"] = {"code": _safe_provider_field(error.get("code"))}
        if kind == "provider.closed":
            session = event.get("session")
            session = session if isinstance(session, Mapping) else {}
            result["reason"] = _safe_provider_field(session.get("reason") or event.get("reason"))
            usage = event.get("usage") or session.get("usage")
            if isinstance(usage, Mapping):
                result["usage"] = {str(k): v for k, v in usage.items()
                                   if isinstance(v, int) and not isinstance(v, bool) and v >= 0}
        return result

    def update(self, kind, content, delegation_id, event_id):
        return append_update(kind, content, delegation_id, event_id, estimate_tokens)

    def control(self, action: str, event_id: str) -> dict[str, Any]:
        if action not in {"mute", "unmute", "close"}:
            raise LiveVoiceError("live_action_invalid")
        return {"type": "session.close" if action == "close" else f"session.input_audio.{action}",
                "event_id": identifier(event_id)}

    def opening(self, goal: str, event_id: str) -> dict[str, Any]:
        return self.update("instructions", goal, None, event_id)
