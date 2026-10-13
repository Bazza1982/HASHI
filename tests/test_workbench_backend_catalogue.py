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
        her_providers={
            "providers": {
                "deepseek": {
                    "engine": "deepseek-api",
                    "status": "stable",
                }
            }
        },
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
    assert payload["schema_version"] == 2
    assert payload["source"] == "hashi_backend_registry"
    assert "her-v2" not in payload["backends"]
    assert payload["backends"]["her-v3"] == {
        "engine": "her-v3",
        "label": "HERV3",
        "models": [],
        "default_model": None,
        "efforts": [],
        "default_effort": None,
        "privacy_levels": [0, 1, 2],
        "providers": {
            "deepseek-api": {
                "engine": "deepseek-api",
                "label": "deepseek",
                "models": ["deepseek-flash", "deepseek-v4-pro"],
                "default_model": "deepseek-v4-pro",
                "model_efforts": {
                    "deepseek-flash": ["off", "high", "max"],
                    "deepseek-v4-pro": ["off", "high", "max"],
                },
                "available": True,
                "status": "stable",
            }
        },
        "creation": {"mode": "provider_model"},
    }
    assert payload["backends"]["codex-cli"]["creation"] == {"mode": "model"}
    assert "deepseek-api" not in payload["backends"]
    assert "openrouter-api" not in payload["backends"]
    assert "secret_keys" not in payload["backends"]["codex-cli"]


def test_offline_agent_metadata_projects_her_v3_provider_model_and_effort(tmp_path):
    server = _server(tmp_path)
    metadata = server._metadata_for_agent(
        {
            "name": "offline-her",
            "type": "flex",
            "workspace_dir": "workspaces/offline-her",
            "active_backend": "her-v2",
            "allowed_backends": [
                {
                    "engine": "her-v2",
                    "model": "deepseek-v4-pro",
                    "effort": "high",
                    "her_v2": {
                        "main": {
                            "provider": "deepseek-api",
                            "model": "deepseek-v4-pro",
                        }
                    },
                }
            ],
        },
        None,
    )

    assert metadata["engine"] == "her-v3"
    assert metadata["active_backend"] == "her-v3"
    assert metadata["model"] == "deepseek-v4-pro"
    assert metadata["allowed_backends"] == [
        {
            "engine": "her-v3",
            "provider": "deepseek-api",
            "model": "deepseek-v4-pro",
            "models": ["deepseek-flash", "deepseek-v4-pro"],
            "effort": "high",
            "efforts": ["off", "high", "max"],
        }
    ]
def test_backend_catalogue_route_is_registered(tmp_path):
    server = _server(tmp_path)

    routes = {
        (route.method, route.resource.canonical)
        for route in server.app.router.routes()
    }
    assert ("GET", "/api/backends/catalogue") in routes
    assert ("POST", "/api/admin/add-agent") in routes


@pytest.mark.asyncio
async def test_catalogue_and_agent_creation_inherit_visible_native_codex_models(tmp_path, monkeypatch):
    from orchestrator.agent_creation import build_ordinary_backend_row

    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    (codex_home / "models_cache.json").write_text(json.dumps({
        "identity": "must-not-be-exposed",
        "models": [
            {"slug": "gpt-6.1-sol", "visibility": "list",
             "supported_reasoning_levels": [{"effort": "max"}, {"effort": "ultra"}]},
            {"slug": "internal-review", "visibility": "hide"},
        ],
    }), encoding="utf-8")
    server = _server(tmp_path)
    response = await server.handle_backend_catalogue(object())
    row = json.loads(response.text)["backends"]["codex-cli"]
    assert "gpt-6.1-sol" in row["models"]
    assert "internal-review" not in row["models"]
    assert row["model_efforts"]["gpt-6.1-sol"] == ["max", "ultra"]
    assert "must-not-be-exposed" not in response.text
    assert build_ordinary_backend_row("codex-cli", "gpt-6.1-sol", "ultra") == {
        "engine": "codex-cli", "model": "gpt-6.1-sol", "effort": "ultra",
    }

    from orchestrator.runtime_effort_options import get_available_models
    assert get_available_models("codex-cli", backend={"available_models": []}) == []
    with pytest.raises(ValueError):
        get_available_models("codex-cli", backend={"available_models": "all"})
    # A new cache revision is observed without restarting the Functions.
    (codex_home / "models_cache.json").write_text(json.dumps({"models": [
        {"slug": "next-cli-model", "visibility": "list"},
    ]}), encoding="utf-8")
    response = await server.handle_backend_catalogue(object())
    refreshed = json.loads(response.text)["backends"]["codex-cli"]["models"]
    assert "next-cli-model" in refreshed
    assert "gpt-6.1-sol" not in refreshed
    # Partial/malformed/missing cache falls back to qualified compatibility.
    for content in ("{", "[]", '{"models": [null, {"slug": "hidden", "visibility": "hide"}]}'):
        (codex_home / "models_cache.json").write_text(content, encoding="utf-8")
        assert "gpt-6-sol" in get_available_models("codex-cli")
        assert "hidden" not in get_available_models("codex-cli")
    (codex_home / "models_cache.json").unlink()
    assert "gpt-6-sol" in get_available_models("codex-cli")
