from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.agent_overview import build_agent_overview
from orchestrator.bridge_memory import SysPromptManager
from orchestrator.parked_topics import ParkedTopicStore
from orchestrator.workbench_api import WorkbenchApiServer
from orchestrator.workzone import save_workzone
from tools.token_tracker import get_summary, record_usage


def _runtime(workspace_dir: Path) -> SimpleNamespace:
    prompts = SysPromptManager(workspace_dir)
    prompts.save("1", "Keep the selected workzone in scope.")
    prompts.activate("1")
    prompts.save("2", "Review changes before committing.")
    parked = ParkedTopicStore(workspace_dir)
    parked.create_topic(
        title="Later review",
        summary_short="Return to the deployment checklist.",
        summary_long="Private long summary",
        recent_context="Private recent context",
        last_user_text="Private user text",
        last_assistant_text="Private assistant text",
        last_exchange_text="Private exchange",
        source_session="session-current",
    )
    return SimpleNamespace(
        name="akane",
        workspace_dir=workspace_dir,
        session_id_dt="session-current",
        sys_prompt_manager=prompts,
        parked_topics=parked,
    )


def test_overview_uses_canonical_hashi_state_and_keeps_all_sys_slots(tmp_path):
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    workzone = tmp_path / "project"
    workzone.mkdir()
    save_workzone(workspace_dir, workzone, source="test")
    record_usage(workspace_dir, "gpt-5.4", "codex-cli", 100, 30, 20, "session-current", 0.125)
    record_usage(workspace_dir, "claude-sonnet-4-6", "claude-cli", 40, 10, 0, "older", 0.025)
    runtime = _runtime(workspace_dir)
    metadata = {
        "id": "akane",
        "name": "akane",
        "display_name": "Akane",
        "status": "online",
        "online": True,
    }

    overview = build_agent_overview(
        metadata=metadata,
        workspace_dir=workspace_dir,
        runtime=runtime,
    )

    canonical = get_summary(workspace_dir, session_id="session-current")
    assert overview["agent"]["status"] == "online"
    assert overview["workzone"] == {"active": True, "path": str(workzone)}
    assert overview["usage"]["all_time"]["input"] == canonical["all_time"]["input"]
    assert overview["usage"]["all_time"]["cost_usd"] == canonical["all_time"]["cost_usd"]
    assert overview["usage"]["all_time"]["total"] == 200
    assert overview["usage"]["session"]["total"] == 150
    assert [item["model"] for item in overview["usage"]["by_model"]] == [
        "gpt-5.4",
        "claude-sonnet-4-6",
    ]

    prompts = overview["system_prompts"]
    assert (prompts["active_count"], prompts["configured_count"], prompts["total_count"]) == (1, 2, 10)
    assert [item["slot"] for item in prompts["slots"]] == [str(index) for index in range(1, 11)]
    assert prompts["slots"][0]["state"] == "on"
    assert prompts["slots"][1]["state"] == "off"
    assert prompts["slots"][2]["state"] == "empty"
    assert prompts["slots"][1]["preview"] == "Review changes before committing."

    parked = overview["parked_topics"]
    assert parked["count"] == 1
    assert parked["topics"][0]["slot"] == 1
    assert parked["topics"][0]["followup"]["status"] == "scheduled"
    serialized = json.dumps(overview)
    assert "Private recent context" not in serialized
    assert "Private user text" not in serialized


class _Request:
    def __init__(self, name: str):
        self.match_info = {"name": name}


@pytest.mark.asyncio
async def test_agent_overview_endpoint_is_single_agent_and_no_store(tmp_path):
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    runtime = _runtime(workspace_dir)
    server = WorkbenchApiServer.__new__(WorkbenchApiServer)
    server._runtime_map = lambda: {"akane": runtime}
    server._load_agent_rows = lambda: [{"name": "akane"}]
    server._is_governed_profile = lambda: False
    server._metadata_for_agent = lambda _row, _runtime: {
        "id": "akane",
        "name": "akane",
        "display_name": "Akane",
        "status": "local",
        "online": True,
        "workspace_dir": str(workspace_dir),
    }

    response = await server.handle_agent_overview(_Request("akane"))
    payload = json.loads(response.text)

    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert payload["ok"] is True
    assert payload["overview"]["agent"]["id"] == "akane"

    missing = await server.handle_agent_overview(_Request("missing"))
    assert missing.status == 404
