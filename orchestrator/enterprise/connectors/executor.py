from __future__ import annotations

from dataclasses import dataclass

from orchestrator.enterprise.audit_ledger import EnterpriseAuditLedger
from orchestrator.enterprise.connectors.base import ConnectorAction, ConnectorResult, record_connector_event
from orchestrator.enterprise.connectors.gate import ConnectorGateResult, evaluate_connector_action
from orchestrator.enterprise.connectors.registry import ConnectorRegistry
from orchestrator.enterprise.credentials import ConnectorCredentialStore
from orchestrator.enterprise.data_governance import (
    DataEgressDecision,
    DataGovernanceAssessment,
    DataGovernancePolicy,
    assess_data_egress,
)
from orchestrator.enterprise.policy import PolicyEvaluator


@dataclass(frozen=True)
class ConnectorExecution:
    result: ConnectorResult
    gate: ConnectorGateResult
    data_governance: DataGovernanceAssessment | None = None


class ConnectorExecutionService:
    def __init__(
        self,
        *,
        registry: ConnectorRegistry,
        credential_store: ConnectorCredentialStore,
        policy_evaluator: PolicyEvaluator,
        ledger: EnterpriseAuditLedger | None = None,
        data_governance_policy: DataGovernancePolicy | None = None,
    ):
        self.registry = registry
        self.credential_store = credential_store
        self.policy_evaluator = policy_evaluator
        self.ledger = ledger
        self.data_governance_policy = data_governance_policy or DataGovernancePolicy()

    def execute(self, action: ConnectorAction, *, credential_id: str) -> ConnectorExecution:
        gate = evaluate_connector_action(
            policy_evaluator=self.policy_evaluator,
            credential_store=self.credential_store,
            action=action,
            credential_id=credential_id,
        )
        if not gate.allowed:
            result = ConnectorResult(ok=False, status=gate.reason, message=f"connector action blocked: {gate.reason}")
            self._record(action, result, gate)
            return ConnectorExecution(result=result, gate=gate)

        data_governance = self._assess_data_governance(action)
        if data_governance and data_governance.decision != DataEgressDecision.ALLOW:
            approval_request_id = None
            if data_governance.decision == DataEgressDecision.APPROVAL_REQUIRED:
                approval = self.policy_evaluator.create_approval_request(
                    action="data.egress",
                    resource=f"connector:{action.connector_type.lower()}:{action.action.lower()}",
                    context={
                        "actor_id": action.actor_id,
                        "project_id": action.project_id,
                        "task_id": action.task_id,
                        "request_id": action.request_id,
                        "correlation_id": action.correlation_id,
                        "connector_type": action.connector_type.lower(),
                        "connector_action": action.action.lower(),
                        "data_governance": data_governance.to_dict(),
                    },
                    reason=data_governance.reason,
                )
                approval_request_id = approval.id
                status = "data_egress_requires_approval"
            else:
                status = "data_egress_denied"
            result = ConnectorResult(
                ok=False,
                status=status,
                message=f"connector action blocked: {data_governance.reason}",
            )
            self._record(
                action,
                result,
                gate,
                extra_context={
                    "data_governance": data_governance.to_dict(),
                    "data_approval_request_id": approval_request_id,
                },
            )
            return ConnectorExecution(result=result, gate=gate, data_governance=data_governance)

        try:
            connector = self.registry.get(action.connector_type)
        except KeyError as exc:
            result = ConnectorResult(ok=False, status="connector_not_registered", message=str(exc))
            self._record(action, result, gate)
            return ConnectorExecution(result=result, gate=gate, data_governance=data_governance)

        try:
            result = connector.execute(action)
        except Exception as exc:
            result = ConnectorResult(ok=False, status="connector_error", message=str(exc))
        self._record(action, result, gate, extra_context={"data_governance": data_governance.to_dict()} if data_governance else None)
        return ConnectorExecution(result=result, gate=gate, data_governance=data_governance)

    def _record(
        self,
        action: ConnectorAction,
        result: ConnectorResult,
        gate: ConnectorGateResult,
        *,
        extra_context: dict | None = None,
    ) -> None:
        if self.ledger is None:
            return
        context = {
            "gate_reason": gate.reason,
            "policy_rule_id": gate.policy_rule_id,
            "approval_request_id": gate.approval_request_id,
        }
        if extra_context:
            context.update(extra_context)
        record_connector_event(self.ledger, action, result, credential_id=gate.credential_id, extra_context=context)

    def _assess_data_governance(self, action: ConnectorAction) -> DataGovernanceAssessment | None:
        connector_type = str(action.connector_type or "").strip().lower()
        action_name = str(action.action or "").strip().lower()
        if connector_type not in {"slack", "google_chat", "teams", "feishu"} or action_name != "message.send":
            return None
        parameters = action.parameters if isinstance(action.parameters, dict) else {}
        return assess_data_egress(
            parameters.get("text") or "",
            policy=self.data_governance_policy,
            destination_region=str(parameters.get("destination_region") or "").strip() or None,
        )
