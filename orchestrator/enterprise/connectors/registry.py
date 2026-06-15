from __future__ import annotations

from dataclasses import dataclass

from orchestrator.enterprise.audit_ledger import EnterpriseAuditLedger
from orchestrator.enterprise.connectors.base import (
    ConnectorAction,
    ConnectorHealth,
    EnterpriseConnector,
    record_connector_event,
)


@dataclass(frozen=True)
class ConnectorHealthSummary:
    connector_type: str
    ok: bool
    status: str
    message: str | None = None
    data: dict | None = None

    def to_dict(self) -> dict:
        return {
            "connector_type": self.connector_type,
            "ok": self.ok,
            "status": self.status,
            "message": self.message,
            "data": dict(self.data or {}),
        }


class ConnectorRegistry:
    def __init__(self, connectors: list[EnterpriseConnector] | tuple[EnterpriseConnector, ...] | None = None):
        self._connectors: dict[str, EnterpriseConnector] = {}
        for connector in connectors or ():
            self.register(connector)

    def register(self, connector: EnterpriseConnector) -> None:
        connector_type = str(getattr(connector, "connector_type", "") or "").strip().lower()
        if not connector_type:
            raise ValueError("connector_type is required")
        if connector_type in self._connectors:
            raise ValueError(f"connector already registered: {connector_type}")
        self._connectors[connector_type] = connector

    def get(self, connector_type: str) -> EnterpriseConnector:
        normalized = str(connector_type or "").strip().lower()
        if not normalized or normalized not in self._connectors:
            raise KeyError(f"connector not registered: {connector_type!r}")
        return self._connectors[normalized]

    def list_types(self) -> list[str]:
        return sorted(self._connectors)

    def health_checks(self, *, ledger: EnterpriseAuditLedger | None = None) -> list[ConnectorHealthSummary]:
        summaries = []
        for connector_type in self.list_types():
            connector = self._connectors[connector_type]
            try:
                health = connector.health_check()
            except Exception as exc:
                health = ConnectorHealth(ok=False, status="unhealthy", message=str(exc), data={})
            summaries.append(_health_summary(connector_type, health))
            if ledger is not None:
                action = ConnectorAction(
                    connector_type=connector_type,
                    action="health_check",
                    resource=f"connector:{connector_type}",
                )
                record_connector_event(ledger, action, health)
        return summaries


def _health_summary(connector_type: str, health: ConnectorHealth) -> ConnectorHealthSummary:
    return ConnectorHealthSummary(
        connector_type=connector_type,
        ok=bool(health.ok),
        status=str(health.status or ("healthy" if health.ok else "unhealthy")),
        message=health.message,
        data=dict(health.data or {}),
    )
