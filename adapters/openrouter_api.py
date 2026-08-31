from __future__ import annotations
import asyncio
import base64
import binascii
import hashlib
import io
import json
import logging
import time
import wave
from dataclasses import dataclass
from itertools import count
from pathlib import Path
from typing import Any, Mapping, Optional
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
    native_attachment_reference_aliases,
    normalize_request_content,
    request_content_has_media,
    request_content_is_voice_origin,
    routing_decisions_payload,
    validate_authorized_media_references,
)


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


@dataclass
class _APIResult:
    """Internal intermediate result from a single API call."""
    text: str
    tool_calls: Optional[list]   # None = no tool calls, just text
    finish_reason: str
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
    for name in ("x-request-id", "request-id", "cf-ray", "x-amzn-requestid"):
        value = str(response.headers.get(name) or "").strip()
        if value:
            return value
    return ""


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

    if isinstance(error, MultimodalContractError):
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
    TRANSIENT_PROVIDER_CALL_RETRIES = 0
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
            error_code = (
                "MEDIA_LIMIT_EXCEEDED"
                if "limit_exceeded" in first.reason
                else "PROVIDER_MODALITY_UNSUPPORTED"
            )
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
    ) -> None:
        """Execute all tool_calls and append tool result messages to `messages`."""
        for tc in tool_calls:
            fn = tc.get("function", {})
            tool_name = fn.get("name", "unknown")
            tc_id = tc.get("id", "")
            raw_args = fn.get("arguments", "{}")

            # Determine stream event kind
            if tool_name == "bash":
                evt_kind = KIND_SHELL_EXEC
            elif tool_name == "file_read":
                evt_kind = KIND_FILE_READ
            elif tool_name == "file_write":
                evt_kind = KIND_FILE_EDIT
            else:
                evt_kind = KIND_TOOL_START

            # Parse arguments
            try:
                arguments = json.loads(raw_args) if raw_args else {}
            except json.JSONDecodeError as e:
                result_text = f"Error: could not parse tool arguments: {e}"
                await self._emit(
                    on_stream_event,
                    evt_kind,
                    f"{tool_name}: {raw_args[:120]}",
                    tool_name=tool_name,
                )
                await self._emit(on_stream_event, KIND_TOOL_END,
                                 f"{tool_name}: argument parse error", tool_name=tool_name,
                                 metadata={"is_error": True})
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": result_text,
                })
                continue

            event_metadata: dict[str, Any] = {}
            event_path = ""
            if isinstance(arguments, Mapping):
                if tool_name == "bash":
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
                metadata=event_metadata,
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
                    metadata={"blocked": True},
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
                        "is_error": True,
                        "tool_result_details": details,
                    },
                )
                raise

            output_preview = result.output[:100].replace("\n", " ")
            await self._emit(on_stream_event, KIND_TOOL_END,
                             f"{tool_name}: {output_preview}", tool_name=tool_name,
                             metadata={
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
        if normalized == "bash":
            return "shell.execute", "shell:bash"
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
            self.global_config.openrouter_url,
            json=payload,
            headers=headers,
        )
        response.raise_for_status()
        data = response.json()
        choices = data.get("choices") or []
        if not choices:
            return _APIResult(text="", tool_calls=None, finish_reason="error")

        choice = choices[0]
        message = choice.get("message") or {}
        finish_reason = choice.get("finish_reason") or "stop"
        ai_text = _assistant_content_text(message.get("content"))
        audio = message.get("audio")
        audio_bytes = b""
        audio_transcript = ""
        if isinstance(audio, Mapping):
            audio_bytes = _decode_provider_audio(audio.get("data"))
            audio_transcript = str(audio.get("transcript") or "")
            if audio_transcript:
                ai_text = audio_transcript

        # Emit reasoning if present
        if on_stream_event is not None:
            reasoning_text = str(message.get("reasoning") or "").strip()
            if reasoning_text:
                await on_stream_event(
                    StreamEvent(
                        kind=KIND_THINKING,
                        summary=reasoning_text[:400],
                        raw_delta=reasoning_text,
                    )
                )
            for detail in message.get("reasoning_details") or []:
                snippet = self._summarize_reasoning_detail(detail)
                if snippet:
                    await on_stream_event(
                        StreamEvent(
                            kind=KIND_THINKING,
                            summary=snippet[:400],
                            raw_delta=self._reasoning_detail_delta(detail),
                        )
                    )

        tool_calls = message.get("tool_calls") or None

        # Extract real token usage from API response
        usage = data.get("usage") or {}
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        thinking_tokens = _usage_thinking_tokens(usage)

        return _APIResult(
            text=ai_text, tool_calls=tool_calls, finish_reason=finish_reason,
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            thinking_tokens=thinking_tokens,
            cost_usd=_usage_cost_usd(usage),
            structured_data=_message_structured_data(message),
            audio_bytes=audio_bytes,
            audio_transcript=audio_transcript,
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
        stream_usage: dict = {}  # usage from final streaming chunk
        saw_done = False
        provider_activity_observed = False
        audio_chunks: list[str] = []
        audio_transcript = ""
        audio_encoded_size = 0

        async with self.client.stream(
            "POST",
            self.global_config.openrouter_url,
            json=payload,
            headers=headers,
        ) as response:
            response.raise_for_status()
            try:
                stream_request = response.request
            except (AttributeError, RuntimeError):
                # Minimal OpenAI-compatible clients and deterministic test
                # doubles may omit httpx's response.request metadata.  Error
                # events still need a concrete request for HTTPStatusError.
                stream_request = httpx.Request(
                    "POST", self.global_config.openrouter_url
                )

            async for line in response.aiter_lines():
                self._touch_activity()

                if not line.startswith("data: "):
                    continue
                data_str = line[6:].strip()
                if data_str == "[DONE]":
                    saw_done = True
                    break

                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                if not isinstance(data, Mapping):
                    continue

                stream_error = _stream_error_exception(
                    data,
                    request=stream_request,
                    provider_activity_observed=provider_activity_observed,
                )
                if stream_error is not None:
                    raise stream_error

                # Capture usage from streaming chunks (sent in final chunk)
                if data.get("usage"):
                    stream_usage = data["usage"]
                    provider_activity_observed = True

                choices = data.get("choices", [])
                if not choices:
                    continue

                choice = choices[0]
                delta = choice.get("delta", {})
                finish_reason = choice.get("finish_reason") or finish_reason

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
                    await on_stream_event(
                        StreamEvent(
                            kind=KIND_THINKING,
                            summary=reasoning_text[:400],
                            raw_delta=reasoning_text,
                        )
                    )
                elif reasoning_details and on_stream_event:
                    for detail in reasoning_details:
                        raw_delta = self._reasoning_detail_delta(detail)
                        if raw_delta:
                            await on_stream_event(
                                StreamEvent(
                                    kind=KIND_THINKING,
                                    summary=raw_delta[:400],
                                    raw_delta=raw_delta,
                                )
                            )
                            continue
                        snippet = self._summarize_reasoning_detail(detail)
                        if snippet:
                            await on_stream_event(
                                StreamEvent(
                                    kind=KIND_THINKING,
                                    summary=snippet[:400],
                                )
                            )

                if content:
                    text_chunks.append(content)
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
                    if fn_delta.get("name"):
                        acc["function"]["name"] += fn_delta["name"]
                    if fn_delta.get("arguments"):
                        acc["function"]["arguments"] += fn_delta["arguments"]

        if not saw_done and not finish_reason:
            raise httpx.RemoteProtocolError(
                "provider stream ended without a completion marker"
            )
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
            finish_reason=finish_reason or "stop",
            prompt_tokens=stream_usage.get("prompt_tokens", 0),
            completion_tokens=stream_usage.get("completion_tokens", 0),
            thinking_tokens=_usage_thinking_tokens(stream_usage),
            cost_usd=_usage_cost_usd(stream_usage),
            audio_bytes=audio_bytes,
            audio_transcript=audio_transcript,
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
        total_tool_calls = 0
        tool_loop_count = 0
        media_routing: tuple[dict[str, Any], ...] = ()
        media_fallback_attempted = False
        last_audio_bytes = b""
        last_audio_transcript = ""
        input_derivatives: tuple[Path, ...] = ()
        input_normalization: tuple[dict[str, Any], ...] = ()

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
                provider_call_started = time.perf_counter()
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
                    except Exception as exc:
                        if self._can_replay_typed_media_fallback(
                            exc,
                            media_routing=media_routing,
                            fallback_attempted=media_fallback_attempted,
                            provider_call_count=provider_call_count,
                            tool_call_count=total_tool_calls,
                        ):
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
                            continue
                        retry_limit = max(
                            0,
                            int(self.TRANSIENT_PROVIDER_CALL_RETRIES),
                        )
                        if (
                            provider_call_retry_count < retry_limit
                            and not provider_call_emitted_text
                            and _transient_provider_call_error(exc)
                        ):
                            provider_call_retry_count += 1
                            provider_transport_retry_count += 1
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
                    break

                # Accumulate usage from each API call
                total_prompt += result.prompt_tokens
                total_completion += result.completion_tokens
                total_thinking += result.thinking_tokens
                provider_call_count += 1
                provider_calls.append(
                    {
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
                        # Wall-clock time HER waited for this provider
                        # invocation.  Streaming includes delivery of the
                        # complete stream, and tool execution starts later.
                        "provider_call_latency_ms": provider_call_latency_ms,
                    }
                )
                if result.cost_usd is None:
                    provider_cost_complete = False
                else:
                    total_cost_usd += result.cost_usd

                last_text = result.text
                last_structured_data = result.structured_data
                last_audio_bytes = result.audio_bytes
                last_audio_transcript = result.audio_transcript

                # No tool calls — we're done
                if not result.tool_calls or not self.tool_registry:
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
                )

            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            from adapters.base import TokenUsage
            usage = TokenUsage(
                input_tokens=total_prompt,
                output_tokens=total_completion,
                thinking_tokens=total_thinking,
            ) if (total_prompt or total_completion) else None
            if audio_output is not None and not last_audio_bytes:
                raise MultimodalContractError(
                    "provider completed a native voice request without audio output",
                    code="PROVIDER_AUDIO_OUTPUT_MISSING",
                )
            output_content: tuple[Mapping[str, Any], ...] = ()
            native_audio_metadata: dict[str, Any] | None = None
            if last_audio_bytes:
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
                is_success=True,
                stop_reason=result.finish_reason if "result" in dir() else "stop",
                usage=usage,
                cost_usd=(
                    round(total_cost_usd, 12)
                    if provider_call_count and provider_cost_complete
                    else None
                ),
                stream_metadata={
                    "meter": {"provider_calls": provider_calls},
                    "provider_transport_retry_count": (
                        provider_transport_retry_count
                    ),
                    "multimodal_routing": list(media_routing),
                    "multimodal_fallback_attempted": media_fallback_attempted,
                    "native_audio": native_audio_metadata,
                    "audio_input_normalization": list(input_normalization),
                },
                tool_call_count=total_tool_calls,
                tool_loop_count=tool_loop_count,
                content=output_content,
            )

        except asyncio.CancelledError:
            self.logger.warning(f"Request cancelled for {request_id}")
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
