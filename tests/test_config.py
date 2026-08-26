import json
import logging
from pathlib import Path

import pytest

from orchestrator.config import (
    LEGACY_PCM_CONFIG_BACKUP_SUFFIX,
    ConfigManager,
)
from orchestrator.her_v2.config import HERv2Config
from orchestrator.her_v2.runtime_configuration import build_her_v2_provider_options
from orchestrator.pcm import load_pcm_document

ROOT = Path(__file__).resolve().parent.parent
CORE_HER_V2_PROVIDERS = {"hashi-api", "deepseek-api", "openrouter-api"}


def _materialize_legacy_pcm(config_path, root):
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    for agent in payload.get("agents", []):
        raw = agent.get("system_md")
        if not raw:
            continue
        path = root / raw
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(f"Legacy Persona for {agent.get('name')}\n", encoding="utf-8")


def _write_base_files(tmp_path, agent):
    config_path = tmp_path / "agents.json"
    secrets_path = tmp_path / "secrets.json"
    config_path.write_text(
        json.dumps(
            {
                "global": {
                    "authorized_id": 0,
                    "base_logs_dir": "logs",
                    "base_media_dir": "media",
                },
                "agents": [agent],
            }
        ),
        encoding="utf-8",
    )
    secrets_path.write_text(json.dumps({"authorized_telegram_id": 0}), encoding="utf-8")
    _materialize_legacy_pcm(config_path, tmp_path)
    return config_path, secrets_path


def test_missing_agent_type_is_rejected(tmp_path):
    config_path, secrets_path = _write_base_files(
        tmp_path,
        {
            "name": "legacy",
            "engine": "gemini-cli",
            "workspace_dir": "workspaces/legacy",
            "system_md": "workspaces/legacy/agent.md",
            "model": "gemini-3-flash",
        },
    )

    with pytest.raises(ValueError, match="no explicit type"):
        ConfigManager(config_path, secrets_path, bridge_home=tmp_path).load()


def test_explicit_fixed_stateless_agent_is_migrated_to_flex(tmp_path, caplog):
    config_path, secrets_path = _write_base_files(
        tmp_path,
        {
            "name": "legacy",
            "type": "fixed",
            "engine": "gemini-cli",
            "workspace_dir": "workspaces/legacy",
            "system_md": "workspaces/legacy/agent.md",
            "model": "gemini-3-flash",
        },
    )

    original = config_path.read_text(encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="BridgeU.Config"):
        _, agents, _ = ConfigManager(config_path, secrets_path, bridge_home=tmp_path).load()

    assert agents[0].type == "flex"
    assert agents[0].active_backend == "gemini-cli"
    assert agents[0].allowed_backends == [
        {"engine": "gemini-cli", "model": "gemini-3-flash"}
    ]
    assert agents[0].default_mode == "flex"
    persisted = json.loads(config_path.read_text(encoding="utf-8"))
    assert persisted["agents"][0]["type"] == "flex"
    assert persisted["agents"][0]["default_mode"] == "flex"
    backup_path = config_path.with_name(config_path.name + LEGACY_PCM_CONFIG_BACKUP_SUFFIX)
    assert backup_path.read_text(encoding="utf-8") == original
    assert "system_md" not in persisted["agents"][0]
    document = load_pcm_document(tmp_path / "workspaces/legacy/agent.md")
    assert "Legacy Persona" in document.persona
    assert "Migrated retired type='fixed'" in caplog.text


def test_explicit_fixed_session_agent_keeps_fixed_working_mode(tmp_path, caplog):
    config_path, secrets_path = _write_base_files(
        tmp_path,
        {
            "name": "legacy",
            "type": "fixed",
            "engine": "codex-cli",
            "workspace_dir": "workspaces/legacy",
            "system_md": "workspaces/legacy/agent.md",
            "model": "gpt-5.4",
            "resume_policy": "latest",
        },
    )

    with caplog.at_level(logging.WARNING, logger="BridgeU.Config"):
        _, agents, _ = ConfigManager(config_path, secrets_path, bridge_home=tmp_path).load()

    assert agents[0].type == "flex"
    assert agents[0].active_backend == "codex-cli"
    assert agents[0].allowed_backends == [
        {"engine": "codex-cli", "model": "gpt-5.4"}
    ]
    assert agents[0].default_mode == "fixed"
    persisted = json.loads(config_path.read_text(encoding="utf-8"))
    assert persisted["agents"][0]["type"] == "flex"
    assert persisted["agents"][0]["default_mode"] == "fixed"
    assert "engine" not in persisted["agents"][0]
    assert "resume_policy" not in persisted["agents"][0]
    assert "default_mode=fixed" in caplog.text


def test_explicit_flex_agent_type_does_not_warn(tmp_path, caplog):
    config_path, secrets_path = _write_base_files(
        tmp_path,
        {
            "name": "flexy",
            "type": "flex",
            "workspace_dir": "workspaces/flexy",
            "system_md": "workspaces/flexy/agent.md",
            "allowed_backends": ["gemini-cli", "codex-cli"],
            "active_backend": "gemini-cli",
        },
    )

    with caplog.at_level(logging.WARNING, logger="BridgeU.Config"):
        _, agents, _ = ConfigManager(config_path, secrets_path, bridge_home=tmp_path).load()

    assert agents[0].type == "flex"
    assert "has no explicit type" not in caplog.text


def test_public_her_configuration_id_resolves_forward_to_v2(tmp_path):
    config_path = tmp_path / "agents.json"
    secrets_path = tmp_path / "secrets.json"
    config_path.write_text(
        json.dumps(
            {
                "global": {
                    "authorized_id": 0,
                    "base_logs_dir": "logs",
                    "base_media_dir": "media",
                    "her_providers": {
                        "providers": {
                            "openrouter": {
                                "base_url": "https://openrouter.ai/api/v1",
                                "secret": "openrouter_key",
                            }
                        },
                    },
                },
                "agents": [
                    {
                        "name": "flexy",
                        "type": "flex",
                        "workspace_dir": "workspaces/flexy",
                        "system_md": "workspaces/flexy/agent.md",
                        "allowed_backends": [{"engine": "her", "model": "role-configured"}],
                        "active_backend": "her",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    secrets_path.write_text(json.dumps({"authorized_telegram_id": 0}), encoding="utf-8")
    _materialize_legacy_pcm(config_path, tmp_path)

    global_cfg, agents, _ = ConfigManager(config_path, secrets_path, bridge_home=tmp_path).load()

    assert global_cfg.her_providers["providers"]["openrouter"]["secret"] == "openrouter_key"
    assert agents[0].active_backend == "her-v2"
    assert agents[0].allowed_backends[0]["engine"] == "her-v2"


def test_agents_sample_has_valid_her_v2_core_providers():
    sample = json.loads((ROOT / "agents.json.sample").read_text(encoding="utf-8"))
    providers = sample["global"]["her_providers"]["providers"]
    her_entries = []

    for agent in sample["agents"]:
        assert all(
            backend.get("engine") != "her"
            for backend in agent.get("allowed_backends", [])
            if isinstance(backend, dict)
        )
        her_entries.extend(
            (agent["allowed_backends"], backend)
            for backend in agent.get("allowed_backends", [])
            if isinstance(backend, dict) and backend.get("engine") == "her-v2"
        )

    assert her_entries
    for allowed_backends, backend in her_entries:
        HERv2Config.from_mapping(backend["her_v2"])
        options = build_her_v2_provider_options(
            allowed_backends,
            providers,
            backend["her_v2"],
        )
        available = {
            option["engine"] for option in options if option.get("available")
        }
        assert CORE_HER_V2_PROVIDERS <= available


@pytest.mark.parametrize(
    "legacy_fragment",
    [
        {"global": {"claw_providers": {"providers": {}}}},
        {
            "agent": {
                "allowed_backends": [{"engine": "claw-cli"}],
                "active_backend": "claw-cli",
            }
        },
    ],
)
def test_removed_claw_configuration_is_rejected(tmp_path, legacy_fragment):
    agent = {
        "name": "legacy",
        "type": "flex",
        "workspace_dir": "workspaces/legacy",
        "system_md": "workspaces/legacy/agent.md",
        "allowed_backends": [{"engine": "codex-cli", "model": "gpt-5.4"}],
        "active_backend": "codex-cli",
    }
    config_path, secrets_path = _write_base_files(tmp_path, agent)
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload["global"].update(legacy_fragment.get("global", {}))
    payload["agents"][0].update(legacy_fragment.get("agent", {}))
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="claw"):
        ConfigManager(config_path, secrets_path, bridge_home=tmp_path).load()


def test_flex_agent_does_not_receive_an_implicit_her_backend(tmp_path):
    config_path = tmp_path / "agents.json"
    secrets_path = tmp_path / "secrets.json"
    config_path.write_text(
        json.dumps(
            {
                "global": {"authorized_id": 0},
                "agents": [
                    {
                        "name": "flexy",
                        "type": "flex",
                        "workspace_dir": "workspaces/flexy",
                        "system_md": "workspaces/flexy/agent.md",
                        "allowed_backends": [{"engine": "gemini-cli"}],
                        "active_backend": "gemini-cli",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    secrets_path.write_text(json.dumps({"authorized_telegram_id": 0}), encoding="utf-8")
    _materialize_legacy_pcm(config_path, tmp_path)

    _, agents, _ = ConfigManager(config_path, secrets_path, bridge_home=tmp_path).load()

    assert agents[0].allowed_backends == [{"engine": "gemini-cli"}]


def test_enterprise_scheduler_lease_config_is_loaded(tmp_path):
    config_path = tmp_path / "agents.json"
    secrets_path = tmp_path / "secrets.json"
    config_path.write_text(
        json.dumps(
            {
                "global": {
                    "authorized_id": 0,
                    "base_logs_dir": "logs",
                    "base_media_dir": "media",
                    "enterprise_database_url": "sqlite:////data/state/enterprise.sqlite",
                    "enterprise_scheduler_lease_enabled": True,
                    "enterprise_scheduler_lease_backend": "kubernetes",
                    "enterprise_scheduler_lease_name": "scheduler-main",
                    "enterprise_scheduler_lease_holder": "pod-a",
                    "enterprise_scheduler_lease_ttl_seconds": 90,
                    "enterprise_scheduler_lease_kubernetes_namespace": "hashi-enterprise",
                    "enterprise_scheduler_lease_kubernetes_in_cluster": False,
                    "enterprise_scheduler_lease_kubeconfig_path": "/tmp/kubeconfig",
                    "enterprise_scheduler_lease_pool_enabled": True,
                    "enterprise_scheduler_lease_pool_min_size": 2,
                    "enterprise_scheduler_lease_pool_max_size": 6,
                },
                "agents": [
                    {
                        "name": "flexy",
                        "type": "flex",
                        "workspace_dir": "workspaces/flexy",
                        "system_md": "workspaces/flexy/agent.md",
                        "allowed_backends": ["gemini-cli"],
                        "active_backend": "gemini-cli",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    secrets_path.write_text(json.dumps({"authorized_telegram_id": 0}), encoding="utf-8")
    _materialize_legacy_pcm(config_path, tmp_path)

    global_cfg, _, _ = ConfigManager(config_path, secrets_path, bridge_home=tmp_path).load()

    assert global_cfg.enterprise_database_url == "sqlite:////data/state/enterprise.sqlite"
    assert global_cfg.enterprise_scheduler_lease_enabled is True
    assert global_cfg.enterprise_scheduler_lease_backend == "kubernetes"
    assert global_cfg.enterprise_scheduler_lease_name == "scheduler-main"
    assert global_cfg.enterprise_scheduler_lease_holder == "pod-a"
    assert global_cfg.enterprise_scheduler_lease_ttl_seconds == 90
    assert global_cfg.enterprise_scheduler_lease_kubernetes_namespace == "hashi-enterprise"
    assert global_cfg.enterprise_scheduler_lease_kubernetes_in_cluster is False
    assert global_cfg.enterprise_scheduler_lease_kubeconfig_path == "/tmp/kubeconfig"
    assert global_cfg.enterprise_scheduler_lease_pool_enabled is True
    assert global_cfg.enterprise_scheduler_lease_pool_min_size == 2
    assert global_cfg.enterprise_scheduler_lease_pool_max_size == 6


def test_enterprise_scheduler_lease_env_overrides_config(tmp_path, monkeypatch):
    monkeypatch.setenv("HASHI_ENTERPRISE_DATABASE_URL", "sqlite:////env/state/enterprise.sqlite")
    monkeypatch.setenv("HASHI_ENTERPRISE_SCHEDULER_LEASE_ENABLED", "1")
    monkeypatch.setenv("HASHI_ENTERPRISE_SCHEDULER_LEASE_BACKEND", "k8s")
    monkeypatch.setenv("HASHI_ENTERPRISE_SCHEDULER_LEASE_NAME", "scheduler-env")
    monkeypatch.setenv("HASHI_ENTERPRISE_SCHEDULER_LEASE_HOLDER", "pod-env")
    monkeypatch.setenv("HASHI_ENTERPRISE_SCHEDULER_LEASE_TTL_SECONDS", "120")
    monkeypatch.setenv("HASHI_ENTERPRISE_SCHEDULER_LEASE_K8S_NAMESPACE", "env-namespace")
    monkeypatch.setenv("HASHI_ENTERPRISE_SCHEDULER_LEASE_K8S_IN_CLUSTER", "false")
    monkeypatch.setenv("HASHI_ENTERPRISE_SCHEDULER_LEASE_KUBECONFIG", "/env/kubeconfig")
    monkeypatch.setenv("HASHI_ENTERPRISE_SCHEDULER_LEASE_POOL_ENABLED", "true")
    monkeypatch.setenv("HASHI_ENTERPRISE_SCHEDULER_LEASE_POOL_MIN_SIZE", "3")
    monkeypatch.setenv("HASHI_ENTERPRISE_SCHEDULER_LEASE_POOL_MAX_SIZE", "7")
    config_path, secrets_path = _write_base_files(
        tmp_path,
        {
            "name": "flexy",
            "type": "flex",
            "workspace_dir": "workspaces/flexy",
            "system_md": "workspaces/flexy/agent.md",
            "allowed_backends": ["gemini-cli"],
            "active_backend": "gemini-cli",
        },
    )

    global_cfg, _, _ = ConfigManager(config_path, secrets_path, bridge_home=tmp_path).load()

    assert global_cfg.enterprise_database_url == "sqlite:////env/state/enterprise.sqlite"
    assert global_cfg.enterprise_scheduler_lease_enabled is True
    assert global_cfg.enterprise_scheduler_lease_backend == "k8s"
    assert global_cfg.enterprise_scheduler_lease_name == "scheduler-env"
    assert global_cfg.enterprise_scheduler_lease_holder == "pod-env"
    assert global_cfg.enterprise_scheduler_lease_ttl_seconds == 120
    assert global_cfg.enterprise_scheduler_lease_kubernetes_namespace == "env-namespace"
    assert global_cfg.enterprise_scheduler_lease_kubernetes_in_cluster is False
    assert global_cfg.enterprise_scheduler_lease_kubeconfig_path == "/env/kubeconfig"
    assert global_cfg.enterprise_scheduler_lease_pool_enabled is True
    assert global_cfg.enterprise_scheduler_lease_pool_min_size == 3
    assert global_cfg.enterprise_scheduler_lease_pool_max_size == 7
