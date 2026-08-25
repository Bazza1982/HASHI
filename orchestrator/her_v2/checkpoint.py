"""Request-local compulsory HER v2 Replanning cadence coordination.

The coordinator observes safe Tool Gateway boundaries only.  The 300-second
and 10-result values are intervals that require Replanning; they are never
provider/tool deadlines and never cap total Execution work or Replan count.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Sequence

from .audit import AuditPersistenceError
from .interfaces import StageInvocationError, TurnStopped
from .models import ReplanningOutcome, ToolEvidenceReceipt


CHECKPOINT_RESULT_THRESHOLD = 10
CHECKPOINT_ELAPSED_THRESHOLD_S = 300.0
MAX_CHECKPOINT_RECEIPT_SUMMARIES = 64
MAX_CHECKPOINT_ARGUMENT_KEYS = 32
MAX_CHECKPOINT_METADATA_CHARS = 256
MAX_REPLAN_RESULT_EXCERPT_CHARS = 4_000

MonotonicClock = Callable[[], float]
ReplanEvaluator = Callable[["CheckpointSnapshot"], Awaitable["ReplanDirective"]]
CheckpointObserver = Callable[[str, Mapping[str, Any]], None]


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    except (TypeError, ValueError):
        encoded = repr(dict(value)).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _bounded_text(value: Any) -> str:
    return str("" if value is None else value)[:MAX_CHECKPOINT_METADATA_CHARS]


def _bounded_result_excerpt(value: Any) -> str:
    return str("" if value is None else value)[:MAX_REPLAN_RESULT_EXCERPT_CHARS]


@dataclass(frozen=True)
class ReplanDirective:
    """One validated Replanning result returned to the active Execution loop."""

    checkpoint_id: str
    outcome: ReplanningOutcome
    active_plan_id: str

    def __post_init__(self) -> None:
        if not str(self.checkpoint_id or "").strip():
            raise ValueError("replan checkpoint_id is required")
        if not str(self.active_plan_id or "").strip():
            raise ValueError("replan active_plan_id is required")
        if not isinstance(self.outcome, ReplanningOutcome):
            raise TypeError("replan directive requires a typed ReplanningOutcome")

    @property
    def complete(self) -> bool:
        return self.outcome.completion_percent == 100

    def execution_control_message(self, *, requested_tool_executed: bool) -> str:
        boundary = (
            "The tool result above completed before this Replan."
            if requested_tool_executed
            else (
                "The requested tool was not executed because compulsory Replanning "
                "became due before admission."
            )
        )
        change = (
            f"changed because {self.outcome.change_reason}"
            if self.outcome.plan_changed
            else "did not change"
        )
        plan_json = json.dumps(
            dict(self.outcome.plan),
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        return (
            "HASHI_COMPULSORY_REPLAN\n"
            f"checkpoint_id: {self.checkpoint_id}\n"
            f"completion: {self.outcome.completion_percent}%\n"
            f"completion_basis: {self.outcome.completion_basis}\n"
            f"plan: {change}\n"
            f"active_plan_id: {self.active_plan_id}\n"
            f"active_plan: {plan_json}\n"
            f"next_step: {self.outcome.next_step}\n"
            f"{boundary} Continue from current workspace and evidence. Do not repeat "
            "a completed side effect merely because Replanning occurred."
        )


@dataclass(frozen=True)
class ToolAdmission:
    """One admitted tool call or one Replan control response replacing admission."""

    token: str
    prospective_action: Mapping[str, Any]
    admitted: bool = True
    directive: ReplanDirective | None = None


@dataclass(frozen=True)
class CheckpointSnapshot:
    """Cadence and evidence facts supplied to one compulsory Replanning call."""

    cycle_id: str
    checkpoint_id: str
    checkpoint_index: int
    trigger_reasons: tuple[str, ...]
    completed_result_count: int
    elapsed_s: float
    receipt_summaries: tuple[Mapping[str, Any], ...]
    receipt_set_sha256: str
    boundary_kind: str
    prospective_action: Mapping[str, Any] | None = None
    execution_candidate: Mapping[str, Any] | None = None

    def as_payload(self, *, include_result_excerpts: bool = False) -> dict[str, Any]:
        summaries: list[dict[str, Any]] = []
        for item in self.receipt_summaries:
            summary = dict(item)
            if not include_result_excerpts:
                summary.pop("result_excerpt", None)
            summaries.append(summary)
        payload = {
            "cycle_id": self.cycle_id,
            "checkpoint_id": self.checkpoint_id,
            "checkpoint_index": self.checkpoint_index,
            "trigger_reasons": list(self.trigger_reasons),
            "completed_result_count": self.completed_result_count,
            "elapsed_s": self.elapsed_s,
            "receipt_summaries": summaries,
            "receipt_set_sha256": self.receipt_set_sha256,
            "boundary_kind": self.boundary_kind,
            "prospective_action": (
                dict(self.prospective_action)
                if self.prospective_action is not None
                else None
            ),
        }
        if include_result_excerpts:
            payload["execution_candidate"] = (
                dict(self.execution_candidate)
                if self.execution_candidate is not None
                else None
            )
        else:
            payload["execution_candidate_present"] = (
                self.execution_candidate is not None
            )
        return payload

    def replan_payload(self) -> dict[str, Any]:
        return self.as_payload(include_result_excerpts=True)


class ReplanCompletionInterruption(BaseException):
    """Stop adding Execution work after compulsory Replanning establishes 100%."""

    def __init__(
        self,
        directive: ReplanDirective,
        snapshot: CheckpointSnapshot,
        receipts: Sequence[ToolEvidenceReceipt],
    ) -> None:
        if not directive.complete:
            raise ValueError("only a 100% Replan may complete Execution")
        super().__init__(f"compulsory Replan completed work: {snapshot.checkpoint_id}")
        self.directive = directive
        self.snapshot = snapshot
        self.receipts = tuple(receipts)


class CheckpointInfrastructureInterruption(BaseException):
    """Carry stop/audit/Replan failures through generic backend error wrappers."""

    def __init__(self, cause: BaseException) -> None:
        super().__init__(str(cause))
        self.cause = cause


class CompulsoryReplanCoordinator:
    """Coordinate compulsory Replanning within one authoritative Execution cycle."""

    def __init__(
        self,
        *,
        cycle_id: str,
        evaluator: ReplanEvaluator,
        observer: CheckpointObserver | None = None,
        clock: MonotonicClock = time.monotonic,
    ) -> None:
        value = str(cycle_id or "").strip()
        if not value:
            raise ValueError("replan cycle_id is required")
        self.cycle_id = value
        self._evaluator = evaluator
        self._observer = observer
        self._clock = clock
        self._condition = asyncio.Condition()
        self._window_started_at = float(clock())
        self._window_result_count = 0
        self._window_receipt_summaries: list[Mapping[str, Any]] = []
        self._seen_receipt_ids: set[str] = set()
        self._receipts: dict[str, ToolEvidenceReceipt] = {}
        self._active_admissions: dict[str, ToolAdmission] = {}
        self._admission_serial = 0
        self._checkpoint_count = 0
        self._checkpoint_task: asyncio.Task[ReplanDirective] | None = None
        self._terminal_interruption: ReplanCompletionInterruption | None = None
        self._latest_directive: ReplanDirective | None = None
        self._closed = False

    @property
    def checkpoint_count(self) -> int:
        return self._checkpoint_count

    @property
    def receipts(self) -> tuple[ToolEvidenceReceipt, ...]:
        return tuple(self._receipts.values())

    @property
    def latest_directive(self) -> ReplanDirective | None:
        """Return the latest completed Replan directive for plan-bound callers."""

        return self._latest_directive

    @property
    def completed_result_count(self) -> int:
        return self._window_result_count

    def elapsed_s(self) -> float:
        return max(0.0, float(self._clock()) - self._window_started_at)

    def due_reasons(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if self._window_result_count >= CHECKPOINT_RESULT_THRESHOLD:
            reasons.append("completed_result_count")
        if self.elapsed_s() >= CHECKPOINT_ELAPSED_THRESHOLD_S:
            reasons.append("elapsed_time")
        return tuple(reasons)

    async def before_tool(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, Any],
        tool_call_id: str,
    ) -> ToolAdmission:
        prospective = self._prospective_action(
            tool_name=tool_name,
            arguments=arguments,
            tool_call_id=tool_call_id,
        )
        task: asyncio.Task[ReplanDirective] | None = None
        async with self._condition:
            self._raise_if_unavailable_locked()
            if self._checkpoint_task is not None:
                task = self._checkpoint_task
            elif self.due_reasons():
                task = self._start_replan_locked(
                    prospective,
                    boundary_kind="before_tool_admission",
                )
            else:
                self._admission_serial += 1
                token = f"{self.cycle_id}:tool:{self._admission_serial}"
                admission = ToolAdmission(token, prospective)
                self._active_admissions[token] = admission
                return admission
        assert task is not None
        directive = await asyncio.shield(task)
        return ToolAdmission(
            token="",
            prospective_action=prospective,
            admitted=False,
            directive=directive,
        )

    async def abandon_tool(self, admission: ToolAdmission) -> None:
        if not admission.admitted:
            return
        async with self._condition:
            self._active_admissions.pop(admission.token, None)
            self._condition.notify_all()

    async def after_tool(
        self,
        admission: ToolAdmission,
        receipt: ToolEvidenceReceipt,
        *,
        result_summary: str = "",
        immediate_safety_result: bool = False,
    ) -> ReplanDirective | None:
        if not admission.admitted:
            raise RuntimeError("a non-admitted tool cannot produce a tool receipt")
        task: asyncio.Task[ReplanDirective] | None = None
        async with self._condition:
            if admission.token not in self._active_admissions:
                raise RuntimeError("replan tool admission was already settled")
            self._active_admissions.pop(admission.token, None)
            self._record_receipt_locked(receipt, result_summary=result_summary)
            self._condition.notify_all()
            if not immediate_safety_result:
                if self._checkpoint_task is not None:
                    task = self._checkpoint_task
                elif self.due_reasons():
                    task = self._start_replan_locked(
                        None,
                        boundary_kind="completed_tool_result",
                    )
        if task is None:
            return None
        directive = await asyncio.shield(task)
        return directive

    async def record_immediate_result(
        self,
        receipt: ToolEvidenceReceipt,
        *,
        result_summary: str = "",
    ) -> None:
        """Count a denial immediately without delaying its return to Execution."""

        async with self._condition:
            self._record_receipt_locked(receipt, result_summary=result_summary)
            self._condition.notify_all()
            self._raise_if_unavailable_locked()

    async def at_execution_completion(
        self,
        *,
        execution_candidate: Mapping[str, Any] | None,
    ) -> ReplanDirective | None:
        """Take a time/result-due Replan at the provider-completion safe boundary."""

        task: asyncio.Task[ReplanDirective] | None = None
        async with self._condition:
            self._raise_if_unavailable_locked()
            if self._checkpoint_task is not None:
                task = self._checkpoint_task
            elif self.due_reasons():
                task = self._start_replan_locked(
                    None,
                    boundary_kind="execution_completion",
                    execution_candidate=execution_candidate,
                )
        if task is None:
            return None
        return await asyncio.shield(task)

    async def close(self) -> None:
        task: asyncio.Task[ReplanDirective] | None = None
        async with self._condition:
            if self._closed:
                return
            self._closed = True
            task = self._checkpoint_task
            self._checkpoint_task = None
            self._condition.notify_all()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _raise_if_unavailable_locked(self) -> None:
        if self._terminal_interruption is not None:
            raise self._terminal_interruption
        if self._closed:
            raise asyncio.CancelledError("replan coordinator is closed")

    def _start_replan_locked(
        self,
        prospective: Mapping[str, Any] | None,
        *,
        boundary_kind: str,
        execution_candidate: Mapping[str, Any] | None = None,
    ) -> asyncio.Task[ReplanDirective]:
        if self._checkpoint_task is not None:
            return self._checkpoint_task
        task = asyncio.create_task(
            self._run_replan(
                prospective,
                boundary_kind=boundary_kind,
                execution_candidate=execution_candidate,
            ),
            name=f"her-v2-replan:{self.cycle_id}:{self._checkpoint_count + 1}",
        )
        self._checkpoint_task = task
        return task

    async def _run_replan(
        self,
        prospective: Mapping[str, Any] | None,
        *,
        boundary_kind: str,
        execution_candidate: Mapping[str, Any] | None,
    ) -> ReplanDirective:
        try:
            async with self._condition:
                while self._active_admissions and not self._closed:
                    await self._condition.wait()
                self._raise_if_unavailable_locked()
                self._checkpoint_count += 1
                snapshot = self._snapshot_locked(
                    prospective,
                    boundary_kind=boundary_kind,
                    execution_candidate=execution_candidate,
                )

            audit_payload = snapshot.as_payload()
            self._observe("replan_due", audit_payload)
            self._observe("replan_started", audit_payload)
            try:
                directive = await self._evaluator(snapshot)
                if not isinstance(directive, ReplanDirective):
                    raise TypeError("replan evaluator returned an untyped directive")
                if directive.checkpoint_id != snapshot.checkpoint_id:
                    raise ValueError("Replanning changed the checkpoint identity")
                self._latest_directive = directive
            except asyncio.CancelledError:
                raise
            except (TurnStopped, AuditPersistenceError) as exc:
                raise CheckpointInfrastructureInterruption(exc) from exc
            except CheckpointInfrastructureInterruption:
                raise
            except StageInvocationError as exc:
                raise CheckpointInfrastructureInterruption(exc) from exc
            except Exception as exc:  # noqa: BLE001 - typed control boundary
                failure = StageInvocationError(
                    f"compulsory Replanning failed: {type(exc).__name__}: {exc}",
                    retryable=False,
                    human_description=(
                        "The compulsory Replanning control failed unexpectedly."
                    ),
                )
                raise CheckpointInfrastructureInterruption(failure) from exc

            completed_payload = snapshot.as_payload()
            completed_payload.update(
                {
                    "completion_percent": directive.outcome.completion_percent,
                    "plan_changed": directive.outcome.plan_changed,
                    "change_reason_present": bool(directive.outcome.change_reason),
                    "next_step": directive.outcome.next_step,
                    "active_plan_id": directive.active_plan_id,
                    "commentary_required": True,
                }
            )
            self._observe("replan_completed", completed_payload)

            if directive.complete:
                interruption = ReplanCompletionInterruption(
                    directive,
                    snapshot,
                    self.receipts,
                )
                async with self._condition:
                    self._terminal_interruption = interruption
                    self._checkpoint_task = None
                    self._condition.notify_all()
                raise interruption

            async with self._condition:
                # Immediate denials can be returned while Replanning is in
                # progress. They were not part of this snapshot and begin the
                # fresh cadence window instead of being lost.
                assessed_count = snapshot.completed_result_count
                carried_summaries = self._window_receipt_summaries[assessed_count:]
                self._window_started_at = float(self._clock())
                self._window_result_count = len(carried_summaries)
                self._window_receipt_summaries = list(carried_summaries)
                self._checkpoint_task = None
                self._condition.notify_all()
            return directive
        except (ReplanCompletionInterruption, CheckpointInfrastructureInterruption):
            raise
        except AuditPersistenceError as exc:
            raise CheckpointInfrastructureInterruption(exc) from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - never flatten control failures
            raise CheckpointInfrastructureInterruption(exc) from exc

    def _observe(self, event: str, payload: Mapping[str, Any]) -> None:
        if self._observer is not None:
            self._observer(event, payload)

    def _snapshot_locked(
        self,
        prospective: Mapping[str, Any] | None,
        *,
        boundary_kind: str,
        execution_candidate: Mapping[str, Any] | None,
    ) -> CheckpointSnapshot:
        identities = sorted(self._seen_receipt_ids)
        receipt_set_sha256 = hashlib.sha256(
            "\n".join(identities).encode("utf-8")
        ).hexdigest()
        checkpoint_id = f"{self.cycle_id}:checkpoint:{self._checkpoint_count}"
        return CheckpointSnapshot(
            cycle_id=self.cycle_id,
            checkpoint_id=checkpoint_id,
            checkpoint_index=self._checkpoint_count,
            trigger_reasons=self.due_reasons(),
            completed_result_count=self._window_result_count,
            elapsed_s=round(self.elapsed_s(), 6),
            receipt_summaries=tuple(
                self._window_receipt_summaries[-MAX_CHECKPOINT_RECEIPT_SUMMARIES:]
            ),
            receipt_set_sha256=receipt_set_sha256,
            boundary_kind=boundary_kind,
            prospective_action=(dict(prospective) if prospective is not None else None),
            execution_candidate=(
                dict(execution_candidate) if execution_candidate is not None else None
            ),
        )

    def _record_receipt_locked(
        self,
        receipt: ToolEvidenceReceipt,
        *,
        result_summary: str,
    ) -> None:
        identity = self._receipt_identity(receipt)
        if identity in self._seen_receipt_ids or not receipt.completed:
            return
        self._seen_receipt_ids.add(identity)
        self._receipts[identity] = receipt
        self._window_result_count += 1
        summary: dict[str, Any] = {
            "evidence_ref": _bounded_text(receipt.evidence_ref),
            "invocation_id": _bounded_text(receipt.invocation_id),
            "attempt": receipt.attempt,
            "tool_call_id": _bounded_text(receipt.tool_call_id),
            "tool_name": _bounded_text(receipt.tool_name),
            "status": receipt.status.value,
            "read_only": receipt.read_only,
            "completed": receipt.completed,
            "output_sha256": _bounded_text(receipt.output_sha256),
            "result_excerpt": _bounded_result_excerpt(result_summary),
        }
        control_disposition = str(
            receipt.details.get("control_disposition") or ""
        ).strip()
        if control_disposition:
            summary["control_disposition"] = _bounded_text(control_disposition)
        self._window_receipt_summaries.append(summary)

    @staticmethod
    def _receipt_identity(receipt: ToolEvidenceReceipt) -> str:
        return "\x1f".join(
            (
                receipt.invocation_id,
                str(receipt.attempt),
                receipt.tool_call_id,
                receipt.evidence_ref,
            )
        )

    @staticmethod
    def _prospective_action(
        *,
        tool_name: str,
        arguments: Mapping[str, Any],
        tool_call_id: str,
    ) -> Mapping[str, Any]:
        return {
            "tool_name": _bounded_text(tool_name),
            "tool_call_id": _bounded_text(tool_call_id),
            "argument_keys": sorted(_bounded_text(key) for key in arguments)[
                :MAX_CHECKPOINT_ARGUMENT_KEYS
            ],
            "arguments_sha256": _canonical_sha256(arguments),
        }
