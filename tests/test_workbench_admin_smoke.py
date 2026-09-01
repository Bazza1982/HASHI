from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from orchestrator.workbench_api import WorkbenchApiServer


class _FakeRequest:
    def __init__(self, payload: dict):
        self._payload = payload
        self.headers: dict[str, str] = {}

    async def json(self):
        return self._payload


class _FakeSmokeRuntime:
    name = "agent1"

    def __init__(self, workspace_dir: Path):
        self.workspace_dir = workspace_dir
        self.transcript_log_path = workspace_dir / "transcript.jsonl"
        self.core_transcript_log_path = workspace_dir / "core_transcript.jsonl"

    async def enqueue_api_text(self, text: str, *, source: str):
        assert text == "smoke prompt"
        assert source == "api-smoke"
        request_id = "req-smoke-current"
        records = [
            {
                "role": "assistant_core",
                "text": "stale response",
                "request_id": "req-smoke-other",
            },
            {
                "role": "assistant_core",
                "text": "HASHI3_SMOKE_OK",
                "visible_text": "HASHI3_SMOKE_OK",
                "request_id": request_id,
            },
        ]
        with self.core_transcript_log_path.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")
        return request_id


def _server(tmp_path: Path) -> WorkbenchApiServer:
    workspace_dir = tmp_path / "workspaces" / "agent1"
    workspace_dir.mkdir(parents=True)
    config_path = tmp_path / "agents.json"
    config_path.write_text(
        json.dumps(
            {
                "global": {},
                "agents": [
                    {
                        "name": "agent1",
                        "workspace_dir": "workspaces/agent1",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    global_config = SimpleNamespace(
        bridge_home=tmp_path,
        project_root=tmp_path,
        deployment_profile="personal",
    )
    return WorkbenchApiServer(
        config_path=config_path,
        global_config=global_config,
        runtimes=[_FakeSmokeRuntime(workspace_dir)],
    )


@pytest.mark.asyncio
async def test_admin_smoke_correlates_core_transcript_by_request_id(tmp_path):
    server = _server(tmp_path)

    response = await server.handle_admin_smoke(
        _FakeRequest(
            {
                "agent": "agent1",
                "include_commands": False,
                "include_chat": True,
                "chat_text": "smoke prompt",
                "timeout_s": 5,
            }
        )
    )

    payload = json.loads(response.text)
    chat = payload["results"][0]["chat"]
    assert payload["ok"] is True
    assert chat["received"] is True
    assert chat["request_id"] == "req-smoke-current"
    assert chat["assistant_text"] == "HASHI3_SMOKE_OK"
