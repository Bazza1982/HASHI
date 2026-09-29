"""GPT-Live adapter primitives for a replaceable Frontend Function.

Not OpenAI Realtime. No Core dependency, SDK installation, shared key in UI,
raw-audio persistence, autonomous backend tools, or arbitrary provider URL.
The application owns start attempts, close recovery, leases and metering.
"""
from __future__ import annotations
from contextlib import asynccontextmanager
from collections.abc import Callable, Mapping
from typing import Any
import json
from urllib.parse import quote
from orchestrator.phone_catalog import OPENAI_LIVE_VOICES
from .protocol import LiveVoiceError, identifier

CREATE_URL = "https://api.openai.com/v1/live/sessions"
ATTACH_ROOT = "wss://api.openai.com/v1/live/sessions"
MAX_FRAME_BYTES = 262144


def session_request(
    sdp: str,
    instructions: str,
    model: str = "gpt-live-1",
    voice: str = "marin",
) -> dict[str, Any]:
    if not isinstance(sdp, str) or not sdp.startswith("v=0") or len(sdp.encode("utf-8")) > 65536:
        raise LiveVoiceError("live_sdp_invalid")
    # The provider limit is token-based. HASHI projects at most 14k characters
    # and keeps a separate transport-byte ceiling so CJK text is not rejected
    # merely for using multi-byte UTF-8.
    if (not isinstance(instructions, str) or not instructions
            or len(instructions) > 14000
            or len(instructions.encode("utf-8")) > 56000):
        raise LiveVoiceError("live_instructions_invalid")
    if model != "gpt-live-1":  # Extend only with a separately qualified configured model.
        raise LiveVoiceError("live_model_unqualified")
    if voice not in OPENAI_LIVE_VOICES:
        raise LiveVoiceError("live_voice_unqualified")
    return {"session": {"model": model, "instructions": instructions, "store": False,
                        "audio": {"output": {"voice": voice}},
                        "delegation": {"type": "client"}},
            "transport": {"type": "webrtc", "sdp": sdp}}


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
                raise LiveVoiceError("live_provider_create_failed", 502)
            chunks, length = [], 0
            async for chunk in response.content.iter_chunked(16384):
                length += len(chunk)
                if length > 1048576:
                    raise LiveVoiceError("live_provider_response_limit", 502)
                chunks.append(chunk)
            value = json.loads(b"".join(chunks))
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
        "session.input_audio.muted", "session.input_audio.unmuted", "error",
    }:
        return None
    # Internal only. The application must normalize/redact before browser projection/logging.
    return value
