"""HASHI API adapter — HASHI's own OpenAI-compatible gateway backend.

The local endpoint is resolved from the Core-published service topology.  A
configured provider URL remains a startup fallback or an explicit remote route,
but it cannot override a healthy endpoint published by this HASHI instance.

Differences from OpenRouter:
  - No API key required (the gateway authenticates at the bind/transport layer).
  - No OpenRouter-specific headers (HTTP-Referer / X-Title / Authorization).
  - Models are the gateway-exposed models (e.g. gpt-5.6-luna, gpt-5.6-sol).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from itertools import count
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from adapters.base import BackendCapabilities, BackendResponse, TokenUsage
from adapters.openrouter_api import (
    _MEDIA_FALLBACK_TOOL_NAMES,
    OpenRouterAdapter,
    ProviderCallObserverError,
    _APIResult,
    _assistant_content_text,
    _backend_failure_response,
    _message_structured_data,
    _stream_error_exception,
    _usage_cost_usd,
    _usage_thinking_tokens,
)
from adapters.stream_events import (
    DELIVERY_INTERNAL,
    HASHI_PROVIDER_ACTIVITY_SSE_TYPE,
    KIND_PROVIDER_ACTIVITY,
    KIND_TEXT_DELTA,
    KIND_THINKING,
    StreamCallback,
    StreamEvent,
)
from orchestrator.multimodal_contract import (
    attachment_manifest,
    native_attachment_reference_aliases,
    normalize_request_content,
)
from orchestrator.pcm import load_pcm_document

_DEFAULT_HASHI_API_BASE_URL = "http://127.0.0.1:18801/v1"
_HASHI_REASONING_EFFORTS = frozenset(
    {"none", "low", "medium", "high", "xhigh", "max"}
)
_HASHI_REASONING_DISABLED_VALUES = frozenset(
    {"off", "false", "0", "disabled"}
)
_TRANSPORT_AUDIT_LOCK = threading.Lock()
_SENSITIVE_HTTP_HEADERS = frozenset(
    {
        "authorization",
        "cookie",
        "proxy-authorization",
        "set-cookie",
        "x-api-key",
    }
)


class HashiApiTransportAuditError(RuntimeError):
    """The local HASHI transport could not durably record an HTTP boundary."""


class HashiApiEndpointError(RuntimeError):
    """The Core-published HASHI gateway route violated its identity contract."""


def _audit_headers(headers: Mapping[str, Any]) -> dict[str, str]:
    """Keep every header name while excluding reusable credential values."""

    return {
        str(name): (
            "[REDACTED]"
            if str(name).strip().casefold() in _SENSITIVE_HTTP_HEADERS
            else str(value)
        )
        for name, value in headers.items()
    }


def _body_evidence(payload: bytes) -> dict[str, Any]:
    """Represent an HTTP body without truncation and with a verification hash."""

    raw = bytes(payload)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {
            "body_encoding": "base64",
            "body": base64.b64encode(raw).decode("ascii"),
            "body_bytes": len(raw),
            "body_sha256": hashlib.sha256(raw).hexdigest(),
        }
    return {
        "body_encoding": "utf-8",
        "body": text,
        "body_bytes": len(raw),
        "body_sha256": hashlib.sha256(raw).hexdigest(),
    }


def _configured_default_base_url(global_config: Any) -> str:
    host = str(getattr(global_config, "api_host", None) or "127.0.0.1").strip()
    if host in {"", "0.0.0.0", "localhost"}:
        host = "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = int(getattr(global_config, "api_gateway_port", None) or 18801)
    except (TypeError, ValueError):
        port = 18801
    if not 1 <= port <= 65535:
        port = 18801
    return f"http://{host}:{port}/v1"


def _provider_base_url(global_config: Any) -> str:
    """Resolve the HASHI gateway base URL from the global provider profile."""
    her = getattr(global_config, "her_providers", None) or {}
    providers = her.get("providers") if isinstance(her, Mapping) else {}
    if isinstance(providers, Mapping):
        profile = providers.get("hashi")
        if isinstance(profile, Mapping):
            base = str(profile.get("base_url") or "").strip().rstrip("/")
            if base:
                return base
    return _configured_default_base_url(global_config) or _DEFAULT_HASHI_API_BASE_URL


def _normalize_gateway_base_url(value: Any, *, append_v1: bool = False) -> str:
    base = str(value or "").strip().rstrip("/")
    parsed = urlparse(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HashiApiEndpointError("HASHI API gateway URL must be an HTTP endpoint")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise HashiApiEndpointError(
            "HASHI API gateway URL must not contain credentials, query, or fragment"
        )
    if append_v1 and parsed.path.rstrip("/") in {"", "/"}:
        base += "/v1"
    return base


def _runtime_gateway_base_url(agent_config: Any, global_config: Any) -> str | None:
    """Resolve this instance's live Gateway route from the Worker topology."""

    runtime = getattr(agent_config, "_hashi_runtime", None)
    orchestrator = getattr(runtime, "orchestrator", None)
    if orchestrator is None:
        return None
    expected_instance = str(
        getattr(global_config, "instance_id", None) or "HASHI"
    ).strip()
    resolver = getattr(orchestrator, "resolve_service_endpoint", None)
    registry = getattr(orchestrator, "endpoint_registry", None)
    if not callable(resolver) and registry is not None:
        resolver = getattr(registry, "resolve", None)
    if not callable(resolver):
        return None
    try:
        endpoint = resolver("api_gateway", expected_instance=expected_instance)
    except Exception as exc:  # topology implementations share no exception identity
        message = str(exc).casefold()
        if "cross-instance" in message or (
            "expected=" in message and "received=" in message
        ):
            raise HashiApiEndpointError(
                "Core rejected the HASHI API gateway route for this instance"
            ) from exc
        return None
    if isinstance(endpoint, Mapping):
        base_url = endpoint.get("base_url")
    else:
        base_url = getattr(endpoint, "base_url", None)
    return _normalize_gateway_base_url(base_url, append_v1=True)


class HashiApiAdapter(OpenRouterAdapter):
    """Provider adapter for HASHI's OpenAI-compatible model gateway.

    The adapter remains stateless between HER stages.  Within one tool-enabled
    stage it uses an opaque Gateway continuation so follow-up tool results are
    sent as an incremental suffix instead of replaying the complete prompt.
    """

    def _trace(self, message: str, *args: Any) -> None:
        """Emit best-effort observability without entering the request boundary."""

        logger = getattr(self, "logger", None)
        log = getattr(logger, "info", None) or getattr(logger, "debug", None)
        if not callable(log):
            return
        try:
            log(message, *args)
        except Exception:  # noqa: BLE001 - diagnostics must never alter execution
            return

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
            # The Gateway owns a request-local visible transcript and may
            # reconstruct it for the exact configured model route. This is
            # continuation-safe but is not a claim of provider-native state.
            continuation_mode="reconstructed",
            tool_request_mode="native",
            recovery_mode="reconstruct_safe",
            reasoning_transport="visible_optional",
        )

    def __init__(self, agent_config, global_config, api_key: str | None = None):
        super().__init__(agent_config, global_config, api_key)
        self.logger = logging.getLogger(f"Backend.HashiApi.{self.config.name}")
        extra = getattr(self.config, "extra", None) or {}
        self.effort = str(extra.get("effort") or "medium").strip().casefold()
        explicit = str(extra.get("hashi_api_url") or "").strip()
        self._explicit_hashi_base_url = (
            _normalize_gateway_base_url(explicit, append_v1=True)
            if explicit
            else None
        )
        self._configured_hashi_base_url = _normalize_gateway_base_url(
            extra.get("base_url") or _provider_base_url(global_config),
            append_v1=True,
        )
        self.hashi_route_source = ""
        self.hashi_url = ""
        self._refresh_hashi_url()
        configured_audit_path = str(
            extra.get("hashi_api_transport_log") or ""
        ).strip()
        if configured_audit_path:
            self.transport_audit_path = Path(configured_audit_path).expanduser()
        else:
            logs_root = getattr(global_config, "base_logs_dir", None)
            self.transport_audit_path = (
                Path(logs_root).expanduser()
                if logs_root
                else Path(self.config.workspace_dir)
            ) / "hashi_api_transport.jsonl"

    def _record_transport_event(
        self,
        event: str,
        *,
        request_id: str,
        provider_call: str,
        streaming: bool,
        request: httpx.Request | None = None,
        response: httpx.Response | None = None,
        response_body: bytes | None = None,
        stream_lines: list[str] | None = None,
        stream_complete: bool | None = None,
        error: BaseException | None = None,
    ) -> str:
        """Append one complete local HTTP boundary record and fsync it."""

        event_id = f"hashi-api-transport-{uuid.uuid4().hex}"
        record: dict[str, Any] = {
            "format": "hashi-api-transport-v1",
            "event_id": event_id,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "event": str(event),
            "process_id": os.getpid(),
            "agent": str(getattr(self.config, "name", "") or ""),
            "model": str(getattr(self.config, "model", "") or ""),
            "request_id": str(request_id or ""),
            "provider_call": str(provider_call or ""),
            "streaming": bool(streaming),
            "gateway_url": self.hashi_url,
            "gateway_route_source": self.hashi_route_source,
        }
        if request is not None:
            request_content = bytes(request.content)
            record["http_request"] = {
                "method": str(request.method),
                "url": str(request.url),
                "headers": _audit_headers(request.headers),
                **_body_evidence(request_content),
            }
        if response is not None:
            body = bytes(response_body) if response_body is not None else b""
            record["http_response"] = {
                "status": int(response.status_code),
                "headers": _audit_headers(response.headers),
                **_body_evidence(body),
            }
        if stream_lines is not None:
            # Lines are retained individually so no SSE event is lost or
            # confused with an embedded newline in provider content.
            record["stream_lines"] = list(stream_lines)
            record["stream_complete"] = bool(stream_complete)
        if error is not None:
            record["error"] = {
                "type": type(error).__name__,
                "message": str(error),
            }

        encoded = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        path = Path(self.transport_audit_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with _TRANSPORT_AUDIT_LOCK, path.open("a", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            path.chmod(0o600)
        except OSError as exc:
            raise HashiApiTransportAuditError(
                f"HASHI API transport audit persistence failed: {exc}"
            ) from exc
        return f"hashi-transport:{path}:{event_id}"

    @staticmethod
    def _attach_transport_refs(error: BaseException, refs: list[str]) -> None:
        if refs:
            setattr(error, "hashi_transport_audit_refs", tuple(refs))

    def _build_audited_request(
        self,
        payload: dict,
        headers: dict,
        *,
        streaming: bool,
    ) -> tuple[httpx.Request, list[str]]:
        """Build first, then persist the exact bytes before network activity."""

        self._refresh_hashi_url()

        request = self.client.build_request(
            "POST",
            self.hashi_url,
            json=payload,
            headers=headers,
        )
        request_id = str(headers.get("X-Hashi-Correlation-ID") or "")
        provider_call = str(headers.get("X-Hashi-Provider-Call") or "")
        ref = self._record_transport_event(
            "client_request_prepared",
            request_id=request_id,
            provider_call=provider_call,
            streaming=streaming,
            request=request,
        )
        return request, [ref]

    def _refresh_hashi_url(self) -> str:
        """Refresh a local route immediately before each physical HTTP call."""

        if self._explicit_hashi_base_url:
            base_url = self._explicit_hashi_base_url
            route_source = "explicit_hashi_api_url"
        else:
            live_base_url = _runtime_gateway_base_url(self.config, self.global_config)
            if live_base_url:
                base_url = live_base_url
                route_source = "core_service_topology"
            else:
                base_url = self._configured_hashi_base_url
                route_source = "configured_fallback"
        resolved = f"{base_url}/chat/completions"
        previous = self.hashi_url
        self.hashi_url = resolved
        self.hashi_route_source = route_source
        if previous and previous != resolved:
            self._trace(
                "HASHI API route refreshed: source=%s endpoint=%s",
                route_source,
                resolved,
            )
        return resolved

    def _hashi_reasoning_effort(self) -> str:
        """Resolve provider reasoning without losing HER's request-time override."""

        extra = dict(getattr(self.config, "extra", None) or {})
        configured = extra.get("provider_reasoning")
        if configured is None:
            configured = extra.get("reasoning_effort")
        if configured is None and self.reasoning_enabled is False:
            configured = "none"
        if configured is None:
            configured = self.effort or extra.get("effort") or "medium"

        normalized = str(configured).strip().casefold()
        if normalized in _HASHI_REASONING_DISABLED_VALUES:
            normalized = "none"
        if normalized not in _HASHI_REASONING_EFFORTS:
            raise ValueError(
                "HASHI reasoning effort must be one of: "
                + ", ".join(sorted(_HASHI_REASONING_EFFORTS))
            )
        return normalized

    def _build_payload(
        self,
        messages: list[dict],
        use_streaming: bool = False,
        tool_tiers: list[str] | None = ...,
        *,
        excluded_tool_names: frozenset[str] = frozenset(),
    ) -> dict:
        payload = super()._build_payload(
            messages,
            use_streaming=use_streaming,
            tool_tiers=tool_tiers,
            excluded_tool_names=excluded_tool_names,
        )
        # OpenRouter's nested ``reasoning`` object is not part of HASHI's
        # Gateway contract. Send one request-scoped Codex effort instead.
        payload.pop("reasoning", None)
        payload["reasoning_effort"] = self._hashi_reasoning_effort()
        return payload

    def _hashi_headers(
        self,
        *,
        request_id: str | None = None,
        provider_call: int | None = None,
        after_tool_end: bool = False,
        external_tool_session: bool = False,
    ) -> dict:
        headers = {"Content-Type": "application/json"}
        if request_id:
            headers["X-Hashi-Correlation-ID"] = str(request_id)[:200]
        if provider_call is not None:
            headers["X-Hashi-Provider-Call"] = str(provider_call)
        headers["X-Hashi-After-Tool-End"] = "true" if after_tool_end else "false"
        if external_tool_session:
            headers["X-Hashi-External-Tool-Session"] = "v1"
        return headers

    async def initialize(self) -> bool:
        self.config.workspace_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_client()
        try:
            if self.config.system_md and Path(self.config.system_md).exists():
                self.sys_prompt = load_pcm_document(
                    self.config.system_md,
                    workspace_dir=self.config.workspace_dir,
                ).system
        except (OSError, UnicodeError) as e:
            self.logger.warning(f"Could not read system_md: {e}")
        self.logger.info("HASHI API adapter initialized in stateless mode.")
        return True

    async def _call_api_once(
        self,
        payload: dict,
        headers: dict,
        on_stream_event: StreamCallback,
    ) -> _APIResult:
        request, audit_refs = self._build_audited_request(
            payload,
            headers,
            streaming=False,
        )
        try:
            response = await self.client.send(request)
        except BaseException as exc:
            failure_ref = self._record_transport_event(
                "client_transport_failed",
                request_id=str(headers.get("X-Hashi-Correlation-ID") or ""),
                provider_call=str(headers.get("X-Hashi-Provider-Call") or ""),
                streaming=False,
                request=request,
                error=exc,
            )
            audit_refs.append(failure_ref)
            self._attach_transport_refs(exc, audit_refs)
            raise

        response_body = bytes(response.content)
        response_ref = self._record_transport_event(
            (
                "client_response_rejected"
                if response.status_code >= 400
                else "client_response_received"
            ),
            request_id=str(headers.get("X-Hashi-Correlation-ID") or ""),
            provider_call=str(headers.get("X-Hashi-Provider-Call") or ""),
            streaming=False,
            request=request,
            response=response,
            response_body=response_body,
        )
        audit_refs.append(response_ref)
        try:
            response.raise_for_status()
            data = response.json()
        except BaseException as exc:
            self._attach_transport_refs(exc, audit_refs)
            raise
        choices = data.get("choices") or []
        if not choices:
            return _APIResult(text="", tool_calls=None, finish_reason="error")

        choice = choices[0]
        message = choice.get("message") or {}
        finish_reason = choice.get("finish_reason") or "stop"
        ai_text = _assistant_content_text(message.get("content"))

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

        tool_calls = message.get("tool_calls") or None
        usage = data.get("usage") or {}
        return _APIResult(
            text=ai_text,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            thinking_tokens=_usage_thinking_tokens(usage),
            cost_usd=_usage_cost_usd(usage),
            structured_data=_message_structured_data(message),
        )

    async def _stream_api_once(
        self,
        payload: dict,
        headers: dict,
        on_stream_event: StreamCallback,
    ) -> _APIResult:
        text_chunks: list[str] = []
        tool_calls_acc: dict[int, dict] = {}
        finish_reason = ""
        stream_usage: dict = {}
        saw_done = False
        provider_activity_observed = False
        stream_lines: list[str] = []
        request, audit_refs = self._build_audited_request(
            payload,
            headers,
            streaming=True,
        )
        try:
            response = await self.client.send(request, stream=True)
        except BaseException as exc:
            failure_ref = self._record_transport_event(
                "client_transport_failed",
                request_id=str(headers.get("X-Hashi-Correlation-ID") or ""),
                provider_call=str(headers.get("X-Hashi-Provider-Call") or ""),
                streaming=True,
                request=request,
                error=exc,
            )
            audit_refs.append(failure_ref)
            self._attach_transport_refs(exc, audit_refs)
            raise

        caught_error: BaseException | None = None
        try:
            if response.status_code >= 400:
                response_body = await response.aread()
                response_ref = self._record_transport_event(
                    "client_response_rejected",
                    request_id=str(headers.get("X-Hashi-Correlation-ID") or ""),
                    provider_call=str(headers.get("X-Hashi-Provider-Call") or ""),
                    streaming=True,
                    request=request,
                    response=response,
                    response_body=response_body,
                    stream_complete=True,
                )
                audit_refs.append(response_ref)
                try:
                    response.raise_for_status()
                except BaseException as exc:
                    self._attach_transport_refs(exc, audit_refs)
                    raise

            async for line in response.aiter_lines():
                stream_lines.append(line)
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

                hashi_event = data.get("hashi")
                if (
                    isinstance(hashi_event, Mapping)
                    and str(hashi_event.get("type") or "")
                    == HASHI_PROVIDER_ACTIVITY_SSE_TYPE
                ):
                    provider_activity_observed = True
                    if on_stream_event is not None:
                        source = (
                            str(hashi_event.get("source") or "hashi-api-gateway")
                            .strip()[:64]
                            or "hashi-api-gateway"
                        )
                        await on_stream_event(
                            StreamEvent(
                                kind=KIND_PROVIDER_ACTIVITY,
                                summary="Provider protocol activity",
                                delivery_class=DELIVERY_INTERNAL,
                                origin=source,
                                metadata={"activity": "protocol_progress"},
                            )
                        )

                stream_error = _stream_error_exception(
                    data,
                    request=response.request,
                    provider_activity_observed=provider_activity_observed,
                )
                if stream_error is not None:
                    raise stream_error

                if data.get("usage"):
                    stream_usage = data["usage"]
                    provider_activity_observed = True

                choices = data.get("choices", [])
                if not choices:
                    continue

                choice = choices[0]
                delta = choice.get("delta", {})
                finish_reason = choice.get("finish_reason") or finish_reason

                content = delta.get("content", "")
                reasoning_text = str(delta.get("reasoning") or "")
                tool_call_deltas = delta.get("tool_calls") or []
                if content or reasoning_text or tool_call_deltas or finish_reason:
                    provider_activity_observed = True

                if reasoning_text and on_stream_event:
                    await on_stream_event(
                        StreamEvent(
                            kind=KIND_THINKING,
                            summary=reasoning_text[:400],
                            raw_delta=reasoning_text,
                        )
                    )

                if content:
                    text_chunks.append(content)
                    if on_stream_event:
                        await on_stream_event(
                            StreamEvent(kind=KIND_TEXT_DELTA, summary=content)
                        )

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
        except BaseException as exc:
            caught_error = exc
            raise
        finally:
            if response.status_code < 400:
                try:
                    response_ref = self._record_transport_event(
                        (
                            "client_stream_received"
                            if caught_error is None
                            else "client_stream_interrupted"
                        ),
                        request_id=str(
                            headers.get("X-Hashi-Correlation-ID") or ""
                        ),
                        provider_call=str(
                            headers.get("X-Hashi-Provider-Call") or ""
                        ),
                        streaming=True,
                        request=request,
                        response=response,
                        response_body="\n".join(stream_lines).encode("utf-8"),
                        stream_lines=stream_lines,
                        stream_complete=saw_done,
                        error=caught_error,
                    )
                    audit_refs.append(response_ref)
                    if caught_error is not None:
                        self._attach_transport_refs(caught_error, audit_refs)
                finally:
                    await response.aclose()
            else:
                await response.aclose()

        full_text = "".join(text_chunks)
        tool_calls = list(tool_calls_acc.values()) if tool_calls_acc else None
        return _APIResult(
            text=full_text,
            tool_calls=tool_calls,
            finish_reason=finish_reason or "stop",
            prompt_tokens=stream_usage.get("prompt_tokens", 0),
            completion_tokens=stream_usage.get("completion_tokens", 0),
            thinking_tokens=_usage_thinking_tokens(stream_usage),
            cost_usd=_usage_cost_usd(stream_usage),
        )

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

        use_streaming = on_stream_event is not None
        last_text = ""
        last_structured_data = None
        total_prompt = 0
        total_completion = 0
        total_thinking = 0
        total_cost_usd = 0.0
        provider_call_count = 0
        provider_cost_complete = True
        provider_calls: list[dict[str, Any]] = []
        total_tool_calls = 0
        tool_loop_count = 0
        media_routing: tuple[dict[str, Any], ...] = ()
        media_fallback_attempted = False
        provider_attempt_count = 0
        gateway_session_id: str | None = None
        gateway_transport_calls: list[dict[str, Any]] = []

        try:
            self._touch_activity()
            messages, media_routing = self._initial_messages(prompt, request_content)
            outbound_messages = messages
            self._last_media_routing = media_routing
            normalized_request_content = normalize_request_content(request_content)
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
            # Gateway sessions intentionally exclude inline media. Text-only
            # tool stages qualify for the request-local continuation protocol.
            if self.tool_registry is not None and not media_routing:
                gateway_session_id = f"hashi-tool-{uuid.uuid4().hex}"

            for loop_idx in count():
                next_provider_recovery_kind = "none"
                while True:
                    provider_attempt_count += 1
                    headers = self._hashi_headers(
                        request_id=request_id,
                        provider_call=provider_attempt_count,
                        after_tool_end=tool_loop_count > 0,
                        external_tool_session=gateway_session_id is not None,
                    )
                    payload = self._build_payload(
                        list(outbound_messages),
                        use_streaming=use_streaming,
                        excluded_tool_names=(
                            _MEDIA_FALLBACK_TOOL_NAMES
                            if all_media_native
                            else frozenset()
                        ),
                    )
                    if gateway_session_id is not None:
                        payload["session_id"] = gateway_session_id
                        # This field is consumed only by HASHI's trusted
                        # Gateway continuation boundary. It is validated
                        # against the instance workspaces root before a
                        # physical provider bridge can use it as cwd.
                        workspace_dir = getattr(
                            self.config,
                            "workspace_dir",
                            None,
                        )
                        if workspace_dir is not None:
                            payload["hashi_tool_workspace"] = str(
                                Path(workspace_dir).resolve()
                            )
                    gateway_transport_calls.append(
                        {
                            "provider_attempt": provider_attempt_count,
                            "incremental": bool(tool_loop_count > 0),
                            "message_count": len(outbound_messages),
                            "message_chars": len(
                                json.dumps(
                                    outbound_messages,
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                )
                            ),
                        }
                    )

                    provider_call_started = time.perf_counter()
                    provider_recovery_kind = next_provider_recovery_kind
                    try:
                        self._trace(
                            "HASHI_API_TRACE provider_call_started "
                            "request_id=%s provider_call=%s after_tool_end=%s "
                            "streaming=%s model=%s",
                            request_id,
                            provider_attempt_count,
                            tool_loop_count > 0,
                            use_streaming,
                            self.config.model,
                        )
                        if use_streaming:
                            result = await self._stream_api_once(
                                payload,
                                headers,
                                on_stream_event,
                            )
                        else:
                            result = await self._call_api_once(
                                payload,
                                headers,
                                on_stream_event,
                            )
                    except HashiApiTransportAuditError:
                        # No further provider or tool activity is allowed when
                        # the mandatory raw transport log is unavailable.
                        raise
                    except asyncio.CancelledError:
                        provider_calls.append(
                            self._provider_call_record(
                                request_id=request_id,
                                serial=provider_attempt_count,
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
                                    "attempt": 1,
                                    "retry_count": 0,
                                    "recovery_kind": provider_recovery_kind,
                                    "status": "cancelled",
                                },
                            )
                        )
                        raise
                    except Exception as exc:
                        provider_calls.append(
                            self._provider_call_record(
                                request_id=request_id,
                                serial=provider_attempt_count,
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
                                    "attempt": 1,
                                    "retry_count": 0,
                                    "recovery_kind": provider_recovery_kind,
                                    "status": "failed_without_receipt",
                                },
                            )
                        )
                        if not self._can_replay_typed_media_fallback(
                            exc,
                            media_routing=media_routing,
                            fallback_attempted=media_fallback_attempted,
                            provider_call_count=provider_call_count,
                            tool_call_count=total_tool_calls,
                        ):
                            raise
                        media_fallback_attempted = True
                        self._enable_request_local_media_fallback(
                            native_attachment_ids
                        )
                        messages = self._typed_media_fallback_messages(
                            prompt,
                            request_content,
                        )
                        outbound_messages = messages
                        gateway_session_id = None
                        media_routing = self._typed_media_fallback_routing(
                            media_routing
                        )
                        self._last_media_routing = media_routing
                        native_attachment_ids = set()
                        native_local_refs = set()
                        all_media_native = False
                        next_provider_recovery_kind = "typed_media_fallback"
                        continue
                    break

                self._trace(
                    "HASHI_API_TRACE provider_call_completed "
                    "request_id=%s provider_call=%s after_tool_end=%s "
                    "finish_reason=%s tool_calls=%s",
                    request_id,
                    provider_attempt_count,
                    tool_loop_count > 0,
                    result.finish_reason,
                    len(result.tool_calls or ()),
                )

                total_prompt += result.prompt_tokens
                total_completion += result.completion_tokens
                total_thinking += result.thinking_tokens
                provider_call_count += 1
                provider_calls.append(
                    self._provider_call_record(
                        request_id=request_id,
                        serial=provider_attempt_count,
                        payload={
                            "input": int(result.prompt_tokens or 0),
                            "output": int(result.completion_tokens or 0),
                            "thinking": int(result.thinking_tokens or 0),
                            "token_source": "provider",
                            "thinking_in_output": True,
                            "cost_usd": result.cost_usd,
                            "prompt_cache_hit_tokens": getattr(
                                result, "prompt_cache_hit_tokens", None
                            ),
                            "prompt_cache_miss_tokens": getattr(
                                result, "prompt_cache_miss_tokens", None
                            ),
                            "provider_call_latency_ms": round(
                                (time.perf_counter() - provider_call_started)
                                * 1000,
                                3,
                            ),
                            "attempt": 1,
                            "retry_count": 0,
                            "recovery_kind": provider_recovery_kind,
                            "status": "completed",
                        },
                    )
                )
                if result.cost_usd is None:
                    provider_cost_complete = False
                else:
                    total_cost_usd += result.cost_usd

                last_text = result.text
                last_structured_data = result.structured_data

                if not result.tool_calls or not self.tool_registry:
                    break

                tool_loop_count += 1
                total_tool_calls += len(result.tool_calls)

                assistant_msg: dict = {"role": "assistant"}
                if result.text:
                    assistant_msg["content"] = result.text
                assistant_msg["tool_calls"] = result.tool_calls
                messages.append(assistant_msg)
                tool_result_start = len(messages)

                await self._run_tool_calls(
                    result.tool_calls,
                    messages,
                    on_stream_event,
                    native_attachment_ids=native_attachment_ids,
                    native_local_refs=native_local_refs,
                    all_media_native=all_media_native,
                )
                outbound_messages = messages[tool_result_start:]
                self._trace(
                    "HASHI_API_TRACE tool_round_completed "
                    "request_id=%s tool_round=%s tool_calls=%s",
                    request_id,
                    tool_loop_count,
                    len(result.tool_calls),
                )

            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            usage = TokenUsage(
                input_tokens=total_prompt,
                output_tokens=total_completion,
                thinking_tokens=total_thinking,
            ) if (total_prompt or total_completion) else None
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
                    "multimodal_routing": list(media_routing),
                    "multimodal_fallback_attempted": media_fallback_attempted,
                    "gateway_continuation": {
                        "enabled": gateway_session_id is not None,
                        "session_id": gateway_session_id,
                        "transport_calls": gateway_transport_calls,
                        "full_prompt_send_count": (
                            1 if gateway_session_id is not None else provider_call_count
                        ),
                    },
                },
                tool_call_count=total_tool_calls,
                tool_loop_count=tool_loop_count,
            )

        except HashiApiTransportAuditError as exc:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            return BackendResponse(
                text="",
                duration_ms=duration_ms,
                error=str(exc),
                is_success=False,
                error_code="AUDIT_PERSISTENCE_FAILURE",
                error_retryable=False,
                side_effects_possible=bool(total_tool_calls),
                tool_call_count=total_tool_calls,
                tool_loop_count=tool_loop_count,
                stream_metadata={
                    "provider_failure_description": (
                        "HASHI stopped because the mandatory local HTTP "
                        "transport record could not be persisted."
                    ),
                    "transport_audit_path": str(self.transport_audit_path),
                    "meter": {"provider_calls": provider_calls},
                    "gateway_continuation": {
                        "enabled": gateway_session_id is not None,
                        "session_id": gateway_session_id,
                        "transport_calls": gateway_transport_calls,
                    },
                },
            )
        except asyncio.CancelledError:
            self.logger.warning(f"Request cancelled for {request_id}")
            raise
        except ProviderCallObserverError:
            raise
        # Provider SDK/HTTP boundaries can raise backend-specific exception types;
        # convert all of them into the adapter's stable failure response.
        except Exception as e:  # noqa: BLE001
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            failure = _backend_failure_response(
                e,
                duration_ms=duration_ms,
                tool_call_count=total_tool_calls,
                tool_loop_count=tool_loop_count,
            )
            metadata = dict(failure.stream_metadata or {})
            metadata["meter"] = {"provider_calls": provider_calls}
            metadata["multimodal_routing"] = list(media_routing)
            metadata["multimodal_fallback_attempted"] = media_fallback_attempted
            metadata["gateway_continuation"] = {
                "enabled": gateway_session_id is not None,
                "session_id": gateway_session_id,
                "transport_calls": gateway_transport_calls,
                "failed_provider_attempt": provider_attempt_count,
                "after_tool_end": tool_loop_count > 0,
            }
            metadata["transport_audit_path"] = str(self.transport_audit_path)
            attachment_id = str(getattr(e, "attachment_id", "") or "")
            if attachment_id:
                metadata["attachment_id"] = attachment_id
            failure.stream_metadata = metadata
            return failure
