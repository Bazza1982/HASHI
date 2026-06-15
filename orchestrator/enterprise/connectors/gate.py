from __future__ import annotations

from dataclasses import dataclass

from orchestrator.enterprise.connectors.base import ConnectorAction
from orchestrator.enterprise.credentials import ConnectorCredentialStore
from orchestrator.enterprise.policy import PolicyDecision, PolicyEvaluator


@dataclass(frozen=True)
class ConnectorGateResult:
    allowed: bool
    reason: str
    credential_id: str | None = None
    policy_rule_id: str | None = None
    approval_request_id: str | None = None


def evaluate_connector_action(
    *,
    policy_evaluator: PolicyEvaluator,
    credential_store: ConnectorCredentialStore,
    action: ConnectorAction,
    credential_id: str,
) -> ConnectorGateResult:
    try:
        credential = credential_store.get_credential(credential_id)
    except ValueError:
        return ConnectorGateResult(False, "connector_credential_not_found", credential_id=credential_id)

    if credential.status != "active":
        return ConnectorGateResult(False, "connector_credential_revoked", credential_id=credential.id)
    if credential.org_id != policy_evaluator.org_id:
        return ConnectorGateResult(False, "connector_credential_org_mismatch", credential_id=credential.id)
    if credential.connector_type != action.connector_type.lower():
        return ConnectorGateResult(False, "connector_credential_type_mismatch", credential_id=credential.id)

    resource = f"connector:{action.connector_type.lower()}:{action.action.lower()}"
    context = {
        "actor_id": action.actor_id,
        "project_id": action.project_id,
        "task_id": action.task_id,
        "request_id": action.request_id,
        "correlation_id": action.correlation_id,
        "connector_type": action.connector_type.lower(),
        "connector_action": action.action.lower(),
        "connector_resource": action.resource,
        "credential_id": credential.id,
        "credential_scopes": list(credential.scopes),
    }
    evaluation = policy_evaluator.evaluate("connector.execute", resource=resource, context=context)
    if evaluation.decision == PolicyDecision.ALLOW:
        return ConnectorGateResult(True, "allowed", credential_id=credential.id, policy_rule_id=evaluation.rule_id)
    if evaluation.decision == PolicyDecision.APPROVAL_REQUIRED:
        approval = policy_evaluator.create_approval_request(
            action="connector.execute",
            resource=resource,
            context=context,
            rule_id=evaluation.rule_id,
            reason=evaluation.reason,
        )
        return ConnectorGateResult(
            False,
            "connector_action_requires_approval",
            credential_id=credential.id,
            policy_rule_id=evaluation.rule_id,
            approval_request_id=approval.id,
        )
    return ConnectorGateResult(
        False,
        "connector_action_denied",
        credential_id=credential.id,
        policy_rule_id=evaluation.rule_id,
    )
