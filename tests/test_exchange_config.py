from __future__ import annotations

import json

import pytest

from orchestrator.config_json import ConfigConflictError, read_config_json
from orchestrator.exchange_config import (
    ExchangeConfigError,
    load_exchange_config,
    load_exchange_credential,
    parse_exchange_config,
    published_agent_records,
    update_exchange_config,
)


def _enabled(**overrides):
    value = {
        "enabled": True,
        "authority_id": "authority_1",
        "url": "wss://remote.example.test/v1/connect",
        "registered_instance_id": "instance_1",
        "instance_alias": "home",
        "credential_ref": "exchange_token",
        "published_agents": ["Planner"],
    }
    value.update(overrides)
    return value


def test_exchange_is_disabled_when_configuration_is_absent(tmp_path):
    (tmp_path / "agents.json").write_text(
        json.dumps({"global": {"instance_id": "HASHI1"}, "agents": []}),
        encoding="utf-8",
    )

    assert load_exchange_config(tmp_path).enabled is False


def test_exchange_config_accepts_wss_and_loopback_lab_ws():
    assert parse_exchange_config(_enabled()).enabled is True
    lab = parse_exchange_config(
        _enabled(url="ws://127.0.0.1:8787/v1/connect")
    )
    assert lab.url == "ws://127.0.0.1:8787/v1/connect"
    hidden = parse_exchange_config(_enabled(published_agents=[]))
    assert hidden.enabled is True
    assert hidden.published_agents == ()


@pytest.mark.parametrize(
    "url",
    [
        "ws://exchange.example.test/v1/connect",
        "https://exchange.example.test/v1/connect",
        "wss://user@example.test/v1/connect",
        "wss://example.test/other",
        "wss://example.test/v1/connect?token=secret",
    ],
)
def test_exchange_config_rejects_unsafe_endpoint(url):
    with pytest.raises(ExchangeConfigError):
        parse_exchange_config(_enabled(url=url))


def test_credential_ref_resolves_only_from_local_secret_store(tmp_path):
    config = parse_exchange_config(_enabled())
    (tmp_path / "secrets.json").write_text(
        json.dumps({"exchange_token": "synthetic-instance-secret"}),
        encoding="utf-8",
    )

    assert (
        load_exchange_credential(tmp_path, config)
        == "synthetic-instance-secret"
    )
    assert "synthetic-instance-secret" not in json.dumps(config.public_dict())


@pytest.mark.parametrize(
    "secret",
    [
        "short",
        "synthetic token with spaces",
        "synthetic-token-with-newline\n",
        "synthetic-token-凭据",
    ],
)
def test_exchange_credential_rejects_non_bearer_safe_values(
    tmp_path,
    secret,
):
    config = parse_exchange_config(_enabled())
    (tmp_path / "secrets.json").write_text(
        json.dumps({"exchange_token": secret}),
        encoding="utf-8",
    )

    with pytest.raises(ExchangeConfigError):
        load_exchange_credential(tmp_path, config)


def test_exchange_update_uses_revision_cas_and_preserves_other_config(tmp_path):
    path = tmp_path / "agents.json"
    path.write_text(
        json.dumps(
            {
                "global": {"instance_id": "HASHI1", "workbench_port": 18802},
                "groups": {"staff": {"members": ["planner"]}},
                "agents": [{"name": "planner", "is_active": True}],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    revision = read_config_json(path).revision

    update_exchange_config(
        path,
        _enabled(),
        expected_revision=revision,
    )

    value = json.loads(path.read_text(encoding="utf-8"))
    assert value["global"]["workbench_port"] == 18802
    assert value["groups"]["staff"]["members"] == ["planner"]
    assert value["exchange"]["enabled"] is True
    with pytest.raises(ConfigConflictError):
        update_exchange_config(
            path,
            {"enabled": False, "published_agents": []},
            expected_revision=revision,
        )


def test_publication_is_explicit_and_filters_inactive_agents(tmp_path):
    (tmp_path / "agents.json").write_text(
        json.dumps(
            {
                "exchange": _enabled(
                    published_agents=["planner", "reviewer", "missing"]
                ),
                "agents": [
                    {
                        "name": "planner",
                        "display_name": "Planner",
                        "is_active": True,
                    },
                    {
                        "name": "reviewer",
                        "display_name": "Reviewer",
                        "is_active": False,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    config = load_exchange_config(tmp_path)

    assert published_agent_records(tmp_path, config) == [
        {
            "agent_id": "planner",
            "display_name": "Planner",
            "message_kinds": ["agent_message", "agent_reply"],
        }
    ]
