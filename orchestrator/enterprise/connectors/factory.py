from __future__ import annotations

from collections.abc import Mapping

from orchestrator.enterprise.connectors.base import EnterpriseConnector
from orchestrator.enterprise.connectors.github import GitHubConnector, GitHubTransport
from orchestrator.enterprise.connectors.registry import ConnectorRegistry
from orchestrator.enterprise.connectors.slack import SlackTransport, SlackWebhookConnector
from orchestrator.enterprise.credentials import ConnectorCredential
from orchestrator.enterprise.secret_refs import ConnectorSecretResolver


class ConnectorFactory:
    def __init__(
        self,
        *,
        secret_resolver: ConnectorSecretResolver,
        transports: Mapping[str, GitHubTransport | SlackTransport] | None = None,
    ):
        self.secret_resolver = secret_resolver
        self.transports = {str(key).lower(): value for key, value in dict(transports or {}).items()}

    def build(self, credential: ConnectorCredential) -> EnterpriseConnector:
        connector_type = credential.connector_type.lower()
        secret = self.secret_resolver.resolve(credential.secret_ref)
        if connector_type == "github":
            return GitHubConnector(token=secret.value, transport=self.transports.get("github"))
        if connector_type == "slack":
            return SlackWebhookConnector(webhook_url=secret.value, transport=self.transports.get("slack"))
        raise ValueError(f"unsupported connector type: {credential.connector_type}")

    def build_registry(self, credentials: list[ConnectorCredential] | tuple[ConnectorCredential, ...]) -> ConnectorRegistry:
        registry = ConnectorRegistry()
        for credential in credentials:
            if credential.status != "active":
                continue
            registry.register(self.build(credential))
        return registry
