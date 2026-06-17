from __future__ import annotations

import pytest

from orchestrator.enterprise import (
    ConnectorSecretResolver,
    FileSecretProvider,
    KubernetesMountedSecretProvider,
)


def test_connector_secret_resolver_preserves_env_and_hashi_defaults():
    resolver = ConnectorSecretResolver(secrets={"github_token": "ghp"}, environ={"SLACK_WEBHOOK": "https://hook"})

    assert resolver.resolve("secrets://github_token").value == "ghp"
    assert resolver.resolve("env://SLACK_WEBHOOK").value == "https://hook"
    assert resolver.resolve("env:SLACK_WEBHOOK").source == "env"


def test_file_secret_provider_reads_within_configured_root(tmp_path):
    root = tmp_path / "secrets"
    root.mkdir()
    (root / "github_token").write_text("ghp-file\n", encoding="utf-8")
    resolver = ConnectorSecretResolver(providers=[FileSecretProvider(root=root)])

    secret = resolver.resolve("file://github_token")

    assert secret.value == "ghp-file"
    assert secret.source == "file"
    assert secret.redacted()["value"] == "[REDACTED]"


def test_file_secret_provider_rejects_path_escape(tmp_path):
    resolver = ConnectorSecretResolver(providers=[FileSecretProvider(root=tmp_path / "secrets")])

    with pytest.raises(ValueError, match="escapes"):
        resolver.resolve("file://../outside")


def test_kubernetes_mounted_secret_provider_reads_namespace_name_key(tmp_path):
    root = tmp_path / "k8s"
    path = root / "prod" / "github" / "token"
    path.parent.mkdir(parents=True)
    path.write_text("ghp-k8s\n", encoding="utf-8")
    resolver = ConnectorSecretResolver(providers=[KubernetesMountedSecretProvider(root=root)])

    secret = resolver.resolve("k8s://prod/github/token")

    assert secret.value == "ghp-k8s"
    assert secret.source == "k8s"


def test_unconfigured_vault_and_k8s_fail_closed():
    resolver = ConnectorSecretResolver()

    with pytest.raises(ValueError, match="vault secret resolver is not configured"):
        resolver.resolve("vault://github/app")
    with pytest.raises(ValueError, match="kubernetes secret resolver is not configured"):
        resolver.resolve("k8s://prod/github/token")
