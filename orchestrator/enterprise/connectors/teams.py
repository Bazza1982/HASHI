from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import httpx

from orchestrator.enterprise.connectors.base import ConnectorAction, ConnectorHealth, ConnectorResult


TeamsTransport = Callable[[str, Mapping[str, Any]], Mapping[str, Any]]


class TeamsWebhookConnector:
    connector_type = "teams"

    def __init__(
        self,
        *,
        webhook_url: str,
        timeout_seconds: float = 10.0,
        transport: TeamsTransport | None = None,
    ):
        self.webhook_url = str(webhook_url or "").strip()
        self.timeout_seconds = timeout_seconds
        self._transport = transport

    def health_check(self) -> ConnectorHealth:
        if not self.webhook_url:
            return ConnectorHealth(ok=False, status="unhealthy", message="Teams webhook URL is not configured")
        return ConnectorHealth(ok=True, status="healthy", message="Teams webhook connector configured")

    def execute(self, action: ConnectorAction) -> ConnectorResult:
        action_name = action.action.lower()
        if action_name == "message.send":
            return self._send_message(action)
        return ConnectorResult(
            ok=False,
            status="unsupported_action",
            message=f"unsupported Teams connector action: {action.action}",
            data={"connector_type": self.connector_type, "action": action.action},
        )

    def _send_message(self, action: ConnectorAction) -> ConnectorResult:
        parameters = dict(action.parameters or {})
        text = str(parameters.get("text") or "").strip()
        if not text:
            return ConnectorResult(ok=False, status="invalid_parameters", message="message.send requires text")
        payload: dict[str, Any] = {"text": text}
        title = str(parameters.get("title") or "").strip()
        if title:
            payload["title"] = title
        sections = parameters.get("sections")
        if isinstance(sections, list):
            payload["sections"] = sections
        if action.dry_run:
            return ConnectorResult(ok=True, status="dry_run", message="Teams message dry run", data={"payload": payload})
        response = self._post(payload)
        return ConnectorResult(ok=True, status="success", message="Teams message sent", data=dict(response))

    def _post(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if self._transport is not None:
            return dict(self._transport(self.webhook_url, payload))
        with httpx.Client(timeout=self.timeout_seconds) as client:
            response = client.post(self.webhook_url, json=payload)
            response.raise_for_status()
            return {"status_code": response.status_code, "text": response.text}
