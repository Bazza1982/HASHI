"""Modular, provider-neutral HER v2 stage orchestrator."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import logging
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from orchestrator.multimodal_contract import (
    attachment_manifest,
    canonical_request_content,
    normalize_request_content,
    resolve_input_capability,
)

from .audit import AuditPersistenceError, DurableAuditLog
from .commentary import (
    CommentaryPort,
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
    TurnControl,
    TurnStopped,
)
from .ledger import ExecutionLedger, LedgerInvariantError, LedgerStore
from .lifecycle import LifecycleViolation
from .models import (
    DeliveryRecord,
    Effort,
    LifecycleState,
    Stage,
    StageResponse,
    SubAgentAssignment,
    SubAgentResult,
    TerminalState,
    ToolEvidenceReceipt,
    TurnResult,
    parse_effort,
)
from .presentation import (
    PRESENTATION_AUTHORITIES,
    FinalStyleRenderer,
    FinalStyleRequest,
    PresentationRequirement,
    RequiredPersonaRenderer,
)
from .progress import ProgressTracker
from .prompts import extract_authoritative_current_request
from .retry import DEFAULT_PROVIDER_RETRY_POLICY, ProviderRetryPolicy
from .runtime_invocation import RuntimeInvocationMixin
from .runtime_support import (
    RuntimeSupportMixin,
    _technical_error_message,
    _technical_error_message_from_failure,
)
from .structured import (
    parse_direct_message,
)
from .strategy_playbook import (
    load_strategy_playbook,
)
from .task_state import HERTaskState

Validator = Callable[[StageResponse], Any]


@dataclass(frozen=True)
class _StageModalityContract:
    input_modalities: frozenset[str]
    output_modalities: frozenset[str]
    input_policy: str = "auto"
    source: str = "unknown"


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


def _content_includes_audio(content: Sequence[Mapping[str, Any]]) -> bool:
    """Return whether typed stage output contains a native audio part."""

    return any(
        str(part.get("type") or "").strip().lower() == "audio"
        for part in content
        if isinstance(part, Mapping)
    )


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
    request_mode: str = "text"
    task_state: HERTaskState | None = None
    request_content: Mapping[str, Any] | None = None
    attachment_manifest: tuple[Mapping[str, Any], ...] = ()
    habit_catalogue: tuple[str, ...] = ()
    relevant_habits: tuple[str, ...] = ()
    strategy_handoff: Mapping[str, Any] | None = None
    strategy_playbook_ref: Mapping[str, Any] | None = None
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
    checkpoint_count: int = 0
    execution_cycle_serial: int = 0
    stage_invocation_serial: int = 0
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
    stage_timing_intervals: dict[str, list[tuple[float, float]]] = field(
        default_factory=dict
    )


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
        final_style: FinalStyleRenderer | None = None,
        habits: HabitAdvisor | None = None,
        meditation: MeditationRunner | None = None,
        dream: DreamMaintainer | None = None,
        logger: logging.Logger | None = None,
        retry_policy: ProviderRetryPolicy | None = None,
        workzone_ref: str = "",
        checkpoint_clock: Callable[[], float] = time.monotonic,
        timing_clock: Callable[[], float] = time.perf_counter,
        skills_catalogue: Sequence[Mapping[str, Any]] | None = None,
        capability_cache_path: Path | str | None = None,
        pcm_input: Mapping[str, Any] | None = None,
        turn_services: Any | None = None,
    ) -> None:
        self.config = config
        self.pcm_input = dict(pcm_input or {})
        self.turn_services = turn_services
        self.provider = provider
        self.ledger_store = ledger_store
        self.audit_log = audit_log
        self.delivery = delivery or RecordingDelivery()
        self.commentary = commentary or NullCommentaryPort()
        self.required_persona = required_persona
        self.final_style = final_style
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
        self.timing_clock = timing_clock
        self.skills_catalogue = tuple(
            dict(item)
            for item in (skills_catalogue or ())
            if isinstance(item, Mapping)
        )
        self.capability_cache_path = capability_cache_path
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
        request_mode: str = "text",
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
            request_mode=(
                str(request_mode or "text").strip().casefold() or "text"
            ),
            # Cognitive control is a permanent HER v2 safety invariant, not an
            # Agent/configuration capability. Every Turn therefore owns one
            # lifecycle-wide TaskState before any stage is invoked.
            task_state=HERTaskState(
                goal=extract_authoritative_current_request(prompt) or prompt
            ),
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
            cleanup = getattr(self.provider, "cleanup_text_audio", None)
            if callable(cleanup):
                with suppress(Exception):
                    cleanup_result = cleanup()
                    if inspect.isawaitable(cleanup_result):
                        await cleanup_result

    async def stop_turn(self, turn_id: str, *, reason: str = "USER_STOP") -> bool:
        control = self._controls.get(str(turn_id))
        if control is None:
            return False
        control.stop(reason)
        return True

    def interrupt_nowait(self, reason: str = "USER_STOP") -> int:
        """Signal live turns and provider children from the control lane."""

        try:
            controls = tuple(self._controls.values())
        except RuntimeError:
            controls = tuple(
                control
                for key in tuple(self._controls)
                if (control := self._controls.get(key)) is not None
            )
        interrupted = 0
        for control in controls:
            control.stop(reason)
            interrupted += 1
        provider_interrupt = getattr(self.provider, "interrupt_nowait", None)
        if callable(provider_interrupt):
            try:
                interrupted += int(provider_interrupt(reason) or 0)
            except Exception:
                self.logger.exception("Out-of-band provider interruption failed")
        return interrupted

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

    async def _stage_modality_contract(
        self, stage: Stage
    ) -> _StageModalityContract:
        def normalized_modalities(value: Any) -> frozenset[str]:
            if isinstance(value, str):
                value = (value,)
            if not isinstance(value, (list, tuple, set, frozenset)):
                return frozenset()
            return frozenset(
                str(item or "").strip().casefold()
                for item in value
                if str(item or "").strip()
            )

        profile = self.config.profile_for(stage)
        resolver = getattr(self.provider, "resolve_stage_modalities", None)
        if callable(resolver):
            raw = resolver(profile)
            if inspect.isawaitable(raw):
                raw = await raw
            if not isinstance(raw, Mapping):
                raise StageInvocationError(
                    "stage modality resolver returned no typed contract",
                    retryable=False,
                    code=ProviderFailureCode.PROVIDER_CONFIGURATION_ERROR,
                    human_description=(
                        "The configured stage model exposes no valid input/output "
                        "modality contract."
                    ),
                )
            input_modalities = normalized_modalities(raw.get("input_modalities"))
            output_modalities = normalized_modalities(
                raw.get("output_modalities")
            )
            input_policy = str(raw.get("input_policy") or "auto").strip().casefold()
            source = str(raw.get("source") or "provider")
        else:
            capability = resolve_input_capability(
                profile.engine,
                profile.model,
                config=profile.options,
                capability_cache_path=self.capability_cache_path,
            )
            input_modalities = capability.input_modalities
            raw_output = profile.options.get("output_modalities")
            if isinstance(raw_output, str):
                raw_output = [raw_output]
            output_modalities = (
                frozenset(
                    str(item or "").strip().casefold()
                    for item in raw_output
                    if str(item or "").strip()
                )
                if isinstance(raw_output, (list, tuple, set, frozenset))
                else capability.output_modalities
            )
            input_policy = str(
                profile.options.get("input_policy") or "auto"
            ).strip().casefold()
            source = capability.source

        if input_policy not in {"auto", "audio_required"}:
            raise StageInvocationError(
                f"invalid input_policy {input_policy!r} for stage {stage.value}",
                retryable=False,
                code=ProviderFailureCode.PROVIDER_CONFIGURATION_ERROR,
                human_description=(
                    "The configured stage model has an invalid text-input "
                    "adaptation policy."
                ),
            )
        if not input_modalities:
            raise StageInvocationError(
                f"stage {stage.value} declares no input modalities",
                retryable=False,
                code=ProviderFailureCode.PROVIDER_CONFIGURATION_ERROR,
                human_description=(
                    "The configured stage model declares no usable input modality."
                ),
            )
        return _StageModalityContract(
            input_modalities=input_modalities,
            output_modalities=output_modalities,
            input_policy=input_policy,
            source=source,
        )

    @staticmethod
    def _text_transport_for_stage(
        stage: Stage, contract: _StageModalityContract
    ) -> str:
        if contract.input_policy == "audio_required":
            if "audio" in contract.input_modalities:
                return "audio"
            raise StageInvocationError(
                f"stage {stage.value} requires audio input but declares no audio capability",
                retryable=False,
                code=ProviderFailureCode.PROVIDER_CONFIGURATION_ERROR,
                human_description=(
                    "The configured stage input policy requires audio, but its "
                    "model has no verified audio input capability."
                ),
            )
        if "text" in contract.input_modalities:
            return "text"
        if "audio" in contract.input_modalities:
            return "audio"
        return "unsupported"

    async def _materialize_text_audio(
        self,
        state: _TurnState,
        *,
        stages: Sequence[Stage],
    ) -> Mapping[str, Any]:
        converter = getattr(self.provider, "materialize_text_audio", None)
        if not callable(converter):
            raise StageInvocationError(
                "stage requires audio input but no text-to-audio converter is available",
                retryable=False,
                code=ProviderFailureCode.INPUT_MODALITY_CONVERSION_FAILED,
                human_description=(
                    "The configured model requires audio input, but HASHI could "
                    "not convert the current text request."
                ),
            )
        authoritative_text = extract_authoritative_current_request(state.request)
        try:
            converted = converter(
                text=authoritative_text,
                turn_id=state.ledger.turn_id,
                request_ref=state.request_ref,
            )
            if inspect.isawaitable(converted):
                converted = await converted
        except StageInvocationError:
            raise
        except Exception as exc:
            raise StageInvocationError(
                f"text-to-audio input conversion failed: {exc}",
                retryable=False,
                code=ProviderFailureCode.INPUT_MODALITY_CONVERSION_FAILED,
                human_description=(
                    "The configured model requires audio input, but HASHI could "
                    "not convert the current text request."
                ),
            ) from exc
        if not isinstance(converted, Mapping):
            raise StageInvocationError(
                "text-to-audio converter returned no canonical content",
                retryable=False,
                code=ProviderFailureCode.INPUT_MODALITY_CONVERSION_FAILED,
                human_description=(
                    "HASHI's text-to-audio converter returned no usable audio "
                    "input."
                ),
            )
        try:
            derived = normalize_request_content(converted)
            derived_manifest = attachment_manifest(derived)
        except Exception as exc:
            raise StageInvocationError(
                f"text-to-audio converter returned invalid canonical content: {exc}",
                retryable=False,
                code=ProviderFailureCode.INPUT_MODALITY_CONVERSION_FAILED,
                human_description=(
                    "HASHI's text-to-audio converter returned invalid audio input."
                ),
            ) from exc
        if not derived_manifest or any(
            str(item.get("modality") or "") != "audio"
            or str(item.get("semantic_role") or "") == "voice_message"
            for item in derived_manifest
        ):
            raise StageInvocationError(
                "derived text transport must contain non-voice audio attachments only",
                retryable=False,
                code=ProviderFailureCode.INPUT_MODALITY_CONVERSION_FAILED,
                human_description=(
                    "HASHI's converted input was not a safe derived audio "
                    "transport asset."
                ),
            )

        original = normalize_request_content(state.request_content)
        merged_parts = [
            copy.deepcopy(item)
            for item in (
                list(original.get("parts") or []) if original is not None else []
            )
        ]
        merged_parts.extend(copy.deepcopy(list(derived.get("parts") or [])))
        for index, item in enumerate(merged_parts, start=1):
            item["item_index"] = index
        merged = canonical_request_content(merged_parts)
        stage_key = "-".join(sorted({stage.value for stage in stages})) or "unknown"
        self._audit(
            state,
            stage="input_adaptation",
            role="runtime",
            event="text_input_adapted_to_audio",
            event_id=(
                f"{state.ledger.turn_id}:input-adaptation:text-to-audio:{stage_key}"
            ),
            payload={
                "authoritative_text_sha256": hashlib.sha256(
                    authoritative_text.encode("utf-8")
                ).hexdigest(),
                "target_stages": [stage.value for stage in stages],
                "derived_attachment_ids": [
                    str(item.get("attachment_id") or "")
                    for item in derived_manifest
                ],
                "semantic_role": "audio_attachment",
                "authoritative_input_remains_text": True,
            },
        )
        return merged

    async def _run_turn(self, state: _TurnState) -> TurnResult:
        """Run HERV3 as one model-owned reasoning/tool loop for every effort."""

        ref = self._audit(
            state,
            stage="initial",
            role="primary",
            event="request_received",
            event_id=f"{state.ledger.turn_id}:request",
            payload={
                "request": state.request,
                "reasoning_effort": state.effort.value,
                "workflow": "single_loop",
                "request_content_version": (
                    state.request_content.get("version")
                    if isinstance(state.request_content, Mapping)
                    else None
                ),
                "attachment_manifest": [dict(item) for item in state.attachment_manifest],
            },
        )
        state.ledger.add_log_ref(ref)
        self.ledger_store.save(state.ledger)

        direct_content: Mapping[str, Any] | None = None
        if not state.attachment_manifest:
            direct_contract = await self._stage_modality_contract(Stage.DIRECT)
            direct_transport = self._text_transport_for_stage(Stage.DIRECT, direct_contract)
            if direct_transport == "audio":
                direct_content = await self._materialize_text_audio(
                    state, stages=(Stage.DIRECT,)
                )
            elif direct_transport == "unsupported":
                raise StageInvocationError(
                    "HERV3 main model accepts neither text nor audio input",
                    retryable=False,
                    code=ProviderFailureCode.PROVIDER_MODALITY_UNSUPPORTED,
                    human_description=(
                        "The configured HERV3 main model cannot consume the current request."
                    ),
                )
        return await self._run_direct(
            state, request_content_override=direct_content
        )

    async def _run_direct(
        self,
        state: _TurnState,
        *,
        request_content_override: Mapping[str, Any] | None = None,
    ) -> TurnResult:
        """Run the HERV3 foreground path as one fully capable agent/tool loop."""

        habits: Sequence[str] = ()
        if self.config.meditation_enabled:
            with suppress(Exception):
                habits = await self.habits.retrieve(
                    goal=state.goal,
                    turn_id=state.ledger.turn_id,
                )

        strategy_playbook: Mapping[str, Any] | None = None
        if self.config.direct_strategy_self_selection:
            playbook = load_strategy_playbook()
            strategy_playbook = playbook.prompt_payload()
            state.strategy_playbook_ref = {
                "playbook_version": playbook.playbook_version,
                "sha256": playbook.sha256,
                "card_count": len(playbook.cards),
                "selection_mode": "optional_context",
            }
            ref = self._audit(
                state,
                stage=Stage.DIRECT.value,
                role=self.config.stage_roles[Stage.DIRECT],
                event="direct_strategy_playbook_attached",
                event_id=f"{state.ledger.turn_id}:direct-strategy-playbook",
                payload=dict(state.strategy_playbook_ref),
            )
            state.ledger.add_log_ref(ref)
            self.ledger_store.save(state.ledger)

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
                "direct_strategy_self_selection": bool(strategy_playbook),
                "strategy_playbook": strategy_playbook,
                "her_v3": True,
                "pcm_input": self.pcm_input,
                "zero_orchestration": True,
                "automatic_effort_upgrade_allowed": False,
                "sub_agent_delegation_allowed": False,
            },
            publish_commentary=False,
            request_content_override=request_content_override,
        )
        assert isinstance(direct_text, str)
        for evidence_ref in response.evidence_refs:
            if evidence_ref not in state.evidence_refs:
                state.evidence_refs.append(evidence_ref)

        presented_text = await self._apply_final_style(state, direct_text)
        await self._deliver(
            state,
            kind="final",
            text=presented_text,
            event_id=f"{state.ledger.turn_id}:final",
            required=True,
            provenance="her_v3_single_loop",
            detail=(
                "single_main_loop=true; planner=false; replanner=false; "
                "reviewer=false"
            ),
            content=tuple(response.content),
        )
        await self._transition(
            state,
            LifecycleState.COMPLETED,
            terminal_reason="direct_response_delivered",
        )
        self._schedule_meditation(
            state, terminal=TerminalState.COMPLETED, execution_summary=direct_text
        )
        return self._result(
            state,
            terminal=TerminalState.COMPLETED,
            text=presented_text,
            content=tuple(response.content),
        )

    async def _apply_final_style(self, state: _TurnState, draft: str) -> str:
        """Apply the optional presentation check without risking final delivery."""

        if self.final_style is None:
            return draft
        try:
            event_id = f"{state.ledger.turn_id}:final-style"

            def section_order(item: Mapping[str, Any]) -> tuple[int, str]:
                try:
                    order = int(item.get("order", 0))
                except (TypeError, ValueError):
                    order = 0
                return order, str(item.get("key") or "")

            sections = sorted(
                (
                    item
                    for item in (self.pcm_input.get("sections") or ())
                    if isinstance(item, Mapping)
                ),
                key=section_order,
            )
            requirements: list[PresentationRequirement] = []
            for index, item in enumerate(sections):
                authority = str(item.get("authority") or "").strip()
                text = str(item.get("text") or "").strip()
                if authority not in PRESENTATION_AUTHORITIES or not text:
                    continue
                requirements.append(
                    PresentationRequirement(
                        key=str(item.get("key") or f"presentation-{index}"),
                        title=str(
                            item.get("title") or item.get("key") or authority
                        ),
                        authority=authority,
                        text=text,
                    )
                )
            request = FinalStyleRequest(
                event_id=event_id,
                turn_id=state.ledger.turn_id,
                draft_text=draft,
                current_request=str(
                    self.pcm_input.get("current_request")
                    or extract_authoritative_current_request(state.goal)
                    or state.goal
                ),
                requirements=tuple(requirements),
            )
            result = await self.final_style.render(request)
            if result.source_event_id != event_id:
                raise ValueError("final style result source identity mismatch")
            if result.decision == "keep":
                return draft
            return result.text
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - presentation must fail open
            self.logger.warning(
                "HERV3 final style rendering failed safely: %s", type(exc).__name__
            )
            return draft


# Public migration alias: implementation remains behind the stable her-v2 adapter ID.
HERv3Runtime = HERv2Runtime
