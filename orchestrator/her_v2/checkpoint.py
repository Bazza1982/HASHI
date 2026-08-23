"""Request-local high-risk Execution checkpoint coordination.

The coordinator observes safe Tool Gateway boundaries only.  It never applies
an elapsed deadline to a provider or tool operation and never caps the number
of tool results an Execution cycle may produce.
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
from .models import (
    CheckpointDecision,
    CheckpointFinding,
    ToolEvidenceReceipt,
)


CHECKPOINT_RESULT_THRESHOLD = 10
CHECKPOINT_ELAPSED_THRESHOLD_S = 300.0
MAX_CHECKPOINT_RECEIPT_SUMMARIES = 64
MAX_CHECKPOINT_ARGUMENT_KEYS = 32
MAX_CHECKPOINT_METADATA_CHARS = 256

MonotonicClock = Callable[[], float]
CheckpointEvaluator = Callable[["CheckpointSnapshot"], Awaitable[CheckpointFinding]]
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


@dataclass(frozen=True)
class ToolAdmission:
    """One already-admitted tool call in the current Execution cycle."""

    token: str
    prospective_action: Mapping[str, Any]


@dataclass(frozen=True)
class CheckpointSnapshot:
    """Bounded cadence and receipt facts supplied to one assessment."""

    cycle_id: str
    checkpoint_index: int
    trigger_reasons: tuple[str, ...]
    completed_result_count: int
    elapsed_s: float
    receipt_summaries: tuple[Mapping[str, Any], ...]
    receipt_set_sha256: str
    prospective_action: Mapping[str, Any] | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "cycle_id": self.cycle_id,
            "checkpoint_index": self.checkpoint_index,
            "trigger_reasons": list(self.trigger_reasons),
            "completed_result_count": self.completed_result_count,
            "elapsed_s": self.elapsed_s,
            "receipt_summaries": [dict(item) for item in self.receipt_summaries],
            "receipt_set_sha256": self.receipt_set_sha256,
            "prospective_action": (
                dict(self.prospective_action)
                if self.prospective_action is not None
                else None
            ),
        }


class CheckpointInterruption(BaseException):
    """Typed control path that generic provider error wrappers must not flatten."""

    def __init__(
        self,
        finding: CheckpointFinding,
        snapshot: CheckpointSnapshot,
        receipts: Sequence[ToolEvidenceReceipt],
        *,
        evaluator_failure: StageInvocationError | None = None,
    ) -> None:
        if finding.decision is CheckpointDecision.CONTINUE:
            raise ValueError("CONTINUE cannot interrupt Execution")
        super().__init__(f"checkpoint {finding.decision.value}: {finding.summary}")
        self.finding = finding
        self.snapshot = snapshot
        self.receipts = tuple(receipts)
        self.evaluator_failure = evaluator_failure


class CheckpointInfrastructureInterruption(BaseException):
    """Carry stop/audit control through generic backend ``except Exception`` code."""

    def __init__(self, cause: BaseException) -> None:
        super().__init__(str(cause))
        self.cause = cause


class HighRiskCheckpointCoordinator:
    """Coordinate one HIGH_RISK authoritative Execution cycle.

    A checkpoint is observed at the next safe boundary once either fixed
    cadence threshold is due.  Existing active calls may settle, while new
    admission remains closed behind a single shared evaluator task.
    """

    def __init__(
        self,
        *,
        cycle_id: str,
        evaluator: CheckpointEvaluator,
        observer: CheckpointObserver | None = None,
        clock: MonotonicClock = time.monotonic,
    ) -> None:
        value = str(cycle_id or "").strip()
        if not value:
            raise ValueError("checkpoint cycle_id is required")
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
        self._checkpoint_task: asyncio.Task[CheckpointFinding] | None = None
        self._terminal_interruption: CheckpointInterruption | None = None
        self._closed = False

    @property
    def checkpoint_count(self) -> int:
        return self._checkpoint_count

    @property
    def receipts(self) -> tuple[ToolEvidenceReceipt, ...]:
        return tuple(self._receipts.values())

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
        while True:
            task: asyncio.Task[CheckpointFinding] | None = None
            async with self._condition:
                self._raise_if_unavailable_locked()
                if self._checkpoint_task is not None:
                    task = self._checkpoint_task
                elif self.due_reasons():
                    task = self._start_checkpoint_locked(prospective)
                else:
                    self._admission_serial += 1
                    token = f"{self.cycle_id}:tool:{self._admission_serial}"
                    admission = ToolAdmission(token, prospective)
                    self._active_admissions[token] = admission
                    return admission
            assert task is not None
            await asyncio.shield(task)

    async def abandon_tool(self, admission: ToolAdmission) -> None:
        async with self._condition:
            self._active_admissions.pop(admission.token, None)
            self._condition.notify_all()

    async def after_tool(
        self,
        admission: ToolAdmission,
        receipt: ToolEvidenceReceipt,
        *,
        immediate_safety_result: bool = False,
    ) -> None:
        task: asyncio.Task[CheckpointFinding] | None = None
        async with self._condition:
            if admission.token not in self._active_admissions:
                raise RuntimeError("checkpoint tool admission was already settled")
            self._active_admissions.pop(admission.token, None)
            self._record_receipt_locked(receipt)
            self._condition.notify_all()
            if not immediate_safety_result:
                if self._checkpoint_task is not None:
                    task = self._checkpoint_task
                elif self.due_reasons():
                    task = self._start_checkpoint_locked(None)
        if task is not None:
            await asyncio.shield(task)

    async def record_immediate_result(self, receipt: ToolEvidenceReceipt) -> None:
        """Count a denial immediately without delaying its return to the model."""

        async with self._condition:
            self._record_receipt_locked(receipt)
            # A due denial is released now.  If Execution continues, the next
            # admission observes the due state and runs the checkpoint first.
            self._condition.notify_all()
            # Preserve the completed denial receipt even when a concurrent
            # checkpoint has already selected a terminal control outcome.
            self._raise_if_unavailable_locked()

    async def close(self) -> None:
        task: asyncio.Task[CheckpointFinding] | None = None
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
            raise asyncio.CancelledError("checkpoint coordinator is closed")

    def _start_checkpoint_locked(
        self, prospective: Mapping[str, Any] | None
    ) -> asyncio.Task[CheckpointFinding]:
        if self._checkpoint_task is not None:
            return self._checkpoint_task
        task = asyncio.create_task(
            self._run_checkpoint(prospective),
            name=f"her-v2-checkpoint:{self.cycle_id}:{self._checkpoint_count + 1}",
        )
        self._checkpoint_task = task
        return task

    async def _run_checkpoint(
        self, prospective: Mapping[str, Any] | None
    ) -> CheckpointFinding:
        try:
            async with self._condition:
                while self._active_admissions and not self._closed:
                    await self._condition.wait()
                self._raise_if_unavailable_locked()
                self._checkpoint_count += 1
                snapshot = self._snapshot_locked(prospective)

            self._observe("checkpoint_due", snapshot.as_payload())
            self._observe("checkpoint_started", snapshot.as_payload())
            evaluator_failure: StageInvocationError | None = None
            try:
                finding = await self._evaluator(snapshot)
                if not isinstance(finding, CheckpointFinding):
                    raise TypeError("checkpoint evaluator returned an untyped finding")
            except asyncio.CancelledError:
                raise
            except (TurnStopped, AuditPersistenceError) as exc:
                raise CheckpointInfrastructureInterruption(exc) from exc
            except StageInvocationError as exc:
                evaluator_failure = exc
                finding = CheckpointFinding(
                    CheckpointDecision.HALT,
                    "The high-risk checkpoint evaluator was unavailable after its "
                    "normal recovery path; further tool admission stopped.",
                )
            except Exception as exc:  # noqa: BLE001 - fail closed at control boundary
                evaluator_failure = StageInvocationError(
                    f"checkpoint evaluator failed: {type(exc).__name__}: {exc}",
                    retryable=False,
                    human_description=(
                        "The high-risk checkpoint evaluator failed unexpectedly."
                    ),
                )
                finding = CheckpointFinding(
                    CheckpointDecision.HALT,
                    "The high-risk checkpoint evaluator failed unexpectedly; "
                    "further tool admission stopped.",
                )

            completed_payload = snapshot.as_payload()
            completed_payload.update(
                {
                    "decision": finding.decision.value,
                    "summary": finding.summary,
                    "question_present": bool(finding.question),
                    "evaluator_failure": (
                        evaluator_failure.audit_payload()
                        if evaluator_failure is not None
                        else None
                    ),
                }
            )
            self._observe("checkpoint_completed", completed_payload)

            if finding.decision is CheckpointDecision.CONTINUE:
                async with self._condition:
                    # Provider-front-door denials remain immediately
                    # reportable while assessment is running.  They were not
                    # part of this snapshot, so carry them into the fresh
                    # cadence window instead of clearing their count.
                    assessed_count = snapshot.completed_result_count
                    carried_summaries = self._window_receipt_summaries[assessed_count:]
                    self._window_started_at = float(self._clock())
                    self._window_result_count = len(carried_summaries)
                    self._window_receipt_summaries = list(carried_summaries)
                    self._checkpoint_task = None
                    self._condition.notify_all()
                return finding

            interruption = CheckpointInterruption(
                finding,
                snapshot,
                self.receipts,
                evaluator_failure=evaluator_failure,
            )
            async with self._condition:
                self._terminal_interruption = interruption
                self._checkpoint_task = None
                self._condition.notify_all()
            raise interruption
        except (CheckpointInterruption, CheckpointInfrastructureInterruption):
            raise
        except AuditPersistenceError as exc:
            raise CheckpointInfrastructureInterruption(exc) from exc
        except Exception as exc:  # noqa: BLE001 - never flatten control failures
            raise CheckpointInfrastructureInterruption(exc) from exc

    def _observe(self, event: str, payload: Mapping[str, Any]) -> None:
        if self._observer is not None:
            self._observer(event, payload)

    def _snapshot_locked(
        self, prospective: Mapping[str, Any] | None
    ) -> CheckpointSnapshot:
        identities = sorted(self._seen_receipt_ids)
        receipt_set_sha256 = hashlib.sha256(
            "\n".join(identities).encode("utf-8")
        ).hexdigest()
        return CheckpointSnapshot(
            cycle_id=self.cycle_id,
            checkpoint_index=self._checkpoint_count,
            trigger_reasons=self.due_reasons(),
            completed_result_count=self._window_result_count,
            elapsed_s=round(self.elapsed_s(), 6),
            receipt_summaries=tuple(
                self._window_receipt_summaries[-MAX_CHECKPOINT_RECEIPT_SUMMARIES:]
            ),
            receipt_set_sha256=receipt_set_sha256,
            prospective_action=(dict(prospective) if prospective is not None else None),
        )

    def _record_receipt_locked(self, receipt: ToolEvidenceReceipt) -> None:
        identity = self._receipt_identity(receipt)
        if identity in self._seen_receipt_ids:
            return
        if not receipt.completed:
            return
        self._seen_receipt_ids.add(identity)
        self._receipts[identity] = receipt
        self._window_result_count += 1
        summary = {
            "evidence_ref": _bounded_text(receipt.evidence_ref),
            "invocation_id": _bounded_text(receipt.invocation_id),
            "attempt": receipt.attempt,
            "tool_call_id": _bounded_text(receipt.tool_call_id),
            "tool_name": _bounded_text(receipt.tool_name),
            "status": receipt.status.value,
            "read_only": receipt.read_only,
            "completed": receipt.completed,
            "output_sha256": _bounded_text(receipt.output_sha256),
            "summary": _bounded_text(
                f"{receipt.tool_name} completed with {receipt.status.value}"
            ),
        }
        control_disposition = str(
            receipt.details.get("control_disposition") or ""
        ).strip()
        if control_disposition in {
            "approval_required",
            "denied",
            "deny",
            "user_input_required",
        }:
            summary["control_disposition"] = control_disposition
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
