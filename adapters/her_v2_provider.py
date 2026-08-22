"""HASHI provider, delivery, and Persona bridges for HER v2."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import ssl
from typing import Any, Mapping

import httpx

from adapters import her_persona
from adapters.base import BackendResponse, TokenUsage
from adapters.stream_events import (
    DELIVERY_FINAL,
    DELIVERY_INTERNAL,
    DELIVERY_REASONING,
    DELIVERY_USER_COMMENTARY,
    KIND_ACKNOWLEDGEMENT,
    KIND_COMMENTARY,
    KIND_INITIAL_RESOLUTION,
    KIND_TEXT_DELTA,
    KIND_THINKING,
    KIND_TOOL_END,
    StreamCallback,
    StreamEvent,
    legacy_delivery_class,
)
from orchestrator.her_v2.audit import AuditPersistenceError, DurableAuditLog
from orchestrator.her_v2.commentary import (
    MAX_PACKAGED_COMMENTARY_CHARS,
    NeutralCommentary,
    PackagedCommentary,
    PersonaPackager,
)
from orchestrator.her_v2.config import ProviderProfile
from orchestrator.her_v2.interfaces import (
    DeliveryPort,
    DeliveryReceipt,
    ProviderFailureCode,
    StageInvocationError,
    StageProvider,
)
from orchestrator.her_v2.models import Stage, StageRequest, StageResponse
from orchestrator.her_v2.presentation import (
    MAX_RENDERED_REQUIRED_MESSAGE_CHARS,
    RenderedRequiredMessage,
    RequiredPersonaRenderer,
    RequiredUserMessage,
)
from orchestrator.her_v2.progress import ProviderActivityTracker
from orchestrator.her_v2.prompts import (
    render_finalisation_system_prompt,
    render_immediate_response_system_prompt,
    render_internal_stage_system_prompt,
    render_persona_commentary_system_prompt,
    render_persona_required_message_system_prompt,
    render_stage_prompt,
)
from orchestrator.her_v2.retry import (
    DEFAULT_PROVIDER_RETRY_POLICY,
    ProviderRetryPolicy,
)

from tools.meter_cost import PerCallUsageLineItem
from tools.token_tracker import resolve_cost_source


def _backend_tool_control(backend: Any) -> tuple[bool, bool]:
    """Return declared tool capability and HASHI isolation capability."""

    capabilities = getattr(backend, "capabilities", None)
    supports_tools = bool(getattr(capabilities, "supports_tool_use", False))
    controls_tools = hasattr(backend, "tool_registry")
    return supports_tools, controls_tools


def _install_system_prompt(backend: Any, prompt: str) -> bool:
    setter = getattr(backend, "set_system_prompt", None)
    if callable(setter):
        setter(prompt)
        return True
    if hasattr(backend, "sys_prompt"):
        backend.sys_prompt = prompt
        return True
    return False


def _normalise_backend_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        for key in ("text", "output_text", "content"):
            if key in value:
                return _normalise_backend_text(value.get(key))
        import json

        return json.dumps(dict(value), ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "".join(_normalise_backend_text(item) for item in value)
    return str(value)


def _provider_structured_data(response: BackendResponse) -> Mapping[str, Any]:
    direct = getattr(response, "structured_data", None)
    if isinstance(direct, Mapping):
        return dict(direct)
    metadata = getattr(response, "stream_metadata", None)
    if isinstance(metadata, Mapping):
        for key in ("structured_data", "parsed", "structured_output"):
            value = metadata.get(key)
            if isinstance(value, Mapping):
                return dict(value)
    return {}


def _backend_response_error(
    response: BackendResponse,
    *,
    fallback: str,
) -> StageInvocationError:
    metadata = (
        dict(response.stream_metadata)
        if isinstance(response.stream_metadata, Mapping)
        else {}
    )
    inferred_code, inferred_retryable, inferred_description = (
        _infer_untyped_backend_failure(response.error or fallback)
    )
    code = response.error_code or inferred_code
    retryable = (
        bool(response.error_retryable)
        if response.error_retryable is not None
        else (
            _retryable_for_provider_code(code)
            if response.error_code
            else inferred_retryable
        )
    )
    return StageInvocationError(
        response.error or fallback,
        retryable=retryable,
        code=code,
        human_description=str(
            metadata.get("provider_failure_description") or inferred_description
        ),
        http_status=response.http_status,
        provider_request_id=response.provider_request_id or "",
        retry_after_s=response.retry_after_s,
        side_effects_possible=bool(response.side_effects_possible),
        details={
            "stop_reason": response.stop_reason,
            "tool_call_count": int(response.tool_call_count or 0),
            "tool_loop_count": int(response.tool_loop_count or 0),
        },
    )


def _retryable_for_provider_code(code: ProviderFailureCode | str) -> bool:
    value = code.value if isinstance(code, ProviderFailureCode) else str(code)
    return value in {
        ProviderFailureCode.PROVIDER_UNKNOWN.value,
        ProviderFailureCode.PROVIDER_REQUEST_TIMEOUT.value,
        ProviderFailureCode.PROVIDER_RATE_LIMITED.value,
        ProviderFailureCode.PROVIDER_SERVER_ERROR.value,
        ProviderFailureCode.PROVIDER_CONNECTION_FAILED.value,
        ProviderFailureCode.PROVIDER_RESPONSE_START_TIMEOUT.value,
        ProviderFailureCode.PROVIDER_INCOMPLETE_STREAM.value,
        ProviderFailureCode.PROVIDER_INCOMPLETE_STREAM_TIMEOUT.value,
        ProviderFailureCode.PROVIDER_REASONING_ONLY_TIMEOUT.value,
        ProviderFailureCode.PROVIDER_STREAM_IDLE_TIMEOUT.value,
        ProviderFailureCode.PROVIDER_EMPTY_RESPONSE.value,
        ProviderFailureCode.STRUCTURED_OUTPUT_INVALID.value,
    }


def _infer_untyped_backend_failure(
    error: str,
) -> tuple[ProviderFailureCode, bool, str]:
    """Best-effort typing for legacy backends that return only error text."""

    text = " ".join(str(error or "").split())
    lowered = text.casefold()
    status_match = re.search(
        r"\b(?:http(?:\s+status)?(?:\s+code)?[\s:=]*)?"
        r"(400|401|403|408|429|5\d\d)\b",
        lowered,
    )
    status = int(status_match.group(1)) if status_match else None

    if status == 400:
        return (
            ProviderFailureCode.PROVIDER_BAD_REQUEST,
            False,
            "The provider rejected the request as invalid.",
        )
    if status == 401 or any(
        token in lowered
        for token in ("unauthorized", "unauthorised", "authentication failed")
    ):
        return (
            ProviderFailureCode.PROVIDER_AUTHENTICATION_FAILED,
            False,
            "The provider rejected the configured credentials.",
        )
    if status == 403 or "forbidden" in lowered:
        return (
            ProviderFailureCode.PROVIDER_PERMISSION_DENIED,
            False,
            "The provider denied access to this model or request.",
        )
    if status == 408:
        return (
            ProviderFailureCode.PROVIDER_REQUEST_TIMEOUT,
            True,
            "The provider timed out while handling the request.",
        )
    if status == 429 or "rate limit" in lowered or "rate-limit" in lowered:
        return (
            ProviderFailureCode.PROVIDER_RATE_LIMITED,
            True,
            "The provider rate-limited the request.",
        )
    if status is not None and 500 <= status <= 599:
        return (
            ProviderFailureCode.PROVIDER_SERVER_ERROR,
            True,
            "The provider reported a temporary server failure.",
        )
    if any(token in lowered for token in ("certificate", "ssl", "tls")):
        return (
            ProviderFailureCode.PROVIDER_TLS_ERROR,
            False,
            "The provider TLS certificate or trust configuration failed.",
        )
    if any(
        token in lowered
        for token in (
            "invalid url",
            "unsupported protocol",
            "executable not found",
            "command not found",
            "no such file or directory",
            "api key is not configured",
            "api key not configured",
            "credentials are not configured",
            "provider is not initialized",
        )
    ):
        return (
            ProviderFailureCode.PROVIDER_CONFIGURATION_ERROR,
            False,
            "The configured provider executable, URL, or credentials are unavailable.",
        )
    if any(
        token in lowered
        for token in (
            "no answer text",
            "without a final assistant message",
            "empty response",
            "no complete response",
        )
    ):
        return (
            ProviderFailureCode.PROVIDER_EMPTY_RESPONSE,
            True,
            "The provider ended without a complete usable response.",
        )
    if any(token in lowered for token in ("timed out", "timeout", "idle for")):
        return (
            ProviderFailureCode.PROVIDER_REQUEST_TIMEOUT,
            True,
            "The provider connection or response timed out.",
        )
    if any(
        token in lowered
        for token in (
            "connection reset",
            "connection refused",
            "connection failed",
            "connection attempts failed",
            "connecterror",
            "network is unreachable",
            "name resolution",
            "dns",
        )
    ):
        return (
            ProviderFailureCode.PROVIDER_CONNECTION_FAILED,
            True,
            "The provider connection was interrupted or could not be established.",
        )
    if any(
        token in lowered
        for token in ("incomplete stream", "stream ended", "unexpected eof")
    ):
        return (
            ProviderFailureCode.PROVIDER_INCOMPLETE_STREAM,
            True,
            "The provider response ended before a complete result arrived.",
        )
    return (
        ProviderFailureCode.PROVIDER_UNKNOWN,
        True,
        "The provider failed for an unknown technical reason.",
    )


def _provider_exception_error(
    error: Exception,
    *,
    label: str,
    side_effects_possible: bool = False,
) -> StageInvocationError:
    """Classify provider exceptions that escaped a backend response boundary."""

    response = getattr(error, "response", None)
    status = int(response.status_code) if isinstance(response, httpx.Response) else None
    retryable = False
    code = ProviderFailureCode.PROVIDER_UNKNOWN
    description = "The provider failed for an unknown technical reason."

    if status is not None:
        if status == 400:
            code = ProviderFailureCode.PROVIDER_BAD_REQUEST
            description = "The provider rejected the request as invalid."
        elif status == 401:
            code = ProviderFailureCode.PROVIDER_AUTHENTICATION_FAILED
            description = "The provider rejected the configured credentials."
        elif status == 403:
            code = ProviderFailureCode.PROVIDER_PERMISSION_DENIED
            description = "The provider denied access to this model or request."
        elif status == 408:
            code = ProviderFailureCode.PROVIDER_REQUEST_TIMEOUT
            description = "The provider timed out while handling the request."
            retryable = True
        elif status == 429:
            code = ProviderFailureCode.PROVIDER_RATE_LIMITED
            description = "The provider rate-limited the request."
            retryable = True
        elif 500 <= status <= 599:
            code = ProviderFailureCode.PROVIDER_SERVER_ERROR
            description = "The provider reported a temporary server failure."
            retryable = True
        elif 400 <= status <= 499:
            code = ProviderFailureCode.PROVIDER_BAD_REQUEST
            description = f"The provider rejected the request with HTTP {status}."
    elif isinstance(error, (httpx.TimeoutException, TimeoutError)):
        code = ProviderFailureCode.PROVIDER_REQUEST_TIMEOUT
        description = "The provider connection or response timed out."
        retryable = True
    elif isinstance(
        error,
        (httpx.RemoteProtocolError, json.JSONDecodeError, UnicodeDecodeError),
    ):
        code = ProviderFailureCode.PROVIDER_INCOMPLETE_STREAM
        description = "The provider response ended before a complete result arrived."
        retryable = True
    elif isinstance(error, (httpx.InvalidURL, httpx.UnsupportedProtocol)):
        code = ProviderFailureCode.PROVIDER_CONFIGURATION_ERROR
        description = "The configured provider URL or protocol is invalid."
    elif isinstance(error, (ssl.SSLError,)):
        code = ProviderFailureCode.PROVIDER_TLS_ERROR
        description = "The provider TLS certificate or trust configuration failed."
    elif isinstance(error, httpx.ConnectError):
        lowered = str(error).casefold()
        if any(token in lowered for token in ("certificate", "ssl", "tls")):
            code = ProviderFailureCode.PROVIDER_TLS_ERROR
            description = "The provider TLS certificate or trust configuration failed."
        else:
            code = ProviderFailureCode.PROVIDER_CONNECTION_FAILED
            description = "A connection to the provider could not be established."
            retryable = True
    elif isinstance(error, (httpx.NetworkError, ConnectionError)):
        code = ProviderFailureCode.PROVIDER_CONNECTION_FAILED
        description = "The provider connection was interrupted."
        retryable = True
    elif isinstance(error, (FileNotFoundError, PermissionError)):
        code = ProviderFailureCode.PROVIDER_CONFIGURATION_ERROR
        description = "The configured provider executable or resource is unavailable."
    else:
        # Unknown provider faults get the single conservative recovery attempt.
        retryable = True

    retry_after_s: float | None = None
    provider_request_id = ""
    if isinstance(response, httpx.Response):
        raw_retry_after = str(response.headers.get("retry-after") or "").strip()
        if raw_retry_after:
            try:
                retry_after_s = max(0.0, float(raw_retry_after))
            except ValueError:
                retry_after_s = None
        for header in ("x-request-id", "request-id", "cf-ray", "x-amzn-requestid"):
            provider_request_id = str(response.headers.get(header) or "").strip()
            if provider_request_id:
                break

    return StageInvocationError(
        f"{label}: {type(error).__name__}: {error}",
        retryable=retryable,
        code=code,
        human_description=description,
        http_status=status,
        provider_request_id=provider_request_id,
        retry_after_s=retry_after_s,
        side_effects_possible=side_effects_possible,
    )


def _registry_allowed_names(registry: Any) -> tuple[str, ...]:
    names = getattr(registry, "allowed_tool_names", None)
    if callable(names):
        return tuple(str(item) for item in names() if str(item).strip())
    definitions = getattr(registry, "get_tool_definitions", None)
    if not callable(definitions):
        return ()
    result: list[str] = []
    try:
        available = definitions(tiers=None)
    except TypeError:
        # Older or third-party registries may not expose tier filtering.
        available = definitions()
    for item in available:
        if not isinstance(item, Mapping):
            continue
        name = str((item.get("function") or {}).get("name") or "").strip()
        if name:
            result.append(name)
    return tuple(result)


def _registry_is_read_only(registry: Any, tool_name: str) -> bool:
    probe = getattr(registry, "is_read_only", None)
    return bool(callable(probe) and probe(tool_name) is True)


def _manager_authorises_profile(manager: Any, profile: ProviderProfile) -> bool:
    """Accept provider/model targets configured at the HASHI instance level."""

    option_getter = getattr(manager, "_her_v2_provider_option", None)
    if callable(option_getter):
        option = option_getter(profile.engine)
        return bool(
            option
            and option.get("available")
            and profile.model in (option.get("models") or ())
        )

    # Compatibility for injected test/third-party managers that expose only
    # the legacy Agent configuration surface.
    manager_config = getattr(manager, "config", None)
    for raw in getattr(manager_config, "allowed_backends", ()) or ():
        if not isinstance(raw, Mapping):
            continue
        if str(raw.get("engine") or "").strip() != profile.engine:
            continue
        models = {
            str(raw.get(key) or "").strip()
            for key in ("model", "default_model")
            if str(raw.get(key) or "").strip()
        }
        configured_models = raw.get("models")
        if isinstance(configured_models, list):
            models.update(
                str(item).strip() for item in configured_models if str(item).strip()
            )
        if profile.model in models:
            return True
    return False


def _internal_stage_system_prompt(request: StageRequest) -> str | None:
    return render_internal_stage_system_prompt(request)


def _finalisation_system_prompt(
    source: her_persona.HERPersonaPackagingSource,
) -> str:
    return render_finalisation_system_prompt(
        guidance=source.guidance,
        display_name=source.display_name,
        usable=source.usable,
        persona_block_begin=her_persona.PERSONA_BLOCK_BEGIN,
        persona_block_end=her_persona.PERSONA_BLOCK_END,
    )


def _immediate_response_system_prompt(
    source: her_persona.HERPersonaPackagingSource,
) -> str:
    return render_immediate_response_system_prompt(
        guidance=source.guidance,
        display_name=source.display_name,
        usable=source.usable,
        persona_block_begin=her_persona.PERSONA_BLOCK_BEGIN,
        persona_block_end=her_persona.PERSONA_BLOCK_END,
    )


class _DelegatedToolRegistry:
    """Narrow a HASHI ToolRegistry without copying secrets or policy logic."""

    def __init__(
        self,
        base: Any,
        delegated_tools: list[str],
        *,
        read_only: bool = False,
    ):
        self._base = base
        requested = {str(item) for item in delegated_tools if str(item).strip()}
        self._allowed = {
            name for name in requested if bool(getattr(base, "is_allowed")(name))
        }
        if read_only:
            self._allowed = {
                name for name in self._allowed if _registry_is_read_only(base, name)
            }
        # Delegation narrows authority only; it must never reintroduce the
        # retired shared-registry tool-round ceiling.
        self.max_loops = None
        self.audit_context = dict(getattr(base, "audit_context", {}) or {})
        if read_only:
            self.audit_context.update(
                {
                    "safety_mode": "read_only",
                    "authority_mode": "her_v2_shadow",
                }
            )

    def is_allowed(self, tool_name: str) -> bool:
        return str(tool_name) in self._allowed

    def is_read_only(self, tool_name: str) -> bool:
        return self.is_allowed(tool_name) and _registry_is_read_only(
            self._base, tool_name
        )

    def get_tool_definitions(self, tiers=None):
        definitions = self._base.get_tool_definitions(tiers=tiers)
        return [
            item
            for item in definitions
            if str((item.get("function") or {}).get("name") or "") in self._allowed
        ]

    async def execute(self, tool_name: str, arguments: dict, tool_call_id: str = ""):
        if self.is_allowed(tool_name):
            scoped_execute = getattr(self._base, "execute_with_audit_context", None)
            if callable(scoped_execute):
                return await scoped_execute(
                    tool_name,
                    arguments,
                    tool_call_id,
                    audit_context=self.audit_context,
                )
            return await self._base.execute(tool_name, arguments, tool_call_id)
        from tools.registry import ToolResult

        result = ToolResult(
            tool_call_id=tool_call_id,
            output=(
                f"Error: tool {tool_name!r} is outside this sub-agent's delegated authority"
            ),
            is_error=True,
        )
        denial_recorder = getattr(self._base, "record_delegated_denial", None)
        if callable(denial_recorder):
            denial_recorder(
                tool_name,
                arguments,
                result,
                audit_context=self.audit_context,
            )
        return result


class _UnboundedToolRegistry:
    """Request-local registry view without a tool round ceiling."""

    def __init__(self, base: Any):
        self._base = base
        # ``None`` is the API-adapter contract for an unbounded tool loop.
        # Keep the shared registry unchanged because it may serve legacy or
        # concurrent non-HER requests that still use its configured ceiling.
        self.max_loops = None

    @property
    def base(self) -> Any:
        return self._base

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)


class _AdapterDelivery(DeliveryPort):
    def __init__(self, callback: StreamCallback, *, allow_early: bool):
        self.callback = callback
        self.allow_early = bool(allow_early)

    async def deliver(
        self,
        *,
        kind: str,
        text: str,
        event_id: str,
        required: bool = False,
        phase: str = "",
        provenance: str = "",
        detail: str = "",
        delivery_id: str = "",
    ) -> DeliveryReceipt:
        if kind == "commentary":
            raise ValueError("raw commentary cannot enter the HASHI transport boundary")
        if self.callback is None:
            if kind in {"final", "clarification"}:
                return DeliveryReceipt(
                    accepted=True,
                    delivered=False,
                    disposition="deferred_to_final_boundary",
                )
            return DeliveryReceipt(
                accepted=False,
                delivered=False,
                disposition="stream_callback_unavailable",
            )
        if kind == "immediate" and not self.allow_early:
            return DeliveryReceipt(
                accepted=False,
                delivered=False,
                disposition="early_delivery_disabled",
            )
        if kind in {"acknowledgement", "immediate"}:
            event_kind = KIND_ACKNOWLEDGEMENT
            delivery_class = DELIVERY_USER_COMMENTARY
        else:
            # The HASHI HER message router defers this lane to the ordinary
            # final-response boundary, preventing duplicate Direct Response or
            # clarification delivery.
            event_kind = KIND_TEXT_DELTA
            delivery_class = DELIVERY_FINAL
        accepted = await self.callback(
            StreamEvent(
                kind=event_kind,
                summary=text,
                event_id=event_id,
                delivery_class=delivery_class,
                origin="her_v2",
                phase=phase or kind,
                required=required,
                provenance=provenance or "model_authored",
                detail=detail,
                delivery_id=delivery_id,
            )
        )
        if kind in {"final", "clarification"}:
            return DeliveryReceipt(
                accepted=True,
                delivered=False,
                disposition="deferred_to_final_boundary",
            )
        # Legacy callbacks return None.  Only an explicit router receipt proves
        # that an early message can replace ordinary final delivery.
        if kind == "immediate":
            delivered = accepted is True
            return DeliveryReceipt(
                accepted=delivered,
                delivered=delivered,
                disposition=(
                    "transport_delivered" if delivered else "transport_rejected"
                ),
            )
        delivered = accepted is not False
        return DeliveryReceipt(
            accepted=delivered,
            delivered=delivered,
            disposition=("transport_delivered" if delivered else "transport_rejected"),
        )

    async def deliver_packaged_commentary(self, commentary: PackagedCommentary) -> bool:
        """Accept only the typed output of the Persona packaging boundary."""

        if self.callback is None:
            return False
        accepted = await self.callback(
            StreamEvent(
                kind=KIND_COMMENTARY,
                summary=commentary.text,
                event_id=commentary.source_event_id,
                delivery_class=DELIVERY_USER_COMMENTARY,
                origin="her_v2:persona_packaging",
                phase=commentary.stage.value,
                required=False,
                provenance=commentary.provenance,
                detail=(
                    "persona_packaging_fallback=true; "
                    f"error_type={commentary.error_type or 'unknown'}"
                    if commentary.fallback
                    else "persona_packaging_fallback=false"
                ),
            )
        )
        return accepted is not False

    async def resolve_initial(
        self,
        *,
        resolution: str,
        text: str,
        target_event_id: str,
        event_id: str,
        delivery_id: str = "",
    ) -> DeliveryReceipt:
        if self.callback is None:
            return DeliveryReceipt(False, False, "stream_callback_unavailable")
        accepted = await self.callback(
            StreamEvent(
                kind=KIND_INITIAL_RESOLUTION,
                summary=text,
                event_id=event_id,
                delivery_class=DELIVERY_INTERNAL,
                origin="her_v2",
                phase="initial_resolution",
                provenance="runtime_control",
                resolution=resolution,
                target_event_id=target_event_id,
                delivery_id=delivery_id,
            )
        )
        delivered = accepted is True
        return DeliveryReceipt(
            accepted=delivered,
            delivered=delivered,
            disposition=(
                f"provisional_{resolution}"
                if delivered
                else "provisional_resolution_rejected"
            ),
        )


class HashiStageProvider(StageProvider):
    """Invoke configured provider adapters without giving HER tool ownership."""

    def __init__(
        self,
        *,
        backend_manager: Any,
        tool_registry: Any = None,
        on_stream_event: StreamCallback = None,
        silent: bool = False,
        retry_policy: ProviderRetryPolicy | None = None,
        audit_log: DurableAuditLog | None = None,
        workzone_ref: str = "",
    ) -> None:
        self.backend_manager = backend_manager
        self.tool_registry = tool_registry
        self.on_stream_event = on_stream_event
        self.silent = silent
        self.retry_policy = retry_policy or DEFAULT_PROVIDER_RETRY_POLICY
        self.audit_log = audit_log
        self.workzone_ref = str(workzone_ref or "")
        self._persona_invocation_serial = 0
        self._persona_audit_contexts: dict[str, tuple[str, str]] = {}
        self.logger = logging.getLogger("HASHI.HERv2.StageProvider")
        self.usage = TokenUsage()
        self.cost_usd = 0.0
        self.tool_call_count = 0
        self.tool_loop_count = 0
        # Per-stage cost line items (Zelda /meter contract).  Populated at the
        # moment each stage/Persona invocation returns, while the real
        # profile.engine / profile.model / stage are still known.
        self.usage_line_items: list[PerCallUsageLineItem] = []

    def _record_usage_line_item(
        self,
        *,
        request_id: str,
        phase: str,
        engine: str,
        model: str,
        response: Any,
    ) -> None:
        """Record one per-stage/per-persona usage line item with provenance."""
        metadata = getattr(response, "stream_metadata", None)
        raw_meter = metadata.get("meter") if isinstance(metadata, Mapping) else None
        raw_calls = (
            raw_meter.get("provider_calls") if isinstance(raw_meter, Mapping) else None
        )
        calls: list[Mapping[str, Any]] = (
            [item for item in raw_calls if isinstance(item, Mapping)]
            if isinstance(raw_calls, list) and raw_calls
            else [
                {
                    "input": int(getattr(response.usage, "input_tokens", 0) or 0),
                    "output": int(getattr(response.usage, "output_tokens", 0) or 0),
                    "thinking": int(
                        getattr(response.usage, "thinking_tokens", 0) or 0
                    ),
                    "token_source": (
                        "provider" if response.usage is not None else "estimated"
                    ),
                    "thinking_in_output": response.usage is not None,
                    "cost_usd": getattr(response, "cost_usd", None),
                }
            ]
        )

        from tools.token_tracker import calc_cost

        parent_request_id = str(request_id or "")
        for index, call in enumerate(calls, start=1):
            input_tokens = int(call.get("input") or 0)
            output_tokens = int(call.get("output") or 0)
            thinking_tokens = int(call.get("thinking") or 0)
            token_source = str(call.get("token_source") or "estimated")
            thinking_in_output = bool(
                call.get("thinking_in_output")
                if call.get("thinking_in_output") is not None
                else token_source == "provider"
            )
            resolved_cost, cost_source = resolve_cost_source(
                cost_usd=call.get("cost_usd"),
                model=model,
                engine=engine,
            )
            if resolved_cost is None and cost_source == "pricing_table":
                resolved_cost = calc_cost(
                    input_tokens,
                    output_tokens,
                    model,
                    thinking_tokens,
                    thinking_in_output=thinking_in_output,
                )
            per_call = len(calls) > 1 or bool(raw_calls)
            self.usage_line_items.append(
                PerCallUsageLineItem(
                    request_id=(
                        f"{parent_request_id}:provider-call:{index}"
                        if per_call
                        else parent_request_id
                    ),
                    parent_request_id=parent_request_id if per_call else "",
                    phase=str(phase or ""),
                    engine=str(engine or ""),
                    model=str(model or ""),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    thinking_tokens=thinking_tokens,
                    token_source=token_source,
                    thinking_in_output=thinking_in_output,
                    cost_usd=resolved_cost,
                    cost_source=cost_source,
                )
            )

    def usage_receipt(self, request_id: str = ""):
        """Return a structured :class:`UsageReceipt` for this provider turn."""
        from tools.meter_cost import UsageReceipt

        return UsageReceipt(
            request_id=str(request_id or ""),
            parent_request_id="",
            line_items=list(self.usage_line_items),
        )

    def bind_persona_audit_context(
        self,
        request_id: str,
        *,
        turn_id: str,
        request_ref: str,
    ) -> None:
        """Bind audit correlation without adding metadata to model inputs."""

        self._persona_audit_contexts[str(request_id)] = (
            str(turn_id),
            str(request_ref),
        )

    async def invoke(
        self, profile: ProviderProfile, request: StageRequest
    ) -> StageResponse:
        try:
            backend = self.backend_manager.create_ephemeral_backend(
                profile.engine, target_model=profile.model
            )
        except Exception as exc:
            raise StageInvocationError(
                f"cannot create configured stage provider {profile.engine}/{profile.model}: {exc}",
                retryable=False,
                code=ProviderFailureCode.PROVIDER_CONFIGURATION_ERROR,
                human_description="The configured provider backend could not be created.",
            ) from exc

        # Provider reasoning remains provider-specific and never receives the
        # HER effort label.  Adapters may consume either the explicit option or
        # their established provider-specific compatibility field.
        backend_extra = dict(getattr(backend.config, "extra", None) or {})
        if profile.reasoning is not None:
            backend_extra["provider_reasoning"] = profile.reasoning
            backend_extra["reasoning_effort"] = profile.reasoning
        backend_extra.update(dict(profile.options))
        backend.config.extra = backend_extra
        if profile.reasoning is not None and hasattr(backend, "set_reasoning_enabled"):
            normalized = str(profile.reasoning or "").strip().casefold()
            backend.set_reasoning_enabled(
                normalized not in {"", "none", "off", "false", "0", "disabled"}
            )

        supports_tools, controls_tools = _backend_tool_control(backend)
        if request.allow_tools and not supports_tools:
            await backend.shutdown()
            raise StageInvocationError(
                f"provider engine {profile.engine!r} does not support requested tool use",
                retryable=False,
                code=ProviderFailureCode.PROVIDER_CONFIGURATION_ERROR,
                human_description=(
                    "The configured provider cannot satisfy this stage's tool contract."
                ),
            )
        if supports_tools and not controls_tools:
            await backend.shutdown()
            raise StageInvocationError(
                f"provider engine {profile.engine!r} cannot prove HASHI tool isolation",
                retryable=False,
                code=ProviderFailureCode.PROVIDER_CONFIGURATION_ERROR,
                human_description=(
                    "The configured provider cannot prove HASHI-owned tool isolation."
                ),
            )
        selected_registry = self.tool_registry if request.allow_tools else None
        if selected_registry is not None and (
            request.role.startswith("sub_agent:") or not request.allow_side_effects
        ):
            delegated = request.context.get("delegated_tools")
            if delegated is None:
                delegated = sorted(
                    name
                    for name in _registry_allowed_names(selected_registry)
                    if bool(getattr(selected_registry, "is_allowed")(name))
                    and _registry_is_read_only(selected_registry, name)
                )
            if not isinstance(delegated, list):
                await backend.shutdown()
                raise StageInvocationError(
                    "sub-agent delegated_tools must be a list",
                    retryable=False,
                    code=ProviderFailureCode.PROVIDER_CONFIGURATION_ERROR,
                    human_description="The delegated tool configuration is invalid.",
                )
            selected_registry = _DelegatedToolRegistry(
                selected_registry,
                delegated,
                read_only=not request.allow_side_effects,
            )
        if selected_registry is not None:
            # The Agent-level registry owns permissions, not HER v2 execution
            # length.  HER tool-enabled stages continue until the model
            # finishes, fails, or the request is cancelled.
            selected_registry = _UnboundedToolRegistry(selected_registry)
        if controls_tools:
            backend.tool_registry = selected_registry
        backend.privacy_level = self.backend_manager.privacy_level
        reasoning_chunks: list[str] = []

        async def _capture(event: StreamEvent) -> None:
            owner = str(event.delivery_class or "") or legacy_delivery_class(event.kind)
            if request.provider_activity_callback is not None:
                content = str(event.raw_delta or event.summary or "")
                event_metadata = (
                    event.metadata if isinstance(event.metadata, Mapping) else {}
                )
                tool_details = event_metadata.get("tool_result_details")
                request.provider_activity_callback(
                    {
                        "kind": event.kind,
                        "content": content,
                        "tool_name": event.tool_name,
                        "tool_details": dict(
                            tool_details if isinstance(tool_details, Mapping) else {}
                        ),
                        "tool_read_only": (
                            _registry_is_read_only(selected_registry, event.tool_name)
                            if event.tool_name and selected_registry is not None
                            else None
                        ),
                    }
                )
            if owner == DELIVERY_REASONING or event.kind == KIND_THINKING:
                trace = str(event.raw_delta or event.summary or "")
                if trace:
                    reasoning_chunks.append(trace)
            # Structured JSON answer deltas are internal.  Reasoning and
            # invalid envelope retries are not meaningful execution progress.
            if event.kind == KIND_TEXT_DELTA:
                return
            if event.kind == KIND_TOOL_END and request.progress_callback is not None:
                request.progress_callback(event.kind, event.summary, True)
            # Reasoning and execution activity retain their normal HASHI
            # presentation owners.
            if self.on_stream_event is None:
                return
            if not event.delivery_class:
                event.delivery_class = owner
            # Provider-native progress is not the HER v2 commentary contract.
            # Only a validated structured ``commentary`` field may enter the
            # separate Persona packaging pipeline.
            if event.delivery_class == DELIVERY_USER_COMMENTARY:
                event.delivery_class = DELIVERY_INTERNAL
            event.origin = event.origin or f"her_v2:{profile.engine}"
            event.phase = event.phase or request.stage.value
            await self.on_stream_event(event)

        try:
            initialized = await backend.initialize()
            if not initialized:
                raise StageInvocationError(
                    f"failed to initialize {profile.engine}/{profile.model}",
                    retryable=False,
                    code=ProviderFailureCode.PROVIDER_CONFIGURATION_ERROR,
                    human_description="The configured provider could not be initialized.",
                )
            stage_prompt = render_stage_prompt(request)
            if request.stage in {
                Stage.IMMEDIATE_RESPONSE,
                Stage.FINALISATION,
            }:
                backend_config = backend.config
                backend_extra = dict(getattr(backend_config, "extra", None) or {})
                source = her_persona.load_persona_packaging_source(
                    getattr(backend_config, "system_md", None),
                    display_name=(
                        backend_extra.get("display_name")
                        or getattr(backend_config, "name", None)
                    ),
                )
                system_prompt = (
                    _immediate_response_system_prompt(source)
                    if request.stage is Stage.IMMEDIATE_RESPONSE
                    else _finalisation_system_prompt(source)
                )
                if not _install_system_prompt(backend, system_prompt):
                    if request.stage is Stage.FINALISATION:
                        raise StageInvocationError(
                            "finalisation backend cannot isolate the HER v2 system prompt",
                            retryable=False,
                            code=ProviderFailureCode.PROVIDER_CONFIGURATION_ERROR,
                            human_description=(
                                "The configured Finalisation provider cannot isolate "
                                "the required system prompt."
                            ),
                        )
                    stage_prompt = f"{system_prompt}\n\n{stage_prompt}"
            else:
                internal_prompt = _internal_stage_system_prompt(request)
                if internal_prompt is not None:
                    installed = _install_system_prompt(backend, internal_prompt)
                    if not installed and request.stage is Stage.EXECUTION:
                        raise StageInvocationError(
                            "execution backend cannot isolate the HER v2 system prompt",
                            retryable=False,
                            code=ProviderFailureCode.PROVIDER_CONFIGURATION_ERROR,
                            human_description=(
                                "The configured Execution provider cannot isolate "
                                "the required system prompt."
                            ),
                        )
                    if not installed:
                        stage_prompt = f"{internal_prompt}\n\n{stage_prompt}"
            response = await backend.generate_response(
                stage_prompt,
                f"{request.turn_id}:{request.stage.value}:{request.attempt}",
                is_retry=request.attempt > 1,
                silent=self.silent,
                on_stream_event=_capture,
            )
            if not response.is_success:
                raise _backend_response_error(
                    response,
                    fallback=(
                        f"{profile.engine}/{profile.model} returned an unsuccessful response"
                    ),
                )
            if (
                not _normalise_backend_text(response.text).strip()
                and not _provider_structured_data(response)
                and not response.tool_call_count
                and str(response.stop_reason or "").casefold()
                in {"", "error", "length", "incomplete"}
            ):
                raise StageInvocationError(
                    f"{profile.engine}/{profile.model} returned no complete response",
                    retryable=True,
                    code=ProviderFailureCode.PROVIDER_EMPTY_RESPONSE,
                    human_description=(
                        "The provider ended without a complete usable response."
                    ),
                    details={"stop_reason": response.stop_reason},
                )
            if response.usage:
                self.usage.input_tokens += int(response.usage.input_tokens or 0)
                self.usage.output_tokens += int(response.usage.output_tokens or 0)
                self.usage.thinking_tokens += int(response.usage.thinking_tokens or 0)
            self.cost_usd += float(response.cost_usd or 0.0)
            self.tool_call_count += int(response.tool_call_count or 0)
            self.tool_loop_count += int(response.tool_loop_count or 0)
            self._record_usage_line_item(
                request_id=request.request_ref or request.turn_id,
                phase=request.stage.value,
                engine=profile.engine,
                model=profile.model,
                response=response,
            )
            evidence_refs = (
                (f"hashi-tools:{request.turn_id}:{request.attempt}",)
                if response.tool_call_count
                else ()
            )
            return StageResponse(
                text=_normalise_backend_text(response.text),
                data=_provider_structured_data(response),
                reasoning_trace="".join(reasoning_chunks).strip() or None,
                provider=profile.engine,
                model=profile.model,
                usage={
                    "input_tokens": int(
                        getattr(response.usage, "input_tokens", 0) or 0
                    ),
                    "output_tokens": int(
                        getattr(response.usage, "output_tokens", 0) or 0
                    ),
                    "thinking_tokens": int(
                        getattr(response.usage, "thinking_tokens", 0) or 0
                    ),
                },
                evidence_refs=evidence_refs,
                provider_attempt=request.attempt,
            )
        except StageInvocationError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise _provider_exception_error(
                exc,
                label=f"{profile.engine}/{profile.model} invocation failed",
            ) from exc
        finally:
            await backend.shutdown()

    async def package_persona_commentary(
        self,
        profile: ProviderProfile,
        *,
        persona_block: str,
        neutral_commentary: str,
        request_id: str,
    ) -> str:
        """Package neutral prose in an isolated, tool-free invocation."""

        system_prompt = render_persona_commentary_system_prompt(
            persona_guidance=persona_block,
            persona_block_begin=her_persona.PERSONA_BLOCK_BEGIN,
            persona_block_end=her_persona.PERSONA_BLOCK_END,
        )
        return await self._package_persona_text(
            profile,
            prompt="NEUTRAL COMMENTARY (quoted, read-only)\n" + neutral_commentary,
            system_prompt=system_prompt,
            request_id=request_id,
            message_label="commentary",
            max_chars=MAX_PACKAGED_COMMENTARY_CHARS,
        )

    async def package_persona_required_message(
        self,
        profile: ProviderProfile,
        *,
        persona_block: str,
        neutral_message: str,
        message_kind: str,
        request_id: str,
    ) -> str:
        """Render one validated final report or clarification with Persona."""

        kind = str(message_kind or "").strip()
        if kind not in {"final", "clarification"}:
            raise StageInvocationError(
                f"unsupported required Persona message kind: {kind!r}",
                retryable=False,
                code=ProviderFailureCode.PROVIDER_CONFIGURATION_ERROR,
                human_description="The required Persona message kind is invalid.",
            )
        label = "FINAL REPORT" if kind == "final" else "CLARIFICATION QUESTION"
        kind_rule = (
            "Do not add a question, invitation, next step, or offer of more work."
            if kind == "final"
            else "Keep it as the same clarification question; do not answer it or add another question."
        )
        system_prompt = render_persona_required_message_system_prompt(
            message_kind=kind.replace("_", " "),
            kind_rule=kind_rule,
            persona_guidance=persona_block,
            persona_block_begin=her_persona.PERSONA_BLOCK_BEGIN,
            persona_block_end=her_persona.PERSONA_BLOCK_END,
        )
        return await self._package_persona_text(
            profile,
            prompt=f"VALIDATED {label} (quoted, read-only)\n{neutral_message}",
            system_prompt=system_prompt,
            request_id=request_id,
            message_label=kind,
            max_chars=MAX_RENDERED_REQUIRED_MESSAGE_CHARS,
        )

    async def _package_persona_text(
        self,
        profile: ProviderProfile,
        *,
        prompt: str,
        system_prompt: str,
        request_id: str,
        message_label: str,
        max_chars: int,
    ) -> str:
        """Run one isolated, tool-free Persona call with one typed recovery."""

        self._persona_invocation_serial += 1
        invocation_serial = self._persona_invocation_serial
        invocation_id = (
            f"{request_id}:persona:{message_label}:invocation:{invocation_serial}"
        )
        invariant_payload = {
            "provider": profile.engine,
            "model": profile.model,
            "goal": "render_validated_message_without_semantic_change",
            "classification": None,
            "authority": "presentation_only",
            "allow_tools": False,
            "allow_side_effects": False,
            "workzone": self.workzone_ref or None,
            "source_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "system_sha256": hashlib.sha256(system_prompt.encode("utf-8")).hexdigest(),
        }
        invariant_hash = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(
                    invariant_payload,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
        )
        bound_turn_id, bound_request_ref = self._persona_audit_contexts.pop(
            str(request_id),
            ("", ""),
        )
        turn_id = bound_turn_id or f"persona:{request_id}"
        request_ref = bound_request_ref or f"hashi-request:{request_id}"
        last_error: StageInvocationError | None = None
        for attempt in range(1, self.retry_policy.max_provider_retries + 2):
            tracker = ProviderActivityTracker()
            try:
                rendered = await self._package_persona_text_once(
                    profile,
                    prompt=prompt,
                    system_prompt=system_prompt,
                    request_id=request_id,
                    message_label=message_label,
                    max_chars=max_chars,
                    attempt=attempt,
                    activity=tracker,
                )
                if self.audit_log is not None:
                    self.audit_log.append(
                        event_id=f"{invocation_id}:attempt:{attempt}:completed",
                        turn_id=turn_id,
                        request_ref=request_ref,
                        stage="persona_presentation",
                        role="persona_packager",
                        event="persona_provider_attempt_completed",
                        provider=profile.engine,
                        model=profile.model,
                        attempt=attempt,
                        payload={
                            "message_label": message_label,
                            "retry_invariant_hash": invariant_hash,
                            "provider_activity": tracker.snapshot(),
                            "rendered_text_sha256": hashlib.sha256(
                                rendered.encode("utf-8")
                            ).hexdigest(),
                        },
                    )
                return rendered
            except asyncio.CancelledError:
                raise
            except AuditPersistenceError:
                raise
            except StageInvocationError as exc:
                last_error = exc
            except Exception as exc:
                last_error = _provider_exception_error(
                    exc,
                    label=f"Persona {message_label} invocation failed",
                )

            assert last_error is not None
            will_retry = bool(
                last_error.retryable
                and attempt <= self.retry_policy.max_provider_retries
            )
            retry_reason = (
                "eligible"
                if will_retry
                else (
                    "failure_non_retryable"
                    if not last_error.retryable
                    else "provider_recovery_already_used"
                )
            )
            retry_delay = (
                last_error.retry_after_s
                if last_error.retry_after_s is not None
                else 0.25
            )
            if self.audit_log is not None:
                self.audit_log.append(
                    event_id=f"{invocation_id}:attempt:{attempt}:failed",
                    turn_id=turn_id,
                    request_ref=request_ref,
                    stage="persona_presentation",
                    role="persona_packager",
                    event="persona_provider_attempt_failed",
                    provider=profile.engine,
                    model=profile.model,
                    attempt=attempt,
                    payload={
                        **last_error.audit_payload(),
                        "message_label": message_label,
                        "will_retry": will_retry,
                        "retry_reason": retry_reason,
                        "retry_delay_s": retry_delay if will_retry else None,
                        "fresh_connection_on_retry": will_retry,
                        "retry_invariant_hash": invariant_hash,
                        "retry_invariants": invariant_payload,
                        "provider_activity": tracker.snapshot(),
                    },
                )
            if not will_retry:
                raise last_error.terminal_copy(
                    f"Persona {message_label} failed after {attempt} attempt(s): "
                    f"{last_error}",
                    attempts=attempt,
                ) from last_error
            if self.audit_log is not None:
                self.audit_log.append(
                    event_id=f"{invocation_id}:attempt:{attempt}:retry-scheduled",
                    turn_id=turn_id,
                    request_ref=request_ref,
                    stage="persona_presentation",
                    role="persona_packager",
                    event="persona_provider_retry_scheduled",
                    provider=profile.engine,
                    model=profile.model,
                    attempt=attempt,
                    payload={
                        "next_attempt": attempt + 1,
                        "retry_delay_s": retry_delay,
                        "fresh_connection": True,
                        "same_provider": True,
                        "same_model": True,
                        "same_goal": True,
                        "same_classification": True,
                        "same_permissions": True,
                        "same_workzone": True,
                        "retry_invariant_hash": invariant_hash,
                    },
                )
            self.logger.warning(
                "HER v2 Persona provider retry: label=%s attempt=%s "
                "error_code=%s retry_after_s=%.3f fresh_connection=true",
                message_label,
                attempt,
                last_error.error_code,
                retry_delay,
            )
            await asyncio.sleep(retry_delay)
        raise AssertionError("unreachable Persona retry state")

    async def _package_persona_text_once(
        self,
        profile: ProviderProfile,
        *,
        prompt: str,
        system_prompt: str,
        request_id: str,
        message_label: str,
        max_chars: int,
        attempt: int,
        activity: ProviderActivityTracker,
    ) -> str:
        backend = None
        try:
            backend = self.backend_manager.create_ephemeral_backend(
                profile.engine, target_model=profile.model
            )
            backend_extra = dict(getattr(backend.config, "extra", None) or {})
            if profile.reasoning is not None:
                backend_extra["provider_reasoning"] = profile.reasoning
                backend_extra["reasoning_effort"] = profile.reasoning
            backend_extra.update(dict(profile.options))
            backend.config.extra = backend_extra
            if profile.reasoning is not None and hasattr(
                backend, "set_reasoning_enabled"
            ):
                normalized = str(profile.reasoning or "").strip().casefold()
                backend.set_reasoning_enabled(
                    normalized not in {"", "none", "off", "false", "0", "disabled"}
                )
            supports_tools, controls_tools = _backend_tool_control(backend)
            if supports_tools and not controls_tools:
                raise StageInvocationError(
                    f"Persona provider {profile.engine!r} cannot prove tool isolation",
                    retryable=False,
                    code=ProviderFailureCode.PROVIDER_CONFIGURATION_ERROR,
                    human_description=(
                        "The Persona provider cannot guarantee a tool-free invocation."
                    ),
                )
            if controls_tools:
                backend.tool_registry = None
            backend.privacy_level = self.backend_manager.privacy_level

            async def _discard_stream(event: StreamEvent) -> None:
                activity.record(
                    {
                        "kind": event.kind,
                        "content": event.raw_delta or event.summary,
                        "tool_name": event.tool_name,
                    }
                )

            initialized = await backend.initialize()
            if not initialized:
                raise StageInvocationError(
                    f"failed to initialize Persona provider {profile.engine}/{profile.model}",
                    retryable=False,
                    code=ProviderFailureCode.PROVIDER_CONFIGURATION_ERROR,
                    human_description="The Persona provider could not be initialized.",
                )
            effective_prompt = prompt
            if not _install_system_prompt(backend, system_prompt):
                effective_prompt = f"{system_prompt}\n\n{prompt}"
            response = await backend.generate_response(
                effective_prompt,
                request_id,
                is_retry=attempt > 1,
                silent=True,
                on_stream_event=_discard_stream,
            )
            if not response.is_success:
                raise _backend_response_error(
                    response,
                    fallback=(
                        f"{profile.engine}/{profile.model} {message_label} render failed"
                    ),
                )
            if response.usage:
                self.usage.input_tokens += int(response.usage.input_tokens or 0)
                self.usage.output_tokens += int(response.usage.output_tokens or 0)
                self.usage.thinking_tokens += int(response.usage.thinking_tokens or 0)
            self.cost_usd += float(response.cost_usd or 0.0)
            self._record_usage_line_item(
                request_id=request_id,
                phase="persona",
                engine=profile.engine,
                model=profile.model,
                response=response,
            )
            text = str(response.text or "").strip()
            if not text:
                raise StageInvocationError(
                    f"Persona provider returned empty {message_label} text",
                    code=ProviderFailureCode.PROVIDER_EMPTY_RESPONSE,
                    human_description="The Persona provider returned no usable text.",
                )
            if len(text) > max_chars:
                raise StageInvocationError(
                    f"Persona provider returned oversized {message_label} text",
                    retryable=False,
                    code=ProviderFailureCode.PROVIDER_BAD_REQUEST,
                    human_description=(
                        "The Persona provider returned a response beyond the safe size limit."
                    ),
                )
            return text
        except asyncio.CancelledError:
            raise
        except StageInvocationError:
            raise
        except Exception as exc:
            raise _provider_exception_error(
                exc,
                label=f"Persona {message_label} invocation failed",
            ) from exc
        finally:
            if backend is not None:
                await backend.shutdown()


class _ConfiguredPersonaPackager(PersonaPackager, RequiredPersonaRenderer):
    def __init__(
        self,
        *,
        provider: HashiStageProvider,
        profile: ProviderProfile,
        source: her_persona.HERPersonaPackagingSource,
        request_id: str,
        logger: logging.Logger,
    ) -> None:
        self.provider = provider
        self.profile = profile
        self.source = source
        self.request_id = request_id
        self.logger = logger
        self.package_index = 0

    async def package(self, commentary: NeutralCommentary) -> PackagedCommentary:
        if not self.source.usable:
            return self._fallback(
                commentary,
                self.source.unavailable_reason or "persona_block_unavailable",
            )
        self.package_index += 1
        package_request_id = f"{self.request_id}:persona-package:{self.package_index}"
        try:
            binder = getattr(self.provider, "bind_persona_audit_context", None)
            if callable(binder):
                binder(
                    package_request_id,
                    turn_id=commentary.turn_id,
                    request_ref=f"hashi-request:{self.request_id}",
                )
            text = await self.provider.package_persona_commentary(
                self.profile,
                persona_block=self.source.guidance,
                neutral_commentary=commentary.text,
                request_id=package_request_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - deterministic presentation fallback
            self.logger.warning("HER v2 Persona packaging failed safely: %s", exc)
            return self._fallback(commentary, type(exc).__name__)
        return PackagedCommentary(
            source_event_id=commentary.event_id,
            stage=commentary.stage,
            text=text,
            provenance="persona_packager",
        )

    def _fallback(
        self, commentary: NeutralCommentary, reason: str
    ) -> PackagedCommentary:
        return PackagedCommentary(
            source_event_id=commentary.event_id,
            stage=commentary.stage,
            text=f"{self.source.display_name} 向您汇报：{commentary.text}",
            provenance="minimal_persona_fallback",
            fallback=True,
            error_type=reason,
        )

    async def render(self, message: RequiredUserMessage) -> RenderedRequiredMessage:
        if not self.source.usable:
            return self._required_fallback(
                message,
                self.source.unavailable_reason or "persona_block_unavailable",
            )
        self.package_index += 1
        package_request_id = (
            f"{self.request_id}:persona-package:{message.kind}:{self.package_index}"
        )
        try:
            binder = getattr(self.provider, "bind_persona_audit_context", None)
            if callable(binder):
                binder(
                    package_request_id,
                    turn_id=message.turn_id,
                    request_ref=f"hashi-request:{self.request_id}",
                )
            text = await self.provider.package_persona_required_message(
                self.profile,
                persona_block=self.source.guidance,
                neutral_message=message.text,
                message_kind=message.kind,
                request_id=package_request_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - required content has safe fallback
            self.logger.warning(
                "HER v2 required Persona rendering failed safely: %s", exc
            )
            return self._required_fallback(message, type(exc).__name__)
        return RenderedRequiredMessage(
            source_event_id=message.event_id,
            kind=message.kind,
            text=text,
            provenance="persona_packager",
        )

    def _required_fallback(
        self, message: RequiredUserMessage, reason: str
    ) -> RenderedRequiredMessage:
        prefix = (
            f"{self.source.display_name} 向您汇报：\n\n"
            if message.kind == "final"
            else f"{self.source.display_name} 想请您确认："
        )
        return RenderedRequiredMessage(
            source_event_id=message.event_id,
            kind=message.kind,
            text=f"{prefix}{message.text}",
            provenance="minimal_persona_fallback",
            fallback=True,
            error_type=reason,
        )
