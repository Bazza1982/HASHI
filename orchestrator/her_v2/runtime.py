"""Modular, provider-neutral HER v2 stage orchestrator."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping, Sequence

from orchestrator.multimodal_contract import (
    attachment_manifest,
    normalize_request_content,
)

from .audit import AuditPersistenceError, DurableAuditLog
from .commentary import (
    CommentaryPort,
    CommentaryValidationError,
    MAX_NEUTRAL_COMMENTARY_CHARS,
    NeutralCommentary,
    NullCommentaryPort,
)
from .checkpoint import (
    CheckpointInfrastructureInterruption,
    CheckpointSnapshot,
    CompulsoryReplanCoordinator,
    ReplanCompletionInterruption,
    ReplanDirective,
)
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
    StructuredOutputError,
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
    ReplanningOutcome,
    ReviewFinding,
    ReviewOutcome,
    Stage,
    StageResponse,
    SubAgentAssignment,
    SubAgentResult,
    TerminalState,
    TriageClassification,
    TriageDecision,
    ToolEvidenceReceipt,
    TurnResult,
    VerificationFinding,
    VerificationOutcome,
    parse_effort,
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
    parse_replanning,
    parse_triage,
    validate_review_response,
    validate_verification_response,
)

Validator = Callable[[StageResponse], Any]


class _ResumeExecutionAfterReplan(BaseException):
    """A completion-boundary Replan found authorised work still remains."""


@dataclass
class _TurnState:
    request: str
    request_ref: str
    goal: str
    effort: Effort
    ledger: ExecutionLedger
    control: TurnControl
    request_content: Mapping[str, Any] | None = None
    attachment_manifest: tuple[Mapping[str, Any], ...] = ()
    media_routing_by_stage: dict[str, list[Mapping[str, Any]]] = field(
        default_factory=dict
    )
    progress: ProgressTracker = field(default_factory=ProgressTracker)
    deliveries: list[DeliveryRecord] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    tool_receipts: dict[str, ToolEvidenceReceipt] = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)
    active_plan: Mapping[str, Any] | None = None
    plan_edit_history: list[Mapping[str, Any]] = field(default_factory=list)
    plan_version: int = 0
    replan_count: int = 0
    review_count: int = 0
    verification_count: int = 0
    checkpoint_count: int = 0
    execution_cycle_serial: int = 0
    stage_invocation_serial: int = 0
    last_review: ReviewFinding | None = None
    review_remediated: bool = False
    last_verification: VerificationFinding | None = None
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
    execution_elapsed_s: float = 0.0
    replan_continuation: dict[str, Any] = field(default_factory=dict)
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
        checkpoint_clock: Callable[[], float] = time.monotonic,
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
        self.checkpoint_clock = checkpoint_clock
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
        request_content: Mapping[str, Any] | None = None,
    ) -> TurnResult:
        prompt = str(request or "").strip()
        if not prompt:
            raise ValueError("HER v2 requires a non-empty authoritative request")
        effort_value = parse_effort(effort)
        identity = turn_id or f"{request_id}-{uuid.uuid4().hex[:12]}"
        request_ref = f"hashi-request:{request_id}"
        goal_ref = "sha256:" + hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        ledger = ExecutionLedger(identity, request_ref, goal_ref)
        control = TurnControl(identity)
        normalized_request_content = normalize_request_content(request_content)
        manifest = attachment_manifest(normalized_request_content)
        state = _TurnState(
            request=prompt,
            request_ref=request_ref,
            goal=prompt,
            effort=effort_value,
            ledger=ledger,
            control=control,
            request_content=normalized_request_content,
            attachment_manifest=manifest,
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
            payload={
                "request": state.request,
                "effort": state.effort.value,
                "request_content_version": (
                    state.request_content.get("version")
                    if isinstance(state.request_content, Mapping)
                    else None
                ),
                "attachment_manifest": [
                    dict(item) for item in state.attachment_manifest
                ],
            },
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

        if (
            triage.classification is TriageClassification.DIRECT_RESPONSE
            and state.attachment_manifest
        ):
            if immediate_pair is None and immediate_error is None:
                await consume_immediate()

            required_ids = {
                str(item.get("attachment_id") or "")
                for item in state.attachment_manifest
            }

            def native_ids(response: StageResponse | None) -> set[str]:
                if response is None:
                    return set()
                return {
                    str(item.get("attachment_id") or "")
                    for item in response.media_routing
                    if str(item.get("route") or "") == "native"
                }

            immediate_response = (
                immediate_pair[0] if immediate_pair is not None else None
            )
            direct_media_fulfilled = bool(required_ids) and required_ids.issubset(
                native_ids(_triage_response)
            ) and required_ids.issubset(native_ids(immediate_response))
            if not direct_media_fulfilled:
                triage = replace(
                    triage,
                    classification=TriageClassification.SIMPLE_TASK,
                )
                self._audit(
                    state,
                    stage=Stage.TRIAGE.value,
                    role=self.config.stage_roles[Stage.TRIAGE],
                    event="direct_response_media_deferred_to_work",
                    event_id=f"{state.ledger.turn_id}:triage:media-fallback",
                    payload={
                        "required_attachment_ids": sorted(required_ids),
                        "triage_native_attachment_ids": sorted(
                            native_ids(_triage_response)
                        ),
                        "immediate_native_attachment_ids": sorted(
                            native_ids(immediate_response)
                        ),
                        "immediate_error_code": (
                            immediate_error.error_code
                            if immediate_error is not None
                            else None
                        ),
                        "classification_override": TriageClassification.SIMPLE_TASK.value,
                        "reason": "direct_response_media_capability_unfulfilled",
                    },
                )

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
        self._record_triage(
            state,
            triage.classification,
        )
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
            ) = await self._render_required_clarification(
                state,
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
            review_limit=self.config.review_limits[state.effort],
            verification_limit=self.config.verification_limits[state.effort],
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
                state, classification, execution, policy.max_reviews
            )

        if (
            policy.assurance
            and execution is not None
            and execution.disposition is not ExecutionDisposition.USER_INPUT_REQUIRED
        ):
            execution, _verification_outcome = await self._verify_and_remediate(
                state,
                classification,
                execution,
                policy.max_verifications,
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
                    "evidence_refs": list(state.last_review.evidence_refs),
                    "remediation_applied": state.review_remediated,
                }
                if state.last_review
                else None
            ),
            "assurance": (
                {
                    "outcome": state.last_verification.outcome.value,
                    "summary": state.last_verification.summary,
                    "checks": [
                        {
                            "claim": check.claim,
                            "verifiability": check.verifiability.value,
                            "result": check.result.value,
                            "method": check.method,
                            "evidence_refs": list(check.evidence_refs),
                            "observed": check.observed,
                            "required": check.required,
                        }
                        for check in state.last_verification.checks
                    ],
                    "evidence_refs": list(state.last_verification.evidence_refs),
                    "limitations": list(state.last_verification.limitations),
                    "attempts": state.verification_count,
                }
                if state.last_verification
                else None
            ),
            "evidence_receipts": [
                {
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
                for receipt in state.tool_receipts.values()
            ],
            "attachment_manifest": [
                dict(item) for item in state.attachment_manifest
            ],
            "media_routing_by_stage": {
                stage: [dict(entry) for entry in entries]
                for stage, entries in state.media_routing_by_stage.items()
            },
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

    def _replan_boundary_observer(
        self, state: _TurnState, event: str, payload: Mapping[str, Any]
    ) -> None:
        checkpoint_id = str(
            payload.get("checkpoint_id")
            or f"{state.ledger.turn_id}:replan-boundary:unknown"
        )
        ref = self._audit(
            state,
            stage=Stage.REPLANNING.value,
            role="compulsory_replan_cadence",
            event=event,
            event_id=f"{checkpoint_id}:{event}",
            payload=dict(payload),
        )
        try:
            state.ledger.add_log_ref(ref)
            self.ledger_store.save(state.ledger)
        except AuditPersistenceError:
            raise
        except Exception as exc:  # noqa: BLE001 - control persistence is required
            raise AuditPersistenceError(
                "compulsory Replan control ledger persistence failed"
            ) from exc
        if event == "replan_completed":
            state.checkpoint_count += 1

    async def _evaluate_compulsory_replan(
        self,
        state: _TurnState,
        classification: TriageClassification,
        snapshot: CheckpointSnapshot,
    ) -> ReplanDirective:
        state.ledger.assert_classification(classification)
        outcome = await self._perform_replan(
            state,
            classification,
            reason="compulsory_execution_cadence",
            reviewer_findings=(),
            checkpoint_id=snapshot.checkpoint_id,
            cadence_context=snapshot.replan_payload(),
        )
        state.replan_continuation = {
            "checkpoint_id": snapshot.checkpoint_id,
            "trigger_reasons": list(snapshot.trigger_reasons),
            "completion_percent": outcome.completion_percent,
            "completion_basis": outcome.completion_basis,
            "plan_changed": outcome.plan_changed,
            "change_reason": outcome.change_reason or None,
            "next_step": outcome.next_step,
            "completed_receipts": [
                dict(item) for item in snapshot.receipt_summaries
            ],
            "completed_evidence_refs": [
                str(item.get("evidence_ref") or "")
                for item in snapshot.receipt_summaries
                if str(item.get("evidence_ref") or "")
            ],
            "completed_side_effects_must_not_be_replayed": [
                {
                    "evidence_ref": item.get("evidence_ref"),
                    "tool_name": item.get("tool_name"),
                    "status": item.get("status"),
                    "output_sha256": item.get("output_sha256"),
                }
                for item in snapshot.receipt_summaries
                if item.get("completed") and not item.get("read_only")
            ],
        }
        if outcome.completion_percent < 100:
            await self._transition(state, LifecycleState.EXECUTING)
        assert state.ledger.plan_id is not None
        return ReplanDirective(
            checkpoint_id=snapshot.checkpoint_id,
            outcome=outcome,
            active_plan_id=state.ledger.plan_id,
        )

    def _new_replan_coordinator(
        self, state: _TurnState, classification: TriageClassification
    ) -> CompulsoryReplanCoordinator | None:
        state.execution_cycle_serial += 1
        if state.effort not in {Effort.HIGH, Effort.XHIGH, Effort.MAX}:
            return None
        cycle_id = (
            f"{state.ledger.turn_id}:execution-cycle:{state.execution_cycle_serial}"
        )
        return CompulsoryReplanCoordinator(
            cycle_id=cycle_id,
            evaluator=lambda snapshot: self._evaluate_compulsory_replan(
                state, classification, snapshot
            ),
            observer=lambda event, payload: self._replan_boundary_observer(
                state, event, payload
            ),
            clock=self.checkpoint_clock,
        )

    def _execution_from_replan_completion(
        self, state: _TurnState, interruption: ReplanCompletionInterruption
    ) -> ExecutionOutcome:
        self._merge_checkpoint_receipts(state, interruption.receipts)
        replan = interruption.directive.outcome
        evidence_refs = tuple(
            receipt.evidence_ref
            for receipt in interruption.receipts
            if receipt.completed
        )
        outcome = ExecutionOutcome(
            disposition=(
                ExecutionDisposition.COMPLETED_WITH_LIMITATIONS
                if state.limitations
                else ExecutionDisposition.COMPLETED
            ),
            summary=replan.completion_basis,
            evidence_refs=evidence_refs,
            limitations=tuple(dict.fromkeys(state.limitations)),
            remaining_work=(),
        )
        self._audit(
            state,
            stage=Stage.REPLANNING.value,
            role="runtime",
            event="replan_confirmed_execution_complete",
            event_id=f"{interruption.snapshot.checkpoint_id}:execution-complete",
            payload={
                "completion_percent": replan.completion_percent,
                "completion_basis": replan.completion_basis,
                "plan_changed": replan.plan_changed,
                "next_step": replan.next_step,
                "evidence_refs": list(evidence_refs),
                "execution_replayed": False,
                "further_tool_admission": False,
            },
        )
        state.last_execution_response = StageResponse(
            data=_execution_payload(outcome),
            provider="hashi-runtime",
            model="compulsory-replan-control",
            evidence_refs=evidence_refs,
            tool_receipts=tuple(interruption.receipts),
        )
        state.last_execution_structure_valid = True
        state.last_execution_error = ""
        state.last_execution_failure = None
        return outcome

    @staticmethod
    def _merge_checkpoint_receipts(
        state: _TurnState, receipts: Sequence[ToolEvidenceReceipt]
    ) -> None:
        for receipt in receipts:
            state.tool_receipts[receipt.evidence_ref] = receipt
            if receipt.completed and receipt.evidence_ref not in state.evidence_refs:
                state.evidence_refs.append(receipt.evidence_ref)
            cleanup = receipt.details.get("foreground_cleanup")
            if isinstance(cleanup, Mapping):
                state.last_foreground_cleanup = dict(cleanup)

    async def _execute_once(
        self, state: _TurnState, classification: TriageClassification
    ) -> ExecutionOutcome | None:
        started_at = time.monotonic()
        try:
            while True:
                try:
                    return await self._execute_once_untracked(state, classification)
                except _ResumeExecutionAfterReplan:
                    # A provider-completion safe boundary ran the compulsory
                    # Replan and found authorised work remains. The previous
                    # candidate and exact receipts are supplied to this fresh
                    # Execution invocation; completed side effects are not replayed.
                    continue
        finally:
            # Verification time is derived from the real wall-clock cost of every
            # authoritative Execution attempt, including high-volume sub-agents and
            # later remediation.  Accumulating attempts avoids shrinking the budget
            # after a short remediation that followed a long initial execution.
            state.execution_elapsed_s += max(0.0, time.monotonic() - started_at)

    async def _execute_once_untracked(
        self, state: _TurnState, classification: TriageClassification
    ) -> ExecutionOutcome | None:
        replan_coordinator = self._new_replan_coordinator(state, classification)
        sub_agent_results: tuple[SubAgentResult, ...] = ()
        state.last_execution_response = None
        state.last_execution_structure_valid = False
        state.last_execution_error = ""
        state.last_execution_failure = None
        try:
            if classification is TriageClassification.HIGH_VOLUME_TASK:
                sub_agent_results = await self._run_subagents(
                    state, replan_coordinator
                )
                for result in sub_agent_results:
                    for ref in result.evidence_refs:
                        if ref not in state.evidence_refs:
                            state.evidence_refs.append(ref)
            profile = (
                self.config.profile_for(Stage.EXECUTION)
                if state.execution_capability_escalated
                else self.config.execution_profile_for(classification)
            )
            execution_invocation = state.stage_invocation_serial + 1
            execution_context: dict[str, Any] = {
                "active_plan": state.active_plan,
                "sub_agent_results": [
                    {
                        "assignment_id": result.assignment_id,
                        "disposition": result.disposition.value,
                        "summary": result.summary,
                        "evidence_refs": list(result.evidence_refs),
                        "limitations": list(result.limitations),
                        "attachment_ids": list(result.attachment_ids),
                    }
                    for result in sub_agent_results
                ],
            }
            if replan_coordinator is not None:
                execution_context.update(
                    {
                        "replan_continuation": (
                            dict(state.replan_continuation)
                            if state.replan_continuation
                            else None
                        ),
                        "continuation_rules": {
                            "continue_from_current_workspace": True,
                            "preserve_completed_evidence": True,
                            "never_repeat_completed_side_effects_because_of_replanning": True,
                        },
                    }
                )
            _response, execution = await self._invoke_stage(
                state,
                Stage.EXECUTION,
                parse_execution,
                profile=profile,
                allow_tools=True,
                allow_side_effects=not self.config.shadow_mode,
                context=execution_context,
                defer_structured_error=True,
                publish_commentary=replan_coordinator is None,
                checkpoint_coordinator=replan_coordinator,
            )
            state.last_execution_response = _response
            state.last_execution_structure_valid = isinstance(
                execution, ExecutionOutcome
            )
            for ref in _response.evidence_refs:
                if ref not in state.evidence_refs:
                    state.evidence_refs.append(ref)
            if isinstance(execution, ExecutionOutcome) and _response.evidence_refs:
                execution = replace(
                    execution,
                    evidence_refs=tuple(
                        dict.fromkeys(
                            (*execution.evidence_refs, *_response.evidence_refs)
                        )
                    ),
                )

            if replan_coordinator is not None:
                candidate_payload: Mapping[str, Any] = (
                    _execution_payload(execution)
                    if isinstance(execution, ExecutionOutcome)
                    else {
                        "structured": False,
                        "text": _response.text,
                        "data": dict(_response.data),
                        "evidence_refs": list(_response.evidence_refs),
                    }
                )
                directive = await replan_coordinator.at_execution_completion(
                    execution_candidate=candidate_payload,
                )
                if directive is not None:
                    self._audit(
                        state,
                        stage=Stage.EXECUTION.value,
                        role="runtime",
                        event="execution_candidate_superseded_by_replan",
                        event_id=(
                            f"{directive.checkpoint_id}:execution-candidate-superseded"
                        ),
                        payload={
                            "completion_percent": (
                                directive.outcome.completion_percent
                            ),
                            "plan_changed": directive.outcome.plan_changed,
                            "execution_replayed": False,
                            "completed_side_effects_preserved": True,
                        },
                    )
                    raise _ResumeExecutionAfterReplan()

                if _response.validation_source not in {
                    "reasoning_recovery",
                    "deferred_to_finalisation",
                }:
                    await self._publish_stage_commentary(
                        state,
                        response=_response,
                        stage=Stage.EXECUTION,
                        invocation=execution_invocation,
                        attempt=_response.provider_attempt,
                    )
        except ReplanCompletionInterruption as exc:
            return self._execution_from_replan_completion(state, exc)
        except CheckpointInfrastructureInterruption as exc:
            if isinstance(exc.cause, (TurnStopped, AuditPersistenceError)):
                raise exc.cause
            if isinstance(exc.cause, StageInvocationError):
                raise exc.cause
            raise StageInvocationError(
                "compulsory Replan infrastructure failed: "
                f"{type(exc.cause).__name__}: {exc.cause}",
                retryable=False,
                code=ProviderFailureCode.PROVIDER_UNKNOWN,
                human_description=(
                    "The compulsory Replanning control boundary failed "
                    "unexpectedly, so execution stopped."
                ),
            ) from exc.cause
        except (TurnStopped, AuditPersistenceError):
            raise
        except StageInvocationError as exc:
            state.last_execution_failure = exc
            state.terminal_failure = exc
            state.last_execution_error = f"[{exc.error_code}] {exc.human_description}"
            return None
        finally:
            if replan_coordinator is not None:
                self._merge_checkpoint_receipts(
                    state, replan_coordinator.receipts
                )
                await replan_coordinator.close()

        if execution is None:
            return None
        assert isinstance(execution, ExecutionOutcome)
        return execution

    async def _run_subagents(
        self,
        state: _TurnState,
        checkpoint: CompulsoryReplanCoordinator | None,
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
            ordered_available_attachment_ids = tuple(
                str(item.get("attachment_id") or "")
                for item in state.attachment_manifest
                if str(item.get("attachment_id") or "")
            )
            available_attachment_ids = set(ordered_available_attachment_ids)
            unauthorized_attachment_ids: set[str] = set()
            raw_attachment_ids = raw.get("attachment_ids")
            if raw_attachment_ids is None:
                normalized_task = task.casefold()
                compare_all = any(
                    phrase in normalized_task
                    for phrase in (
                        "all images",
                        "every image",
                        "all attachments",
                        "every attachment",
                        "全部图片",
                        "所有图片",
                        "全部附件",
                        "所有附件",
                    )
                )
                selected_attachment_ids = (
                    ordered_available_attachment_ids if compare_all else ()
                )
            else:
                if not isinstance(raw_attachment_ids, list):
                    raise StageInvocationError(
                        f"sub-agent assignment {assignment_id!r} attachment_ids must be a list",
                        retryable=False,
                    )
                requested_attachment_ids = tuple(
                    str(item).strip()
                    for item in raw_attachment_ids
                    if str(item).strip()
                )
                unauthorized_attachment_ids = (
                    set(requested_attachment_ids) - {"*"}
                ) - available_attachment_ids
                if "*" in requested_attachment_ids:
                    selected_attachment_ids = ordered_available_attachment_ids
                else:
                    requested_set = set(requested_attachment_ids)
                    selected_attachment_ids = tuple(
                        attachment_id
                        for attachment_id in ordered_available_attachment_ids
                        if attachment_id in requested_set
                    )
            if unauthorized_attachment_ids:
                raise StageInvocationError(
                    f"sub-agent assignment {assignment_id!r} requests unauthorized attachments",
                    retryable=False,
                    details={
                        "attachment_ids": sorted(unauthorized_attachment_ids)
                    },
                )
            assignments.append(
                SubAgentAssignment(
                    assignment_id=assignment_id,
                    task=task,
                    profile=profile_name,
                    tools=tuple(str(item) for item in raw_tools if str(item).strip()),
                    allow_side_effects=requested_side_effects,
                    attachment_ids=selected_attachment_ids,
                )
            )
            seen.add(assignment_id)
        tasks = [
            asyncio.create_task(
                self._invoke_subagent(state, assignment, checkpoint)
            )
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
        self,
        state: _TurnState,
        assignment: SubAgentAssignment,
        checkpoint: CompulsoryReplanCoordinator | None,
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
                "attachment_ids": list(assignment.attachment_ids),
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
            "authorized_attachment_ids": list(assignment.attachment_ids),
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
                checkpoint_coordinator=checkpoint,
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
                assignment.attachment_ids,
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
                    "attachment_ids": list(result.attachment_ids),
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
                assignment.attachment_ids,
            )

    @staticmethod
    def _replanning_workflow_state_and_evidence(
        state: _TurnState,
        *,
        reason: str,
        reviewer_findings: Sequence[str],
        checkpoint_id: str,
        cadence_context: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        raw_execution = state.last_execution_response
        execution_response = (
            {
                "text": raw_execution.text,
                "data": dict(raw_execution.data),
                "provider": raw_execution.provider,
                "model": raw_execution.model,
                "usage": dict(raw_execution.usage),
                "evidence_refs": list(raw_execution.evidence_refs),
                "validation_source": raw_execution.validation_source or None,
            }
            if raw_execution is not None
            else None
        )
        review = (
            {
                "outcome": state.last_review.outcome.value,
                "summary": state.last_review.summary,
                "findings": list(state.last_review.findings),
                "evidence_refs": list(state.last_review.evidence_refs),
                "remediation_applied": state.review_remediated,
            }
            if state.last_review is not None
            else None
        )
        verification = (
            {
                "outcome": state.last_verification.outcome.value,
                "summary": state.last_verification.summary,
                "checks": [
                    {
                        "claim": check.claim,
                        "verifiability": check.verifiability.value,
                        "result": check.result.value,
                        "method": check.method,
                        "evidence_refs": list(check.evidence_refs),
                        "observed": check.observed,
                        "required": check.required,
                    }
                    for check in state.last_verification.checks
                ],
                "evidence_refs": list(state.last_verification.evidence_refs),
                "limitations": list(state.last_verification.limitations),
            }
            if state.last_verification is not None
            else None
        )
        return {
            "replan_trigger": {
                "reason": reason,
                "checkpoint_id": checkpoint_id,
                "cadence_triggered": bool(checkpoint_id),
                "cadence": dict(cadence_context or {}),
                "reviewer_or_verifier_findings": list(reviewer_findings),
            },
            "ledger": state.ledger.to_dict(),
            "execution": {
                "invocation_id": state.last_execution_invocation_id or None,
                "response": execution_response,
                "structured_output_valid": state.last_execution_structure_valid,
                "structure_error": state.last_execution_error or None,
                "provider_failure": (
                    state.last_execution_failure.audit_payload()
                    if state.last_execution_failure is not None
                    else None
                ),
                "elapsed_s": round(state.execution_elapsed_s, 6),
            },
            "evidence_refs": list(state.evidence_refs),
            "tool_receipts": [
                {
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
                for receipt in sorted(
                    state.tool_receipts.values(), key=lambda item: item.evidence_ref
                )
            ],
            "limitations": list(state.limitations),
            "review": review,
            "verification": verification,
            "foreground_cleanup": dict(state.last_foreground_cleanup) or None,
            "attachment_manifest": [dict(item) for item in state.attachment_manifest],
            "media_routing_by_stage": {
                stage: [dict(entry) for entry in entries]
                for stage, entries in state.media_routing_by_stage.items()
            },
            "workflow_counters": {
                "completed_replans": state.replan_count,
                "reviews": state.review_count,
                "verifications": state.verification_count,
                "checkpoints": state.checkpoint_count,
                "execution_cycles": state.execution_cycle_serial,
            },
        }

    async def _perform_replan(
        self,
        state: _TurnState,
        classification: TriageClassification,
        *,
        reason: str,
        reviewer_findings: Sequence[str],
        checkpoint_id: str = "",
        cadence_context: Mapping[str, Any] | None = None,
    ) -> ReplanningOutcome:
        if state.active_plan is None or state.ledger.plan_id is None:
            raise StageInvocationError(
                "Replanning requires an active plan", retryable=False
            )
        prior_plan = dict(state.active_plan)
        prior_plan_id = state.ledger.plan_id
        logical_replan_id = str(checkpoint_id or "").strip() or (
            f"{state.ledger.turn_id}:replan:{state.replan_count + 1}"
        )

        def validate_replan(response: StageResponse) -> ReplanningOutcome:
            outcome = parse_replanning(response)
            assert isinstance(outcome, ReplanningOutcome)
            previous_semantics = self._semantic_plan(prior_plan)
            replacement_semantics = self._semantic_plan(outcome.plan)
            if not outcome.plan_changed and replacement_semantics != previous_semantics:
                raise StructuredOutputError(
                    "plan_changed=false requires the replacement plan to remain "
                    "semantically unchanged"
                )
            if outcome.plan_changed and replacement_semantics == previous_semantics:
                raise StructuredOutputError(
                    "plan_changed=true requires a materially changed replacement plan"
                )
            if outcome.completion_percent == 100 and (
                outcome.plan_changed or replacement_semantics != previous_semantics
            ):
                raise StructuredOutputError(
                    "completion_percent=100 requires an unchanged active plan and "
                    "plan_changed=false"
                )
            return outcome

        await self._transition(state, LifecycleState.REPLANNING)
        workflow_state_and_evidence = self._replanning_workflow_state_and_evidence(
            state,
            reason=reason,
            reviewer_findings=reviewer_findings,
            checkpoint_id=logical_replan_id if checkpoint_id else "",
            cadence_context=cadence_context,
        )
        response, outcome = await self._invoke_stage(
            state,
            Stage.REPLANNING,
            validate_replan,
            allow_tools=False,
            publish_commentary=False,
            context={
                "active_plan": prior_plan,
                "plan_edit_history": [
                    dict(entry) for entry in state.plan_edit_history
                ],
                "workflow_state_and_evidence": workflow_state_and_evidence,
            },
        )
        assert isinstance(outcome, ReplanningOutcome)
        state.replan_count += 1
        self._activate_plan(state, outcome.plan, replacement=True)
        state.plan_edit_history.append(
            {
                "revision": state.replan_count,
                "trigger": reason,
                "checkpoint_id": logical_replan_id if checkpoint_id else None,
                "previous_plan_id": prior_plan_id,
                "active_plan_id": state.ledger.plan_id,
                "completion_percent": outcome.completion_percent,
                "completion_basis": outcome.completion_basis,
                "plan_changed": outcome.plan_changed,
                "change_reason": outcome.change_reason or None,
                "next_step": outcome.next_step,
                "resulting_plan": dict(outcome.plan),
            }
        )
        await self._publish_mandatory_replan_commentary(
            state,
            outcome=outcome,
            response=response,
            checkpoint_id=logical_replan_id,
        )
        if (
            classification is TriageClassification.SIMPLE_TASK
            and not state.execution_capability_escalated
            and outcome.completion_percent < 100
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
        return outcome

    @staticmethod
    def _semantic_plan(plan: Mapping[str, Any]) -> str:
        semantic = {
            key: plan.get(key)
            for key in ("plan", "success_criteria", "parallel_groups", "sub_agents")
            if key in plan
        }
        try:
            return json.dumps(
                semantic,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
        except (TypeError, ValueError):
            return repr(semantic)

    async def _publish_mandatory_replan_commentary(
        self,
        state: _TurnState,
        *,
        outcome: ReplanningOutcome,
        response: StageResponse,
        checkpoint_id: str,
    ) -> None:
        change_fact = (
            outcome.change_reason[:1_000].strip()
            if outcome.plan_changed
            else "plan is unchanged"
        )
        change_sentence = (
            f"The plan changed because {change_fact}."
            if outcome.plan_changed
            else "The plan is unchanged."
        )
        deterministic_prefix = (
            f"Progress is {outcome.completion_percent}%. {change_sentence} Next: "
        )
        next_fact = outcome.next_step[
            : max(1, MAX_NEUTRAL_COMMENTARY_CHARS - len(deterministic_prefix))
        ].strip()
        deterministic = f"{deterministic_prefix}{next_fact}"
        candidate = str(outcome.commentary or "").strip()
        normalized_candidate = " ".join(candidate.split()).casefold()
        required_fragments = [
            f"{outcome.completion_percent}%".casefold(),
            " ".join(outcome.next_step.split()).casefold(),
        ]
        plan_status_fact = (
            "plan changed" if outcome.plan_changed else "plan is unchanged"
        )
        if outcome.plan_changed:
            required_fragments.append(
                " ".join(outcome.change_reason.split()).casefold()
            )
            has_plan_status_claim = any(
                marker in normalized_candidate
                for marker in (
                    "plan changed",
                    "plan has changed",
                    "plan was changed",
                    "changed the plan",
                    "adjusted the plan",
                    "revised the plan",
                    "计划改变",
                    "计划已改变",
                    "计划有变",
                    "计划已调整",
                    "调整了计划",
                )
            )
        else:
            has_plan_status_claim = any(
                marker in normalized_candidate
                for marker in (
                    "unchanged",
                    "not changed",
                    "did not change",
                    "remains the same",
                    "计划未改变",
                    "计划没有改变",
                    "计划不变",
                    "计划保持不变",
                )
            )
        if not has_plan_status_claim or not candidate or any(
            fragment not in normalized_candidate for fragment in required_fragments
        ):
            candidate = deterministic
            fallback_used = True
        else:
            fallback_used = False

        try:
            protected_facts = [
                f"{outcome.completion_percent}%",
                next_fact,
                plan_status_fact,
            ]
            if outcome.plan_changed:
                protected_facts.append(change_fact)
            commentary = NeutralCommentary(
                event_id=f"{checkpoint_id}:commentary",
                turn_id=state.ledger.turn_id,
                stage=Stage.REPLANNING,
                attempt=max(1, int(response.provider_attempt or 1)),
                text=candidate,
                required_facts=tuple(protected_facts),
                minimal_persona_fallback_reason=(
                    "replan_model_commentary_fallback" if fallback_used else ""
                ),
            )
        except CommentaryValidationError:
            commentary = NeutralCommentary(
                event_id=f"{checkpoint_id}:commentary",
                turn_id=state.ledger.turn_id,
                stage=Stage.REPLANNING,
                attempt=max(1, int(response.provider_attempt or 1)),
                text=deterministic,
                required_facts=tuple(protected_facts),
                minimal_persona_fallback_reason=(
                    "replan_model_commentary_fallback"
                ),
            )
            fallback_used = True

        accepted = False
        error_type = ""
        try:
            accepted = bool(await self.commentary.publish(commentary))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - presentation cannot govern work
            error_type = type(exc).__name__
            self.logger.warning(
                "HER v2 mandatory Replan commentary failed safely at %s: %s",
                checkpoint_id,
                exc,
            )
        self._audit_optional_commentary(
            state,
            event="replan_commentary_publish_result",
            event_id=f"{commentary.event_id}:publish",
            payload={
                "stage": Stage.REPLANNING.value,
                "checkpoint_id": checkpoint_id,
                "accepted": accepted,
                "fallback_used": fallback_used,
                "error_type": error_type or None,
                "completion_percent": outcome.completion_percent,
                "plan_changed": outcome.plan_changed,
                "text_sha256": hashlib.sha256(
                    commentary.text.encode("utf-8")
                ).hexdigest(),
                "text_length": len(commentary.text),
                "workflow_authority": False,
                "exactly_once_identity": commentary.event_id,
            },
        )
        if accepted:
            state.progress.record("commentary", commentary.text)

    async def _review_and_remediate(
        self,
        state: _TurnState,
        classification: TriageClassification,
        execution: ExecutionOutcome,
        max_reviews: int,
    ) -> tuple[ExecutionOutcome | None, ReviewOutcome | None]:
        if max_reviews <= 0:
            return execution, None

        finding = await self._review_once(
            state,
            classification,
            execution,
            review_kind="independent",
        )
        if finding.outcome is ReviewOutcome.PASS:
            return execution, finding.outcome
        if finding.outcome in {
            ReviewOutcome.CONDITIONAL_PASS,
            ReviewOutcome.INCONCLUSIVE,
            ReviewOutcome.UNAVAILABLE,
        }:
            self._merge_assurance_limitations(
                state, finding.findings or (finding.summary,)
            )
            return execution, finding.outcome

        initial_findings = finding.findings or (finding.summary,)
        can_remediate = state.active_plan is not None
        if not can_remediate:
            self._merge_assurance_limitations(state, initial_findings)
            return execution, finding.outcome

        replan = await self._perform_replan(
            state,
            classification,
            reason="independent_review_failed",
            reviewer_findings=initial_findings,
        )
        if replan.completion_percent < 100:
            await self._transition(state, LifecycleState.EXECUTING)
            execution = await self._execute_once(state, classification)
            await self._transition(state, LifecycleState.EXECUTION_COMPLETED)
            if execution is None:
                self._merge_assurance_limitations(state, initial_findings)
                return None, finding.outcome
            self._merge_execution_evidence(state, execution)
            state.review_remediated = True
            if execution.disposition is ExecutionDisposition.USER_INPUT_REQUIRED:
                return execution, finding.outcome
        else:
            # Completion at 100% is authoritative for controlling further work:
            # keep the existing Execution evidence and continue only with the
            # applicable read-only/validation assurance stage.
            await self._transition(state, LifecycleState.EXECUTION_COMPLETED)

        # Reviewed always closes a remediated finding once. Assured proceeds
        # from remediation into its stronger Verification stage instead.
        if state.effort is Effort.MAX:
            return execution, finding.outcome

        closure = await self._review_once(
            state,
            classification,
            execution,
            review_kind="closure",
            findings_to_close=initial_findings,
        )
        if closure.outcome is not ReviewOutcome.PASS:
            self._merge_assurance_limitations(
                state, closure.findings or (closure.summary,)
            )
        return execution, closure.outcome

    async def _review_once(
        self,
        state: _TurnState,
        classification: TriageClassification,
        execution: ExecutionOutcome,
        *,
        review_kind: str,
        findings_to_close: Sequence[str] = (),
    ) -> ReviewFinding:
        await self._transition(state, LifecycleState.REVIEWING)
        state.review_count += 1
        try:
            _response, finding = await self._invoke_stage(
                state,
                Stage.REVIEW,
                validate_review_response,
                allow_tools=True,
                allow_side_effects=False,
                context={
                    "classification": classification.value,
                    "active_plan": state.active_plan,
                    "execution": _execution_payload(execution),
                    "evidence_refs": list(state.evidence_refs),
                    "review_kind": review_kind,
                    "findings_to_close": list(findings_to_close),
                    "reviewer_authority": "advisory_read_only",
                    "delegated_tools": [
                        "workspace_inspect",
                        "file_read",
                        "file_list",
                        "process_list",
                        "media_read",
                    ],
                    "habits_included": False,
                },
            )
            assert isinstance(finding, ReviewFinding)
        except StageInvocationError as exc:
            finding = ReviewFinding(
                outcome=ReviewOutcome.UNAVAILABLE,
                summary=f"Independent Review unavailable: {exc.human_description}",
            )
        state.last_review = finding
        return finding

    async def _verify_and_remediate(
        self,
        state: _TurnState,
        classification: TriageClassification,
        execution: ExecutionOutcome,
        max_verifications: int,
    ) -> tuple[ExecutionOutcome | None, VerificationOutcome | None]:
        last_outcome: VerificationOutcome | None = None
        while state.verification_count < max_verifications:
            finding = await self._verification_once(
                state, classification, execution
            )
            last_outcome = finding.outcome
            if finding.outcome is VerificationOutcome.VERIFIED:
                return execution, last_outcome
            if finding.outcome in {
                VerificationOutcome.NOT_AI_VERIFIABLE,
                VerificationOutcome.UNAVAILABLE,
            }:
                self._merge_assurance_limitations(
                    state, finding.limitations or (finding.summary,)
                )
                return execution, last_outcome
            if finding.outcome is VerificationOutcome.INCONCLUSIVE:
                if state.verification_count < max_verifications:
                    continue
                self._merge_assurance_limitations(
                    state, finding.limitations or (finding.summary,)
                )
                return execution, last_outcome

            failed_checks = tuple(
                check
                for check in finding.checks
                if check.required and check.result is VerificationOutcome.FAILED
            )
            if not failed_checks:
                self._merge_assurance_limitations(
                    state, finding.limitations or (finding.summary,)
                )
                return execution, last_outcome

            can_remediate = (
                state.verification_count < max_verifications
                and state.active_plan is not None
            )
            if not can_remediate:
                self._merge_assurance_limitations(
                    state,
                    finding.limitations
                    or tuple(
                        check.observed or check.claim for check in failed_checks
                    ),
                )
                return execution, last_outcome

            replan = await self._perform_replan(
                state,
                classification,
                reason="assurance_verification_failed",
                reviewer_findings=tuple(
                    check.observed or check.claim for check in failed_checks
                ),
            )
            if replan.completion_percent < 100:
                await self._transition(state, LifecycleState.EXECUTING)
                execution = await self._execute_once(state, classification)
                await self._transition(state, LifecycleState.EXECUTION_COMPLETED)
                if execution is None:
                    self._merge_assurance_limitations(state, (finding.summary,))
                    return None, last_outcome
                self._merge_execution_evidence(state, execution)
                if execution.disposition is ExecutionDisposition.USER_INPUT_REQUIRED:
                    return execution, last_outcome
            else:
                await self._transition(state, LifecycleState.EXECUTION_COMPLETED)
            # The next loop is a fresh Verification invocation against the
            # latest workspace state, whether or not Replanning found more
            # substantive work was still authorised.
        return execution, last_outcome

    async def _verification_once(
        self,
        state: _TurnState,
        classification: TriageClassification,
        execution: ExecutionOutcome,
    ) -> VerificationFinding:
        if state.ledger.status is not LifecycleState.VERIFYING:
            await self._transition(state, LifecycleState.VERIFYING)
        state.verification_count += 1
        try:
            _response, finding = await self._invoke_stage(
                state,
                Stage.VERIFICATION,
                validate_verification_response,
                allow_tools=True,
                allow_side_effects=not self.config.shadow_mode,
                context={
                    "classification": classification.value,
                    "active_plan": state.active_plan,
                    "execution": _execution_payload(execution),
                    "review": (
                        {
                            "outcome": state.last_review.outcome.value,
                            "summary": state.last_review.summary,
                            "findings": list(state.last_review.findings),
                            "evidence_refs": list(state.last_review.evidence_refs),
                            "remediation_applied": state.review_remediated,
                        }
                        if state.last_review is not None
                        else None
                    ),
                    "verification_attempt": state.verification_count,
                    "maximum_verification_attempts": min(
                        3, self.config.verification_limits[state.effort]
                    ),
                    "execution_elapsed_s": round(state.execution_elapsed_s, 6),
                    "verification_run_policy": {
                        "workspace": "authoritative_current_workspace",
                        "workspace_copied": False,
                        "process_authority": "inherited",
                        "environment": "inherited",
                        "network": "inherited",
                        "timeout_basis": "cumulative_execution_elapsed",
                    },
                    "evidence_refs": list(state.evidence_refs),
                    "delegated_tools": [
                        "workspace_inspect",
                        *([] if self.config.shadow_mode else ["verification_run"]),
                        "file_read",
                        "file_list",
                        "process_list",
                        "media_read",
                    ],
                    "verifier_authority": (
                        "advisory_read_only"
                        if self.config.shadow_mode
                        else "advisory_authoritative_workspace_verification"
                    ),
                    "habits_included": False,
                },
            )
            assert isinstance(finding, VerificationFinding)
        except StageInvocationError as exc:
            finding = VerificationFinding(
                outcome=VerificationOutcome.UNAVAILABLE,
                summary=f"Assurance Verification unavailable: {exc.human_description}",
                limitations=("No current tool-backed Verification result is available.",),
            )
        state.last_verification = finding
        return finding

    @staticmethod
    def _merge_assurance_limitations(
        state: _TurnState, limitations: Sequence[str]
    ) -> None:
        for limitation in limitations:
            value = str(limitation or "").strip()
            if value and value not in state.limitations:
                state.limitations.append(value)
