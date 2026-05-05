from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace

import pytest

sys.modules.setdefault("edge_tts", types.ModuleType("edge_tts"))

from adapters.stream_events import KIND_SHELL_EXEC, KIND_THINKING, StreamEvent
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator.audit_mode import (
    AuditConfig,
    AuditResult,
    AuditProcessor,
    AuditTelemetryCollector,
    DEFAULT_AUDIT_CRITERION_SLOT_ID,
    DEFAULT_AUDIT_CRITERION_SLOT_TEXT,
    audit_result_from_mapping,
    build_audit_system_prompt,
    build_audit_user_prompt,
    effective_audit_criteria,
    format_audit_report,
    load_audit_config,
    parse_audit_json,
    should_audit_source,
    should_notify_audit_result,
    visible_audit_criteria,
)


def test_should_audit_user_sources_but_not_session_reset():
    for source in ["api", "text", "voice", "photo", "document", "sticker"]:
        assert should_audit_source(source)

    assert not should_audit_source("session_reset")
    assert not should_audit_source("bridge:hchat")
    assert not should_audit_source("hchat-reply:akane")
    assert not should_audit_source("scheduler")
    assert not should_audit_source("unknown")


def test_load_audit_config_reads_state_and_defaults():
    defaults = load_audit_config({})
    assert defaults.delivery == "always"
    assert defaults.severity_threshold == "low"

    config = load_audit_config(
        {
            "core": {"backend": "codex-cli", "model": "gpt-5.4"},
            "audit": {
                "backend": "claude-cli",
                "model": "claude-sonnet-4-6",
                "context_window": "5",
                "delivery": "always",
                "severity_threshold": "high",
                "timeout_s": "12.5",
            },
        }
    )

    assert config.core_backend == "codex-cli"
    assert config.core_model == "gpt-5.4"
    assert config.audit_backend == "claude-cli"
    assert config.audit_model == "claude-sonnet-4-6"
    assert config.context_window == 5
    assert config.delivery == "always"
    assert config.severity_threshold == "high"
    assert config.timeout_s == 12.5


def test_audit_prompt_is_observer_only_and_contains_criteria():
    system = build_audit_system_prompt({"2": "Flag missing tests.", "1": "Flag approval bypass."})
    user = build_audit_user_prompt(
        user_request="Deploy this.",
        core_raw="I deployed it.",
        telemetry={"tool_call_count": 1},
        severity_threshold="medium",
    )

    assert "Do not rewrite, polish, answer, or replace" in system
    assert "Start your response with { and end it with }" in system
    assert "audit_reasoning" in user
    assert "Criterion 1: Flag approval bypass." in system
    assert "Criterion 2: Flag missing tests." in system
    assert f"Criterion {DEFAULT_AUDIT_CRITERION_SLOT_ID}: {DEFAULT_AUDIT_CRITERION_SLOT_TEXT}" in system
    assert "unauthorized external/API calls" in system
    assert "verified or safely tested outcomes" in system
    assert "<core_raw>" in user
    assert "I deployed it." in user
    assert "strict JSON" in user


def test_default_audit_slot_9_can_be_overridden_or_suppressed():
    assert effective_audit_criteria({})[DEFAULT_AUDIT_CRITERION_SLOT_ID] == DEFAULT_AUDIT_CRITERION_SLOT_TEXT
    assert visible_audit_criteria({}) == {DEFAULT_AUDIT_CRITERION_SLOT_ID: DEFAULT_AUDIT_CRITERION_SLOT_TEXT}

    custom = "Only flag privacy regressions."
    assert visible_audit_criteria({DEFAULT_AUDIT_CRITERION_SLOT_ID: custom}) == {
        DEFAULT_AUDIT_CRITERION_SLOT_ID: custom
    }
    assert DEFAULT_AUDIT_CRITERION_SLOT_TEXT not in build_audit_system_prompt(
        {DEFAULT_AUDIT_CRITERION_SLOT_ID: custom}
    )

    assert visible_audit_criteria({DEFAULT_AUDIT_CRITERION_SLOT_ID: ""}) == {}
    assert DEFAULT_AUDIT_CRITERION_SLOT_TEXT not in build_audit_system_prompt(
        {DEFAULT_AUDIT_CRITERION_SLOT_ID: ""}
    )


def test_audit_prompt_compacts_telemetry_and_repeats_contract_at_tail():
    stream_events = [
        {
            "kind": "shell_exec",
            "summary": f"command {idx} " + ("x" * 500),
            "detail": "detail " + ("y" * 5000),
            "tool_name": "shell",
            "file_path": f"/tmp/example-{idx}",
            "timestamp": float(idx),
        }
        for idx in range(180)
    ]
    telemetry = {
        "request_id": "req-large",
        "source": "text",
        "summary": "large prompt",
        "stream_events": stream_events,
        "stream_event_count": len(stream_events),
        "observable_actions": stream_events,
    }

    user = build_audit_user_prompt(
        user_request="Answer the security register question.",
        core_raw="From the security register: no other open risks.",
        telemetry=telemetry,
        severity_threshold="low",
        evidence_path="/tmp/audit_evidence/req-large.json",
    )

    tail = user[-4000:]
    assert '"stream_events"' not in user
    assert '"observable_actions"' in user
    assert "FINAL AUDIT OUTPUT INSTRUCTION" in tail
    assert "Primary current-turn evidence file: /tmp/audit_evidence/req-large.json" in user
    assert "Do not answer the original user request." in tail
    assert "Start with { and end with }." in tail
    assert len(user) < 24000


def test_audit_full_prompt_clips_long_user_request_and_stays_under_cap():
    long_request = "Please audit this document.\n" + ("document text " * 2500) + "UNIQUE_LONG_REQUEST_TAIL"
    processor = AuditProcessor()

    prompt = processor.build_prompt_text(
        user_request=long_request,
        core_raw="Core answered briefly.",
        telemetry={"request_id": "req-long", "stream_event_count": 0},
        evidence_path="/tmp/audit_evidence/req-long.json",
    )

    assert len(prompt) < 24000
    assert "read evidence file for full user request" in prompt
    assert "FINAL AUDIT OUTPUT INSTRUCTION" in prompt[-4000:]
    assert "UNIQUE_LONG_REQUEST_TAIL" not in prompt


@pytest.mark.asyncio
async def test_telemetry_collector_keeps_raw_events_for_audit():
    collector = AuditTelemetryCollector()

    await collector.record(StreamEvent(kind=KIND_THINKING, summary="Thinking..."))
    await collector.record(StreamEvent(kind=KIND_SHELL_EXEC, summary="Running rm", detail="rm -rf /tmp/example"))

    data = collector.to_dict()
    assert data["stream_event_count"] == 2
    assert data["thinking_event_count"] == 1
    assert data["action_event_count"] == 1
    assert data["observable_actions"][0]["detail"] == "rm -rf /tmp/example"


@pytest.mark.asyncio
async def test_ollama_action_count_falls_back_to_stream_events():
    collector = AuditTelemetryCollector()
    await collector.record(StreamEvent(kind=KIND_SHELL_EXEC, summary="Running command"))
    await collector.record(StreamEvent(kind=KIND_SHELL_EXEC, summary="Running second command"))

    runtime = object.__new__(FlexibleAgentRuntime)
    runtime.config = SimpleNamespace(active_backend="ollama-api")
    runtime.get_current_model = lambda: "llama3.2"
    item = SimpleNamespace(
        request_id="req-ollama",
        source="text",
        summary="test",
        silent=False,
    )
    response = SimpleNamespace(tool_call_count=None, tool_loop_count=0, usage=None)

    telemetry = FlexibleAgentRuntime._build_audit_telemetry(runtime, item, response, collector)

    assert telemetry["tool_call_count"] == 2
    assert telemetry["action_event_count"] == 2


@pytest.mark.asyncio
async def test_audit_processor_success_path_calls_audit_backend():
    calls = []

    async def fake_invoker(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            text='{"status":"warn","max_severity":"high","triggered_sensors":["approval"],"summary":"approval issue","audit_reasoning":"The output claims deployment completed and telemetry shows one tool call.","findings":[{"severity":"high","category":"approval","evidence":"deployed without approval","recommendation":"ask first"}]}',
            is_success=True,
            error=None,
        )

    processor = AuditProcessor(backend_invoker=fake_invoker)

    result = await processor.process(
        request_id="req-1",
        source="text",
        user_request="Deploy.",
        core_raw="Done.",
        telemetry={"tool_call_count": 1},
        audit_criteria={"1": "Flag deployments without approval."},
    )

    assert result.audit_used is True
    assert result.audit_failed is False
    assert result.status == "warn"
    assert result.max_severity == "high"
    assert "telemetry shows one tool call" in result.reasoning
    assert result.findings[0].category == "approval"
    assert calls[0]["engine"] == "claude-cli"
    assert calls[0]["model"] == "claude-sonnet-4-6"
    assert calls[0]["request_id"] == "req-1:audit"


@pytest.mark.asyncio
async def test_audit_processor_timeout_fails_closed_to_no_user_block():
    async def slow_invoker(**kwargs):
        await asyncio.sleep(0.05)
        return SimpleNamespace(text='{"status":"pass","findings":[]}', is_success=True)

    processor = AuditProcessor(backend_invoker=slow_invoker, timeout_s=0.001)

    result = await processor.process(request_id="req-2", source="text", user_request="hi", core_raw="hello")

    assert result.audit_used is False
    assert result.audit_failed is True
    assert result.fallback_reason == "timeout"


def test_notification_threshold_and_report_format():
    result = audit_result_from_mapping(
        {
            "status": "warn",
            "max_severity": "medium",
            "summary": "one issue",
            "audit_reasoning": "The final output says tests were skipped.",
            "findings": [{"severity": "medium", "category": "quality", "evidence": "tests missing"}],
        }
    )

    assert should_notify_audit_result(result, AuditConfig(severity_threshold="medium", delivery="issues_only"))
    assert not should_notify_audit_result(result, AuditConfig(severity_threshold="high", delivery="issues_only"))
    report = format_audit_report(result)
    assert "🟡 Audit Report" in report
    assert "Status: WARN" in report
    assert "🧠 Audit Reasoning" in report
    assert "🟡 Medium: 1" in report
    assert "1. 🟡 MEDIUM · quality" in report
    assert "tests missing" in report


def test_parse_audit_json_extracts_object_from_prose():
    parsed = parse_audit_json(
        'Here is the audit:\n{"status":"pass","max_severity":"none","summary":"ok","audit_reasoning":"No risky evidence.","findings":[]}\nDone.'
    )

    assert parsed["status"] == "pass"
    assert parsed["audit_reasoning"] == "No risky evidence."


def test_failed_audit_report_is_visible_in_always_mode():
    result = AuditResult(audit_failed=True, fallback_reason="invalid_json", raw_text="not json")

    assert should_notify_audit_result(result, AuditConfig(delivery="always"))
    report = format_audit_report(result)
    assert "⚫ Audit Report" in report
    assert "Status: ERROR" in report
    assert "invalid_json" in report
