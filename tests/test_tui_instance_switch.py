from __future__ import annotations

import pytest

pytest.importorskip("textual")

from tui.app import HASHITuiApp
from tui.instances import InstanceTarget


class _QuietTui(HASHITuiApp):
    async def _run_startup_sequence(self):
        return


class _Resolver:
    def __init__(self, targets):
        self.targets = targets

    async def discover(self, *, refresh=True):
        return self.targets


class _Candidate:
    def __init__(self, *, healthy=True, agents_ok=True):
        self.healthy = healthy
        self.agents_ok = agents_ok
        self.reset_agents = []

    async def health_info(self):
        return {"ok": True, "instance_id": "HASHI2"} if self.healthy else {"ok": False, "error": "offline"}

    async def agents_info(self):
        if not self.agents_ok:
            return {"ok": False, "error": "directory offline"}
        return {
            "ok": True,
            "agents": [
                {
                    "name": "agent2",
                    "display_name": "Agent Two",
                    "online": True,
                    "active_backend": "codex-cli",
                    "mode": "flex",
                }
            ],
        }

    async def get_recent_transcript(self, agent, limit=20):
        return []

    def reset_offset(self, agent):
        self.reset_agents.append(agent)


def _targets():
    return [
        InstanceTarget(
            instance_id="HASHI1",
            display_name="HASHI1",
            current=True,
            available=True,
            transport="direct",
            workbench_urls=("http://127.0.0.1:18800",),
        ),
        InstanceTarget(
            instance_id="HASHI2",
            display_name="HASHI2",
            current=False,
            available=True,
            transport="remote",
            remote_url="http://127.0.0.1:8766",
            handshake_state="handshake_accepted",
            live_status="online",
        ),
    ]


@pytest.mark.asyncio
async def test_instance_switch_commits_only_after_health_and_directory_checks(tmp_path):
    (tmp_path / "agents.json").write_text(
        '{"global":{"instance_id":"HASHI1","workbench_port":18800},"agents":[]}',
        encoding="utf-8",
    )
    app = _QuietTui(bridge_home=tmp_path, launch_instance_id="HASHI1")
    candidate = _Candidate()
    app._instance_resolver = _Resolver(_targets())
    app._client_for_instance = lambda target: candidate

    async with app.run_test() as pilot:
        await pilot.pause()
        await app._handle_instance_cmd("/instance HASHI2")
        await pilot.pause()

        assert app.current_instance_id == "HASHI2"
        assert app.api is candidate
        assert app.current_agent == "agent2"
        assert app._connection_generation == 1


@pytest.mark.asyncio
async def test_failed_instance_switch_keeps_existing_connection(tmp_path):
    (tmp_path / "agents.json").write_text(
        '{"global":{"instance_id":"HASHI1","workbench_port":18800},"agents":[]}',
        encoding="utf-8",
    )
    app = _QuietTui(bridge_home=tmp_path, launch_instance_id="HASHI1")
    original_api = app.api
    app._instance_resolver = _Resolver(_targets())
    app._client_for_instance = lambda target: _Candidate(healthy=False)

    async with app.run_test() as pilot:
        await pilot.pause()
        await app._handle_instance_cmd("/instance HASHI2")
        await pilot.pause()

        assert app.current_instance_id == "HASHI1"
        assert app.api is original_api
        assert app._connection_generation == 0
