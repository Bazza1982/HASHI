from __future__ import annotations

import json

import pytest
from multidict import MultiDict

from orchestrator.config_json import read_config_json, write_config_json
from tests.test_workbench_telegram_credential import ADMIN_TOKEN, _Request, _server


def _request(*, query=None, payload=None):
    request = _Request(headers={"X-Workbench-Token": ADMIN_TOKEN}, payload=payload)
    request.query = MultiDict(query or [])
    return request


@pytest.mark.asyncio
async def test_access_pages_are_bounded_revision_fenced_and_legacy_compatible(tmp_path):
    server = _server(tmp_path, verifier=None)
    config = read_config_json(server.config_path)
    config["global"]["authorized_telegram_user_ids"] = list(range(100, 351))
    config["agents"][0]["scoped_telegram_user_ids"] = list(range(1000, 1122))
    write_config_json(server.config_path, config)

    legacy = json.loads((await server.handle_admin_agent_telegram_access(_request())).text)
    assert len(legacy["global_user_ids"]) == 252
    assert "pagination" not in legacy

    first = json.loads((await server.handle_admin_agent_telegram_access(
        _request(query=[("page_size", "100")])
    )).text)
    assert first["primary_user_id"] == "42"
    assert len(first["global_user_ids"]) == 100
    assert len(first["agent_user_ids"]) == 100
    assert first["pagination"] == {
        "page_size": 100,
        "global": {"page": 0, "total": 252},
        "agent": {"page": 0, "total": 122},
    }
    second_response = await server.handle_admin_agent_telegram_access(
        _request(query=[("page_size", "100"), ("global_page", "1"),
                        ("agent_page", "1"), ("revision", first["revision"])])
    )
    second = json.loads(second_response.text)
    assert second_response.status == 200
    assert second["global_user_ids"] == legacy["global_user_ids"][100:200]
    assert second["agent_user_ids"] == legacy["agent_user_ids"][100:]
    assert second["revision"] == first["revision"]

    changed = server.telegram_access_service.change(
        "lily", expected_revision=first["revision"], scope="global",
        action="add", user_id="777",
    )
    stale = await server.handle_admin_agent_telegram_access(
        _request(query=[("page_size", "100"), ("global_page", "1"),
                        ("revision", first["revision"])])
    )
    assert stale.status == 409
    assert json.loads(stale.text)["error_code"] == "telegram_access_changed"
    assert changed["global_user_ids"][-1] == "777"


@pytest.mark.asyncio
async def test_bad_page_does_not_mutate_and_successful_change_returns_last_page(tmp_path):
    server = _server(tmp_path, verifier=None)
    config = read_config_json(server.config_path)
    config["agents"][0]["scoped_telegram_user_ids"] = list(range(100, 106))
    write_config_json(server.config_path, config)
    before = server.telegram_access_service.status("lily")
    payload = {"expected_revision": before["revision"], "scope": "agent",
               "action": "remove", "user_id": "105"}

    bad = await server.handle_admin_agent_telegram_access_change(
        _request(query=[("page_size", "0")], payload=payload)
    )
    assert bad.status == 400
    assert server.telegram_access_service.status("lily") == before

    duplicate = await server.handle_admin_agent_telegram_access(
        _request(query=[("page_size", "2"), ("page_size", "4")])
    )
    assert duplicate.status == 400

    result = await server.handle_admin_agent_telegram_access_change(
        _request(query=[("page_size", "2"), ("agent_page", "2")], payload=payload)
    )
    page = json.loads(result.text)
    assert result.status == 200
    assert page["agent_user_ids"] == ["104"]
    assert page["pagination"]["agent"] == {"page": 2, "total": 5}
    assert page["revision"] != before["revision"]


@pytest.mark.asyncio
async def test_access_paging_over_real_http_preserves_query_and_revision(tmp_path):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    server = _server(tmp_path, verifier=None)
    config = read_config_json(server.config_path)
    config["global"]["authorized_telegram_user_ids"] = list(range(100, 203))
    write_config_json(server.config_path, config)
    app = web.Application()
    path = "/api/admin/agents/{agent_id}/telegram-access"
    app.router.add_get(path, server.handle_admin_agent_telegram_access)
    app.router.add_put(path, server.handle_admin_agent_telegram_access_change)
    async with TestClient(TestServer(app)) as client:
        headers = {"X-Workbench-Token": ADMIN_TOKEN}
        first_response = await client.get(
            "/api/admin/agents/lily/telegram-access?page_size=100", headers=headers
        )
        assert first_response.status == 200
        first = await first_response.json()
        assert len(first["global_user_ids"]) == 100
        second_response = await client.get(
            "/api/admin/agents/lily/telegram-access",
            params={"page_size": "100", "global_page": "1", "revision": first["revision"]},
            headers=headers,
        )
        assert second_response.status == 200
        second = await second_response.json()
        assert second["global_user_ids"] == ["199", "200", "201", "202"]
        changed = await client.put(
            "/api/admin/agents/lily/telegram-access?page_size=100&global_page=1",
            headers=headers,
            json={"expected_revision": first["revision"], "scope": "global",
                  "action": "remove", "user_id": "202"},
        )
        assert changed.status == 200
        updated = await changed.json()
        assert updated["global_user_ids"] == ["199", "200", "201"]
        stale = await client.get(
            "/api/admin/agents/lily/telegram-access",
            params={"page_size": "100", "global_page": "1", "revision": first["revision"]},
            headers=headers,
        )
        assert stale.status == 409
