from __future__ import annotations

from orchestrator.enterprise.connectors.base import ConnectorAction

_WEBHOOK_MESSAGE_CONNECTORS = frozenset({"slack", "google_chat"})


def validate_connector_action(action: ConnectorAction) -> str | None:
    connector_type = str(action.connector_type or "").strip().lower()
    action_name = str(action.action or "").strip().lower()
    if connector_type in _WEBHOOK_MESSAGE_CONNECTORS and action_name == "message.send":
        parameters = action.parameters if isinstance(action.parameters, dict) else {}
        text = str(parameters.get("text") or "").strip()
        if not text:
            return "message.send requires non-empty text in parameters"
    return None