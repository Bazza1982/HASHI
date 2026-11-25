"""Focused regressions for PR #203 recovery context preservation."""
from __future__ import annotations

import json

import pytest

from orchestrator.her_v2.recovery_projection import bounded_prompt_projection
from orchestrator.her_v2.wip_journal import WIPJournal


def _checkpoint(**changes):
    value = {
        "available": True,
        "format": "herv3-provider-context-v1",
        "provider": "openrouter-api",
        "model": "test-model",
        "stage": "direct",
        "boundary": "completed_tool_batch",
        "message_count": 3,
        "reasoning_count": 0,
        "digest": "sha256:" + "a" * 64,
    }
    value.update(changes)
    return value


@pytest.mark.parametrize("value", [
    {"available": False},
    {"available": "false", "message_count": 2},
    {"available": "unknown", "message_count": 2},
    {"digest": "sha256:" + "a" * 64, "message_count": 2},
    {"available": True, "message_count": 2},
    {"present": True, "items_evidence_ref": "her_provider_context_items:s:t"},
    {"present": True, "digest": "sha256:" + "a" * 64,
     "sha256": "sha256:" + "b" * 64, "evidence_ref": "fake"},
    {**_checkpoint(), "truncated": True},
    None, "", [],
])
def test_invalid_checkpoint_never_promotes_to_present(value):
    context = {"session_id": "s", "interrupted_turn_id": "t", "provider_context_checkpoint": value}
    for _ in range(3):
        context = bounded_prompt_projection(context)
        checkpoint = context["provider_context_checkpoint"]
        assert checkpoint.get("present") is not True
        assert "items_evidence_ref" not in checkpoint


@pytest.mark.parametrize("context", [
    {"interrupted_turn_id": "t", "provider_context_checkpoint": _checkpoint()},
    {"session_id": "s", "provider_context_checkpoint": _checkpoint()},
    {"session_id": "s", "interrupted_turn_id": "t",
     "provider_context_checkpoint": _checkpoint(truncated="true")},
])
def test_checkpoint_without_complete_canonical_identity_is_unavailable(context):
    projected = bounded_prompt_projection(context)["provider_context_checkpoint"]
    assert projected.get("present") is not True
    assert "items_evidence_ref" not in projected


def test_valid_checkpoint_identity_survives_repeated_projection():
    context = {"session_id": "s", "interrupted_turn_id": "t", "provider_context_checkpoint": _checkpoint()}
    first = bounded_prompt_projection(context)["provider_context_checkpoint"]
    assert first["present"] is True
    assert first["digest"] == "sha256:" + "a" * 64
    assert first["items_evidence_ref"] == "her_provider_context_items:s:t"
    context = {"session_id": "s", "interrupted_turn_id": "t", "provider_context_checkpoint": first}
    assert bounded_prompt_projection(context)["provider_context_checkpoint"] == first


def test_small_receipts_keep_results_and_are_not_limited_to_recent_eight():
    receipts = [
        {"operation_id": f"read-{n}", "tool_call_id": f"call-{n}",
         "tool_name": "file_read", "status": "completed", "completed": True,
         "result": {"file": f"result-{n}.txt", "summary": f"valuable fact {n}"}}
        for n in range(12)
    ]
    view = bounded_prompt_projection({"session_id": "s", "interrupted_turn_id": "t", "tool_receipts": receipts})
    result = view["tool_receipts"]
    assert result["total"] == 12
    assert result["omitted"] == 0
    assert len(result["recent"]) == 12
    assert result["recent"][0]["completed"] is True
    assert result["recent"][0]["tool_call_id"] == "call-0"
    assert result["recent"][0]["result"]["summary"] == "valuable fact 0"


def test_large_receipt_keeps_useful_nested_summary_and_priority():
    receipts = [
        {"operation_id": "original-read", "status": "completed",
         "result": {"summary": "The archive contains the requested report",
                    "data": "large evidence " * 1000}},
        *[{"operation_id": f"later-{n}", "status": "completed"} for n in range(1500)],
    ]
    view = bounded_prompt_projection({
        "session_id": "s", "interrupted_turn_id": "t", "tool_receipts": receipts,
    })["tool_receipts"]
    original = next(item for item in view["recent"] if item["operation_id"] == "original-read")
    assert original["result"]["summary"] == "The archive contains the requested report"
    assert original["result"]["data"]["truncated"] is True
    assert original["canonical_evidence_ref"]


def test_four_long_unfinished_wip_goals_keep_original_and_latest(tmp_path):
    journal = WIPJournal(tmp_path / "wip.jsonl")
    originals = []
    for n in range(4):
        goal = f"GOAL-{n}: recover important user task {n}; " + ("meaningful detail " * 200)
        originals.append(goal)
        journal.begin_turn(request_id=f"r{n}", prompt=goal, request_summary=goal)
    journal.append_audit({"event": "tool_completed", "event_id": "progress-1",
                          "payload": {"summary": "Latest progress: saved the report"}})
    journal.append_audit({"event": "provider_error", "event_id": "failure-1",
                          "payload": {"summary": "Unresolved: verify the missing page"}})
    capsule = journal.recovery_capsule(journal.snapshot().records)
    projected = journal.project_capsule(capsule, evidence_ref=f"wip_journal:{journal.path}")
    unfinished = projected["unfinished_requests"]
    assert len(json.dumps(unfinished, ensure_ascii=False).encode("utf-8")) <= 8192
    assert unfinished["total"] == 4
    assert unfinished["omitted"] == 0
    serialized = json.dumps(projected, ensure_ascii=False)
    assert "GOAL-0: recover important user task" in serialized
    assert "GOAL-3: recover important user task" in serialized
    assert "Latest progress: saved the report" in serialized
    assert "Unresolved: verify the missing page" in serialized
    assert all(goal.strip() in journal.path.read_text(encoding="utf-8") for goal in originals)


def test_wip_keeps_latest_progress_and_failure_when_event_rows_are_large(tmp_path):
    journal = WIPJournal(tmp_path / "wip.jsonl")
    journal.begin_turn(request_id="original", prompt="Build the report", request_summary="Build the report")
    for n in range(10):
        journal.append_audit({"event": "tool_completed", "event_id": f"progress-{n}",
                              "payload": {"summary": f"Progress {n}: " + "detail " * 700}})
        journal.append_audit({"event": "provider_error", "event_id": f"failure-{n}",
                              "payload": {"summary": f"Failure {n}: " + "error " * 700}})
    projected = journal.project_capsule(journal.recovery_capsule(journal.snapshot().records),
                                        evidence_ref=f"wip_journal:{journal.path}")
    serialized = json.dumps(projected, ensure_ascii=False)
    assert "Progress 9:" in serialized
    assert "Failure 9:" in serialized
    assert projected["unfinished_work_activity"]["total"] == 10
    assert projected["failures"]["total"] == 10
    assert len(json.dumps(projected["unfinished_work_activity"], ensure_ascii=False).encode("utf-8")) <= 8192
    assert len(json.dumps(projected["failures"], ensure_ascii=False).encode("utf-8")) <= 8192


@pytest.mark.asyncio
async def test_omitted_recovery_receipt_retrievable_by_scoped_agent_tool(tmp_path, monkeypatch):
    from dataclasses import replace
    from adapters.her_v2_provider import _EvidenceRecordingToolRegistry
    from orchestrator.her_v2.backend_session import HerBackendSessionCoordinator
    from tests.test_herv3_p0_checkpoint_recovery_bounds import _runtime_event
    from tests.test_herv3_pr203_provider_wire import _direct_request
    from tools.registry import ToolRegistry

    agent_home = tmp_path / "agent-home"
    agent_home.mkdir()
    coordinator = HerBackendSessionCoordinator(agent_home / "backend_state" / "her_v2")

    def accept(request_id):
        encoded, _ = coordinator.prepare_transport(
            session_id="session-1", sections=[], resources=[], user_message="recover",
            request_id=request_id, message_id="message-" + request_id,
            instance_id="HASHI-test", agent_id="agent1", owner_id="owner",
            hashi_conversation_id="conversation", context_generation=1,
            workzone_identity=str(agent_home),
        )
        return coordinator.accept(encoded)

    source = accept("source-request")
    full_result = "first valuable result " + ("long evidence " * 3000) + " FINAL-FACT"
    coordinator.store.record_runtime_event(
        session_id=source.session_id, turn_id=source.turn_id,
        record=_runtime_event(source.turn_id, "receipt-event", "tool_receipt",
                              receipt={"operation_id": "read-old", "tool_call_id": "read-old",
                                       "status": "completed", "completed": True, "output": full_result}),
    )
    assert coordinator.store.reconcile_interrupted() == 1
    successor = accept("successor")
    context = coordinator.store.consume_recovery_context(
        session_id=successor.session_id, current_turn_id=successor.turn_id)
    view = bounded_prompt_projection(context)
    assert view["source_request_id"] == "source-request"
    receipt = view["tool_receipts"]["recent"][0]
    assert receipt["output"]["truncated"] is True

    registry = ToolRegistry(
        allowed_tools=["request_diagnostics"], access_root=agent_home,
        workspace_dir=agent_home, secrets={},
        audit_context={"agent_name": "agent1", "owner_id": "owner",
                       "instance_id": "HASHI-test", "hashi_session_id": "conversation",
                       "context_generation": 1, "session_workspace": str(agent_home)},
    )
    request = replace(_direct_request(), context={
        "her_v3": True, "pcm_input": {"runtime_context": {"active_turn_recovery": view}},
    })
    registry = _EvidenceRecordingToolRegistry(registry, request)
    evidence = view["tool_receipts"]["evidence_ref"]
    from orchestrator import request_diagnostics as diagnostics
    build_report = diagnostics.build_request_diagnostics
    report_calls = []

    def counted_report(**kwargs):
        report_calls.append(kwargs["request_id"])
        return build_report(**kwargs)

    monkeypatch.setattr(diagnostics, "build_request_diagnostics", counted_report)
    fragments = []
    offset = 0
    digest = ""
    while True:
        result = await registry.execute("request_diagnostics", {
            "request_id": view["source_request_id"], "recovery_evidence_ref": evidence,
            "recovery_item_id": "read-old", "offset": offset,
            "expected_digest": digest,
        }, tool_call_id=f"evidence-{offset}")
        assert not result.is_error, result.output
        page = json.loads(result.output)["recovery_evidence"]
        assert page["status"] == "available"
        digest = page["digest"]
        fragments.append(page["content"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert json.loads("".join(fragments))["output"] == full_result
    assert len(fragments) > 1
    assert report_calls == ["source-request"], "later pages must not rebuild full request diagnostics"
    stale = await registry.execute("request_diagnostics", {
        "request_id": "source-request", "recovery_evidence_ref": evidence,
        "recovery_item_id": "read-old", "offset": 2048,
        "expected_digest": "sha256:" + "0" * 64,
    }, tool_call_id="stale-page")
    assert json.loads(stale.output)["recovery_evidence"]["status"] == "not available"
    unrelated = await registry.execute("request_diagnostics", {
        "request_id": "other-request",
        "recovery_evidence_ref": evidence.replace(source.turn_id, "unrelated-turn"),
    }, tool_call_id="unrelated-evidence")
    assert json.loads(unrelated.output)["recovery_evidence"]["status"] == "not available"

    other_registry = ToolRegistry(
        allowed_tools=["request_diagnostics"], access_root=agent_home,
        workspace_dir=agent_home, secrets={},
        audit_context={"agent_name": "agent1", "owner_id": "other-owner",
                       "instance_id": "HASHI-test", "hashi_session_id": "conversation",
                       "context_generation": 1, "session_workspace": str(agent_home)},
    )
    other_registry = _EvidenceRecordingToolRegistry(other_registry, request)
    rejected = await other_registry.execute("request_diagnostics", {
        "request_id": "source-request", "recovery_evidence_ref": evidence,
        "recovery_item_id": "read-old",
    }, tool_call_id="other-owner")
    assert "FINAL-FACT" not in rejected.output
    assert "not available" in rejected.output.lower()

@pytest.mark.asyncio
async def test_agent_tool_recovers_verified_checkpoint_tool_output(tmp_path):
    from dataclasses import replace
    from adapters.her_v2_provider import _EvidenceRecordingToolRegistry
    from orchestrator.her_v2.backend_session import HerBackendSessionCoordinator
    from tests.test_her_v2_control_plane import _run_claim
    from tests.test_herv3_pr203_provider_wire import _direct_request
    from tools.registry import ToolRegistry

    home = tmp_path / "agent-home"
    home.mkdir()
    coordinator = HerBackendSessionCoordinator(home / "backend_state" / "her_v2")

    def accept(request_id):
        encoded, _ = coordinator.prepare_transport(
            session_id="session-1", sections=[], resources=[], user_message="write the report",
            request_id=request_id, message_id="message-" + request_id,
            instance_id="HASHI-test", agent_id="agent1", owner_id="owner",
            hashi_conversation_id="conversation", context_generation=1,
            workzone_identity=str(home),
        )
        return coordinator.accept(encoded)

    source = accept("source-request")
    writer = ToolRegistry(allowed_tools=["file_write"], access_root=home,
                          workspace_dir=home, secrets={})
    target = home / "report.txt"
    written = await writer.execute("file_write", {
        "path": str(target), "content": "real isolated tool effect",
    }, tool_call_id="write-1")
    assert not written.is_error
    assert target.read_text(encoding="utf-8") == "real isolated tool effect"
    coordinator.record_provider_context_checkpoint(source, {
        "provider": "openrouter-api", "model": "test-model", "stage": "direct",
        "boundary": "completed_tool_batch",
        "messages": [
            {"role": "user", "content": "write the report"},
            {"role": "assistant", "tool_calls": [
                {"id": "write-1", "type": "function",
                 "function": {"name": "file_write", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "write-1", "content": written.output},
        ],
        "reasoning": [{"private": "hidden-reasoning-must-not-leak"}],
        "producer_run_claim": _run_claim("run-source", "source-request"),
    })
    assert coordinator.store.reconcile_interrupted() == 1
    successor = accept("successor")
    context = coordinator.store.consume_recovery_context(
        session_id=successor.session_id, current_turn_id=successor.turn_id)
    view = bounded_prompt_projection(context)
    evidence = view["provider_context_checkpoint"]["items_evidence_ref"]
    registry = ToolRegistry(
        allowed_tools=["request_diagnostics"], access_root=home, workspace_dir=home, secrets={},
        audit_context={"agent_name": "agent1", "owner_id": "owner",
                       "instance_id": "HASHI-test", "hashi_session_id": "conversation",
                       "context_generation": 1, "session_workspace": str(home)},
    )
    request = replace(_direct_request(), context={
        "her_v3": True, "pcm_input": {"runtime_context": {"active_turn_recovery": view}},
    })
    registry = _EvidenceRecordingToolRegistry(registry, request)
    result = await registry.execute("request_diagnostics", {
        "request_id": "source-request", "recovery_evidence_ref": evidence,
        "recovery_item_id": "write-1",
    }, tool_call_id="fetch-result")
    page = json.loads(result.output)["recovery_evidence"]
    assert page["status"] == "available"
    assert page["field"] == "provider_tool_result"
    assert json.loads(page["content"]) == {"tool_call_id": "write-1", "content": written.output}
    assert "hidden-reasoning-must-not-leak" not in result.output
    for wrong_id in ("", "unknown"):
        rejected = await registry.execute("request_diagnostics", {
            "request_id": "source-request", "recovery_evidence_ref": evidence,
            "recovery_item_id": wrong_id,
        }, tool_call_id="wrong-id")
        assert json.loads(rejected.output)["recovery_evidence"]["status"] == "not available"

# HASHI-Fingerprint: hfp_501cfe5c0b5d4e1cb8b14d8300e4bc6b
