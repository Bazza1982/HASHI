"""PAO-owned capacity retry and cross-Engine fallback execution.

The semantic policy lives in :mod:`orchestrator.model_fallback_policy`.  This
module consumes that policy for an admitted Run.  It retries only the typed
``PROVIDER_CAPACITY_UNAVAILABLE`` failure, keeps the original Engine selected,
and invokes only exact user-allowed fallback routes.  A failed turn with
possible effects is never replayed: the default is to stop, while the explicit
high-risk option emits a fixed continuation into the same resumable Session.
"""

from __future__ import annotations

import asyncio
import inspect
import math
from copy import deepcopy
from dataclasses import replace
from typing import Any, Awaitable, Callable, Mapping

from adapters.stream_events import (
    DELIVERY_TECHNICAL,
    KIND_PROGRESS,
    StreamEvent,
)
from orchestrator import model_fallback_policy as policy_owner
from orchestrator import ui_language
from orchestrator.flexible_backend_registry import canonical_backend_engine

Sleep = Callable[[float], Awaitable[Any] | Any]

_BACKGROUND_SOURCES = frozenset(
    {"scheduler", "background", "job", "jobs", "cron", "heartbeat", "nudge", "system"}
)


def _source_for_engine(engine: str) -> str | None:
    canonical = canonical_backend_engine(engine)
    if canonical == "codex-cli":
        return policy_owner.SOURCE_CODEX_CLI
    # HERV3 consumes the same policy inside its own durable Turn loop.  Replaying
    # its outer fixed envelope here would violate PAO/HER idempotency.
    return None


def _actor(item: Any) -> Any:
    return (
        getattr(item, "owner_id", None)
        or getattr(item, "chat_id", None)
        or "default"
    )


def _side_effects_possible(response: Any) -> bool:
    return bool(
        getattr(response, "side_effects_possible", False)
        or int(getattr(response, "tool_call_count", 0) or 0) > 0
    )


def _resumable_session(runtime: Any) -> bool:
    backend = getattr(getattr(runtime, "backend_manager", None), "current_backend", None)
    capabilities = getattr(backend, "capabilities", None)
    return bool(
        getattr(capabilities, "supports_sessions", False)
        and str(getattr(backend, "_session_id", None) or "").strip()
    )


def _continuation_prompt(request_id: str) -> str:
    return (
        "Continue the unfinished work from the immediately preceding failed turn "
        "in this same Engine Session. Do not replay the original request and do "
        "not repeat completed actions. Inspect prior tool results and observable "
        "effects first, then perform only work that is still unfinished. If the "
        "completion state is uncertain, stop and report that uncertainty. "
        f"HASHI recovery request: {request_id}."
    )


def _annotate(
    response: Any,
    *,
    mode: str,
    retry_attempts: int,
    delays_s: list[float],
    blocking_reason: str | None = None,
    fallback_attempts: list[Mapping[str, Any]] | None = None,
) -> Any:
    notice_key = {
        policy_owner.REASON_EFFECT_PAUSED: "fallback.runtime.effect_paused",
        policy_owner.REASON_UNSAFE_CHECKPOINT: "fallback.runtime.effect_paused",
        policy_owner.REASON_RESUME_UNAVAILABLE: "fallback.runtime.resume_unavailable",
        policy_owner.REASON_RETRY_EXHAUSTED: "fallback.runtime.retry_exhausted",
        policy_owner.REASON_CONFIRMATION_REQUIRED: (
            "fallback.runtime.confirmation_unavailable"
        ),
        policy_owner.REASON_NO_ALLOWED_TARGET: (
            "fallback.runtime.fallback_unavailable"
        ),
        policy_owner.REASON_MISSING_KEY_OR_RUNTIME: (
            "fallback.runtime.fallback_unavailable"
        ),
        policy_owner.REASON_INCOMPATIBLE_CAPABILITY: (
            "fallback.runtime.fallback_unavailable"
        ),
        policy_owner.REASON_BUDGET_EXHAUSTED: (
            "fallback.runtime.fallback_unavailable"
        ),
    }.get(blocking_reason)
    provider_error = str(getattr(response, "error", "") or "")
    metadata = dict(getattr(response, "stream_metadata", None) or {})
    metadata["capacity_recovery"] = {
        "version": 1,
        "mode": str(mode or policy_owner.MODE_OFF),
        "retry_attempts": int(retry_attempts),
        "retry_delays_s": [float(value) for value in delays_s],
        "blocking_reason": blocking_reason,
        "fallback_attempts": [dict(item) for item in (fallback_attempts or [])],
        "user_notice_code": blocking_reason if notice_key else None,
        "provider_error": provider_error or None,
    }
    response.stream_metadata = metadata
    if notice_key:
        response.error = ui_language.tr(notice_key)
    return response


async def _emit(on_stream_event: Any, key: str, **values: Any) -> None:
    if not callable(on_stream_event):
        return
    result = on_stream_event(
        StreamEvent(
            kind=KIND_PROGRESS,
            summary=ui_language.tr(key, **values),
            delivery_class=DELIVERY_TECHNICAL,
            origin="hashi_capacity_recovery",
            phase="recovery",
            required=True,
            provenance="hashi_policy",
        )
    )
    if inspect.isawaitable(result):
        await result


async def _wait(sleep: Sleep, delay_s: float) -> None:
    result = sleep(delay_s)
    if inspect.isawaitable(result):
        await result


def _known_cost(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def _route_facts(runtime: Any, policy: policy_owner.FallbackPolicy) -> dict[str, Any]:
    from orchestrator.runtime_effort_options import get_available_models

    manager = runtime.backend_manager
    try:
        options = list(manager.get_her_v2_provider_options())
    except Exception:
        options = []
    by_engine = {
        canonical_backend_engine(str(item.get("engine") or "")): item
        for item in options
    }
    allowed_backends = list(getattr(getattr(manager, "config", None), "allowed_backends", ()) or ())
    codex_rows = [
        row
        for row in allowed_backends
        if canonical_backend_engine(str(row.get("engine") or "")) == "codex-cli"
    ]
    try:
        codex_models = set(
            get_available_models("codex-cli", allowed_backends=codex_rows)
        )
    except (TypeError, ValueError):
        codex_models = set()
    facts: dict[str, Any] = {}
    for route in policy.routes:
        if route.target_kind == policy_owner.TARGET_KIND_HERV3_MODEL:
            option = by_engine.get(canonical_backend_engine(route.provider))
            available = bool(option is not None and option.get("available") is not False)
            models = [str(value) for value in (option or {}).get("models") or []]
            model_ready = available and route.model in models
            facts[route.key()] = policy_owner.FallbackRouteFacts(
                credential_ready=available,
                capability_ok=model_ready,
                capability_reason=(
                    policy_owner.REASON_MISSING_KEY_OR_RUNTIME
                    if not available
                    else policy_owner.REASON_INCOMPATIBLE_CAPABILITY
                ),
                known_extra_cost_usd=_known_cost(
                    (option or {}).get("estimated_fallback_call_cost_usd")
                ),
            )
            continue
        model_ready = bool(not route.model or route.model in codex_models)
        facts[route.key()] = policy_owner.FallbackRouteFacts(
            credential_ready=bool(codex_rows),
            capability_ok=bool(codex_rows and model_ready),
            capability_reason=(
                policy_owner.REASON_MISSING_KEY_OR_RUNTIME
                if not codex_rows
                else policy_owner.REASON_INCOMPATIBLE_CAPABILITY
            ),
        )
    return facts


def _source_model(runtime: Any) -> str:
    backend = getattr(runtime.backend_manager, "current_backend", None)
    return str(
        getattr(backend, "model", None)
        or getattr(getattr(backend, "config", None), "model", None)
        or ""
    )


def _interactive(item: Any) -> bool:
    source = str(getattr(item, "source", "") or "").strip().casefold()
    if source in _BACKGROUND_SOURCES:
        return False
    return bool(getattr(item, "deliver_to_telegram", False))


def _modality(item: Any) -> str:
    try:
        from orchestrator.multimodal_contract import request_content_is_voice_origin

        voice_origin = bool(
            request_content_is_voice_origin(getattr(item, "request_content", None))
        )
    except Exception:
        voice_origin = False
    return policy_owner.request_modality(voice_origin=voice_origin)


def _target_label(route: policy_owner.FallbackRoute) -> str:
    if route.target_kind == policy_owner.TARGET_KIND_CODEX_CLI:
        return "codex-cli" + (f"/{route.model}" if route.model else "")
    return f"{route.provider}/{route.model}"


async def _attempt_fallback(
    runtime: Any,
    item: Any,
    response: Any,
    *,
    policy: policy_owner.FallbackPolicy,
    source: str,
    original_prompt: str,
    generation_kwargs: Mapping[str, Any],
    on_stream_event: Any,
    retry_attempts: int,
    delays_s: list[float],
    side_effects_seen: bool,
    partial_answer_seen: bool,
) -> Any:
    budget_used = 0
    source_model = _source_model(runtime)
    excluded: set[str] = {
        route.key()
        for route in policy.routes
        if source == policy_owner.SOURCE_CODEX_CLI
        and route.target_kind == policy_owner.TARGET_KIND_CODEX_CLI
        and (not route.model or route.model == source_model)
    }
    attempts: list[dict[str, Any]] = []
    current = response
    while True:
        facts = policy_owner.FallbackRequestFacts(
            source=source,
            source_provider=canonical_backend_engine(runtime.config.active_backend),
            source_model=source_model,
            modality=_modality(item),
            privacy_level=int(getattr(runtime.backend_manager, "privacy_level", 1)),
            tool_activity=bool(side_effects_seen or _side_effects_possible(current)),
            visible_partial_answer=bool(
                partial_answer_seen
                or str(getattr(current, "text", "") or "").strip()
            ),
            checkpoint_safe=False,
            budget_used=budget_used,
            known_extra_cost_usd=None,
            interactive=_interactive(item),
            request_id=str(item.request_id),
            route_facts=_route_facts(runtime, policy),
            excluded_route_keys=frozenset(excluded),
        )
        decision = policy_owner.resolve_fallback_decision(policy, facts)
        if (
            decision.disposition == policy_owner.DISPOSITION_ASK_REQUIRED
            and decision.confirmation is not None
        ):
            from orchestrator.runtime_her_fallback import request_fallback_confirmation

            confirmation = await request_fallback_confirmation(
                runtime,
                decision.confirmation,
                target_label=_target_label(decision.route),
                request_id=facts.request_id,
            )
            if confirmation is not None:
                facts = replace(facts, confirmation=confirmation)
                decision = policy_owner.resolve_fallback_decision(policy, facts)
        if not decision.allowed or decision.route is None:
            return _annotate(
                current,
                mode=policy.mode,
                retry_attempts=retry_attempts,
                delays_s=delays_s,
                blocking_reason=decision.blocking_reason,
                fallback_attempts=attempts,
            )

        route = decision.route
        await _emit(
            on_stream_event,
            "fallback.runtime.switch",
            target=_target_label(route),
        )
        runtime._log_maintenance(
            item,
            "capacity_fallback_selected",
            target_kind=route.target_kind,
            provider=route.provider,
            model=route.model,
            route_key=route.key(),
        )
        fallback_kwargs = dict(generation_kwargs)
        fallback_kwargs["is_retry"] = False
        try:
            current = await runtime.backend_manager.generate_fallback_response(
                target_kind=route.target_kind,
                provider=route.provider,
                model=route.model,
                prompt=original_prompt,
                request_id=item.request_id,
                **fallback_kwargs,
            )
        except Exception as exc:
            from adapters.base import BackendResponse

            current = BackendResponse(
                text="",
                duration_ms=0,
                error=f"Fallback target failed: {type(exc).__name__}",
                is_success=False,
                error_code="FALLBACK_TARGET_FAILED",
                error_retryable=False,
            )
        budget_used += 1
        excluded.add(route.key())
        attempts.append(
            {
                "route_key": route.key(),
                "target": _target_label(route),
                "success": bool(getattr(current, "is_success", False)),
                "error_code": getattr(current, "error_code", None),
            }
        )
        if getattr(current, "is_success", False):
            return _annotate(
                current,
                mode=policy.mode,
                retry_attempts=retry_attempts,
                delays_s=delays_s,
                fallback_attempts=attempts,
            )


async def generate_with_capacity_recovery(
    runtime: Any,
    item: Any,
    prompt: str,
    generation_kwargs: Mapping[str, Any],
    *,
    sleep: Sleep = asyncio.sleep,
) -> Any:
    """Generate one response and apply the user's bounded capacity policy."""

    kwargs = deepcopy(dict(generation_kwargs))
    response = await runtime.backend_manager.generate_response(
        prompt,
        item.request_id,
        **kwargs,
    )
    if getattr(response, "is_success", False):
        return response
    source = _source_for_engine(str(runtime.config.active_backend or ""))
    if source is None:
        return response
    policy, _document = policy_owner.read_policy(runtime, _actor(item))
    retry_attempts = 0
    delays_s: list[float] = []
    side_effects_seen = _side_effects_possible(response)
    partial_answer_seen = bool(str(getattr(response, "text", "") or "").strip())
    on_stream_event = kwargs.get("on_stream_event")
    while True:
        decision = policy_owner.resolve_capacity_retry(
            policy,
            policy_owner.CapacityRetryFacts(
                source=source,
                failure_code=str(getattr(response, "error_code", "") or ""),
                retryable=getattr(response, "error_retryable", None) is True,
                retry_index=retry_attempts,
                retry_after_s=getattr(response, "retry_after_s", None),
                side_effects_possible=_side_effects_possible(response),
                resumable_session=_resumable_session(runtime),
            ),
        )
        if not decision.allowed:
            break
        delay_s = float(decision.delay_s or 0.0)
        delays_s.append(delay_s)
        await _emit(
            on_stream_event,
            "fallback.retry.wait",
            attempt=decision.retry_number,
            count=len(policy_owner.CAPACITY_RETRY_DELAYS_S),
            seconds=math.ceil(delay_s),
        )
        runtime._log_maintenance(
            item,
            "capacity_retry_scheduled",
            attempt=decision.retry_number,
            delay_s=delay_s,
            same_session=decision.continue_session,
        )
        await _wait(sleep, delay_s)
        retry_attempts += 1
        retry_kwargs = deepcopy(kwargs)
        retry_kwargs["is_retry"] = True
        retry_prompt = prompt
        if decision.continue_session:
            retry_prompt = _continuation_prompt(str(item.request_id))
            retry_kwargs.pop("request_content", None)
        response = await runtime.backend_manager.generate_response(
            retry_prompt,
            item.request_id,
            **retry_kwargs,
        )
        side_effects_seen = bool(side_effects_seen or _side_effects_possible(response))
        partial_answer_seen = bool(
            partial_answer_seen
            or str(getattr(response, "text", "") or "").strip()
        )
        if getattr(response, "is_success", False):
            return _annotate(
                response,
                mode=policy.mode,
                retry_attempts=retry_attempts,
                delays_s=delays_s,
            )

    if (
        str(getattr(response, "error_code", "") or "").upper()
        == policy_owner.CAPACITY_FAILURE_CODE
        and retry_attempts >= len(policy_owner.CAPACITY_RETRY_DELAYS_S)
        and policy.mode in {policy_owner.MODE_ASK, policy_owner.MODE_AUTO}
    ):
        return await _attempt_fallback(
            runtime,
            item,
            response,
            policy=policy,
            source=source,
            original_prompt=prompt,
            generation_kwargs=kwargs,
            on_stream_event=on_stream_event,
            retry_attempts=retry_attempts,
            delays_s=delays_s,
            side_effects_seen=side_effects_seen,
            partial_answer_seen=partial_answer_seen,
        )
    return _annotate(
        response,
        mode=policy.mode,
        retry_attempts=retry_attempts,
        delays_s=delays_s,
        blocking_reason=decision.blocking_reason,
    )


__all__ = ["generate_with_capacity_recovery"]
