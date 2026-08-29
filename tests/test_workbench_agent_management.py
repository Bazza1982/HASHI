from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.workbench_api import WorkbenchApiServer


class _Request:
    def __init__(self, *, query=None, match_info=None, payload=None, headers=None):
        self.query = query or {}
        self.match_info = match_info or {}
        self._payload = payload
        self.headers = headers or {}

    async def json(self):
        return self._payload


def _config(agent_active: bool = False) -> dict:
    return {
        "global": {},
        "agents": [
            {
                "name": "lily",
                "display_name": "Lily",
                "emoji": "🌸",
                "workspace_dir": "workspaces/lily",
                "type": "flex",
                "active_backend": "codex-cli",
                "allowed_backends": [{"engine": "codex-cli", "model": "gpt-5.6"}],
                "is_active": agent_active,
            }
        ],
    }


def _server(tmp_path: Path, *, active: bool = False, orchestrator=None) -> WorkbenchApiServer:
    config_path = tmp_path / "agents.json"
    config_path.write_text(
        json.dumps(_config(active), indent=2) + "\n",
        encoding="utf-8-sig",
        newline="\r\n",
    )
    global_config = SimpleNamespace(
        deployment_profile="personal",
        bridge_home=tmp_path,
        workbench_port=18800,
        project_root=tmp_path,
    )
    return WorkbenchApiServer(
        config_path=config_path,
        global_config=global_config,
        orchestrator=orchestrator,
    )


@pytest.mark.asyncio
async def test_agents_can_include_inactive_for_authenticated_workbench_gateway(tmp_path):
    server = _server(tmp_path, active=False)

    normal = await server.handle_agents(_Request())
    complete = await server.handle_agents(_Request(query={"include_inactive": "1"}))

    assert json.loads(normal.text)["agents"] == []
    agents = json.loads(complete.text)["agents"]
    assert len(agents) == 1
    assert agents[0]["id"] == "lily"
    assert agents[0]["is_active"] is False
    assert agents[0]["status"] == "inactive"


@pytest.mark.asyncio
async def test_agent_metadata_update_preserves_config_encoding_and_updates_values(tmp_path):
    server = _server(tmp_path, active=False)

    response = await server.handle_agent_metadata(
        _Request(
            match_info={"name": "lily"},
            payload={"display_name": "Lily Moon", "emoji": "🌙"},
        )
    )

    assert response.status == 200
    payload = json.loads(response.text)
    assert payload["agent"]["display_name"] == "Lily Moon"
    assert payload["agent"]["emoji"] == "🌙"
    raw = server.config_path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in raw
    stored = json.loads(raw.decode("utf-8-sig"))
    assert stored["agents"][0]["display_name"] == "Lily Moon"


@pytest.mark.asyncio
async def test_agent_activation_persists_before_start_and_can_be_disabled(tmp_path):
    seen_active_values: list[bool] = []
    orchestrator = SimpleNamespace(runtimes=[])
    server = _server(tmp_path, active=False, orchestrator=orchestrator)

    async def start_agent(name: str):
        raw = json.loads(server.config_path.read_text(encoding="utf-8-sig"))
        seen_active_values.append(raw["agents"][0]["is_active"])
        return True, f"started {name}"

    async def stop_agent(name: str):
        return True, f"stopped {name}"

    orchestrator.start_agent = start_agent
    orchestrator.stop_agent = stop_agent

    activated = await server.handle_agent_active(
        _Request(match_info={"name": "lily"}, payload={"is_active": True})
    )
    disabled = await server.handle_agent_active(
        _Request(match_info={"name": "lily"}, payload={"is_active": False})
    )

    assert activated.status == 200
    assert json.loads(activated.text)["agent"]["is_active"] is True
    assert seen_active_values == [True]
    assert disabled.status == 200
    assert json.loads(disabled.text)["agent"]["is_active"] is False
    stored = json.loads(server.config_path.read_text(encoding="utf-8-sig"))
    assert stored["agents"][0]["is_active"] is False
