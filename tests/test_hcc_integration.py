"""Real command/PCM/adapter/session/skill boundaries; no live providers required."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from adapters.her_v2 import HERv2Adapter
from orchestrator.admin_local_testing import execute_local_command
from orchestrator.bridge_memory import BridgeContextAssembler, BridgeMemoryStore
from orchestrator.config_json import ConfigDurabilityError
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator.hcc import is_hcc_enabled, set_hcc_enabled
from orchestrator.her_v2.backend_session import HerBackendSessionCoordinator, HerFixedProtocolError
from orchestrator.pcm import load_pcm_document, render_pcm_document
from orchestrator.pcm_transfer import build_agent_continuity_plan
from orchestrator.scheduler import TaskScheduler
from orchestrator.runtime_pipeline import _resolve_session_scope, SESSION_SCOPE_ISOLATED
from orchestrator.skill_manager import SkillManager
from orchestrator.workspace_state import WorkspaceStateStore

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "skills" / "hcc-refresh" / "scripts" / "hcc_update.py"


def workspace(path, cache="CURRENT_PRIVATE_CACHE"):
    path.mkdir(parents=True, exist_ok=True)
    (path / "agent.md").write_text(render_pcm_document(persona="P", system="S", memory="M", hcc=cache), encoding="utf-8")
    return path


def runtime_fixture(path):
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.name = "test-agent"
    runtime.workspace_dir = path
    runtime.global_config = SimpleNamespace(authorized_id=42, bridge_home=path.parent, project_root=path.parent, instance_id="TEST")
    runtime.config = SimpleNamespace(name=runtime.name, workspace_dir=path, active_backend="her-v2", access_scope="workspace", extra={})
    runtime._authorized_telegram_ids = [42]
    runtime._active_chat_ids = {}
    runtime.logger = logging.getLogger("test.hcc")
    runtime.error_logger = runtime.logger
    return runtime


@pytest.mark.asyncio
async def test_real_local_command_dispatch_persists_flag_and_status(tmp_path):
    home = workspace(tmp_path / "agent")
    runtime = runtime_fixture(home)
    before = (home / "agent.md").read_bytes()
    initial = await execute_local_command(runtime, "/hcc", session_metadata={"ui_locale": "zh-CN"})
    assert initial["ok"] and initial["messages"]
    # Cached HCC content is present in the fixture, so it is available by default.
    assert is_hcc_enabled(home)
    result = await execute_local_command(runtime, "/hcc on", session_metadata={"ui_locale": "zh-CN"})
    assert result["ok"], result
    assert WorkspaceStateStore(home).read()["hcc_enabled"] is True
    text = "\n".join(message["text"] for message in result["messages"])
    assert "缓存" in text and "CURRENT_PRIVATE_CACHE" not in text
    reloaded = runtime_fixture(home)
    result = await execute_local_command(reloaded, "/hcc off")
    assert result["ok"] and not is_hcc_enabled(home)
    # A new runtime and PCM assembly must honour the persisted opt-out.
    reloaded = runtime_fixture(home)
    assert WorkspaceStateStore(home).read()["hcc_enabled"] is False
    builder = BridgeContextAssembler(BridgeMemoryStore(home), home / "agent.md")
    adapter = transport_adapter(home, tmp_path / "restarted-her-state")
    _, accepted, _ = prepare(adapter, builder, 10)
    assert "CURRENT_PRIVATE_CACHE" not in accepted.materialized_prompt
    assert "HCC is user-configured temporary context" not in accepted.materialized_prompt
    assert (home / "agent.md").read_bytes() == before
    invalid = await execute_local_command(reloaded, "/hcc nonsense")
    assert "on|off" in str(invalid["messages"])
    assert not is_hcc_enabled(home)
    status = await execute_local_command(reloaded, "/hcc status")
    assert status["ok"] and "HCC" in str(status["messages"])
    assert not (home / "tasks.json").exists()


@pytest.mark.asyncio
async def test_command_reports_write_failure_without_overwriting_corrupt_state(tmp_path):
    home = workspace(tmp_path / "agent")
    state = home / "state.json"
    state.write_text("{broken", encoding="utf-8")
    result = await execute_local_command(runtime_fixture(home), "/hcc on")
    assert result["ok"] is False, result
    assert result["error_code"] == "hcc_save_unconfirmed"
    assert result["request_outcome"] == "unknown"
    text = "\n".join(message["text"] for message in result["messages"])
    assert "could not be confirmed" in text.lower(), text
    assert state.read_text() == "{broken"
    # The failed write must not persist an explicit enable flag (cached content
    # still exists, but no opt-in flag may be fabricated by a failed command).
    assert WorkspaceStateStore(home).read().get("hcc_enabled") is not True


@pytest.mark.asyncio
async def test_unauthorized_command_does_not_mutate(tmp_path):
    home = workspace(tmp_path / "agent")
    state = home / "state.json"
    original = {"hcc_enabled": False, "other_preference": {"value": "preserve"}}
    state.write_text(json.dumps(original, indent=2), encoding="utf-8")
    before_state, before_agent = state.read_bytes(), (home / "agent.md").read_bytes()
    runtime = runtime_fixture(home)
    update = SimpleNamespace(effective_user=SimpleNamespace(id=7), message=SimpleNamespace(reply_text=AsyncMock()))
    await runtime.cmd_hcc(update, SimpleNamespace(args=["on"]))
    assert state.read_bytes() == before_state
    assert json.loads(state.read_text(encoding="utf-8")) == original
    assert (home / "agent.md").read_bytes() == before_agent
    update.message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_hcc_write_preserves_entire_previous_state(tmp_path, monkeypatch):
    home = workspace(tmp_path / "agent")
    state = home / "state.json"
    original = {"hcc_enabled": False, "other_preference": {"value": "preserve"}}
    state.write_text(json.dumps(original, indent=2), encoding="utf-8")
    before_state, before_agent = state.read_bytes(), (home / "agent.md").read_bytes()
    original_replace = os.replace

    def fail_state_publication(source, destination):
        if Path(destination) == state:
            raise OSError("simulated atomic publication failure")
        return original_replace(source, destination)

    monkeypatch.setattr("orchestrator.config_json.os.replace", fail_state_publication)
    result = await execute_local_command(runtime_fixture(home), "/hcc on")
    assert result["ok"] is False, result
    assert result["error_code"] == "hcc_save_unconfirmed"
    assert result["request_outcome"] == "unknown"
    text = "\n".join(message["text"] for message in result["messages"])
    assert "could not be confirmed" in text.lower()
    assert state.read_bytes() == before_state
    assert json.loads(state.read_text(encoding="utf-8")) == original
    assert (home / "agent.md").read_bytes() == before_agent


@pytest.mark.asyncio
async def test_hcc_committed_durability_failure_is_not_retried(tmp_path, monkeypatch):
    home = workspace(tmp_path / "agent")
    state = home / "state.json"
    state.write_text(json.dumps({"hcc_enabled": False}), encoding="utf-8")
    calls = []

    def published_but_not_durable(workspace_dir, enabled):
        calls.append((workspace_dir, enabled))
        set_hcc_enabled(workspace_dir, enabled)
        raise ConfigDurabilityError("directory sync failed after publication")

    monkeypatch.setattr("orchestrator.runtime_hcc.hcc.set_hcc_enabled", published_but_not_durable)
    result = await execute_local_command(runtime_fixture(home), "/hcc on")
    assert result["ok"] is False, result
    assert result["error_code"] == "hcc_save_unconfirmed"
    assert result["request_outcome"] == "unknown"
    assert len(calls) == 1
    assert WorkspaceStateStore(home).read()["hcc_enabled"] is True


@pytest.mark.asyncio
async def test_hcc_command_uses_canonical_frontend_reply_owner(tmp_path):
    home = workspace(tmp_path / "agent")
    runtime = runtime_fixture(home)
    runtime._reply_text = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        effective_chat=SimpleNamespace(id=42),
        message=SimpleNamespace(
            reply_text=AsyncMock(
                side_effect=AssertionError("direct Telegram egress is forbidden")
            )
        ),
    )

    await runtime.cmd_hcc(update, SimpleNamespace(args=[]))

    runtime._reply_text.assert_awaited_once()
    update.message.reply_text.assert_not_awaited()


def transport_adapter(home, store):
    adapter = HERv2Adapter.__new__(HERv2Adapter)
    adapter._session_coordinator = HerBackendSessionCoordinator(store)
    adapter._session_id = "her-hcc-integration"
    adapter._fixed_transport_audit = {}
    adapter.config = SimpleNamespace(name="test-agent", workspace_dir=home)
    adapter.global_config = SimpleNamespace(instance_id="TEST")
    return adapter


def prepare(adapter, builder, number):
    payload = builder.build_prompt_payload(f"unrelated query {number}", "her-v2", incremental=True, inject_memory=False)
    encoded, audit = adapter.prepare_fixed_turn_input(
        prompt_payload=payload, user_message=f"unrelated query {number}", request_id=f"turn-{number}",
        request_meta={"owner_id": "42", "hashi_session_id": "hashi-hcc-integration", "context_generation": 1},
    )
    envelope = json.loads(encoded.split("\n", 1)[1])
    accepted = adapter._session_coordinator.accept(encoded)
    adapter._session_coordinator.complete(accepted, assistant_text=f"Answer {number}")
    return envelope, accepted, audit


@pytest.mark.parametrize("cache", ["CACHED BY DEFAULT", None, ""])
def test_default_pcm_adapter_includes_only_available_cached_hcc(tmp_path, cache):
    home = workspace(tmp_path / "agent", cache)
    assert not (home / "state.json").exists()
    builder = BridgeContextAssembler(BridgeMemoryStore(home), home / "agent.md")
    adapter = transport_adapter(home, tmp_path / "her-state")
    envelope, accepted, _ = prepare(adapter, builder, 1)
    sections = envelope["pcm_snapshot"]["sections"]
    if cache:
        assert sections["hcc"]["text"] == cache
        assert cache in accepted.materialized_prompt
    else:
        assert "hcc" not in sections and "hcc_usage" not in sections
        assert "HCC is user-configured temporary context" not in accepted.materialized_prompt
    assert not (home / "state.json").exists()


@pytest.mark.parametrize("removal", ["off", "empty", "absent"])
def test_pcm_to_real_adapter_to_durable_her_updates_and_revokes(tmp_path, removal, monkeypatch):
    home = workspace(tmp_path / "agent", "CACHE_VERSION_ONE")
    set_hcc_enabled(home, True)
    builder = BridgeContextAssembler(BridgeMemoryStore(home), home / "agent.md")
    # Freeze only the external clock input, so crossing a second is not a PCM change.
    clock_text = builder._build_time_fyi()
    monkeypatch.setattr(builder, "_build_time_fyi", lambda: clock_text)
    adapter = transport_adapter(home, tmp_path / "her-state")
    first, accepted, _ = prepare(adapter, builder, 1)
    assert first["operation"] == "open_session"
    assert first["pcm_snapshot"]["sections"]["hcc"]["text"] == "CACHE_VERSION_ONE"
    assert "CACHE_VERSION_ONE" in accepted.materialized_prompt
    second, accepted, _ = prepare(adapter, builder, 2)
    assert second["operation"] == "append_turn"
    assert second["pcm_delta"]["operations"] == []
    assert "CACHE_VERSION_ONE" in accepted.materialized_prompt, "unchanged cache is still visible each turn"
    workspace(home, "CACHE_VERSION_TWO")
    third, accepted, _ = prepare(adapter, builder, 3)
    assert [op["key"] for op in third["pcm_delta"]["operations"]] == ["hcc"]
    assert "CACHE_VERSION_TWO" in accepted.materialized_prompt
    assert "CACHE_VERSION_ONE" not in accepted.materialized_prompt
    if removal == "off":
        set_hcc_enabled(home, False)
    else:
        workspace(home, "" if removal == "empty" else None)
    # Recreate the coordinator: omission bugs must not resurrect durable PCM.
    adapter = transport_adapter(home, tmp_path / "her-state")
    fourth, accepted, _ = prepare(adapter, builder, 4)
    assert {op["key"] for op in fourth["pcm_delta"]["operations"] if op["op"] == "remove"} == {"hcc", "hcc_usage"}
    assert "CACHE_VERSION_TWO" not in accepted.materialized_prompt
    fifth, accepted, _ = prepare(adapter, builder, 5)
    assert fifth["pcm_delta"]["operations"] == []
    assert "CACHE_VERSION_TWO" not in accepted.materialized_prompt
    workspace(home, "CACHE_VERSION_THREE")
    set_hcc_enabled(home, True)
    sixth, accepted, _ = prepare(adapter, builder, 6)
    assert {op["key"] for op in sixth["pcm_delta"]["operations"]} == {"hcc", "hcc_usage"}
    assert "CACHE_VERSION_THREE" in accepted.materialized_prompt


@pytest.mark.parametrize("invalid", ["hcc", None, [7], [""]])
def test_adapter_rejects_malformed_removal_contract(tmp_path, invalid):
    home = workspace(tmp_path / "agent")
    adapter = transport_adapter(home, tmp_path / "her-state")
    with pytest.raises(HerFixedProtocolError, match="removed_section_keys"):
        adapter.prepare_fixed_turn_input(
            prompt_payload={"transport_snapshot": {"sections": [], "removed_section_keys": invalid}},
            user_message="q", request_id="bad", request_meta={},
        )


def run_cli(home, *args, input=None, env=None):
    environment = dict(os.environ)
    environment.pop("BRIDGE_WORKSPACE_DIR", None)
    environment.update(env or {})
    return subprocess.run([sys.executable, str(CLI), "--workspace", str(home), *args], input=input,
                          text=True, encoding="utf-8", capture_output=True, env=environment, timeout=15)


@pytest.mark.integration
def test_real_cli_inspect_replace_stale_and_bound_workspace(tmp_path):
    home = workspace(tmp_path / "agent", "")
    first = run_cli(home, "inspect", "weather")
    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout) == {"entry": "weather", "exists": False, "sha256": None}
    update = run_cli(home, "replace", "weather", "--expected", "absent", input="Observed 08:30: 晴 18C")
    assert update.returncode == 0, update.stderr
    assert json.loads(update.stdout)["updated"] is True
    assert "晴" not in update.stdout, "CLI receipt must not contain cache body"
    path = home / "agent.md"
    before = path.read_bytes()
    stale = run_cli(home, "replace", "weather", "--expected", "absent", input="old data")
    assert stale.returncode == 2 and "HCCConflictError" in stale.stderr
    assert path.read_bytes() == before
    revision = json.loads(run_cli(home, "inspect", "weather").stdout)["sha256"]
    empty = run_cli(home, "replace", "weather", "--expected", revision, input="")
    assert empty.returncode == 2 and path.read_bytes() == before
    wrong = workspace(tmp_path / "other-agent")
    cross = run_cli(wrong, "replace", "news", "--expected", "absent", input="data", env={"BRIDGE_WORKSPACE_DIR": str(home)})
    assert cross.returncode == 2 and "bound Agent" in cross.stderr
    # No cwd fallback: a child Workzone is not silently treated as Agent identity.
    env = {k: v for k, v in os.environ.items() if k != "BRIDGE_WORKSPACE_DIR"}
    missing = subprocess.run([sys.executable, str(CLI), "inspect", "weather"], cwd=home, env=env, capture_output=True, text=True, timeout=15)
    assert missing.returncode == 2 and "cwd is not an identity" in missing.stderr


@pytest.mark.asyncio
async def test_actual_scheduler_and_skill_manager_use_existing_job_path(tmp_path):
    home = workspace(tmp_path / "agent", "OLD_CACHE")
    project = tmp_path / "project"
    shutil.copytree(ROOT / "skills" / "hcc-refresh", project / "skills" / "hcc-refresh")
    manager = SkillManager(project, project / "tasks.json")
    runtime = runtime_fixture(home)
    runtime.skill_manager = manager
    runtime.enqueue_request = AsyncMock(return_value="queued-test")  # The external provider boundary only.
    state = home / "state.json"
    original = {"hcc_enabled": False, "other_preference": {"value": "preserve"}}
    state.write_text(json.dumps(original, indent=2), encoding="utf-8")
    before_state = state.read_bytes()
    scheduler = TaskScheduler(tasks_path=project / "tasks.json", state_path=project / "scheduler-state.json", runtimes=[runtime], authorized_id=42)
    cron = {"id": "weather", "agent": runtime.name, "schedule": "*/5 * * * *", "timezone": "Australia/Sydney", "action": "skill:hcc-refresh", "args": "Entry weather; source test fixture; maximum 100 tokens."}
    before = (home / "agent.md").read_bytes()
    ok = await scheduler._fire_cron_job(cron, runtime_map={runtime.name: runtime}, tasks={"crons": [cron]}, now_dt=datetime(2026, 9, 15, 1, tzinfo=timezone.utc))
    assert ok
    runtime.enqueue_request.assert_awaited_once()
    call = runtime.enqueue_request.await_args.kwargs
    assert call["skill_id"] == "hcc-refresh" and call["source"] == "scheduler-skill"
    assert "hcc_update.py" in call["prompt"] and "Entry weather" in call["prompt"]
    assert call["scheduler_context"]["task_id"] == "weather"
    assert _resolve_session_scope(SimpleNamespace(scheduler_context=call["scheduler_context"])) == SESSION_SCOPE_ISOLATED
    assert runtime.config.active_backend == "her-v2"
    assert state.read_bytes() == before_state, "enqueue must not mutate Agent settings"
    assert WorkspaceStateStore(home).read() == original
    assert (home / "agent.md").read_bytes() == before, "enqueue itself must not mutate source cache"


def test_portable_pcm_inventory_carries_hcc_and_preference_without_new_store(tmp_path):
    home = workspace(tmp_path / "source", "Source observation, not live at destination")
    set_hcc_enabled(home, True)
    plan = build_agent_continuity_plan(home)
    selected = {item.relative_path: item for item in plan.items}
    assert {"agent.md", "state.json"} <= selected.keys()
    destination = tmp_path / "destination"
    destination.mkdir()
    for key in ("agent.md", "state.json"):
        shutil.copyfile(selected[key].source, destination / key)
    assert load_pcm_document(destination / "agent.md").hcc == "Source observation, not live at destination"
    assert is_hcc_enabled(destination)
