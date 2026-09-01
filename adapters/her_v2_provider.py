"""HASHI provider, delivery, and Persona bridges for HER v2."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import re
import ssl
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import httpx

from adapters import her_persona
from adapters.base import BackendResponse, TokenUsage
from adapters.openrouter_api import ProviderCallObserverError
from adapters.stream_events import (
    DELIVERY_FINAL,
    DELIVERY_INTERNAL,
    DELIVERY_REASONING,
    DELIVERY_TECHNICAL,
    DELIVERY_USER_COMMENTARY,
    KIND_ACKNOWLEDGEMENT,
    KIND_COMMENTARY,
    KIND_INITIAL_RESOLUTION,
    KIND_PROVIDER_ACTIVITY,
    KIND_TEXT_DELTA,
    KIND_THINKING,
    KIND_TOOL_END,
    KIND_TOOL_START,
    StreamCallback,
    StreamEvent,
    legacy_delivery_class,
)
from orchestrator.her_v2.audit import AuditPersistenceError, DurableAuditLog
from orchestrator.her_v2.cognitive_control import (
    COGNITIVE_DECISION_TOOL,
    StageCognitiveController,
    cognitive_system_contract,
)
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
from orchestrator.her_v2.models import (
    Stage,
    StageRequest,
    StageResponse,
    ToolEvidenceReceipt,
    ToolReceiptStatus,
)
from orchestrator.her_v2.presentation import (
    RenderedRequiredMessage,
    RequiredPersonaRenderer,
    RequiredUserMessage,
)
from orchestrator.her_v2.progress import ProviderActivityTracker
from orchestrator.her_v2.prompts import (
    render_direct_system_prompt,
    render_execution_system_prompt,
    render_finalisation_system_prompt,
    render_immediate_response_system_prompt,
    render_internal_stage_system_prompt,
    render_persona_commentary_system_prompt,
    render_review_system_prompt,
    render_stage_prompt,
    uses_complete_system_prompt,
)
from orchestrator.her_v2.retry import (
    DEFAULT_PROVIDER_RETRY_POLICY,
    ProviderRetryPolicy,
)
from orchestrator.multimodal_contract import (
    MultimodalContractError,
    attachment_manifest,
    canonical_request_content,
    native_attachment_reference_aliases,
    route_request_content,
    routing_decisions_payload,
    request_content_is_voice_origin,
    subset_request_content,
    validate_authorized_media_references,
)
from orchestrator.voice_transcript_gate import await_authorized_transcript
from tools.meter_cost import PerCallUsageLineItem
from tools.smart_tools import smart_tool_spec
from tools.token_tracker import resolve_cost_source

_HASHI_VERIFICATION_POLICY_ARGUMENT = "_hashi_verification_policy"
_PERSONA_COMMENTARY_AGENT_FAILED_FIELD = "persona_commentary_agent_failed"


def _optional_nonnegative_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _optional_nonnegative_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return round(max(0.0, float(value)), 3)
    except (TypeError, ValueError):
        return None


def _persona_commentary_agent_failure(text: str) -> tuple[bool, str]:
    """Recognise only the Persona commentary agent's explicit JSON failure."""

    try:
        payload = json.loads(str(text or "").strip())
    except (json.JSONDecodeError, TypeError):
        return False, ""
    if not isinstance(payload, Mapping) or (
        payload.get(_PERSONA_COMMENTARY_AGENT_FAILED_FIELD) is not True
    ):
        return False, ""

    raw_reason = payload.get("reason")
    reason = raw_reason if isinstance(raw_reason, str) else ""
    return True, reason or "unspecified"


def _backend_tool_control(backend: Any) -> tuple[bool, bool]:
    """Return declared tool capability and HASHI isolation capability."""

    capabilities = getattr(backend, "capabilities", None)
    supports_tools = bool(getattr(capabilities, "supports_tool_use", False))
    controls_tools = hasattr(backend, "tool_registry")
    return supports_tools, controls_tools


def _media_fallback_modalities(registry: Any) -> frozenset[str]:
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


def _argument_reference_values(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, Mapping):
        values: set[str] = set()
        for item in value.values():
            values.update(_argument_reference_values(item))
        return values
    if isinstance(value, (list, tuple, set, frozenset)):
        values = set()
        for item in value:
            values.update(_argument_reference_values(item))
        return values
    return set()


def _reference_matches_attachment(
    value: str,
    *,
    attachment_id: str,
    local_ref: str,
) -> bool:
    normalized = str(value or "").strip()
    return bool(
        (local_ref and normalized == local_ref)
        or (attachment_id and normalized == attachment_id)
        or (attachment_id and normalized.endswith(f":{attachment_id}"))
    )


def _matched_attachment_ids(
    arguments: Mapping[str, Any],
    manifest: tuple[Mapping[str, Any], ...],
) -> tuple[str, ...]:
    values = {
        item
        for item in _argument_reference_values(arguments)
        if not item.strip().casefold().startswith("data:")
    }
    matched: list[str] = []
    for attachment in manifest:
        attachment_id = str(attachment.get("attachment_id") or "")
        local_ref = str(attachment.get("local_ref") or "")
        aliases = native_attachment_reference_aliases(
            manifest,
            {attachment_id},
        )
        if any(
            _reference_matches_attachment(
                value,
                attachment_id=attachment_id,
                local_ref=local_ref,
            )
            or value in aliases
            or Path(value).name in aliases
            for value in values
        ):
            matched.append(attachment_id)
    return tuple(dict.fromkeys(matched))


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
            "attachment_id": metadata.get("attachment_id"),
            "media_routing": list(metadata.get("multimodal_routing") or []),
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
    result: list[str] = []
    for item in _registry_tool_definitions(registry):
        name = str((item.get("function") or {}).get("name") or "").strip()
        if name:
            result.append(name)
    return tuple(result)


def _registry_tool_definitions(registry: Any) -> tuple[dict[str, Any], ...]:
    definitions = getattr(registry, "get_tool_definitions", None)
    if not callable(definitions):
        return ()
    try:
        available = definitions(tiers=None)
    except TypeError:
        available = definitions()
    if not isinstance(available, Sequence) or isinstance(available, (str, bytes)):
        return ()
    return tuple(dict(item) for item in available if isinstance(item, Mapping))


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
    *,
    goal: str,
    relevant_habits: Sequence[str],
    draft_response: str,
    reviewer_findings: Mapping[str, Any] | None,
    completion_evidence: Mapping[str, Any],
) -> str:
    return render_finalisation_system_prompt(
        goal=goal,
        relevant_habits=relevant_habits,
        draft_response=draft_response,
        reviewer_findings=reviewer_findings,
        completion_evidence=completion_evidence,
        guidance=source.guidance,
        display_name=source.display_name,
        usable=source.usable,
        persona_block_begin=her_persona.PERSONA_BLOCK_BEGIN,
        persona_block_end=her_persona.PERSONA_BLOCK_END,
    )


def _immediate_response_system_prompt(
    source: her_persona.HERPersonaPackagingSource,
    *,
    goal: str,
) -> str:
    return render_immediate_response_system_prompt(
        goal=goal,
        guidance=source.guidance,
        display_name=source.display_name,
        usable=source.usable,
        persona_block_begin=her_persona.PERSONA_BLOCK_BEGIN,
        persona_block_end=her_persona.PERSONA_BLOCK_END,
    )


def _execution_system_prompt(
    source: her_persona.HERPersonaPackagingSource,
    *,
    goal: str,
    relevant_habits: Sequence[str],
    active_plan: Mapping[str, Any] | None,
    delegated_execution: Mapping[str, Any] | None,
    strategy_handoff: Mapping[str, Any] | None,
    tool_catalogue: list[Mapping[str, Any]],
) -> str:
    return render_execution_system_prompt(
        goal=goal,
        relevant_habits=relevant_habits,
        active_plan=active_plan,
        delegated_execution=delegated_execution,
        strategy_handoff=strategy_handoff,
        tool_catalogue=tool_catalogue,
        guidance=source.guidance,
        display_name=source.display_name,
        usable=source.usable,
        persona_block_begin=her_persona.PERSONA_BLOCK_BEGIN,
        persona_block_end=her_persona.PERSONA_BLOCK_END,
    )


def _direct_system_prompt(
    source: her_persona.HERPersonaPackagingSource,
    *,
    goal: str,
    habit_catalogue: Sequence[str],
    skills_catalogue: Sequence[Mapping[str, Any]],
    tool_catalogue: Sequence[Mapping[str, Any]],
    strategy_playbook: Mapping[str, Any] | None,
) -> str:
    return render_direct_system_prompt(
        goal=goal,
        habit_catalogue=habit_catalogue,
        skills_catalogue=skills_catalogue,
        tool_catalogue=tool_catalogue,
        strategy_playbook=strategy_playbook,
        guidance=source.guidance,
        display_name=source.display_name,
        usable=source.usable,
        persona_block_begin=her_persona.PERSONA_BLOCK_BEGIN,
        persona_block_end=her_persona.PERSONA_BLOCK_END,
    )


def _review_system_prompt(
    *,
    goal: str,
    relevant_habits: Sequence[str],
    active_plan_id: str | None,
    active_plan: Mapping[str, Any] | None,
    draft_response: str,
    execution_record: Mapping[str, Any] | None,
    evidence_refs: Sequence[str],
    review_kind: str,
    findings_to_close: Sequence[str],
    available_review_tools: list[Mapping[str, Any]],
) -> str:
    return render_review_system_prompt(
        goal=goal,
        relevant_habits=relevant_habits,
        active_plan_id=active_plan_id,
        active_plan=active_plan,
        draft_response=draft_response,
        execution_record=execution_record,
        evidence_refs=evidence_refs,
        review_kind=review_kind,
        findings_to_close=findings_to_close,
        available_review_tools=available_review_tools,
    )


class _DelegatedToolRegistry:
    """Narrow a HASHI ToolRegistry without copying secrets or policy logic."""

    def __init__(
        self,
        base: Any,
        delegated_tools: list[str],
        *,
        read_only: bool = False,
        verification_policy: Mapping[str, Any] | None = None,
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
        self._verification_policy = dict(verification_policy or {})
        if read_only:
            self.audit_context.update(
                {
                    "safety_mode": "read_only",
                    "authority_mode": "her_v2_shadow",
                }
            )
        elif self._verification_policy:
            self.audit_context.update(
                {
                    "safety_mode": "workspace_verification",
                    "authority_mode": "her_v2_review_verification",
                    "verification_execution_elapsed_s": self._verification_policy.get(
                        "execution_elapsed_s"
                    ),
                }
            )

    @property
    def base(self) -> Any:
        return self._base

    def is_allowed(self, tool_name: str) -> bool:
        return str(tool_name) in self._allowed

    def is_read_only(self, tool_name: str) -> bool:
        return self.is_allowed(tool_name) and _registry_is_read_only(
            self._base, tool_name
        )

    def get_tool_definitions(self, tiers=None):
        del tiers
        definitions = _registry_tool_definitions(self._base)
        return [
            item
            for item in definitions
            if str((item.get("function") or {}).get("name") or "") in self._allowed
        ]

    def evaluate_admission(
        self, tool_name: str, arguments: dict, tool_call_id: str = ""
    ):
        """Evaluate delegated and shared policy without dispatching a tool."""

        if not self.is_allowed(tool_name):
            from tools.registry import ToolResult

            return ToolResult(
                tool_call_id=tool_call_id,
                output=(
                    f"Error: tool {tool_name!r} is outside this sub-agent's "
                    "delegated authority"
                ),
                is_error=True,
                details={"control_disposition": "denied"},
            )
        scoped_evaluator = getattr(
            self._base, "evaluate_admission_with_audit_context", None
        )
        if callable(scoped_evaluator):
            return scoped_evaluator(
                tool_name,
                arguments,
                tool_call_id,
                audit_context=self.audit_context,
            )
        evaluator = getattr(self._base, "evaluate_admission", None)
        if callable(evaluator):
            return evaluator(tool_name, arguments, tool_call_id)
        return None

    async def execute(self, tool_name: str, arguments: dict, tool_call_id: str = ""):
        return await self.execute_with_audit_context(
            tool_name, arguments, tool_call_id
        )

    async def execute_with_audit_context(
        self,
        tool_name: str,
        arguments: dict,
        tool_call_id: str = "",
        *,
        audit_context: Mapping[str, Any] | None = None,
    ):
        scoped_context = dict(self.audit_context)
        scoped_context.update(dict(audit_context or {}))
        if self.is_allowed(tool_name):
            effective_arguments = dict(arguments or {})
            if tool_name == "verification_run" and self._verification_policy:
                # The model cannot select or reduce the runtime timeout basis.  This
                # request-local value is added after schema validation and never
                # mutates the shared ToolRegistry used by concurrent turns.
                effective_arguments[_HASHI_VERIFICATION_POLICY_ARGUMENT] = dict(
                    self._verification_policy
                )
            scoped_execute = getattr(self._base, "execute_with_audit_context", None)
            if callable(scoped_execute):
                return await scoped_execute(
                    tool_name,
                    effective_arguments,
                    tool_call_id,
                    audit_context=scoped_context,
                )
            return await self._base.execute(
                tool_name, effective_arguments, tool_call_id
            )
        from tools.registry import ToolResult

        result = ToolResult(
            tool_call_id=tool_call_id,
            output=(
                f"Error: tool {tool_name!r} is outside this sub-agent's delegated authority"
            ),
            is_error=True,
            details={"control_disposition": "denied"},
        )
        denial_recorder = getattr(self.base, "record_delegated_denial", None)
        if callable(denial_recorder):
            recorded = denial_recorder(
                tool_name,
                arguments,
                result,
                audit_context=scoped_context,
            )
            if recorded is not None:
                result = recorded
        return result


def _evidence_ref_segment(value: Any, *, fallback: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip())
    return normalized.strip("-") or fallback


class _EvidenceRecordingToolRegistry:
    """Attach exact, current-invocation receipts to completed tool calls."""

    def __init__(
        self,
        base: Any,
        request: StageRequest,
        *,
        model: str = "",
        provider: str = "",
        audit_log: DurableAuditLog | None = None,
    ):
        self._base = base
        self._request = request
        self._audit_context = {
            "task_id": str(request.turn_id or ""),
            "stage": request.stage.value,
            "model": str(model or ""),
        }
        self._receipts: list[ToolEvidenceReceipt] = []
        self._provider = str(provider or "")
        self._model = str(model or "")
        self._audit_log = audit_log
        self._serial = 0
        self.max_loops = None

    @property
    def receipts(self) -> tuple[ToolEvidenceReceipt, ...]:
        return tuple(self._receipts)

    @property
    def base(self) -> Any:
        return getattr(self._base, "base", self._base)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)

    def _receipt(
        self,
        *,
        tool_name: str,
        tool_call_id: str,
        result: Any | None,
        completed: bool,
        status: ToolReceiptStatus,
        attachment_ids: tuple[str, ...] = (),
    ) -> ToolEvidenceReceipt:
        self._serial += 1
        effective_call_id = str(tool_call_id or f"call-{self._serial}")
        invocation = str(
            self._request.invocation_id
            or (
                f"{self._request.turn_id}:{self._request.stage.value}:"
                f"{self._request.attempt}"
            )
        )
        evidence_ref = ":".join(
            (
                "hashi-tool",
                _evidence_ref_segment(self._request.turn_id, fallback="turn"),
                self._request.stage.value,
                "invocation",
                _evidence_ref_segment(invocation, fallback="unknown"),
                "attempt",
                str(self._request.attempt),
                "call",
                _evidence_ref_segment(
                    effective_call_id, fallback=f"call-{self._serial}"
                ),
                "receipt",
                str(self._serial),
            )
        )
        output = str(getattr(result, "output", "") or "")
        details = dict(getattr(result, "details", None) or {})
        details.setdefault("receipt_serial", self._serial)
        if self._request.role.startswith("sub_agent:"):
            details.setdefault("plan_id", self._request.plan_id)
            details.setdefault(
                "assignment_id",
                str(self._request.context.get("assignment_id") or ""),
            )
        if attachment_ids:
            details["attachment_ids"] = list(attachment_ids)
        return ToolEvidenceReceipt(
            evidence_ref=evidence_ref,
            stage=self._request.stage,
            invocation_id=invocation,
            attempt=self._request.attempt,
            tool_call_id=effective_call_id,
            tool_name=str(tool_name),
            status=status,
            read_only=_registry_is_read_only(self._base, tool_name),
            completed=completed,
            output_sha256=hashlib.sha256(output.encode("utf-8")).hexdigest(),
            details=details,
        )

    async def execute(self, tool_name: str, arguments: dict, tool_call_id: str = ""):
        effective_call_id = str(tool_call_id or f"call-{self._serial + 1}")
        invocation_id = str(
            self._request.invocation_id
            or (
                f"{self._request.turn_id}:{self._request.stage.value}:"
                f"{self._request.attempt}"
            )
        )
        operation_id = (
            f"{invocation_id}:attempt:{self._request.attempt}:"
            f"tool:{effective_call_id}"
        )
        if self._audit_log is not None:
            arguments_digest = hashlib.sha256(
                json.dumps(
                    dict(arguments or {}),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
            ).hexdigest()
            self._audit_log.append(
                event_id=(
                    f"{self._request.invocation_id or self._request.turn_id}:"
                    f"attempt:{self._request.attempt}:"
                    f"tool:{effective_call_id}:intent"
                ),
                turn_id=self._request.turn_id,
                request_ref=self._request.request_ref,
                stage=self._request.stage.value,
                role=self._request.role,
                event="tool_intent",
                provider=self._provider,
                model=self._model,
                attempt=self._request.attempt,
                plan_id=self._request.plan_id or None,
                payload={
                    "operation_id": operation_id,
                    "invocation_id": invocation_id,
                    "attempt": self._request.attempt,
                    "tool_call_id": effective_call_id,
                    "tool_name": str(tool_name),
                    "arguments_sha256": "sha256:" + arguments_digest,
                    "read_only": _registry_is_read_only(self._base, tool_name),
                },
            )
        matched_attachment_ids = _matched_attachment_ids(
            dict(arguments or {}), self._request.attachment_manifest
        )
        try:
            scoped_execute = getattr(
                self._base, "execute_with_audit_context", None
            )
            if callable(scoped_execute):
                result = await scoped_execute(
                    tool_name,
                    arguments,
                    tool_call_id,
                    audit_context=self._audit_context,
                )
            else:
                result = await self._base.execute(
                    tool_name, arguments, tool_call_id
                )
        except asyncio.CancelledError:
            receipt = self._receipt(
                    tool_name=tool_name,
                    tool_call_id=effective_call_id,
                    result=None,
                    completed=False,
                    status=ToolReceiptStatus.CANCELLED,
                    attachment_ids=matched_attachment_ids,
                )
            self._receipts.append(receipt)
            self._record_recovery_receipt(receipt)
            raise
        except Exception:
            receipt = self._receipt(
                    tool_name=tool_name,
                    tool_call_id=effective_call_id,
                    result=None,
                    completed=False,
                    status=ToolReceiptStatus.FAILED,
                    attachment_ids=matched_attachment_ids,
                )
            self._receipts.append(receipt)
            self._record_recovery_receipt(receipt)
            raise

        receipt = self._receipt(
            tool_name=tool_name,
            tool_call_id=effective_call_id,
            result=result,
            completed=True,
            status=(
                ToolReceiptStatus.FAILED
                if bool(getattr(result, "is_error", False))
                else ToolReceiptStatus.SUCCESS
            ),
            attachment_ids=matched_attachment_ids,
        )
        self._receipts.append(receipt)
        self._record_recovery_receipt(receipt)
        return self._attach_receipt(result, receipt, fallback_call_id=tool_call_id)

    def _record_recovery_receipt(self, receipt: ToolEvidenceReceipt) -> None:
        if self._audit_log is None:
            return
        payload = {
            "evidence_ref": receipt.evidence_ref,
            "stage": receipt.stage.value,
            "invocation_id": receipt.invocation_id,
            "attempt": receipt.attempt,
            "tool_call_id": receipt.tool_call_id,
            "tool_name": receipt.tool_name,
            "status": receipt.status.value,
            "read_only": receipt.read_only,
            "completed": receipt.completed,
            "output_sha256": receipt.output_sha256,
            "details": dict(receipt.details),
        }
        operation_id = (
            f"{receipt.invocation_id}:attempt:{receipt.attempt}:"
            f"tool:{receipt.tool_call_id}"
        )
        payload["operation_id"] = operation_id
        self._audit_log.append(
            event_id=(
                f"{self._request.invocation_id or self._request.turn_id}:"
                f"attempt:{self._request.attempt}:"
                f"tool:{receipt.tool_call_id}:receipt"
            ),
            turn_id=self._request.turn_id,
            request_ref=self._request.request_ref,
            stage=self._request.stage.value,
            role=self._request.role,
            event="tool_receipt",
            provider=self._provider,
            model=self._model,
            attempt=self._request.attempt,
            plan_id=self._request.plan_id or None,
            payload={
                "operation_id": operation_id,
                "tool_call_id": receipt.tool_call_id,
                "receipt": payload,
            },
        )

    async def record_policy_denial(
        self,
        tool_name: str,
        arguments: dict,
        tool_call_id: str,
        *,
        output: str,
        decision: str,
    ):
        """Record a provider-front-door policy denial as a completed receipt."""

        from tools.registry import ToolResult

        result = ToolResult(
            tool_call_id=tool_call_id,
            output=str(output),
            is_error=True,
            details={"control_disposition": str(decision or "denied")},
        )
        return self._record_denial_result(
            tool_name,
            arguments,
            tool_call_id,
            result,
        )

    def record_immediate_denial_if_any(
        self,
        tool_name: str,
        arguments: dict,
        tool_call_id: str,
    ):
        """Record a policy-only denial before periodic admission gating."""

        evaluator = getattr(self._base, "evaluate_admission", None)
        if not callable(evaluator):
            return None
        result = evaluator(tool_name, arguments, tool_call_id)
        if result is None:
            return None
        return self._record_denial_result(
            tool_name,
            arguments,
            tool_call_id,
            result,
        )

    def _record_denial_result(
        self,
        tool_name: str,
        arguments: dict,
        tool_call_id: str,
        result: Any,
    ):
        denial_recorder = getattr(self.base, "record_delegated_denial", None)
        if callable(denial_recorder):
            scoped_context = dict(
                getattr(self._base, "audit_context", {}) or {}
            )
            scoped_context.update(self._audit_context)
            recorded = denial_recorder(
                tool_name,
                arguments,
                result,
                audit_context=scoped_context,
            )
            if recorded is not None:
                result = recorded
        receipt = self._receipt(
            tool_name=tool_name,
            tool_call_id=str(
                getattr(result, "tool_call_id", "") or tool_call_id
            ),
            result=result,
            completed=True,
            status=ToolReceiptStatus.FAILED,
        )
        self._receipts.append(receipt)
        return self._attach_receipt(result, receipt, fallback_call_id=tool_call_id)

    @staticmethod
    def _attach_receipt(
        result: Any, receipt: ToolEvidenceReceipt, *, fallback_call_id: str
    ):
        from tools.registry import ToolResult

        details = dict(getattr(result, "details", None) or {})
        output = str(getattr(result, "output", "") or "")
        if not details.get("smart_result"):
            output += f"\n\nHASHI_EVIDENCE_RECEIPT: {receipt.evidence_ref}"
        details.update(
            {
                "evidence_ref": receipt.evidence_ref,
                "receipt_status": receipt.status.value,
                "receipt_completed": receipt.completed,
            }
        )
        return ToolResult(
            tool_call_id=str(getattr(result, "tool_call_id", "") or fallback_call_id),
            output=output,
            is_error=bool(getattr(result, "is_error", False)),
            content=getattr(result, "content", None),
            details=details,
        )

    def receipt_for_result(self, result: Any) -> ToolEvidenceReceipt:
        details = dict(getattr(result, "details", None) or {})
        evidence_ref = str(details.get("evidence_ref") or "")
        for receipt in reversed(self._receipts):
            if receipt.evidence_ref == evidence_ref:
                return receipt
        raise RuntimeError("completed tool result has no matching evidence receipt")


class _CompulsoryReplanToolRegistry:
    """Run compulsory Replanning at exact Execution tool boundaries."""

    def __init__(
        self,
        base: _EvidenceRecordingToolRegistry,
        coordinator: Any,
        *,
        bound_plan_id: str = "",
        enforce_plan_binding: bool = False,
    ):
        self._base = base
        self._coordinator = coordinator
        self._bound_plan_id = str(bound_plan_id or "")
        self._enforce_plan_binding = bool(enforce_plan_binding)
        self.max_loops = None

    @property
    def base(self) -> Any:
        return self._base.base

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)

    def _superseding_directive(self):
        directive = getattr(self._coordinator, "latest_directive", None)
        if (
            not self._enforce_plan_binding
            or directive is None
            or not self._bound_plan_id
            or str(directive.active_plan_id or "") == self._bound_plan_id
        ):
            return None
        return directive

    def _superseded_message(self, directive: Any, *, tool_executed: bool) -> str:
        boundary = (
            "The tool result above completed before the replacement plan became active."
            if tool_executed
            else "The requested tool was not executed."
        )
        return (
            "HASHI_PLAN_SUPERSEDED\n"
            f"bound_plan_id: {self._bound_plan_id}\n"
            f"active_plan_id: {directive.active_plan_id}\n"
            f"checkpoint_id: {directive.checkpoint_id}\n"
            f"{boundary} This bounded assignment belongs to the superseded plan. "
            "Do not call another tool or adopt the replacement plan. Return only a "
            "truthful bounded result describing completed evidence and remaining work."
        )

    async def execute(self, tool_name: str, arguments: dict, tool_call_id: str = ""):
        superseding = self._superseding_directive()
        if superseding is not None:
            return await self._base.record_policy_denial(
                tool_name,
                arguments,
                tool_call_id,
                output=self._superseded_message(
                    superseding,
                    tool_executed=False,
                ),
                decision="plan_superseded",
            )
        immediate_denial = self._base.record_immediate_denial_if_any(
            tool_name,
            arguments,
            tool_call_id,
        )
        if immediate_denial is not None:
            await self._coordinator.record_immediate_result(
                self._base.receipt_for_result(immediate_denial),
                result_summary=str(getattr(immediate_denial, "output", "") or ""),
            )
            return immediate_denial
        admission = await self._coordinator.before_tool(
            tool_name=tool_name,
            arguments=arguments,
            tool_call_id=tool_call_id,
        )
        if not admission.admitted:
            from tools.registry import ToolResult

            directive = admission.directive
            if directive is None:
                raise RuntimeError("Replan-blocked admission has no directive")
            if (
                self._enforce_plan_binding
                and self._bound_plan_id
                and str(directive.active_plan_id or "") != self._bound_plan_id
            ):
                output = self._superseded_message(directive, tool_executed=False)
                disposition = "plan_superseded"
            else:
                output = directive.execution_control_message(
                    requested_tool_executed=False
                )
                disposition = "compulsory_replan"
            return ToolResult(
                tool_call_id=tool_call_id,
                output=output,
                is_error=True,
                details={
                    "control_disposition": disposition,
                    "checkpoint_id": directive.checkpoint_id,
                    "completion_percent": directive.outcome.completion_percent,
                    "plan_changed": directive.outcome.plan_changed,
                    "tool_executed": False,
                },
            )
        try:
            result = await self._base.execute(tool_name, arguments, tool_call_id)
            receipt = self._base.receipt_for_result(result)
            details = dict(getattr(result, "details", None) or {})
            directive = await self._coordinator.after_tool(
                admission,
                receipt,
                result_summary=str(getattr(result, "output", "") or ""),
                immediate_safety_result=(
                    str(details.get("control_disposition") or "")
                    in {"approval_required", "denied", "user_input_required"}
                ),
            )
        except BaseException:
            await self._coordinator.abandon_tool(admission)
            raise
        if directive is not None:
            from tools.registry import ToolResult

            output = str(getattr(result, "output", "") or "")
            plan_superseded = bool(
                self._enforce_plan_binding
                and self._bound_plan_id
                and str(directive.active_plan_id or "") != self._bound_plan_id
            )
            control_message = (
                self._superseded_message(directive, tool_executed=True)
                if plan_superseded
                else directive.execution_control_message(requested_tool_executed=True)
            )
            output = f"{output.rstrip()}\n\n{control_message}"
            replan_details = dict(getattr(result, "details", None) or {})
            replan_details.update(
                {
                    "control_disposition": (
                        "plan_superseded" if plan_superseded else "compulsory_replan"
                    ),
                    "checkpoint_id": directive.checkpoint_id,
                    "completion_percent": directive.outcome.completion_percent,
                    "plan_changed": directive.outcome.plan_changed,
                    "tool_executed": True,
                }
            )
            result = ToolResult(
                tool_call_id=str(getattr(result, "tool_call_id", "") or tool_call_id),
                output=output,
                is_error=bool(getattr(result, "is_error", False)),
                content=getattr(result, "content", None),
                details=replan_details,
            )
        return result

    async def record_policy_denial(
        self,
        tool_name: str,
        arguments: dict,
        tool_call_id: str,
        *,
        output: str,
        decision: str,
    ):
        result = await self._base.record_policy_denial(
            tool_name,
            arguments,
            tool_call_id,
            output=output,
            decision=decision,
        )
        await self._coordinator.record_immediate_result(
            self._base.receipt_for_result(result),
            result_summary=str(getattr(result, "output", "") or ""),
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
        return getattr(self._base, "base", self._base)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)


class _CognitiveControlToolRegistry:
    """Expose a typed decision boundary when tool evidence stops changing.

    This wrapper is request-local and stage-neutral.  It does not count tool
    calls or impose an execution ceiling.  Ordinary tools stay available for
    as long as they produce semantically distinct evidence.  After a detected
    cycle, only the internal cognitive decision tool is advertised until the
    same model finalises, reports a blocker, or records a distinct hypothesis
    with a narrow tool set and explicit stop condition.
    """

    def __init__(
        self,
        base: Any,
        request: StageRequest,
        *,
        audit_log: DurableAuditLog | None = None,
        provider: str = "",
        model: str = "",
    ) -> None:
        self._base = base
        self._request = request
        self._audit_log = audit_log
        self._provider = str(provider or "")
        self._model = str(model or "")
        self._audit_serial = 0
        self.controller = StageCognitiveController(
            stage=request.stage.value,
            goal=request.goal,
        )
        self.max_loops = None

    @property
    def base(self) -> Any:
        return getattr(self._base, "base", self._base)

    @property
    def cognitive_final_response_required(self) -> bool:
        return self.controller.final_response_required

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)

    def _base_definitions(self, tiers=None) -> list[dict[str, Any]]:
        getter = getattr(self._base, "get_tool_definitions", None)
        if not callable(getter):
            return []
        try:
            raw = getter(tiers=tiers)
        except TypeError:
            raw = getter(tiers)
        return [dict(item) for item in raw if isinstance(item, Mapping)]

    def _all_base_tool_names(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                str((item.get("function") or {}).get("name") or "").strip()
                for item in self._base_definitions(None)
                if str((item.get("function") or {}).get("name") or "").strip()
            )
        )

    def get_tool_definitions(self, tiers=None):
        if self.controller.awaiting_decision:
            return [self.controller.decision_schema()]
        if self.controller.final_response_required:
            return []
        definitions = self._base_definitions(tiers)
        allowed = self.controller.active_tool_allowlist
        if allowed is None:
            return definitions
        return [
            item
            for item in definitions
            if str((item.get("function") or {}).get("name") or "") in allowed
        ]

    def allowed_tool_names(self) -> tuple[str, ...]:
        if self.controller.awaiting_decision:
            return (COGNITIVE_DECISION_TOOL,)
        if self.controller.final_response_required:
            return ()
        allowed = self.controller.active_tool_allowlist
        names = self._all_base_tool_names()
        return (
            names
            if allowed is None
            else tuple(name for name in names if name in allowed)
        )

    def is_allowed(self, tool_name: str) -> bool:
        name = str(tool_name or "")
        if self.controller.awaiting_decision:
            return name == COGNITIVE_DECISION_TOOL
        if self.controller.final_response_required:
            return False
        allowed = self.controller.active_tool_allowlist
        if allowed is not None and name not in allowed:
            return False
        checker = getattr(self._base, "is_allowed", None)
        return (
            bool(checker(name))
            if callable(checker)
            else name in self._all_base_tool_names()
        )

    def is_read_only(self, tool_name: str) -> bool:
        if str(tool_name or "") == COGNITIVE_DECISION_TOOL:
            return True
        checker = getattr(self._base, "is_read_only", None)
        return bool(checker(tool_name)) if callable(checker) else False

    def evaluate_admission(
        self, tool_name: str, arguments: dict, tool_call_id: str = ""
    ):
        if str(tool_name or "") == COGNITIVE_DECISION_TOOL:
            return None
        evaluator = getattr(self._base, "evaluate_admission", None)
        return (
            evaluator(tool_name, arguments, tool_call_id)
            if callable(evaluator)
            else None
        )

    def _audit(self, event: str, payload: Mapping[str, Any]) -> None:
        if self._audit_log is None:
            return
        self._audit_serial += 1
        invocation = str(
            self._request.invocation_id
            or (
                f"{self._request.turn_id}:{self._request.stage.value}:"
                f"{self._request.attempt}"
            )
        )
        self._audit_log.append(
            event_id=(f"{invocation}:cognitive:{self._audit_serial}:{event}"),
            turn_id=self._request.turn_id,
            request_ref=self._request.request_ref,
            stage=self._request.stage.value,
            role=self._request.role,
            event=event,
            provider=self._provider,
            model=self._model,
            attempt=self._request.attempt,
            plan_id=self._request.plan_id or None,
            payload=dict(payload),
        )

    @staticmethod
    def _result(
        *,
        tool_call_id: str,
        output: str,
        is_error: bool,
        details: Mapping[str, Any] | None = None,
        content: Any = None,
    ):
        from tools.registry import ToolResult

        return ToolResult(
            tool_call_id=str(tool_call_id or ""),
            output=str(output),
            is_error=bool(is_error),
            content=content,
            details=dict(details or {}),
        )

    async def execute(self, tool_name: str, arguments: dict, tool_call_id: str = ""):
        name = str(tool_name or "")
        if name == COGNITIVE_DECISION_TOOL:
            decision, rejected = self.controller.decide(
                arguments,
                available_tools=self._all_base_tool_names(),
            )
            self._audit(
                "cognitive_decision_rejected" if rejected else "cognitive_decision",
                {
                    "decision": decision,
                    "state": self.controller.snapshot(),
                },
            )
            return self._result(
                tool_call_id=tool_call_id,
                output=json.dumps(
                    decision,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                is_error=rejected,
                details={
                    "control_disposition": (
                        "cognitive_decision_rejected"
                        if rejected
                        else "cognitive_decision"
                    ),
                    "cognitive_control": self.controller.snapshot(),
                },
            )

        if not self.is_allowed(name):
            payload = self.controller.interrupt_payload()
            return self._result(
                tool_call_id=tool_call_id,
                output=json.dumps(
                    {
                        "status": "blocked",
                        "code": "COGNITIVE_DECISION_REQUIRED",
                        "cognitive_interrupt": payload or None,
                        "instruction": (
                            "Use the currently advertised cognitive decision "
                            "boundary or return the normal stage response; do not "
                            "call another ordinary tool."
                        ),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                is_error=True,
                details={
                    "control_disposition": "cognitive_interrupt",
                    "cognitive_control": self.controller.snapshot(),
                },
            )

        result = await self._base.execute(name, arguments, tool_call_id)
        details = dict(getattr(result, "details", None) or {})
        interrupt = self.controller.observe(
            tool_name=name,
            tool_profile=smart_tool_spec(name).profile,
            arguments=arguments,
            output=str(getattr(result, "output", "") or ""),
            details=details,
            is_error=bool(getattr(result, "is_error", False)),
        )
        if interrupt is None:
            return result

        payload = self.controller.interrupt_payload()
        self._audit(
            "cognitive_interrupt",
            {
                "interrupt": interrupt.as_dict(),
                "state": self.controller.snapshot(),
            },
        )
        output = str(getattr(result, "output", "") or "").rstrip()
        output += "\n\nHASHI_COGNITIVE_INTERRUPT\n" + json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        details.update(
            {
                "control_disposition": "cognitive_interrupt",
                "cognitive_interrupt": interrupt.as_dict(),
                "cognitive_control": self.controller.snapshot(),
            }
        )
        return self._result(
            tool_call_id=str(getattr(result, "tool_call_id", "") or tool_call_id),
            output=output,
            is_error=bool(getattr(result, "is_error", False)),
            details=details,
            content=getattr(result, "content", None),
        )

    def note_provider_completion(self) -> str:
        decision = self.controller.note_provider_completion()
        if decision:
            self._audit(
                "cognitive_boundary_completed",
                {
                    "decision": decision,
                    "state": self.controller.snapshot(),
                },
            )
        return decision


class _MediaRoutingToolRegistry:
    """Prevent one attachment from taking native and fallback routes together."""

    _MEDIA_FALLBACK_TOOLS = frozenset({"media_read", "vision_inspect"})

    def __init__(
        self,
        base: Any,
        *,
        native_attachment_ids: set[str],
        native_local_refs: set[str],
        all_media_native: bool,
    ) -> None:
        self._base = base
        self.native_attachment_ids = set(native_attachment_ids)
        self.native_local_refs = set(native_local_refs)
        self.all_media_native = bool(all_media_native)
        self.max_loops = None

    @property
    def base(self) -> Any:
        return getattr(self._base, "base", self._base)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)

    def is_allowed(self, tool_name: str) -> bool:
        if self.all_media_native and tool_name in self._MEDIA_FALLBACK_TOOLS:
            return False
        return bool(getattr(self._base, "is_allowed")(tool_name))

    def get_tool_definitions(self, *args, **kwargs):
        definitions = list(self._base.get_tool_definitions(*args, **kwargs))
        if not self.all_media_native:
            return definitions
        return [
            definition
            for definition in definitions
            if str((definition.get("function") or {}).get("name") or "")
            not in self._MEDIA_FALLBACK_TOOLS
        ]

    def enable_local_media_fallback(self, attachment_ids: set[str]) -> None:
        """Release only attachments covered by one typed provider fallback."""

        released = {str(item) for item in attachment_ids if str(item).strip()}
        if not released.issubset(self.native_attachment_ids):
            raise ValueError("cannot release an attachment outside the native route")
        self.native_attachment_ids.difference_update(released)
        if not self.native_attachment_ids:
            self.native_local_refs.clear()
        self.all_media_native = False

    async def execute(self, tool_name: str, arguments: dict, tool_call_id: str = ""):
        if tool_name in self._MEDIA_FALLBACK_TOOLS:
            values = _argument_reference_values(dict(arguments or {}))
            references_native = any(
                any(
                    _reference_matches_attachment(
                        value,
                        attachment_id=attachment_id,
                        local_ref="",
                    )
                    for attachment_id in self.native_attachment_ids
                )
                or value in self.native_local_refs
                or Path(value).name in self.native_local_refs
                for value in values
            )
            if self.all_media_native or references_native:
                from tools.registry import ToolResult

                return ToolResult(
                    tool_call_id=tool_call_id,
                    output=(
                        "Error: this attachment was already supplied through the "
                        "native media route; duplicate fallback processing is blocked."
                    ),
                    is_error=True,
                    details={
                        "routing_guard": "native_media_duplicate_fallback_blocked"
                    },
                )
        return await self._base.execute(tool_name, arguments, tool_call_id)


class _AdapterDelivery(DeliveryPort):
    def __init__(
        self,
        callback: StreamCallback,
        *,
        allow_immediate_response: bool,
    ):
        self.callback = callback
        self.allow_immediate_response = bool(allow_immediate_response)

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
        content: tuple[Mapping[str, Any], ...] = (),
    ) -> DeliveryReceipt:
        if kind in {"commentary", "draft"}:
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
        if kind == "immediate" and not self.allow_immediate_response:
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
                metadata={"content": [dict(part) for part in content]},
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
                origin=(
                    "her_v2:primary_execution"
                    if commentary.draft_response
                    else "her_v2:persona_packaging"
                ),
                phase=commentary.stage.value,
                # A Primary Execution draft is prescribed user-visible output,
                # not optional stage chatter. ``allow_immediate_response`` has
                # no bearing on this reviewed-workflow message.
                required=(
                    commentary.draft_response
                    or commentary.stage is Stage.REPLANNING
                ),
                provenance=commentary.provenance,
                detail=(
                    "temporary=true; pending_review_and_finalisation=true; "
                    "exact_primary_execution_text=true"
                    if commentary.draft_response
                    else (
                        "persona_packaging_fallback=true; "
                        f"error_type={commentary.error_type or 'unknown'}"
                        if commentary.fallback
                        else "persona_packaging_fallback=false"
                    )
                ),
            )
        )
        return accepted is True if commentary.draft_response else accepted is not False

    async def deliver_activity(
        self,
        *,
        kind: str,
        text: str,
        event_id: str,
        phase: str,
        metadata: Mapping[str, Any],
    ) -> bool:
        """Publish deterministic runtime activity through the technical lane."""

        if self.callback is None:
            return False
        accepted = await self.callback(
            StreamEvent(
                kind=kind,
                summary=text,
                event_id=event_id,
                delivery_class=DELIVERY_TECHNICAL,
                origin="her_v2:runtime",
                phase=phase,
                provenance="runtime_state",
                metadata=dict(metadata),
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
        runtime_context: Any = None,
        usage_observer: Callable[[PerCallUsageLineItem], None] | None = None,
        default_recovery_kind: str = "none",
        cognitive_control_enabled: bool = False,
    ) -> None:
        self.backend_manager = backend_manager
        self.tool_registry = tool_registry
        self.on_stream_event = on_stream_event
        self.silent = silent
        self.retry_policy = retry_policy or DEFAULT_PROVIDER_RETRY_POLICY
        self.audit_log = audit_log
        self.workzone_ref = str(workzone_ref or "")
        self.runtime_context = runtime_context
        self.usage_observer = usage_observer
        self.default_recovery_kind = str(default_recovery_kind or "none")
        self.cognitive_control_enabled = bool(cognitive_control_enabled)
        self._persona_invocation_serial = 0
        self._persona_audit_contexts: dict[str, tuple[str, str]] = {}
        self.logger = logging.getLogger("HASHI.HERv2.StageProvider")
        self.usage = TokenUsage()
        self.cost_usd = 0.0
        self.tool_call_count = 0
        self.tool_loop_count = 0
        self._stage_modality_cache: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._derived_text_audio_cache: dict[str, Mapping[str, Any]] = {}
        self._derived_text_audio_paths: set[Path] = set()
        self._derived_text_audio_lock = asyncio.Lock()
        # Per-stage cost line items (Zelda /meter contract).  Populated at the
        # moment each stage/Persona invocation returns, while the real
        # profile.engine / profile.model / stage are still known.
        self.usage_line_items: list[PerCallUsageLineItem] = []
        self._observed_provider_request_ids: set[str] = set()

    async def resolve_stage_modalities(
        self, profile: ProviderProfile
    ) -> Mapping[str, Any]:
        """Resolve the exact stage model's input/output contract.

        Stage targets can select a different model from the Agent's active
        backend, so this probe uses the same ephemeral-backend selection and
        option overlay as the real invocation.  It initializes no provider
        connection and is cached for the lifetime of this Turn.
        """

        if str(profile.engine or "").strip().casefold() in {
            "codex",
            "codex-cli",
            "codex-app-server",
        }:
            raise StageInvocationError(
                "Codex is a separate HASHI backend, not an internal HER provider",
                retryable=False,
                code=ProviderFailureCode.PROVIDER_CONFIGURATION_ERROR,
                human_description=(
                    "HER v2 must use hashi-api for configured GPT models rather "
                    "than selecting the Codex backend internally."
                ),
            )
        options_fingerprint = hashlib.sha256(
            json.dumps(
                dict(profile.options),
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        cache_key = (profile.engine, profile.model, options_fingerprint)
        cached = self._stage_modality_cache.get(cache_key)
        if cached is not None:
            return dict(cached)

        try:
            backend = self.backend_manager.create_ephemeral_backend(
                profile.engine, target_model=profile.model
            )
        except Exception as exc:
            raise StageInvocationError(
                "cannot resolve configured stage modality contract for "
                f"{profile.engine}/{profile.model}: {exc}",
                retryable=False,
                code=ProviderFailureCode.PROVIDER_CONFIGURATION_ERROR,
                human_description=(
                    "The configured stage model's input capability could not "
                    "be resolved."
                ),
            ) from exc

        try:
            backend_extra = dict(getattr(backend.config, "extra", None) or {})
            backend_extra.update(dict(profile.options))
            backend.config.extra = backend_extra
            apply_multimodal = getattr(
                backend, "_apply_declared_multimodal_capabilities", None
            )
            if callable(apply_multimodal):
                apply_multimodal(backend_extra)
            capability_resolver = getattr(backend, "resolve_input_capability", None)
            capability = (
                capability_resolver()
                if callable(capability_resolver)
                else getattr(backend, "input_capability", None)
            )
            if capability is None:
                raise StageInvocationError(
                    "stage backend exposes no exact input capability",
                    retryable=False,
                    code=ProviderFailureCode.PROVIDER_CONFIGURATION_ERROR,
                    human_description=(
                        "The configured stage model exposes no exact input "
                        "capability."
                    ),
                )
            backend_capabilities = getattr(backend, "capabilities", None)
            resolved = {
                "input_modalities": tuple(sorted(capability.input_modalities)),
                "output_modalities": tuple(
                    sorted(
                        getattr(
                            backend_capabilities,
                            "output_modalities",
                            frozenset({"text"}),
                        )
                        or ()
                    )
                ),
                "input_policy": str(
                    getattr(backend_capabilities, "input_policy", "auto") or "auto"
                )
                .strip()
                .casefold(),
                "source": str(getattr(capability, "source", "unknown")),
            }
            self._stage_modality_cache[cache_key] = dict(resolved)
            return resolved
        except StageInvocationError:
            raise
        except Exception as exc:
            raise StageInvocationError(
                "invalid stage modality contract for "
                f"{profile.engine}/{profile.model}: {exc}",
                retryable=False,
                code=ProviderFailureCode.PROVIDER_CONFIGURATION_ERROR,
                human_description=(
                    "The configured stage model has an invalid input/output "
                    "modality declaration."
                ),
            ) from exc
        finally:
            with suppress(Exception):
                await backend.shutdown()

    async def materialize_text_audio(
        self,
        *,
        text: str,
        turn_id: str,
        request_ref: str,
    ) -> Mapping[str, Any]:
        """Create one request-scoped TTS transport asset for a text Turn."""

        authoritative_text = str(text or "").strip()
        if not authoritative_text:
            raise StageInvocationError(
                "text-to-audio adaptation received no authoritative text",
                retryable=False,
                code=ProviderFailureCode.INPUT_MODALITY_CONVERSION_FAILED,
                human_description=(
                    "The text request could not be converted to the audio input "
                    "required by the configured model."
                ),
            )
        cache_key = hashlib.sha256(authoritative_text.encode("utf-8")).hexdigest()
        async with self._derived_text_audio_lock:
            cached = self._derived_text_audio_cache.get(cache_key)
            if cached is not None:
                return cached

            voice_manager = getattr(self.runtime_context, "voice_manager", None)
            synthesizer = getattr(voice_manager, "synthesize_reply", None)
            if not callable(synthesizer):
                raise StageInvocationError(
                    "no HASHI TTS converter is available for text input adaptation",
                    retryable=False,
                    code=ProviderFailureCode.INPUT_MODALITY_CONVERSION_FAILED,
                    human_description=(
                        "The configured model requires audio input, but HASHI's "
                        "text-to-audio converter is unavailable."
                    ),
                )
            agent_name = str(
                getattr(self.runtime_context, "name", "hashi") or "hashi"
            )
            try:
                asset = await synthesizer(
                    agent_name,
                    f"{turn_id}-input-adaptation",
                    authoritative_text,
                    max_retries=0,
                    force=True,
                    max_chars_override=len(authoritative_text) + 1,
                )
            except Exception as exc:
                raise StageInvocationError(
                    f"text-to-audio input conversion failed: {exc}",
                    retryable=False,
                    code=ProviderFailureCode.INPUT_MODALITY_CONVERSION_FAILED,
                    human_description=(
                        "The text request could not be converted to the audio input "
                        "required by the configured model."
                    ),
                ) from exc
            if asset is None:
                raise StageInvocationError(
                    "text-to-audio input conversion produced no asset",
                    retryable=False,
                    code=ProviderFailureCode.INPUT_MODALITY_CONVERSION_FAILED,
                    human_description=(
                        "The text request could not be converted to the audio input "
                        "required by the configured model."
                    ),
                )

            audio_path = Path(asset.ogg_path).expanduser().resolve()
            try:
                payload = audio_path.read_bytes()
            except OSError as exc:
                raise StageInvocationError(
                    f"text-to-audio transport asset is unavailable: {exc}",
                    retryable=False,
                    code=ProviderFailureCode.INPUT_MODALITY_CONVERSION_FAILED,
                    human_description=(
                        "HASHI created no readable audio asset for the configured "
                        "model input."
                    ),
                ) from exc
            if not payload:
                raise StageInvocationError(
                    "text-to-audio transport asset is empty",
                    retryable=False,
                    code=ProviderFailureCode.INPUT_MODALITY_CONVERSION_FAILED,
                    human_description=(
                        "HASHI created an empty audio asset for the configured "
                        "model input."
                    ),
                )

            for candidate in {
                audio_path,
                audio_path.with_suffix(".mp3"),
                audio_path.with_suffix(".wav"),
            }:
                if candidate.exists():
                    self._derived_text_audio_paths.add(candidate)
            content = canonical_request_content(
                [
                    {
                        "type": "media",
                        "item_index": 1,
                        "attachment_id": f"derived-tts-{cache_key[:24]}",
                        "modality": "audio",
                        "kind": "derived_tts",
                        "semantic_role": "audio_attachment",
                        "mime_type": str(asset.mime_type or "audio/ogg"),
                        "filename": audio_path.name,
                        "caption": "",
                        "local_ref": str(audio_path),
                        "size_bytes": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                        "transport": {},
                    }
                ]
            )
            self._derived_text_audio_cache[cache_key] = content
            return content

    async def cleanup_text_audio(self) -> None:
        """Delete request-scoped TTS transport files after every stage settles."""

        paths = tuple(self._derived_text_audio_paths)
        self._derived_text_audio_paths.clear()
        self._derived_text_audio_cache.clear()
        for path in paths:
            with suppress(OSError):
                path.unlink()

    def _native_voice_state(self, request: StageRequest) -> dict[str, Any] | None:
        if not request_content_is_voice_origin(request.request_content):
            return None
        request_id = str(request.request_ref or "")
        if request_id.startswith("hashi-request:"):
            request_id = request_id.split(":", 1)[1]
        registry = getattr(self.runtime_context, "_native_voice_transcripts", None)
        state = registry.get(request_id) if isinstance(registry, dict) else None
        if not isinstance(state, dict):
            manifest = attachment_manifest(request.request_content)
            attachment_id = (
                str(manifest[0].get("attachment_id") or "") if manifest else ""
            )
            state = (
                registry.get(attachment_id)
                if isinstance(registry, dict) and attachment_id
                else None
            )
        return state if isinstance(state, dict) else None

    async def _released_voice_transcript(
        self, request: StageRequest
    ) -> tuple[str, str]:
        return await await_authorized_transcript(
            self._native_voice_state(request),
            require_confirmation=True,
        )

    def _record_usage_line_item(
        self,
        *,
        request_id: str,
        phase: str,
        engine: str,
        model: str,
        response: Any,
        invocation_id: str = "",
        attempt: int = 1,
        recovery_kind: str = "none",
    ) -> None:
        """Record one per-stage/per-persona usage line item with provenance."""
        metadata = getattr(response, "stream_metadata", None)
        raw_meter = metadata.get("meter") if isinstance(metadata, Mapping) else None
        raw_calls = (
            raw_meter.get("provider_calls") if isinstance(raw_meter, Mapping) else None
        )
        calls: list[Mapping[str, Any]] = (
            [item for item in raw_calls if isinstance(item, Mapping)]
            if isinstance(raw_calls, list)
            else [
                {
                    "input": int(getattr(response.usage, "input_tokens", 0) or 0),
                    "output": int(getattr(response.usage, "output_tokens", 0) or 0),
                    "thinking": int(getattr(response.usage, "thinking_tokens", 0) or 0),
                    "token_source": (
                        "provider" if response.usage is not None else "estimated"
                    ),
                    "thinking_in_output": response.usage is not None,
                    "cost_usd": getattr(response, "cost_usd", None),
                }
            ]
        )
        if not calls:
            # An explicit empty physical-call list proves that validation or
            # payload construction failed before any Provider request began.
            return

        from tools.token_tracker import calc_cost

        observed_provider_request_ids = getattr(
            self, "_observed_provider_request_ids", None
        )
        if observed_provider_request_ids is None:
            observed_provider_request_ids = set()
            self._observed_provider_request_ids = observed_provider_request_ids
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
            prompt_cache_hit_tokens = _optional_nonnegative_int(
                call.get("prompt_cache_hit_tokens")
            )
            prompt_cache_miss_tokens = _optional_nonnegative_int(
                call.get("prompt_cache_miss_tokens")
            )
            provider_call_latency_ms = _optional_nonnegative_float(
                call.get("provider_call_latency_ms")
            )
            status = str(
                call.get("status")
                or (
                    "completed"
                    if bool(getattr(response, "is_success", True))
                    else "failed_response"
                )
            )
            if token_source == "unknown" and call.get("cost_usd") is None:
                # A request without a Provider receipt may still have been
                # processed and billed. Zero-token table pricing would falsely
                # turn that uncertainty into a known $0.00 charge.
                resolved_cost, cost_source = None, "unknown"
            else:
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
                    cached_tokens=prompt_cache_hit_tokens or 0,
                    thinking_in_output=thinking_in_output,
                )
            per_call = len(calls) > 1 or bool(raw_calls)
            call_identity = "|".join(
                (
                    parent_request_id,
                    str(phase or ""),
                    str(invocation_id or ""),
                    str(max(1, int(attempt))),
                    str(index),
                    str(call.get("provider_request_id") or ""),
                )
            )
            provider_request_id = str(call.get("provider_request_id") or "").strip()
            if not provider_request_id:
                provider_request_id = "hashi-provider:" + hashlib.sha256(
                    call_identity.encode("utf-8")
                ).hexdigest()
            if provider_request_id in observed_provider_request_ids:
                # Physical-call observers write immediately. The aggregate
                # BackendResponse later repeats the same facts solely for
                # reconciliation and must never double-charge them.
                continue
            resolved_recovery_kind = str(
                call.get("recovery_kind") or recovery_kind or "none"
            )
            if resolved_recovery_kind in {"", "none"}:
                resolved_recovery_kind = str(
                    getattr(self, "default_recovery_kind", "none") or "none"
                )
            line_item = PerCallUsageLineItem(
                request_id=(
                    f"{parent_request_id}:provider-call:"
                    + hashlib.sha256(
                        provider_request_id.encode("utf-8")
                    ).hexdigest()[:16]
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
                provider_request_id=provider_request_id,
                attempt=max(1, int(call.get("attempt") or attempt or 1)),
                retry_count=max(
                    0,
                    int(call.get("retry_count") or max(0, int(attempt) - 1)),
                ),
                recovery_kind=(
                    "fresh_connection_retry"
                    if int(attempt) > 1
                    else resolved_recovery_kind
                ),
                compact=bool(call.get("compact", False)),
                status=status,
            )
            # /reboot min may retain the already-imported meter dataclass.
            # Dynamic attachment keeps mixed old/new module shapes safe.
            line_item.prompt_cache_hit_tokens = prompt_cache_hit_tokens
            line_item.prompt_cache_miss_tokens = prompt_cache_miss_tokens
            line_item.provider_call_latency_ms = provider_call_latency_ms
            observed_provider_request_ids.add(provider_request_id)
            self.usage_line_items.append(line_item)
            self._notify_usage_observer(line_item)

    def _bind_provider_call_observer(
        self,
        backend: Any,
        *,
        request_id: str,
        phase: str,
        engine: str,
        model: str,
        invocation_id: str = "",
        attempt: int = 1,
        recovery_kind: str = "none",
    ) -> None:
        """Durably account each physical Provider call as it settles."""

        setter = getattr(backend, "set_provider_call_observer", None)
        if not callable(setter):
            return

        def observe(call: Mapping[str, Any]) -> None:
            payload = dict(call)
            response = BackendResponse(
                text="",
                duration_ms=float(payload.get("provider_call_latency_ms") or 0.0),
                is_success=str(payload.get("status") or "completed") == "completed",
                usage=TokenUsage(
                    input_tokens=int(payload.get("input") or 0),
                    output_tokens=int(payload.get("output") or 0),
                    thinking_tokens=int(payload.get("thinking") or 0),
                ),
                cost_usd=payload.get("cost_usd"),
                stream_metadata={"meter": {"provider_calls": [payload]}},
            )
            self._record_usage_line_item(
                request_id=request_id,
                phase=phase,
                engine=engine,
                model=model,
                response=response,
                invocation_id=invocation_id,
                attempt=attempt,
                recovery_kind=recovery_kind,
            )

        setter(observe)

    @staticmethod
    def _accounting_observer_failure(
        error: ProviderCallObserverError,
    ) -> StageInvocationError:
        cause = error.__cause__
        if isinstance(cause, StageInvocationError):
            return cause
        return StageInvocationError(
            "Provider request accounting could not be persisted",
            retryable=False,
            code=ProviderFailureCode.AUDIT_PERSISTENCE_FAILURE,
            human_description=(
                "A physical Provider request settled, but HASHI could not "
                "durably record it; execution stopped without replay."
            ),
            details={"error_type": type(cause or error).__name__},
        )

    def usage_receipt(self, request_id: str = ""):
        """Return a structured :class:`UsageReceipt` for this provider turn."""
        from tools.meter_cost import UsageReceipt

        return UsageReceipt(
            request_id=str(request_id or ""),
            parent_request_id="",
            line_items=list(self.usage_line_items),
        )

    def _record_unreceipted_provider_attempt(
        self,
        request: StageRequest,
        *,
        engine: str,
        model: str,
        label: str,
        status: str,
    ) -> None:
        identity = "|".join(
            (
                str(request.request_ref or request.turn_id),
                request.stage.value,
                str(request.invocation_id or ""),
                str(request.attempt),
                str(label),
                str(status),
            )
        )
        line_item = PerCallUsageLineItem(
            request_id=str(request.request_ref or request.turn_id),
            parent_request_id=str(request.request_ref or ""),
            phase=request.stage.value,
            engine=str(engine),
            model=str(model),
            token_source="unknown",
            cost_usd=None,
            cost_source="unknown",
            provider_request_id="hashi-provider:" + hashlib.sha256(
                identity.encode("utf-8")
            ).hexdigest(),
            attempt=max(1, int(request.attempt)),
            retry_count=max(0, int(request.attempt) - 1),
            recovery_kind=(
                "fresh_connection_retry"
                if request.attempt > 1
                else (
                    str(getattr(self, "default_recovery_kind", "none") or "none")
                    if str(getattr(self, "default_recovery_kind", "none") or "none")
                    != "none"
                    else str(label)
                )
            ),
            status=str(status),
        )
        self.usage_line_items.append(line_item)
        self._notify_usage_observer(line_item)

    def _notify_usage_observer(self, line_item: PerCallUsageLineItem) -> None:
        """Synchronously durabilise one real call before stage progression."""

        observer = getattr(self, "usage_observer", None)
        if observer is None:
            return
        try:
            observer(line_item)
        except StageInvocationError:
            raise
        except Exception as exc:
            raise StageInvocationError(
                "Provider request accounting could not be persisted",
                retryable=False,
                code=ProviderFailureCode.AUDIT_PERSISTENCE_FAILURE,
                human_description=(
                    "The Provider answered, but HASHI could not durably record "
                    "the request usage; the stage stopped without replaying it."
                ),
                details={
                    "provider_request_id": line_item.provider_request_id,
                    "error_type": type(exc).__name__,
                },
            ) from exc

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

    def tool_catalogue(
        self,
        *,
        allow_side_effects: bool,
        delegated_tools: Sequence[str] | None = None,
    ) -> tuple[Mapping[str, Any], ...]:
        """Return the exact Registry-approved prompt catalogue."""

        if self.tool_registry is None:
            return ()
        requested = (
            [str(item).strip() for item in delegated_tools if str(item).strip()]
            if delegated_tools is not None
            else list(_registry_allowed_names(self.tool_registry))
        )
        narrowed = _DelegatedToolRegistry(
            self.tool_registry,
            requested,
            read_only=not allow_side_effects,
        )
        catalogue: list[Mapping[str, Any]] = []
        for definition in _registry_tool_definitions(narrowed):
            name = str(
                (definition.get("function") or {}).get("name") or ""
            ).strip()
            if not name:
                continue
            catalogue.append(
                {
                    **definition,
                    "hashi_read_only": _registry_is_read_only(
                        self.tool_registry, name
                    ),
                }
            )
        return tuple(catalogue)

    async def invoke(
        self, profile: ProviderProfile, request: StageRequest
    ) -> StageResponse:
        if str(profile.engine or "").strip().casefold() in {
            "codex",
            "codex-cli",
            "codex-app-server",
        }:
            raise StageInvocationError(
                "Codex is a separate HASHI backend, not an internal HER provider",
                retryable=False,
                code=ProviderFailureCode.PROVIDER_CONFIGURATION_ERROR,
                human_description=(
                    "HER v2 must use hashi-api for configured GPT models rather "
                    "than selecting the Codex backend internally."
                ),
            )
        native_audio_stage = bool(
            request.stage in {Stage.DIRECT, Stage.IMMEDIATE_RESPONSE}
            and request_content_is_voice_origin(request.request_content)
            and profile.options.get("_native_audio_route")
        )
        if native_audio_stage and not bool(
            profile.options.get("audio_model_tools", False)
        ):
            # Effort Zero normally owns the complete Direct tool catalogue.
            # The native-audio proof of concept is a narrower route: the
            # original audio and PCM are available, but no tool definitions or
            # side-effect authority reach the audio model.
            request = replace(
                request,
                allow_tools=False,
                allow_side_effects=False,
            )
        if native_audio_stage and (request.allow_tools or request.allow_side_effects):
            transcript, transcript_state = await self._released_voice_transcript(
                request
            )
            if not transcript or transcript_state != "released":
                return StageResponse(
                    text=(
                        "The actionable voice path was not authorized through "
                        "Safe Voice, so no tool-capable audio model was invoked."
                    ),
                    provider="hashi-runtime",
                    model="safe-voice-boundary",
                    media_routing=tuple(
                        {
                            "attachment_id": str(item.get("attachment_id") or ""),
                            "item_index": item.get("item_index"),
                            "modality": str(item.get("modality") or "audio"),
                            "route": "transcript_unavailable",
                            "reason": f"local_stt_{transcript_state}",
                            "transport": None,
                        }
                        for item in request.attachment_manifest
                    ),
                    validation_source="runtime_voice_boundary",
                )
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
        if native_audio_stage:
            # Provider-created output assets are initially unowned.  Bind their
            # one-time claim correlation to the outer HASHI request, not the
            # HER stage invocation ID used for provider tracing.
            request_ref = str(request.request_ref or "")
            backend_extra["_native_audio_claim_request_id"] = (
                request_ref.removeprefix("hashi-request:")
            )
        elif request_content_is_voice_origin(request.request_content):
            # Triage and work stages may hear audio, but they remain text-output
            # stages.  Only Direct/Immediate may request generated audio.
            backend_extra["_native_audio_output_disabled"] = True
        backend.config.extra = backend_extra
        apply_multimodal = getattr(
            backend, "_apply_declared_multimodal_capabilities", None
        )
        if callable(apply_multimodal):
            apply_multimodal(backend_extra)
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
        delegated = (
            request.context.get("delegated_tools")
            if selected_registry is not None
            else None
        )
        if selected_registry is not None and (
            request.role.startswith("sub_agent:")
            or not request.allow_side_effects
            or delegated is not None
        ):
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
                verification_policy=(
                    {
                        **dict(
                            request.context.get("verification_run_policy")
                            if isinstance(
                                request.context.get("verification_run_policy"),
                                Mapping,
                            )
                            else {}
                        ),
                        "execution_elapsed_s": request.context.get(
                            "execution_elapsed_s", 0.0
                        ),
                    }
                    if request.stage is Stage.REVIEW and request.allow_side_effects
                    else None
                ),
            )
        evidence_registry: _EvidenceRecordingToolRegistry | None = None
        cognitive_registry: _CognitiveControlToolRegistry | None = None
        if selected_registry is not None:
            # The Agent-level registry owns permissions, not HER v2 execution
            # length.  HER tool-enabled stages continue until the model
            # finishes, fails, or the request is cancelled.
            evidence_registry = _EvidenceRecordingToolRegistry(
                selected_registry,
                request,
                model=profile.model,
                provider=profile.engine,
                audit_log=self.audit_log,
            )
            selected_registry = evidence_registry
            if request.checkpoint_coordinator is not None:
                if request.stage is not Stage.EXECUTION:
                    await backend.shutdown()
                    raise StageInvocationError(
                        "compulsory Replan coordinator may be installed only for Execution",
                        retryable=False,
                        code=ProviderFailureCode.PROVIDER_CONFIGURATION_ERROR,
                    )
                selected_registry = _CompulsoryReplanToolRegistry(
                    evidence_registry,
                    request.checkpoint_coordinator,
                    bound_plan_id=str(request.plan_id or ""),
                    enforce_plan_binding=request.role.startswith("sub_agent:"),
                )
            if self.cognitive_control_enabled:
                cognitive_registry = _CognitiveControlToolRegistry(
                    selected_registry,
                    request,
                    audit_log=self.audit_log,
                    provider=profile.engine,
                    model=profile.model,
                )
                selected_registry = cognitive_registry
            selected_registry = _UnboundedToolRegistry(selected_registry)
        if controls_tools:
            backend.tool_registry = selected_registry
        fallback_registry = selected_registry
        backend.privacy_level = self.backend_manager.privacy_level
        media_routing: tuple[dict[str, Any], ...] = ()
        provider_request_content = request.request_content
        media_preflight_error: StageInvocationError | None = None
        local_fallback_modalities = (
            _media_fallback_modalities(selected_registry)
            if request.allow_tools
            else frozenset()
        )
        if (
            request.stage is Stage.TRIAGE
            and request_content_is_voice_origin(request.request_content)
        ):
            triage_input_policy = str(
                profile.options.get("_voice_triage_input_policy") or "auto"
            ).strip().casefold()
            capability_resolver = getattr(backend, "resolve_input_capability", None)
            triage_capability = (
                capability_resolver()
                if callable(capability_resolver)
                else getattr(backend, "input_capability", None)
            )
            triage_hears_audio = bool(
                triage_capability is not None
                and triage_capability.supports("audio")
            )
            if triage_input_policy == "native" and not triage_hears_audio:
                await backend.shutdown()
                raise StageInvocationError(
                    "voice Triage is configured native but its exact model cannot consume audio",
                    retryable=False,
                    code=ProviderFailureCode.PROVIDER_MODALITY_UNSUPPORTED,
                    human_description=(
                        "The configured native voice Triage model has no verified audio input capability."
                    ),
                )
            needs_text_transcript = (
                triage_input_policy == "transcript" or not triage_hears_audio
            )
            voice_state = self._native_voice_state(request)
            safe_voice_gate = bool(
                isinstance(voice_state, dict) and voice_state.get("safe_voice")
            )
            transcript = ""
            transcript_state = ""
            if safe_voice_gate or needs_text_transcript:
                transcript, transcript_state = await self._released_voice_transcript(
                    request
                )
                original_manifest = tuple(request.attachment_manifest)
                if not transcript or transcript_state != "released":
                    media_routing = tuple(
                        {
                            "attachment_id": str(item.get("attachment_id") or ""),
                            "item_index": item.get("item_index"),
                            "modality": str(item.get("modality") or "audio"),
                            "route": "transcript_unavailable",
                            "reason": f"local_stt_{transcript_state}",
                            "transport": None,
                        }
                        for item in original_manifest
                    )
                    await backend.shutdown()
                    return StageResponse(
                        data={
                            "classification": "DIRECT_RESPONSE",
                            "real_goal": (
                                "Keep the native no-tool voice chat response; "
                                "the transcript-dependent path is unavailable."
                            ),
                            "selected_strategy_cards": [],
                            "relevant_habits": [],
                            "execution_brief": {
                                "strategy": "",
                                "stages": [],
                                "dependencies": [],
                                "verification": [],
                                "success_criteria": [],
                                "replan_conditions": [],
                            },
                            "clarification": "",
                        },
                        provider="hashi-runtime",
                        model="safe-voice-boundary",
                        media_routing=media_routing,
                        validation_source="runtime_voice_boundary",
                    )
            if needs_text_transcript:
                original_manifest = tuple(request.attachment_manifest)
                media_routing = tuple(
                    {
                        "attachment_id": str(item.get("attachment_id") or ""),
                        "item_index": item.get("item_index"),
                        "modality": str(item.get("modality") or "audio"),
                        "route": "local_transcript",
                        "reason": "text_triage_uses_released_local_stt",
                        "transport": None,
                    }
                    for item in original_manifest
                )
                request = replace(
                    request,
                    goal=(
                        f"[Local voice transcription]\n{transcript}\n\n"
                        f"[Original user caption/request]\n{request.goal}"
                    ),
                    request_content=None,
                    attachment_manifest=(),
                )
                provider_request_content = None
        if request.attachment_manifest:
            try:
                canonical_manifest = attachment_manifest(request.request_content)
            except MultimodalContractError as exc:
                media_preflight_error = StageInvocationError(
                    str(exc),
                    retryable=False,
                    code=exc.code,
                    human_description=(
                        "The stage received invalid canonical attachment metadata."
                    ),
                    details={"attachment_id": exc.attachment_id or None},
                )
                canonical_manifest = ()
            if (
                media_preflight_error is None
                and tuple(request.attachment_manifest) != canonical_manifest
            ):
                media_preflight_error = StageInvocationError(
                    "stage attachment manifest does not match canonical request content",
                    retryable=False,
                    code=ProviderFailureCode.INVALID_MULTIMODAL_CONTENT,
                    human_description=(
                        "Attachment identity changed before the provider stage."
                    ),
                )
            roots_resolver = getattr(backend, "authorized_media_roots", None)
            if media_preflight_error is None and callable(roots_resolver):
                try:
                    validate_authorized_media_references(
                        request.request_content,
                        authorized_roots=roots_resolver(),
                    )
                except MultimodalContractError as exc:
                    media_preflight_error = StageInvocationError(
                        str(exc),
                        retryable=False,
                        code=exc.code,
                        human_description=(
                            "A required attachment failed integrity or access "
                            "validation before stage routing."
                        ),
                        details={"attachment_id": exc.attachment_id or None},
                    )
            capability = None
            if media_preflight_error is None:
                capability_resolver = getattr(backend, "resolve_input_capability", None)
                capability = (
                    capability_resolver()
                    if callable(capability_resolver)
                    else getattr(backend, "input_capability", None)
                )
            if media_preflight_error is None and capability is None:
                media_preflight_error = StageInvocationError(
                    "stage backend exposes no model-specific media capability",
                    retryable=False,
                    code=ProviderFailureCode.PROVIDER_CONFIGURATION_ERROR,
                    human_description=(
                        "The selected stage backend cannot prove its media input capability."
                    ),
                )
            elif media_preflight_error is None:
                decisions = route_request_content(
                    request.request_content,
                    capability,
                    fallback_modalities=local_fallback_modalities,
                )
                media_routing = routing_decisions_payload(decisions)
                force_local_unavailable = False
                if request.force_local_media_fallback:
                    can_force_local = (
                        request.allow_tools
                        and fallback_registry is not None
                        and bool(local_fallback_modalities)
                        and all(
                            item.modality in local_fallback_modalities
                            for item in decisions
                        )
                    )
                    if can_force_local:
                        media_routing = tuple(
                            {
                                **dict(item.as_dict()),
                                "route": "local_fallback",
                                "reason": "provider_typed_modality_unsupported",
                                "transport": None,
                            }
                            for item in decisions
                        )
                        native_ids: set[str] = set()
                    else:
                        native_ids = set()
                        force_local_unavailable = True
                else:
                    native_ids = {
                        item.attachment_id
                        for item in decisions
                        if item.route == "native"
                    }
                if request.allow_tools and selected_registry is not None and native_ids:
                    native_local_refs = native_attachment_reference_aliases(
                        request.attachment_manifest,
                        native_ids,
                    )
                    selected_registry = _MediaRoutingToolRegistry(
                        selected_registry,
                        native_attachment_ids=native_ids,
                        native_local_refs=native_local_refs,
                        all_media_native=bool(decisions)
                        and all(item.route == "native" for item in decisions),
                    )
                    if controls_tools:
                        backend.tool_registry = selected_registry
                non_native = (
                    list(decisions)
                    if request.force_local_media_fallback
                    and not force_local_unavailable
                    else [item for item in decisions if item.route != "native"]
                )
                if non_native:
                    # Per-part routing is authoritative: an adapter must never
                    # receive media that its exact provider/model capability
                    # does not support.  Tool-enabled stages keep those parts
                    # on the existing local fallback path while sending only
                    # the native subset over the provider wire.
                    provider_request_content = (
                        subset_request_content(request.request_content, native_ids)
                        if native_ids
                        else None
                    )
                unsupported = (
                    []
                    if request.force_local_media_fallback
                    and not force_local_unavailable
                    else [item for item in decisions if item.route == "unsupported"]
                )
                if force_local_unavailable:
                    first = decisions[0]
                    media_preflight_error = StageInvocationError(
                        "A previous typed media fallback cannot be safely resumed for "
                        f"attachment {first.attachment_id}",
                        retryable=False,
                        code=ProviderFailureCode.PROVIDER_MODALITY_UNSUPPORTED,
                        human_description=(
                            "The sole automatic media fallback was already consumed, "
                            "and the selected stage no longer exposes the required "
                            "authorized local media route."
                        ),
                        details={"media_routing": list(media_routing)},
                    )
                elif request.stage is Stage.IMMEDIATE_RESPONSE and non_native:
                    first = non_native[0]
                    media_preflight_error = StageInvocationError(
                        "Immediate Response cannot consume every required attachment: "
                        f"{first.attachment_id}",
                        retryable=False,
                        code=ProviderFailureCode.PROVIDER_MODALITY_UNSUPPORTED,
                        human_description=(
                            "The Immediate Response model cannot consume every required "
                            "attachment; the turn must use the local media work path."
                        ),
                        details={"media_routing": list(media_routing)},
                    )
                elif unsupported:
                    first = unsupported[0]
                    media_preflight_error = StageInvocationError(
                        "Stage cannot consume or locally interpret required attachment: "
                        f"{first.attachment_id}",
                        retryable=False,
                        code=ProviderFailureCode.PROVIDER_MODALITY_UNSUPPORTED,
                        human_description=(
                            "The selected stage model has no verified native input "
                            "route, and this stage has no authorized local media "
                            "fallback for a required attachment."
                        ),
                        details={"media_routing": list(media_routing)},
                    )
        reasoning_chunks: list[str] = []
        provider_tool_activity = False
        provider_replay_activity = False
        provider_request_inflight: tuple[str, str, str, bool] | None = None

        async def _capture(event: StreamEvent) -> None:
            nonlocal provider_replay_activity, provider_tool_activity
            content = str(event.raw_delta or event.summary or "")
            if content or event.tool_name:
                provider_replay_activity = True
            if event.kind in {KIND_TOOL_START, KIND_TOOL_END} or event.tool_name:
                provider_tool_activity = True
            owner = str(event.delivery_class or "") or legacy_delivery_class(event.kind)
            if request.provider_activity_callback is not None:
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
            if event.kind in {KIND_TEXT_DELTA, KIND_PROVIDER_ACTIVITY}:
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
            if (
                event.delivery_class == DELIVERY_USER_COMMENTARY
                and event.kind != "voice_warning"
            ):
                event.delivery_class = DELIVERY_INTERNAL
            event.origin = event.origin or f"her_v2:{profile.engine}"
            event.phase = event.phase or request.stage.value
            await self.on_stream_event(event)

        try:
            if media_preflight_error is not None:
                raise media_preflight_error
            initialized = await backend.initialize()
            if not initialized:
                raise StageInvocationError(
                    f"failed to initialize {profile.engine}/{profile.model}",
                    retryable=False,
                    code=ProviderFailureCode.PROVIDER_CONFIGURATION_ERROR,
                    human_description="The configured provider could not be initialized.",
                )
            self._bind_provider_call_observer(
                backend,
                request_id=request.request_ref or request.turn_id,
                phase=request.stage.value,
                engine=profile.engine,
                model=profile.model,
                invocation_id=request.invocation_id,
                attempt=request.attempt,
                recovery_kind=(
                    "json_repair"
                    if request.stage is Stage.JSON_REPAIR
                    else (
                        "fresh_connection_retry" if request.attempt > 1 else "none"
                    )
                ),
            )
            prompt_request = request
            if request.stage in {Stage.PLANNING, Stage.REPLANNING} and (
                "available_execution_tools" not in request.context
            ):
                prompt_request = replace(
                    request,
                    context={
                        **dict(request.context),
                        "available_execution_tools": list(
                            self.tool_catalogue(
                                allow_side_effects=bool(
                                    request.context.get(
                                        "execution_allow_side_effects", True
                                    )
                                )
                            )
                        ),
                    },
                )
            stage_prompt = render_stage_prompt(prompt_request)
            primary_execution = (
                request.stage is Stage.EXECUTION
                and not request.role.startswith("sub_agent:")
            )
            primary_direct = request.stage is Stage.DIRECT
            primary_review = (
                request.stage is Stage.REVIEW
                and not request.role.startswith("sub_agent:")
            )
            if primary_review:
                raw_execution_record = request.context.get("execution")
                execution_record = (
                    dict(raw_execution_record)
                    if isinstance(raw_execution_record, Mapping)
                    else None
                )
                definitions_getter = getattr(
                    selected_registry, "get_tool_definitions", None
                )
                raw_definitions = (
                    definitions_getter() if callable(definitions_getter) else []
                )
                review_tools = [
                    dict(item) for item in raw_definitions if isinstance(item, Mapping)
                ]
                system_prompt = _review_system_prompt(
                    goal=request.goal,
                    relevant_habits=[
                        str(item)
                        for item in (request.context.get("relevant_habits") or [])
                        if str(item).strip()
                    ],
                    active_plan_id=request.plan_id,
                    active_plan=(
                        request.context.get("active_plan")
                        if isinstance(request.context.get("active_plan"), Mapping)
                        else None
                    ),
                    draft_response=str(
                        request.context.get("draft_response")
                        or (execution_record or {}).get("summary")
                        or ""
                    ),
                    execution_record=execution_record,
                    evidence_refs=[
                        str(item)
                        for item in (request.context.get("evidence_refs") or [])
                        if str(item).strip()
                    ],
                    review_kind=str(
                        request.context.get("review_kind") or "independent"
                    ),
                    findings_to_close=[
                        str(item)
                        for item in (request.context.get("findings_to_close") or [])
                        if str(item).strip()
                    ],
                    available_review_tools=review_tools,
                )
                if not _install_system_prompt(backend, system_prompt):
                    raise StageInvocationError(
                        "review backend cannot isolate the HER v2 system prompt",
                        retryable=False,
                        code=ProviderFailureCode.PROVIDER_CONFIGURATION_ERROR,
                        human_description=(
                            "The configured Review provider cannot isolate the "
                            "required system prompt."
                        ),
                    )
                stage_prompt = request.goal
            elif (
                request.stage
                in {
                    Stage.IMMEDIATE_RESPONSE,
                    Stage.FINALISATION,
                }
                or primary_execution
                or primary_direct
            ):
                backend_config = backend.config
                backend_extra = dict(getattr(backend_config, "extra", None) or {})
                source = her_persona.load_persona_packaging_source(
                    getattr(backend_config, "system_md", None),
                    display_name=(
                        backend_extra.get("display_name")
                        or getattr(backend_config, "name", None)
                    ),
                )
                if request.stage is Stage.IMMEDIATE_RESPONSE:
                    system_prompt = _immediate_response_system_prompt(
                        source, goal=request.goal
                    )
                elif primary_direct or primary_execution:
                    definitions_getter = getattr(
                        selected_registry, "get_tool_definitions", None
                    )
                    raw_definitions = (
                        definitions_getter() if callable(definitions_getter) else []
                    )
                    tool_catalogue = [
                        dict(item)
                        for item in raw_definitions
                        if isinstance(item, Mapping)
                    ]
                    if primary_direct:
                        raw_habits = request.context.get("habit_catalogue")
                        habits = (
                            [str(item) for item in raw_habits if str(item).strip()]
                            if isinstance(raw_habits, (list, tuple))
                            else []
                        )
                        raw_skills = request.context.get("skills_catalogue")
                        skills = (
                            [
                                dict(item)
                                for item in raw_skills
                                if isinstance(item, Mapping)
                            ]
                            if isinstance(raw_skills, (list, tuple))
                            else []
                        )
                        system_prompt = _direct_system_prompt(
                            source,
                            goal=request.goal,
                            habit_catalogue=habits,
                            skills_catalogue=skills,
                            tool_catalogue=tool_catalogue,
                            strategy_playbook=(
                                request.context.get("strategy_playbook")
                                if isinstance(
                                    request.context.get("strategy_playbook"), Mapping
                                )
                                else None
                            ),
                        )
                    else:
                        raw_sub_agent_results = request.context.get(
                            "sub_agent_results", []
                        )
                        delegated_results = (
                            [
                                dict(item)
                                for item in raw_sub_agent_results
                                if isinstance(item, Mapping)
                            ]
                            if isinstance(raw_sub_agent_results, list)
                            else []
                        )
                        system_prompt = _execution_system_prompt(
                            source,
                            goal=request.goal,
                            relevant_habits=[
                                str(item)
                                for item in (
                                    request.context.get("relevant_habits") or []
                                )
                                if str(item).strip()
                            ],
                            active_plan=(
                                request.context.get("active_plan")
                                if isinstance(
                                    request.context.get("active_plan"), Mapping
                                )
                                else None
                            ),
                            delegated_execution=(
                                {
                                    "plan_id": request.plan_id,
                                    "results": delegated_results,
                                }
                                if delegated_results
                                else None
                            ),
                            strategy_handoff=(
                                request.context.get("strategy_handoff")
                                if isinstance(
                                    request.context.get("strategy_handoff"), Mapping
                                )
                                else None
                            ),
                            tool_catalogue=tool_catalogue,
                        )
                else:
                    system_prompt = _finalisation_system_prompt(
                        source,
                        goal=request.goal,
                        relevant_habits=[
                            str(item)
                            for item in (request.context.get("relevant_habits") or [])
                            if str(item).strip()
                        ],
                        draft_response=str(request.context.get("draft_response") or ""),
                        reviewer_findings=(
                            request.context.get("reviewer_findings")
                            if isinstance(
                                request.context.get("reviewer_findings"), Mapping
                            )
                            else None
                        ),
                        completion_evidence=(
                            request.context.get("completion_evidence")
                            if isinstance(
                                request.context.get("completion_evidence"), Mapping
                            )
                            else {}
                        ),
                    )
                if not _install_system_prompt(backend, system_prompt):
                    if (
                        request.stage is Stage.FINALISATION
                        or primary_execution
                        or primary_direct
                    ):
                        raise StageInvocationError(
                            f"{request.stage.value} backend cannot isolate the HER v2 system prompt",
                            retryable=False,
                            code=ProviderFailureCode.PROVIDER_CONFIGURATION_ERROR,
                            human_description=(
                                f"The configured {request.stage.value} provider cannot "
                                "isolate the required system prompt."
                            ),
                        )
                    stage_prompt = f"{system_prompt}\n\n{stage_prompt}"
                elif request.stage is Stage.FINALISATION:
                    stage_prompt = request.goal
            else:
                internal_prompt = _internal_stage_system_prompt(prompt_request)
                if internal_prompt is not None:
                    installed = _install_system_prompt(backend, internal_prompt)
                    if not installed and (
                        request.stage is Stage.EXECUTION
                        or request.stage is Stage.JSON_REPAIR
                        or uses_complete_system_prompt(request.stage)
                    ):
                        raise StageInvocationError(
                            f"{request.stage.value} backend cannot isolate the HER v2 system prompt",
                            retryable=False,
                            code=ProviderFailureCode.PROVIDER_CONFIGURATION_ERROR,
                            human_description=(
                                f"The configured {request.stage.value} provider cannot isolate "
                                "the required system prompt."
                            ),
                        )
                    if installed and (
                        uses_complete_system_prompt(request.stage)
                        or request.role.startswith("sub_agent:")
                    ):
                        # The complete, dynamically rendered stage contract is now
                        # isolated from the configured Agent Persona.  The raw goal
                        # provides the non-empty user turn required by every backend;
                        # all instructions, evidence, and output schema remain owned
                        # by the single external system prompt asset.
                        stage_prompt = request.goal
                    elif not installed:
                        stage_prompt = f"{internal_prompt}\n\n{stage_prompt}"
            if cognitive_registry is not None:
                contract = cognitive_system_contract()
                current_system = str(getattr(backend, "sys_prompt", "") or "").strip()
                if current_system:
                    _install_system_prompt(
                        backend,
                        f"{current_system}\n\n{contract}",
                    )
                else:
                    stage_prompt = f"{contract}\n\n{stage_prompt}"

            generation_kwargs: dict[str, Any] = {
                "is_retry": request.attempt > 1,
                "silent": self.silent,
                "on_stream_event": _capture,
            }
            if provider_request_content is not None:
                parameters = inspect.signature(backend.generate_response).parameters
                if "request_content" not in parameters and not any(
                    parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in parameters.values()
                ):
                    raise StageInvocationError(
                        f"{profile.engine}/{profile.model} cannot accept structured request content",
                        retryable=False,
                        code=ProviderFailureCode.PROVIDER_MODALITY_UNSUPPORTED,
                        human_description=(
                            "The selected stage adapter cannot serialize its declared media capability."
                        ),
                        details={"media_routing": list(media_routing)},
                    )
                generation_kwargs["request_content"] = provider_request_content
            provider_request_inflight = (
                profile.engine,
                profile.model,
                "stage",
                callable(getattr(backend, "set_provider_call_observer", None)),
            )
            response = await backend.generate_response(
                stage_prompt,
                f"{request.turn_id}:{request.stage.value}:{request.attempt}",
                **generation_kwargs,
            )
            provider_request_inflight = None
            response_metadata = (
                dict(response.stream_metadata)
                if isinstance(response.stream_metadata, Mapping)
                else {}
            )
            self._record_usage_line_item(
                request_id=request.request_ref or request.turn_id,
                phase=request.stage.value,
                engine=profile.engine,
                model=profile.model,
                response=response,
                invocation_id=request.invocation_id,
                attempt=request.attempt,
                recovery_kind=(
                    "json_repair"
                    if request.stage is Stage.JSON_REPAIR
                    else ("fresh_connection_retry" if request.attempt > 1 else "none")
                ),
            )
            native_fallback_attempted = False
            if (
                native_audio_stage
                and not response.is_success
                and bool(profile.options.get("_voice_fallback_enabled", True))
                and not response.side_effects_possible
                and not response.tool_call_count
                and not provider_tool_activity
                and not provider_replay_activity
                and not str(response.text or "").strip()
                and not response.structured_data
            ):
                transcript, transcript_state = await self._released_voice_transcript(
                    request
                )
                if transcript and transcript_state == "released":
                    native_fallback_attempted = True
                    if self.on_stream_event is not None:
                        fallback_base = (
                            f"{request.turn_id}:{request.stage.value}:native-fallback"
                        )
                        await self.on_stream_event(
                            StreamEvent(
                                kind="voice_fallback_started",
                                summary="",
                                event_id=f"{fallback_base}:started",
                                delivery_class=DELIVERY_INTERNAL,
                                origin="her_v2:runtime",
                                phase=request.stage.value,
                                provenance="runtime_control",
                            )
                        )
                        await self.on_stream_event(
                            StreamEvent(
                                kind="voice_warning",
                                summary=(
                                    "Native voice reply was unavailable; HASHI is "
                                    "using local speech recognition, the text model, "
                                    "and text-to-speech fallback."
                                ),
                                event_id=f"{fallback_base}:warning",
                                delivery_class=DELIVERY_USER_COMMENTARY,
                                origin="her_v2:runtime",
                                phase=request.stage.value,
                                provenance="runtime_control",
                            )
                        )
                    fallback_provider = str(
                        profile.options.get("_voice_fallback_provider")
                        or profile.engine
                    ).strip()
                    fallback_model = str(
                        profile.options.get("_voice_fallback_model")
                        or profile.model
                    ).strip()
                    try:
                        fallback_backend = (
                            self.backend_manager.create_ephemeral_backend(
                                fallback_provider,
                                target_model=fallback_model,
                            )
                        )
                    except Exception as exc:
                        raise StageInvocationError(
                            "cannot create configured native voice fallback "
                            f"{fallback_provider}/{fallback_model}: {exc}",
                            retryable=False,
                            code=ProviderFailureCode.PROVIDER_CONFIGURATION_ERROR,
                            human_description=(
                                "The configured native voice fallback model could "
                                "not be created."
                            ),
                        ) from exc
                    try:
                        fallback_backend.privacy_level = (
                            self.backend_manager.privacy_level
                        )
                        if hasattr(fallback_backend, "tool_registry"):
                            fallback_backend.tool_registry = None
                        if profile.reasoning is not None and hasattr(
                            fallback_backend, "set_reasoning_enabled"
                        ):
                            normalized_reasoning = str(
                                profile.reasoning or ""
                            ).strip().casefold()
                            fallback_backend.set_reasoning_enabled(
                                normalized_reasoning
                                not in {
                                    "",
                                    "none",
                                    "off",
                                    "false",
                                    "0",
                                    "disabled",
                                }
                            )
                        if not _install_system_prompt(
                            fallback_backend, system_prompt
                        ):
                            raise StageInvocationError(
                                "native voice fallback backend cannot isolate the "
                                "HER v2 system prompt",
                                retryable=False,
                                code=(
                                    ProviderFailureCode.PROVIDER_CONFIGURATION_ERROR
                                ),
                            )
                        if not await fallback_backend.initialize():
                            raise StageInvocationError(
                                "native voice fallback backend failed to initialize",
                                retryable=False,
                                code=(
                                    ProviderFailureCode.PROVIDER_CONFIGURATION_ERROR
                                ),
                            )
                        self._bind_provider_call_observer(
                            fallback_backend,
                            request_id=request.request_ref or request.turn_id,
                            phase=request.stage.value,
                            engine=fallback_provider,
                            model=fallback_model,
                            invocation_id=(
                                f"{request.invocation_id}:native-audio-fallback"
                            ),
                            attempt=request.attempt,
                            recovery_kind="native_audio_fallback",
                        )
                        provider_request_inflight = (
                            fallback_provider,
                            fallback_model,
                            "native_audio_fallback",
                            callable(
                                getattr(
                                    fallback_backend,
                                    "set_provider_call_observer",
                                    None,
                                )
                            ),
                        )
                        response = await fallback_backend.generate_response(
                            f"[Local voice transcription]\n{transcript}",
                            (
                                f"{request.turn_id}:{request.stage.value}:"
                                f"{request.attempt}:native-audio-fallback"
                            ),
                            is_retry=True,
                            silent=self.silent,
                            on_stream_event=_capture,
                        )
                        provider_request_inflight = None
                        self._record_usage_line_item(
                            request_id=request.request_ref or request.turn_id,
                            phase=request.stage.value,
                            engine=fallback_provider,
                            model=fallback_model,
                            response=response,
                            invocation_id=f"{request.invocation_id}:native-audio-fallback",
                            attempt=request.attempt,
                            recovery_kind="native_audio_fallback",
                        )
                    finally:
                        await fallback_backend.shutdown()
                    response_metadata = (
                        dict(response.stream_metadata)
                        if isinstance(response.stream_metadata, Mapping)
                        else {}
                    )
                    response_metadata.update(
                        {
                            "native_audio_fallback": True,
                            "native_audio_fallback_provider": fallback_provider,
                            "native_audio_fallback_model": fallback_model,
                        }
                    )
                    response.stream_metadata = response_metadata
            response_media_routing = response_metadata.get("multimodal_routing")
            if isinstance(response_media_routing, (list, tuple)):
                normalized_response_routing = tuple(
                    dict(item)
                    for item in response_media_routing
                    if isinstance(item, Mapping)
                )
                expected_ids = tuple(
                    str(item.get("attachment_id") or "") for item in media_routing
                )
                response_ids = tuple(
                    str(item.get("attachment_id") or "")
                    for item in normalized_response_routing
                )
                if response_ids == expected_ids:
                    media_routing = normalized_response_routing
                elif (
                    response_ids
                    and len(response_ids) == len(set(response_ids))
                    and all(
                        attachment_id in expected_ids for attachment_id in response_ids
                    )
                    and response_ids
                    == tuple(
                        attachment_id
                        for attachment_id in expected_ids
                        if attachment_id in set(response_ids)
                    )
                ):
                    # Mixed-modality preflight sends only the native subset to
                    # the adapter.  Merge that subset's runtime result (notably
                    # a typed modality fallback) without allowing it to rewrite
                    # the identity or ordering of locally routed attachments.
                    response_by_id = {
                        str(item.get("attachment_id") or ""): item
                        for item in normalized_response_routing
                    }
                    merged_routing: list[dict[str, Any]] = []
                    merge_valid = True
                    for expected in media_routing:
                        expected_item = dict(expected)
                        attachment_id = str(expected_item.get("attachment_id") or "")
                        replacement = response_by_id.get(attachment_id)
                        if replacement is None:
                            merged_routing.append(expected_item)
                            continue
                        if (
                            str(replacement.get("modality") or "")
                            != str(expected_item.get("modality") or "")
                            or replacement.get("item_index")
                            != expected_item.get("item_index")
                            or str(replacement.get("route") or "")
                            not in {"native", "local_fallback", "unsupported"}
                        ):
                            merge_valid = False
                            break
                        merged_routing.append(
                            {
                                **expected_item,
                                "route": replacement.get("route"),
                                "reason": replacement.get("reason"),
                                "transport": replacement.get("transport"),
                            }
                        )
                    if merge_valid:
                        media_routing = tuple(merged_routing)
            adapter_media_fallback_attempted = bool(
                response_metadata.get("multimodal_fallback_attempted")
            ) or any(
                str(item.get("reason") or "") == "provider_typed_modality_unsupported"
                for item in media_routing
            )
            her_media_fallback_attempted = False
            if (
                not response.is_success
                and response.error_code
                == ProviderFailureCode.PROVIDER_MODALITY_UNSUPPORTED.value
                and request.stage is not Stage.IMMEDIATE_RESPONSE
                and not response.side_effects_possible
                and not response.tool_call_count
                and not provider_tool_activity
                and not provider_replay_activity
                and not response_metadata.get("provider_activity_observed")
                and not str(response.text or "").strip()
                and not response.structured_data
                and provider_request_content is not None
                and not adapter_media_fallback_attempted
                and request.allow_tools
                and fallback_registry is not None
                and bool(local_fallback_modalities)
                and all(
                    str(item.get("modality") or "") in local_fallback_modalities
                    for item in media_routing
                )
            ):
                # Capability drift is the sole automatic media fallback.  The
                # first request was rejected before tools/side effects, and this
                # one text-only replay is the complete fallback allowance.
                media_routing = tuple(
                    {
                        **dict(item),
                        "route": "local_fallback",
                        "reason": "provider_typed_modality_unsupported",
                        "transport": None,
                    }
                    for item in media_routing
                )
                her_media_fallback_attempted = True
                if controls_tools:
                    backend.tool_registry = fallback_registry
                self._bind_provider_call_observer(
                    backend,
                    request_id=request.request_ref or request.turn_id,
                    phase=request.stage.value,
                    engine=profile.engine,
                    model=profile.model,
                    invocation_id=f"{request.invocation_id}:media-fallback",
                    attempt=request.attempt,
                    recovery_kind="media_fallback",
                )
                provider_request_inflight = (
                    profile.engine,
                    profile.model,
                    "media_fallback",
                    callable(getattr(backend, "set_provider_call_observer", None)),
                )
                response = await backend.generate_response(
                    stage_prompt,
                    f"{request.turn_id}:{request.stage.value}:{request.attempt}:media-fallback",
                    is_retry=True,
                    silent=self.silent,
                    on_stream_event=_capture,
                )
                provider_request_inflight = None
                self._record_usage_line_item(
                    request_id=request.request_ref or request.turn_id,
                    phase=request.stage.value,
                    engine=profile.engine,
                    model=profile.model,
                    response=response,
                    invocation_id=f"{request.invocation_id}:media-fallback",
                    attempt=request.attempt,
                    recovery_kind="media_fallback",
                )
            if her_media_fallback_attempted:
                fallback_metadata = dict(response.stream_metadata or {})
                fallback_metadata["multimodal_routing"] = list(media_routing)
                fallback_metadata["multimodal_fallback_attempted"] = True
                response.stream_metadata = fallback_metadata
            elif native_fallback_attempted:
                fallback_metadata = dict(response.stream_metadata or {})
                fallback_metadata["native_audio_fallback"] = True
                response.stream_metadata = fallback_metadata
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
            if cognitive_registry is not None:
                cognitive_registry.note_provider_completion()
            if response.usage:
                self.usage.input_tokens += int(response.usage.input_tokens or 0)
                self.usage.output_tokens += int(response.usage.output_tokens or 0)
                self.usage.thinking_tokens += int(response.usage.thinking_tokens or 0)
            self.cost_usd += float(response.cost_usd or 0.0)
            self.tool_call_count += int(response.tool_call_count or 0)
            self.tool_loop_count += int(response.tool_loop_count or 0)
            tool_receipts = (
                evidence_registry.receipts if evidence_registry is not None else ()
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
                evidence_refs=tuple(item.evidence_ref for item in tool_receipts),
                provider_attempt=request.attempt,
                tool_receipts=tool_receipts,
                media_routing=media_routing,
                content=tuple(
                    dict(part)
                    for part in getattr(response, "content", ())
                    if isinstance(part, Mapping)
                ),
                cognitive_control=(
                    cognitive_registry.controller.snapshot()
                    if cognitive_registry is not None
                    else {}
                ),
            )
        except ProviderCallObserverError as exc:
            raise self._accounting_observer_failure(exc) from exc
        except StageInvocationError as exc:
            if provider_request_inflight is not None:
                engine, model, label, physically_observed = provider_request_inflight
                if (
                    exc.code != ProviderFailureCode.AUDIT_PERSISTENCE_FAILURE
                    and not physically_observed
                ):
                    self._record_unreceipted_provider_attempt(
                        request,
                        engine=engine,
                        model=model,
                        label=label,
                        status="failed_without_receipt",
                    )
            raise
        except asyncio.CancelledError:
            if provider_request_inflight is not None:
                engine, model, label, physically_observed = provider_request_inflight
                if not physically_observed:
                    self._record_unreceipted_provider_attempt(
                        request,
                        engine=engine,
                        model=model,
                        label=label,
                        status="cancelled",
                    )
            raise
        except Exception as exc:
            if provider_request_inflight is not None:
                engine, model, label, physically_observed = provider_request_inflight
                if not physically_observed:
                    self._record_unreceipted_provider_attempt(
                        request,
                        engine=engine,
                        model=model,
                        label=label,
                        status="failed_without_receipt",
                    )
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
            self._bind_provider_call_observer(
                backend,
                request_id=request_id,
                phase="persona",
                engine=profile.engine,
                model=profile.model,
                invocation_id=request_id,
                attempt=attempt,
                recovery_kind=(
                    "fresh_connection_retry" if attempt > 1 else "none"
                ),
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
        except ProviderCallObserverError as exc:
            raise self._accounting_observer_failure(exc) from exc
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
        event_digest = hashlib.sha256(commentary.event_id.encode("utf-8")).hexdigest()[
            :16
        ]
        package_request_id = (
            f"{self.request_id}:persona-package:{event_digest}:{self.package_index}"
        )
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
            declared_failure, failure_reason = _persona_commentary_agent_failure(text)
            if declared_failure:
                self.logger.warning(
                    "HER v2 Persona commentary agent declared failure; "
                    "using display-name fallback: reason=%s",
                    failure_reason,
                )
                return self._fallback(
                    commentary,
                    _PERSONA_COMMENTARY_AGENT_FAILED_FIELD,
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
            text=f"{self.source.display_name}: {commentary.text}",
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
            text = await self.provider.package_persona_commentary(
                self.profile,
                persona_block=self.source.guidance,
                neutral_commentary=message.text,
                request_id=package_request_id,
            )
            declared_failure, failure_reason = _persona_commentary_agent_failure(text)
            if declared_failure:
                self.logger.warning(
                    "HER v2 Persona commentary agent declared clarification failure; "
                    "using display-name fallback: reason=%s",
                    failure_reason,
                )
                return self._required_fallback(
                    message,
                    _PERSONA_COMMENTARY_AGENT_FAILED_FIELD,
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
        prefix = f"{self.source.display_name}: "
        return RenderedRequiredMessage(
            source_event_id=message.event_id,
            kind=message.kind,
            text=f"{prefix}{message.text}",
            provenance="minimal_persona_fallback",
            fallback=True,
            error_type=reason,
        )
