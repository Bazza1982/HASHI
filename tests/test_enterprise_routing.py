from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.conversation_router import ConversationRouter
from orchestrator.enterprise.routing import agent_project_ids, evaluate_project_route


class _Runtime:
    def __init__(self, name: str):
        self.name = name
        self.startup_success = True
        self.requests: list[dict] = []

    def get_runtime_metadata(self):
        return {"online": True, "status": "online", "engine": "test", "model": "test"}

    async def enqueue_api_text(self, text: str, *, source: str, deliver_to_telegram: bool):
        self.requests.append(
            {
                "text": text,
                "source": source,
                "deliver_to_telegram": deliver_to_telegram,
            }
        )
        return f"req-{len(self.requests)}"

    def register_request_listener(self, request_id, listener):
        return None


def _write_bridge_config(tmp_path: Path) -> tuple[Path, Path]:
    config_path = tmp_path / "agents.json"
    capabilities_path = tmp_path / "agent_capabilities.json"
    config_path.write_text(
        json.dumps(
            {
                "agents": [
                    {"name": "alice", "project_id": "prj-a"},
                    {"name": "bob", "project_ids": ["prj-a", "prj-b"]},
                    {"name": "carol", "project_id": "prj-b"},
                ]
            }
        ),
        encoding="utf-8",
    )
    capabilities_path.write_text(
        json.dumps(
            {
                "agents": [
                    {
                        "name": "alice",
                        "can_talk_to": ["bob", "carol"],
                        "can_receive_from": ["bob", "carol"],
                        "allowed_incoming_intents": ["ask", "notify"],
                        "granted_scopes": ["conversation"],
                    },
                    {
                        "name": "bob",
                        "can_talk_to": ["alice", "carol"],
                        "can_receive_from": ["alice", "carol"],
                        "allowed_incoming_intents": ["ask", "notify"],
                        "granted_scopes": ["conversation"],
                    },
                    {
                        "name": "carol",
                        "can_talk_to": ["alice", "bob"],
                        "can_receive_from": ["alice", "bob"],
                        "allowed_incoming_intents": ["ask", "notify"],
                        "granted_scopes": ["conversation"],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    return config_path, capabilities_path


def test_agent_project_ids_normalizes_single_and_multiple_values():
    assert agent_project_ids({"project_id": " prj-a ", "project_ids": ["prj-b", "", None]}) == {
        "prj-a",
        "prj-b",
    }
    assert agent_project_ids({"project_ids": "prj-c"}) == {"prj-c"}
    assert agent_project_ids({}) == set()


def test_evaluate_project_route_fails_closed_for_cross_project_target():
    decision = evaluate_project_route(
        project_id="prj-a",
        from_agent="alice",
        to_agent="carol",
        sender_row={"project_id": "prj-a"},
        target_row={"project_id": "prj-b"},
    )

    assert decision.allowed is False
    assert decision.reason == "target carol is not available in project prj-a"


def test_evaluate_project_route_allows_legacy_messages_without_project():
    decision = evaluate_project_route(
        project_id=None,
        from_agent="alice",
        to_agent="carol",
        sender_row={"project_id": "prj-a"},
        target_row={"project_id": "prj-b"},
    )

    assert decision.allowed is True


@pytest.mark.asyncio
async def test_conversation_router_allows_agents_in_requested_project(tmp_path):
    config_path, capabilities_path = _write_bridge_config(tmp_path)
    bob = _Runtime("bob")
    router = ConversationRouter(config_path, capabilities_path, tmp_path / "bridge.sqlite", [bob])

    result = await router.send_message(
        {
            "from_agent": "alice",
            "to_agent": "bob",
            "text": "Please help.",
            "project_id": "prj-a",
        }
    )

    assert result["ok"] is True
    assert result["status"] == "queued"
    assert bob.requests[0]["source"].startswith("bridge:")
    saved = router.get_message(result["message_id"])
    assert saved["project_id"] == "prj-a"
    assert saved["meta"]["project_id"] == "prj-a"


@pytest.mark.asyncio
async def test_conversation_router_rejects_cross_project_target(tmp_path):
    config_path, capabilities_path = _write_bridge_config(tmp_path)
    carol = _Runtime("carol")
    router = ConversationRouter(config_path, capabilities_path, tmp_path / "bridge.sqlite", [carol])

    with pytest.raises(PermissionError, match="target carol is not available in project prj-a"):
        await router.send_message(
            {
                "from_agent": "alice",
                "to_agent": "carol",
                "text": "Please help.",
                "project_id": "prj-a",
            }
        )

    rows = router.store._conn.execute("SELECT thread_id FROM threads").fetchall()
    assert len(rows) == 1
    thread = router.get_thread(rows[0]["thread_id"])
    assert thread["status"] == "rejected"
    assert carol.requests == []


@pytest.mark.asyncio
async def test_conversation_router_rejects_cross_project_sender_from_meta(tmp_path):
    config_path, capabilities_path = _write_bridge_config(tmp_path)
    bob = _Runtime("bob")
    router = ConversationRouter(config_path, capabilities_path, tmp_path / "bridge.sqlite", [bob])

    with pytest.raises(PermissionError, match="sender carol is not available in project prj-a"):
        await router.send_message(
            {
                "from_agent": "carol",
                "to_agent": "bob",
                "text": "Please help.",
                "meta": {"project_id": "prj-a"},
            }
        )

    assert bob.requests == []
