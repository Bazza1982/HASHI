from __future__ import annotations

from orchestrator.enterprise.capabilities import AgentCapabilityRegistry


def test_agent_capability_registry_summarizes_backends_tools_and_bridge_caps():
    registry = AgentCapabilityRegistry(
        agent_rows=[
            {
                "name": "nana",
                "display_name": "Nana",
                "type": "flex",
                "project_ids": ["prj-research", "prj-ops"],
                "active_backend": "grok-cli",
                "allowed_backends": [
                    {
                        "engine": "grok-cli",
                        "tools": {"allowed": ["file_read", "file_write"]},
                    },
                    {
                        "engine": "codex-cli",
                        "allowed_tools": ["bash", "file_write"],
                    },
                ],
            }
        ],
        capability_rows=[
            {
                "name": "nana",
                "can_talk_to": ["zelda"],
                "can_receive_from": ["zelda"],
                "allowed_incoming_intents": ["ask"],
                "granted_scopes": ["conversation"],
                "tags": ["enterprise"],
            }
        ],
    )

    summary = registry.get_agent("nana")

    assert summary is not None
    assert summary.project_ids == ("prj-ops", "prj-research")
    assert summary.active_backend == "grok-cli"
    assert summary.allowed_backends == ("grok-cli", "codex-cli")
    assert summary.allowed_tools == ("bash", "file_read", "file_write")
    assert summary.can_talk_to == ("zelda",)
    assert summary.to_dict()["bridge"]["granted_scopes"] == ["conversation"]


def test_agent_capability_registry_filters_by_project():
    registry = AgentCapabilityRegistry(
        agent_rows=[
            {"name": "research", "project_id": "prj-research"},
            {"name": "shared", "project_ids": ["prj-research", "prj-ops"]},
            {"name": "finance", "project_id": "prj-finance"},
            {"name": "unscoped"},
        ],
    )

    assert [item.name for item in registry.list_agents(project_id="prj-research")] == ["research", "shared"]
    assert [item.name for item in registry.list_agents(project_id="prj-finance")] == ["finance"]
    assert [item.name for item in registry.list_agents()] == ["finance", "research", "shared", "unscoped"]


def test_agent_capability_registry_accepts_capability_mapping():
    registry = AgentCapabilityRegistry(
        agent_rows=[{"name": "zelda", "project_id": "prj-research"}],
        capability_rows={"zelda": {"can_talk_to": ["nana"], "granted_scopes": ["conversation"]}},
    )

    summary = registry.get_agent("zelda")

    assert summary is not None
    assert summary.can_talk_to == ("nana",)
    assert summary.granted_scopes == ("conversation",)
