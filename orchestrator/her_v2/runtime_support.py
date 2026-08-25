"""Lifecycle, delivery, audit, and result support for HER v2."""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import TYPE_CHECKING, Any, Mapping

from .audit import AuditPersistenceError
from .interfaces import (
    DeliveryReceipt,
    StageInvocationError,
    TurnStopped,
)
from .lifecycle import LifecycleViolation
from .models import (
    DeliveryRecord,
    ExecutionOutcome,
    LifecycleState,
    Stage,
    TerminalState,
    TriageClassification,
    TurnResult,
)
from .presentation import (
    RenderedRequiredMessage,
    RequiredMessageValidationError,
    RequiredUserMessage,
)

if TYPE_CHECKING:
    from .runtime import _TurnState


class RuntimeSupportMixin:
    def _record_triage(
        self,
        state: _TurnState,
        classification: TriageClassification,
    ) -> None:
        ref = self._audit(
            state,
            stage=Stage.TRIAGE.value,
            role=self.config.stage_roles[Stage.TRIAGE],
            event="classification_recorded",
            event_id=f"{state.ledger.turn_id}:classification",
            payload={
                "classification": classification.value,
            },
        )
        state.ledger.add_log_ref(ref)
        state.ledger.record_triage(classification)
        self.ledger_store.save(state.ledger)

    async def _transition(
        self,
        state: _TurnState,
        requested: LifecycleState,
        *,
        terminal_reason: str | None = None,
    ) -> None:
        if state.control.stopped and requested not in {
            LifecycleState.ERROR,
            LifecycleState.STOPPED,
        }:
            raise TurnStopped(state.control.reason)
        previous = state.ledger.status
        ref = self._audit(
            state,
            stage="lifecycle",
            role="runtime",
            event="transition",
            event_id=(
                f"{state.ledger.turn_id}:lifecycle:{previous.value}:{requested.value}:"
                f"{state.replan_count}:{state.review_count}:{state.verification_count}"
            ),
            payload={
                "from": previous.value,
                "to": requested.value,
                "terminal_reason": terminal_reason,
            },
        )
        state.ledger.add_log_ref(ref)
        state.ledger.transition(requested, terminal_reason=terminal_reason)
        self.ledger_store.save(state.ledger)
        state.progress.record("lifecycle", f"{previous.value}->{requested.value}")

    def _activate_plan(
        self, state: _TurnState, plan: Mapping[str, Any], *, replacement: bool
    ) -> None:
        state.plan_version += 1
        plan_id = f"{state.ledger.turn_id}:plan:v{state.plan_version}"
        state.ledger.activate_plan(plan_id, replacement=replacement)
        state.active_plan = dict(plan)
        self.ledger_store.save(state.ledger)

    def _merge_execution_evidence(
        self, state: _TurnState, execution: ExecutionOutcome
    ) -> None:
        for ref in execution.evidence_refs:
            if ref not in state.evidence_refs:
                state.evidence_refs.append(ref)
        for limitation in execution.limitations:
            if limitation not in state.limitations:
                state.limitations.append(limitation)

    async def _render_required_clarification(
        self,
        state: _TurnState,
        *,
        text: str,
        event_id: str,
    ) -> tuple[str, str, str]:
        """Render a required clarification without granting workflow authority."""

        message = RequiredUserMessage(
            event_id=event_id,
            turn_id=state.ledger.turn_id,
            kind="clarification",
            text=text,
        )
        if self.required_persona is None:
            self._audit(
                state,
                stage="persona_presentation",
                role="required_message_renderer",
                event="required_persona_render_skipped",
                event_id=f"{event_id}:persona:skipped",
                payload={
                    "kind": message.kind,
                    "reason": "renderer_unavailable",
                    "source_text_sha256": hashlib.sha256(
                        message.text.encode("utf-8")
                    ).hexdigest(),
                    "workflow_authority_changed": False,
                },
            )
            return (
                message.text,
                "unrendered_required_message",
                ("persona_rendering_fallback=true; error_type=renderer_unavailable"),
            )

        self._audit(
            state,
            stage="persona_presentation",
            role="required_message_renderer",
            event="required_persona_render_started",
            event_id=f"{event_id}:persona:start",
            payload={
                "kind": message.kind,
                "source_text_sha256": hashlib.sha256(
                    message.text.encode("utf-8")
                ).hexdigest(),
                "workflow_authority_changed": False,
            },
        )
        try:
            rendered = await self.required_persona.render(message)
            if not isinstance(rendered, RenderedRequiredMessage):
                raise RequiredMessageValidationError(
                    "required Persona renderer returned an untyped result"
                )
            if rendered.source_event_id != message.event_id:
                raise RequiredMessageValidationError(
                    "required Persona renderer changed the source identity"
                )
            if rendered.kind != message.kind:
                raise RequiredMessageValidationError(
                    "required Persona renderer changed the delivery kind"
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - validated content must survive
            rendered = RenderedRequiredMessage(
                source_event_id=message.event_id,
                kind=message.kind,
                text=message.text,
                provenance="required_message_identity_fallback",
                fallback=True,
                error_type=type(exc).__name__,
            )
            self.logger.warning(
                "HER v2 required Persona rendering degraded safely: %s", exc
            )

        self._audit(
            state,
            stage="persona_presentation",
            role="required_message_renderer",
            event="required_persona_render_completed",
            event_id=f"{event_id}:persona:complete",
            payload={
                "kind": message.kind,
                "source_text_sha256": hashlib.sha256(
                    message.text.encode("utf-8")
                ).hexdigest(),
                "rendered_text_sha256": hashlib.sha256(
                    rendered.text.encode("utf-8")
                ).hexdigest(),
                "provenance": rendered.provenance,
                "fallback": rendered.fallback,
                "error_type": rendered.error_type or None,
                "workflow_authority_changed": False,
            },
        )
        detail = (
            "persona_rendering_fallback=true; "
            f"error_type={rendered.error_type or 'unknown'}"
            if rendered.fallback
            else "persona_rendering_fallback=false"
        )
        return rendered.text, rendered.provenance, detail

    async def _deliver(
        self,
        state: _TurnState,
        *,
        kind: str,
        text: str,
        event_id: str,
        required: bool,
        phase: str = "",
        provenance: str = "",
        detail: str = "",
    ) -> bool:
        if kind == "commentary":
            raise ValueError("raw commentary cannot use the workflow delivery boundary")
        if any(item.event_id == event_id for item in state.deliveries):
            return True
        delivery_id = ""
        if kind in {"final", "clarification"}:
            delivery_id = f"{state.ledger.turn_id}:delivery:{kind}"
            state.delivery_id = delivery_id
            state.delivery_kind = kind
            state.delivery_event_id = event_id
        self._audit(
            state,
            stage="delivery",
            role="hashi_transport",
            event="delivery_intent",
            event_id=f"{event_id}:{kind}:intent",
            payload={
                "kind": kind,
                "required": required,
                "delivery_id": delivery_id or None,
                "message_event_id": event_id,
            },
        )
        delivered = False
        disposition = "transport_rejected"
        try:
            raw_receipt = await self.delivery.deliver(
                kind=kind,
                text=text,
                event_id=event_id,
                required=required,
                phase=phase,
                provenance=provenance,
                detail=detail,
                delivery_id=delivery_id,
            )
            if isinstance(raw_receipt, DeliveryReceipt):
                accepted = bool(raw_receipt.accepted)
                delivered = bool(raw_receipt.delivered)
                disposition = str(raw_receipt.disposition or "transport_rejected")
            else:
                accepted = bool(raw_receipt)
                delivered = accepted
                disposition = (
                    "transport_delivered" if accepted else "transport_rejected"
                )
        except Exception as exc:
            accepted = False
            delivered = False
            disposition = "transport_exception"
            self.logger.warning(
                "HER v2 %s delivery failed without changing workflow authority: %s",
                kind,
                exc,
            )
        delivery_payload: dict[str, Any] = {
            "kind": kind,
            "accepted": accepted,
            "delivered": delivered,
            "disposition": disposition,
            "required": required,
            "delivery_id": delivery_id or None,
            "message_event_id": event_id,
        }
        if phase:
            delivery_payload["phase"] = phase
        if provenance:
            delivery_payload["provenance"] = provenance
        self._audit(
            state,
            stage="delivery",
            role="hashi_transport",
            event="delivery_result",
            event_id=f"{event_id}:{kind}:delivery",
            payload=delivery_payload,
        )
        if accepted:
            state.deliveries.append(DeliveryRecord(kind, text, event_id))
            if kind in {"acknowledgement", "draft", "clarification", "final"}:
                progress_kind = "delivery" if delivered else "delivery_deferred"
                state.progress.record(f"{progress_kind}:{kind}", text)
        return accepted

    async def _resolve_initial(
        self,
        state: _TurnState,
        *,
        resolution: str,
        text: str,
        target_event_id: str,
        event_id: str,
    ) -> bool:
        resolver = getattr(self.delivery, "resolve_initial", None)
        if not callable(resolver):
            return False
        delivery_id = ""
        if resolution in {"final", "clarification"}:
            delivery_id = f"{state.ledger.turn_id}:delivery:{resolution}"
            state.delivery_id = delivery_id
            state.delivery_kind = resolution
            state.delivery_event_id = target_event_id
        self._audit(
            state,
            stage="delivery",
            role="hashi_transport",
            event="initial_resolution_intent",
            event_id=f"{event_id}:intent",
            payload={
                "resolution": resolution,
                "target_event_id": target_event_id,
                "delivery_id": delivery_id or None,
            },
        )
        accepted = False
        delivered = False
        disposition = "provisional_resolution_rejected"
        try:
            raw_receipt = await resolver(
                resolution=resolution,
                text=text,
                target_event_id=target_event_id,
                event_id=event_id,
                delivery_id=delivery_id,
            )
            if isinstance(raw_receipt, DeliveryReceipt):
                accepted = bool(raw_receipt.accepted)
                delivered = bool(raw_receipt.delivered)
                disposition = str(raw_receipt.disposition or disposition)
            else:
                accepted = bool(raw_receipt)
                delivered = accepted
                disposition = (
                    f"provisional_{resolution}"
                    if accepted
                    else "provisional_resolution_rejected"
                )
        except Exception as exc:
            disposition = "provisional_resolution_exception"
            self.logger.warning(
                "HER v2 initial %s resolution failed without changing workflow "
                "authority: %s",
                resolution,
                exc,
            )
        self._audit(
            state,
            stage="delivery",
            role="hashi_transport",
            event="initial_resolution_result",
            event_id=f"{event_id}:delivery",
            payload={
                "resolution": resolution,
                "target_event_id": target_event_id,
                "accepted": accepted,
                "delivered": delivered,
                "disposition": disposition,
                "delivery_id": delivery_id or None,
            },
        )
        if accepted:
            index = next(
                (
                    index
                    for index, record in enumerate(state.deliveries)
                    if record.event_id == target_event_id
                ),
                None,
            )
            if index is not None:
                if resolution == "discard":
                    state.deliveries.pop(index)
                else:
                    kind = (
                        "acknowledgement" if resolution == "commentary" else resolution
                    )
                    state.deliveries[index] = DeliveryRecord(
                        kind, text, target_event_id
                    )
            state.progress.record(f"initial_resolution:{resolution}", disposition)
        return delivered

    def _audit(
        self,
        state: _TurnState,
        *,
        stage: str,
        role: str,
        event: str,
        event_id: str,
        provider: str = "",
        model: str = "",
        attempt: int = 1,
        payload: Mapping[str, Any] | None = None,
    ) -> str:
        return self.audit_log.append(
            event_id=event_id,
            turn_id=state.ledger.turn_id,
            request_ref=state.request_ref,
            stage=stage,
            role=role,
            event=event,
            provider=provider,
            model=model,
            attempt=attempt,
            plan_id=state.ledger.plan_id,
            payload=payload,
        )

    async def _stopped_result(self, state: _TurnState, reason: str) -> TurnResult:
        if not state.ledger.is_terminal:
            try:
                await self._transition(
                    state,
                    LifecycleState.STOPPED,
                    terminal_reason=reason,
                )
            except AuditPersistenceError:
                state.ledger.transition(LifecycleState.STOPPED, terminal_reason=reason)
                self.ledger_store.save(state.ledger)
        return self._result(
            state,
            terminal=TerminalState.STOPPED,
            text="",
            error=reason,
        )

    async def _error_result(
        self,
        state: _TurnState,
        error: str,
        *,
        text: str = "",
    ) -> TurnResult:
        if not state.ledger.is_terminal:
            try:
                await self._transition(
                    state,
                    LifecycleState.ERROR,
                    terminal_reason=error,
                )
            except (AuditPersistenceError, LifecycleViolation):
                if not state.ledger.is_terminal:
                    state.ledger.transition(LifecycleState.ERROR, terminal_reason=error)
                self.ledger_store.save(state.ledger)
        return self._result(
            state,
            terminal=TerminalState.ERROR,
            text=text,
            error=error,
        )

    def _schedule_meditation(
        self,
        state: _TurnState,
        *,
        terminal: TerminalState,
        execution_summary: str = "",
    ) -> None:
        if (
            not self.config.meditation_enabled
            or self.config.shadow_mode
            or terminal
            not in {
                TerminalState.COMPLETED,
                TerminalState.COMPLETED_WITH_LIMITATIONS,
            }
        ):
            return

        async def _run() -> None:
            try:
                await self.meditation.meditate(
                    turn_id=state.ledger.turn_id,
                    goal=state.goal,
                    summary=execution_summary,
                    evidence_refs=tuple(state.evidence_refs),
                    limitations=tuple(state.limitations),
                    terminal_state=terminal,
                )
            except Exception as exc:
                self.logger.warning("HER v2 Meditation failed safely: %s", exc)

        task = asyncio.create_task(_run())
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def _result(
        self,
        state: _TurnState,
        *,
        terminal: TerminalState,
        text: str,
        error: str = "",
        final_was_immediate: bool = False,
        final_already_delivered: bool = False,
    ) -> TurnResult:
        failure = state.terminal_failure or state.last_execution_failure
        primary_failure = (
            {
                "code": failure.error_code,
                "description": failure.human_description,
                "attempts": failure.attempts,
                "side_effects_possible": failure.side_effects_possible,
            }
            if failure is not None
            else {}
        )
        recovery_value = (
            failure.details.get("recovery_decision") if failure is not None else None
        )
        recovery_decision = (
            dict(recovery_value) if isinstance(recovery_value, Mapping) else {}
        )
        failure_cleanup = (
            failure.details.get("foreground_cleanup") if failure is not None else None
        )
        cleanup_value = failure_cleanup or state.last_foreground_cleanup
        foreground_cleanup = (
            dict(cleanup_value) if isinstance(cleanup_value, Mapping) else {}
        )
        return TurnResult(
            turn_id=state.ledger.turn_id,
            terminal_state=terminal,
            text=text,
            classification=state.ledger.classification,
            ledger=state.ledger.to_dict(),
            delivery_records=tuple(state.deliveries),
            evidence_refs=tuple(state.evidence_refs),
            limitations=tuple(state.limitations),
            error=error,
            primary_failure=primary_failure,
            recovery_decision=recovery_decision,
            foreground_cleanup=foreground_cleanup,
            final_was_immediate=final_was_immediate,
            final_already_delivered=final_already_delivered,
            delivery_id=state.delivery_id,
            delivery_kind=state.delivery_kind,
            delivery_event_id=state.delivery_event_id,
            review_count=state.review_count,
            verification_count=state.verification_count,
            checkpoint_count=state.checkpoint_count,
            assurance_status="",
            replan_count=state.replan_count,
        )


def _execution_payload(outcome: ExecutionOutcome) -> dict[str, Any]:
    return {
        "disposition": outcome.disposition.value,
        "summary": outcome.summary,
        "work_performed": list(outcome.work_performed),
        "verification": list(outcome.verification),
        "evidence_refs": list(outcome.evidence_refs),
        "limitations": list(outcome.limitations),
        "remaining_work": list(outcome.remaining_work),
        "clarification": outcome.clarification or None,
    }


def _payload_hash(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        dict(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _technical_error_message(
    turn_id: str,
    *,
    code: str,
    description: str,
    attempts: int,
    side_effects_possible: bool,
    recovery_decision: Mapping[str, Any] | None = None,
    foreground_cleanup: Mapping[str, Any] | None = None,
) -> str:
    side_effect_text = (
        "possible; automatic replay was blocked"
        if side_effects_possible
        else "none observed"
    )
    lines = [
        "⚠️ A technical provider failure prevented completion.",
        "",
        "Primary failure:",
        f"Error code: `{code}`",
        f"Description: {description}",
        f"Provider attempts: {max(1, int(attempts))}",
        f"Possible side effects: {side_effect_text}",
    ]
    recovery = dict(recovery_decision) if isinstance(recovery_decision, Mapping) else {}
    if recovery:
        lines.extend(
            [
                "",
                "Recovery decision:",
                f"Code: `{recovery.get('code') or 'UNKNOWN'}`",
                f"Description: {recovery.get('description') or 'No description recorded.'}",
                "Automatic replay attempted: "
                + (
                    "yes"
                    if recovery.get("automatic_replay_attempted") is True
                    else "no"
                ),
            ]
        )
    cleanup = (
        dict(foreground_cleanup) if isinstance(foreground_cleanup, Mapping) else {}
    )
    if cleanup:
        lines.extend(
            [
                "",
                "Foreground cleanup:",
                f"Status: `{cleanup.get('status') or 'unknown'}`",
                "Process reaped: "
                + ("yes" if cleanup.get("process_reaped") is True else "no"),
            ]
        )
        if cleanup.get("errors"):
            lines.append("Cleanup errors: " + "; ".join(map(str, cleanup["errors"])))
    lines.extend(["", f"Reference: `{turn_id}`"])
    return "\n".join(lines)


def _technical_error_message_from_failure(
    turn_id: str,
    failure: StageInvocationError,
    *,
    foreground_cleanup: Mapping[str, Any] | None = None,
) -> str:
    failure_cleanup = failure.details.get("foreground_cleanup")
    return _technical_error_message(
        turn_id,
        code=failure.error_code,
        description=failure.human_description,
        attempts=failure.attempts,
        side_effects_possible=failure.side_effects_possible,
        recovery_decision=(
            failure.details.get("recovery_decision")
            if isinstance(failure.details.get("recovery_decision"), Mapping)
            else None
        ),
        foreground_cleanup=(
            failure_cleanup
            if isinstance(failure_cleanup, Mapping)
            else foreground_cleanup
        ),
    )


def _normalise_text(value: str) -> str:
    return " ".join(str(value or "").casefold().split()).rstrip(".?!。？！")


def _terminal_reason(state: TerminalState) -> str:
    return {
        TerminalState.COMPLETED: "goal_achieved",
        TerminalState.COMPLETED_WITH_LIMITATIONS: "completed_with_disclosed_limitations",
        TerminalState.FAILED: "goal_not_achieved",
        TerminalState.ERROR: "technical_failure",
        TerminalState.STOPPED: "authorised_stop",
        TerminalState.PENDING_USER_INPUT: "user_input_required",
    }[state]
