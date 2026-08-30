from __future__ import annotations

import json

import pytest
from aiohttp import web

from tui.api_client import TuiApiClient
from tui.instances import InstanceResolver, load_launch_instance


def test_load_launch_instance_accepts_bom_and_uses_repository_config(tmp_path):
    (tmp_path / "agents.json").write_text(
        "\ufeff" + json.dumps({"global": {"instance_id": "hashi9", "workbench_port": 19999}}),
        encoding="utf-8",
    )

    assert load_launch_instance(tmp_path) == ("HASHI9", 19999)


@pytest.mark.asyncio
async def test_instance_discovery_requires_handshake_liveness_and_proxy_capability(tmp_path):
    (tmp_path / "agents.json").write_text(
        json.dumps({"global": {"instance_id": "HASHI1", "workbench_port": 18800, "remote_port": 8766}}),
        encoding="utf-8",
    )
    peers = [
        {
            "instance_id": "HASHI2",
            "display_name": "HASHI2",
            "capabilities": ["handshake_v2", "tui_proxy_v1"],
            "properties": {"handshake_state": "handshake_accepted", "live_status": "online"},
            "route_kind": "same_host",
        },
        {
            "instance_id": "HASHI3",
            "display_name": "HASHI3",
            "capabilities": ["handshake_v2", "tui_proxy_v1"],
            "properties": {"handshake_state": "handshake_timed_out", "live_status": "offline"},
        },
        {
            "instance_id": "HASHI4",
            "display_name": "HASHI4",
            "capabilities": ["handshake_v2"],
            "properties": {"handshake_state": "handshake_accepted", "live_status": "online"},
        },
    ]

    async def health(_request):
        return web.json_response({"ok": True, "instance": {"instance_id": "HASHI1"}})

    async def peer_list(_request):
        return web.json_response({"ok": True, "peers": peers})

    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_get("/peers", peer_list)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    resolver = InstanceResolver(tmp_path, ["http://127.0.0.1:18800"])
    resolver._remote_candidates = lambda: [f"http://127.0.0.1:{port}"]
    try:
        targets = await resolver.discover(refresh=True)
    finally:
        await runner.cleanup()

    by_id = {target.instance_id: target for target in targets}
    assert by_id["HASHI1"].current is True
    assert by_id["HASHI2"].available is True
    assert by_id["HASHI2"].transport == "remote"
    assert by_id["HASHI3"].available is False
    assert by_id["HASHI3"].reason == "handshake required"
    assert by_id["HASHI4"].available is False
    assert "upgrade/restart" in by_id["HASHI4"].reason


@pytest.mark.asyncio
async def test_proxied_api_client_rejects_wrong_target_identity():
    async def proxy(_request):
        return web.json_response(
            {
                "ok": True,
                "target_instance": "HASHI9",
                "result": {"ok": True, "instance_id": "HASHI9"},
            }
        )

    app = web.Application()
    app.router.add_post("/tui/proxy", proxy)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    client = TuiApiClient(
        remote_url=f"http://127.0.0.1:{port}",
        target_instance="HASHI2",
        expected_instance_id="HASHI2",
    )
    try:
        health = await client.health_info()
    finally:
        await runner.cleanup()

    assert health["ok"] is False
    assert health["error"] == "target_identity_mismatch"
