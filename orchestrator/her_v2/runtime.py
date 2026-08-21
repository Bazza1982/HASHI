"""Modular, provider-neutral HER v2 stage orchestrator."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from .audit import AuditPersistenceError, DurableAuditLog
from .commentary import (
    CommentaryPort,
    CommentaryValidationError,
    NullCommentaryPort,
    commentary_from_stage_response,
)
from .config import HERv2Config, ProviderProfile
from .interfaces import (
    DeliveryReceipt,
    DeliveryPort,
    DreamMaintainer,
    HabitAdvisor,
    MeditationRunner,
    NullDreamMaintainer,
    NullHabitAdvisor,
    NullMeditationRunner,
    ReconciliationRequired,
    RecordingDelivery,
    StageInvocationError,
    StageProvider,
    StructuredOutputError,
    TurnControl,
    TurnStopped,
)
from .ledger import ExecutionLedger, LedgerInvariantError, LedgerStore
from .lifecycle import LifecycleViolation
from .models import (
    DeliveryRecord,
    Effort,
    ExecutionDisposition,
    ExecutionOutcome,
    LifecycleState,
    ReviewFinding,
    ReviewOutcome,
    Route,
    Stage,
    StageRequest,
    StageResponse,
    SubAgentAssignment,
    SubAgentResult,
    TerminalState,
    TriageClassification,
    TriageDecision,
    TurnResult,
    WORK_CLASSIFICATIONS,
    terminal_lifecycle,
)
from .policy import replan_eligible, resolve_policy, terminal_for_execution
from .presentation import (
    RenderedRequiredMessage,
    RequiredMessageValidationError,
    RequiredPersonaRenderer,
    RequiredUserMessage,
)
from .progress import ProgressTracker
from .structured import (
    parse_execution,
    parse_immediate,
    parse_plan,
    parse_report,
    parse_review,
    parse_triage,
    resolve_stage_response,
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
    late_immediate_source_task: asyncio.Task | None = None
    late_immediate_delivery_task: asyncio.Task | None = None


class HERv2Runtime:
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
        effort_value = effort if isinstance(effort, Effort) else Effort(str(effort).lower())
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
            return await asyncio.wait_for(
                self._run_turn(state), timeout=self.config.hard_timeout_s
            )
        except asyncio.TimeoutError:
            control.stop("HARD_SAFETY_TIMEOUT")
            return await self._error_result(state, "hard safety timeout exhausted")
        except TurnStopped as exc:
            return await self._stopped_result(state, exc.reason)
        except AuditPersistenceError as exc:
            if self.config.audit_failure_terminal is TerminalState.STOPPED:
                return await self._stopped_result(
                    state, f"AUDIT_PERSISTENCE_FAILURE: {exc}"
                )
            return await self._error_result(state, str(exc))
        except ReconciliationRequired as exc:
            return await self._reconciliation_result(
                state,
                str(exc),
                evidence_refs=exc.evidence_refs,
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
            return await self._error_result(state, str(exc))
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
            await asyncio.gather(
                immediate_task, triage_task, return_exceptions=True
            )
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
            raise StageInvocationError(
                "direct response requires a valid Immediate Response: "
                f"{immediate_error or 'response unavailable'}"
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
                _normalise_text(triage.clarification)
                != _normalise_text(immediate_text)
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
            raise StageInvocationError("Triage returned an unsupported work classification")
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
        if delivery_task.done() or (
            deliver_if_source_ready and source_task.done()
        ):
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
        self._merge_execution_evidence(state, execution)
        while execution.disposition is ExecutionDisposition.REPLAN_REQUIRED:
            if not replan_eligible(classification, policy):
                state.limitations.append(
                    "Execution requested Replanning outside the selected HER policy."
                )
                execution = ExecutionOutcome(
                    ExecutionDisposition.ABANDONED,
                    execution.summary,
                    execution.evidence_refs,
                    tuple(state.limitations),
                )
                break
            if state.replan_count >= policy.max_replans:
                state.limitations.append("Configured Replanning limit was reached.")
                execution = ExecutionOutcome(
                    ExecutionDisposition.ABANDONED,
                    execution.summary,
                    execution.evidence_refs,
                    tuple(state.limitations),
                )
                break
            await self._perform_replan(
                state,
                classification,
                reason=execution.replan_reason,
                reviewer_findings=(),
            )
            await self._transition(state, LifecycleState.EXECUTING)
            execution = await self._execute_once(state, classification)
            self._merge_execution_evidence(state, execution)

        self._merge_execution_evidence(state, execution)
        if execution.disposition is ExecutionDisposition.USER_INPUT_REQUIRED:
            return await self._execution_input_result(state, execution)
        await self._transition(state, LifecycleState.EXECUTION_COMPLETED)

        review_outcome: ReviewOutcome | None = None
        if policy.review:
            execution, review_outcome = await self._review_and_remediate(
                state, classification, execution, policy.max_reviews, policy.max_replans
            )
            if execution.disposition is ExecutionDisposition.USER_INPUT_REQUIRED:
                return await self._execution_input_result(state, execution)

        await self._transition(state, LifecycleState.FINALISING)
        desired_terminal = terminal_for_execution(
            execution.disposition,
            review_outcome=review_outcome,
            material_limitations=bool(state.limitations),
        )
        report = ""
        try:
            _response, report = await self._invoke_stage(
                state,
                Stage.FINALISATION,
                parse_report,
                allow_tools=False,
                attempts=self.config.reporting_attempts,
                context={
                    "required_terminal_state": desired_terminal.value,
                    "execution_summary": execution.summary,
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
                },
            )
            assert isinstance(report, str)
        except StageInvocationError as exc:
            if desired_terminal in {
                TerminalState.COMPLETED,
                TerminalState.COMPLETED_WITH_LIMITATIONS,
            }:
                desired_terminal = TerminalState.COMPLETED_WITH_REPORT_PENDING
                state.limitations.append(str(exc))
            else:
                desired_terminal = TerminalState.ERROR
                state.limitations.append(f"Final reporting failed: {exc}")

        report_provenance = ""
        report_detail = ""
        if report:
            await self._settle_late_immediate(
                state,
                reason="final_report_ready_before_immediate_response",
                deliver_if_source_ready=True,
            )
            report, report_provenance, report_detail = (
                await self._render_required_message(
                    state,
                    kind="final",
                    text=report,
                    event_id=f"{state.ledger.turn_id}:final",
                )
            )
            await self._deliver(
                state,
                kind="final",
                text=report,
                event_id=f"{state.ledger.turn_id}:final",
                required=True,
                provenance=report_provenance,
                detail=report_detail,
            )
        await self._transition(
            state,
            terminal_lifecycle(desired_terminal),
            terminal_reason=_terminal_reason(desired_terminal),
        )
        self._schedule_meditation(
            state,
            terminal=desired_terminal,
            execution_summary=execution.summary,
        )
        return self._result(
            state,
            terminal=desired_terminal,
            text=report,
            error=(
                "report_generation_failed"
                if desired_terminal is TerminalState.COMPLETED_WITH_REPORT_PENDING
                else ""
            ),
        )

    async def _execution_input_result(
        self,
        state: _TurnState,
        execution: ExecutionOutcome,
    ) -> TurnResult:
        """Pause truthfully when execution discovers missing user authority."""

        await self._settle_late_immediate(
            state,
            reason="authoritative_clarification_ready",
            deliver_if_source_ready=True,
        )
        clarification, provenance, detail = await self._render_required_message(
            state,
            kind="clarification",
            text=execution.clarification,
            event_id=f"{state.ledger.turn_id}:execution:clarification",
        )
        await self._deliver(
            state,
            kind="clarification",
            text=clarification,
            event_id=f"{state.ledger.turn_id}:execution:clarification",
            required=True,
            provenance=provenance,
            detail=detail,
        )
        await self._transition(
            state,
            LifecycleState.PENDING_USER_INPUT,
            terminal_reason="execution_user_input_required",
        )
        return self._result(
            state,
            terminal=TerminalState.PENDING_USER_INPUT,
            text=clarification,
        )

    async def _execute_once(
        self, state: _TurnState, classification: TriageClassification
    ) -> ExecutionOutcome:
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
        _response, execution = await self._invoke_stage(
            state,
            Stage.EXECUTION,
            parse_execution,
            profile=profile,
            allow_tools=True,
            allow_side_effects=not self.config.shadow_mode,
            context={
                "classification": classification.value,
                "active_plan": state.active_plan,
                "plan_id": state.ledger.plan_id,
                "authority": "primary_agent",
                "execution_capability_escalated": state.execution_capability_escalated,
                "shadow_mode": self.config.shadow_mode,
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
        )
        assert isinstance(execution, ExecutionOutcome)
        if _response.evidence_refs:
            execution = ExecutionOutcome(
                execution.disposition,
                execution.summary,
                tuple(dict.fromkeys((*execution.evidence_refs, *_response.evidence_refs))),
                execution.limitations,
                execution.replan_reason,
                execution.clarification,
            )
        return execution

    async def _run_subagents(
        self, state: _TurnState
    ) -> tuple[SubAgentResult, ...]:
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
        if len(raw_assignments) > self.config.max_subagents:
            raise StageInvocationError(
                f"high-volume plan exceeds the {self.config.max_subagents} sub-agent limit",
                retryable=False,
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
            if profile_name not in self.config.profiles or profile_name == self.config.stage_roles.get(Stage.REVIEW):
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
        profile = self.config.profiles[assignment.profile]
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
            )
            assert isinstance(outcome, ExecutionOutcome)
            if outcome.disposition in {
                ExecutionDisposition.REPLAN_REQUIRED,
                ExecutionDisposition.USER_INPUT_REQUIRED,
            }:
                outcome = ExecutionOutcome(
                    ExecutionDisposition.FAILED,
                    "Sub-agent requested prohibited orchestration authority; request returned as evidence.",
                    outcome.evidence_refs,
                    tuple(
                        item
                        for item in (outcome.replan_reason, outcome.clarification)
                        if item
                    ),
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
        except (TurnStopped, AuditPersistenceError, ReconciliationRequired):
            raise
        except Exception as exc:
            self._audit(
                state,
                stage=Stage.EXECUTION.value,
                role=role,
                event="sub_agent_failed",
                event_id=f"{event_prefix}:failed",
                provider=profile.engine,
                model=profile.model,
                payload={"error_type": type(exc).__name__, "error": str(exc)},
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
            raise StageInvocationError("Replanning requires an active plan", retryable=False)
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
                    "execution_profile": self.config.profile_for(
                        Stage.EXECUTION
                    ).name,
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
    ) -> tuple[ExecutionOutcome, ReviewOutcome | None]:
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
            self._merge_execution_evidence(state, execution)
            if execution.disposition is ExecutionDisposition.USER_INPUT_REQUIRED:
                return execution, last_outcome
            await self._transition(state, LifecycleState.EXECUTION_COMPLETED)
            if state.effort is Effort.XHIGH:
                return execution, last_outcome
            # MAX may perform another independent Review, bounded above.
        return execution, last_outcome

    async def _invoke_stage(
        self,
        state: _TurnState,
        stage: Stage,
        validator: Validator,
        *,
        profile: ProviderProfile | None = None,
        allow_tools: bool,
        allow_side_effects: bool = False,
        attempts: int | None = None,
        context: Mapping[str, Any] | None = None,
        role_override: str | None = None,
        publish_commentary: bool = True,
    ) -> tuple[StageResponse, Any]:
        selected = profile or self.config.profile_for(stage)
        role = role_override or (
            selected.name if profile is not None else self.config.stage_roles.get(
                stage, selected.name
            )
        )
        # A model invocation that may already have performed an external side
        # effect is never automatically replayed.  Provider adapters may make
        # their own provably pre-side-effect transport retry, but orchestration
        # cannot safely assume an unknown execution attempt was side-effect-free.
        limit = (
            1
            if allow_side_effects
            else int(
                attempts
                or max(selected.max_attempts, self.config.structured_repair_attempts)
            )
        )
        last_error: Exception | None = None
        structure_retry_feedback: Mapping[str, Any] | None = None
        state.stage_invocation_serial += 1
        invocation_serial = state.stage_invocation_serial
        for attempt in range(1, limit + 1):
            if state.control.stopped:
                raise TurnStopped(state.control.reason)
            attempt_context = dict(context or {})
            if structure_retry_feedback is not None:
                attempt_context["previous_structure_error"] = dict(
                    structure_retry_feedback
                )
                attempt_context["retry_instruction"] = (
                    "Correct only the reported response-envelope defect. Preserve "
                    "the authoritative goal, classification, evidence, and uncertainty."
                )
            request = StageRequest(
                turn_id=state.ledger.turn_id,
                request_ref=state.request_ref,
                stage=stage,
                role=role,
                attempt=attempt,
                goal=state.goal,
                classification=state.ledger.classification,
                effort=state.effort,
                plan_id=state.ledger.plan_id,
                context=attempt_context,
                allow_tools=allow_tools,
                allow_side_effects=allow_side_effects,
                progress_callback=self._progress_callback(state),
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
                    "context": attempt_context,
                },
            )
            state.ledger.add_log_ref(start_ref)
            self.ledger_store.save(state.ledger)
            state.progress.record(
                "stage_started", f"{stage.value}:{attempt}"
            )
            response: StageResponse | None = None
            try:
                response = await self._await_provider_operation(
                    state,
                    self.provider.invoke(selected, request),
                    stage=stage,
                    timeout_s=selected.timeout_s,
                )
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
                        "reasoning_available": bool(
                            response.reasoning_trace
                            and str(response.reasoning_trace).strip()
                        ),
                        "validation_pending": True,
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
                    if not allow_side_effects:
                        raise
                    try:
                        repaired_response, parsed = await self._repair_structure(
                            state,
                            target_stage=stage,
                            validator=validator,
                            profile=selected,
                            role=role,
                            original_attempt=attempt,
                            original_invocation=invocation_serial,
                            original_response=response,
                            validation_error=exc,
                        )
                    except (TurnStopped, AuditPersistenceError):
                        raise
                    except StageInvocationError as repair_error:
                        self._audit(
                            state,
                            stage=stage.value,
                            role=role,
                            event="stage_attempt_failed",
                            event_id=f"{attempt_prefix}:failed",
                            provider=response.provider or selected.engine,
                            model=response.model or selected.model,
                            attempt=attempt,
                            payload={
                                "error_type": type(exc).__name__,
                                "error": str(exc),
                                "will_retry": False,
                                "provider_response_received": True,
                                "structure_repair_attempted": True,
                                "structure_repair_error": str(repair_error),
                                "reconciliation_required": True,
                            },
                        )
                        raise ReconciliationRequired(
                            f"{stage.value} returned an invalid result after possible "
                            "side effects, and tool-free structure repair was exhausted; "
                            "execution outcome requires reconciliation and was not replayed",
                            evidence_refs=response.evidence_refs,
                        ) from repair_error
                    effective_response = StageResponse(
                        text=repaired_response.text,
                        data=repaired_response.data,
                        reasoning_trace=response.reasoning_trace,
                        provider=response.provider or selected.engine,
                        model=response.model or selected.model,
                        usage=response.usage,
                        evidence_refs=response.evidence_refs,
                    )
                    validation_source = "tool_free_structure_repair"

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
                        "reasoning_available": bool(
                            effective_response.reasoning_trace
                        ),
                        "validation_source": validation_source,
                    },
                )
                state.ledger.add_log_ref(complete_ref)
                self.ledger_store.save(state.ledger)
                if publish_commentary and validation_source not in {
                    "reasoning_recovery",
                    "tool_free_structure_repair",
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
            except ReconciliationRequired:
                raise
            except asyncio.TimeoutError:
                last_error = StageInvocationError(
                    f"{stage.value} timed out after {selected.timeout_s:g}s"
                )
            except (StructuredOutputError, StageInvocationError) as exc:
                last_error = exc
                if isinstance(exc, StructuredOutputError):
                    structure_retry_feedback = {
                        "attempt": attempt,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                if isinstance(exc, StageInvocationError) and not exc.retryable:
                    limit = attempt
            except Exception as exc:
                last_error = StageInvocationError(
                    f"{stage.value} provider failure: {type(exc).__name__}: {exc}"
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
                        "error_type": type(last_error).__name__,
                        "error": str(last_error),
                        "will_retry": attempt < limit,
                        "provider_response_received": response is not None,
                    },
                )
            except AuditPersistenceError:
                raise
            if attempt >= limit:
                break
        raise StageInvocationError(
            f"{stage.value} exhausted {limit} attempt(s): {last_error}"
        )

    async def _repair_structure(
        self,
        state: _TurnState,
        *,
        target_stage: Stage,
        validator: Validator,
        profile: ProviderProfile,
        role: str,
        original_attempt: int,
        original_invocation: int,
        original_response: StageResponse,
        validation_error: StructuredOutputError,
    ) -> tuple[StageResponse, Any]:
        """Repair only the response envelope; never re-enter side-effect execution."""

        repair_key = (
            f"{state.ledger.turn_id}:{target_stage.value}:invocation:"
            f"{original_invocation}:attempt:{original_attempt}:structure-repair"
        )
        self._audit(
            state,
            stage=target_stage.value,
            role=role,
            event="structure_repair_requested",
            event_id=f"{repair_key}:requested",
            provider=original_response.provider or profile.engine,
            model=original_response.model or profile.model,
            attempt=original_attempt,
            payload={
                "repair_target_stage": target_stage.value,
                "validation_error": str(validation_error),
                "tools_authorised": False,
                "side_effects_authorised": False,
                "original_execution_replayed": False,
                "original_evidence_refs": list(original_response.evidence_refs),
            },
        )
        repair_context = {
            "repair_target_stage": target_stage.value,
            "repair_of_role": role,
            "repair_of_attempt": original_attempt,
            "repair_of_invocation": original_invocation,
            "validation_error": str(validation_error),
            "original_provider_response": {
                "text": original_response.text,
                "data": dict(original_response.data),
                "provider": original_response.provider or profile.engine,
                "model": original_response.model or profile.model,
                "evidence_refs": list(original_response.evidence_refs),
            },
            "repair_authority": "structure_only",
            "original_execution_must_not_be_replayed": True,
        }
        repair_profile = self.config.profile_for_route(
            Route.STRUCTURE_REPAIR,
            base_profile=profile,
        )
        try:
            repaired_response, parsed = await self._invoke_stage(
                state,
                Stage.STRUCTURE_REPAIR,
                validator,
                profile=repair_profile,
                allow_tools=False,
                allow_side_effects=False,
                attempts=self.config.structured_repair_attempts,
                context=repair_context,
                role_override=f"{role}:structure_repair",
                publish_commentary=False,
            )
        except (TurnStopped, AuditPersistenceError):
            raise
        except StageInvocationError as exc:
            self._audit(
                state,
                stage=target_stage.value,
                role=role,
                event="structure_repair_failed",
                event_id=f"{repair_key}:failed",
                provider=repair_profile.engine,
                model=repair_profile.model,
                attempt=original_attempt,
                payload={
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "original_execution_replayed": False,
                },
            )
            raise
        self._audit(
            state,
            stage=target_stage.value,
            role=role,
            event="structure_repair_completed",
            event_id=f"{repair_key}:completed",
            provider=repaired_response.provider or repair_profile.engine,
            model=repaired_response.model or repair_profile.model,
            attempt=original_attempt,
            payload={
                "repair_target_stage": target_stage.value,
                "original_execution_replayed": False,
                "evidence_refs_preserved": list(original_response.evidence_refs),
            },
        )
        return repaired_response, parsed

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

    async def _await_provider_operation(
        self,
        state: _TurnState,
        operation,
        *,
        stage: Stage,
        timeout_s: float,
    ) -> StageResponse:
        task = asyncio.create_task(state.control.run_cancellable(operation))
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            while not task.done():
                stage_remaining = float(timeout_s) - (loop.time() - started)
                idle_remaining = (
                    self.config.user_idle_timeout_s - state.progress.idle_for()
                )
                if stage_remaining <= 0:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    raise asyncio.TimeoutError(
                        f"{stage.value} stage timeout exhausted"
                    )
                if idle_remaining <= 0:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    raise StageInvocationError(
                        f"{stage.value} exceeded the user idle-progress timeout",
                        retryable=False,
                    )
                try:
                    await asyncio.wait_for(
                        asyncio.shield(task),
                        timeout=min(stage_remaining, idle_remaining, 0.25),
                    )
                except asyncio.TimeoutError:
                    continue
            return await task
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise

    def _record_triage(
        self, state: _TurnState, classification: TriageClassification
    ) -> None:
        ref = self._audit(
            state,
            stage=Stage.TRIAGE.value,
            role=self.config.stage_roles[Stage.TRIAGE],
            event="classification_recorded",
            event_id=f"{state.ledger.turn_id}:classification",
            payload={"classification": classification.value},
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
                f"{state.replan_count}:{state.review_count}"
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

    async def _render_required_message(
        self,
        state: _TurnState,
        *,
        kind: str,
        text: str,
        event_id: str,
    ) -> tuple[str, str, str]:
        """Render Persona without granting presentation workflow authority."""

        message = RequiredUserMessage(
            event_id=event_id,
            turn_id=state.ledger.turn_id,
            kind=kind,
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
                    "kind": kind,
                    "reason": "renderer_unavailable",
                    "source_text_sha256": hashlib.sha256(
                        message.text.encode("utf-8")
                    ).hexdigest(),
                    "workflow_authority_changed": False,
                },
            )
            return message.text, "unrendered_required_message", (
                "persona_rendering_fallback=true; "
                "error_type=renderer_unavailable"
            )

        self._audit(
            state,
            stage="persona_presentation",
            role="required_message_renderer",
            event="required_persona_render_started",
            event_id=f"{event_id}:persona:start",
            payload={
                "kind": kind,
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
                "kind": kind,
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
            raise ValueError(
                "raw commentary cannot use the workflow delivery boundary"
            )
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
            if kind in {"acknowledgement", "clarification", "final"}:
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
                        "acknowledgement"
                        if resolution == "commentary"
                        else resolution
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

    async def _error_result(self, state: _TurnState, error: str) -> TurnResult:
        if not state.ledger.is_terminal:
            try:
                await self._transition(
                    state,
                    LifecycleState.ERROR,
                    terminal_reason=error,
                )
            except (AuditPersistenceError, LifecycleViolation):
                if not state.ledger.is_terminal:
                    state.ledger.transition(
                        LifecycleState.ERROR, terminal_reason=error
                    )
                self.ledger_store.save(state.ledger)
        return self._result(
            state,
            terminal=TerminalState.ERROR,
            text="",
            error=error,
        )

    async def _reconciliation_result(
        self,
        state: _TurnState,
        error: str,
        *,
        evidence_refs: Sequence[str] = (),
    ) -> TurnResult:
        for ref in evidence_refs:
            value = str(ref or "").strip()
            if value and value not in state.evidence_refs:
                state.evidence_refs.append(value)
        limitation = (
            "Execution may have changed external state, but its result could not be "
            "validated. HASHI did not replay the execution; operator reconciliation "
            "is required before retrying or claiming completion."
        )
        if limitation not in state.limitations:
            state.limitations.append(limitation)
        if not state.ledger.is_terminal:
            try:
                ref = self._audit(
                    state,
                    stage=Stage.EXECUTION.value,
                    role="runtime",
                    event="execution_reconciliation_required",
                    event_id=f"{state.ledger.turn_id}:execution:reconciliation-required",
                    payload={
                        "reason": error,
                        "evidence_refs": list(state.evidence_refs),
                        "execution_replayed": False,
                        "automatic_retry_permitted": False,
                    },
                )
                state.ledger.add_log_ref(ref)
                await self._transition(
                    state,
                    LifecycleState.RECONCILIATION_REQUIRED,
                    terminal_reason="execution_outcome_unconfirmed",
                )
            except AuditPersistenceError:
                state.ledger.transition(
                    LifecycleState.RECONCILIATION_REQUIRED,
                    terminal_reason="execution_outcome_unconfirmed",
                )
                self.ledger_store.save(state.ledger)
        return self._result(
            state,
            terminal=TerminalState.RECONCILIATION_REQUIRED,
            text="",
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
            or terminal not in {
                TerminalState.COMPLETED,
                TerminalState.COMPLETED_WITH_LIMITATIONS,
                TerminalState.COMPLETED_WITH_REPORT_PENDING,
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
            final_was_immediate=final_was_immediate,
            final_already_delivered=final_already_delivered,
            delivery_id=state.delivery_id,
            delivery_kind=state.delivery_kind,
            delivery_event_id=state.delivery_event_id,
            review_count=state.review_count,
            replan_count=state.replan_count,
        )


def _normalise_text(value: str) -> str:
    return " ".join(str(value or "").casefold().split()).rstrip(".?!。？！")


def _terminal_reason(state: TerminalState) -> str:
    return {
        TerminalState.COMPLETED: "goal_achieved",
        TerminalState.COMPLETED_WITH_LIMITATIONS: "completed_with_disclosed_limitations",
        TerminalState.COMPLETED_WITH_REPORT_PENDING: "report_generation_exhausted",
        TerminalState.RECONCILIATION_REQUIRED: "execution_outcome_unconfirmed",
        TerminalState.FAILED: "goal_not_achieved",
        TerminalState.ERROR: "technical_failure",
        TerminalState.ABANDONED: "continuation_not_justified",
        TerminalState.STOPPED: "authorised_stop",
        TerminalState.PENDING_USER_INPUT: "user_input_required",
    }[state]
