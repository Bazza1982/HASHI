from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.workbench_api import WorkbenchApiServer


def _server(tmp_path: Path) -> WorkbenchApiServer:
    config_path = tmp_path / "agents.json"
    config_path.write_text('{"global": {}, "agents": []}\n', encoding="utf-8")
    global_config = SimpleNamespace(
        deployment_profile="personal",
        bridge_home=tmp_path,
        workbench_port=18800,
        project_root=tmp_path,
    )
    return WorkbenchApiServer(
        config_path=config_path,
        global_config=global_config,
        reconcile_session_runs=False,
    )


@pytest.mark.asyncio
async def test_backend_catalogue_exposes_public_selectable_registry(tmp_path):
    server = _server(tmp_path)

    response = await server.handle_backend_catalogue(object())

    assert response.status == 200
    payload = json.loads(response.text)
    assert payload["ok"] is True
    assert payload["schema_version"] == 1
    assert payload["source"] == "hashi_backend_registry"
    assert payload["backends"]["her-v2"] == {
        "engine": "her-v2",
        "label": "HER",
        "models": ["role-configured"],
        "default_model": "role-configured",
        "efforts": ["zero", "low", "medium"],
        "default_effort": "medium",
        "privacy_levels": [0, 1],
    }
    assert "deepseek-api" not in payload["backends"]
    assert "openrouter-api" not in payload["backends"]
    assert "secret_keys" not in payload["backends"]["codex-cli"]


def test_backend_catalogue_route_is_registered(tmp_path):
    server = _server(tmp_path)

    routes = {
        (route.method, route.resource.canonical)
        for route in server.app.router.routes()
    }
    assert ("GET", "/api/backends/catalogue") in routes
