"""Durable HER v2 Habit, Meditation, and Dream support.

The orchestration core depends only on the small interfaces in ``interfaces``.
This module owns the concrete agent-local Python implementation used by the
HASHI compatibility adapter.  Meditation is journalled before it is queued,
Habit writes are deterministic and replay-safe, and Dream remains outside the
live turn lifecycle.

The on-disk Habit and journal formats deliberately remain compatible with the
earlier Python implementation.  Keeping those data formats is a migration
choice, not a dependency on the retired HER execution backend.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from adapters import her_dream, her_habits

from .audit import AuditPersistenceError, DurableAuditLog
from .interfaces import StageInvocationError
from .models import Stage, StageResponse, TerminalState


MaintenanceInvoker = Callable[
    [Stage, str, str, str, float], Awaitable[StageResponse]
]
NotificationSender = Callable[[dict[str, Any]], Awaitable[bool | None]]
ConfigGetter = Callable[[], her_habits.HabitMeditationConfig]


@dataclass(frozen=True)
class LearningRecovery:
    interrupted_meditations: int = 0
    resumed_meditations: int = 0
    recovered_dreams: int = 0
    resumed_notifications: int = 0


class HERv2Learning:
    """Concrete, restart-safe learning services for one Agent workspace."""

    def __init__(
        self,
        *,
        workspace_dir: Path,
        agent_name: str,
        config_getter: ConfigGetter,
        invoke_model: MaintenanceInvoker,
        audit_log: DurableAuditLog,
        notification_sender: NotificationSender | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.workspace_dir = Path(workspace_dir)
        self.agent_name = str(agent_name)
        self.config_getter = config_getter
        self.invoke_model = invoke_model
        self.audit_log = audit_log
        self.notification_sender = notification_sender
        self.logger = logger or logging.getLogger(
            f"HASHI.HERv2.Learning.{self.agent_name}"
        )

        self.store = her_habits.HERHabitStore(
            self.workspace_dir, logger=self.logger
        )
        self.meditation_journal = her_habits.HERMeditationJournal(
            self.workspace_dir,
            logger=self.logger,
        )
        self.dream_journal = her_dream.HERDreamJournal(
            self.workspace_dir, logger=self.logger
        )

        self.habit_execution_lock = asyncio.Lock()
        self.meditation_execution_lock = asyncio.Lock()
        self.dream_execution_lock = asyncio.Lock()
        self.dream_run_lock = asyncio.Lock()
        self.meditation_tasks: set[asyncio.Task] = set()
        self.meditation_job_ids: set[str] = set()
        self.notification_tasks: set[asyncio.Task] = set()
        self.notification_job_ids: set[str] = set()
        # Dream command execution registers its current task in this set.
        self.dream_tasks: set[asyncio.Task] = set()

    def bind_turn(
        self,
        *,
        learning_eligible: bool = True,
        notification_context: Mapping[str, Any] | None = None,
    ) -> "HERv2TurnLearning":
        return HERv2TurnLearning(
            owner=self,
            learning_eligible=bool(learning_eligible),
            notification_context=dict(notification_context or {}),
        )

    async def retrieve(self, *, goal: str, turn_id: str) -> Sequence[str]:
        config = self.config_getter()
        if not config.enabled:
            return ()
        # Bridge-managed conversation context is useful to the live turn, but
        # it is not Habit-retrieval authority.  Match only against the current
        # authoritative request, using the legacy bounded extraction contract.
        current_request = her_habits.extract_current_request(goal)
        selected = self.store.retrieve(
            current_request, limit=config.retrieval_limit
        )
        context = her_habits.render_habit_advisory_context(selected)
        ref = self._audit(
            event_id=f"{turn_id}:habits:planning-retrieval",
            turn_id=turn_id,
            request_ref=f"hashi-turn:{turn_id}",
            stage=Stage.PLANNING.value,
            event="habit_planning_retrieval",
            payload={
                "selected_habit_ids": [habit.habit_id for habit in selected],
                "selected_count": len(selected),
                "planning_only": True,
                "authority": "advisory",
            },
        )
        # The runtime stores only a compact log reference in its Ledger.  The
        # retrieval itself remains independently auditable here.
        del ref
        return (context,) if context else ()

    async def enqueue_meditation(
        self,
        *,
        turn_id: str,
        goal: str,
        summary: str,
        evidence_refs: Sequence[str],
        limitations: Sequence[str],
        terminal_state: TerminalState,
        notification_context: Mapping[str, Any] | None = None,
    ) -> None:
        config = self.config_getter()
        if not config.enabled:
            return

        evidence_text = ", ".join(str(item) for item in evidence_refs if str(item))
        result = SimpleNamespace(
            stdout="",
            text=(
                str(summary or "").strip()
                + (f"\nEvidence references: {evidence_text}" if evidence_text else "")
            ),
            stderr="\n".join(str(item) for item in limitations if str(item)),
            tool_uses=[],
            tool_results=[],
            completion_status=terminal_state.value,
            stop_reason=terminal_state.value.casefold(),
        )
        prompt = her_habits.build_meditation_prompt(
            agent_name=self.agent_name,
            # Meditation is turn-based.  Do not persist Bridge conversation
            # background into the durable learning job for this turn.
            task_prompt=her_habits.extract_current_request(goal),
            result=result,
            habits=self.store.load(),
            config=config,
        )
        job_id = hashlib.sha256(
            f"her-v2-meditation\0{turn_id}".encode("utf-8")
        ).hexdigest()[:32]
        self._audit(
            event_id=f"{turn_id}:meditation:enqueue-intent",
            turn_id=turn_id,
            request_ref=f"hashi-turn:{turn_id}",
            stage=Stage.MEDITATION.value,
            event="meditation_enqueue_intent",
            payload={
                "job_id": job_id,
                "terminal_state": terminal_state.value,
                "evidence_refs": list(evidence_refs),
                "limitations": list(limitations),
            },
        )
        _identity, queued = self.meditation_journal.enqueue(
            job_id=job_id,
            request_id=turn_id,
            prompt=prompt,
            max_actions=config.max_actions,
            notification_context=notification_context,
        )
        self._audit(
            event_id=f"{turn_id}:meditation:enqueued",
            turn_id=turn_id,
            request_ref=f"hashi-turn:{turn_id}",
            stage=Stage.MEDITATION.value,
            event="meditation_enqueued",
            payload={"job_id": job_id, "new_job": queued},
        )
        current = self.meditation_journal.get(job_id)
        if current and current.get("status") in {"pending", "applying"}:
            self.spawn_meditation(job_id, config=config)

    def recover(self) -> LearningRecovery:
        recovered_dreams = her_dream.recover_interrupted_runs(
            store=self.store,
            journal=self.dream_journal,
        )
        interrupted = self.meditation_journal.recover_interrupted_jobs()
        resumed = 0
        if self.config_getter().enabled:
            resumed = sum(
                self.spawn_meditation(job["job_id"])
                for job in self.meditation_journal.pending_jobs(limit=16)
            )
        notifications = self.resume_notifications()
        if interrupted or resumed or recovered_dreams or notifications:
            self._audit(
                event_id=f"her-v2-learning:startup-recovery:{uuid.uuid4().hex}",
                turn_id="startup-recovery",
                request_ref="hashi-process:startup",
                stage="recovery",
                event="learning_recovery_completed",
                payload={
                    "interrupted_meditations": interrupted,
                    "resumed_meditations": resumed,
                    "recovered_dreams": recovered_dreams,
                    "resumed_notifications": notifications,
                },
            )
        return LearningRecovery(
            interrupted_meditations=interrupted,
            resumed_meditations=resumed,
            recovered_dreams=recovered_dreams,
            resumed_notifications=notifications,
        )

    def spawn_meditation(
        self,
        job_id: str,
        *,
        config: her_habits.HabitMeditationConfig | None = None,
    ) -> bool:
        if job_id in self.meditation_job_ids:
            return False
        selected_config = config or self.config_getter()
        task = asyncio.create_task(
            self._run_meditation(job_id, selected_config),
            name=f"her-v2-meditation:{self.agent_name}:{job_id}",
        )
        self.meditation_job_ids.add(job_id)
        self.meditation_tasks.add(task)

        def _done(done_task: asyncio.Task) -> None:
            self.meditation_tasks.discard(done_task)
            self.meditation_job_ids.discard(job_id)
            if done_task.cancelled():
                return
            try:
                error = done_task.exception()
            except Exception:
                error = None
            if error is not None:
                self.logger.warning(
                    "Unhandled HER v2 Meditation task error: job=%s error=%s",
                    job_id,
                    type(error).__name__,
                )

        task.add_done_callback(_done)
        return True

    async def _run_meditation(
        self,
        job_id: str,
        config: her_habits.HabitMeditationConfig,
    ) -> None:
        job = self.meditation_journal.get(job_id)
        if job is None:
            return
        turn_id = str(job.get("request_id") or job_id)
        request_ref = f"hashi-turn:{turn_id}"
        try:
            async with self.meditation_execution_lock:
                if not self.config_getter().enabled:
                    return
                phase = self.meditation_journal.claim(job_id)
                if phase is None:
                    return
                job = self.meditation_journal.get(job_id)
                if job is None:
                    return
                execution_attempt = int(job.get("attempts") or 1)
                if phase == "meditate":
                    prompt = str(job.get("prompt") or "")
                    actions: list[Mapping[str, Any]] | None = None
                    validation_attempt = 0
                    validation_idle_started = time.monotonic()
                    while actions is None:
                        validation_attempt += 1
                        self._audit(
                            event_id=(
                                f"{turn_id}:meditation:{job_id}:"
                                f"run:{execution_attempt}:repair:{validation_attempt}:start"
                            ),
                            turn_id=turn_id,
                            request_ref=request_ref,
                            stage=Stage.MEDITATION.value,
                            event="stage_started",
                            attempt=validation_attempt,
                            payload={
                                "job_id": job_id,
                                "allow_tools": False,
                                "allow_side_effects": False,
                            },
                        )
                        response = await self.invoke_model(
                            Stage.MEDITATION,
                            prompt,
                            turn_id,
                            f"{job_id}:attempt:{validation_attempt}",
                            config.meditation_idle_timeout_seconds,
                        )
                        self.audit_log.record_reasoning(
                            event_id=(
                                f"{turn_id}:meditation:{job_id}:"
                                f"run:{execution_attempt}:repair:{validation_attempt}:reasoning"
                            ),
                            turn_id=turn_id,
                            request_ref=request_ref,
                            stage=Stage.MEDITATION.value,
                            role="meditation",
                            provider=response.provider,
                            model=response.model,
                            attempt=validation_attempt,
                            plan_id=None,
                            trace=response.reasoning_trace,
                        )
                        self._audit(
                            event_id=(
                                f"{turn_id}:meditation:{job_id}:"
                                f"run:{execution_attempt}:repair:{validation_attempt}:complete"
                            ),
                            turn_id=turn_id,
                            request_ref=request_ref,
                            stage=Stage.MEDITATION.value,
                            event="stage_completed",
                            provider=response.provider,
                            model=response.model,
                            attempt=validation_attempt,
                            payload={"job_id": job_id, "output": response.text},
                        )
                        try:
                            actions = her_habits.parse_meditation_actions(
                                response.text,
                                max_actions=int(
                                    job.get("max_actions") or config.max_actions
                                ),
                            )
                            break
                        except her_habits.MeditationValidationError as exc:
                            idle_for = time.monotonic() - validation_idle_started
                            if idle_for >= float(
                                config.meditation_idle_timeout_seconds
                            ):
                                raise
                            prompt = her_habits.build_meditation_correction_prompt(
                                rejected_output=response.text,
                                error=exc,
                            )
                            await asyncio.sleep(
                                min(
                                    5.0,
                                    0.25 * (2 ** min(validation_attempt - 1, 5)),
                                    max(
                                        0.0,
                                        float(
                                            config.meditation_idle_timeout_seconds
                                        )
                                        - idle_for,
                                    ),
                                )
                            )
                    async with self.habit_execution_lock:
                        baseline = self.store.capture_action_baseline(
                            actions,
                            max_actions=int(
                                job.get("max_actions") or config.max_actions
                            ),
                            idempotency_key=job_id,
                        )
                        job = self.meditation_journal.store_actions(
                            job_id,
                            actions,
                            action_baseline=baseline,
                        )

                actions = job.get("actions")
                if not isinstance(actions, list):
                    raise her_habits.MeditationValidationError(
                        "durable Meditation actions are missing"
                    )
                # Required audit is durable before any Habit side effect begins.
                self._audit(
                    event_id=f"{turn_id}:meditation:{job_id}:write-authorised",
                    turn_id=turn_id,
                    request_ref=request_ref,
                    stage=Stage.MEDITATION.value,
                    event="habit_write_authorised",
                    payload={"job_id": job_id, "action_count": len(actions)},
                )
                async with self.habit_execution_lock:
                    outcomes, changes = self.store.apply_actions_with_changes(
                        actions,
                        max_actions=int(job.get("max_actions") or config.max_actions),
                        idempotency_key=job_id,
                        audit_context={
                            "source": "her_v2_meditation",
                            "job_id": job_id,
                            "turn_id": turn_id,
                        },
                        action_baseline=job.get("action_baseline"),
                    )
                    self.meditation_journal.mark_complete(
                        job_id,
                        outcomes,
                        changes=[change.to_payload() for change in changes],
                    )
                self._audit(
                    event_id=f"{turn_id}:meditation:{job_id}:completed",
                    turn_id=turn_id,
                    request_ref=request_ref,
                    stage=Stage.MEDITATION.value,
                    event="meditation_completed",
                    payload={
                        "job_id": job_id,
                        "outcomes": outcomes,
                        "changes": [change.to_payload() for change in changes],
                    },
                )
            self.spawn_notification(job_id)
        except asyncio.CancelledError:
            try:
                self.meditation_journal.mark_pending(
                    job_id, reason="runtime_shutdown"
                )
            except Exception:
                pass
            raise
        except her_habits.MeditationValidationError as exc:
            self.meditation_journal.mark_failed(
                job_id,
                error_code="invalid_output",
                error_summary=str(exc),
            )
            self._audit_failure(turn_id, job_id, exc, code="invalid_output")
        except AuditPersistenceError as exc:
            try:
                self.meditation_journal.mark_pending(
                    job_id, reason="audit_persistence_failure"
                )
            except Exception:
                pass
            self.logger.error(
                "HER v2 Meditation stopped before an unaudited write: job=%s error=%s",
                job_id,
                exc,
            )
        except StageInvocationError as exc:
            try:
                if exc.retryable:
                    self.meditation_journal.mark_pending(
                        job_id, reason="provider_retryable_error"
                    )
                else:
                    self.meditation_journal.mark_failed(
                        job_id,
                        error_code="provider_error",
                        error_summary=str(exc),
                    )
            except Exception:
                pass
            self._audit_failure(turn_id, job_id, exc, code="provider_error")
        except (OSError, asyncio.TimeoutError) as exc:
            try:
                self.meditation_journal.mark_pending(
                    job_id, reason=type(exc).__name__
                )
            except Exception:
                pass
            self._audit_failure(turn_id, job_id, exc, code="retryable_error")
        except Exception as exc:
            try:
                self.meditation_journal.mark_failed(
                    job_id,
                    error_code="runtime_error",
                    error_summary=f"{type(exc).__name__}: {exc}",
                )
            except Exception:
                pass
            self._audit_failure(turn_id, job_id, exc, code="runtime_error")

    def _audit_failure(
        self, turn_id: str, job_id: str, error: Exception, *, code: str
    ) -> None:
        try:
            self._audit(
                event_id=f"{turn_id}:meditation:{job_id}:failed:{code}",
                turn_id=turn_id,
                request_ref=f"hashi-turn:{turn_id}",
                stage=Stage.MEDITATION.value,
                event="meditation_failed",
                payload={
                    "job_id": job_id,
                    "error_code": code,
                    "error_type": type(error).__name__,
                    "error": str(error),
                },
            )
        except AuditPersistenceError:
            self.logger.error(
                "HER v2 Meditation failure audit was unavailable: job=%s", job_id
            )

    def spawn_notification(self, job_id: str) -> bool:
        if self.notification_sender is None or job_id in self.notification_job_ids:
            return False
        task = asyncio.create_task(
            self._run_notification(job_id),
            name=f"her-v2-habit-notification:{self.agent_name}:{job_id}",
        )
        self.notification_job_ids.add(job_id)
        self.notification_tasks.add(task)

        def _done(done_task: asyncio.Task) -> None:
            self.notification_tasks.discard(done_task)
            self.notification_job_ids.discard(job_id)

        task.add_done_callback(_done)
        return True

    def resume_notifications(self) -> int:
        return sum(
            self.spawn_notification(job["job_id"])
            for job in self.meditation_journal.pending_notifications(limit=32)
        )

    async def _run_notification(self, job_id: str) -> None:
        assert self.notification_sender is not None
        idle_started = time.monotonic()
        while True:
            job = self.meditation_journal.claim_notification(job_id)
            if job is None:
                return
            turn_id = str(job.get("request_id") or job_id)
            try:
                self._audit(
                    event_id=(
                        f"{turn_id}:habit-notification:{job_id}:"
                        f"attempt:{(job.get('notification') or {}).get('attempts', 1)}:start"
                    ),
                    turn_id=turn_id,
                    request_ref=f"hashi-turn:{turn_id}",
                    stage="habit_notification",
                    event="habit_notification_delivery_started",
                    payload={"job_id": job_id, "changes": job.get("changes") or []},
                )
                delivered = await self.notification_sender(job)
                if delivered is None:
                    self.meditation_journal.mark_notification_deferred(
                        job_id,
                        reason="delivery temporarily unavailable",
                    )
                    self._record_notification_audits(
                        job,
                        "habit_notification_deferred",
                    )
                    return
                if delivered is not True:
                    raise RuntimeError("Habit notification was not accepted")
                self.meditation_journal.mark_notification_sent(job_id)
                self._record_notification_audits(job, "habit_notification_sent")
                return
            except asyncio.CancelledError:
                try:
                    self.meditation_journal.mark_notification_retry(
                        job_id, reason="runtime_shutdown"
                    )
                except Exception:
                    pass
                raise
            except Exception as exc:
                self.meditation_journal.mark_notification_retry(
                    job_id, reason=f"{type(exc).__name__}: {exc}"
                )
                self._record_notification_audits(
                    job,
                    "habit_notification_failed",
                    error=f"{type(exc).__name__}: {exc}",
                )
                current = self.meditation_journal.get(job_id) or {}
                notification = current.get("notification") or {}
                if notification.get("status") != "pending":
                    return
                idle_window = float(
                    self.config_getter().meditation_idle_timeout_seconds
                )
                if time.monotonic() - idle_started >= idle_window:
                    return
                await asyncio.sleep(
                    min(
                        10.0,
                        2.0 ** int(notification.get("attempts") or 1),
                        max(0.0, idle_window - (time.monotonic() - idle_started)),
                    )
                )

    def _record_notification_audits(
        self,
        job: Mapping[str, Any],
        event: str,
        *,
        error: str = "",
    ) -> None:
        job_id = str(job.get("job_id") or "unknown")
        turn_id = str(job.get("request_id") or job_id)
        current = self.meditation_journal.get(job_id) or dict(job)
        notification = current.get("notification") or {}
        payload = {
            "job_id": job_id,
            "changes": current.get("changes") or [],
            "notification": notification,
            **({"error": error} if error else {}),
        }
        self._audit(
            event_id=(
                f"{turn_id}:habit-notification:{job_id}:"
                f"attempt:{notification.get('attempts', 0)}:{event}"
            ),
            turn_id=turn_id,
            request_ref=f"hashi-turn:{turn_id}",
            stage="habit_notification",
            event=event,
            payload=payload,
        )
        try:
            her_habits.append_habit_audit(
                self.workspace_dir,
                event,
                agent_id=self.agent_name,
                request_id=turn_id,
                **payload,
            )
        except Exception as exc:
            self.logger.warning(
                "HER v2 Habit notification compatibility audit failed: job=%s error=%s",
                job_id,
                type(exc).__name__,
            )

    async def pause_meditations(self) -> int:
        tasks = [task for task in self.meditation_tasks if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return len(tasks)

    async def shutdown(self) -> None:
        tasks = [
            task
            for task in (
                *self.meditation_tasks,
                *self.notification_tasks,
                *self.dream_tasks,
            )
            if not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _audit(
        self,
        *,
        event_id: str,
        turn_id: str,
        request_ref: str,
        stage: str,
        event: str,
        payload: Mapping[str, Any],
        provider: str = "",
        model: str = "",
        attempt: int = 1,
    ) -> str:
        return self.audit_log.append(
            event_id=event_id,
            turn_id=turn_id,
            request_ref=request_ref,
            stage=stage,
            role="her_v2_learning",
            event=event,
            provider=provider,
            model=model,
            attempt=attempt,
            payload=payload,
        )


@dataclass(frozen=True)
class HERv2TurnLearning:
    """Request-bound facade carrying delivery context into Meditation."""

    owner: HERv2Learning
    learning_eligible: bool
    notification_context: Mapping[str, Any]

    async def retrieve(self, *, goal: str, turn_id: str) -> Sequence[str]:
        if not self.learning_eligible:
            return ()
        return await self.owner.retrieve(goal=goal, turn_id=turn_id)

    async def meditate(
        self,
        *,
        turn_id: str,
        goal: str,
        summary: str,
        evidence_refs: Sequence[str],
        limitations: Sequence[str],
        terminal_state: TerminalState,
    ) -> None:
        if not self.learning_eligible:
            return
        await self.owner.enqueue_meditation(
            turn_id=turn_id,
            goal=goal,
            summary=summary,
            evidence_refs=evidence_refs,
            limitations=limitations,
            terminal_state=terminal_state,
            notification_context=self.notification_context,
        )


__all__ = ["HERv2Learning", "HERv2TurnLearning", "LearningRecovery"]
