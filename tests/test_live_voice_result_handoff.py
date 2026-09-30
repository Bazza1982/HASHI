"""The phone identifies a complete canonical Run answer alongside separate effect evidence."""
from __future__ import annotations

from hashlib import sha256
from types import SimpleNamespace

import pytest

from orchestrator.frontend_live_voice import worker_actions
from tests.test_live_voice_actions import phone
from tools.registry import ToolRegistry


@pytest.mark.asyncio
async def test_long_query_result_keeps_every_item_without_treating_model_text_as_evidence(
    phone, tmp_path, monkeypatch,
):
    accepted = phone.store.accept_run(
        session_id=phone.session_id, owner_id=phone.owner_id, agent_id=phone.agent_id,
        request_id="phone-news-handoff", text="Read every item in the news report",
        source="session-api", idempotency_key="phone-news-handoff",
        expected_context_generation=phone.binding.context_generation,
    )
    report = "\n".join(f"News item {number:02d}: " + ("detail " * 18).strip() for number in range(1, 31))
    assert len(report) > 3000
    (tmp_path / "news.txt").write_text(report, encoding="utf-8")
    runtime = SimpleNamespace(workspace_dir=tmp_path, name=phone.agent_id,
                              session_store=phone.store)
    registry = ToolRegistry(allowed_tools=["file_read"], access_root=tmp_path,
                            workspace_dir=tmp_path, secrets={},
                            audit_context={"request_id": accepted.request_id})
    for index in range(29):
        read = await registry.execute("file_read", {"path": "news.txt", "limit": 100}, f"read-news-{index}")
        assert not read.is_error
    assert read.details["effect_receipt"]["complete_content"] is False
    phone.store.finish_request(accepted.request_id, success=True, assistant_text=report)

    verifier_calls = []

    async def overconfident_verifier(_runtime, state, **_kwargs):
        verifier_calls.append(state)
        return {"actions": [{"action_id": "news", "verified": True,
                             "evidence_refs": [state["actions"][0]["receipts"][0]["evidence_ref"]],
                             "receipt": "All thirty items verified."}]}

    monkeypatch.setattr(worker_actions, "invoke_phone_judgment", overconfident_verifier)
    inspected = await worker_actions.inspect_phone_action_results(
        runtime, accepted.request_id,
        [{"action_id": "news", "kind": "query", "request": "Read every item in the news report"}],
    )
    result = inspected["run_result"]
    assert result["text"] is None
    assert result["content_owner"] == "pao_session_store"
    assert result["content_included"] is False
    assert result["characters"] == len(report)
    assert result["sha256"] == sha256(report.encode("utf-8")).hexdigest()
    assert result["complete"] is True
    assert result["verification"] == "model_authored_unverified"
    assert result["message_id"] == phone.store.get_run(
        accepted.run_id, owner_id=phone.owner_id)["final_message_id"]
    assert phone.store.get_message(result["message_id"], session_id=phone.session_id)["text"] == report
    assert inspected["actions"][0]["status"] == "unknown"
    assert inspected["actions"][0]["evidence_refs"] == []
    assert len(inspected["tool_observations"]) == 29
    assert inspected["tool_observations"][0]["complete_content"] is False
    assert inspected["verification_input"] == [{"action_id": "news", "receipt_count_total": 29,
                                                "receipt_count_supplied": 12, "receipts_omitted": 17}]
    assert inspected["inspection_status"] == "evidence_budget_exceeded"
    assert verifier_calls == []


@pytest.mark.asyncio
async def test_completed_model_claim_without_tool_evidence_remains_unverified(phone, tmp_path):
    accepted = phone.store.accept_run(
        session_id=phone.session_id, owner_id=phone.owner_id, agent_id=phone.agent_id,
        request_id="phone-unsupported-count", text="Check current news",
        source="session-api", idempotency_key="phone-unsupported-count",
        expected_context_generation=phone.binding.context_generation,
    )
    phone.store.finish_request(accepted.request_id, success=True,
                               assistant_text="I found eight current news stories.")
    runtime = SimpleNamespace(workspace_dir=tmp_path, name=phone.agent_id,
                              session_store=phone.store)
    inspected = await worker_actions.inspect_phone_action_results(
        runtime, accepted.request_id,
        [{"action_id": "news", "kind": "query", "request": "Check current news"}],
    )
    assert inspected["run_result"]["text"] is None
    assert phone.store.get_message(inspected["run_result"]["message_id"],
                                   session_id=phone.session_id)["text"] == "I found eight current news stories."
    assert inspected["run_result"]["verification"] == "model_authored_unverified"
    assert inspected["actions"][0]["status"] == "unknown"
    assert inspected["tool_observations"] == []

@pytest.mark.asyncio
async def test_stopped_query_cannot_be_verified_from_partial_successful_reads(phone, tmp_path, monkeypatch):
    accepted = phone.store.accept_run(
        session_id=phone.session_id, owner_id=phone.owner_id, agent_id=phone.agent_id,
        request_id="phone-stopped-news", text="Find and report all news",
        source="session-api", idempotency_key="phone-stopped-news",
        expected_context_generation=phone.binding.context_generation,
    )
    (tmp_path / "one-result.txt").write_text("First result, more expected.", encoding="utf-8")
    runtime = SimpleNamespace(workspace_dir=tmp_path, name=phone.agent_id,
                              session_store=phone.store)
    registry = ToolRegistry(allowed_tools=["file_read"], access_root=tmp_path,
                            workspace_dir=tmp_path, secrets={},
                            audit_context={"request_id": accepted.request_id})
    read = await registry.execute("file_read", {"path": "one-result.txt"}, "read-before-stop")
    assert not read.is_error
    phone.store.cancel_run(accepted.run_id, owner_id=phone.owner_id, reason="user_stop")
    calls = []

    async def would_claim_success(_runtime, _state, **_kwargs):
        calls.append(True)
        return {"actions": [{"action_id": "news", "verified": True,
                             "evidence_refs": [read.details["effect_receipt"]["evidence_ref"]],
                             "receipt": "All news received."}]}

    monkeypatch.setattr(worker_actions, "invoke_phone_judgment", would_claim_success)
    inspected = await worker_actions.inspect_phone_action_results(
        runtime, accepted.request_id,
        [{"action_id": "news", "kind": "query", "request": "Find and report all news"}],
    )
    assert inspected["run_state"] == "stopped"
    assert inspected["actions"][0]["status"] == "unknown"
    assert inspected["actions"][0]["evidence_refs"] == []
    assert inspected["tool_observations"][0]["evidence_ref"] == read.details["effect_receipt"]["evidence_ref"]
    assert inspected["run_result"] is None
    assert calls == []


@pytest.mark.asyncio
async def test_stopped_write_keeps_real_saved_readback_separate_from_stop(phone, tmp_path, monkeypatch):
    accepted = phone.store.accept_run(
        session_id=phone.session_id, owner_id=phone.owner_id, agent_id=phone.agent_id,
        request_id="phone-stopped-write", text="Save the exercise record",
        source="session-api", idempotency_key="phone-stopped-write",
        expected_context_generation=phone.binding.context_generation,
    )
    runtime = SimpleNamespace(workspace_dir=tmp_path, name=phone.agent_id,
                              session_store=phone.store)
    registry = ToolRegistry(allowed_tools=["file_write"], access_root=tmp_path,
                            workspace_dir=tmp_path, secrets={}, audit_context={
        "_runtime": runtime, "owner_id": phone.owner_id,
        "hashi_session_id": phone.session_id, "hashi_run_id": accepted.run_id,
        "request_id": accepted.request_id})
    saved = await registry.execute("file_write", {"path": "fitness.txt",
        "content": "Exercise: 40 minutes\n"}, "save-before-stop")
    assert not saved.is_error
    phone.store.cancel_run(accepted.run_id, owner_id=phone.owner_id, reason="user_stop")

    async def verify_saved_effect(_runtime, state, **_kwargs):
        row = state["actions"][0]
        receipt = row["receipts"][0]
        assert row["kind"] == "write" and receipt["readback"] is True
        assert receipt["observed"] == "Exercise: 40 minutes\n"
        return {"actions": [{"action_id": row["action_id"], "verified": True,
                             "evidence_refs": [receipt["evidence_ref"]],
                             "receipt": "The exercise record was saved and read back."}]}

    monkeypatch.setattr(worker_actions, "invoke_phone_judgment", verify_saved_effect)
    inspected = await worker_actions.inspect_phone_action_results(runtime, accepted.request_id,
        [{"action_id": "fitness", "kind": "write", "request": "Save the exercise record"}])
    assert inspected["run_state"] == "stopped"
    assert inspected["actions"][0]["status"] == "verified"
    assert inspected["actions"][0]["evidence_refs"]
    assert (tmp_path / "fitness.txt").read_text() == "Exercise: 40 minutes\n"

@pytest.mark.asyncio
async def test_failed_optional_verifier_preserves_completed_run_answer(phone, tmp_path, monkeypatch):
    accepted = phone.store.accept_run(
        session_id=phone.session_id, owner_id=phone.owner_id, agent_id=phone.agent_id,
        request_id="phone-verifier-failure", text="Read the news",
        source="session-api", idempotency_key="phone-verifier-failure",
        expected_context_generation=phone.binding.context_generation,
    )
    report = "First item. Second item. Third item."
    (tmp_path / "news.txt").write_text(report, encoding="utf-8")
    runtime = SimpleNamespace(workspace_dir=tmp_path, name=phone.agent_id,
                              session_store=phone.store)
    registry = ToolRegistry(allowed_tools=["file_read"], access_root=tmp_path,
                            workspace_dir=tmp_path, secrets={},
                            audit_context={"request_id": accepted.request_id})
    read = await registry.execute("file_read", {"path": "news.txt"}, "read-before-verifier-error")
    assert not read.is_error
    phone.store.finish_request(accepted.request_id, success=True, assistant_text=report)

    async def verifier_unavailable(_runtime, _state, **_kwargs):
        raise RuntimeError("temporary model failure")

    monkeypatch.setattr(worker_actions, "invoke_phone_judgment", verifier_unavailable)
    inspected = await worker_actions.inspect_phone_action_results(
        runtime, accepted.request_id,
        [{"action_id": "news", "kind": "query", "request": "Read the news"}],
    )
    assert inspected["run_result"]["text"] is None
    assert phone.store.get_message(inspected["run_result"]["message_id"],
                                   session_id=phone.session_id)["text"] == report
    assert inspected["actions"][0]["status"] == "unknown"
    assert inspected["inspection_error"] == "effect_inspection_unavailable"


@pytest.mark.asyncio
async def test_large_canonical_answer_stays_in_session_store_not_worker_ipc(phone, tmp_path):
    accepted = phone.store.accept_run(
        session_id=phone.session_id, owner_id=phone.owner_id, agent_id=phone.agent_id,
        request_id="phone-oversize-report", text="Read the complete report",
        source="session-api", idempotency_key="phone-oversize-report",
        expected_context_generation=phone.binding.context_generation,
    )
    report = ("chapter detail. " * 40000).strip()
    phone.store.finish_request(accepted.request_id, success=True, assistant_text=report)
    runtime = SimpleNamespace(workspace_dir=tmp_path, name=phone.agent_id,
                              session_store=phone.store)
    inspected = await worker_actions.inspect_phone_action_results(
        runtime, accepted.request_id,
        [{"action_id": "report", "kind": "query", "request": "Read the complete report"}],
    )
    result = inspected["run_result"]
    assert result["text"] is None
    assert result["complete"] is True
    assert result["content_owner"] == "pao_session_store"
    assert result["bytes"] == len(report.encode("utf-8"))
    assert result["message_id"]
    assert phone.store.get_message(result["message_id"], session_id=phone.session_id)["text"] == report


@pytest.mark.asyncio
async def test_one_partial_read_cannot_certify_entire_query(phone, tmp_path, monkeypatch):
    accepted = phone.store.accept_run(
        session_id=phone.session_id, owner_id=phone.owner_id, agent_id=phone.agent_id,
        request_id="phone-partial-news", text="Read all current news",
        source="session-api", idempotency_key="phone-partial-news",
        expected_context_generation=phone.binding.context_generation,
    )
    report = "\n".join(f"News {number}: details of the story" for number in range(30))
    (tmp_path / "news.txt").write_text(report, encoding="utf-8")
    runtime = SimpleNamespace(workspace_dir=tmp_path, name=phone.agent_id,
                              session_store=phone.store)
    registry = ToolRegistry(allowed_tools=["file_read"], access_root=tmp_path,
                            workspace_dir=tmp_path, secrets={},
                            audit_context={"request_id": accepted.request_id})
    read = await registry.execute("file_read", {"path": "news.txt", "limit": 100}, "partial-read")
    receipt = read.details["effect_receipt"]
    assert receipt["complete_content"] is False
    phone.store.finish_request(accepted.request_id, success=True, assistant_text=report)

    async def overconfident(_runtime, state, **_kwargs):
        assert state["actions"][0]["receipts_omitted"] == 0
        return {"actions": [{"action_id": "news", "verified": True,
                             "evidence_refs": [receipt["evidence_ref"]],
                             "receipt": "All thirty stories verified."}]}

    monkeypatch.setattr(worker_actions, "invoke_phone_judgment", overconfident)
    inspected = await worker_actions.inspect_phone_action_results(
        runtime, accepted.request_id,
        [{"action_id": "news", "kind": "query", "request": "Read all current news"}],
    )
    assert inspected["actions"][0]["status"] == "unknown"
    assert inspected["actions"][0]["evidence_refs"] == []
    assert inspected["tool_observations"][0]["complete_content"] is False
