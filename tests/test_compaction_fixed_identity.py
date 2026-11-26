from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from orchestrator.bridge_memory import BridgeContextAssembler, BridgeMemoryStore
from orchestrator.context_compaction import (
    CAPSULE_FORMAT,
    CompactionStore,
    ContextCompactionCoordinator,
    install_history_section,
)
from orchestrator.her_v2.backend_session import HerBackendSessionCoordinator
from orchestrator.pcm import render_pcm_document
from orchestrator.session_store import SessionStore


def _valid_invoker(calls: list):
    async def invoke(route, request, system_prompt, user_prompt):
        calls.append((route, request, user_prompt))
        payload = {
            "format": CAPSULE_FORMAT,
            "source_segment_ids": list(request.source_segment_ids),
            "source_digest": request.source_digest,
            "active_historical_goals": ["Continue fixed session work"],
            "decisions_and_constraints": [],
            "completed_work_and_verification": [],
            "unresolved_work_questions_failures": [],
            "evidence_refs": [],
            "preferences_and_definitions": [],
            "omissions_and_uncertainty": [],
            "summary": "Compacted historical summary.",
        }
        return SimpleNamespace(text=json.dumps(payload), structured_data=None)

    return invoke


async def _async_empty():
    return []


def _fixed_runtime(tmp_path):
    pcm = tmp_path / "agent.md"
    pcm.write_text(
        render_pcm_document(persona="Persona", system="System", memory=""),
        encoding="utf-8",
    )
    mem = BridgeMemoryStore(tmp_path)
    state_store = SimpleNamespace(
        read=lambda: {},
        update=lambda cb: cb({}),
    )
    manager = SimpleNamespace(
        state_store=state_store,
        agent_mode="fixed",
        privacy_level=1,
        config=SimpleNamespace(
            active_backend="her-v2",
            allowed_backends=[
                {
                    "engine": "deepseek-api",
                    "model": "deepseek-v4-pro",
                    "models": ["deepseek-v4-flash", "deepseek-v4-pro"],
                    "context_window_tokens": 128_000,
                    "response_headroom_tokens": 16_384,
                }
            ],
        ),
        get_her_v2_configuration=lambda: SimpleNamespace(
            provider="deepseek-api",
            fast_provider="deepseek-api",
            fast_model="deepseek-v4-flash",
            pro_model="deepseek-v4-pro",
        ),
    )
    runtime = SimpleNamespace(
        name="fixed-agent",
        workspace_dir=tmp_path,
        config=SimpleNamespace(active_backend="her-v2", system_md=pcm, name="fixed-agent"),
        global_config=SimpleNamespace(bridge_home=tmp_path, timezone="UTC", instance_id="HASHI4"),
        memory_store=mem,
        context_assembler=BridgeContextAssembler(mem, pcm),
        backend_manager=manager,
        _get_active_skill_sections=lambda: [],
        _get_available_skill_catalogue=lambda: [],
        _get_available_tool_catalogue=lambda: [],
        _workzone_prompt_section=lambda: [],
        _request_meta_by_id={},
    )
    runtime._build_pre_turn_context_sections = lambda *_a, **_kw: _async_empty()
    return runtime


def test_timestamp_alone_cannot_remove_an_unverified_exchange():
    pcm = {
        "extra:hashi_managed_conversation_history": {
            "metadata": {"compacted_through_ts": "2026-01-01T00:00:00Z"}
        }
    }
    history = [
        {"request_id": "not-in-capsule", "conversation_at": "2026-01-01T00:00:00Z",
         "user_message": "retain this", "assistant_text": "not compacted"}
    ]
    assert HerBackendSessionCoordinator._uncompacted_history(pcm, history) == history


@pytest.mark.asyncio
async def test_compaction_removes_only_proven_fixed_session_prefix_after_restart(tmp_path):
    pao = SessionStore(tmp_path / "state" / "sessions.sqlite3", instance_id="HASHI4")
    session = pao.create_session(owner_id="user:1", agent_id="fixed-agent")
    workspace = pao.session_workspace(session["session_id"], session["context_generation"])
    runtime = _fixed_runtime(workspace)
    runtime.session_store = pao
    her = HerBackendSessionCoordinator(
        workspace / "backend_state" / "her_v2",
        execution_owner={"owner_id": "test-owner", "owner_pid": 1, "generation_id": "g1"},
    )
    backend = SimpleNamespace(_session_id="her-fixed-1", _session_coordinator=her)
    runtime.backend_manager.current_backend = backend

    def prepare(request_id, message, sections):
        payload = runtime.context_assembler.build_prompt_payload(
            message, "her-v2", extra_sections=sections,
            incremental=her.store.session(backend._session_id) is not None,
        )
        return her.prepare_transport(
            session_id=backend._session_id,
            sections=payload["transport_snapshot"]["sections"],
            resources=[], user_message=message, request_id=request_id,
            message_id=request_id, instance_id="HASHI4", agent_id="fixed-agent",
            owner_id="user:1", hashi_conversation_id=session["session_id"],
            context_generation=session["context_generation"], workzone_identity=str(workspace),
            conversation_at="2026-01-01T00:00:00Z",
        )[0]

    for i in range(12):
        request = f"req-{i}"
        user = f"user fact {i} " + "detail " * 40
        reply = f"assistant fact {i} " + "answer " * 40
        accepted = pao.accept_run(
            session_id=session["session_id"], owner_id="user:1", agent_id="fixed-agent",
            request_id=request, text=user, source="test", idempotency_key=request,
        )
        fence = pao.mark_request_running(request, worker_id="test-worker")
        her_turn = her.accept(prepare(request, user, []))
        her.complete(her_turn, assistant_text=reply)
        pao.finish_request(request, success=True, assistant_text=reply, fencing_token=fence)
        uid = runtime.memory_store.record_turn("user", "test", user)
        aid = runtime.memory_store.record_turn("assistant", "her-v2", reply)
        runtime.memory_store.record_completed_exchange(
            user, reply, "test", assistant_source="her-v2", user_turn_id=uid,
            assistant_turn_id=aid, origin="session",
            origin_ref=f"session:{session['session_id']}:run:{accepted.run_id}",
        )

    compactor = ContextCompactionCoordinator(
        runtime, store=CompactionStore(workspace), memory_store=runtime.memory_store,
        invoker=_valid_invoker([]),
    )
    runtime._context_compaction_test_coordinator = compactor
    outcome = await compactor.compact(trigger="execution_stage_auto", request_ref="compact", force=True)
    assert outcome.status == "completed"
    assert outcome.covered_through_turn_id == 4

    # Reconstruct both owners, as after an Agent restart.
    runtime.session_store = SessionStore(tmp_path / "state" / "sessions.sqlite3", instance_id="HASHI4")
    backend._session_coordinator = her = HerBackendSessionCoordinator(
        workspace / "backend_state" / "her_v2",
        execution_owner={"owner_id": "test-owner", "owner_pid": 1, "generation_id": "g1"},
    )
    sections, _ = install_history_section(
        runtime, [], workspace_dir=workspace, memory_store=runtime.memory_store,
        capsule_only_for_incremental=True,
    )
    next_turn = her.accept(prepare("req-next", "continue", sections))
    assert "Compacted historical summary." in next_turn.materialized_prompt
    assert "user fact 0" not in next_turn.materialized_prompt
    assert "ASSISTANT: assistant fact 1 answer " not in next_turn.materialized_prompt
    assert "user fact 2" in next_turn.materialized_prompt
    assert "assistant fact 11" in next_turn.materialized_prompt
    assert [row["request_id"] for row in next_turn.pcm_input["history"]] == [
        f"req-{i}" for i in range(2, 12)
    ]
    history = her.store.recent_completed_turns(backend._session_id)
    assert len(history) == 12  # Original rows are never deleted.
    metadata = sections[0][2]
    pcm = {"extra:hashi_managed_conversation_history": {"metadata": metadata}}
    altered = [dict(row) for row in history]
    altered[0]["assistant_text"] = "changed after the capsule was made"
    assert HerBackendSessionCoordinator._uncompacted_history(pcm, altered) == altered
    wrong_session = {"extra:hashi_managed_conversation_history": {
        "metadata": {**metadata, "compacted_her_session_id": "other-session"}
    }}
    assert HerBackendSessionCoordinator._uncompacted_history(wrong_session, history) == history

    run = runtime.session_store.get_run_by_request("req-0", owner_id="user:1", agent_id="fixed-agent")
    for owner, agent, generation in (
        ("different-owner", "fixed-agent", session["context_generation"]),
        ("user:1", "different-agent", session["context_generation"]),
        ("user:1", "fixed-agent", session["context_generation"] + 1),
    ):
        assert runtime.session_store.compaction_request_ids(
            session_id=session["session_id"], owner_id=owner, agent_id=agent,
            context_generation=generation, run_ids=[run["run_id"]],
        ) == {}

    her.complete(next_turn, assistant_text="new uncompressed answer")
    later = her.accept(prepare("req-later", "continue again", sections))
    assert [row["request_id"] for row in later.pcm_input["history"]] == [
        *(f"req-{i}" for i in range(2, 12)), "req-next",
    ]
    assert compactor.store.read_state()["generation"] == 1
# HASHI-Fingerprint: hfp_6d4cdc4e0fb247cda35ae96668e9ed33
