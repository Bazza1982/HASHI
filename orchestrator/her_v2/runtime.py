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
from .checkpoint import (
    CheckpointInfrastructureInterruption,
    CheckpointSnapshot,
    CompulsoryReplanCoordinator,
    ReplanCompletionInterruption,
    ReplanDirective,
)
from .commentary import (
    MAX_NEUTRAL_COMMENTARY_CHARS,
    CommentaryPort,
    CommentaryValidationError,
    NeutralCommentary,
    NullCommentaryPort,
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
    ToolEvidenceReceipt,
    TriageClassification,
    TriageDecision,
    TurnResult,
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
    parse_direct_message,
    parse_execution,
    parse_execution_message,
    parse_finalisation,
    parse_immediate,
    parse_plan,
    parse_replanning,
    parse_triage,
    validate_review_response,
)

Validator = Callable[[StageResponse], Any]


class _ResumeExecutionAfterReplan(BaseException):
    """A completion-boundary Replan found authorised work still remains."""


def _subagent_result_payload(result: SubAgentResult) -> dict[str, Any]:
    return {
        "plan_id": result.plan_id,
        "assignment_id": result.assignment_id,
        "disposition": result.disposition.value,
        "summary": result.summary,
        "evidence_refs": list(result.evidence_refs),
        "limitations": list(result.limitations),
        "attachment_ids": list(result.attachment_ids),
        "source_plan_id": result.source_plan_id or result.plan_id,
        "reused": result.reused,
    }


def _tool_catalogue_name(entry: Mapping[str, Any]) -> str:
    function = entry.get("function")
    return str(
        function.get("name") if isinstance(function, Mapping) else ""
    ).strip()


@dataclass
class _SubAgentBatch:
    """One delegation batch frozen to one authoritative plan snapshot."""

    plan_id: str
    plan_version: int
    assignments: tuple[SubAgentAssignment, ...]
    parallel_groups: tuple[tuple[str, ...], ...]
    predecessor_plan_id: str = ""
    results: dict[str, SubAgentResult] = field(default_factory=dict)
    running: set[str] = field(default_factory=set)
    cancelled: set[str] = field(default_factory=set)

    def as_payload(self) -> dict[str, Any]:
        statuses: list[dict[str, Any]] = []
        for assignment in self.assignments:
            result = self.results.get(assignment.assignment_id)
            if result is not None:
                status = result.disposition.value
            elif assignment.assignment_id in self.running:
                status = "RUNNING"
            elif assignment.assignment_id in self.cancelled:
                status = "CANCELLED"
            else:
                status = "PENDING"
            statuses.append(
                {
                    "assignment_id": assignment.assignment_id,
                    "status": status,
                }
            )
        return {
            "plan_id": self.plan_id,
            "plan_version": self.plan_version,
            "predecessor_plan_id": self.predecessor_plan_id or None,
            "parallel_groups": [list(group) for group in self.parallel_groups],
            "assignments": [
                {
                    **dict(assignment.definition),
                    "id": assignment.assignment_id,
                    "task": assignment.task,
                    "profile": assignment.profile,
                    "tools": list(assignment.tools),
                    "attachment_ids": list(assignment.attachment_ids),
                    "allow_side_effects": assignment.allow_side_effects,
                }
                for assignment in self.assignments
            ],
            "assignment_statuses": statuses,
            "results": [
                _subagent_result_payload(self.results[assignment.assignment_id])
                for assignment in self.assignments
                if assignment.assignment_id in self.results
            ],
        }


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
    previous_plan_id: str = ""
    sub_agent_batches: dict[str, _SubAgentBatch] = field(default_factory=dict)
    replan_count: int = 0
    review_count: int = 0
    verification_count: int = 0
    checkpoint_count: int = 0
    execution_cycle_serial: int = 0
    stage_invocation_serial: int = 0
    last_review: ReviewFinding | None = None
    review_remediated: bool = False
    review_resolved_by_replan: bool = False
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
    execution_completed_by_replan: bool = False
    execution_draft_serial: int = 0
    execution_draft_event_id: str = ""
    execution_draft_text: str = ""
    execution_draft_delivered: bool = False


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
        skills_catalogue: Sequence[Mapping[str, Any]] | None = None,
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
        self.skills_catalogue = tuple(
            dict(item)
            for item in (skills_catalogue or ())
            if isinstance(item, Mapping)
        )
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

        if state.effort is Effort.ZERO:
            return await self._run_direct(state)

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
            direct_media_fulfilled = (
                bool(required_ids)
                and required_ids.issubset(native_ids(_triage_response))
                and required_ids.issubset(native_ids(immediate_response))
            )
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

    async def _run_direct(self, state: _TurnState) -> TurnResult:
        """Run the zero-orchestration path as one fully capable agent call."""

        habits: Sequence[str] = ()
        if self.config.meditation_enabled:
            with suppress(Exception):
                habits = await self.habits.retrieve(
                    goal=state.goal,
                    turn_id=state.ledger.turn_id,
                )

        response, direct_text = await self._invoke_stage(
            state,
            Stage.DIRECT,
            parse_direct_message,
            allow_tools=True,
            allow_side_effects=not self.config.shadow_mode,
            context={
                "habit_catalogue": list(habits),
                "habits_are_advisory": True,
                "skills_catalogue": [dict(item) for item in self.skills_catalogue],
                "zero_orchestration": True,
                "automatic_effort_upgrade_allowed": False,
                "sub_agent_delegation_allowed": False,
            },
            publish_commentary=False,
        )
        assert isinstance(direct_text, str)
        for evidence_ref in response.evidence_refs:
            if evidence_ref not in state.evidence_refs:
                state.evidence_refs.append(evidence_ref)

        await self._deliver(
            state,
            kind="final",
            text=direct_text,
            event_id=f"{state.ledger.turn_id}:final",
            required=True,
            provenance="zero_orchestration_direct",
            detail=(
                "single_direct_invocation=true; orchestration_upgrade=false; "
                "finalisation_invoked=false"
            ),
        )
        await self._transition(
            state,
            LifecycleState.COMPLETED,
            terminal_reason="direct_response_delivered",
        )
        return self._result(
            state,
            terminal=TerminalState.COMPLETED,
            text=direct_text,
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

    def _execution_tool_catalogue(self) -> tuple[Mapping[str, Any], ...]:
        resolver = getattr(self.provider, "tool_catalogue", None)
        if not callable(resolver):
            return ()
        try:
            raw_catalogue = resolver(
                allow_side_effects=not self.config.shadow_mode,
                delegated_tools=None,
            )
        except Exception as exc:
            self.logger.warning(
                "HER v2 could not render the advisory execution tool catalogue: %s",
                exc,
            )
            return ()
        if not isinstance(raw_catalogue, Sequence) or isinstance(
            raw_catalogue, (str, bytes)
        ):
            return ()
        catalogue: list[Mapping[str, Any]] = []
        seen: set[str] = set()
        for raw in raw_catalogue:
            if not isinstance(raw, Mapping):
                continue
            entry = dict(raw)
            name = _tool_catalogue_name(entry)
            if not name or name in seen:
                continue
            entry["hashi_read_only"] = entry.get("hashi_read_only") is True
            seen.add(name)
            catalogue.append(entry)
        return tuple(catalogue)

    async def _run_work(
        self, state: _TurnState, classification: TriageClassification
    ) -> TurnResult:
        state.ledger.assert_classification(classification)
        policy = resolve_policy(
            state.effort,
            review_limit=self.config.review_limits[state.effort],
        )
        if policy.planning:
            execution_tool_catalogue = self._execution_tool_catalogue()

            def validate_plan(response: StageResponse) -> Mapping[str, Any]:
                plan = parse_plan(response)
                if (
                    classification is not TriageClassification.HIGH_VOLUME_TASK
                    and plan.get("sub_agents")
                ):
                    raise StructuredOutputError(
                        "Sub-agent assignments are permitted only for HIGH_VOLUME_TASK"
                    )
                return plan

            planning_context: dict[str, Any] = {
                "classification": classification.value,
                "available_execution_tools": [
                    dict(item) for item in execution_tool_catalogue
                ],
                "execution_allow_side_effects": not self.config.shadow_mode,
            }
            if classification is TriageClassification.HIGH_VOLUME_TASK:
                planning_context["available_sub_agent_profiles"] = list(
                    self.config.sub_agent_execution_profile_names()
                )
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
                validate_plan,
                allow_tools=False,
                context=planning_context,
            )
            await self._transition(state, LifecycleState.PLANNED)
            self._activate_plan(state, plan, replacement=False)

        await self._transition(state, LifecycleState.EXECUTING)
        execution = await self._execute_once(state, classification)
        if isinstance(execution, ExecutionOutcome):
            self._merge_execution_evidence(state, execution)
        await self._transition(state, LifecycleState.EXECUTION_COMPLETED)

        if execution is None:
            failure = state.last_execution_failure
            if failure is not None:
                report = _technical_error_message_from_failure(
                    state.ledger.turn_id,
                    failure,
                    foreground_cleanup=state.last_foreground_cleanup,
                )
                report = f"{report}\n\nExact error:\n{failure}"
                error = state.last_execution_error or str(failure)
            else:
                error = state.last_execution_error or "execution_empty_response"
                report = (
                    "⚠️ Execution ended without a response.\n\n"
                    f"Exact error:\n{error}\n\n"
                    f"Reference: `{state.ledger.turn_id}`"
                )
            await self._settle_late_immediate(
                state,
                reason="execution_technical_error_ready",
                deliver_if_source_ready=True,
            )
            await self._deliver(
                state,
                kind="final",
                text=report,
                event_id=f"{state.ledger.turn_id}:final",
                required=True,
                provenance="runtime_execution_error",
                detail="deterministic_technical_error=true; exact_error_included=true",
            )
            await self._transition(
                state,
                LifecycleState.ERROR,
                terminal_reason="technical_failure",
            )
            return self._result(
                state,
                terminal=TerminalState.ERROR,
                text=report,
                error=error,
            )

        if (
            isinstance(execution, str)
            and state.effort in {Effort.LOW, Effort.MEDIUM, Effort.HIGH}
            and not state.execution_completed_by_replan
        ):
            await self._settle_late_immediate(
                state,
                reason="execution_response_ready",
                deliver_if_source_ready=True,
            )
            await self._deliver(
                state,
                kind="final",
                text=execution,
                event_id=f"{state.ledger.turn_id}:final",
                required=True,
                provenance="primary_execution_natural_language",
                detail="execution_workflow_completed=true; finalisation_invoked=false",
            )
            await self._transition(
                state,
                LifecycleState.COMPLETED,
                terminal_reason="execution_response_delivered",
            )
            self._schedule_meditation(
                state,
                terminal=TerminalState.COMPLETED,
                execution_summary=execution,
            )
            return self._result(
                state,
                terminal=TerminalState.COMPLETED,
                text=execution,
            )

        if isinstance(execution, str) and state.effort in {Effort.XHIGH, Effort.MAX}:
            await self._advance_execution_draft_commentary(
                state,
                response=execution,
            )

        if policy.review and execution is not None:
            execution, _review_outcome = await self._review_and_remediate(
                state, classification, execution, policy.max_reviews
            )

        await self._transition(state, LifecycleState.FINALISING)
        raw_execution = state.last_execution_response
        finalisation: FinalisationOutcome | None = None
        finalisation_error = ""
        finalisation_failure: StageInvocationError | None = None
        reviewer_findings = (
            {
                "status": state.last_review.outcome.value,
                "reason": state.last_review.summary,
                "conditions": (
                    "\n".join(state.last_review.findings)
                    if state.last_review.findings
                    else None
                ),
                "remediation_applied": state.review_remediated,
            }
            if state.last_review
            else None
        )
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
                if isinstance(execution, ExecutionOutcome)
                and state.last_execution_structure_valid
                else None
            ),
            "draft_response": execution if isinstance(execution, str) else None,
            "execution_json_valid": state.last_execution_structure_valid,
            "execution_stage_error": state.last_execution_error or None,
            "execution_completed_by_replan": state.execution_completed_by_replan,
            "active_plan": (
                dict(state.active_plan) if state.active_plan is not None else None
            ),
            "delegated_execution": (
                state.sub_agent_batches[str(state.ledger.plan_id)].as_payload()
                if str(state.ledger.plan_id) in state.sub_agent_batches
                else None
            ),
            "plan_edit_history": [dict(entry) for entry in state.plan_edit_history],
            "evidence_refs": list(state.evidence_refs),
            "limitations": list(state.limitations),
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
            "attachment_manifest": [dict(item) for item in state.attachment_manifest],
            "media_routing_by_stage": {
                stage: [dict(entry) for entry in entries]
                for stage, entries in state.media_routing_by_stage.items()
            },
        }
        model_completion_evidence = {
            key: value
            for key, value in execution_evidence.items()
            if key not in {"raw_execution_output", "draft_response"}
        }
        if raw_execution is not None:
            model_completion_evidence["execution_record"] = {
                "data": dict(raw_execution.data),
                "provider": raw_execution.provider,
                "model": raw_execution.model,
                "evidence_refs": list(raw_execution.evidence_refs),
            }
        execution_evidence_hash = _payload_hash(execution_evidence)
        finalisation_context = {
            "draft_response": execution if isinstance(execution, str) else None,
            "reviewer_findings": reviewer_findings,
            "completion_evidence": model_completion_evidence,
            # Rolling compatibility for observers that inspect the former
            # flattened Finalisation context.  The model sees only the four
            # variables rendered into system_finalisation.txt.
            **execution_evidence,
            "review": reviewer_findings,
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
        elif isinstance(canonical_execution, str):
            # A non-empty model-authored natural-language response means the
            # Execution workflow ended normally.  Review/Finalisation may
            # improve its presentation, but must not reinterpret it as a
            # machine-classified objective outcome.
            desired_terminal = TerminalState.COMPLETED
            error = ""
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

        if isinstance(canonical_execution, ExecutionOutcome):
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

        review_disclosure = self._required_review_disclosure(state)
        if review_disclosure and not self._report_discloses_review_state(
            report,
            state.last_review,
        ):
            report = f"{report.rstrip()}\n\nValidation note: {review_disclosure}"
            detail = f"{detail}; review_disclosure_appended=true"
            self._audit(
                state,
                stage=Stage.FINALISATION.value,
                role="runtime",
                event="finalisation_review_disclosure_appended",
                event_id=(
                    f"{state.ledger.turn_id}:finalisation:review-disclosure-appended"
                ),
                payload={
                    "review_outcome": (
                        state.last_review.outcome.value
                        if state.last_review is not None
                        else None
                    ),
                    "review_remediated": state.review_remediated,
                    "review_resolved_by_replan": state.review_resolved_by_replan,
                    "disclosure_sha256": hashlib.sha256(
                        review_disclosure.encode("utf-8")
                    ).hexdigest(),
                },
            )

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
        draft_replaced = False
        if state.execution_draft_delivered and state.execution_draft_event_id:
            draft_replaced = await self._resolve_initial(
                state,
                resolution=delivery_kind,
                text=report,
                target_event_id=state.execution_draft_event_id,
                event_id=f"{state.execution_draft_event_id}:{delivery_kind}",
            )
        if not draft_replaced:
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
                canonical_execution.summary
                if isinstance(canonical_execution, ExecutionOutcome)
                else (
                    canonical_execution if isinstance(canonical_execution, str) else ""
                )
            ),
        )
        return self._result(
            state,
            terminal=desired_terminal,
            text=report,
            error=error,
            final_already_delivered=draft_replaced,
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
            "completed_receipts": [dict(item) for item in snapshot.receipt_summaries],
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
        state.execution_completed_by_replan = True
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
    ) -> str | ExecutionOutcome | None:
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
            # Review validation time is derived from the real wall-clock cost of
            # every authoritative Execution attempt, including high-volume
            # sub-agents and later remediation. Accumulating attempts avoids
            # shrinking the tool timeout after a short remediation that followed
            # a long initial execution.
            state.execution_elapsed_s += max(0.0, time.monotonic() - started_at)

    async def _execute_once_untracked(
        self, state: _TurnState, classification: TriageClassification
    ) -> str | ExecutionOutcome | None:
        replan_coordinator = self._new_replan_coordinator(state, classification)
        sub_agent_results: tuple[SubAgentResult, ...] = ()
        state.last_execution_response = None
        state.last_execution_structure_valid = False
        state.execution_completed_by_replan = False
        state.last_execution_error = ""
        state.last_execution_failure = None
        try:
            if classification is TriageClassification.HIGH_VOLUME_TASK:
                sub_agent_results = await self._run_subagents(state, replan_coordinator)
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
                    _subagent_result_payload(result) for result in sub_agent_results
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
            execution_plan_id = str(state.ledger.plan_id or "")
            _response, execution = await self._invoke_stage(
                state,
                Stage.EXECUTION,
                parse_execution_message,
                profile=profile,
                allow_tools=True,
                allow_side_effects=not self.config.shadow_mode,
                context=execution_context,
                defer_structured_error=False,
                publish_commentary=replan_coordinator is None,
                checkpoint_coordinator=replan_coordinator,
            )
            state.last_execution_response = _response
            state.last_execution_structure_valid = False
            for ref in _response.evidence_refs:
                if ref not in state.evidence_refs:
                    state.evidence_refs.append(ref)
            delegated_plan_snapshot = bool(
                classification is TriageClassification.HIGH_VOLUME_TASK
                and (
                    sub_agent_results
                    or (
                        isinstance(state.active_plan, Mapping)
                        and state.active_plan.get("sub_agents")
                    )
                )
            )
            if (
                delegated_plan_snapshot
                and str(state.ledger.plan_id or "") != execution_plan_id
            ):
                self._audit(
                    state,
                    stage=Stage.EXECUTION.value,
                    role="runtime",
                    event="execution_candidate_superseded_by_plan_snapshot",
                    event_id=(
                        f"{state.ledger.turn_id}:execution:invocation:"
                        f"{execution_invocation}:plan-superseded"
                    ),
                    plan_id=execution_plan_id,
                    payload={
                        "candidate_plan_id": execution_plan_id,
                        "active_plan_id": state.ledger.plan_id,
                        "execution_replayed": False,
                        "completed_side_effects_preserved": True,
                    },
                )
                raise _ResumeExecutionAfterReplan()
            if replan_coordinator is not None:
                candidate_payload: Mapping[str, Any] = (
                    _execution_payload(execution)
                    if isinstance(execution, ExecutionOutcome)
                    else {
                        "natural_language_response": str(execution),
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

                if _response.validation_source != "reasoning_recovery":
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
            state.last_execution_error = (
                f"[{exc.error_code}] {exc.human_description}\n\nExact error: {exc}"
            )
            return None
        finally:
            if replan_coordinator is not None:
                self._merge_checkpoint_receipts(state, replan_coordinator.receipts)
                await replan_coordinator.close()

        if execution is None:
            return None
        assert isinstance(execution, str)
        return execution

    async def _run_subagents(
        self,
        state: _TurnState,
        checkpoint: CompulsoryReplanCoordinator | None,
    ) -> tuple[SubAgentResult, ...]:
        plan_id = str(state.ledger.plan_id or "")
        if not plan_id or not isinstance(state.active_plan, Mapping):
            raise StageInvocationError(
                "sub-agent dispatch requires an authoritative plan snapshot",
                retryable=False,
            )
        batch = self._subagent_batch(state, plan_id, state.active_plan)
        if not batch.assignments:
            return ()

        assignment_by_id = {
            assignment.assignment_id: assignment for assignment in batch.assignments
        }
        waves = (
            batch.parallel_groups
            if batch.parallel_groups
            else (tuple(assignment_by_id),)
        )
        for wave_index, wave in enumerate(waves, start=1):
            pending = [
                assignment_by_id[assignment_id]
                for assignment_id in wave
                if assignment_id not in batch.results
            ]
            if not pending:
                continue
            batch.running.update(item.assignment_id for item in pending)
            tasks = [
                asyncio.create_task(
                    self._invoke_subagent_for_batch(
                        state,
                        batch,
                        assignment,
                        checkpoint,
                    ),
                    name=(
                        f"her-v2-sub-agent:{state.ledger.turn_id}:"
                        f"{batch.plan_version}:{assignment.assignment_id}"
                    ),
                )
                for assignment in pending
            ]
            try:
                wave_results = tuple(await asyncio.gather(*tasks))
            except BaseException:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                for assignment in pending:
                    if assignment.assignment_id not in batch.results:
                        batch.cancelled.add(assignment.assignment_id)
                raise
            finally:
                batch.running.difference_update(
                    assignment.assignment_id for assignment in pending
                )
            for result in wave_results:
                assert batch.results.get(result.assignment_id) == result

            current_plan_id = str(state.ledger.plan_id or "")
            if current_plan_id != batch.plan_id:
                remaining_ids = {
                    assignment_id
                    for later_wave in waves[wave_index:]
                    for assignment_id in later_wave
                    if assignment_id not in batch.results
                }
                batch.cancelled.update(remaining_ids)
                self._audit(
                    state,
                    stage=Stage.EXECUTION.value,
                    role="runtime",
                    event="sub_agent_batch_superseded",
                    event_id=(
                        f"{state.ledger.turn_id}:sub-agent-batch:"
                        f"{batch.plan_version}:superseded"
                    ),
                    plan_id=batch.plan_id,
                    payload={
                        "batch_plan_id": batch.plan_id,
                        "active_plan_id": current_plan_id or None,
                        "completed_assignment_ids": list(batch.results),
                        "cancelled_assignment_ids": sorted(batch.cancelled),
                        "results_attached_to_replacement_plan": False,
                    },
                )
                raise _ResumeExecutionAfterReplan()

        return tuple(
            batch.results[assignment.assignment_id] for assignment in batch.assignments
        )

    def _subagent_batch(
        self,
        state: _TurnState,
        plan_id: str,
        plan: Mapping[str, Any],
    ) -> _SubAgentBatch:
        existing = state.sub_agent_batches.get(plan_id)
        if existing is not None:
            return existing

        raw_assignments = (
            plan.get("sub_agents", []) if isinstance(plan, Mapping) else []
        )
        if not isinstance(raw_assignments, list):
            raise StageInvocationError(
                "high-volume plan sub_agents must be a list", retryable=False
            )
        assignments: list[SubAgentAssignment] = []
        seen: set[str] = set()
        available_profiles = set(self.config.sub_agent_execution_profile_names())
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
            if profile_name not in available_profiles:
                raise StageInvocationError(
                    f"sub-agent assignment {assignment_id!r} selects unavailable "
                    f"execution profile {profile_name!r}; available profiles: "
                    f"{', '.join(sorted(available_profiles))}",
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
                    details={"attachment_ids": sorted(unauthorized_attachment_ids)},
                )
            assignments.append(
                SubAgentAssignment(
                    assignment_id=assignment_id,
                    task=task,
                    profile=profile_name,
                    tools=tuple(str(item) for item in raw_tools if str(item).strip()),
                    allow_side_effects=requested_side_effects,
                    attachment_ids=selected_attachment_ids,
                    definition=dict(raw),
                )
            )
            seen.add(assignment_id)

        raw_parallel_groups = plan.get("parallel_groups", [])
        if not isinstance(raw_parallel_groups, list):
            raise StageInvocationError(
                "high-volume plan parallel_groups must be a list", retryable=False
            )
        parallel_groups: list[tuple[str, ...]] = []
        scheduled_ids: set[str] = set()
        for index, raw_group in enumerate(raw_parallel_groups, start=1):
            if (
                not isinstance(raw_group, list)
                or not raw_group
                or any(
                    not isinstance(item, str) or not item.strip() for item in raw_group
                )
            ):
                raise StageInvocationError(
                    f"sub-agent parallel group {index} is invalid", retryable=False
                )
            group = tuple(item.strip() for item in raw_group)
            unknown = set(group) - seen
            repeated = set(group) & scheduled_ids
            if unknown or repeated or len(group) != len(set(group)):
                raise StageInvocationError(
                    f"sub-agent parallel group {index} has invalid assignment IDs",
                    retryable=False,
                    details={
                        "unknown_assignment_ids": sorted(unknown),
                        "repeated_assignment_ids": sorted(repeated),
                    },
                )
            scheduled_ids.update(group)
            parallel_groups.append(group)
        if parallel_groups and scheduled_ids != seen:
            raise StageInvocationError(
                "sub-agent parallel_groups must schedule every assignment",
                retryable=False,
                details={"omitted_assignment_ids": sorted(seen - scheduled_ids)},
            )

        batch = _SubAgentBatch(
            plan_id=plan_id,
            plan_version=state.plan_version,
            assignments=tuple(assignments),
            parallel_groups=tuple(parallel_groups),
            predecessor_plan_id=state.previous_plan_id,
        )
        state.sub_agent_batches[plan_id] = batch
        predecessor = state.sub_agent_batches.get(state.previous_plan_id)
        if predecessor is not None:
            previous_assignments = {
                item.assignment_id: item for item in predecessor.assignments
            }
            for assignment in batch.assignments:
                previous_assignment = previous_assignments.get(assignment.assignment_id)
                previous_result = predecessor.results.get(assignment.assignment_id)
                if (
                    previous_assignment is None
                    or previous_result is None
                    or previous_result.disposition
                    not in {
                        ExecutionDisposition.COMPLETED,
                        ExecutionDisposition.COMPLETED_WITH_LIMITATIONS,
                    }
                    or self._subagent_assignment_signature(previous_assignment)
                    != self._subagent_assignment_signature(assignment)
                ):
                    continue
                source_plan_id = (
                    previous_result.source_plan_id or previous_result.plan_id
                )
                reused = replace(
                    previous_result,
                    plan_id=plan_id,
                    source_plan_id=source_plan_id,
                    reused=True,
                )
                batch.results[assignment.assignment_id] = reused
                self._audit(
                    state,
                    stage=Stage.EXECUTION.value,
                    role="runtime",
                    event="sub_agent_result_reused",
                    event_id=(
                        f"{state.ledger.turn_id}:sub-agent-batch:"
                        f"{batch.plan_version}:{assignment.assignment_id}:reused"
                    ),
                    plan_id=plan_id,
                    payload={
                        "assignment_id": assignment.assignment_id,
                        "source_plan_id": source_plan_id,
                        "target_plan_id": plan_id,
                        "assignment_preserved_exactly": True,
                        "prior_result_successful": True,
                    },
                )
        self._audit(
            state,
            stage=Stage.EXECUTION.value,
            role="runtime",
            event="sub_agent_batch_created",
            event_id=(
                f"{state.ledger.turn_id}:sub-agent-batch:{batch.plan_version}:created"
            ),
            plan_id=plan_id,
            payload={
                "plan_id": plan_id,
                "predecessor_plan_id": batch.predecessor_plan_id or None,
                "assignment_ids": [item.assignment_id for item in batch.assignments],
                "parallel_groups": [list(group) for group in batch.parallel_groups],
                "reused_assignment_ids": [
                    item.assignment_id
                    for item in batch.assignments
                    if item.assignment_id in batch.results
                ],
            },
        )
        return batch

    @staticmethod
    def _subagent_assignment_signature(assignment: SubAgentAssignment) -> str:
        return _payload_hash(
            {
                **dict(assignment.definition),
                "id": assignment.assignment_id,
                "task": assignment.task,
                "profile": assignment.profile,
                "tools": list(assignment.tools),
                "attachment_ids": list(assignment.attachment_ids),
                "allow_side_effects": assignment.allow_side_effects,
            }
        )

    async def _invoke_subagent_for_batch(
        self,
        state: _TurnState,
        batch: _SubAgentBatch,
        assignment: SubAgentAssignment,
        checkpoint: CompulsoryReplanCoordinator | None,
    ) -> SubAgentResult:
        try:
            result = await self._invoke_subagent(
                state,
                assignment,
                checkpoint,
                plan_id=batch.plan_id,
                plan_version=batch.plan_version,
            )
        except asyncio.CancelledError:
            batch.cancelled.add(assignment.assignment_id)
            raise
        else:
            batch.results[assignment.assignment_id] = result
            batch.cancelled.discard(assignment.assignment_id)
            return result
        finally:
            batch.running.discard(assignment.assignment_id)

    async def _invoke_subagent(
        self,
        state: _TurnState,
        assignment: SubAgentAssignment,
        checkpoint: CompulsoryReplanCoordinator | None,
        *,
        plan_id: str,
        plan_version: int,
    ) -> SubAgentResult:
        profile = self.config.profile_for_name(assignment.profile)
        role = f"sub_agent:{assignment.assignment_id}"
        side_effects_authorised = (
            assignment.allow_side_effects and not self.config.shadow_mode
        )
        event_prefix = (
            f"{state.ledger.turn_id}:sub-agent:{plan_version}:"
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
            plan_id=plan_id,
            payload={
                "plan_id": plan_id,
                "assignment_id": assignment.assignment_id,
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
            "plan_id": plan_id,
            "assignment_id": assignment.assignment_id,
            "assigned_task": assignment.task,
            "assignment_definition": dict(assignment.definition),
            "profile": assignment.profile,
            "delegated_tools": list(assignment.tools),
            "authorized_attachment_ids": list(assignment.attachment_ids),
            "authority": {
                "scope": "bounded_execution_only",
                "may_change_user_goal": False,
                "may_change_classification": False,
                "may_change_active_plan": False,
                "may_replan": False,
                "may_contact_user": False,
                "may_finalise": False,
                "may_create_subagents": False,
            },
            "may_replan": False,
            "may_contact_user": False,
            "may_finalise": False,
            "may_create_subagents": False,
            "shadow_mode": self.config.shadow_mode,
            "replan_continuation": (
                dict(state.replan_continuation)
                if state.replan_continuation and state.previous_plan_id
                else None
            ),
            "continuation_rules": {
                "continue_from_current_workspace": True,
                "preserve_completed_evidence": True,
                "never_repeat_completed_side_effects_because_of_replanning": True,
            },
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
                bound_plan_id=plan_id,
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
                assignment_id=assignment.assignment_id,
                plan_id=plan_id,
                disposition=outcome.disposition,
                summary=outcome.summary,
                evidence_refs=evidence_refs,
                limitations=outcome.limitations,
                attachment_ids=assignment.attachment_ids,
                source_plan_id=plan_id,
            )
            self._audit(
                state,
                stage=Stage.EXECUTION.value,
                role=role,
                event="sub_agent_completed",
                event_id=f"{event_prefix}:completed",
                provider=response.provider or profile.engine,
                model=response.model or profile.model,
                plan_id=plan_id,
                payload=_subagent_result_payload(result),
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
                plan_id=plan_id,
                payload=failure_payload,
            )
            return SubAgentResult(
                assignment_id=assignment.assignment_id,
                plan_id=plan_id,
                disposition=ExecutionDisposition.FAILED,
                summary=f"Sub-agent execution failed: {exc}",
                limitations=("Sub-agent result unavailable.",),
                attachment_ids=assignment.attachment_ids,
                source_plan_id=plan_id,
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
                "status": state.last_review.outcome.value,
                "reason": state.last_review.summary,
                "conditions": (
                    "\n".join(state.last_review.findings)
                    if state.last_review.findings
                    else None
                ),
                "remediation_applied": state.review_remediated,
            }
            if state.last_review is not None
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
            "delegated_execution": (
                state.sub_agent_batches[str(state.ledger.plan_id)].as_payload()
                if str(state.ledger.plan_id) in state.sub_agent_batches
                else None
            ),
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
            "foreground_cleanup": dict(state.last_foreground_cleanup) or None,
            "attachment_manifest": [dict(item) for item in state.attachment_manifest],
            "media_routing_by_stage": {
                stage: [dict(entry) for entry in entries]
                for stage, entries in state.media_routing_by_stage.items()
            },
            "workflow_counters": {
                "completed_replans": state.replan_count,
                "reviews": state.review_count,
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
        execution_tool_catalogue = self._execution_tool_catalogue()
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
            if (
                classification is not TriageClassification.HIGH_VOLUME_TASK
                and outcome.plan.get("sub_agents")
            ):
                raise StructuredOutputError(
                    "Replanning may delegate sub-agents only for HIGH_VOLUME_TASK"
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
        replanning_context: dict[str, Any] = {
            "active_plan": prior_plan,
            "available_execution_tools": [
                dict(item) for item in execution_tool_catalogue
            ],
            "execution_allow_side_effects": not self.config.shadow_mode,
            "plan_edit_history": [dict(entry) for entry in state.plan_edit_history],
            "workflow_state_and_evidence": workflow_state_and_evidence,
        }
        if classification is TriageClassification.HIGH_VOLUME_TASK:
            replanning_context["available_sub_agent_profiles"] = list(
                self.config.sub_agent_execution_profile_names()
            )
        response, outcome = await self._invoke_stage(
            state,
            Stage.REPLANNING,
            validate_replan,
            allow_tools=False,
            publish_commentary=False,
            context=replanning_context,
        )
        assert isinstance(outcome, ReplanningOutcome)
        state.replan_count += 1
        if outcome.plan_changed:
            self._activate_plan(state, outcome.plan, replacement=True)
        else:
            # An unchanged Replan is a progress calibration against the current
            # authoritative snapshot, not a plan replacement.  Preserve its ID
            # and version so in-flight work remains bound to the same plan.
            outcome = replace(outcome, plan=prior_plan)
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
        if (
            not has_plan_status_claim
            or not candidate
            or any(
                fragment not in normalized_candidate for fragment in required_fragments
            )
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
                minimal_persona_fallback_reason=("replan_model_commentary_fallback"),
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
        execution: str | ExecutionOutcome,
        max_reviews: int,
    ) -> tuple[str | ExecutionOutcome | None, ReviewOutcome | None]:
        if max_reviews <= 0:
            return execution, None

        finding = await self._review_once(
            state,
            classification,
            execution,
            review_kind="independent",
        )
        while True:
            if finding.outcome is ReviewOutcome.PASS:
                return execution, finding.outcome
            if finding.outcome is ReviewOutcome.CONDITIONAL_PASS:
                self._merge_review_limitations(
                    state, finding.findings or (finding.summary,)
                )
                return execution, finding.outcome
            if finding.outcome in {
                ReviewOutcome.INCONCLUSIVE,
                ReviewOutcome.UNAVAILABLE,
            }:
                # These are runtime-only technical states, not model-authored
                # Review decisions. Do not create an endless max-effort loop
                # when the reviewer provider or its tools are unavailable.
                self._merge_review_limitations(
                    state, finding.findings or (finding.summary,)
                )
                return execution, finding.outcome

            failure_reasons = finding.findings or (finding.summary,)
            if state.active_plan is None:
                self._merge_review_limitations(state, failure_reasons)
                return execution, finding.outcome

            state.review_resolved_by_replan = False
            replan = await self._perform_replan(
                state,
                classification,
                reason="independent_review_failed",
                reviewer_findings=failure_reasons,
            )
            if replan.completion_percent < 100:
                await self._transition(state, LifecycleState.EXECUTING)
                execution = await self._execute_once(state, classification)
                await self._transition(state, LifecycleState.EXECUTION_COMPLETED)
                if execution is None:
                    self._merge_review_limitations(state, failure_reasons)
                    return None, finding.outcome
                if isinstance(execution, str) and state.effort in {
                    Effort.XHIGH,
                    Effort.MAX,
                }:
                    await self._advance_execution_draft_commentary(
                        state,
                        response=execution,
                    )
                if isinstance(execution, ExecutionOutcome):
                    self._merge_execution_evidence(state, execution)
                state.review_remediated = True
            else:
                state.review_resolved_by_replan = True
                await self._transition(state, LifecycleState.EXECUTION_COMPLETED)

            if state.effort is Effort.XHIGH:
                # xhigh has exactly one independent Review.  A FAIL is passed
                # through Replanning to one permitted repair Execution, whose
                # latest natural-language response becomes the new draft for
                # Finalisation.  There is deliberately no closure Review.
                return execution, finding.outcome

            finding = await self._review_once(
                state,
                classification,
                execution,
                review_kind="closure",
                findings_to_close=failure_reasons,
            )
            # max has no Review/fix round limit. Continue until the reviewer
            # returns PASS or CONDITIONAL_PASS (or a runtime-only technical
            # state makes further Review impossible).

    async def _review_once(
        self,
        state: _TurnState,
        classification: TriageClassification,
        execution: str | ExecutionOutcome,
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
                allow_side_effects=not self.config.shadow_mode,
                context={
                    "classification": classification.value,
                    "active_plan": state.active_plan,
                    "execution": (
                        _execution_payload(execution)
                        if isinstance(execution, ExecutionOutcome)
                        else None
                    ),
                    "draft_response": (
                        execution if isinstance(execution, str) else None
                    ),
                    "evidence_refs": list(state.evidence_refs),
                    "review_kind": review_kind,
                    "findings_to_close": list(findings_to_close),
                    "reviewer_authority": (
                        "independent_verification_without_remediation"
                    ),
                    "delegated_tools": [
                        "workspace_inspect",
                        "file_read",
                        "file_list",
                        "process_list",
                        "media_read",
                        *([] if self.config.shadow_mode else ["verification_run"]),
                    ],
                    "execution_elapsed_s": state.execution_elapsed_s,
                    "verification_run_policy": {
                        "workspace": "authoritative_current_workspace",
                        "workspace_copied": False,
                        "process_authority": "inherited",
                        "environment": "inherited",
                        "network": "inherited",
                        "timeout_basis": "cumulative_execution_elapsed",
                    },
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

    @staticmethod
    def _merge_review_limitations(
        state: _TurnState, limitations: Sequence[str]
    ) -> None:
        for limitation in limitations:
            value = str(limitation or "").strip()
            if value and value not in state.limitations:
                state.limitations.append(value)

    @staticmethod
    def _required_review_disclosure(state: _TurnState) -> str:
        finding = state.last_review
        if finding is None or finding.outcome is ReviewOutcome.PASS:
            return ""
        if finding.outcome is ReviewOutcome.FAIL and (
            state.review_remediated or state.review_resolved_by_replan
        ):
            # xhigh intentionally has no closure Review after its one repair,
            # and Replanning may instead establish that the authorised goal is
            # already complete.  The pre-repair FAIL is not a current verdict.
            return ""

        details = tuple(
            value
            for value in (
                str(item or "").strip()
                for item in (finding.findings or (finding.summary,))
            )
            if value
        )
        detail = "; ".join(dict.fromkeys(details))
        if finding.outcome is ReviewOutcome.CONDITIONAL_PASS:
            return f"Independent validation completed with a limitation: {detail}"
        if finding.outcome is ReviewOutcome.UNAVAILABLE:
            unavailable_detail = detail
            prefix = "Independent Review unavailable:"
            if unavailable_detail.casefold().startswith(prefix.casefold()):
                unavailable_detail = unavailable_detail[len(prefix) :].strip()
            return f"Independent validation was unavailable: {unavailable_detail}"
        if finding.outcome is ReviewOutcome.INCONCLUSIVE:
            return f"Independent validation was inconclusive: {detail}"
        if finding.outcome is ReviewOutcome.FAIL:
            return f"Independent validation found an unresolved issue: {detail}"
        return ""

    @staticmethod
    def _report_discloses_review_state(
        report: str,
        finding: ReviewFinding | None,
    ) -> bool:
        if finding is None:
            return True
        normalised_report = _normalise_text(report)
        details = tuple(
            _normalise_text(item)
            for item in (finding.findings or (finding.summary,))
            if _normalise_text(item)
        )
        if any(item in normalised_report for item in details):
            return True
        if finding.outcome is ReviewOutcome.UNAVAILABLE:
            return any(
                marker in normalised_report
                for marker in (
                    "review was unavailable",
                    "review is unavailable",
                    "validation was unavailable",
                    "validation is unavailable",
                    "verification was unavailable",
                    "verification is unavailable",
                )
            )
        if finding.outcome is ReviewOutcome.INCONCLUSIVE:
            return any(
                marker in normalised_report
                for marker in (
                    "review was inconclusive",
                    "review is inconclusive",
                    "validation was inconclusive",
                    "validation is inconclusive",
                    "verification was inconclusive",
                    "verification is inconclusive",
                )
            )
        return False
