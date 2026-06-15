from __future__ import annotations

from dataclasses import dataclass

from orchestrator.enterprise.audit_ledger import EnterpriseAuditLedger, LedgerEvent
from orchestrator.enterprise.tasks import EnterpriseTask, TaskRegistry, TaskStatus


@dataclass(frozen=True)
class FailedTaskEscalation:
    task: EnterpriseTask
    event: LedgerEvent


def record_failed_task_escalation(
    ledger: EnterpriseAuditLedger,
    task: EnterpriseTask,
    *,
    actor_id: str | None = None,
    reason: str | None = None,
    escalation_target: str | None = None,
    severity: str = "high",
) -> LedgerEvent:
    if task.status != TaskStatus.FAILED.value:
        raise ValueError(f"task must be failed before escalation: {task.id}")

    context = {
        "failed_reason": reason or task.failed_reason,
        "prompt_summary": task.prompt_summary,
        "agent_id": task.agent_id,
        "user_id": task.user_id,
        "severity": severity,
    }
    if escalation_target:
        context["escalation_target"] = escalation_target

    return ledger.append(
        event_type="task",
        action="task.escalate_failed",
        status="open",
        actor_id=actor_id or task.agent_id or task.user_id,
        project_id=task.project_id,
        task_id=task.id,
        context=context,
    )


def transition_task_with_failure_escalation(
    tasks: TaskRegistry,
    ledger: EnterpriseAuditLedger,
    task_id: str,
    *,
    failed_reason: str,
    actor_id: str | None = None,
    escalation_target: str | None = None,
    severity: str = "high",
) -> FailedTaskEscalation:
    task = tasks.transition_task(task_id, TaskStatus.FAILED, failed_reason=failed_reason)
    event = record_failed_task_escalation(
        ledger,
        task,
        actor_id=actor_id,
        reason=failed_reason,
        escalation_target=escalation_target,
        severity=severity,
    )
    return FailedTaskEscalation(task=task, event=event)
