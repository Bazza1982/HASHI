from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator import workbench_api as workbench_module
from orchestrator.config_json import ConfigConflictError, read_config_json, write_config_json
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
        her_providers={
            "providers": {
                "hashi": {
                    "fast_model": "gpt-5.6-luna",
                    "pro_model": "gpt-5.6-sol",
                    "status": "provisional",
                }
            }
        },
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
async def test_agent_metadata_update_normalizes_config_encoding_and_updates_values(tmp_path):
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
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert b"\r" not in raw
    assert raw.endswith(b"\n")
    stored = json.loads(raw.decode("utf-8-sig"))
    assert stored["agents"][0]["display_name"] == "Lily Moon"


@pytest.mark.asyncio
async def test_agent_metadata_rejects_intervening_config_publication(tmp_path, monkeypatch):
    server = _server(tmp_path, active=False)
    actual_write = write_config_json

    def interleaved(path, stale):
        winner = read_config_json(path)
        winner["unrelated"] = {"kept": True}
        actual_write(path, winner)
        actual_write(path, stale)

    monkeypatch.setattr(workbench_module, "write_config_json", interleaved)

    before_agent = read_config_json(server.config_path)["agents"][0]["display_name"]
    with pytest.raises(ConfigConflictError):
        await server.handle_agent_metadata(
            _Request(
                match_info={"name": "lily"},
                payload={"display_name": "stale value"},
            )
        )

    persisted = read_config_json(server.config_path)
    assert persisted["unrelated"] == {"kept": True}
    assert persisted["agents"][0]["display_name"] == before_agent


@pytest.mark.asyncio
async def test_agent_activation_persists_before_start_and_can_be_disabled(tmp_path):
    seen_active_values: list[bool] = []
    orchestrator = SimpleNamespace(runtimes=[])
    server = _server(tmp_path, active=False, orchestrator=orchestrator)
    snapshot = read_config_json(server.config_path)
    snapshot["agents"].append(
        {
            "name": "guardian",
            "type": "flex",
            "workspace_dir": "workspaces/guardian",
            "active_backend": "codex-cli",
            "allowed_backends": [{"engine": "codex-cli"}],
            "is_active": True,
        }
    )
    write_config_json(server.config_path, snapshot)

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


@pytest.mark.asyncio
async def test_add_agent_api_rejects_raw_config_and_publishes_public_intent(tmp_path):
    server = _server(tmp_path, active=False)

    rejected = await server.handle_admin_add_agent(
        _Request(payload={"name": "unsafe", "backend": "codex-cli", "agent_cfg": {}})
    )
    assert rejected.status == 400
    assert json.loads(rejected.text)["error_code"] == "invalid_request"

    created = await server.handle_admin_add_agent(
        _Request(
            payload={
                "name": "new-agent",
                "display_name": "New Agent",
                "backend": "codex-cli",
                "model": "gpt-5.6-sol",
                "effort": "medium",
                "is_active": False,
            }
        )
    )

    assert created.status == 201
    response = json.loads(created.text)
    assert response["ok"] is True
    assert response["agent"] == {
        "name": "new-agent",
        "display_name": "New Agent",
        "is_active": False,
        "active_backend": "codex-cli",
    }
    stored = read_config_json(server.config_path)
    row = next(item for item in stored["agents"] if item["name"] == "new-agent")
    assert row["is_active"] is False
    assert row["allowed_backends"] == [
        {"engine": "codex-cli", "model": "gpt-5.6-sol", "effort": "medium"}
    ]
    assert (tmp_path / "workspaces" / "new-agent" / "agent.md").is_file()


@pytest.mark.asyncio
async def test_add_agent_api_starts_agent_when_created_active(tmp_path):
    start_calls: list[str] = []
    orchestrator = SimpleNamespace(runtimes=[])

    async def start_agent(name: str):
        start_calls.append(name)
        return True, f"started {name}"

    orchestrator.start_agent = start_agent
    server = _server(tmp_path, active=False, orchestrator=orchestrator)

    created = await server.handle_admin_add_agent(
        _Request(
            payload={
                "name": "ready-agent",
                "display_name": "Ready Agent",
                "backend": "codex-cli",
                "model": "gpt-5.6-sol",
                "is_active": True,
            }
        )
    )

    assert created.status == 201
    response = json.loads(created.text)
    assert response["ok"] is True
    assert response["agent"]["is_active"] is True
    assert response["lifecycle"] == {
        "ok": True,
        "message": "started ready-agent",
    }
    assert start_calls == ["ready-agent"]


@pytest.mark.asyncio
async def test_add_agent_api_start_failure_leaves_created_agent_inactive(tmp_path):
    orchestrator = SimpleNamespace(runtimes=[])

    async def start_agent(name: str):
        return False, f"worker failed for {name}"

    orchestrator.start_agent = start_agent
    server = _server(tmp_path, active=False, orchestrator=orchestrator)

    created = await server.handle_admin_add_agent(
        _Request(
            payload={
                "name": "offline-agent",
                "display_name": "Offline Agent",
                "backend": "codex-cli",
                "model": "gpt-5.6-sol",
                "is_active": True,
            }
        )
    )

    assert created.status == 503
    response = json.loads(created.text)
    assert response["ok"] is False
    assert response["error_code"] == "agent_start_failed"
    assert response["created"] == {"workspace": True, "config": True}
    assert response["agent"]["is_active"] is False
    assert response["lifecycle"] == {
        "ok": False,
        "message": "worker failed for offline-agent",
    }
    stored = read_config_json(server.config_path)
    row = next(item for item in stored["agents"] if item["name"] == "offline-agent")
    assert row["is_active"] is False


@pytest.mark.asyncio
async def test_add_agent_api_does_not_overwrite_newer_config_after_start_failure(
    tmp_path, monkeypatch
):
    orchestrator = SimpleNamespace(runtimes=[])

    async def start_agent(name: str):
        return False, f"worker failed for {name}"

    orchestrator.start_agent = start_agent
    server = _server(tmp_path, active=False, orchestrator=orchestrator)
    actual_write = write_config_json
    injected = False

    def interleaved(path, stale):
        nonlocal injected
        if not injected:
            injected = True
            winner = read_config_json(path)
            winner["newer_configuration"] = {"kept": True}
            created_row = next(
                item for item in winner["agents"] if item["name"] == "racing-agent"
            )
            created_row["is_active"] = False
            actual_write(path, winner)
        actual_write(path, stale)

    monkeypatch.setattr(workbench_module, "write_config_json", interleaved)

    created = await server.handle_admin_add_agent(
        _Request(
            payload={
                "name": "racing-agent",
                "display_name": "Racing Agent",
                "backend": "codex-cli",
                "model": "gpt-5.6-sol",
                "is_active": True,
            }
        )
    )

    assert created.status == 503
    response = json.loads(created.text)
    assert response["ok"] is False
    assert response["error_code"] == "agent_start_failed"
    assert response["agent"]["is_active"] is False
    assert "configuration changed" in response["configuration_warning"].lower()
    stored = read_config_json(server.config_path)
    assert stored["newer_configuration"] == {"kept": True}
    row = next(item for item in stored["agents"] if item["name"] == "racing-agent")
    assert row["is_active"] is False


@pytest.mark.asyncio
async def test_add_agent_api_persists_her_orchestration_effort(tmp_path):
    server = _server(tmp_path, active=False)

    retired = await server.handle_admin_add_agent(
        _Request(
            payload={
                "name": "old-preset",
                "backend": "her-v2",
                "preset": "balanced",
            }
        )
    )
    assert retired.status == 400
    assert json.loads(retired.text)["error_code"] == "invalid_request"

    created = await server.handle_admin_add_agent(
        _Request(
            payload={
                "name": "strategist",
                "display_name": "Strategist",
                "backend": "her-v2",
                "effort": "low",
                "is_active": False,
            }
        )
    )

    assert created.status == 201
    stored = read_config_json(server.config_path)
    row = next(item for item in stored["agents"] if item["name"] == "strategist")
    assert row["allowed_backends"][0]["engine"] == "her-v2"
    assert row["allowed_backends"][0]["effort"] == "low"
    assert set(row["allowed_backends"][0]["her_v2"]["profiles"]) == {
        "lightweight", "triage", "premium", "reviewer", "orchestrator"
    }


@pytest.mark.asyncio
async def test_add_agent_api_maps_duplicate_to_conflict(tmp_path):
    server = _server(tmp_path, active=False)
    request = _Request(payload={"name": "duplicate", "backend": "codex-cli"})

    first = await server.handle_admin_add_agent(request)
    second = await server.handle_admin_add_agent(request)

    assert first.status == 201
    assert second.status == 409
    assert json.loads(second.text)["error_code"] == "agent_exists"
