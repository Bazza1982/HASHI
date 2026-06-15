from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from orchestrator.enterprise.audit_ledger import EnterpriseAuditLedger, LedgerEvent


@dataclass(frozen=True)
class ConnectorAction:
    connector_type: str
    action: str
    resource: str = "*"
    actor_id: str | None = None
    project_id: str | None = None
    task_id: str | None = None
    request_id: str | None = None
    correlation_id: str | None = None
    dry_run: bool = False
    parameters: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ConnectorResult:
    ok: bool
    status: str
    message: str | None = None
    data: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ConnectorHealth:
    ok: bool
    status: str
    message: str | None = None
    data: Mapping[str, Any] | None = None


class EnterpriseConnector(Protocol):
    connector_type: str

    def health_check(self) -> ConnectorHealth:
        ...

    def execute(self, action: ConnectorAction) -> ConnectorResult:
        ...


def record_connector_event(
    ledger: EnterpriseAuditLedger,
    action: ConnectorAction,
    result: ConnectorResult | ConnectorHealth,
    *,
    credential_id: str | None = None,
    extra_context: Mapping[str, Any] | None = None,
) -> LedgerEvent:
    context = {
        "connector_type": action.connector_type,
        "connector_action": action.action,
        "resource": action.resource,
        "dry_run": action.dry_run,
        "message": result.message,
        "data": dict(result.data or {}),
        "parameters": _redact_parameters(action.parameters or {}),
    }
    if credential_id:
        context["credential_id"] = credential_id
    if extra_context:
        context.update({str(key): _json_safe_value(value) for key, value in extra_context.items()})
    return ledger.append(
        event_type="connector",
        action=f"{action.connector_type}.{action.action}",
        status=result.status,
        actor_id=action.actor_id,
        project_id=action.project_id,
        task_id=action.task_id,
        request_id=action.request_id,
        correlation_id=action.correlation_id,
        context=context,
    )


def _redact_parameters(parameters: Mapping[str, Any]) -> dict[str, Any]:
    redacted = {}
    sensitive_terms = ("token", "secret", "password", "key", "credential")
    for key, value in parameters.items():
        text_key = str(key)
        if any(term in text_key.lower() for term in sensitive_terms):
            redacted[text_key] = "[REDACTED]"
        else:
            redacted[text_key] = _json_safe_value(value)
    return redacted


def _json_safe_value(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe_value(item) for item in value]
    return repr(value)
