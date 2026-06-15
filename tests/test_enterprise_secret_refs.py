from __future__ import annotations

import pytest

from orchestrator.enterprise import ConnectorSecretResolver


def test_connector_secret_resolver_reads_env_refs():
    resolver = ConnectorSecretResolver(environ={"GITHUB_TOKEN": "ghp-test"})

    secret = resolver.resolve("env://GITHUB_TOKEN")

    assert secret.value == "ghp-test"
    assert secret.source == "env"
    assert secret.redacted() == {"ref": "env://GITHUB_TOKEN", "source": "env", "value": "[REDACTED]"}


def test_connector_secret_resolver_reads_hashi_secret_refs():
    resolver = ConnectorSecretResolver(secrets={"github_token": "provider-secret"})

    secret = resolver.resolve("secrets://github_token")

    assert secret.value == "provider-secret"
    assert secret.source == "hashi"


def test_connector_secret_resolver_supports_hashi_scheme_alias():
    resolver = ConnectorSecretResolver(secrets={"github_token": "provider-secret"})

    secret = resolver.resolve("hashi://github_token")

    assert secret.value == "provider-secret"
    assert secret.source == "hashi"


def test_connector_secret_resolver_fails_closed_for_missing_env():
    resolver = ConnectorSecretResolver(environ={})

    with pytest.raises(ValueError, match="environment secret is not set"):
        resolver.resolve("env://MISSING")


def test_connector_secret_resolver_fails_closed_for_unconfigured_vault():
    resolver = ConnectorSecretResolver()

    with pytest.raises(ValueError, match="vault secret resolver is not configured"):
        resolver.resolve("vault://github/app")


def test_connector_secret_resolver_rejects_unknown_scheme():
    resolver = ConnectorSecretResolver()

    with pytest.raises(ValueError, match="unsupported secret_ref scheme"):
        resolver.resolve("plain-secret")
