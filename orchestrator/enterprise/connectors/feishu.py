from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import httpx

from orchestrator.enterprise.connectors.base import ConnectorAction, ConnectorHealth, ConnectorResult


FeishuTransport = Callable[[str, Mapping[str, Any]], Mapping[str, Any]]


class FeishuWebhookConnector:
    connector_type = "feishu"

    def __init__(
        self,
        *,
        webhook_url: str,
        timeout_seconds: float = 10.0,
        transport: FeishuTransport | None = None,
    ):
        self.webhook_url = str(webhook_url or "").strip()
        self.timeout_seconds = timeout_seconds
        self._transport = transport

    def health_check(self) -> ConnectorHealth:
        if not self.webhook_url:
            return ConnectorHealth(ok=False, status="unhealthy", message="Feishu webhook URL is not configured")
        return ConnectorHealth(ok=True, status="healthy", message="Feishu webhook connector configured")

    def execute(self, action: ConnectorAction) -> ConnectorResult:
        action_name = action.action.lower()
        if action_name == "message.send":
            return self._send_message(action)
        return ConnectorResult(
            ok=False,
            status="unsupported_action",
            message=f"unsupported Feishu connector action: {action.action}",
            data={"connector_type": self.connector_type, "action": action.action},
        )

    def _send_message(self, action: ConnectorAction) -> ConnectorResult:
        parameters = dict(action.parameters or {})
        text = str(parameters.get("text") or "").strip()
        if not text:
            return ConnectorResult(ok=False, status="invalid_parameters", message="message.send requires text")
        payload: dict[str, Any] = {
            "msg_type": "text",
            "content": {"text": text},
        }
        if action.dry_run:
            return ConnectorResult(ok=True, status="dry_run", message="Feishu message dry run", data={"payload": payload})
        response = self._post(payload)
        return ConnectorResult(ok=True, status="success", message="Feishu message sent", data=dict(response))

    def _post(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if self._transport is not None:
            return dict(self._transport(self.webhook_url, payload))
        with httpx.Client(timeout=self.timeout_seconds) as client:
            response = client.post(self.webhook_url, json=payload)
            response.raise_for_status()
            return {"status_code": response.status_code, "text": response.text}
