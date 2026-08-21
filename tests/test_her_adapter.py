from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import stat
import textwrap
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import adapters.her as her_adapter_module
from adapters.claw_cli import ClawCLIAdapter
from adapters.her import (
    ClawBinaryNotFound,
    ClawCommandError,
    ClawError,
    ClawJsonError,
    ClawPackagedRuntimeError,
    ClawProviderConfigError,
    ClawProviderSecretMissing,
    ClawTaskResult,
    ClawTimeoutError,
    HERAdapter,
    _build_claw_technical_lease,
    _claw_compact_execution_ledger,
    _claw_contains_dangling_tool_markup,
    _claw_incomplete_response,
    _claw_jsonl_to_stream_events,
    _claw_run_is_incomplete,
    _HERStreamCadenceController,
    _parse_json_output,
    _parse_stream_json_output,
    build_claw_env,
    build_claw_task_args,
    detect_hashi_claw_platform,
    discover_claw_binary,
    find_claw_binary,
    load_packaged_claw_manifest,
    resolve_packaged_claw_binary,
    run_claw_doctor,
    run_claw_json_command,
    run_claw_task,
)
from adapters.registry import get_backend_class
from adapters.stream_events import (
    DELIVERY_CONTROL,
    DELIVERY_FINAL,
    DELIVERY_INTERNAL,
    DELIVERY_TECHNICAL,
    DELIVERY_USER_COMMENTARY,
    KIND_ACKNOWLEDGEMENT,
    KIND_COMMENTARY,
    KIND_PROGRESS,
    KIND_TEXT_DELTA,
    KIND_THINKING,
    KIND_TOOL_END,
    KIND_TOOL_START,
    StreamEvent,
)
from orchestrator.flexible_backend_registry import (
    allows_custom_models,
    get_secret_lookup_order,
    is_cli_backend,
)


def _write_exe(path: Path, body: str) -> Path:
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _write_development_selection(
    bridge_home: Path, binary_body: str = "#!/bin/sh\nexit 0\n"
) -> Path:
    from adapters.her import detect_hashi_claw_platform

    state_root = bridge_home / "state" / "her_rebuild"
    candidate_id = "dev-test-candidate"
    candidate_dir = state_root / "candidates" / candidate_id
    candidate_dir.mkdir(parents=True)
    binary = candidate_dir / (
        "claw.exe" if detect_hashi_claw_platform().system == "windows" else "claw"
    )
    binary.write_text(binary_body, encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    build_log = candidate_dir / "build.log"
    build_log.write_text("test build\n", encoding="utf-8")
    verification = {"schema_version": 1, "result": "passed"}
    (candidate_dir / "quick-verification.json").write_text(
        json.dumps(verification), encoding="utf-8"
    )
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    target = detect_hashi_claw_platform().rust_target_triple
    metadata = {
        "schema_version": 1,
        "candidate_id": candidate_id,
        "job_id": "rebuild-test",
        "development_build": True,
        "production_certified": False,
        "source_fingerprint": "a" * 64,
        "source_git_head": "b" * 40,
        "source_dirty": False,
        "target": target,
        "profile": "hashi-dev",
        "features": [],
        "cargo_version": "cargo test",
        "rustc_version": "rustc test",
        "build_started_at": "2026-08-16T00:00:00+00:00",
        "build_finished_at": "2026-08-16T00:00:01+00:00",
        "build_duration_seconds": 1.0,
        "binary_name": binary.name,
        "binary_sha256": digest,
        "binary_size": binary.stat().st_size,
        "candidate_dir": str(candidate_dir.resolve()),
        "binary_path": str(binary.resolve()),
        "build_log_path": str(build_log.resolve()),
        "quick_verification": verification,
        "created_at": "2026-08-16T00:00:01+00:00",
    }
    (candidate_dir / "candidate.json").write_text(
        json.dumps(metadata), encoding="utf-8"
    )
    selection = {
        "schema_version": 1,
        "active": {
            "candidate_id": candidate_id,
            "candidate_path": str(candidate_dir.resolve()),
            "binary_path": str(binary.resolve()),
            "binary_sha256": digest,
            "source_fingerprint": "a" * 64,
            "target": target,
            "profile": "hashi-dev",
            "development_build": True,
            "production_certified": False,
        },
        "previous": None,
        "selected_at": "2026-08-16T00:00:01+00:00",
        "selecting_job_id": "rebuild-test",
        "adoption_state": "selected_not_yet_adopted",
    }
    selection_path = state_root / "development-selection.json"
    selection_path.write_text(json.dumps(selection), encoding="utf-8")
    return binary


def test_private_raw_log_chmod_failure_does_not_break_persistence(
    monkeypatch, tmp_path
):
    adapter = HERAdapter.__new__(HERAdapter)
    adapter.config = SimpleNamespace(workspace_dir=tmp_path)

    def reject_chmod(_path, _mode):
        raise OSError("permission hardening unavailable")

    monkeypatch.setattr(Path, "chmod", reject_chmod)
    adapter._persist_stream_json_line(b'{"kind":"run_started"}')
    adapter._persist_control_event(
        "req-chmod",
        {"kind": "control_invocation", "gate": "planning"},
    )

    assert "run_started" in (tmp_path / "claw_exec_events.jsonl").read_text()
    assert "req-chmod" in (tmp_path / "claw_control_events.jsonl").read_text()


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (
            {
                "status": "ok",
                "configured_servers": 1,
                "config_load_error": None,
                "servers": [{"name": "hashi-tools", "valid": True}],
            },
            True,
        ),
        (
            {
                "status": "ok",
                "configured_servers": 1,
                "config_load_error": None,
                "servers": [{"name": "hashi-tools", "required": True}],
            },
            True,
        ),
        (
            {
                "status": "error",
                "configured_servers": 1,
                "config_load_error": "invalid settings",
                "servers": [{"name": "hashi-tools", "required": True}],
            },
            False,
        ),
        (
            {
                "status": "ok",
                "configured_servers": 1,
                "config_load_error": None,
                "servers": [{"name": "hashi-tools", "valid": False}],
            },
            False,
        ),
    ],
)
def test_tool_gateway_accepts_legacy_and_current_mcp_list_contracts(
    monkeypatch, tmp_path, status, expected
):
    adapter = HERAdapter.__new__(HERAdapter)
    adapter.config = SimpleNamespace(workspace_dir=tmp_path, extra={})
    adapter._gateway_context_path = tmp_path / "context.json"
    adapter._gateway_config_home = tmp_path / "config"
    adapter._binary = tmp_path / "hashi-her"
    adapter.logger = logging.getLogger("test.her.gateway")
    adapter._task_env = dict

    registry = SimpleNamespace(get_tool_definitions=lambda: [{"name": "probe"}])
    context = SimpleNamespace(build_registry=lambda: registry)
    monkeypatch.setattr(
        "tools.gateway.context.load_gateway_context", lambda _path: context
    )
    monkeypatch.setattr(
        "adapters.her.run_claw_json_command",
        lambda *_args, **_kwargs: SimpleNamespace(json_data=status),
    )

    if expected:
        adapter._validate_tool_gateway()
    else:
        with pytest.raises(
            ClawProviderConfigError,
            match="required HASHI Tool Gateway is invalid",
        ):
            adapter._validate_tool_gateway()


def test_tool_gateway_settings_disable_unavailable_native_web_search(
    monkeypatch, tmp_path
):
    adapter = HERAdapter.__new__(HERAdapter)
    adapter.config = SimpleNamespace(workspace_dir=tmp_path, name="lulu")
    adapter.global_config = SimpleNamespace(base_media_dir=None, workbench_port=18800)
    adapter.tool_registry = SimpleNamespace(audit_context={})
    adapter.logger = logging.getLogger("test.her.gateway.settings")
    adapter._gateway_context_path = None
    adapter._gateway_config_home = None
    context = SimpleNamespace(agent="lulu", allowed_tools=("web_search",))

    captured = {}

    def fake_write_gateway_context(*_args, **kwargs):
        captured.update(kwargs)
        return context

    monkeypatch.setattr(
        "tools.gateway.context.write_gateway_context",
        fake_write_gateway_context,
    )

    adapter._prepare_tool_gateway()

    settings_path = tmp_path / "backend_state" / "her_config" / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert settings["permissions"]["deniedTools"] == ["WebSearch"]
    assert "hashi-tools" in settings["mcpServers"]
    assert stat.S_IMODE(settings_path.stat().st_mode) == 0o600
    assert captured["workbench_api_base_url"] == "http://127.0.0.1:18800"


def test_claw_replan_without_model_commentary_remains_technical():
    prompt = """
    你的名字是 Sunny。
    称呼用户为「爸爸」。
    - **Emoji:** ☀️
    """
    events = _claw_jsonl_to_stream_events(
        {
            "kind": "task_plan",
            "phase": "replan",
            "frame": {
                "active_goal": "修复任务",
                "completed": ["定位失败原因"],
                "remaining_work": ["运行回归测试"],
                "failures": [],
                "next_action": "运行针对性回归测试",
            },
        },
        commentary_prompt=prompt,
    )

    assert all(event.kind != KIND_COMMENTARY for event in events)
    plan = events[0]
    assert plan.kind == KIND_PROGRESS
    assert plan.delivery_class == DELIVERY_TECHNICAL
    assert plan.origin == "her_planner"


def test_claw_task_plan_never_reuses_acknowledgement_as_commentary():
    events = _claw_jsonl_to_stream_events(
        {
            "kind": "task_plan",
            "phase": "replan",
            "revision": 2,
            "frame": {
                "active_goal": "修复任务",
                "acknowledgement": "Sunny 已确认第一阶段结果，接下来会核验修复。☀️",
            },
        },
        request_id="req-persona",
    )

    assert all(event.kind != KIND_COMMENTARY for event in events)
    assert events[0].delivery_class == DELIVERY_TECHNICAL


def test_claw_explicit_task_commentary_is_user_visible():
    events = _claw_jsonl_to_stream_events(
        {
            "kind": "task_commentary",
            "phase": "execution",
            "revision": 2,
            "text": "Sunny 已完成日志核验，正在运行最后一项测试。☀️",
        },
        request_id="req-persona",
    )

    [commentary] = events
    assert commentary.kind == KIND_COMMENTARY
    assert commentary.delivery_class == DELIVERY_USER_COMMENTARY
    assert commentary.event_id == "req-persona:commentary:execution:2:0"
    assert commentary.provenance == "model_authored"


def test_claw_tool_bound_assistant_commentary_is_user_visible_primary_model_text():
    events = _claw_jsonl_to_stream_events(
        {
            "kind": "assistant_commentary",
            "phase": "execution",
            "iteration": 3,
            "event_id": "assistant-commentary:3",
            "text": "Sunny found the source record and is checking it now. ☀️",
        },
        request_id="req-assistant-commentary",
    )

    [commentary] = events
    assert commentary.kind == KIND_COMMENTARY
    assert commentary.delivery_class == DELIVERY_USER_COMMENTARY
    assert commentary.event_id == "assistant-commentary:3"
    assert commentary.origin == "primary_model"
    assert commentary.phase == "execution"
    assert commentary.provenance == "model_authored"


def test_claw_provider_retry_remains_technical_verbose_telemetry():
    events = _claw_jsonl_to_stream_events(
        {
            "kind": "provider_retry",
            "attempt": 2,
            "max_attempts": 2,
            "reason": "504 Gateway Timeout",
            "summary": "retrying incomplete provider stream (2/2)",
        },
        request_id="req-provider-retry",
    )

    [retry] = events
    assert retry.kind == KIND_PROGRESS
    assert retry.delivery_class == DELIVERY_TECHNICAL
    assert retry.origin == "her_runtime"
    assert retry.phase == "execution"


def test_claw_internal_error_is_technical_unless_user_action_is_required():
    [internal] = _claw_jsonl_to_stream_events(
        {"kind": "error", "error": "planner response was invalid JSON"},
        request_id="req-error",
    )
    [actionable] = _claw_jsonl_to_stream_events(
        {
            "kind": "error",
            "error": "approval is required",
            "user_action_required": True,
        },
        request_id="req-error",
        source_index=2,
    )

    assert internal.delivery_class == DELIVERY_TECHNICAL
    assert internal.required is False
    assert actionable.delivery_class == DELIVERY_CONTROL
    assert actionable.required is True


def test_claw_initial_task_plan_does_not_duplicate_acknowledgement_as_commentary():
    events = _claw_jsonl_to_stream_events(
        {
            "kind": "task_plan",
            "phase": "initial",
            "frame": {
                "active_goal": "inspect",
                "completed": [],
                "remaining_work": ["inspect"],
                "next_action": "inspect",
            },
        },
        commentary_prompt="You are Sunny.",
    )

    assert all(event.kind != KIND_COMMENTARY for event in events)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("started", "🧠 semantic_compaction started"),
        ("completed", "✅ semantic_compaction completed"),
        ("failed", "⚠️ semantic_compaction failed"),
    ],
)
def test_semantic_compaction_lifecycle_maps_to_bounded_verbose_progress(
    status, expected
):
    event = {
        "kind": "semantic_compaction",
        "status": status,
        "request_id": "req-compaction",
        "session_id": "session-compaction",
        "trigger_phase": "post_tool",
        "estimated_input_tokens": 351_000,
        "removed_message_count": 144 if status == "completed" else 0,
        "timeout_seconds": 3_595,
        "timeout_source": "user override",
        "elapsed_ms": 40_840,
        "original_context_unchanged": status != "completed",
        "will_continue": True,
        "reason": "provider call timed out" if status == "failed" else "",
    }

    [mapped] = _claw_jsonl_to_stream_events(event)

    assert mapped.kind == KIND_PROGRESS
    assert expected in mapped.summary
    assert len(mapped.summary) <= 500
    assert "request_id=req-compaction" in mapped.detail
    assert "session_id=session-compaction" in mapped.detail
    assert "trigger_phase=post_tool" in mapped.detail
    assert "estimated_input_tokens=351000" in mapped.detail
    assert "timeout_seconds=3595" in mapped.detail
    assert "timeout_source=user override" in mapped.detail
    assert "elapsed_ms=40840" in mapped.detail
    if status == "failed":
        assert "original context unchanged" in mapped.summary
        assert "continuing" in mapped.summary


@pytest.mark.asyncio
async def test_claw_technical_lease_emits_neutral_update_and_stops_cleanly():
    received = []

    async def callback(event):
        received.append(event)

    prompt = "你的名字是 Sunny。称呼用户为「爸爸」。\n- **Emoji:** ☀️"
    controller = _HERStreamCadenceController(
        callback,
        prompt=prompt,
        progress_enabled=True,
        first_update_s=0.01,
        target_interval_s=0.02,
        hard_interval_s=0.03,
        activity_grace_s=0.001,
    )
    lease_task = asyncio.create_task(controller.run())
    for _ in range(30):
        if received:
            break
        await asyncio.sleep(0.005)
    controller.close()
    await lease_task

    assert len(received) == 1
    assert received[0].kind == KIND_PROGRESS
    assert received[0].delivery_class == DELIVERY_TECHNICAL
    assert received[0].origin == "her_runtime"
    assert received[0].summary == _build_claw_technical_lease(prompt)
    assert received[0].summary.startswith("HER 仍在处理")
    assert "爸爸" not in received[0].summary
    assert "Sunny" not in received[0].summary
    assert "☀️" not in received[0].summary


@pytest.mark.asyncio
async def test_claw_cadence_coalesces_pending_commentary_to_newest_event():
    received = []
    delivered = asyncio.Event()

    async def callback(event):
        received.append(event)
        if event.kind == KIND_COMMENTARY and event.delivery_class != DELIVERY_INTERNAL:
            delivered.set()

    controller = _HERStreamCadenceController(
        callback,
        prompt="You are Sunny.",
        progress_enabled=True,
        first_update_s=0.01,
        target_interval_s=0.02,
        hard_interval_s=0.03,
        activity_grace_s=0,
    )
    first = StreamEvent(
        kind=KIND_COMMENTARY,
        summary="Sunny has completed the inspection and will run the focused check next.",
        event_id="req-commentary:replan:1",
    )
    second = replace(first, event_id="req-commentary:replan:2")

    cadence_task = asyncio.create_task(controller.run())
    await controller.forward(first)
    await controller.forward(second)
    await asyncio.wait_for(delivered.wait(), timeout=1)
    controller.close()
    await cadence_task

    delivered_commentary = [
        event
        for event in received
        if event.kind == KIND_COMMENTARY and event.delivery_class != DELIVERY_INTERNAL
    ]
    assert delivered_commentary == [second]
    suppressed = next(event for event in received if event.event_id == first.event_id)
    assert suppressed.delivery_class == DELIVERY_INTERNAL
    assert "suppressed_reason=coalesced_by_newer_commentary" in suppressed.detail


@pytest.mark.asyncio
async def test_claw_cadence_technical_activity_does_not_delay_persona_commentary():
    received = []
    delivered = asyncio.Event()

    async def callback(event):
        received.append((time.monotonic(), event))
        if event.kind == KIND_COMMENTARY and event.delivery_class != DELIVERY_INTERNAL:
            delivered.set()

    controller = _HERStreamCadenceController(
        callback,
        prompt="You are Sunny.",
        progress_enabled=True,
        first_update_s=0.01,
        target_interval_s=0.02,
        hard_interval_s=0.2,
        activity_grace_s=0.1,
    )
    cadence_task = asyncio.create_task(controller.run())
    await controller.forward(
        StreamEvent(
            kind=KIND_COMMENTARY,
            summary="Sunny has reached a material checkpoint. ☀️",
            event_id="req-separated:commentary:1",
            delivery_class=DELIVERY_USER_COMMENTARY,
        )
    )
    started = time.monotonic()
    for index in range(5):
        await controller.forward(
            StreamEvent(
                kind=KIND_PROGRESS,
                summary=f"technical event {index}",
                event_id=f"req-separated:technical:{index}",
                delivery_class=DELIVERY_TECHNICAL,
            )
        )
        await asyncio.sleep(0.004)
    await asyncio.wait_for(delivered.wait(), timeout=0.2)
    await controller.finish()
    await cadence_task

    commentary_at = next(
        timestamp
        for timestamp, event in received
        if event.kind == KIND_COMMENTARY and event.delivery_class != DELIVERY_INTERNAL
    )
    assert commentary_at - started < 0.08


@pytest.mark.asyncio
async def test_claw_cadence_finish_supersedes_only_latest_pending_commentary():
    received = []

    async def callback(event):
        received.append(event)

    controller = _HERStreamCadenceController(
        callback,
        prompt="You are Sunny.",
        progress_enabled=True,
        first_update_s=60,
        target_interval_s=60,
        hard_interval_s=60,
        activity_grace_s=0,
    )
    cadence_task = asyncio.create_task(controller.run())
    first = StreamEvent(
        kind=KIND_COMMENTARY,
        summary="Sunny completed the first material step. ☀️",
        event_id="req-finish:commentary:1",
        delivery_class=DELIVERY_USER_COMMENTARY,
    )
    second = replace(
        first,
        summary="Sunny completed the latest material step. ☀️",
        event_id="req-finish:commentary:2",
    )
    await controller.forward(first)
    await controller.forward(second)
    await controller.finish(pending_reason="superseded_by_final")
    await cadence_task

    assert [event.event_id for event in received] == [
        "req-finish:commentary:1",
        "req-finish:commentary:2",
    ]
    assert all(event.delivery_class == DELIVERY_INTERNAL for event in received)
    assert "suppressed_reason=coalesced_by_newer_commentary" in received[0].detail
    assert "suppressed_reason=superseded_by_final" in received[1].detail


@pytest.mark.asyncio
async def test_disabled_claw_controller_audits_but_suppresses_progress_commentary():
    received = []

    async def callback(event):
        received.append(event)

    controller = _HERStreamCadenceController(
        callback,
        prompt="You are Sunny.",
        progress_enabled=False,
    )
    await controller.forward(
        StreamEvent(
            kind=KIND_ACKNOWLEDGEMENT, summary="Sunny will inspect the request."
        )
    )
    await controller.forward(
        StreamEvent(kind=KIND_COMMENTARY, summary="Sunny has a progress update.")
    )
    controller.close()

    assert [event.kind for event in received] == [
        KIND_ACKNOWLEDGEMENT,
        KIND_COMMENTARY,
    ]
    assert received[1].delivery_class == DELIVERY_INTERNAL
    assert "suppressed_reason=commentary_cadence_disabled" in received[1].detail


@pytest.mark.asyncio
async def test_claw_cadence_suppresses_unchanged_material_revision_after_delivery():
    received = []
    first_delivered = asyncio.Event()

    async def callback(event):
        received.append(event)
        if event.delivery_class == DELIVERY_USER_COMMENTARY:
            first_delivered.set()

    controller = _HERStreamCadenceController(
        callback,
        prompt="You are Sunny.",
        progress_enabled=True,
        first_update_s=0.01,
        target_interval_s=0.02,
        hard_interval_s=0.03,
        activity_grace_s=0,
    )
    cadence_task = asyncio.create_task(controller.run())
    first = StreamEvent(
        kind=KIND_COMMENTARY,
        summary="Sunny has completed the inspection.",
        event_id="req-commentary:replan:1",
        delivery_class=DELIVERY_USER_COMMENTARY,
        phase="execution",
        revision=1,
    )
    second = replace(first, event_id="req-commentary:replan:2", revision=2)

    await controller.forward(first)
    await asyncio.wait_for(first_delivered.wait(), timeout=1)
    await controller.forward(second)
    controller.close()
    await cadence_task

    assert received[0] == first
    suppressed = next(event for event in received if event.event_id == second.event_id)
    assert suppressed.delivery_class == DELIVERY_INTERNAL
    assert "suppressed_reason=unchanged_material_progress" in suppressed.detail


def test_legacy_incomplete_text_without_provenance_is_rejected():
    result = ClawTaskResult(
        text=(
            "Execution status: INCOMPLETE.\n\n"
            "Completed and verified: The tool ledger contains 68 successful result(s).\n\n"
            "Unfinished or unverified: 3 tool result(s) failed."
        ),
        model="deepseek/test",
        permission_mode="workspace-write",
        cwd="/workspace",
        returncode=0,
        duration_ms=10,
        stdout="",
        stderr="",
        json_data={},
        tool_uses=[],
        tool_results=[],
        iterations=77,
        completion_status="incomplete",
        stop_reason="no_final_text",
    )

    assert _claw_run_is_incomplete(result) is True
    response, metadata = _claw_incomplete_response(result, prompt="继续完成任务")

    assert response == ""
    assert metadata["terminal_protocol_valid"] is False
    assert "primary-model final message" in metadata["terminal_protocol_error"]
    assert metadata["fallback_report_generated"] is False


def test_incomplete_primary_model_report_is_preserved_without_runtime_advice():
    result = ClawTaskResult(
        text="殿下，一项读取失败但后续读取已核实状态；建议从保存点继续。",
        model="deepseek/test",
        permission_mode="read-only",
        cwd="/workspace",
        returncode=0,
        duration_ms=10,
        stdout="",
        stderr="",
        json_data={},
        tool_uses=[
            {"id": "read-1", "name": "read_file"},
            {"id": "read-2", "name": "mcp__hashi-tools__file_read"},
        ],
        tool_results=[
            {
                "tool_use_id": "read-1",
                "output": "outside workspace",
                "is_error": True,
            },
            {"tool_use_id": "read-2", "output": "verified", "is_error": False},
        ],
        session_id="session-live-canary",
        iterations=12,
        completion_status="incomplete",
        stop_reason="max_iterations",
        terminal_kind="model_report",
        message_origin="primary_model",
        exit_reasoning_status="completed",
        exit_reasoning_attempts=1,
    )

    response, metadata = _claw_incomplete_response(result, prompt="continue")

    assert response == result.text
    assert "**CONTINUE**" not in response
    assert "**PIVOT**" not in response
    assert metadata["terminal_protocol_valid"] is True


def test_claw_max_iterations_preserves_primary_agent_final():
    effort = "high"
    iterations = 96
    primary_final = f"已走完 {iterations} 轮；下一回合从存档继续。"
    result = ClawTaskResult(
        text=primary_final,
        model="deepseek/test",
        permission_mode="workspace-write",
        cwd="/workspace",
        returncode=0,
        duration_ms=10,
        stdout="",
        stderr="",
        json_data={},
        tool_uses=[{"id": "write-1", "name": "browser_click"}],
        tool_results=[
            {"tool_use_id": "write-1", "output": {"success": True}, "is_error": False}
        ],
        session_id=f"session-{effort}",
        iterations=iterations,
        completion_status="incomplete",
        stop_reason="max_iterations",
        terminal_kind="model_report",
        message_origin="primary_model",
        exit_reasoning_status="completed",
        exit_reasoning_attempts=1,
    )

    response, metadata = _claw_incomplete_response(result, prompt="请继续")

    assert response == primary_final
    assert "**CONTINUE**" not in response
    assert metadata["persona_final_response_preserved"] is True
    assert metadata["fallback_report_generated"] is False


def test_claw_budget_exhaustion_also_preserves_safe_primary_agent_closing():
    result = ClawTaskResult(
        text="殿下，这回合预算已用尽；臣会从保存处继续。",
        model="deepseek/test",
        permission_mode="workspace-write",
        cwd="/workspace",
        returncode=0,
        duration_ms=10,
        stdout="",
        stderr="",
        json_data={},
        tool_uses=[],
        tool_results=[],
        session_id="session-budget",
        iterations=9,
        completion_status="incomplete",
        stop_reason="budget_exhausted",
        terminal_kind="model_report",
        message_origin="primary_model",
        exit_reasoning_status="completed",
        exit_reasoning_attempts=1,
    )

    response, metadata = _claw_incomplete_response(result, prompt="请继续")

    assert response.startswith(result.text)
    assert metadata["persona_final_response_preserved"] is True
    assert metadata.get("persona_render_required") is not True


def test_claw_max_iterations_does_not_deliver_dangling_tool_markup():
    result = ClawTaskResult(
        text="准备继续。\n<｜｜DSML｜｜tool_calls>\n尚未执行的调用",
        model="deepseek/test",
        permission_mode="read-only",
        cwd="/workspace",
        returncode=0,
        duration_ms=10,
        stdout="",
        stderr="",
        json_data={},
        tool_uses=[{"id": "read-1", "name": "bash"}],
        tool_results=[{"tool_use_id": "read-1", "output": "ok", "is_error": False}],
        session_id="session-1",
        iterations=12,
        completion_status="incomplete",
        stop_reason="max_iterations",
        terminal_kind="model_report",
        message_origin="primary_model",
        exit_reasoning_status="completed",
        exit_reasoning_attempts=2,
    )

    response, metadata = _claw_incomplete_response(
        result,
        prompt="""
# IDENTITY.md
- **Name:** 小夏 (Sunny)
- **Self-reference:** Uses 小夏 or 我, with preference for 小夏.
- **Emoji:** 🌸

# USER.md
- **What to call them:** 爸爸（可用敬称「您」，禁止使用「你」）
""".strip(),
    )

    assert "<｜｜DSML｜｜tool_calls>" not in response
    assert response == ""
    assert metadata["persona_final_response_preserved"] is False
    assert metadata["persona_interpretation_generated"] is False
    assert metadata["dangling_tool_markup_blocked"] is True
    assert metadata["fallback_report_generated"] is False
    assert "primary-model final message" in metadata["terminal_protocol_error"]


def test_claw_max_iterations_blocks_deepseek_single_bar_dsml_markup():
    result = ClawTaskResult(
        text='<｜DSML｜tool_calls>\n<｜DSML｜invoke name="bash">',
        model="deepseek/test",
        permission_mode="read-only",
        cwd="/workspace",
        returncode=0,
        duration_ms=10,
        stdout="",
        stderr="",
        json_data={},
        tool_uses=[],
        tool_results=[],
        session_id="session-1",
        iterations=31,
        completion_status="incomplete",
        stop_reason="max_iterations",
        terminal_kind="model_report",
        message_origin="primary_model",
        exit_reasoning_status="completed",
        exit_reasoning_attempts=2,
    )

    response, metadata = _claw_incomplete_response(result, prompt="请继续")

    assert "DSML" not in response
    assert metadata["dangling_tool_markup_blocked"] is True
    assert metadata["persona_final_response_preserved"] is False


def test_dangling_markup_detector_allows_explicit_fenced_examples():
    assert _claw_contains_dangling_tool_markup(
        '<｜DSML｜tool_calls><｜DSML｜invoke name="bash">'
    )
    assert not _claw_contains_dangling_tool_markup(
        'Example only:\n```text\n<｜DSML｜tool_calls><｜DSML｜invoke name="bash">\n```'
    )


def test_compact_execution_ledger_distinguishes_verified_and_unverified_actions():
    result = ClawTaskResult(
        text="done",
        model="deepseek/test",
        permission_mode="workspace-write",
        cwd="/workspace",
        returncode=0,
        duration_ms=10,
        stdout="",
        stderr="",
        json_data={},
        tool_uses=[
            {"id": "read-1", "name": "read_file"},
            {"id": "write-1", "name": "write_file"},
            {"id": "send-1", "name": "external_send"},
        ],
        tool_results=[
            {"tool_use_id": "read-1", "output": "state", "is_error": False},
            {"tool_use_id": "write-1", "output": "written", "is_error": False},
            {
                "tool_use_id": "send-1",
                "output": "transport unavailable",
                "is_error": True,
            },
        ],
    )

    ledger = _claw_compact_execution_ledger(result)

    assert ledger["total_entries"] == 3
    assert [entry["verification"] for entry in ledger["entries"]] == [
        "verified",
        "unverified_side_effect",
        "failed",
    ]
    assert all("output" not in entry for entry in ledger["entries"])


@pytest.mark.asyncio
async def test_completed_adapter_path_blocks_dangling_dsml_and_marks_incomplete(
    tmp_path,
):
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={},
        resolve_access_root=lambda: tmp_path,
    )
    adapter = HERAdapter(cfg, SimpleNamespace(), api_key="test-key")
    adapter._binary = tmp_path / "hashi-her"
    adapter._run_task_async = AsyncMock(
        return_value=ClawTaskResult(
            text='<｜DSML｜tool_calls><｜DSML｜invoke name="bash">',
            model="deepseek/test",
            permission_mode="workspace-write",
            cwd=str(tmp_path),
            returncode=0,
            duration_ms=1,
            stdout="",
            stderr="",
            json_data={"usage": {}},
            tool_uses=[],
            tool_results=[],
            session_id="session-dsml",
            iterations=2,
            completion_status="completed",
            stop_reason="end_turn",
            terminal_kind="model_report",
            message_origin="primary_model",
            exit_reasoning_status="embedded",
            exit_reasoning_attempts=0,
        )
    )

    response = await adapter.generate_response("report the result", "req-dsml")

    assert response.is_success is False
    assert response.text == ""
    assert "dangling tool-call markup" in response.error
    assert response.stream_metadata["dangling_tool_markup_blocked"] is True


@pytest.mark.asyncio
async def test_completed_adapter_path_exposes_nonblocking_planning_failure(tmp_path):
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={},
        resolve_access_root=lambda: tmp_path,
    )
    adapter = HERAdapter(cfg, SimpleNamespace(), api_key="test-key")
    adapter._binary = tmp_path / "hashi-her"
    adapter._run_task_async = AsyncMock(
        return_value=ClawTaskResult(
            text="实际工具执行与状态回读均已完成。",
            model="deepseek/test",
            permission_mode="workspace-write",
            cwd=str(tmp_path),
            returncode=0,
            duration_ms=1,
            stdout="",
            stderr="",
            json_data={"usage": {}},
            tool_uses=[{"id": "write-1", "name": "write_file"}],
            tool_results=[
                {
                    "tool_use_id": "write-1",
                    "tool_name": "write_file",
                    "output": "verified",
                    "is_error": False,
                }
            ],
            session_id="session-planning-fallback",
            iterations=2,
            completion_status="completed",
            stop_reason="end_turn",
            planning_status="failed",
            planning_error=(
                "task frame planned_tools contains non-canonical tool prose "
                "`write_file 或 hashi_file_write`"
            ),
            terminal_kind="model_report",
            message_origin="primary_model",
            exit_reasoning_status="embedded",
            exit_reasoning_attempts=0,
        )
    )

    response = await adapter.generate_response("请更新文件", "req-planning-fallback")

    assert response.is_success is True
    assert response.text == "实际工具执行与状态回读均已完成。"
    assert "内部规划报告" not in response.text
    assert response.stream_metadata["planning_status"] == "failed"
    assert (
        "write_file 或 hashi_file_write" in response.stream_metadata["planning_error"]
    )
    assert "planning_failure_user_visible" not in response.stream_metadata


def test_claw_max_iterations_without_model_final_is_a_protocol_error_not_persona_fallback():
    result = ClawTaskResult(
        text="",
        model="deepseek/test",
        permission_mode="workspace-write",
        cwd="/workspace",
        returncode=0,
        duration_ms=10,
        stdout="",
        stderr="",
        json_data={},
        tool_uses=[
            {"id": f"tool-{index}", "name": "bash" if index <= 5 else "edit_file"}
            for index in range(1, 12)
        ],
        tool_results=[
            {"tool_use_id": f"tool-{index}", "output": "ok", "is_error": False}
            for index in range(1, 12)
        ],
        iterations=12,
        completion_status="incomplete",
        stop_reason="max_iterations",
    )

    response, metadata = _claw_incomplete_response(
        result,
        prompt="""
# IDENTITY.md
- **Name:** 小夏 (Sunny)
- **Vibe:** Warm and helpful. Calls Barry 爸爸.
- **Self-reference:** Uses 小夏 or 我, with preference for 小夏.
- **Emoji:** 🌸

# USER.md - About Your Human
- **What to call them:** 爸爸（可用敬称「您」，禁止使用「你」）
""".strip(),
    )

    assert response == ""
    assert metadata["persona_interpretation_generated"] is False
    assert metadata["terminal_protocol_valid"] is False
    assert "primary-model final message" in metadata["terminal_protocol_error"]


def test_stream_json_parser_accepts_legacy_diagnostics_when_run_finished_exists():
    output = "\n".join(
        [
            json.dumps({"kind": "run_started", "session_id": "session-1"}),
            "Permission approval required",
            "Approve this tool call? [y/N]: not-json",
            json.dumps(
                {"kind": "run_finished", "message": "done", "session_id": "session-1"}
            ),
        ]
    )

    parsed = _parse_stream_json_output(
        output,
        command=["/runtime/hashi-her", "prompt", "PRIVATE USER PROMPT"],
    )

    assert parsed["message"] == "done"
    assert parsed["_protocol_non_json_line_count"] == 2


def test_stream_json_parser_missing_final_is_safe_and_fail_closed():
    output = "\n".join(
        [
            json.dumps({"kind": "run_started", "session_id": "session-1"}),
            "Permission approval required for PRIVATE USER PROMPT",
            json.dumps(
                {
                    "kind": "api_http_error",
                    "type": "error",
                    "message": "Invalid assistant message",
                }
            ),
        ]
    )

    with pytest.raises(ClawJsonError) as raised:
        _parse_stream_json_output(
            output,
            command=["/runtime/hashi-her", "prompt", "PRIVATE USER PROMPT"],
        )

    message = str(raised.value)
    assert "did not include run_finished" in message
    assert "last_error_kind=api_http_error" in message
    assert "non_json_lines=1" in message
    assert "PRIVATE USER PROMPT" not in message


def test_json_parser_error_does_not_echo_command_or_output():
    with pytest.raises(ClawJsonError) as raised:
        _parse_json_output(
            "PRIVATE USER PROMPT",
            command=["/runtime/hashi-her", "prompt", "PRIVATE USER PROMPT"],
        )

    message = str(raised.value)
    assert "hashi-her" in message
    assert "PRIVATE USER PROMPT" not in message


def _write_packaged_claw(
    root: Path,
    *,
    platform_key: str = "linux-x86_64",
    rust_target_triple: str = "x86_64-unknown-linux-gnu",
    body: str = "#!/usr/bin/env python3\nprint('ok')\n",
) -> Path:
    (root / "bin" / platform_key).mkdir(parents=True, exist_ok=True)
    binary = _write_exe(root / "bin" / platform_key / "hashi-her", body)
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "manifest_version": 1,
                "runtime": "hashi-her",
                "version": "0.0.0-test",
                "binaries": {
                    platform_key: {
                        "path": str(binary.relative_to(root)),
                        "binary_name": "hashi-her",
                        "rust_target_triple": rust_target_triple,
                        "sha256": digest,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return binary


def test_find_claw_binary_accepts_configured_executable(tmp_path):
    fake = _write_exe(
        tmp_path / "claw",
        """
        #!/usr/bin/env python3
        print("ok")
        """,
    )

    assert find_claw_binary(fake) == fake.resolve()


def test_detect_hashi_claw_platform_linux_wsl_candidate():
    platform = detect_hashi_claw_platform(
        system="Linux",
        machine="x86_64",
        release="6.6.0-microsoft-standard-WSL2",
    )

    assert platform.key == "linux-x86_64"
    assert platform.rust_target_triple == "x86_64-unknown-linux-gnu"
    assert platform.is_wsl is True
    assert platform.candidate_keys == ("linux-x86_64-wsl", "linux-x86_64")


def test_load_packaged_claw_manifest_rejects_non_hashi_runtime(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {"manifest_version": 1, "runtime": "claw", "version": "1", "binaries": {}}
        ),
        encoding="utf-8",
    )

    with pytest.raises(ClawPackagedRuntimeError, match="hashi-her"):
        load_packaged_claw_manifest(manifest)


def test_resolve_packaged_claw_binary_validates_checksum(tmp_path):
    root = tmp_path / "hashi_assets" / "her"
    binary = _write_packaged_claw(root)
    platform = detect_hashi_claw_platform(
        system="Linux", machine="x86_64", release="6.8"
    )

    resolved = resolve_packaged_claw_binary(root, platform=platform)

    assert resolved.path == binary.resolve()
    assert resolved.source == "packaged"
    assert resolved.packaged_version == "0.0.0-test"


def test_find_claw_binary_uses_packaged_runtime_before_env(tmp_path):
    root = tmp_path / "hashi_assets" / "her"
    packaged = _write_packaged_claw(root)
    env_claw = _write_exe(
        tmp_path / "env-claw",
        """
        #!/usr/bin/env python3
        print("env")
        """,
    )
    global_cfg = SimpleNamespace(project_root=tmp_path)

    assert (
        find_claw_binary(
            global_config=global_cfg, env={"CLAW_BINARY": str(env_claw), "PATH": ""}
        )
        == packaged.resolve()
    )


def test_find_claw_binary_explicit_development_selection_precedes_packaged(tmp_path):
    _write_packaged_claw(tmp_path / "hashi_assets" / "her")
    development = _write_development_selection(tmp_path)
    global_cfg = SimpleNamespace(
        project_root=tmp_path,
        bridge_home=tmp_path,
        claw_providers={"runtime_policy": "require-packaged"},
    )

    resolved = discover_claw_binary(global_config=global_cfg, env={"PATH": ""})

    assert resolved.path == development.resolve()
    assert resolved.source == "development-source-build"
    assert resolved.manifest_path == (
        tmp_path / "state" / "her_rebuild" / "development-selection.json"
    )


def test_find_claw_binary_invalid_development_selection_fails_closed(tmp_path):
    packaged = _write_packaged_claw(tmp_path / "hashi_assets" / "her")
    development = _write_development_selection(tmp_path)
    development.write_text("tampered", encoding="utf-8")
    global_cfg = SimpleNamespace(project_root=tmp_path, bridge_home=tmp_path)

    with pytest.raises(ClawPackagedRuntimeError, match="refusing silent fallback"):
        discover_claw_binary(global_config=global_cfg, env={"PATH": ""})

    assert packaged.is_file()


def test_find_claw_binary_empty_development_selection_uses_packaged(tmp_path):
    packaged = _write_packaged_claw(tmp_path / "hashi_assets" / "her")
    state_root = tmp_path / "state" / "her_rebuild"
    state_root.mkdir(parents=True)
    (state_root / "development-selection.json").write_text(
        json.dumps({"schema_version": 1, "active": None}), encoding="utf-8"
    )
    global_cfg = SimpleNamespace(project_root=tmp_path, bridge_home=tmp_path)

    resolved = discover_claw_binary(global_config=global_cfg, env={"PATH": ""})

    assert resolved.path == packaged.resolve()
    assert resolved.source == "packaged"


def test_find_claw_binary_checksum_mismatch_falls_back_to_env(tmp_path):
    root = tmp_path / "hashi_assets" / "her"
    packaged = _write_packaged_claw(root)
    packaged.write_text("#!/usr/bin/env python3\nprint('tampered')\n", encoding="utf-8")
    packaged.chmod(packaged.stat().st_mode | stat.S_IXUSR)
    env_claw = _write_exe(
        tmp_path / "env-claw",
        """
        #!/usr/bin/env python3
        print("env")
        """,
    )
    global_cfg = SimpleNamespace(project_root=tmp_path)

    resolved = discover_claw_binary(
        global_config=global_cfg, env={"CLAW_BINARY": str(env_claw), "PATH": ""}
    )

    assert resolved.path == env_claw.resolve()
    assert resolved.source == "env:CLAW_BINARY"
    assert any("checksum mismatch" in warning for warning in resolved.warnings)


def test_find_claw_binary_require_packaged_fails_closed(tmp_path):
    root = tmp_path / "hashi_assets" / "her"
    packaged = _write_packaged_claw(root)
    packaged.write_text("#!/usr/bin/env python3\nprint('tampered')\n", encoding="utf-8")
    packaged.chmod(packaged.stat().st_mode | stat.S_IXUSR)
    global_cfg = SimpleNamespace(project_root=tmp_path)
    agent_cfg = SimpleNamespace(extra={"claw_runtime_policy": "require-packaged"})

    with pytest.raises(ClawBinaryNotFound, match="required but unavailable"):
        find_claw_binary(
            global_config=global_cfg, agent_config=agent_cfg, env={"PATH": ""}
        )


def test_find_claw_binary_require_packaged_does_not_bypass_manifest(tmp_path):
    root = tmp_path / "hashi_assets" / "her"
    packaged = _write_packaged_claw(root)
    configured = _write_exe(
        tmp_path / "configured-claw",
        """
        #!/usr/bin/env python3
        print("configured")
        """,
    )
    global_cfg = SimpleNamespace(
        project_root=tmp_path,
        claw_providers={
            "binary_path": str(configured),
            "runtime_policy": "require-packaged",
        },
    )

    resolved = discover_claw_binary(global_config=global_cfg, env={"PATH": ""})

    assert resolved.path == packaged.resolve()
    assert resolved.source == "packaged"


def test_find_claw_binary_reports_missing_configured_path(tmp_path):
    with pytest.raises(ClawBinaryNotFound):
        find_claw_binary(tmp_path / "missing", env={"PATH": ""})


def test_find_claw_binary_accepts_global_claw_provider_binary(tmp_path):
    fake = _write_exe(
        tmp_path / "claw",
        """
        #!/usr/bin/env python3
        print("ok")
        """,
    )
    global_cfg = SimpleNamespace(claw_providers={"binary_path": str(fake)})

    assert (
        find_claw_binary(global_config=global_cfg, env={"PATH": ""}) == fake.resolve()
    )


def test_build_claw_env_uses_allowlist_only():
    env = build_claw_env(
        {
            "OPENAI_BASE_URL": "https://example.invalid/v1",
            "OPENAI_API_KEY": "secret",
            "CLAW_MAX_TOOL_ITERATIONS": "96",
            "CLAW_TASK_PLANNING": "1",
            "CLAW_EXECUTION_EFFORT": "high",
            "CLAW_POST_TOOL_STALL_TIMEOUT_SECONDS": "90",
            "CLAW_POST_TOOL_RETRY_STALL_TIMEOUT_SECONDS_OPENAI": "180",
            "HASHI_MANAGED_TRANSPORT": "1",
            "ANTHROPIC_API_KEY": "must-not-pass",
            "HASHI_REMOTE_SHARED_TOKEN": "must-not-pass",
            "HOME": "/tmp/home",
            "PATH": "/bin",
        }
    )

    assert env == {
        "OPENAI_BASE_URL": "https://example.invalid/v1",
        "OPENAI_API_KEY": "secret",
        "CLAW_MAX_TOOL_ITERATIONS": "96",
        "CLAW_TASK_PLANNING": "1",
        "CLAW_EXECUTION_EFFORT": "high",
        "CLAW_POST_TOOL_STALL_TIMEOUT_SECONDS": "90",
        "CLAW_POST_TOOL_RETRY_STALL_TIMEOUT_SECONDS_OPENAI": "180",
        "HASHI_MANAGED_TRANSPORT": "1",
        "HOME": "/tmp/home",
        "PATH": "/bin",
    }


@pytest.mark.parametrize(
    ("effort", "expected_iterations"),
    [
        ("low", "12"),
        ("medium", "32"),
        ("high", "96"),
        ("xhigh", "192"),
        ("max", "384"),
        ("max+", "512"),
    ],
)
def test_claw_execution_effort_maps_to_iteration_budget(
    tmp_path, monkeypatch, effort, expected_iterations
):
    monkeypatch.setenv("CLAW_MAX_PLUS_TOKEN_BUDGET", "1")
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={"effort": effort},
        resolve_access_root=lambda: tmp_path,
    )
    adapter = ClawCLIAdapter(cfg, SimpleNamespace(), api_key="test-key")

    assert adapter.effort == effort
    assert adapter._task_env()["CLAW_MAX_TOOL_ITERATIONS"] == expected_iterations
    assert adapter._task_env()["CLAW_TASK_PLANNING"] == (
        "0" if effort == "low" else "1"
    )
    assert adapter._task_env()["CLAW_EXECUTION_EFFORT"] == effort
    if effort == "max+":
        assert "CLAW_MAX_PLUS_TIME_BUDGET_SECONDS" not in adapter._task_env()
        assert "CLAW_MAX_PLUS_TOKEN_BUDGET" not in adapter._task_env()


def test_claw_explicit_max_iterations_overrides_execution_effort(tmp_path):
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={"effort": "low", "max_tool_iterations": 77},
        resolve_access_root=lambda: tmp_path,
    )
    adapter = ClawCLIAdapter(cfg, SimpleNamespace(), api_key="test-key")

    assert adapter._task_env()["CLAW_MAX_TOOL_ITERATIONS"] == "77"


def test_max_plus_checkpoint_is_request_correlated_and_atomically_recoverable(tmp_path):
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={"effort": "max+"},
        resolve_access_root=lambda: tmp_path,
    )
    adapter = ClawCLIAdapter(cfg, SimpleNamespace(), api_key="test-key")
    event = {
        "kind": "max_plus_checkpoint",
        "phase": "evidence_update",
        "budget": {"tokens_used": 123},
        "stop_reason": None,
        "frame": {"active_goal": "verify max plus"},
    }

    adapter._persist_control_event("req-max-plus", event)

    checkpoint = json.loads(
        (tmp_path / "backend_state" / "claw_max_plus_checkpoint.json").read_text(
            encoding="utf-8"
        )
    )
    assert checkpoint == {"request_id": "req-max-plus", "event": event}
    assert not (
        tmp_path / "backend_state" / "claw_max_plus_checkpoint.json.tmp"
    ).exists()


def test_run_claw_doctor_parses_json(tmp_path):
    fake = _write_exe(
        tmp_path / "claw",
        """
        #!/usr/bin/env python3
        import json
        print(json.dumps({"kind": "doctor", "status": "ok"}))
        """,
    )

    assert run_claw_doctor(tmp_path, binary_path=fake) == {
        "kind": "doctor",
        "status": "ok",
    }


def test_run_claw_json_command_raises_for_non_zero_json_error(tmp_path):
    fake = _write_exe(
        tmp_path / "claw",
        """
        #!/usr/bin/env python3
        import json, sys
        print("runtime startup diagnostic")
        print(json.dumps({"error": "bad key", "kind": "api_http_error"}), file=sys.stderr)
        raise SystemExit(1)
        """,
    )

    with pytest.raises(ClawCommandError) as raised:
        run_claw_json_command(
            ["doctor", "--output-format", "json"], cwd=tmp_path, binary_path=fake
        )

    assert raised.value.returncode == 1
    assert raised.value.parsed_error == {"error": "bad key", "kind": "api_http_error"}


def test_run_claw_json_command_raises_for_non_json_output(tmp_path):
    fake = _write_exe(
        tmp_path / "claw",
        """
        #!/usr/bin/env python3
        print("not json")
        """,
    )

    with pytest.raises(ClawJsonError):
        run_claw_json_command(
            ["doctor", "--output-format", "json"], cwd=tmp_path, binary_path=fake
        )


def test_run_claw_json_command_timeout(tmp_path):
    fake = _write_exe(
        tmp_path / "claw",
        """
        #!/usr/bin/env python3
        import time
        time.sleep(2)
        """,
    )

    with pytest.raises(ClawTimeoutError):
        run_claw_json_command(
            ["doctor", "--output-format", "json"],
            cwd=tmp_path,
            binary_path=fake,
            timeout_s=0.1,
        )


def test_run_claw_task_builds_safe_one_shot_command(tmp_path):
    fake = _write_exe(
        tmp_path / "claw",
        """
        #!/usr/bin/env python3
        import json, sys
        assert "--permission-mode" in sys.argv
        assert "read-only" in sys.argv
        assert "--allowedTools" in sys.argv
        assert "read,glob" in sys.argv
        assert "--stdin" not in sys.argv
        assert "prompt" not in sys.argv
        assert "inspect" not in sys.argv
        assert sys.stdin.read() == "inspect"
        print(json.dumps({
          "message": "done",
          "model": "deepseek/test",
          "iterations": 2,
          "estimated_cost": "$0.0001",
          "task_checkpoint": {"active_goal": "inspect", "next_action": "ask"},
          "pending_interaction": {"interaction_id": "ask-1", "kind": "question", "question": "Continue?"},
          "planning_status": "failed",
          "planning_error": "task frame planned_tools contains non-canonical tool prose",
          "tool_uses": [{"name": "read_file"}],
          "tool_results": [{"is_error": False}]
        }))
        """,
    )

    result = run_claw_task(
        tmp_path,
        "inspect",
        "deepseek/test",
        permission_mode="read-only",
        allowed_tools=["read", "glob"],
        binary_path=fake,
    )

    assert result.text == "done"
    assert result.model == "deepseek/test"
    assert result.permission_mode == "read-only"
    assert result.iterations == 2
    assert result.tool_uses == [{"name": "read_file"}]
    assert result.tool_results == [{"is_error": False}]
    assert result.task_checkpoint == {"active_goal": "inspect", "next_action": "ask"}
    assert result.pending_interaction == {
        "interaction_id": "ask-1",
        "kind": "question",
        "question": "Continue?",
    }
    assert result.planning_status == "failed"
    assert result.planning_error == (
        "task frame planned_tools contains non-canonical tool prose"
    )


def test_run_claw_task_rejects_invalid_permission_mode(tmp_path):
    with pytest.raises(ValueError, match="permission_mode"):
        run_claw_task(tmp_path, "prompt", "model", permission_mode="root")


def test_build_claw_task_args_matches_cli_shape():
    assert build_claw_task_args(
        "hello",
        "deepseek/test",
        permission_mode="read-only",
        resume="latest",
        allowed_tools=["read"],
        skip_permissions=True,
    ) == [
        "--model",
        "deepseek/test",
        "--permission-mode",
        "read-only",
        "--output-format",
        "json",
        "--allowedTools",
        "read",
        "--dangerously-skip-permissions",
        "--resume",
        "latest",
        "prompt",
        "hello",
    ]


def test_build_claw_task_args_accepts_stream_json():
    args = build_claw_task_args(
        "hello",
        "deepseek/test",
        permission_mode="read-only",
        output_format="stream-json",
    )

    assert args[args.index("--output-format") + 1] == "stream-json"
    assert "--allowedTools" not in args
    assert "hello" not in args
    assert "prompt" not in args
    assert "--stdin" not in args


def test_run_claw_task_streams_large_prompt_over_stdin(tmp_path):
    fake = _write_exe(
        tmp_path / "claw",
        """
        #!/usr/bin/env python3
        import json, sys
        prompt = sys.stdin.read()
        print(json.dumps({
            "message": str(len(prompt)),
            "model": "deepseek/test",
            "argv_bytes": sum(len(value.encode()) for value in sys.argv[1:]),
            "prompt_in_argv": prompt in sys.argv,
        }))
        """,
    )
    prompt = "large-prompt\n" + ("x" * (200 * 1024))

    result = run_claw_task(
        tmp_path,
        prompt,
        "deepseek/test",
        permission_mode="read-only",
        binary_path=fake,
    )

    assert result.text == str(len(prompt))
    assert result.json_data["prompt_in_argv"] is False
    assert result.json_data["argv_bytes"] < 1_000


def test_run_claw_task_passes_resumed_prompt_as_text_without_stdin(tmp_path):
    fake = _write_exe(
        tmp_path / "claw",
        """
        #!/usr/bin/env python3
        import json, sys
        stdin = sys.stdin.read()
        print(json.dumps({
            "message": "done",
            "model": "deepseek/test",
            "argv": sys.argv[1:],
            "stdin": stdin,
        }))
        """,
    )

    result = run_claw_task(
        tmp_path,
        "resume the real task",
        "deepseek/test",
        permission_mode="read-only",
        resume="session-1",
        binary_path=fake,
    )

    assert result.text == "done"
    assert result.json_data["argv"][-4:] == [
        "--resume",
        "session-1",
        "prompt",
        "resume the real task",
    ]
    assert "--stdin" not in result.json_data["argv"]
    assert result.json_data["stdin"] == ""


def test_claw_adapter_defaults_to_all_native_tools_and_accepts_wildcard(tmp_path):
    base = {
        "name": "test",
        "workspace_dir": tmp_path,
        "model": "deepseek/test",
        "resolve_access_root": lambda: tmp_path,
    }
    unrestricted = ClawCLIAdapter(
        SimpleNamespace(**base, extra={}),
        SimpleNamespace(),
        api_key="test-key",
    )
    wildcard = ClawCLIAdapter(
        SimpleNamespace(**base, extra={"allowed_tools": ["*"]}),
        SimpleNamespace(),
        api_key="test-key",
    )

    assert unrestricted._allowed_tools() is None
    assert wildcard._allowed_tools() is None


def test_registry_never_exposes_retired_her_adapter():
    from adapters.her_v2 import HERv2Adapter

    assert get_backend_class("her") is HERv2Adapter
    assert is_cli_backend("her")
    assert not allows_custom_models("her")
    assert get_backend_class("claw-cli") is HERv2Adapter
    assert is_cli_backend("claw-cli")
    assert not allows_custom_models("claw-cli")
    assert not allows_custom_models("codex-cli")
    assert get_secret_lookup_order("claw-cli", "ying") == []


def test_claw_provider_env_resolves_secret_and_base_url(tmp_path):
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="openrouter:deepseek/test",
        extra={},
        resolve_access_root=lambda: tmp_path,
        _hashi_secrets={"openrouter_key": "provider-secret"},
    )
    global_cfg = SimpleNamespace(
        claw_providers={
            "providers": {
                "openrouter": {
                    "base_url": "https://openrouter.invalid/v1",
                    "secret": "openrouter_key",
                    "status": "stable",
                }
            }
        }
    )
    adapter = ClawCLIAdapter(cfg, global_cfg, api_key="legacy-secret")

    assert adapter._claw_model() == "deepseek/test"
    assert adapter._task_env()["OPENAI_BASE_URL"] == "https://openrouter.invalid/v1"
    assert adapter._task_env()["OPENAI_API_KEY"] == "provider-secret"


def test_explicit_deepseek_provider_translates_bare_model_for_certified_her(tmp_path):
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek:deepseek-v4-flash",
        extra={"provider": "deepseek"},
        resolve_access_root=lambda: tmp_path,
    )
    global_cfg = SimpleNamespace(
        claw_providers={
            "providers": {
                "deepseek": {
                    "base_url": "https://deepseek.invalid/v1",
                    "secret": "deepseek_api_key",
                }
            }
        }
    )
    adapter = ClawCLIAdapter(cfg, global_cfg, api_key=None)

    assert adapter._provider_and_model() == ("deepseek", "deepseek-v4-flash")
    assert adapter._claw_model() == "local/deepseek-v4-flash"


def test_explicit_provider_model_prefix_overrides_certified_runtime_default(tmp_path):
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek:deepseek-v4-flash",
        extra={"provider": "deepseek"},
        resolve_access_root=lambda: tmp_path,
    )
    global_cfg = SimpleNamespace(
        claw_providers={
            "providers": {
                "deepseek": {
                    "base_url": "https://deepseek.invalid/v1",
                    "secret": "deepseek_api_key",
                    "claw_model_prefix": "openai",
                }
            }
        }
    )
    adapter = ClawCLIAdapter(cfg, global_cfg, api_key=None)

    assert adapter._claw_model() == "openai/deepseek-v4-flash"


def test_openrouter_model_slug_is_preserved_for_claw_runtime(tmp_path):
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/deepseek-v4-flash",
        extra={"provider": "openrouter"},
        resolve_access_root=lambda: tmp_path,
    )
    global_cfg = SimpleNamespace(
        claw_providers={
            "providers": {
                "openrouter": {
                    "base_url": "https://openrouter.invalid/v1",
                    "secret": "openrouter_key",
                }
            }
        }
    )
    adapter = ClawCLIAdapter(cfg, global_cfg, api_key=None)

    assert adapter._provider_and_model() == (
        "openrouter",
        "deepseek/deepseek-v4-flash",
    )
    assert adapter._claw_model() == "deepseek/deepseek-v4-flash"


def test_claw_provider_missing_secret_raises(tmp_path):
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={"provider": "openrouter"},
        resolve_access_root=lambda: tmp_path,
        _hashi_secrets={},
    )
    global_cfg = SimpleNamespace(
        claw_providers={
            "providers": {
                "openrouter": {
                    "base_url": "https://openrouter.invalid/v1",
                    "secret": "openrouter_key",
                }
            }
        }
    )
    adapter = ClawCLIAdapter(cfg, global_cfg, api_key="legacy-secret")

    with pytest.raises(ClawProviderSecretMissing):
        adapter._task_env()


def test_claw_provider_legacy_env_fallback(tmp_path):
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={"openai_base_url": "https://legacy.invalid/v1"},
        resolve_access_root=lambda: tmp_path,
    )
    global_cfg = SimpleNamespace(claw_providers={})
    adapter = ClawCLIAdapter(cfg, global_cfg, api_key="legacy-secret")

    assert adapter._task_env()["OPENAI_BASE_URL"] == "https://legacy.invalid/v1"
    assert adapter._task_env()["OPENAI_API_KEY"] == "legacy-secret"


def test_claw_provider_ollama_dummy_key_is_not_redacted(tmp_path):
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="ollama:qwen2.5-coder:32b",
        extra={},
        resolve_access_root=lambda: tmp_path,
    )
    global_cfg = SimpleNamespace(
        claw_providers={
            "providers": {
                "ollama": {
                    "base_url": "http://localhost:11434/v1",
                    "secret": None,
                    "dummy_api_key": "__ollama_dummy__",
                    "status": "provisional",
                }
            }
        }
    )
    adapter = ClawCLIAdapter(cfg, global_cfg, api_key=None)

    assert adapter._claw_model() == "qwen2.5-coder:32b"
    assert adapter._task_env()["OPENAI_API_KEY"] == "__ollama_dummy__"


def test_claw_permission_mode_respects_global_max(tmp_path):
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={"permission_mode": "danger-full-access"},
        resolve_access_root=lambda: tmp_path,
    )
    global_cfg = SimpleNamespace(
        claw_providers={"max_permission_mode": "workspace-write"}
    )
    adapter = ClawCLIAdapter(cfg, global_cfg, api_key="test-key")

    assert adapter._permission_mode() == "workspace-write"


@pytest.mark.asyncio
async def test_claw_adapter_degrades_when_binary_missing(tmp_path):
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={"claw_binary_path": str(tmp_path / "missing")},
        resolve_access_root=lambda: tmp_path,
    )
    global_cfg = SimpleNamespace()
    adapter = ClawCLIAdapter(cfg, global_cfg, api_key="test-key")

    assert await adapter.initialize() is False


@pytest.mark.asyncio
async def test_claw_adapter_degrades_when_provider_secret_missing(tmp_path):
    fake = _write_exe(
        tmp_path / "claw",
        """
        #!/usr/bin/env python3
        import json
        print(json.dumps({"kind": "version", "version": "0.1.0"}))
        """,
    )
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={"claw_binary_path": str(fake), "provider": "openrouter"},
        resolve_access_root=lambda: tmp_path,
        _hashi_secrets={},
    )
    global_cfg = SimpleNamespace(
        claw_providers={
            "providers": {
                "openrouter": {
                    "base_url": "https://openrouter.invalid/v1",
                    "secret": "openrouter_key",
                }
            }
        }
    )
    adapter = ClawCLIAdapter(cfg, global_cfg, api_key=None)

    assert await adapter.initialize() is False


@pytest.mark.asyncio
async def test_claw_adapter_generate_response_with_fake_binary(tmp_path):
    fake = _write_exe(
        tmp_path / "claw",
        """
        #!/usr/bin/env python3
        import json, sys
        if sys.argv[1] == "version":
            print(json.dumps({"kind": "version", "version": "0.1.0", "git_sha": "fake"}))
        else:
            resume = sys.argv[sys.argv.index("--resume") + 1] if "--resume" in sys.argv else None
            print(json.dumps({
              "message": "adapter done",
              "model": "deepseek/test",
              "session_id": resume or "session-1",
              "iterations": 1,
              "completion_status": "incomplete",
              "stop_reason": "max_iterations",
              "terminal_kind": "model_report",
              "message_origin": "primary_model",
              "exit_reasoning_status": "completed",
              "exit_reasoning_attempts": 1,
              "tool_uses": [
                {"id": "read-1", "name": "browser_get_text"},
                {"id": "click-1", "name": "browser_click"}
              ],
              "tool_results": [
                {"tool_use_id": "read-1", "tool_name": "browser_get_text", "output": "feed text", "is_error": False},
                {"tool_use_id": "click-1", "tool_name": "browser_click", "output": "{\\"matched\\":1,\\"state_changed\\":false}", "is_error": False}
              ],
              "usage": {"input_tokens": 3, "output_tokens": 2}
            }))
        """,
    )
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={
            "claw_binary_path": str(fake),
            "permission_mode": "read-only",
            "resume": "latest",
        },
        resolve_access_root=lambda: tmp_path,
    )
    global_cfg = SimpleNamespace()
    adapter = ClawCLIAdapter(cfg, global_cfg, api_key="test-key")

    assert await adapter.initialize() is True
    assert adapter.capabilities.supports_sessions is True
    response = await adapter.generate_response("hello", "req-1")
    resumed = await adapter.generate_response("continue", "req-2")

    assert response.is_success is True
    assert response.text == "adapter done"
    assert "**CONTINUE**" not in response.text
    assert "**PIVOT**" not in response.text
    assert resumed.is_success is True
    assert adapter._session_id == "session-1"
    assert response.usage.input_tokens == 3
    assert response.usage.output_tokens == 2
    assert response.stop_reason == "max_iterations"
    assert response.stream_metadata["claw_completion_status"] == "incomplete"
    assert response.stream_metadata["claw_execution_effort"] == "high"
    assert response.stream_metadata["claw_max_iterations"] == 96
    assert response.stream_metadata["fallback_report_generated"] is False
    assert response.stream_metadata["persona_final_response_preserved"] is True
    assert response.stream_metadata["terminal_kind"] == "model_report"
    assert response.stream_metadata["message_origin"] == "primary_model"
    assert response.stream_metadata["exit_reasoning_status"] == "completed"
    assert "recommended_action" not in response.stream_metadata


@pytest.mark.asyncio
async def test_claw_adapter_new_session_clears_resume_identity(tmp_path):
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={},
        resolve_access_root=lambda: tmp_path,
    )
    adapter = ClawCLIAdapter(cfg, SimpleNamespace(), api_key="test-key")
    adapter._session_id = "session-old"
    adapter._persist_session_identity()

    assert await adapter.handle_new_session() is True
    assert adapter._session_id is None
    assert not adapter._session_state_path.exists()


def test_claw_adapter_session_checkpoint_survives_adapter_recreation(tmp_path):
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={},
        resolve_access_root=lambda: tmp_path,
    )
    first = ClawCLIAdapter(cfg, SimpleNamespace(), api_key="test-key")
    first._session_id = "session-persisted"
    first._persist_session_identity()

    second = ClawCLIAdapter(cfg, SimpleNamespace(), api_key="test-key")
    second._load_session_identity()

    assert second._session_id == "session-persisted"
    assert second._session_state_path.stat().st_mode & 0o777 == 0o600


def test_claw_adapter_ignores_checkpoint_for_other_model(tmp_path):
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/old",
        extra={},
        resolve_access_root=lambda: tmp_path,
    )
    first = ClawCLIAdapter(cfg, SimpleNamespace(), api_key="test-key")
    first._session_id = "session-old-model"
    first._persist_session_identity()

    cfg.model = "deepseek/new"
    second = ClawCLIAdapter(cfg, SimpleNamespace(), api_key="test-key")
    second._load_session_identity()

    assert second._session_id is None


def test_her_full_context_session_mode_clears_stale_checkpoint(tmp_path):
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={},
        resolve_access_root=lambda: tmp_path,
    )
    adapter = HERAdapter(cfg, SimpleNamespace(), api_key="test-key")
    adapter._session_id = "fixed-session"
    adapter._persist_session_identity()

    adapter.set_session_mode(False)

    assert adapter._session_mode is False
    assert adapter._session_id is None
    assert not adapter._session_state_path.exists()


@pytest.mark.asyncio
async def test_her_full_context_turn_never_resumes_or_checkpoints_session(tmp_path):
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={},
        resolve_access_root=lambda: tmp_path,
    )
    adapter = HERAdapter(cfg, SimpleNamespace(), api_key="test-key")
    adapter._binary = tmp_path / "hashi-her"
    adapter._session_id = "stale-session"
    adapter._persist_session_identity()
    adapter.set_session_mode(False)
    adapter._run_task_async = AsyncMock(
        return_value=ClawTaskResult(
            text="done",
            model="deepseek/test",
            permission_mode="workspace-write",
            cwd=str(tmp_path),
            returncode=0,
            duration_ms=1,
            stdout="",
            stderr="",
            json_data={},
            tool_uses=[],
            tool_results=[],
            session_id="full-context-session",
            completion_status="completed",
            stop_reason="end_turn",
            terminal_kind="model_report",
            message_origin="primary_model",
            exit_reasoning_status="embedded",
            exit_reasoning_attempts=0,
        )
    )

    response = await adapter.generate_response("complete context", "req-flex")

    assert response.is_success is True
    assert adapter._run_task_async.await_args.kwargs["resume"] is None
    assert adapter._run_task_async.await_args.kwargs["track_session_identity"] is False
    assert adapter._session_id is None
    assert not adapter._session_state_path.exists()


def _concurrency_task_result(tmp_path, *, text: str, session_id: str) -> ClawTaskResult:
    return ClawTaskResult(
        text=text,
        model="deepseek/test",
        permission_mode="workspace-write",
        cwd=str(tmp_path),
        returncode=0,
        duration_ms=1,
        stdout="",
        stderr="",
        json_data={"usage": {}},
        tool_uses=[],
        tool_results=[],
        session_id=session_id,
        iterations=1,
        completion_status="completed",
        stop_reason="end_turn",
        terminal_kind="model_report",
        message_origin="primary_model",
        exit_reasoning_status="embedded",
        exit_reasoning_attempts=0,
    )


@pytest.mark.asyncio
async def test_her_persistent_session_is_single_flight(tmp_path):
    runtime = SimpleNamespace(
        _request_meta_by_id={
            "req-first": {"session_scope": "persistent"},
            "req-second": {"session_scope": "persistent"},
        },
        current_request_meta=None,
    )
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={},
        resolve_access_root=lambda: tmp_path,
        _hashi_runtime=runtime,
    )
    adapter = HERAdapter(cfg, SimpleNamespace(), api_key="test-key")
    adapter._binary = tmp_path / "hashi-her"
    adapter._session_id = "session-initial"
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    calls = []

    async def run_task(prompt, *, resume, request_id, track_session_identity, **kwargs):
        calls.append((request_id, resume, track_session_identity))
        if request_id == "req-first":
            first_started.set()
            await release_first.wait()
            return _concurrency_task_result(
                tmp_path,
                text="first",
                session_id="session-after-first",
            )
        return _concurrency_task_result(
            tmp_path,
            text="second",
            session_id="session-after-second",
        )

    adapter._run_task_async = run_task
    first = asyncio.create_task(adapter.generate_response("first", "req-first"))
    await first_started.wait()
    second = asyncio.create_task(adapter.generate_response("second", "req-second"))
    await asyncio.sleep(0.02)

    assert calls == [("req-first", "session-initial", True)]
    assert adapter.persistent_session_busy is True

    release_first.set()
    first_response, second_response = await asyncio.gather(first, second)

    assert first_response.is_success is True
    assert second_response.is_success is True
    assert calls == [
        ("req-first", "session-initial", True),
        ("req-second", "session-after-first", True),
    ]
    assert adapter._session_id == "session-after-second"


@pytest.mark.asyncio
async def test_her_isolated_turn_runs_while_background_persistent_turn_is_unfinished(
    tmp_path,
):
    runtime = SimpleNamespace(
        _request_meta_by_id={
            "req-background": {"session_scope": "persistent"},
            "req-isolated": {"session_scope": "isolated_per_run"},
        },
        current_request_meta=None,
    )
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={},
        resolve_access_root=lambda: tmp_path,
        _hashi_runtime=runtime,
    )
    adapter = HERAdapter(cfg, SimpleNamespace(), api_key="test-key")
    adapter._binary = tmp_path / "hashi-her"
    adapter._session_id = "session-main"
    background_started = asyncio.Event()
    release_background = asyncio.Event()
    calls = []

    async def run_task(prompt, *, resume, request_id, track_session_identity, **kwargs):
        calls.append((request_id, resume, track_session_identity))
        if request_id == "req-background":
            background_started.set()
            await release_background.wait()
            return _concurrency_task_result(
                tmp_path,
                text="background",
                session_id="session-main-next",
            )
        return _concurrency_task_result(
            tmp_path,
            text="isolated",
            session_id="session-isolated",
        )

    adapter._run_task_async = run_task
    background = asyncio.create_task(
        adapter.generate_response("background", "req-background")
    )
    await background_started.wait()

    isolated = await asyncio.wait_for(
        adapter.generate_response("cron", "req-isolated"),
        timeout=1,
    )

    assert isolated.is_success is True
    assert ("req-isolated", None, False) in calls
    assert adapter._session_id == "session-main"

    release_background.set()
    background_response = await background
    assert background_response.is_success is True
    assert adapter._session_id == "session-main-next"


@pytest.mark.asyncio
async def test_her_isolated_continuation_resumes_exact_checkpoint_without_replacing_primary(
    tmp_path,
):
    runtime = SimpleNamespace(
        _request_meta_by_id={
            "req-continue": {
                "session_scope": "isolated_resume",
                "resume_session_id": "session-scheduler",
            },
        },
        current_request_meta=None,
    )
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={},
        resolve_access_root=lambda: tmp_path,
        _hashi_runtime=runtime,
    )
    adapter = HERAdapter(cfg, SimpleNamespace(), api_key="test-key")
    adapter._binary = tmp_path / "hashi-her"
    adapter._session_id = "session-main"
    calls = []

    async def run_task(prompt, *, resume, request_id, track_session_identity, **kwargs):
        calls.append((request_id, resume, track_session_identity))
        return _concurrency_task_result(
            tmp_path,
            text="continued scheduler result",
            session_id="session-scheduler-next",
        )

    adapter._run_task_async = run_task

    response = await adapter.generate_response("continue", "req-continue")

    assert response.is_success is True
    assert calls == [("req-continue", "session-scheduler", False)]
    assert adapter._session_id == "session-main"
    assert response.stream_metadata["her_session_scope"] == "isolated_resume"
    assert response.stream_metadata["her_session_id"] == "session-scheduler-next"
    assert response.stream_metadata["her_resumed_session"] is True


@pytest.mark.asyncio
async def test_her_failure_persists_structured_error_and_redacted_stderr(tmp_path):
    fake = _write_exe(
        tmp_path / "hashi-her",
        """
        #!/usr/bin/env python3
        import json, sys
        print(json.dumps({
            "kind": "run_finished",
            "message": "Execution failed before completion.",
            "completion_status": "error",
            "error_kind": "api_http_error",
            "http_status": 400,
            "error_type": "invalid_request_error",
            "provider_request_id": "provider-req-123",
            "error_message": "tool result is missing",
            "body_snippet": "invalid sequence",
            "retryable": False,
        }))
        print("Authorization: Bearer secret-token", file=sys.stderr)
        raise SystemExit(1)
        """,
    )
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={},
        resolve_access_root=lambda: tmp_path,
    )
    adapter = HERAdapter(cfg, SimpleNamespace(), api_key="test-key")
    adapter._binary = fake
    adapter._supports_stream_json = True

    with pytest.raises(ClawCommandError) as raised:
        await adapter._run_task_async(
            "prompt",
            resume="session-before-error",
            request_id="req-error",
        )

    assert str(raised.value) == "tool result is missing"
    records = [
        json.loads(line)
        for line in (tmp_path / "backend_state" / "her_diagnostics.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    failure = next(record for record in records if record["kind"] == "command_failure")
    assert failure["request_id"] == "req-error"
    assert failure["parsed_error"]["http_status"] == 400
    assert failure["parsed_error"]["provider_request_id"] == "provider-req-123"
    assert '"kind": "run_finished"' in failure["stdout"]
    assert failure["stderr"] == "Authorization: Bearer <redacted>\n"
    assert "secret-token" not in json.dumps(records)


@pytest.mark.asyncio
async def test_her_early_stream_failure_uses_stderr_error_and_persists_it(tmp_path):
    fake = _write_exe(
        tmp_path / "hashi-her",
        """
        #!/usr/bin/env python3
        import json, sys
        print(json.dumps({"kind": "run_started", "model": "bad/model"}))
        print("INFO provider startup", file=sys.stderr)
        print(json.dumps({
            "kind": "api_http_error",
            "type": "error",
            "error": "unsupported model",
            "exit_code": 1,
        }), file=sys.stderr)
        raise SystemExit(1)
        """,
    )
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="bad/model",
        extra={},
        resolve_access_root=lambda: tmp_path,
    )
    adapter = HERAdapter(cfg, SimpleNamespace(), api_key="test-key")
    adapter._binary = fake
    adapter._supports_stream_json = True

    with pytest.raises(ClawCommandError) as raised:
        await adapter._run_task_async(
            "prompt",
            resume=None,
            request_id="req-early-error",
        )

    assert str(raised.value) == "unsupported model"
    assert raised.value.returncode == 1
    assert raised.value.parsed_error == {
        "kind": "api_http_error",
        "type": "error",
        "error": "unsupported model",
        "exit_code": 1,
    }
    records = [
        json.loads(line)
        for line in (tmp_path / "backend_state" / "her_diagnostics.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    failure = next(record for record in records if record["kind"] == "command_failure")
    assert failure["request_id"] == "req-early-error"
    assert failure["parsed_error"] == raised.value.parsed_error
    assert '"kind": "run_started"' in failure["stdout"]
    assert "INFO provider startup" in failure["stderr"]


@pytest.mark.asyncio
async def test_her_unstructured_failure_surfaces_redacted_stderr_and_persists_both(
    tmp_path,
):
    fake = _write_exe(
        tmp_path / "hashi-her",
        """
        #!/usr/bin/env python3
        import sys
        print("partial provider stdout")
        print("Authorization: Bearer secret-token", file=sys.stderr)
        print("503 Service Unavailable: provider offline", file=sys.stderr)
        raise SystemExit(1)
        """,
    )
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={},
        resolve_access_root=lambda: tmp_path,
    )
    adapter = HERAdapter(cfg, SimpleNamespace(), api_key="test-key")
    adapter._binary = fake
    adapter._supports_stream_json = True

    with pytest.raises(ClawCommandError) as raised:
        await adapter._run_task_async(
            "prompt",
            resume=None,
            request_id="req-unstructured-error",
        )

    assert str(raised.value) == (
        "Authorization: Bearer <redacted>\n"
        "503 Service Unavailable: provider offline"
    )
    records = [
        json.loads(line)
        for line in (tmp_path / "backend_state" / "her_diagnostics.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    failure = next(record for record in records if record["kind"] == "command_failure")
    assert failure["stdout"] == "partial provider stdout\n"
    assert failure["stderr"].endswith("503 Service Unavailable: provider offline\n")
    assert "secret-token" not in json.dumps(records)


@pytest.mark.asyncio
async def test_her_failed_persistent_turn_quarantines_session_and_surfaces_metadata(
    tmp_path,
):
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={},
        resolve_access_root=lambda: tmp_path,
    )
    adapter = HERAdapter(cfg, SimpleNamespace(), api_key="test-key")
    adapter._binary = tmp_path / "hashi-her"
    adapter._session_id = "session-poisoned"
    adapter._persist_session_identity()
    adapter._run_task_async = AsyncMock(
        side_effect=ClawCommandError(
            "pending tool result",
            returncode=1,
            parsed_error={
                "kind": "run_finished",
                "error_kind": "invalid_session_state",
                "error_type": "invalid_message_sequence",
                "error_message": "pending tool result",
            },
        )
    )

    response = await adapter.generate_response("continue", "req-poisoned")

    assert response.is_success is False
    assert response.error == "pending tool result"
    assert (
        response.stream_metadata["her_error"]["error_kind"] == "invalid_session_state"
    )
    assert (
        response.stream_metadata["her_error"]["error_type"]
        == "invalid_message_sequence"
    )
    assert adapter._session_id is None
    assert not adapter._session_state_path.exists()
    records = [
        json.loads(line)
        for line in (tmp_path / "backend_state" / "her_diagnostics.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    quarantine = next(
        record for record in records if record["kind"] == "session_quarantined"
    )
    assert quarantine["session_id"] == "session-poisoned"
    assert quarantine["error_kind"] == "invalid_session_state"


@pytest.mark.asyncio
async def test_her_physical_provider_failure_preserves_checkpoint_and_exact_error(tmp_path):
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={},
        resolve_access_root=lambda: tmp_path,
    )
    adapter = HERAdapter(cfg, SimpleNamespace(), api_key="test-key")
    adapter._binary = tmp_path / "hashi-her"
    adapter._session_id = "session-before-provider-error"
    adapter._persist_session_identity()
    provider_error = "429 Too Many Requests\nrequest_id=req_exact_123"
    adapter._run_task_async = AsyncMock(
        side_effect=ClawCommandError(
            provider_error,
            returncode=1,
            parsed_error={
                "kind": "run_finished",
                "error_message": provider_error,
                "terminal_kind": "provider_error",
                "message_origin": "provider",
                "exit_reasoning_status": "failed_physical",
                "checkpoint_preserved": True,
                "session_id": "session-after-provider-error",
                "model": "deepseek/test",
                "provider": "openai",
            },
        )
    )

    response = await adapter.generate_response("continue", "req-provider-error")

    assert response.is_success is False
    assert response.error == provider_error
    assert response.stream_metadata["her_error"]["terminal_kind"] == "provider_error"
    assert response.stream_metadata["her_error"]["message_origin"] == "provider"
    assert adapter._session_id == "session-after-provider-error"
    persisted = json.loads(adapter._session_state_path.read_text(encoding="utf-8"))
    assert persisted["session_id"] == "session-after-provider-error"


@pytest.mark.asyncio
async def test_isolated_model_renderer_rejects_nonempty_text_without_provenance(
    tmp_path,
):
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={},
        resolve_access_root=lambda: tmp_path,
    )
    adapter = HERAdapter(cfg, SimpleNamespace(), api_key="test-key")
    adapter._run_task_async = AsyncMock(
        return_value=ClawTaskResult(
            text="legacy deterministic report",
            model="deepseek/test",
            permission_mode="read-only",
            cwd=str(tmp_path),
            returncode=0,
            duration_ms=1,
            stdout="",
            stderr="",
            json_data={},
            tool_uses=[],
            tool_results=[],
            completion_status="completed",
            stop_reason="end_turn",
        )
    )

    with pytest.raises(ClawError, match="primary-model final message"):
        await adapter.run_habit_dream_model("render", request_id="req-render")


@pytest.mark.asyncio
async def test_her_task_runner_applies_meditation_safety_overrides(tmp_path):
    fake = _write_exe(
        tmp_path / "hashi-her",
        """
        #!/usr/bin/env python3
        import json, os, sys
        print(json.dumps({
            "message": "meditated",
            "model": "deepseek/test",
            "session_id": "meditation-session",
            "argv": sys.argv[1:],
            "cwd": os.getcwd(),
            "planning": os.environ.get("CLAW_TASK_PLANNING"),
            "iterations": os.environ.get("CLAW_MAX_TOOL_ITERATIONS"),
        }))
        """,
    )
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={
            "permission_mode": "danger-full-access",
            "dangerously_skip_permissions": True,
        },
        resolve_access_root=lambda: tmp_path,
    )
    adapter = HERAdapter(cfg, SimpleNamespace(), api_key="test-key")
    adapter._binary = fake
    worker_cwd = tmp_path / "worker"
    worker_cwd.mkdir()

    result = await adapter._run_task_async(
        "Meditate on bounded evidence.",
        resume=None,
        request_id="req-meditation",
        track_session_identity=False,
        permission_mode_override="read-only",
        allowed_tools_override=["read_file"],
        task_env_overrides={
            "CLAW_TASK_PLANNING": "0",
            "CLAW_MAX_TOOL_ITERATIONS": "8",
        },
        model_override="deepseek/worker",
        cwd_override=worker_cwd,
    )

    args = result.json_data["argv"]
    assert result.permission_mode == "read-only"
    assert args[args.index("--permission-mode") + 1] == "read-only"
    assert args[args.index("--model") + 1] == "deepseek/worker"
    assert args[args.index("--allowedTools") + 1] == "read_file"
    assert "--dangerously-skip-permissions" not in args
    assert "--resume" not in args
    assert result.json_data["planning"] == "0"
    assert result.json_data["iterations"] == "8"
    assert result.json_data["cwd"] == str(worker_cwd)
    assert result.cwd == str(worker_cwd)


@pytest.mark.asyncio
async def test_her_task_runner_passes_resumed_prompt_as_text_without_stdin(tmp_path):
    fake = _write_exe(
        tmp_path / "hashi-her",
        """
        #!/usr/bin/env python3
        import json, sys
        stdin = sys.stdin.read()
        print(json.dumps({
            "kind": "run_started",
            "model": "deepseek/test",
            "session_id": "session-next",
        }))
        print(json.dumps({
            "kind": "run_finished",
            "message": "resumed",
            "model": "deepseek/test",
            "session_id": "session-next",
            "completion_status": "completed",
            "stop_reason": "end_turn",
            "terminal_kind": "model_report",
            "message_origin": "primary_model",
            "exit_reasoning_status": "completed",
            "exit_reasoning_attempts": 1,
            "provider_stop_reason": "end_turn",
            "tool_uses": [],
            "tool_results": [],
            "argv": sys.argv[1:],
            "stdin": stdin,
        }))
        """,
    )
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={"permission_mode": "danger-full-access"},
        resolve_access_root=lambda: tmp_path,
    )
    adapter = HERAdapter(cfg, SimpleNamespace(), api_key="test-key")
    adapter._binary = fake
    adapter._supports_stream_json = True

    result = await adapter._run_task_async(
        "continue the exact checkpoint",
        resume="session-current",
        request_id="req-resume-stdin-regression",
    )

    assert result.text == "resumed"
    assert result.json_data["argv"][-4:] == [
        "--resume",
        "session-current",
        "prompt",
        "continue the exact checkpoint",
    ]
    assert "--stdin" not in result.json_data["argv"]
    assert result.json_data["stdin"] == ""


@pytest.mark.asyncio
async def test_her_task_runner_passes_effective_timeout_policy_to_compaction(tmp_path):
    fake = _write_exe(
        tmp_path / "hashi-her",
        """
        #!/usr/bin/env python3
        import json, os
        print(json.dumps({
            "message": "done",
            "model": "deepseek/test",
            "session_id": "timeout-session",
            "idle": os.environ.get("CLAW_SEMANTIC_COMPACTION_IDLE_TIMEOUT_SECONDS"),
            "idle_source": os.environ.get("CLAW_SEMANTIC_COMPACTION_IDLE_TIMEOUT_SOURCE"),
            "hard": os.environ.get("CLAW_REQUEST_HARD_TIMEOUT_SECONDS"),
            "hard_source": os.environ.get("CLAW_REQUEST_HARD_TIMEOUT_SOURCE"),
            "post_tool": os.environ.get("CLAW_POST_TOOL_STALL_TIMEOUT_SECONDS"),
            "post_tool_retry": os.environ.get("CLAW_POST_TOOL_RETRY_STALL_TIMEOUT_SECONDS"),
        }))
        """,
    )
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={
            "permission_mode": "danger-full-access",
            "idle_timeout_sec": 7_200,
            "hard_timeout_sec": 14_400,
            "post_tool_stall_timeout_sec": 90,
            "post_tool_stall_timeout_sec_by_provider": {"deepseek": 75},
            "post_tool_retry_stall_timeout_sec": 180,
            "_hashi_timeout_policy": {
                "sources": {
                    "idle_timeout_sec": "user override",
                    "hard_timeout_sec": "backend configuration",
                }
            },
        },
        resolve_access_root=lambda: tmp_path,
    )
    adapter = HERAdapter(cfg, SimpleNamespace(), api_key="test-key")
    adapter._binary = fake

    result = await adapter._run_task_async(
        "exercise timeout inheritance",
        resume=None,
        request_id="req-timeout-policy",
        task_env_overrides={
            "CLAW_SEMANTIC_COMPACTION_IDLE_TIMEOUT_SECONDS": "1",
            "CLAW_REQUEST_HARD_TIMEOUT_SECONDS": "2",
        },
    )

    assert result.json_data["idle"] == "7200"
    assert result.json_data["idle_source"] == "user override"
    assert result.json_data["hard"] == "14400"
    assert result.json_data["hard_source"] == "backend configuration"
    assert result.json_data["post_tool"] == "75"
    assert result.json_data["post_tool_retry"] == "180"


def test_post_tool_timeout_provider_override_follows_the_actual_task_model(tmp_path):
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/base-model",
        extra={
            "post_tool_stall_timeout_sec_by_provider": {
                "deepseek": 75,
                "openrouter": 95,
            }
        },
        resolve_access_root=lambda: tmp_path,
    )
    adapter = HERAdapter(cfg, SimpleNamespace(), api_key="test-key")

    assert adapter._post_tool_timeout_env("openrouter/worker-model") == {
        "CLAW_POST_TOOL_STALL_TIMEOUT_SECONDS": "95"
    }


@pytest.mark.asyncio
async def test_claw_adapter_stream_json_emits_verbose_events(tmp_path, caplog):
    fake = _write_exe(
        tmp_path / "claw",
        """
        #!/usr/bin/env python3
        import json, sys, time
        if "--help" in sys.argv:
            print("Usage: claw [--output-format text|json|stream-json] prompt TEXT")
        elif sys.argv[1] == "version":
            print(json.dumps({"kind": "version", "version": "0.1.0", "git_sha": "fake"}))
        else:
            assert "stream-json" in sys.argv
            for event in [
                {"kind": "run_started", "model": "deepseek/test", "session_id": "stream-session"},
                {"kind": "task_acknowledgement", "text": "I will inspect the requested file only."},
                {"kind": "permission_required", "tool_name": "bash", "current_mode": "workspace-write",
                 "required_mode": "danger-full-access", "reason": "shell execution", "input": "private command"},
                {"kind": "permission_decision", "tool_name": "bash", "decision": "denied",
                 "reason": "approval input unavailable"},
                {"kind": "task_plan", "phase": "initial", "frame": {
                    "active_goal": "inspect file",
                    "assurance": {
                        "validation_strategy": ["verify the exact file contents"],
                        "validation_evidence": ["read_file returned the requested contents"],
                        "test_strategy": ["run the parser regression check"],
                        "testing_evidence": ["parser regression passed"],
                        "critical_review_findings": [],
                        "unverified_items": [],
                    },
                }},
                {"kind": "control_invocation", "stage": "independent_review", "gate": "planning",
                 "revision_round": 1, "format_attempt": 1,
                 "request": {"system_prompt": ["PLANNING GATE"], "user_message": "raw task frame",
                             "allow_tools": False},
                 "raw_output": json.dumps({"decision": "pass"}), "outcome": "parsed", "error": None,
                 "usage": {"input_tokens": 13, "output_tokens": 5,
                           "cache_creation_input_tokens": 2, "cache_read_input_tokens": 3}},
                {"kind": "independent_review", "gate": "planning", "revision_round": 1,
                 "summary": "The revised plan is adequate.",
                 "review": {"decision": "pass", "summary": "The revised plan is adequate.",
                            "findings": [], "missing_evidence": [], "required_changes": [],
                            "evidence_refs": ["task frame"]}},
                {"kind": "control_invocation", "stage": "independent_review", "gate": "completion",
                 "revision_round": 0, "format_attempt": 1,
                 "request": {"system_prompt": ["COMPLETION REVIEW"], "user_message": "evidence index",
                             "allow_tools": True},
                 "raw_output": "", "outcome": "inspection_tools", "error": None,
                 "usage": {"input_tokens": 17, "output_tokens": 3}},
                {"kind": "independent_review", "gate": "completion", "revision_round": 0,
                 "summary": "Validation and tests are supported by raw evidence.",
                 "review": {"decision": "pass", "summary": "Validation and tests are supported by raw evidence.",
                            "findings": [
                                {"category": "verification", "issue": "File identity checked"},
                                {"category": "testing", "issue": "Planned test checked"},
                            ], "missing_evidence": [], "required_changes": [],
                            "evidence_refs": ["ReviewFile sha256", "ReviewRun isolated result"]}},
                {"kind": "semantic_compaction", "status": "started", "session_id": "stream-session",
                 "trigger_phase": "post_tool", "estimated_input_tokens": 351000,
                 "removed_message_count": 0, "timeout_seconds": 3595,
                 "timeout_source": "user override", "elapsed_ms": 0,
                 "original_context_unchanged": True, "will_continue": True},
                {"kind": "semantic_compaction", "status": "completed", "session_id": "stream-session",
                 "trigger_phase": "post_tool", "estimated_input_tokens": 351000,
                 "removed_message_count": 12, "timeout_seconds": 3595,
                 "timeout_source": "user override", "elapsed_ms": 40840,
                 "original_context_unchanged": False, "will_continue": True},
                {"kind": "thinking_summary", "summary": "thinking block received (48 chars hidden)", "thinking_chars": 48},
                {"kind": "assistant_delta", "text": "partial answer"},
                {"kind": "tool_start", "name": "read_file", "summary": "reading README.md"},
                {"kind": "tool_end", "name": "read_file", "summary": "read_file completed", "output_preview": "ok"},
                {"kind": "usage", "input_tokens": 5, "output_tokens": 7, "thinking_token_source": "estimated"},
                {"kind": "provider_stop_reason", "reason": "end_turn"},
                {"kind": "run_finished", "message": "final answer", "model": "deepseek/test", "iterations": 1,
                 "completion_status": "completed", "stop_reason": "end_turn", "provider_stop_reason": "end_turn",
                 "terminal_kind": "model_report", "message_origin": "primary_model",
                 "exit_reasoning_status": "completed", "exit_reasoning_attempts": 1,
                 "tool_uses": [{"name": "read_file"}], "tool_results": [],
                 "usage": {"input_tokens": 5, "output_tokens": 7}},
            ]:
                print(json.dumps(event), flush=True)
                time.sleep(0.01)
        """,
    )
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={"claw_binary_path": str(fake), "permission_mode": "read-only"},
        resolve_access_root=lambda: tmp_path,
    )
    adapter = ClawCLIAdapter(cfg, SimpleNamespace(), api_key="test-key")
    events = []

    async def collect(event):
        events.append(event)

    assert await adapter.initialize() is True
    with caplog.at_level(logging.INFO):
        response = await adapter.generate_response(
            "hello", "req-stream", on_stream_event=collect
        )

    assert response.is_success is True
    assert response.text == "final answer"
    assert response.usage.input_tokens == 5
    assert response.usage.output_tokens == 7
    assert response.usage.thinking_tokens == 12
    assert response.stop_reason == "end_turn"
    assert response.stream_metadata["claw_completion_status"] == "completed"
    assert response.stream_metadata["claw_provider_stop_reason"] == "end_turn"
    assert "fallback_report_generated" not in response.stream_metadata
    assert adapter._session_id == "stream-session"
    assert adapter.capabilities.supports_thinking_stream is True
    assert adapter.capabilities.supports_answer_stream is True
    assert events
    assert all(event.event_id for event in events)
    assert all(event.delivery_class for event in events)
    assert KIND_THINKING in [event.kind for event in events]
    assert KIND_ACKNOWLEDGEMENT in [event.kind for event in events]
    acknowledgement = next(
        event for event in events if event.kind == KIND_ACKNOWLEDGEMENT
    )
    assert acknowledgement.event_id == "req-stream:ack:initial"
    assert acknowledgement.delivery_class == DELIVERY_USER_COMMENTARY
    assert any(
        event.kind == KIND_PROGRESS
        and event.summary == "HER permission required for bash"
        and event.delivery_class == "control"
        and event.required is True
        and "private command" not in event.detail
        for event in events
    )
    assert any(
        event.kind == KIND_PROGRESS
        and event.summary == "HER permission denied for bash"
        for event in events
    )
    assert "review" in [event.kind for event in events]
    assert "validation" in [event.kind for event in events]
    assert "testing" in [event.kind for event in events]
    assert any(
        event.kind == "review" and "planning r1: PASS" in event.summary
        for event in events
    )
    assert any(
        event.kind == "validation" and "read_file returned" in event.summary
        for event in events
    )
    assert any(
        event.kind == "testing" and "parser regression passed" in event.summary
        for event in events
    )
    assert any(
        event.kind == "validation" and "evidence review r0: PASS" in event.summary
        for event in events
    )
    assert any(
        event.kind == "testing" and "evidence review r0: PASS" in event.summary
        for event in events
    )
    assert any(
        event.kind == KIND_PROGRESS and "HER stream started" in event.summary
        for event in events
    )
    assert KIND_TEXT_DELTA in [event.kind for event in events]
    assert all(
        event.delivery_class == "internal"
        for event in events
        if event.kind == KIND_TEXT_DELTA
    )
    assert KIND_TOOL_START in [event.kind for event in events]
    assert KIND_TOOL_END in [event.kind for event in events]
    assert (
        sum(
            event.kind == KIND_PROGRESS and "semantic_compaction" in event.summary
            for event in events
        )
        == 2
    )
    assert any(
        event.kind == KIND_PROGRESS
        and "🧠 semantic_compaction started" in event.summary
        and "~351K tokens" in event.summary
        and "user override" in event.summary
        and "request_id=req-stream" in event.detail
        for event in events
    )
    assert any(
        event.kind == KIND_PROGRESS
        and "✅ semantic_compaction completed" in event.summary
        and "removed 12" in event.summary
        and "raw history retained" in event.summary
        for event in events
    )
    assert any(event.detail == "thinking_chars=48" for event in events)
    assert not any("may be summarized or hidden" in event.summary for event in events)
    assert "HER tool started:" in caplog.text
    assert "name=read_file" in caplog.text
    assert "HER tool finished:" in caplog.text
    assert "output_preview=ok" in caplog.text
    assert "HER control invocation:" in caplog.text
    assert "input_tokens=13 output_tokens=5" in caplog.text
    assert "outcome=inspection_tools allow_tools=True" in caplog.text
    raw_events = [
        json.loads(line)
        for line in (tmp_path / "claw_exec_events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    persisted_review = next(
        event
        for event in raw_events
        if event.get("kind") == "independent_review" and event.get("gate") == "planning"
    )
    assert persisted_review["revision_round"] == 1
    assert persisted_review["review"] == {
        "decision": "pass",
        "summary": "The revised plan is adequate.",
        "findings": [],
        "missing_evidence": [],
        "required_changes": [],
        "evidence_refs": ["task frame"],
    }
    persisted_control = next(
        event for event in raw_events if event.get("kind") == "control_invocation"
    )
    assert persisted_control["request"]["user_message"] == "raw task frame"
    assert json.loads(persisted_control["raw_output"]) == {"decision": "pass"}
    assert persisted_control["usage"]["input_tokens"] == 13
    persisted_compaction = next(
        event for event in raw_events if event.get("kind") == "semantic_compaction"
    )
    assert persisted_compaction["session_id"] == "stream-session"
    correlated_controls = [
        json.loads(line)
        for line in (tmp_path / "claw_control_events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert {record["request_id"] for record in correlated_controls} == {"req-stream"}
    assert any(
        record["event"].get("kind") == "control_invocation"
        and record["event"].get("gate") == "planning"
        for record in correlated_controls
    )
    assert any(
        record["request_id"] == "req-stream"
        and record["event"].get("kind") == "semantic_compaction"
        and record["event"].get("request_id") == "req-stream"
        and record["event"].get("session_id") == "stream-session"
        for record in correlated_controls
    )
    assert (tmp_path / "claw_exec_events.jsonl").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "claw_control_events.jsonl").stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_medium_adapter_delivers_native_tool_turn_commentary(
    tmp_path, monkeypatch
):
    fake = _write_exe(
        tmp_path / "claw",
        """
        #!/usr/bin/env python3
        import json, sys, time
        if "--help" in sys.argv:
            print("Usage: claw [--output-format text|json|stream-json] prompt TEXT")
        elif sys.argv[1] == "version":
            print(json.dumps({"kind": "version", "version": "0.1.0", "git_sha": "fake"}))
        else:
            events = [
                {"kind": "run_started", "model": "deepseek/test", "session_id": "medium-commentary"},
                {"kind": "task_acknowledgement", "event_id": "task-acknowledgement:1",
                 "text": "Sunny will inspect the request. ☀️"},
                {"kind": "assistant_commentary", "event_id": "assistant-commentary:1",
                 "phase": "execution", "iteration": 1,
                 "text": "Sunny completed the inspection and is validating it. ☀️"},
                {"kind": "run_finished", "message": "verified final answer", "model": "deepseek/test",
                 "session_id": "medium-commentary", "iterations": 1,
                 "completion_status": "completed", "stop_reason": "end_turn",
                 "terminal_kind": "model_report", "message_origin": "primary_model",
                 "exit_reasoning_status": "completed", "exit_reasoning_attempts": 1,
                 "tool_uses": [], "tool_results": [],
                 "usage": {"input_tokens": 5, "output_tokens": 7}},
            ]
            for event in events:
                print(json.dumps(event), flush=True)
                if event["kind"] == "assistant_commentary":
                    time.sleep(0.05)
        """,
    )
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={"claw_binary_path": str(fake), "effort": "medium"},
        resolve_access_root=lambda: tmp_path,
    )
    observed_enablement = []
    native_controller = her_adapter_module._HERStreamCadenceController

    def fast_controller(*args, **kwargs):
        observed_enablement.append(kwargs["progress_enabled"])
        kwargs.update(
            first_update_s=0.001,
            target_interval_s=0.002,
            hard_interval_s=0.003,
            activity_grace_s=0,
        )
        return native_controller(*args, **kwargs)

    monkeypatch.setattr(
        her_adapter_module,
        "_HERStreamCadenceController",
        fast_controller,
    )
    adapter = HERAdapter(cfg, SimpleNamespace(), api_key="test-key")
    assert await adapter.initialize() is True
    events = []

    async def capture(event):
        events.append(event)

    response = await adapter.generate_response(
        "inspect request",
        "req-medium-commentary",
        on_stream_event=capture,
    )

    assert response.is_success is True
    assert observed_enablement == [True]
    assert [
        event.summary
        for event in events
        if event.kind == KIND_COMMENTARY
        and event.delivery_class == DELIVERY_USER_COMMENTARY
    ] == ["Sunny completed the inspection and is validating it. ☀️"]


@pytest.mark.asyncio
async def test_claw_adapter_terminal_final_supersedes_one_pending_commentary(tmp_path):
    fake = _write_exe(
        tmp_path / "claw",
        """
        #!/usr/bin/env python3
        import json, sys
        if "--help" in sys.argv:
            print("Usage: claw [--output-format text|json|stream-json] prompt TEXT")
        elif sys.argv[1] == "version":
            print(json.dumps({"kind": "version", "version": "0.1.0", "git_sha": "fake"}))
        else:
            for event in [
                {"kind": "run_started", "model": "deepseek/test", "session_id": "commentary-session"},
                {"kind": "task_acknowledgement", "event_id": "task-acknowledgement:1",
                 "text": "Sunny will inspect the request. ☀️"},
                {"kind": "task_plan", "phase": "initial", "revision": 1,
                 "event_id": "task-plan:1", "frame": {"active_goal": "inspect request"}},
                {"kind": "task_commentary", "phase": "replan", "revision": 2,
                 "event_id": "task-commentary:2",
                 "text": "Sunny completed the inspection and is finalizing. ☀️"},
                {"kind": "run_finished", "message": "verified final answer", "model": "deepseek/test",
                 "session_id": "commentary-session", "iterations": 1,
                 "completion_status": "completed", "stop_reason": "end_turn",
                 "terminal_kind": "model_report", "message_origin": "primary_model",
                 "exit_reasoning_status": "completed", "exit_reasoning_attempts": 1,
                 "tool_uses": [], "tool_results": [],
                 "usage": {"input_tokens": 5, "output_tokens": 7}},
            ]:
                print(json.dumps(event), flush=True)
        """,
    )
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={"claw_binary_path": str(fake), "effort": "max+"},
        resolve_access_root=lambda: tmp_path,
    )
    adapter = HERAdapter(cfg, SimpleNamespace(), api_key="test-key")
    assert await adapter.initialize() is True
    events = []

    async def capture(event):
        events.append(event)

    response = await adapter.generate_response(
        "inspect request",
        "req-terminal-commentary",
        on_stream_event=capture,
    )

    assert response.is_success is True
    assert response.text == "verified final answer"
    visible_commentary = [
        event
        for event in events
        if event.kind == KIND_COMMENTARY
        and event.delivery_class == DELIVERY_USER_COMMENTARY
    ]
    assert visible_commentary == []
    [superseded] = [
        event
        for event in events
        if event.kind == KIND_COMMENTARY
        and "suppressed_reason=superseded_by_final" in event.detail
    ]
    assert superseded.event_id == "task-commentary:2"
    assert superseded.delivery_class == DELIVERY_INTERNAL


@pytest.mark.asyncio
async def test_claw_adapter_stream_json_emits_actual_thinking_delta(tmp_path):
    fake = _write_exe(
        tmp_path / "claw",
        """
        #!/usr/bin/env python3
        import json, sys
        if "--help" in sys.argv:
            print("Usage: claw [--output-format text|json|stream-json] prompt TEXT")
        elif sys.argv[1] == "version":
            print(json.dumps({"kind": "version", "version": "0.1.0", "git_sha": "fake"}))
        else:
            for event in [
                {"kind": "run_started", "model": "deepseek/test"},
                {"kind": "thinking_delta", "text": " Need to inspect adapter mapping.", "thinking_chars": 33,
                 "reasoning_source": "reasoning", "visibility": "provider_returned"},
                {"kind": "thinking_redacted", "summary": "provider emitted encrypted reasoning block", "thinking_chars": 0,
                 "reasoning_source": "reasoning_details.encrypted", "visibility": "provider_redacted"},
                {"kind": "thinking_summary", "summary": "legacy aggregate should not double count", "thinking_chars": 99},
                {"kind": "usage", "input_tokens": 5, "output_tokens": 7},
                {"kind": "run_finished", "message": "final answer", "model": "deepseek/test", "iterations": 1,
                 "completion_status": "completed", "stop_reason": "end_turn",
                 "terminal_kind": "model_report", "message_origin": "primary_model",
                 "exit_reasoning_status": "completed", "exit_reasoning_attempts": 1,
                 "tool_uses": [], "tool_results": [], "usage": {"input_tokens": 5, "output_tokens": 7}},
            ]:
                print(json.dumps(event), flush=True)
        """,
    )
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={"claw_binary_path": str(fake), "permission_mode": "read-only"},
        resolve_access_root=lambda: tmp_path,
    )
    adapter = ClawCLIAdapter(cfg, SimpleNamespace(), api_key="test-key")
    events = []

    async def collect(event):
        events.append(event)

    assert await adapter.initialize() is True
    response = await adapter.generate_response(
        "hello", "req-stream", on_stream_event=collect
    )

    assert response.is_success is True
    assert response.usage.thinking_tokens == 8
    assert response.stream_metadata["claw_thinking"] == {
        "thinking_chars": 33,
        "thinking_tokens": 8,
        "thinking_event_count": 2,
        "thinking_redacted_count": 1,
        "thinking_sources": ["reasoning", "reasoning_details.encrypted"],
    }
    assert any(
        event.kind == KIND_THINKING
        and event.summary == " Need to inspect adapter mapping."
        and event.raw_delta == " Need to inspect adapter mapping."
        and event.detail == "thinking_chars=33;source=reasoning"
        for event in events
    )
    assert any(
        event.kind == KIND_THINKING
        and event.detail
        == "thinking_chars=0;redacted=true;source=reasoning_details.encrypted"
        for event in events
    )


@pytest.mark.asyncio
async def test_claw_direct_response_acknowledgement_is_final_only(tmp_path):
    fake = _write_exe(
        tmp_path / "claw",
        """
        #!/usr/bin/env python3
        import json, sys
        if "--help" in sys.argv:
            print("Usage: claw [--output-format text|json|stream-json] prompt [--stdin] [TEXT]")
        elif sys.argv[1] == "version":
            print(json.dumps({"kind": "version", "version": "0.1.0", "git_sha": "fake"}))
        else:
            answer = ("Hello from the configured Persona. " * 40).strip()
            for event in [
                {"kind": "run_started", "model": "deepseek/test"},
                {"kind": "task_acknowledgement", "text": answer},
                {"kind": "task_plan", "phase": "initial", "frame": {
                    "active_goal": "answer directly",
                    "direct_response": True,
                    "remaining_work": [],
                }},
                {"kind": "run_finished", "message": answer,
                 "model": "deepseek/test", "iterations": 0,
                 "completion_status": "completed", "stop_reason": "end_turn",
                 "terminal_kind": "model_report", "message_origin": "primary_model",
                 "exit_reasoning_status": "embedded", "exit_reasoning_attempts": 0,
                 "provider_stop_reason": "end_turn", "tool_uses": [], "tool_results": [],
                 "usage": {"input_tokens": 4, "output_tokens": 7}},
            ]:
                print(json.dumps(event), flush=True)
        """,
    )
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={
            "claw_binary_path": str(fake),
            "permission_mode": "read-only",
            "effort": "medium",
        },
        resolve_access_root=lambda: tmp_path,
    )
    adapter = ClawCLIAdapter(cfg, SimpleNamespace(), api_key="test-key")
    events = []

    async def collect(event):
        events.append(event)

    assert await adapter.initialize() is True
    response = await adapter.generate_response(
        "hello",
        "req-direct",
        on_stream_event=collect,
    )

    assert response.is_success is True
    assert response.tool_loop_count == 0
    expected = ("Hello from the configured Persona. " * 40).strip()
    assert len(expected) > 500
    assert response.text == expected
    acknowledgements = [event for event in events if event.kind == KIND_ACKNOWLEDGEMENT]
    assert len(acknowledgements) == 1
    assert acknowledgements[0].event_id == "req-direct:final"
    assert acknowledgements[0].delivery_class == DELIVERY_FINAL
    assert acknowledgements[0].required is True
    assert acknowledgements[0].summary == expected
    assert all(
        event.delivery_class != DELIVERY_USER_COMMENTARY for event in acknowledgements
    )


@pytest.mark.asyncio
async def test_claw_adapter_shutdown_kills_running_process(tmp_path):
    fake = _write_exe(
        tmp_path / "claw",
        """
        #!/usr/bin/env python3
        import json, sys, time
        if sys.argv[1] == "version":
            print(json.dumps({"kind": "version", "version": "0.1.0"}))
        else:
            time.sleep(20)
        """,
    )
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={
            "claw_binary_path": str(fake),
            "permission_mode": "read-only",
            "idle_timeout_sec": 1,
            "hard_timeout_sec": 30,
        },
        resolve_access_root=lambda: tmp_path,
    )
    global_cfg = SimpleNamespace()
    adapter = ClawCLIAdapter(cfg, global_cfg, api_key="test-key")
    assert await adapter.initialize() is True

    task = asyncio.create_task(adapter.generate_response("hello", "req-slow"))
    for _ in range(50):
        if adapter.current_proc is not None:
            break
        await asyncio.sleep(0.02)
    assert adapter.current_proc is not None

    await adapter.shutdown()
    response = await task

    assert response.is_success is False


@pytest.mark.asyncio
async def test_her_idle_timeout_uses_monotonic_clock_when_wall_clock_jumps(
    tmp_path, monkeypatch
):
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={"idle_timeout_sec": 1, "hard_timeout_sec": 30},
        resolve_access_root=lambda: tmp_path,
    )
    adapter = HERAdapter.__new__(HERAdapter)
    adapter.config = cfg
    activity_state = [time.monotonic()]
    task = asyncio.create_task(asyncio.sleep(0.02))

    monkeypatch.setattr("adapters.her.time.time", lambda: 10**12)
    timeout_kind = await asyncio.wait_for(
        adapter._wait_for_her_task_with_timeouts(
            task,
            started_monotonic=time.perf_counter(),
            activity_state=activity_state,
        ),
        timeout=0.2,
    )

    assert timeout_kind is None


def test_backend_activity_age_prefers_monotonic_clock(tmp_path, monkeypatch):
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={},
        resolve_access_root=lambda: tmp_path,
    )
    adapter = HERAdapter.__new__(HERAdapter)
    adapter.config = cfg
    adapter.last_activity_at = 1.0
    adapter.last_activity_monotonic = time.monotonic()

    monkeypatch.setattr("adapters.base.time.time", lambda: 10**12)

    assert adapter._last_activity_age() < 1.0


@pytest.mark.asyncio
async def test_claw_adapter_shutdown_stops_all_concurrent_her_processes(tmp_path):
    fake = _write_exe(
        tmp_path / "claw",
        """
        #!/usr/bin/env python3
        import json, sys, time
        if sys.argv[1] == "version":
            print(json.dumps({"kind": "version", "version": "0.1.0"}))
        elif "--help" in sys.argv:
            print("--output-format stream-json prompt --stdin")
        else:
            sys.stdin.read()
            time.sleep(20)
        """,
    )
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={
            "claw_binary_path": str(fake),
            "permission_mode": "read-only",
            "idle_timeout_sec": 30,
            "hard_timeout_sec": 60,
        },
        resolve_access_root=lambda: tmp_path,
    )
    adapter = ClawCLIAdapter(cfg, SimpleNamespace(), api_key="test-key")
    assert await adapter.initialize() is True

    foreground = asyncio.create_task(
        adapter.generate_response("foreground", "req-foreground")
    )
    isolated = asyncio.create_task(
        adapter.run_habit_dream_model("dream", request_id="req-dream")
    )
    for _ in range(100):
        if len(adapter._active_processes) == 2:
            break
        await asyncio.sleep(0.02)
    assert set(adapter._active_processes) == {"req-foreground", "req-dream"}

    await adapter.shutdown()
    foreground_result, isolated_result = await asyncio.gather(
        foreground,
        isolated,
        return_exceptions=True,
    )

    assert foreground_result.is_success is False
    assert isinstance(isolated_result, (ClawCommandError, ClawJsonError))
    assert adapter._active_processes == {}
    assert adapter.current_proc is None


@pytest.mark.asyncio
async def test_her_adapter_shutdown_reaps_request_during_semantic_compaction(tmp_path):
    fake = _write_exe(
        tmp_path / "claw-compaction",
        """
        #!/usr/bin/env python3
        import json, sys, time
        if sys.argv[1] == "version":
            print(json.dumps({"kind": "version", "version": "0.1.0"}))
        elif sys.argv[1] in {"doctor", "status"}:
            print(json.dumps({"kind": sys.argv[1], "ok": True}))
        elif "--help" in sys.argv:
            print("--output-format stream-json prompt --stdin")
        else:
            sys.stdin.read()
            print(json.dumps({
                "kind": "run_started",
                "session_id": "compaction-stop-session",
                "model": "deepseek/test",
            }), flush=True)
            print(json.dumps({
                "kind": "semantic_compaction",
                "status": "started",
                "session_id": "compaction-stop-session",
                "trigger_phase": "pre_provider",
                "estimated_input_tokens": 351000,
                "timeout_seconds": 3595,
                "timeout_source": "user override",
                "original_context_unchanged": True,
                "will_continue": True,
            }), flush=True)
            time.sleep(20)
        """,
    )
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={
            "claw_binary_path": str(fake),
            "permission_mode": "read-only",
            "idle_timeout_sec": 30,
            "hard_timeout_sec": 60,
        },
        resolve_access_root=lambda: tmp_path,
    )
    adapter = HERAdapter(cfg, SimpleNamespace(), api_key="test-key")
    events = []

    async def collect(event):
        events.append(event)

    assert await adapter.initialize() is True
    task = asyncio.create_task(
        adapter.generate_response(
            "trigger compaction",
            "req-compaction-stop",
            on_stream_event=collect,
        )
    )
    for _ in range(100):
        if any("semantic_compaction started" in event.summary for event in events):
            break
        await asyncio.sleep(0.02)

    assert any("semantic_compaction started" in event.summary for event in events)
    process = adapter._active_processes["req-compaction-stop"]

    await adapter.shutdown()
    response = await task

    assert response.is_success is False
    assert process.returncode is not None
    assert adapter._active_processes == {}
    assert adapter.current_proc is None


@pytest.mark.asyncio
async def test_claw_adapter_enforces_idle_timeout_and_logs_effective_policy(
    tmp_path, caplog
):
    fake = _write_exe(
        tmp_path / "claw",
        """
        #!/usr/bin/env python3
        import json, sys, time
        if sys.argv[1] == "version":
            print(json.dumps({"kind": "version", "version": "0.1.0"}))
        else:
            time.sleep(20)
        """,
    )
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={
            "claw_binary_path": str(fake),
            "permission_mode": "read-only",
            "idle_timeout_sec": 1,
            "hard_timeout_sec": 30,
        },
        resolve_access_root=lambda: tmp_path,
    )
    adapter = ClawCLIAdapter(cfg, SimpleNamespace(), api_key="test-key")
    assert await adapter.initialize() is True

    with caplog.at_level(logging.ERROR):
        response = await adapter.generate_response("hello", "req-claw-idle")

    assert response.is_success is False
    assert "idle for 1s" in response.error
    assert "kind=idle" in caplog.text
    assert "idle_timeout_s=1" in caplog.text
    assert "hard_timeout_s=30" in caplog.text
    assert "last_output_age_s=" in caplog.text
    assert "total_runtime_s=" in caplog.text
