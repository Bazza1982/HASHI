from __future__ import annotations

from dataclasses import dataclass

from orchestrator.enterprise.audit_ledger import EnterpriseAuditLedger
from orchestrator.enterprise.connectors.base import ConnectorAction, ConnectorResult, record_connector_event
from orchestrator.enterprise.connectors.gate import ConnectorGateResult, evaluate_connector_action
from orchestrator.enterprise.connectors.registry import ConnectorRegistry
from orchestrator.enterprise.credentials import ConnectorCredentialStore
from orchestrator.enterprise.policy import PolicyEvaluator


@dataclass(frozen=True)
class ConnectorExecution:
    result: ConnectorResult
    gate: ConnectorGateResult


class ConnectorExecutionService:
    def __init__(
        self,
        *,
        registry: ConnectorRegistry,
        credential_store: ConnectorCredentialStore,
        policy_evaluator: PolicyEvaluator,
        ledger: EnterpriseAuditLedger | None = None,
    ):
        self.registry = registry
        self.credential_store = credential_store
        self.policy_evaluator = policy_evaluator
        self.ledger = ledger

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

        try:
            connector = self.registry.get(action.connector_type)
        except KeyError as exc:
            result = ConnectorResult(ok=False, status="connector_not_registered", message=str(exc))
            self._record(action, result, gate)
            return ConnectorExecution(result=result, gate=gate)

        try:
            result = connector.execute(action)
        except Exception as exc:
            result = ConnectorResult(ok=False, status="connector_error", message=str(exc))
        self._record(action, result, gate)
        return ConnectorExecution(result=result, gate=gate)

    def _record(self, action: ConnectorAction, result: ConnectorResult, gate: ConnectorGateResult) -> None:
        if self.ledger is None:
            return
        record_connector_event(
            self.ledger,
            action,
            result,
            credential_id=gate.credential_id,
            extra_context={
                "gate_reason": gate.reason,
                "policy_rule_id": gate.policy_rule_id,
                "approval_request_id": gate.approval_request_id,
            },
        )
