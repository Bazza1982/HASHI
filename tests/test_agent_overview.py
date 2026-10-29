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
    from orchestrator.workzone import workzone_execution_platform, workzone_path_kind

    expected_kind = workzone_path_kind(str(workzone))
    expected_platform = workzone_execution_platform()
    assert overview["workzone"] == {
        "active": True,
        "revision": 0,
        "active_revision": 0,
        "pending_activation": False,
        "path": str(workzone),
        "path_kind": expected_kind,
        "execution_platform": expected_platform,
        "host_compatible": True,
        "active_count": 1,
        "configured_count": 1,
        "slots": [
            {
                "slot": "main",
                "role": "primary",
                "path": str(workzone),
                "label": workzone.name,
                "enabled": True,
                "available": True,
                "path_kind": expected_kind,
                "execution_platform": expected_platform,
                "host_compatible": True,
            }
        ],
    }
    assert overview["usage"]["all_time"]["input"] == canonical["all_time"]["input"]
    assert overview["usage"]["all_time"]["cost_usd"] == canonical["all_time"]["cost_usd"]
    # Provider completion tokens already contain their reasoning-token subset.
    assert overview["usage"]["all_time"]["total"] == 180
    assert overview["usage"]["session"]["total"] == 130
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


def test_overview_projects_runtime_multi_workzones_instead_of_stale_legacy_file(
    tmp_path,
):
    workspace = tmp_path / "workspace"
    legacy = tmp_path / "legacy"
    attached = tmp_path / "attached"
    for path in (workspace, legacy, attached):
        path.mkdir()
    save_workzone(workspace, legacy, source="legacy")
    runtime = _runtime(workspace)
    runtime._workzone_state = {
        "revision": 9,
        "slots": [
            {
                "slot_id": "main",
                "path": str(legacy),
                "enabled": False,
            },
            {
                "slot_id": "1",
                "path": str(attached),
                "enabled": True,
                "label": "shared",
            },
        ],
    }

    overview = build_agent_overview(
        metadata={"name": "akane", "status": "online", "online": True},
        workspace_dir=workspace,
        runtime=runtime,
    )["workzone"]

    assert overview["active"] is True
    assert overview["path"] is None
    assert overview["revision"] == 9
    assert overview["active_revision"] == 9
    assert overview["pending_activation"] is False
    assert overview["active_count"] == 1
    assert overview["configured_count"] == 2
    assert [(slot["slot"], slot["enabled"]) for slot in overview["slots"]] == [
        ("main", False),
        ("1", True),
    ]


def test_overview_prefers_newer_authoritative_workzone_to_worker_metadata(tmp_path):
    workspace = tmp_path / "workspace"
    active = tmp_path / "active"
    desired = tmp_path / "desired"
    attached = tmp_path / "attached"
    for path in (workspace, active, desired, attached):
        path.mkdir()
    runtime = _runtime(workspace)
    metadata = {
        "name": "akane",
        "status": "online",
        "online": True,
        "workzone_state": {
            "revision": 1,
            "slots": [
                {"slot_id": "main", "path": str(active), "enabled": True},
            ],
        },
    }

    overview = build_agent_overview(
        metadata=metadata,
        workspace_dir=workspace,
        runtime=runtime,
        desired_workzone_state={
            "revision": 2,
            "slots": [
                {"slot_id": "main", "path": str(desired), "enabled": True},
                {"slot_id": "3", "path": str(attached), "enabled": True},
            ],
        },
    )["workzone"]

    assert overview["revision"] == 2
    assert overview["active_revision"] == 1
    assert overview["pending_activation"] is True
    assert [slot["path"] for slot in overview["slots"]] == [
        str(desired.resolve()),
        str(attached.resolve()),
    ]


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
    server._v1_owner_id = lambda _request: "user:77"
    desired = tmp_path / "desired"
    desired.mkdir()
    requested = []
    server.session_store = SimpleNamespace(
        get_agent_workzone_set=lambda **kwargs: (
            requested.append(kwargs)
            or {
                "revision": 2,
                "slots": [
                    {
                        "slot_id": "main",
                        "path": str(desired),
                        "enabled": True,
                    }
                ],
            }
        )
    )
    server._metadata_for_agent = lambda _row, _runtime: {
        "id": "akane",
        "name": "akane",
        "display_name": "Akane",
        "status": "local",
        "online": True,
        "workspace_dir": str(workspace_dir),
        "workzone_state": {
            "revision": 1,
            "slots": [
                {
                    "slot_id": "main",
                    "path": str(workspace_dir),
                    "enabled": True,
                }
            ],
        },
    }

    response = await server.handle_agent_overview(_Request("akane"))
    payload = json.loads(response.text)

    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert payload["ok"] is True
    assert payload["overview"]["agent"]["id"] == "akane"
    assert payload["overview"]["workzone"]["revision"] == 2
    assert payload["overview"]["workzone"]["active_revision"] == 1
    assert payload["overview"]["workzone"]["pending_activation"] is True
    assert payload["overview"]["workzone"]["path"] == str(desired.resolve())
    assert requested == [{"owner_id": "user:77", "agent_id": "akane"}]

    missing = await server.handle_agent_overview(_Request("missing"))
    assert missing.status == 404
