"""Modular, provider-neutral HER v2 stage orchestrator."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from contextlib import suppress
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping, Sequence

from .audit import AuditPersistenceError, DurableAuditLog
from .commentary import CommentaryPort, NullCommentaryPort
from .config import HERv2Config
from .interfaces import (
    DeliveryPort,
    DreamMaintainer,
    HabitAdvisor,
    MeditationRunner,
    NullDreamMaintainer,
    NullHabitAdvisor,
    NullMeditationRunner,
    ProviderFailureCode,
    RecordingDelivery,
    StageInvocationError,
    StageProvider,
    TurnControl,
    TurnStopped,
)
from .ledger import ExecutionLedger, LedgerInvariantError, LedgerStore
from .lifecycle import LifecycleViolation
from .models import (
    WORK_CLASSIFICATIONS,
    DeliveryRecord,
    Effort,
    ExecutionDisposition,
    ExecutionOutcome,
    FinalisationOutcome,
    LifecycleState,
    ReviewFinding,
    ReviewOutcome,
    Stage,
    StageResponse,
    SubAgentAssignment,
    SubAgentResult,
    TerminalState,
    TriageClassification,
    TriageDecision,
    TurnResult,
    terminal_lifecycle,
)
from .policy import resolve_policy, terminal_for_execution
from .presentation import RequiredPersonaRenderer
from .progress import ProgressTracker
from .retry import DEFAULT_PROVIDER_RETRY_POLICY, ProviderRetryPolicy
from .runtime_invocation import RuntimeInvocationMixin
from .runtime_support import (
    RuntimeSupportMixin,
    _execution_payload,
    _normalise_text,
    _payload_hash,
    _technical_error_message,
    _technical_error_message_from_failure,
    _terminal_reason,
)
from .structured import (
    parse_execution,
    parse_finalisation,
    parse_immediate,
    parse_plan,
    parse_review,
    parse_triage,
)

Validator = Callable[[StageResponse], Any]


@dataclass
class _TurnState:
    request: str
    request_ref: str
    goal: str
    effort: Effort
    ledger: ExecutionLedger
    control: TurnControl
    progress: ProgressTracker = field(default_factory=ProgressTracker)
    deliveries: list[DeliveryRecord] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    active_plan: Mapping[str, Any] | None = None
    plan_version: int = 0
    replan_count: int = 0
    review_count: int = 0
    stage_invocation_serial: int = 0
    last_review: ReviewFinding | None = None
    delivery_id: str = ""
    delivery_kind: str = ""
    delivery_event_id: str = ""
    execution_capability_escalated: bool = False
    last_execution_response: StageResponse | None = None
    last_execution_structure_valid: bool = False
    last_execution_error: str = ""
    last_execution_failure: StageInvocationError | None = None
    terminal_failure: StageInvocationError | None = None
    last_foreground_cleanup: Mapping[str, Any] = field(default_factory=dict)
    last_execution_invocation_id: str = ""
    late_immediate_source_task: asyncio.Task | None = None
    late_immediate_delivery_task: asyncio.Task | None = None


class HERv2Runtime(RuntimeInvocationMixin, RuntimeSupportMixin):
    """Execute one HER v2 turn while enforcing the locked runtime invariants."""

    def __init__(
        self,
        *,
        config: HERv2Config,
        provider: StageProvider,
        ledger_store: LedgerStore,
        audit_log: DurableAuditLog,
        delivery: DeliveryPort | None = None,
        commentary: CommentaryPort | None = None,
        required_persona: RequiredPersonaRenderer | None = None,
        habits: HabitAdvisor | None = None,
        meditation: MeditationRunner | None = None,
        dream: DreamMaintainer | None = None,
        logger: logging.Logger | None = None,
        retry_policy: ProviderRetryPolicy | None = None,
        workzone_ref: str = "",
    ) -> None:
        self.config = config
        self.provider = provider
        self.ledger_store = ledger_store
        self.audit_log = audit_log
        self.delivery = delivery or RecordingDelivery()
        self.commentary = commentary or NullCommentaryPort()
        self.required_persona = required_persona
        self.habits = habits or NullHabitAdvisor()
        self.meditation = meditation or NullMeditationRunner()
        # Dream is intentionally owned by a background maintenance caller.  It
        # is retained as an explicit replaceable dependency but is never
        # invoked from run_turn().
        self.dream = dream or NullDreamMaintainer()
        self.logger = logger or logging.getLogger("HASHI.HERv2")
        self.retry_policy = retry_policy or DEFAULT_PROVIDER_RETRY_POLICY
        self.workzone_ref = str(workzone_ref or "")
        self._controls: dict[str, TurnControl] = {}
        self._turn_tasks: dict[str, asyncio.Task] = {}
        self._background_tasks: set[asyncio.Task] = set()

    async def run_turn(
        self,
        request: str,
        request_id: str,
        *,
        effort: Effort | str = Effort.MEDIUM,
        turn_id: str | None = None,
    ) -> TurnResult:
        prompt = str(request or "").strip()
        if not prompt:
            raise ValueError("HER v2 requires a non-empty authoritative request")
        effort_value = (
            effort if isinstance(effort, Effort) else Effort(str(effort).lower())
        )
        identity = turn_id or f"{request_id}-{uuid.uuid4().hex[:12]}"
        request_ref = f"hashi-request:{request_id}"
        goal_ref = "sha256:" + hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        ledger = ExecutionLedger(identity, request_ref, goal_ref)
        control = TurnControl(identity)
        state = _TurnState(
            request=prompt,
            request_ref=request_ref,
            goal=prompt,
            effort=effort_value,
            ledger=ledger,
            control=control,
        )
        self.ledger_store.save(ledger)
        self._controls[identity] = control
        current = asyncio.current_task()
        if current is not None:
            self._turn_tasks[identity] = current
        try:
            return await self._run_turn(state)
        except TurnStopped as exc:
            return await self._stopped_result(state, exc.reason)
        except AuditPersistenceError as exc:
            if self.config.audit_failure_terminal is TerminalState.STOPPED:
                return await self._stopped_result(
                    state, f"AUDIT_PERSISTENCE_FAILURE: {exc}"
                )
            return await self._error_result(
                state,
                f"[{ProviderFailureCode.AUDIT_PERSISTENCE_FAILURE.value}] {exc}",
                text=_technical_error_message(
                    state.ledger.turn_id,
                    code=ProviderFailureCode.AUDIT_PERSISTENCE_FAILURE.value,
                    description=(
                        "Required audit persistence failed, so execution stopped "
                        "before any unaudited continuation."
                    ),
                    attempts=1,
                    side_effects_possible=False,
                ),
            )
        except (LedgerInvariantError, LifecycleViolation) as exc:
            self.ledger_store.save(state.ledger)
            return self._result(
                state,
                terminal=TerminalState.ERROR,
                text="",
                error=str(exc),
            )
        except StageInvocationError as exc:
            state.terminal_failure = exc
            return await self._error_result(
                state,
                f"[{exc.error_code}] {exc.human_description}",
                text=_technical_error_message_from_failure(
                    state.ledger.turn_id,
                    exc,
                    foreground_cleanup=state.last_foreground_cleanup,
                ),
            )
        finally:
            self._controls.pop(identity, None)
            self._turn_tasks.pop(identity, None)

    async def stop_turn(self, turn_id: str, *, reason: str = "USER_STOP") -> bool:
        control = self._controls.get(str(turn_id))
        if control is None:
            return False
        control.stop(reason)
        return True

    async def steer(
        self,
        old_turn_id: str,
        new_request: str,
        new_request_id: str,
        *,
        effort: Effort | str = Effort.MEDIUM,
    ) -> TurnResult:
        stopped = await self.stop_turn(old_turn_id, reason="STEERED")
        old_task = self._turn_tasks.get(old_turn_id)
        if stopped and old_task is not None and old_task is not asyncio.current_task():
            await asyncio.gather(old_task, return_exceptions=True)
        return await self.run_turn(new_request, new_request_id, effort=effort)

    def reconcile_after_restart(self) -> tuple[ExecutionLedger, ...]:
        """Mark old in-flight ledgers ERROR; never reconstruct execution stacks."""

        return self.ledger_store.reconcile_interrupted()

    async def shutdown(self, *, reason: str = "RUNTIME_SHUTDOWN") -> None:
        controls = tuple(self._controls.values())
        for control in controls:
            control.stop(reason)
        if controls:
            await asyncio.gather(
                *(control.wait_stopped() for control in controls),
                return_exceptions=True,
            )
        for task in tuple(self._background_tasks):
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks.clear()

    async def _run_turn(self, state: _TurnState) -> TurnResult:
        ref = self._audit(
            state,
            stage="initial",
            role="primary",
            event="request_received",
            event_id=f"{state.ledger.turn_id}:request",
            payload={"request": state.request, "effort": state.effort.value},
        )
        state.ledger.add_log_ref(ref)
        self.ledger_store.save(state.ledger)

        immediate_task = asyncio.create_task(
            self._invoke_stage(
                state,
                Stage.IMMEDIATE_RESPONSE,
                parse_immediate,
                allow_tools=False,
            )
        )
        triage_task = asyncio.create_task(
            self._invoke_stage(
                state,
                Stage.TRIAGE,
                parse_triage,
                allow_tools=False,
            )
        )
        immediate_pair = None
        triage_pair = None
        immediate_error: StageInvocationError | None = None
        immediate_delivery_attempted_early = False
        immediate_delivered_early = False

        async def consume_immediate() -> None:
            nonlocal immediate_pair, immediate_error
            try:
                immediate_pair = await immediate_task
            except StageInvocationError as exc:
                immediate_error = exc

        try:
            done, _pending = await asyncio.wait(
                {immediate_task, triage_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if immediate_task in done and triage_task not in done:
                await consume_immediate()
                if immediate_pair is not None:
                    immediate_delivery_attempted_early = True
                    immediate_delivered_early = await self._deliver(
                        state,
                        kind="immediate",
                        text=immediate_pair[1],
                        event_id=f"{state.ledger.turn_id}:immediate",
                        required=False,
                    )
            triage_pair = await triage_task
        except BaseException:
            immediate_task.cancel()
            triage_task.cancel()
            await asyncio.gather(immediate_task, triage_task, return_exceptions=True)
            if immediate_delivered_early:
                with suppress(Exception):
                    await self._resolve_initial(
                        state,
                        resolution="discard",
                        text="",
                        target_event_id=f"{state.ledger.turn_id}:immediate",
                        event_id=f"{state.ledger.turn_id}:immediate:discard",
                    )
            raise

        _triage_response, triage = triage_pair
        assert isinstance(triage, TriageDecision)

        # Triage's model-authored goal is evidence, not authority.  Every
        # downstream request continues to receive the immutable user request.
        if triage.goal and _normalise_text(triage.goal) != _normalise_text(state.goal):
            self._audit(
                state,
                stage=Stage.TRIAGE.value,
                role=self.config.stage_roles[Stage.TRIAGE],
                event="triage_goal_interpretation_recorded",
                event_id=f"{state.ledger.turn_id}:triage:goal-interpretation",
                payload={
                    "authoritative_goal_ref": state.ledger.goal_ref,
                    "triage_interpretation": triage.goal,
                    "authority_changed": False,
                },
            )
        self._record_triage(state, triage.classification)
        state.progress.record("classification", triage.classification.value)

        # Triage is authoritative, but winning this race does not cancel a
        # still-useful Immediate Response.  Work begins without waiting while
        # the optional acknowledgement remains owned by this turn.
        immediate_pending_for_work = False
        if immediate_pair is None and immediate_error is None:
            if triage.classification is TriageClassification.DIRECT_RESPONSE:
                await consume_immediate()
            elif immediate_task.done():
                await consume_immediate()
            elif triage.classification in WORK_CLASSIFICATIONS:
                immediate_pending_for_work = True
                self._audit(
                    state,
                    stage=Stage.IMMEDIATE_RESPONSE.value,
                    role=self.config.stage_roles[Stage.IMMEDIATE_RESPONSE],
                    event="optional_stage_continues",
                    event_id=f"{state.ledger.turn_id}:immediate:continues",
                    payload={
                        "classification": triage.classification.value,
                        "reason": "triage_completed_before_optional_immediate_response",
                        "authoritative_path_waited": False,
                        "delivery_when_ready": "acknowledgement",
                    },
                )
            else:
                immediate_task.cancel()
                await asyncio.gather(immediate_task, return_exceptions=True)
                self._audit(
                    state,
                    stage=Stage.IMMEDIATE_RESPONSE.value,
                    role=self.config.stage_roles[Stage.IMMEDIATE_RESPONSE],
                    event="optional_stage_superseded",
                    event_id=f"{state.ledger.turn_id}:immediate:superseded",
                    payload={
                        "classification": triage.classification.value,
                        "reason": "authoritative_clarification_ready",
                        "authoritative_path_waited": False,
                    },
                )

        if immediate_error is not None:
            self._audit(
                state,
                stage=Stage.IMMEDIATE_RESPONSE.value,
                role=self.config.stage_roles[Stage.IMMEDIATE_RESPONSE],
                event="optional_stage_degraded",
                event_id=f"{state.ledger.turn_id}:immediate:degraded",
                payload={
                    "classification": triage.classification.value,
                    "reason": str(immediate_error),
                    "authoritative_path_continued": (
                        triage.classification
                        is not TriageClassification.DIRECT_RESPONSE
                    ),
                },
            )
        if (
            triage.classification is TriageClassification.DIRECT_RESPONSE
            and immediate_pair is None
        ):
            if isinstance(immediate_error, StageInvocationError):
                raise immediate_error.terminal_copy(
                    "direct response requires a valid Immediate Response: "
                    f"{immediate_error}",
                    attempts=immediate_error.attempts,
                    human_description=(
                        "direct response requires a valid Immediate Response; "
                        f"{immediate_error.human_description}"
                    ),
                ) from immediate_error
            raise StageInvocationError(
                "direct response requires a valid Immediate Response: "
                f"{immediate_error or 'response unavailable'}",
                retryable=False,
                code=ProviderFailureCode.PROVIDER_EMPTY_RESPONSE,
                human_description=(
                    "The required Immediate Response did not produce usable content."
                ),
            )

        immediate_text = ""
        if immediate_pair is not None:
            _immediate_response, immediate_text = immediate_pair
            assert isinstance(immediate_text, str)

        clarification = triage.clarification
        clarification_provenance = ""
        clarification_detail = ""
        if triage.classification is TriageClassification.CONFIRMATION_REQUIRED:
            (
                clarification,
                clarification_provenance,
                clarification_detail,
            ) = await self._render_required_message(
                state,
                kind="clarification",
                text=triage.clarification,
                event_id=f"{state.ledger.turn_id}:clarification",
            )

        immediate_resolution_delivered = False
        if immediate_delivered_early:
            if triage.classification is TriageClassification.DIRECT_RESPONSE:
                resolution = "final"
                resolution_text = immediate_text
            elif triage.classification is TriageClassification.CONFIRMATION_REQUIRED:
                resolution = "clarification"
                resolution_text = clarification
            else:
                resolution = "commentary"
                resolution_text = immediate_text
            immediate_resolution_delivered = await self._resolve_initial(
                state,
                resolution=resolution,
                text=resolution_text,
                target_event_id=f"{state.ledger.turn_id}:immediate",
                event_id=f"{state.ledger.turn_id}:immediate:resolution",
            )

        immediate_kind = (
            "final"
            if triage.classification is TriageClassification.DIRECT_RESPONSE
            else "acknowledgement"
        )
        if immediate_text and (
            not immediate_delivery_attempted_early or not immediate_delivered_early
        ):
            await self._deliver(
                state,
                kind=immediate_kind,
                text=immediate_text,
                event_id=f"{state.ledger.turn_id}:immediate",
                required=immediate_kind == "final",
            )

        if triage.classification is TriageClassification.DIRECT_RESPONSE:
            await self._transition(state, LifecycleState.FINALISING)
            await self._transition(
                state,
                LifecycleState.COMPLETED,
                terminal_reason="direct_response",
            )
            return self._result(
                state,
                terminal=TerminalState.COMPLETED,
                text=immediate_text,
                final_was_immediate=True,
                final_already_delivered=immediate_resolution_delivered,
            )

        if triage.classification is TriageClassification.CONFIRMATION_REQUIRED:
            if not immediate_resolution_delivered and (
                _normalise_text(triage.clarification) != _normalise_text(immediate_text)
            ):
                await self._deliver(
                    state,
                    kind="clarification",
                    text=clarification,
                    event_id=f"{state.ledger.turn_id}:clarification",
                    required=True,
                    provenance=clarification_provenance,
                    detail=clarification_detail,
                )
            await self._transition(
                state,
                LifecycleState.PENDING_USER_INPUT,
                terminal_reason="confirmation_required",
            )
            return self._result(
                state,
                terminal=TerminalState.PENDING_USER_INPUT,
                text=clarification,
                final_already_delivered=immediate_resolution_delivered,
            )

        if triage.classification not in WORK_CLASSIFICATIONS:
            raise StageInvocationError(
                "Triage returned an unsupported work classification"
            )
        if not immediate_pending_for_work:
            return await self._run_work(state, triage.classification)

        late_immediate = asyncio.create_task(
            self._deliver_pending_immediate(state, immediate_task)
        )
        state.late_immediate_source_task = immediate_task
        state.late_immediate_delivery_task = late_immediate
        try:
            return await self._run_work(state, triage.classification)
        finally:
            await self._settle_late_immediate(
                state,
                reason="authoritative_work_path_ended_before_immediate_response",
                deliver_if_source_ready=False,
            )

    async def _deliver_pending_immediate(
        self,
        state: _TurnState,
        immediate_task: asyncio.Task,
    ) -> None:
        """Deliver a Triage-late Immediate response without blocking work."""

        try:
            _response, immediate_text = await immediate_task
        except asyncio.CancelledError:
            raise
        except TurnStopped:
            return
        except StageInvocationError as exc:
            self._audit(
                state,
                stage=Stage.IMMEDIATE_RESPONSE.value,
                role=self.config.stage_roles[Stage.IMMEDIATE_RESPONSE],
                event="optional_stage_degraded",
                event_id=f"{state.ledger.turn_id}:immediate:degraded",
                payload={
                    "classification": (
                        state.ledger.classification.value
                        if state.ledger.classification
                        else None
                    ),
                    "reason": str(exc),
                    "authoritative_path_continued": True,
                },
            )
            return
        assert isinstance(immediate_text, str)
        await self._deliver(
            state,
            kind="acknowledgement",
            text=immediate_text,
            event_id=f"{state.ledger.turn_id}:immediate",
            required=False,
        )

    async def _settle_late_immediate(
        self,
        state: _TurnState,
        *,
        reason: str,
        deliver_if_source_ready: bool,
    ) -> None:
        """Order or supersede a Triage-late acknowledgement before resolution."""

        source_task = state.late_immediate_source_task
        delivery_task = state.late_immediate_delivery_task
        if source_task is None or delivery_task is None:
            return
        if delivery_task.done() or (deliver_if_source_ready and source_task.done()):
            try:
                await delivery_task
            finally:
                state.late_immediate_source_task = None
                state.late_immediate_delivery_task = None
            return

        delivery_task.cancel()
        await asyncio.gather(delivery_task, return_exceptions=True)
        state.late_immediate_source_task = None
        state.late_immediate_delivery_task = None
        self._audit(
            state,
            stage=Stage.IMMEDIATE_RESPONSE.value,
            role=self.config.stage_roles[Stage.IMMEDIATE_RESPONSE],
            event="optional_stage_superseded",
            event_id=f"{state.ledger.turn_id}:immediate:superseded",
            payload={
                "classification": (
                    state.ledger.classification.value
                    if state.ledger.classification
                    else None
                ),
                "reason": reason,
                "authoritative_path_waited": False,
            },
        )

    async def _run_work(
        self, state: _TurnState, classification: TriageClassification
    ) -> TurnResult:
        state.ledger.assert_classification(classification)
        policy = resolve_policy(
            state.effort,
            replan_limit=self.config.replan_limits[state.effort],
            review_limit=self.config.review_limits[state.effort],
        )
        if policy.planning:
            planning_context: dict[str, Any] = {
                "classification": classification.value,
            }
            # ``meditation_enabled`` is the compatibility switch for the
            # whole Habit–Meditation loop, matching /habit off semantics.
            if self.config.meditation_enabled:
                habits: Sequence[str] = ()
                with suppress(Exception):
                    habits = await self.habits.retrieve(
                        goal=state.goal, turn_id=state.ledger.turn_id
                    )
                planning_context.update(
                    {
                        "habits": list(habits),
                        "habits_are_advisory": True,
                    }
                )
            _response, plan = await self._invoke_stage(
                state,
                Stage.PLANNING,
                parse_plan,
                allow_tools=False,
                context=planning_context,
            )
            await self._transition(state, LifecycleState.PLANNED)
            self._activate_plan(state, plan, replacement=False)

        await self._transition(state, LifecycleState.EXECUTING)
        execution = await self._execute_once(state, classification)
        if execution is not None:
            self._merge_execution_evidence(state, execution)
        await self._transition(state, LifecycleState.EXECUTION_COMPLETED)

        if (
            policy.review
            and execution is not None
            and execution.disposition is not ExecutionDisposition.USER_INPUT_REQUIRED
        ):
            execution, _review_outcome = await self._review_and_remediate(
                state, classification, execution, policy.max_reviews, policy.max_replans
            )

        await self._transition(state, LifecycleState.FINALISING)
        raw_execution = state.last_execution_response
        finalisation: FinalisationOutcome | None = None
        finalisation_error = ""
        finalisation_failure: StageInvocationError | None = None
        execution_evidence = {
            "raw_execution_output": (
                {
                    "text": raw_execution.text,
                    "data": dict(raw_execution.data),
                    "provider": raw_execution.provider,
                    "model": raw_execution.model,
                    "evidence_refs": list(raw_execution.evidence_refs),
                }
                if raw_execution is not None
                else None
            ),
            "parsed_execution_result": (
                _execution_payload(execution)
                if execution is not None and state.last_execution_structure_valid
                else None
            ),
            "execution_json_valid": state.last_execution_structure_valid,
            "execution_stage_error": state.last_execution_error or None,
            "evidence_refs": list(state.evidence_refs),
            "limitations": list(state.limitations),
            "review": (
                {
                    "outcome": state.last_review.outcome.value,
                    "summary": state.last_review.summary,
                    "findings": list(state.last_review.findings),
                }
                if state.last_review
                else None
            ),
        }
        execution_evidence_hash = _payload_hash(execution_evidence)
        finalisation_context = {
            **execution_evidence,
            "execution_invocation_id": state.last_execution_invocation_id or None,
            "execution_evidence_hash": execution_evidence_hash,
        }
        finalisation_context["finalisation_input_hash"] = _payload_hash(
            {
                "goal_ref": state.ledger.goal_ref,
                "classification": (
                    state.ledger.classification.value
                    if state.ledger.classification
                    else None
                ),
                "execution_invocation_id": state.last_execution_invocation_id or None,
                "execution_evidence_hash": execution_evidence_hash,
            }
        )
        try:
            _response, finalisation = await self._invoke_stage(
                state,
                Stage.FINALISATION,
                parse_finalisation,
                allow_tools=False,
                context=finalisation_context,
            )
            assert isinstance(finalisation, FinalisationOutcome)
        except StageInvocationError as exc:
            finalisation_error = str(exc)
            finalisation_failure = exc

        canonical_execution = execution
        if state.last_execution_error:
            canonical_execution = None
            desired_terminal = TerminalState.ERROR
            error = state.last_execution_error
        elif canonical_execution is not None:
            # A valid Execution envelope is the source of truth. Finalisation
            # can render it, but cannot alter its disposition.
            if (
                finalisation is not None
                and finalisation.execution_result is not None
                and finalisation.execution_result.disposition
                is not canonical_execution.disposition
            ):
                self._audit(
                    state,
                    stage=Stage.FINALISATION.value,
                    role="runtime",
                    event="finalisation_disposition_override_ignored",
                    event_id=(
                        f"{state.ledger.turn_id}:finalisation:"
                        "disposition-override-ignored"
                    ),
                    payload={
                        "execution_disposition": canonical_execution.disposition.value,
                        "finalisation_disposition": (
                            finalisation.execution_result.disposition.value
                        ),
                    },
                )
            desired_terminal = terminal_for_execution(
                canonical_execution.disposition,
            )
            error = ""
        else:
            canonical_execution = (
                finalisation.execution_result
                if finalisation is not None and finalisation.execution_result_present
                else None
            )
            if canonical_execution is None:
                desired_terminal = TerminalState.ERROR
                error = "execution_result_unusable"
            else:
                desired_terminal = terminal_for_execution(
                    canonical_execution.disposition,
                )
                error = ""

        if canonical_execution is not None:
            self._merge_execution_evidence(state, canonical_execution)

        if finalisation is None:
            desired_terminal = TerminalState.ERROR
            error = (
                state.last_execution_error
                or (
                    f"[{finalisation_failure.error_code}] "
                    f"{finalisation_failure.human_description}"
                    if finalisation_failure is not None
                    else finalisation_error
                )
                or "finalisation_result_unusable"
            )
            limitation = f"Finalisation failed: {error}"
            if limitation not in state.limitations:
                state.limitations.append(limitation)
            report = (
                "HER v2 could not produce a trustworthy final report. The turn has "
                "been marked ERROR; the Execution record remains available for inspection."
            )
            provenance = "runtime_finalisation_error_fallback"
            detail = "persona_rendering_unavailable=true"
        else:
            report = finalisation.final_message
            provenance = "her_v2_combined_finalisation"
            detail = "persona_rendered_in_finalisation=true"

        terminal_failure = state.last_execution_failure or finalisation_failure
        if desired_terminal is TerminalState.ERROR and terminal_failure is not None:
            state.terminal_failure = terminal_failure
            diagnostic = _technical_error_message_from_failure(
                state.ledger.turn_id,
                terminal_failure,
                foreground_cleanup=state.last_foreground_cleanup,
            )
            if diagnostic not in report:
                report = f"{report.rstrip()}\n\n{diagnostic}"

        await self._settle_late_immediate(
            state,
            reason="final_report_ready_before_immediate_response",
            deliver_if_source_ready=True,
        )
        delivery_kind = (
            "clarification"
            if desired_terminal is TerminalState.PENDING_USER_INPUT
            else "final"
        )
        final_event_id = f"{state.ledger.turn_id}:{delivery_kind}"
        await self._deliver(
            state,
            kind=delivery_kind,
            text=report,
            event_id=final_event_id,
            required=True,
            provenance=provenance,
            detail=detail,
        )
        await self._transition(
            state,
            terminal_lifecycle(desired_terminal),
            terminal_reason=_terminal_reason(desired_terminal),
        )
        self._schedule_meditation(
            state,
            terminal=desired_terminal,
            execution_summary=(
                canonical_execution.summary if canonical_execution is not None else ""
            ),
        )
        return self._result(
            state,
            terminal=desired_terminal,
            text=report,
            error=error,
        )

    async def _execute_once(
        self, state: _TurnState, classification: TriageClassification
    ) -> ExecutionOutcome | None:
        sub_agent_results: tuple[SubAgentResult, ...] = ()
        if classification is TriageClassification.HIGH_VOLUME_TASK:
            sub_agent_results = await self._run_subagents(state)
            for result in sub_agent_results:
                for ref in result.evidence_refs:
                    if ref not in state.evidence_refs:
                        state.evidence_refs.append(ref)
        profile = (
            self.config.profile_for(Stage.EXECUTION)
            if state.execution_capability_escalated
            else self.config.execution_profile_for(classification)
        )
        state.last_execution_response = None
        state.last_execution_structure_valid = False
        state.last_execution_error = ""
        state.last_execution_failure = None
        try:
            _response, execution = await self._invoke_stage(
                state,
                Stage.EXECUTION,
                parse_execution,
                profile=profile,
                allow_tools=True,
                allow_side_effects=not self.config.shadow_mode,
                context={
                    "active_plan": state.active_plan,
                    "sub_agent_results": [
                        {
                            "assignment_id": result.assignment_id,
                            "disposition": result.disposition.value,
                            "summary": result.summary,
                            "evidence_refs": list(result.evidence_refs),
                            "limitations": list(result.limitations),
                        }
                        for result in sub_agent_results
                    ],
                },
                defer_structured_error=True,
            )
        except (TurnStopped, AuditPersistenceError):
            raise
        except StageInvocationError as exc:
            state.last_execution_failure = exc
            state.terminal_failure = exc
            state.last_execution_error = f"[{exc.error_code}] {exc.human_description}"
            return None

        state.last_execution_response = _response
        state.last_execution_structure_valid = isinstance(execution, ExecutionOutcome)
        for ref in _response.evidence_refs:
            if ref not in state.evidence_refs:
                state.evidence_refs.append(ref)
        if execution is None:
            return None
        assert isinstance(execution, ExecutionOutcome)
        if _response.evidence_refs:
            execution = replace(
                execution,
                evidence_refs=tuple(
                    dict.fromkeys((*execution.evidence_refs, *_response.evidence_refs))
                ),
            )
        return execution

    async def _run_subagents(self, state: _TurnState) -> tuple[SubAgentResult, ...]:
        raw_assignments = (
            state.active_plan.get("sub_agents", [])
            if isinstance(state.active_plan, Mapping)
            else []
        )
        if not raw_assignments:
            return ()
        if not isinstance(raw_assignments, list):
            raise StageInvocationError(
                "high-volume plan sub_agents must be a list", retryable=False
            )
        assignments: list[SubAgentAssignment] = []
        seen: set[str] = set()
        for index, raw in enumerate(raw_assignments, start=1):
            if not isinstance(raw, Mapping):
                raise StageInvocationError(
                    f"sub-agent assignment {index} must be an object", retryable=False
                )
            assignment_id = str(raw.get("id") or f"sub-{index}").strip()
            task = str(raw.get("task") or "").strip()
            profile_name = str(raw.get("profile") or "lightweight").strip()
            if not assignment_id or assignment_id in seen or not task:
                raise StageInvocationError(
                    "sub-agent assignments require unique IDs and bounded tasks",
                    retryable=False,
                )
            if (
                profile_name not in self.config.profiles
                or profile_name == self.config.stage_roles.get(Stage.REVIEW)
            ):
                raise StageInvocationError(
                    f"sub-agent assignment {assignment_id!r} selects an unavailable execution profile",
                    retryable=False,
                )
            raw_tools = raw.get("tools") or []
            if not isinstance(raw_tools, list):
                raise StageInvocationError(
                    f"sub-agent assignment {assignment_id!r} tools must be a list",
                    retryable=False,
                )
            requested_side_effects = raw.get("allow_side_effects", False)
            if not isinstance(requested_side_effects, bool):
                raise StageInvocationError(
                    f"sub-agent assignment {assignment_id!r} allow_side_effects must be a boolean",
                    retryable=False,
                )
            assignments.append(
                SubAgentAssignment(
                    assignment_id=assignment_id,
                    task=task,
                    profile=profile_name,
                    tools=tuple(str(item) for item in raw_tools if str(item).strip()),
                    allow_side_effects=requested_side_effects,
                )
            )
            seen.add(assignment_id)
        tasks = [
            asyncio.create_task(self._invoke_subagent(state, assignment))
            for assignment in assignments
        ]
        try:
            results = tuple(await asyncio.gather(*tasks))
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        return results

    async def _invoke_subagent(
        self, state: _TurnState, assignment: SubAgentAssignment
    ) -> SubAgentResult:
        profile = self.config.profile_for_name(assignment.profile)
        role = f"sub_agent:{assignment.assignment_id}"
        side_effects_authorised = (
            assignment.allow_side_effects and not self.config.shadow_mode
        )
        event_prefix = (
            f"{state.ledger.turn_id}:sub-agent:{state.plan_version}:"
            f"{assignment.assignment_id}"
        )
        self._audit(
            state,
            stage=Stage.EXECUTION.value,
            role=role,
            event="sub_agent_assigned",
            event_id=f"{event_prefix}:assigned",
            provider=profile.engine,
            model=profile.model,
            payload={
                "task": assignment.task,
                "profile": assignment.profile,
                "delegated_tools": list(assignment.tools),
                "side_effects_requested": assignment.allow_side_effects,
                "allow_side_effects": side_effects_authorised,
                "shadow_mode": self.config.shadow_mode,
                "can_replan": False,
                "can_finalise": False,
                "can_delegate": False,
            },
        )
        request_context = {
            "assignment_id": assignment.assignment_id,
            "assigned_task": assignment.task,
            "delegated_tools": list(assignment.tools),
            "authority": "bounded_execution_only",
            "may_replan": False,
            "may_contact_user": False,
            "may_finalise": False,
            "may_create_subagents": False,
            "shadow_mode": self.config.shadow_mode,
        }
        try:
            response, outcome = await self._invoke_stage(
                state,
                Stage.EXECUTION,
                parse_execution,
                profile=profile,
                allow_tools=bool(assignment.tools),
                allow_side_effects=side_effects_authorised,
                context=request_context,
                role_override=role,
                publish_commentary=False,
                retry_on_failure=not side_effects_authorised,
            )
            assert isinstance(outcome, ExecutionOutcome)
            if outcome.disposition is ExecutionDisposition.USER_INPUT_REQUIRED:
                outcome = ExecutionOutcome(
                    ExecutionDisposition.FAILED,
                    "Sub-agent requested user-facing authority; request returned as evidence.",
                    outcome.evidence_refs,
                    tuple(item for item in (outcome.clarification,) if item),
                )
            evidence_refs = tuple(
                dict.fromkeys((*outcome.evidence_refs, *response.evidence_refs))
            )
            result = SubAgentResult(
                assignment.assignment_id,
                outcome.disposition,
                outcome.summary,
                evidence_refs,
                outcome.limitations,
            )
            self._audit(
                state,
                stage=Stage.EXECUTION.value,
                role=role,
                event="sub_agent_completed",
                event_id=f"{event_prefix}:completed",
                provider=response.provider or profile.engine,
                model=response.model or profile.model,
                payload={
                    "disposition": result.disposition.value,
                    "summary": result.summary,
                    "evidence_refs": list(result.evidence_refs),
                    "limitations": list(result.limitations),
                },
            )
            return result
        except (TurnStopped, AuditPersistenceError):
            raise
        except Exception as exc:
            failure_payload = (
                exc.audit_payload()
                if isinstance(exc, StageInvocationError)
                else {"error_type": type(exc).__name__, "error": str(exc)}
            )
            self._audit(
                state,
                stage=Stage.EXECUTION.value,
                role=role,
                event="sub_agent_failed",
                event_id=f"{event_prefix}:failed",
                provider=profile.engine,
                model=profile.model,
                payload=failure_payload,
            )
            return SubAgentResult(
                assignment.assignment_id,
                ExecutionDisposition.FAILED,
                f"Sub-agent execution failed: {exc}",
                (),
                ("Sub-agent result unavailable.",),
            )

    async def _perform_replan(
        self,
        state: _TurnState,
        classification: TriageClassification,
        *,
        reason: str,
        reviewer_findings: Sequence[str],
    ) -> None:
        if state.active_plan is None or state.ledger.plan_id is None:
            raise StageInvocationError(
                "Replanning requires an active plan", retryable=False
            )
        await self._transition(state, LifecycleState.REPLANNING)
        _response, plan = await self._invoke_stage(
            state,
            Stage.REPLANNING,
            parse_plan,
            allow_tools=False,
            context={
                "classification": classification.value,
                "active_plan": state.active_plan,
                "reason": reason,
                "reviewer_findings": list(reviewer_findings),
                "execution_evidence_refs": list(state.evidence_refs),
                "habits_included": False,
            },
        )
        state.replan_count += 1
        self._activate_plan(state, plan, replacement=True)
        if (
            classification is TriageClassification.SIMPLE_TASK
            and not state.execution_capability_escalated
        ):
            state.execution_capability_escalated = True
            self._audit(
                state,
                stage=Stage.REPLANNING.value,
                role=self.config.stage_roles[Stage.REPLANNING],
                event="execution_capability_escalated",
                event_id=(
                    f"{state.ledger.turn_id}:replan:{state.replan_count}:"
                    "capability-escalated"
                ),
                payload={
                    "classification": classification.value,
                    "classification_changed": False,
                    "execution_profile": self.config.profile_for(Stage.EXECUTION).name,
                    "reason": reason,
                },
            )

    async def _review_and_remediate(
        self,
        state: _TurnState,
        classification: TriageClassification,
        execution: ExecutionOutcome,
        max_reviews: int,
        max_replans: int,
    ) -> tuple[ExecutionOutcome | None, ReviewOutcome | None]:
        last_outcome: ReviewOutcome | None = None
        remediation_count = 0
        while state.review_count < max_reviews:
            await self._transition(state, LifecycleState.REVIEWING)
            state.review_count += 1
            try:
                _response, finding = await self._invoke_stage(
                    state,
                    Stage.REVIEW,
                    parse_review,
                    allow_tools=False,
                    allow_side_effects=False,
                    context={
                        "classification": classification.value,
                        "active_plan": state.active_plan,
                        "execution_summary": execution.summary,
                        "evidence_refs": list(state.evidence_refs),
                        "reviewer_authority": "advisory_only",
                        "habits_included": False,
                    },
                )
                assert isinstance(finding, ReviewFinding)
            except StageInvocationError as exc:
                state.limitations.append(f"Independent Review unavailable: {exc}")
                return execution, ReviewOutcome.CONDITIONAL_PASS
            state.last_review = finding
            last_outcome = finding.outcome
            if finding.outcome is ReviewOutcome.PASS:
                return execution, last_outcome
            if finding.outcome is ReviewOutcome.CONDITIONAL_PASS:
                state.limitations.extend(finding.findings or (finding.summary,))
                return execution, last_outcome

            state.limitations.extend(finding.findings or (finding.summary,))
            can_remediate = (
                state.active_plan is not None
                and remediation_count < max_reviews
                and state.replan_count < max_replans
            )
            if not can_remediate:
                return execution, last_outcome
            await self._perform_replan(
                state,
                classification,
                reason="independent_review_failed",
                reviewer_findings=finding.findings or (finding.summary,),
            )
            remediation_count += 1
            await self._transition(state, LifecycleState.EXECUTING)
            execution = await self._execute_once(state, classification)
            if execution is None:
                await self._transition(state, LifecycleState.EXECUTION_COMPLETED)
                return None, last_outcome
            self._merge_execution_evidence(state, execution)
            if execution.disposition is ExecutionDisposition.USER_INPUT_REQUIRED:
                return execution, last_outcome
            await self._transition(state, LifecycleState.EXECUTION_COMPLETED)
            if state.effort is Effort.XHIGH:
                return execution, last_outcome
            # MAX may perform another independent Review, bounded above.
        return execution, last_outcome
