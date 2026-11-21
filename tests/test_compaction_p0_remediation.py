from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from orchestrator.context_compaction import (
    CAPSULE_FORMAT,
    ContextCompactionCoordinator,
    install_history_section,
)
from orchestrator.her_v2.backend_session import HerBackendSessionCoordinator
from orchestrator.pcm import render_pcm_document
from orchestrator.bridge_memory import BridgeContextAssembler, BridgeMemoryStore


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


async def _async_empty():
    return []


@pytest.mark.asyncio
async def test_fixed_mode_retains_unverified_history_while_installing_capsule(tmp_path):
    runtime = _fixed_runtime(tmp_path)
    # Seed 12 older turns (beyond recent_exchanges guard) so compaction can summarize prefix.
    for i in range(12):
        runtime.memory_store.record_turn("user", "text", f"Fixed user turn {i} " + ("detail " * 80))
        runtime.memory_store.record_turn("assistant", "her-v2", f"Fixed reply {i} " + ("result " * 80))

    # 1. Fixed mode must no longer skip install_history_section on non-incremental build.
    sections, snapshot = install_history_section(runtime, [])
    assert snapshot is not None
    assert len(sections) == 1
    assert "Fixed reply" in sections[0][1]

    # 2. Simulate HER fixed backend session accumulating historical turns, then committing a capsule.
    session_coord = HerBackendSessionCoordinator(
        tmp_path / "backend_state" / "her_v2",
        execution_owner={"owner_id": "test-owner", "owner_pid": 1, "generation_id": "g1"},
    )
    calls = []
    compactor = ContextCompactionCoordinator(runtime, invoker=_valid_invoker(calls))
    outcome = await compactor.compact(trigger="execution_stage_auto", request_ref="req-inc-2", force=True)
    assert outcome.status == "completed"
    assert outcome.changed is True

    # Mark turn timestamps in bridge_memory to pre-date a later turn in fixed_sessions.
    with sqlite3.connect(runtime.memory_store.db_path) as conn:
        max_ts = conn.execute("SELECT MAX(ts) FROM turns WHERE id <= ?", (outcome.covered_through_turn_id,)).fetchone()[0]

    inc_sections, _ = install_history_section(runtime, [], capsule_only_for_incremental=True)
    assert len(inc_sections) == 1
    assert len(inc_sections[0]) == 2  # No Run identity proves this separate history was compacted.

    class _FixedBackend:
        _session_id = "her-fixed-1"
        persistent_session_busy = False
        capabilities = SimpleNamespace(supports_sessions=True, supports_thinking_stream=True)

        @staticmethod
        def can_resume_fixed_session() -> bool:
            return session_coord.store.session("her-fixed-1") is not None

        def prepare_fixed_turn_input(self, *, prompt_payload, user_message, request_id, request_meta):
            snap = prompt_payload["transport_snapshot"]
            return session_coord.prepare_transport(
                session_id=self._session_id,
                sections=snap["sections"],
                resources=[],
                user_message=user_message,
                request_id=request_id,
                message_id=request_id,
                instance_id="HASHI4",
                agent_id="fixed-agent",
                owner_id="user:1",
                hashi_conversation_id="conv-1",
                context_generation=1,
                workzone_identity=str(tmp_path),
                conversation_at=str(request_meta.get("conversation_at") or ""),
            )

    runtime.backend_manager.current_backend = _FixedBackend()

    # This separate session's timestamp overlaps the capsule but its Run is not covered.
    early_ts = max_ts
    payload1 = runtime.context_assembler.build_prompt_payload("Turn 1 prompt", "her-v2", extra_sections=[])
    enc1, audit1 = runtime.backend_manager.current_backend.prepare_fixed_turn_input(
        prompt_payload=payload1, user_message="Turn 1 prompt", request_id="req-1", request_meta={"conversation_at": early_ts}
    )
    assert audit1["materialized_tokens"] > 0
    acc1 = session_coord.accept(enc1)
    session_coord.complete(acc1, assistant_text="Reply 1 early old history " + ("x" * 600))

    # Append a newer turn AFTER the capsule cutoff.
    later_ts = "2099-01-01T00:00:00+00:00"
    payload2 = runtime.context_assembler.build_prompt_payload("Turn 2 prompt", "her-v2", extra_sections=inc_sections)
    enc2, audit2 = runtime.backend_manager.current_backend.prepare_fixed_turn_input(
        prompt_payload=payload2, user_message="Turn 2 prompt", request_id="req-2", request_meta={"conversation_at": later_ts}
    )
    assert audit2["incremental"] is True
    assert audit2["materialized_tokens"] > 0
    acc2 = session_coord.accept(enc2)
    session_coord.complete(acc2, assistant_text="Reply 2 fresh context")

    # Time is a live PCM section and can change between incremental turns.
    runtime.context_assembler._build_time_fyi = lambda: "Synthetic updated time for turn 3"

    # Both unrelated Turns remain visible; a coincident timestamp is not proof of coverage.
    payload3 = runtime.context_assembler.build_prompt_payload("Turn 3 prompt", "her-v2", extra_sections=inc_sections)
    enc3, audit3 = runtime.backend_manager.current_backend.prepare_fixed_turn_input(
        prompt_payload=payload3, user_message="Turn 3 prompt", request_id="req-3", request_meta={"conversation_at": later_ts}
    )
    third_delta = json.loads(enc3.split("\n", 1)[1])["pcm_delta"]["operations"]
    assert [operation["key"] for operation in third_delta] == ["time"]
    assert "Compacted historical summary." not in enc3  # Capsule already synced in Turn 2.
    acc3 = session_coord.accept(enc3)
    assert acc3.materialized_prompt.count("Compacted historical summary.") == 1
    assert "Turn 2 prompt" in acc3.materialized_prompt
    assert "Reply 1 early old history" in acc3.materialized_prompt
    assert len(session_coord.store.recent_completed_turns("her-fixed-1")) == 2


@pytest.mark.asyncio
async def test_t27_compact_force_bypasses_64k_and_partial_reduction_commits_valid_capsule(tmp_path):
    runtime = _fixed_runtime(tmp_path)
    for i in range(6):
        runtime.memory_store.record_turn("user", "text", f"Short turn {i} " + ("word " * 40))
        runtime.memory_store.record_turn("assistant", "her-v2", f"Short reply {i} " + ("word " * 40))

    runtime._last_full_prompt_tokens = 1200  # Way below 64k manual window
    calls = []
    coordinator = ContextCompactionCoordinator(runtime, invoker=_valid_invoker(calls))

    # 1. Normal manual command without force still respects 64k window.
    below = await coordinator.compact(trigger="manual_command", request_ref="req-no-force", force=False)
    assert below.status == "not_needed"
    assert below.code == "BELOW_MANUAL_COMPACTION_WINDOW"

    # 2. force=True bypasses 64k window AND even with huge required_reduction_tokens,
    # a strictly shrinking valid capsule is NEVER discarded!
    forced = await coordinator.compact(
        trigger="manual_command",
        request_ref="req-force",
        force=True,
    )
    assert forced.status == "completed"
    assert forced.changed is True
    assert coordinator.store.read_state()["generation"] == 1

    # 3. Also verify partial reduction on non-forced auto compaction keeps valid capsule.
    for i in range(14):
        runtime.memory_store.record_turn("user", "text", f"More turn {i} " + ("word " * 60))
        runtime.memory_store.record_turn("assistant", "her-v2", f"More reply {i} " + ("word " * 60))
    partial = await coordinator.compact(
        trigger="execution_stage_auto",
        request_ref="req-partial",
        force=False,
        required_reduction_tokens=999_999,
    )
    assert partial.status == "completed"
    assert partial.changed is True
    assert partial.code == "COMPACTION_PARTIAL_REDUCTION"
    assert coordinator.store.read_state()["generation"] == 2
