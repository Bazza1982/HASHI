"""Provider invocation and structured recovery for HER v2."""

from __future__ import annotations

import asyncio
import copy
import hashlib
from typing import TYPE_CHECKING, Any, Callable, Mapping

from .audit import AuditPersistenceError
from .commentary import (
    CommentaryValidationError,
    commentary_from_stage_response,
)
from .config import ProviderProfile
from .interfaces import (
    ProviderFailureCode,
    StageInvocationError,
    StructuredOutputError,
    TurnStopped,
)
from .models import Stage, StageRequest, StageResponse, ToolEvidenceReceipt
from .progress import ProviderActivityTracker
from .runtime_support import _payload_hash
from .structured import resolve_stage_response
from orchestrator.multimodal_contract import (
    attachment_manifest,
    canonical_request_content,
    subset_request_content,
)

if TYPE_CHECKING:
    from .runtime import _TurnState


Validator = Callable[[StageResponse], Any]


def _tool_receipt_payload(receipt: ToolEvidenceReceipt) -> dict[str, Any]:
    return {
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


def _used_typed_media_fallback(value: Any) -> bool:
    if not isinstance(value, (list, tuple)):
        return False
    return any(
        isinstance(item, Mapping)
        and str(item.get("reason") or "")
        == "provider_typed_modality_unsupported"
        for item in value
    )


class RuntimeInvocationMixin:
    async def _invoke_stage(
        self,
        state: _TurnState,
        stage: Stage,
        validator: Validator,
        *,
        profile: ProviderProfile | None = None,
        allow_tools: bool,
        allow_side_effects: bool = False,
        context: Mapping[str, Any] | None = None,
        role_override: str | None = None,
        publish_commentary: bool = True,
        defer_structured_error: bool = False,
        retry_on_failure: bool = True,
    ) -> tuple[StageResponse, Any]:
        selected = profile or self.config.profile_for(stage)
        role = role_override or (
            selected.name
            if profile is not None
            else self.config.stage_roles.get(stage, selected.name)
        )
        base_context = copy.deepcopy(dict(context or {}))
        stage_request_content = copy.deepcopy(state.request_content)
        authorised_attachment_ids = base_context.get("authorized_attachment_ids")
        if isinstance(authorised_attachment_ids, list):
            stage_request_content = subset_request_content(
                stage_request_content,
                authorised_attachment_ids,
            )
        elif role.startswith("sub_agent:"):
            # Missing or malformed assignment scope must fail closed instead
            # of inheriting every attachment from the parent turn.
            stage_request_content = subset_request_content(
                stage_request_content,
                (),
            )
        stage_attachment_manifest = attachment_manifest(stage_request_content)
        stage_goal = state.goal
        if role.startswith("sub_agent:"):
            # A bounded sub-agent receives user-authored text plus only its
            # assignment-scoped manifest.  The aggregate /long prompt contains
            # transport receipts for every attachment, so forwarding that goal
            # verbatim would leak paths outside the assignment.
            text_parts = [
                str(part.get("text") or "")
                for part in (
                    state.request_content.get("parts", [])
                    if isinstance(state.request_content, Mapping)
                    else []
                )
                if isinstance(part, Mapping) and part.get("type") == "text"
            ]
            stage_goal = "\n".join(item for item in text_parts if item.strip())
            for item in state.attachment_manifest:
                local_ref = str(item.get("local_ref") or "")
                if local_ref:
                    stage_goal = stage_goal.replace(
                        local_ref, "[attachment path available only through assignment manifest]"
                    )
            authorised_ids = {
                str(item.get("attachment_id") or "")
                for item in stage_attachment_manifest
            }
            unauthorized_refs = tuple(
                str(item.get("local_ref") or "")
                for item in state.attachment_manifest
                if str(item.get("attachment_id") or "") not in authorised_ids
                and str(item.get("local_ref") or "")
            )
            if isinstance(stage_request_content, Mapping) and unauthorized_refs:
                scrubbed_parts = copy.deepcopy(
                    list(stage_request_content.get("parts") or [])
                )
                for part in scrubbed_parts:
                    if not isinstance(part, Mapping) or part.get("type") != "text":
                        continue
                    scrubbed_text = str(part.get("text") or "")
                    for local_ref in unauthorized_refs:
                        scrubbed_text = scrubbed_text.replace(
                            local_ref,
                            "[attachment path not authorized for this assignment]",
                        )
                    part["text"] = scrubbed_text
                stage_request_content = canonical_request_content(scrubbed_parts)
            if not stage_goal.strip():
                stage_goal = str(base_context.get("assigned_task") or state.goal)
            if state.attachment_manifest:
                base_context["authorized_attachment_manifest"] = [
                    copy.deepcopy(item) for item in stage_attachment_manifest
                ]
        invariant_payload = {
            "provider": selected.engine,
            "model": selected.model,
            "provider_reasoning": selected.reasoning,
            "goal_ref": state.ledger.goal_ref,
            "classification": (
                state.ledger.classification.value
                if state.ledger.classification
                else None
            ),
            "role": role,
            "allow_tools": allow_tools,
            "allow_side_effects": allow_side_effects,
            "delegated_tools": base_context.get("delegated_tools"),
            "workzone": self.workzone_ref or None,
            "plan_id": state.ledger.plan_id,
            "attachments": [
                {
                    "attachment_id": item.get("attachment_id"),
                    "item_index": item.get("item_index"),
                    "modality": item.get("modality"),
                    "size_bytes": item.get("size_bytes"),
                    "sha256": item.get("sha256"),
                }
                for item in stage_attachment_manifest
            ],
        }
        retry_invariant_hash = _payload_hash(invariant_payload)
        last_error: StageInvocationError | None = None
        structure_retry_feedback: Mapping[str, Any] | None = None
        state.stage_invocation_serial += 1
        invocation_serial = state.stage_invocation_serial
        invocation_id = (
            f"{state.ledger.turn_id}:{stage.value}:invocation:{invocation_serial}"
        )
        if stage is Stage.EXECUTION and not role.startswith("sub_agent:"):
            state.last_execution_invocation_id = invocation_id
        provider_retry_count = 0
        media_fallback_consumed = False
        attempt = 0
        while True:
            attempt += 1
            if state.control.stopped:
                raise TurnStopped(state.control.reason)
            attempt_context = copy.deepcopy(base_context)
            if structure_retry_feedback is not None:
                attempt_context["previous_structure_error"] = dict(
                    structure_retry_feedback
                )
                attempt_context["retry_instruction"] = (
                    "Correct only the reported response-envelope defect. Preserve "
                    "the authoritative goal, classification, evidence, and uncertainty."
                )
            provider_activity = ProviderActivityTracker()
            request = StageRequest(
                turn_id=state.ledger.turn_id,
                request_ref=state.request_ref,
                stage=stage,
                role=role,
                attempt=attempt,
                goal=stage_goal,
                classification=state.ledger.classification,
                effort=state.effort,
                plan_id=state.ledger.plan_id,
                context=attempt_context,
                request_content=copy.deepcopy(stage_request_content),
                attachment_manifest=tuple(
                    copy.deepcopy(item) for item in stage_attachment_manifest
                ),
                force_local_media_fallback=media_fallback_consumed,
                allow_tools=allow_tools,
                allow_side_effects=allow_side_effects,
                invocation_id=invocation_id,
                retry_invariant_hash=retry_invariant_hash,
                progress_callback=self._progress_callback(state),
                provider_activity_callback=self._provider_activity_callback(
                    state, provider_activity
                ),
            )
            attempt_prefix = (
                f"{state.ledger.turn_id}:{stage.value}:invocation:"
                f"{invocation_serial}:attempt:{attempt}"
            )
            start_ref = self._audit(
                state,
                stage=stage.value,
                role=role,
                event="stage_started",
                event_id=f"{attempt_prefix}:start",
                provider=selected.engine,
                model=selected.model,
                attempt=attempt,
                payload={
                    "goal_ref": state.ledger.goal_ref,
                    "classification": (
                        state.ledger.classification.value
                        if state.ledger.classification
                        else None
                    ),
                    "effort": state.effort.value,
                    "provider_reasoning": selected.reasoning,
                    "allow_tools": allow_tools,
                    "allow_side_effects": allow_side_effects,
                    "fresh_connection": attempt > 1,
                    "provider_retry_count": provider_retry_count,
                    "retry_invariant_hash": retry_invariant_hash,
                    "retry_invariants": invariant_payload,
                    "context": attempt_context,
                },
            )
            state.ledger.add_log_ref(start_ref)
            self.ledger_store.save(state.ledger)
            state.progress.record(
                "stage_started",
                stage.value,
                meaningful=False,
            )
            response: StageResponse | None = None
            try:
                response = await state.control.run_cancellable(
                    self.provider.invoke(selected, request)
                )
                if _used_typed_media_fallback(response.media_routing):
                    media_fallback_consumed = True
                response_ref = self._audit(
                    state,
                    stage=stage.value,
                    role=role,
                    event="provider_response_received",
                    event_id=f"{attempt_prefix}:provider-response",
                    provider=response.provider or selected.engine,
                    model=response.model or selected.model,
                    attempt=attempt,
                    payload={
                        "text": response.text,
                        "data": dict(response.data),
                        "usage": dict(response.usage),
                        "evidence_refs": list(response.evidence_refs),
                        "tool_receipts": [
                            _tool_receipt_payload(item)
                            for item in response.tool_receipts
                        ],
                        "media_routing": [
                            dict(item) for item in response.media_routing
                        ],
                        "reasoning_available": bool(
                            response.reasoning_trace
                            and str(response.reasoning_trace).strip()
                        ),
                        "validation_pending": True,
                        "provider_activity": provider_activity.snapshot(),
                    },
                )
                state.ledger.add_log_ref(response_ref)
                reasoning_ref = self.audit_log.record_reasoning(
                    event_id=f"{attempt_prefix}:reasoning",
                    turn_id=state.ledger.turn_id,
                    request_ref=state.request_ref,
                    stage=stage.value,
                    role=role,
                    provider=response.provider or selected.engine,
                    model=response.model or selected.model,
                    attempt=attempt,
                    plan_id=state.ledger.plan_id,
                    trace=response.reasoning_trace,
                )
                state.ledger.add_log_ref(reasoning_ref)
                self.ledger_store.save(state.ledger)

                effective_response = response
                validation_source = "provider_response"
                try:
                    resolution = resolve_stage_response(response, validator)
                    effective_response = resolution.response
                    parsed = resolution.parsed
                    validation_source = resolution.source
                    if resolution.recovered:
                        self._audit(
                            state,
                            stage=stage.value,
                            role=role,
                            event="structured_response_compatibility_applied",
                            event_id=f"{attempt_prefix}:compatibility",
                            provider=response.provider or selected.engine,
                            model=response.model or selected.model,
                            attempt=attempt,
                            payload={
                                "validation_source": resolution.source,
                                "rejected_candidates": [
                                    {"source": source, "error": error}
                                    for source, error in resolution.rejected_candidates
                                ],
                                "authority_changed": False,
                                "reasoning_exposed_to_user": False,
                            },
                        )
                except StructuredOutputError as exc:
                    if not defer_structured_error:
                        raise
                    if stage is not Stage.EXECUTION or role.startswith("sub_agent:"):
                        raise
                    parsed = None
                    validation_source = "deferred_to_finalisation"
                    self._audit(
                        state,
                        stage=stage.value,
                        role=role,
                        event="execution_structure_deferred_to_finalisation",
                        event_id=f"{attempt_prefix}:deferred-to-finalisation",
                        provider=response.provider or selected.engine,
                        model=response.model or selected.model,
                        attempt=attempt,
                        payload={
                            "validation_error": str(exc),
                            "execution_replayed": False,
                            "raw_output_preserved": True,
                        },
                    )

                complete_ref = self._audit(
                    state,
                    stage=stage.value,
                    role=role,
                    event="stage_completed",
                    event_id=f"{attempt_prefix}:complete",
                    provider=effective_response.provider or selected.engine,
                    model=effective_response.model or selected.model,
                    attempt=attempt,
                    payload={
                        "output": (
                            dict(effective_response.data)
                            if effective_response.data
                            else effective_response.text
                        ),
                        "evidence_refs": list(effective_response.evidence_refs),
                        "tool_receipts": [
                            _tool_receipt_payload(item)
                            for item in effective_response.tool_receipts
                        ],
                        "media_routing": [
                            dict(item) for item in effective_response.media_routing
                        ],
                        "reasoning_available": bool(effective_response.reasoning_trace),
                        "validation_source": validation_source,
                        "retry_invariant_hash": retry_invariant_hash,
                        "provider_activity": provider_activity.snapshot(),
                    },
                )
                state.ledger.add_log_ref(complete_ref)
                self.ledger_store.save(state.ledger)
                for receipt in effective_response.tool_receipts:
                    state.tool_receipts[receipt.evidence_ref] = receipt
                if effective_response.media_routing:
                    state.media_routing_by_stage.setdefault(stage.value, []).append(
                        {
                            "role": role,
                            "invocation_id": invocation_id,
                            "attempt": attempt,
                            "decisions": [
                                dict(item)
                                for item in effective_response.media_routing
                            ],
                        }
                    )
                if publish_commentary and validation_source not in {
                    "reasoning_recovery",
                    "deferred_to_finalisation",
                }:
                    await self._publish_stage_commentary(
                        state,
                        response=effective_response,
                        stage=stage,
                        invocation=invocation_serial,
                        attempt=attempt,
                    )
                state.progress.record(
                    stage.value,
                    str(effective_response.data or effective_response.text),
                )
                return effective_response, parsed
            except TurnStopped:
                raise
            except AuditPersistenceError:
                raise
            except (StructuredOutputError, StageInvocationError) as exc:
                last_error = exc
                if isinstance(exc, StageInvocationError) and _used_typed_media_fallback(
                    exc.details.get("media_routing")
                ):
                    media_fallback_consumed = True
                if isinstance(exc, StructuredOutputError):
                    structure_retry_feedback = {
                        "attempt": attempt,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
            except Exception as exc:
                last_error = StageInvocationError(
                    f"{stage.value} provider failure: {type(exc).__name__}: {exc}",
                    code=ProviderFailureCode.PROVIDER_UNKNOWN,
                    human_description="The provider stage failed unexpectedly.",
                )
            assert isinstance(last_error, StageInvocationError)
            is_structured_repair = isinstance(last_error, StructuredOutputError)
            unobserved_side_effects = bool(
                last_error.side_effects_possible and not provider_activity.tool_started
            )
            replay_safe = bool(
                provider_activity.replay_safe(allow_side_effects=allow_side_effects)
                and not unobserved_side_effects
            )
            retry_kind = (
                "structured_repair" if is_structured_repair else "provider_recovery"
            )
            retry_reason = "eligible"
            if not retry_on_failure:
                retry_reason = "retry_disabled_for_call"
            elif not last_error.retryable:
                retry_reason = "failure_non_retryable"
            elif not replay_safe:
                retry_reason = "side_effect_replay_blocked"
            elif is_structured_repair and state.progress.expired(
                self.config.user_idle_timeout_s
            ):
                retry_reason = "user_meaningful_progress_idle_expired"
            elif (
                not is_structured_repair
                and provider_retry_count >= self.retry_policy.max_provider_retries
            ):
                retry_reason = "provider_recovery_already_used"
            will_retry = retry_reason == "eligible"
            possible_side_effects = bool(
                provider_activity.side_effects_possible or unobserved_side_effects
            )
            recovery_decision: dict[str, Any] | None = None
            if not will_retry and not replay_safe and last_error.retryable:
                blocked_code = (
                    ProviderFailureCode.SIDE_EFFECT_REPLAY_BLOCKED
                    if possible_side_effects
                    else ProviderFailureCode.REPLAY_SAFETY_UNPROVEN
                )
                blocked_description = (
                    "The provider failed after a tool with possible side effects "
                    "started, so HASHI did not replay the execution."
                    if possible_side_effects
                    else "The provider failed while a proven read-only tool call "
                    "remained incomplete, so HASHI could not prove replay safety."
                )
                recovery_decision = {
                    "code": blocked_code.value,
                    "description": blocked_description,
                    "reason": retry_reason,
                    "automatic_replay_attempted": False,
                }
            retry_delay = 0.0
            if will_retry:
                retry_delay = (
                    last_error.retry_after_s
                    if last_error.retry_after_s is not None
                    else min(
                        5.0,
                        0.25 * (2 ** min(max(0, attempt - 1), 5)),
                    )
                )
            try:
                self._audit(
                    state,
                    stage=stage.value,
                    role=role,
                    event="stage_attempt_failed",
                    event_id=f"{attempt_prefix}:failed",
                    provider=selected.engine,
                    model=selected.model,
                    attempt=attempt,
                    payload={
                        **last_error.audit_payload(),
                        "will_retry": will_retry,
                        "retry_kind": retry_kind,
                        "retry_reason": retry_reason,
                        "retry_delay_s": retry_delay if will_retry else None,
                        "fresh_connection_on_retry": will_retry,
                        "retry_invariant_hash": retry_invariant_hash,
                        "provider_response_received": response is not None,
                        "provider_activity": provider_activity.snapshot(),
                        "replay_safe": replay_safe,
                        "side_effects_possible": possible_side_effects,
                        "recovery_decision": recovery_decision,
                    },
                )
            except AuditPersistenceError:
                raise
            if not will_retry:
                if recovery_decision is not None:
                    cleanup = provider_activity.snapshot().get(
                        "foreground_cleanup"
                    ) or dict(state.last_foreground_cleanup)
                    raise last_error.terminal_copy(
                        f"{stage.value} failed after tool activity; automatic replay "
                        f"was blocked by {recovery_decision['code']}: {last_error}",
                        attempts=attempt,
                        side_effects_possible=possible_side_effects,
                        details={
                            "retry_reason": retry_reason,
                            "recovery_decision": recovery_decision,
                            "foreground_cleanup": cleanup or None,
                        },
                    ) from last_error
                raise last_error.terminal_copy(
                    f"{stage.value} failed after {attempt} attempt(s): {last_error}",
                    attempts=attempt,
                    details={"retry_reason": retry_reason},
                ) from last_error
            if not is_structured_repair:
                provider_retry_count += 1
            await self._wait_for_stage_retry(
                state,
                stage=stage,
                attempt=attempt,
                role=role,
                provider=selected.engine,
                model=selected.model,
                retry_kind=retry_kind,
                retry_delay=retry_delay,
                retry_invariant_hash=retry_invariant_hash,
                invocation_id=invocation_id,
            )

    async def _publish_stage_commentary(
        self,
        state: _TurnState,
        *,
        response: StageResponse,
        stage: Stage,
        invocation: int,
        attempt: int,
    ) -> None:
        """Forward optional model-authored neutral prose without workflow authority."""

        try:
            commentary = commentary_from_stage_response(
                response,
                turn_id=state.ledger.turn_id,
                stage=stage,
                invocation=invocation,
                attempt=attempt,
            )
        except CommentaryValidationError as exc:
            self.logger.warning(
                "HER v2 ignored invalid optional commentary at %s/%s: %s",
                stage.value,
                attempt,
                exc,
            )
            self._audit_optional_commentary(
                state,
                event="commentary_rejected",
                event_id=(
                    f"{state.ledger.turn_id}:commentary:{stage.value}:"
                    f"{invocation}:{attempt}:rejected"
                ),
                payload={
                    "stage": stage.value,
                    "attempt": attempt,
                    "reason": str(exc),
                },
            )
            return
        if commentary is None:
            return
        accepted = False
        error_type = ""
        try:
            accepted = bool(await self.commentary.publish(commentary))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - presentation is optional
            error_type = type(exc).__name__
            self.logger.warning(
                "HER v2 commentary lane failed safely at %s/%s: %s",
                stage.value,
                attempt,
                exc,
            )
        self._audit_optional_commentary(
            state,
            event="commentary_publish_result",
            event_id=f"{commentary.event_id}:publish",
            payload={
                "stage": stage.value,
                "attempt": attempt,
                "accepted": accepted,
                "error_type": error_type or None,
                "text_sha256": hashlib.sha256(
                    commentary.text.encode("utf-8")
                ).hexdigest(),
                "text_length": len(commentary.text),
                "workflow_authority": False,
            },
        )
        if accepted:
            state.progress.record("commentary", commentary.text)

    def _audit_optional_commentary(
        self,
        state: _TurnState,
        *,
        event: str,
        event_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        try:
            self._audit(
                state,
                stage="commentary",
                role="neutral_commentary_lane",
                event=event,
                event_id=event_id,
                payload=payload,
            )
        except AuditPersistenceError as exc:
            self.logger.warning(
                "HER v2 optional commentary audit failed without changing workflow: %s",
                exc,
            )

    def _progress_callback(self, state: _TurnState):
        def _record(kind: str, content: str, meaningful: bool = True) -> None:
            state.progress.record(kind, content, meaningful=meaningful)

        return _record

    def _provider_activity_callback(
        self,
        state: _TurnState,
        tracker: ProviderActivityTracker,
    ):
        def _record(event: Mapping[str, Any]) -> None:
            tracker.record(event)
            tool_details = event.get("tool_details")
            cleanup = (
                tool_details.get("foreground_cleanup")
                if isinstance(tool_details, Mapping)
                else None
            )
            if isinstance(cleanup, Mapping):
                state.last_foreground_cleanup = dict(cleanup)

        return _record

    async def _wait_for_stage_retry(
        self,
        state: _TurnState,
        *,
        stage: Stage,
        attempt: int,
        role: str,
        provider: str,
        model: str,
        retry_kind: str,
        retry_delay: float,
        retry_invariant_hash: str,
        invocation_id: str,
    ) -> None:
        """Audit and wait for a same-route fresh-connection recovery."""

        delay = max(0.0, float(retry_delay))
        if retry_kind == "structured_repair":
            idle_remaining = self.config.user_idle_timeout_s - state.progress.idle_for()
            if idle_remaining <= 0:
                raise StageInvocationError(
                    f"{stage.value} structured repair exceeded the user "
                    "meaningful-progress idle timeout",
                    retryable=False,
                    code=ProviderFailureCode.STRUCTURED_OUTPUT_INVALID,
                    human_description=(
                        "Structured response repair stopped after the user-visible "
                        "progress boundary expired."
                    ),
                    attempts=attempt,
                )
            delay = min(delay, idle_remaining)
        self._audit(
            state,
            stage=stage.value,
            role=role,
            event="stage_retry_scheduled",
            event_id=(f"{invocation_id}:attempt:{attempt}:retry-scheduled"),
            provider=provider,
            model=model,
            attempt=attempt,
            payload={
                "retry_kind": retry_kind,
                "failed_attempt": attempt,
                "next_attempt": attempt + 1,
                "retry_delay_s": delay,
                "fresh_connection": True,
                "same_provider": True,
                "same_model": True,
                "same_goal": True,
                "same_classification": True,
                "same_permissions": True,
                "same_workzone": True,
                "retry_invariant_hash": retry_invariant_hash,
            },
        )
        try:
            stopped = await asyncio.wait_for(
                state.control.stop_event.wait(),
                timeout=delay,
            )
        except asyncio.TimeoutError:
            stopped = False
        if stopped or state.control.stopped:
            raise TurnStopped(state.control.reason)
        if retry_kind == "structured_repair" and state.progress.expired(
            self.config.user_idle_timeout_s
        ):
            raise StageInvocationError(
                f"{stage.value} structured repair exceeded the user "
                "meaningful-progress idle timeout",
                retryable=False,
                code=ProviderFailureCode.STRUCTURED_OUTPUT_INVALID,
                human_description=(
                    "Structured response repair stopped after the user-visible "
                    "progress boundary expired."
                ),
                attempts=attempt,
            )
