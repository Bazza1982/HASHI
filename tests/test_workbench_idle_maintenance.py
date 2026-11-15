from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace as NS

import pytest

from orchestrator.workbench_api import WorkbenchApiServer


class _LiveVoice:
    def __init__(self, *, start_error=None):
        self.start_error = start_error

    async def start(self):
        if self.start_error is not None:
            raise self.start_error
        return None

    async def shutdown(self):
        return None


@pytest.mark.asyncio
async def test_optional_maintenance_cannot_block_api_listener(tmp_path, monkeypatch):
    config_path = tmp_path / "agents.json"
    config_path.write_text(
        json.dumps(
            {
                "global": {},
                "agents": [{"name": "agent", "workspace_dir": "workspaces/agent"}],
            }
        ),
        encoding="utf-8",
    )
    server = WorkbenchApiServer(
        config_path=config_path,
        global_config=NS(
            bridge_home=tmp_path,
            project_root=tmp_path,
            instance_id="fixture",
            authorized_id=7,
            deployment_profile="personal",
            api_host="127.0.0.1",
            workbench_port=0,
        ),
        reconcile_session_runs=False,
    )
    server.live_voice_manager = _LiveVoice(
        start_error=OSError("simulated optional Live Voice recovery failure")
    )

    def failed_optional_maintenance():
        raise OSError("simulated optional maintenance failure")

    monkeypatch.setattr(
        server.session_store, "cleanup_attachments", failed_optional_maintenance
    )
    monkeypatch.setattr(
        server.session_store, "cleanup_audio_assets", failed_optional_maintenance
    )

    await asyncio.wait_for(server.start(), timeout=1.0)
    try:
        await asyncio.sleep(0)
        assert server.bound_port > 0
    finally:
        await server.shutdown()
