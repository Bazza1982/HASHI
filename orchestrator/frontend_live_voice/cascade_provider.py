"""Cascade Voice Provider for HASHI Phone.

Binds WebRTC and sideband events to an optional local voice-worker process.
Supports faster-whisper STT + text reasoning + Kokoro TTS.
Preserves existing LiveVoiceManager, FC, and background task semantics.
"""
from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote

from orchestrator.phone_catalog import CASCADE_VOICES
from tools.token_tracker import estimate_tokens
from .openai_limits import (
    MAX_LIVE_INPUT_MESSAGES,
    MAX_LIVE_INPUT_TOKENS,
    MAX_LIVE_INSTRUCTION_TOKENS,
)
from .openai_live import append_update, provider_http_session
from .protocol import LiveVoiceError, identifier
from .provider import ProviderCapabilities

_LOGGER = logging.getLogger(__name__)
DEFAULT_CASCADE_WORKER_URL = "http://127.0.0.1:8775"
DEFAULT_CASCADE_WORKER_WS_URL = "ws://127.0.0.1:8775"
MAX_FRAME_BYTES = 262144


def _normalise_input_messages(
    input_messages: Sequence[Mapping[str, Any]] | None,
    *,
    enforce_message_limit: bool = True,
) -> list[dict[str, Any]]:
    normalized_input: list[dict[str, Any]] = []
    for raw in input_messages or ():
        if not isinstance(raw, Mapping):
            raise LiveVoiceError("live_input_invalid")
        role = str(raw.get("role") or "")
        if role not in {"developer", "user", "assistant"}:
            raise LiveVoiceError("live_input_invalid")

        text = raw.get("text")
        if text is None:
            content = raw.get("content")
            if (
                isinstance(content, Sequence)
                and not isinstance(content, (str, bytes))
                and len(content) == 1
                and isinstance(content[0], Mapping)
            ):
                text = content[0].get("text")
        if not isinstance(text, str) or not text.strip():
            raise LiveVoiceError("live_input_invalid")

        normalized_input.append(
            {
                "type": "message",
                "role": role,
                "content": [
                    {
                        "type": "output_text" if role == "assistant" else "input_text",
                        "text": text,
                    }
                ],
            }
        )
    if enforce_message_limit and len(normalized_input) > MAX_LIVE_INPUT_MESSAGES:
        raise LiveVoiceError("live_input_limit")
    return normalized_input


class CascadeProvider:
    """Optional local-cascade Phone provider; default off."""

    provider_id = "local-cascade"
    capabilities = ProviderCapabilities(
        version="cascade-v1",
        max_input_messages=MAX_LIVE_INPUT_MESSAGES,
        max_instruction_tokens=MAX_LIVE_INSTRUCTION_TOKENS,
        max_input_tokens=MAX_LIVE_INPUT_TOKENS,
        max_session_seconds=1800,
        duplex_audio=True,
        proactive_opening=True,
        transcript=True,
        context_updates=True,
        task_results=True,
        recovery=True,
        playback_control="client",
        output_completion="unavailable",
    )

    def credential(self, secrets: Mapping[str, Any]) -> str:
        """Return non-empty credential only when local cascade is explicitly configured."""
        enabled = (
            secrets.get("cascade_enabled")
            or os.environ.get("CASCADE_ENABLED")
            or secrets.get("cascade_worker_url")
            or os.environ.get("CASCADE_WORKER_URL")
        )
        if not enabled:
            return ""
        token = secrets.get("cascade_worker_token") or os.environ.get("CASCADE_WORKER_TOKEN")
        return str(token or "local-cascade-token").strip()

    def validate_selection(self, model: str, voice: str) -> None:
        if model != "cascade-v1":
            raise LiveVoiceError("live_model_unqualified", 503)
        if voice not in CASCADE_VOICES:
            raise LiveVoiceError("live_voice_unqualified", 503)

    def media_descriptor(self) -> dict[str, Any]:
        return {
            "transport": "webrtc",
            "protocol": self.capabilities.version,
            "data_channel": "oai-events",
            "version": 1,
        }

    def encode_history(
        self, messages: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        result = []
        for item in messages:
            role = item.get("role")
            text = item.get("text")
            if text is None:
                content = item.get("content")
                if isinstance(content, list) and len(content) == 1 and isinstance(content[0], Mapping):
                    text = content[0].get("text")
            if role not in {"developer", "user", "assistant"} or not isinstance(text, str) or not text:
                raise LiveVoiceError("live_input_invalid", 503)
            result.append(
                {
                    "type": "message",
                    "role": role,
                    "content": [
                        {"type": "output_text" if role == "assistant" else "input_text", "text": text}
                    ],
                }
            )
        return result

    def validate_session(
        self,
        instructions: str,
        model: str,
        voice: str,
        messages: Sequence[Mapping[str, Any]],
    ) -> None:
        self.validate_selection(model, voice)
        if not isinstance(instructions, str) or not instructions:
            raise LiveVoiceError("live_instructions_invalid")
        if estimate_tokens(instructions) > MAX_LIVE_INSTRUCTION_TOKENS:
            raise LiveVoiceError("live_instructions_invalid")
        _normalise_input_messages(self.encode_history(messages))

    def http_session(self) -> Any:
        return provider_http_session()

    async def fit_input(
        self, http: Any, **kwargs: Any
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        input_messages = kwargs.get("input_messages", ())
        normalized = _normalise_input_messages(input_messages, enforce_message_limit=False)
        total_tokens = sum(
            estimate_tokens(msg["content"][0]["text"]) for msg in normalized if msg.get("content")
        )
        fitted = normalized[-MAX_LIVE_INPUT_MESSAGES:]
        fitted_tokens = sum(
            estimate_tokens(msg["content"][0]["text"]) for msg in fitted if msg.get("content")
        )
        return fitted, {
            "input_tokens_exact": fitted_tokens,
            "provider_tokens_limit": MAX_LIVE_INPUT_TOKENS,
            "history_included_units": len(fitted),
            "history_omitted_units": len(normalized) - len(fitted),
            "provider_count_requests": 1,
        }

    async def create(
        self,
        http: Any,
        *,
        key: str,
        sdp: str,
        instructions: str,
        model: str,
        voice: str,
        input_messages: Sequence[Mapping[str, Any]],
        input_token_count_exact: int,
    ) -> Mapping[str, Any]:
        if not key:
            raise LiveVoiceError("live_credential_unavailable", 503)
        if not isinstance(sdp, str) or not sdp.startswith("v=0") or len(sdp.encode("utf-8")) > 65536:
            raise LiveVoiceError("live_sdp_invalid")
        self.validate_selection(model, voice)

        worker_url = os.environ.get("CASCADE_WORKER_URL", DEFAULT_CASCADE_WORKER_URL)
        create_endpoint = f"{worker_url.rstrip('/')}/v1/sessions"
        payload = {
            "sdp": sdp,
            "instructions": instructions,
            "model": model,
            "voice": voice,
            "input_messages": _normalise_input_messages(input_messages),
        }
        try:
            async with http.post(
                create_endpoint,
                json=payload,
                headers={"Authorization": f"Bearer {key}"},
                allow_redirects=False,
                timeout=10,
            ) as response:
                if response.status == 401:
                    raise LiveVoiceError("live_credential_invalid", 401)
                if response.status in (400, 422):
                    raise LiveVoiceError("live_provider_create_rejected", 400)
                if response.status not in (200, 201):
                    raise LiveVoiceError("live_provider_create_failed", 502)
                value = await response.json()
        except LiveVoiceError:
            raise
        except Exception as exc:
            raise LiveVoiceError("live_provider_create_unknown", 502) from exc

        session_id = identifier(value.get("provider_session_id") or value.get("session_id"))
        answer_sdp = value.get("sdp_answer") or value.get("sdp")
        if not isinstance(answer_sdp, str) or not answer_sdp.startswith("v=0") or len(answer_sdp.encode()) > 65536:
            raise LiveVoiceError("live_provider_answer_invalid", 502)
        return {"provider_session_id": session_id, "sdp_answer": answer_sdp}

    async def attach(self, http: Any, *, key: str, provider_session_id: str) -> Any:
        if not key:
            raise LiveVoiceError("live_credential_unavailable", 503)
        worker_ws_url = os.environ.get("CASCADE_WORKER_WS_URL", DEFAULT_CASCADE_WORKER_WS_URL)
        target = f"{worker_ws_url.rstrip('/')}/v1/sessions/{quote(identifier(provider_session_id), safe='')}/attach?token={quote(key)}"
        _LOGGER.info("CascadeProvider.attach connecting to %s", target)
        return await http.ws_connect(
            target,
            headers={"Authorization": f"Bearer {key}"},
            heartbeat=20,
            max_msg_size=MAX_FRAME_BYTES,
        )

    def normalize_event(self, raw: str) -> dict[str, Any] | None:
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_FRAME_BYTES:
            raise LiveVoiceError("live_provider_event_limit", 502)
        try:
            event = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise LiveVoiceError("live_provider_event_invalid", 502) from exc
        if not isinstance(event, dict):
            raise LiveVoiceError("live_provider_event_invalid", 502)

        mapping = {
            "session.started": "provider.ready",
            "session.closed": "provider.closed",
            "session.input_transcript.delta": "conversation.user.delta",
            "session.output_transcript.delta": "conversation.assistant.delta",
            "session.delegation.created": "action.proposed",
            "session.commentary.appended": "update.accepted",
            "session.thinking.appended": "update.accepted",
            "session.instructions.appended": "update.accepted",
            "session.input_audio.muted": "control.accepted",
            "session.input_audio.unmuted": "control.accepted",
            "session.output_gate.closed": "output.interrupted",
            "error": "provider.error",
        }
        kind = mapping.get(event.get("type"))
        if kind is None:
            return None

        result = {
            key: event[key]
            for key in ("event_id", "client_event_id", "delta", "start_ms", "end_ms", "offset_ms")
            if key in event
        }
        result["type"] = kind
        if kind in {"conversation.user.delta", "conversation.assistant.delta"}:
            if "event_id" not in result or not result["event_id"]:
                from uuid import uuid4
                result["event_id"] = f"evt_{uuid4().hex[:12]}"
            if "delta" not in result or not isinstance(result["delta"], str):
                result["delta"] = str(event.get("delta") or "")
            if not isinstance(result.get("start_ms"), int) or isinstance(result.get("start_ms"), bool) or result["start_ms"] < 0:
                result["start_ms"] = 0
            if (
                not isinstance(result.get("end_ms"), int)
                or isinstance(result.get("end_ms"), bool)
                or result["end_ms"] < result["start_ms"]
            ):
                result["end_ms"] = max(result["start_ms"], result["start_ms"] + max(50, len(result["delta"]) * 50))
        if kind == "output.interrupted":
            result["generation"] = int(event.get("generation") or 1)
            result["reason"] = str(event.get("reason") or "barge_in")
        if kind == "action.proposed":
            delegation = event.get("delegation")
            if not isinstance(delegation, Mapping) or delegation.get("target") != "client":
                return None
            result["delegation"] = {"target": "client", "id": delegation.get("id")}
        if kind == "provider.error":
            error = event.get("error") if isinstance(event.get("error"), Mapping) else {}
            result["error"] = {"code": str(error.get("code") or "cascade_error")}
        if kind == "provider.closed":
            session = event.get("session") if isinstance(event.get("session"), Mapping) else {}
            result["reason"] = str(session.get("reason") or event.get("reason") or "normal")
        return result

    def update(
        self, kind: str, content: str, delegation_id: str | None, event_id: str
    ) -> dict[str, Any]:
        return append_update(kind, content, delegation_id, event_id, estimate_tokens)

    def control(self, action: str, event_id: str) -> dict[str, Any]:
        if action not in {"mute", "unmute", "close"}:
            raise LiveVoiceError("live_action_invalid")
        return {
            "type": "session.close" if action == "close" else f"session.input_audio.{action}",
            "event_id": identifier(event_id),
        }

    def opening(self, goal: str, event_id: str) -> dict[str, Any]:
        return self.update("instructions", goal, None, event_id)
