from __future__ import annotations

import asyncio
import json
import sqlite3
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from adapters.base import BackendResponse
from adapters.her_v2 import _ExecutionStageCompactionProvider
from orchestrator import runtime_pipeline, runtime_session
from orchestrator.admin_local_testing import execute_local_command
from orchestrator.bridge_memory import BridgeContextAssembler, BridgeMemoryStore
from orchestrator.context_compaction import (
    CAPSULE_FORMAT,
    CONTEXT_PROTECTED_SET_TOO_LARGE,
    DEFAULT_AUTO_COMPACTION_TRIGGER_TOKENS,
    DEFAULT_MANUAL_COMPACTION_MIN_TOKENS,
    DEFAULT_POST_COMPACTION_TARGET_TOKENS,
    DEFAULT_UNKNOWN_COMPACTOR_BUDGET_TOKENS,
    CapacityProfile,
    CompactionFailure,
    CompactionRequest,
    CompactionStore,
    CompactRouteConfig,
    ContextCapacityError,
    ContextCompactionCoordinator,
    ResolvedCompactRoute,
    cancel_runtime_compaction,
    capacity_error_text,
    compact_status_text,
    configure_route,
    ensure_route_state,
    estimate_tokens,
    install_history_section,
    load_policy,
    load_route_config,
    resolve_compact_route,
    resolve_target_capacity,
    resolve_trigger_budget,
    render_history,
    schedule_execution_stage,
)
from orchestrator.her_v2.interfaces import StageInvocationError
from orchestrator.her_v2.wip_journal import WIPJournal
from orchestrator.her_v2.models import Stage, StageRequest
from orchestrator.runtime_pipeline import (
    _typed_capacity_recovery_is_safe,
    recover_typed_context_capacity_rejection,
    request_context_warning_fields,
)


class _StateStore:
    def __init__(self, value=None):
        self.value = deepcopy(value or {})

    def read(self):
        return deepcopy(self.value)

    def update(self, callback):
        candidate = callback(deepcopy(self.value))
        self.value = deepcopy(candidate)
        return deepcopy(self.value)


class _Manager:
    def __init__(self, state_store, *, capacity=1_000_000, headroom=16_384):
        self.state_store = state_store
        self.agent_mode = "flex"
        self.privacy_level = 1
        self.config = SimpleNamespace(
            active_backend="her-v2",
            allowed_backends=[
                {
                    "engine": "deepseek-api",
                    "model": "deepseek-chat",
                    "models": ["deepseek-v4-flash", "deepseek-v4-pro"],
                    "context_window_tokens": capacity,
                    "response_headroom_tokens": headroom,
                    "semantic_reasoning": True,
                },
                {
                    "engine": "openrouter-api",
                    "model": "test/compact-model",
                    "models": ["test/compact-model"],
                    "context_window_tokens": capacity,
                    "response_headroom_tokens": headroom,
                    "semantic_reasoning": True,
                },
            ],
        )
        self._selected = SimpleNamespace(
            provider="deepseek-api",
            fast_model="deepseek-v4-flash",
            pro_model="deepseek-v4-pro",
        )

    def get_her_v2_configuration(self):
        return self._selected

    def _effective_her_v2_config(self):
        return {
            "profiles": {
                "lightweight": {
                    "engine": "deepseek-api",
                    "model": "deepseek-v4-flash",
                    "reasoning": "high",
                },
                "premium": {
                    "engine": "deepseek-api",
                    "model": "deepseek-v4-pro",
                    "reasoning": "high",
                },
            }
        }


class _Runtime:
    def __init__(self, workspace: Path, *, capacity=1_000_000, headroom=16_384):
        self.name = "test-agent"
        self.workspace_dir = workspace
        self.config = SimpleNamespace(active_backend="her-v2")
        self.state_store = _StateStore()
        self.backend_manager = _Manager(
            self.state_store,
            capacity=capacity,
            headroom=headroom,
        )
        self.global_config = SimpleNamespace(her_providers={}, authorized_id=1)
        self.memory_store = SimpleNamespace(
            db_path=workspace / "bridge_memory.sqlite",
            retrieve_memories=lambda *_args, **_kwargs: [],
            get_recent_turns=lambda *_args, **_kwargs: [],
            get_last_user_turn_ts=lambda: None,
        )
        self._busy = False

    def _backend_busy(self):
        return self._busy


def _write_turns(runtime: _Runtime, exchanges: int, *, chars: int = 800) -> None:
    with sqlite3.connect(runtime.memory_store.db_path) as connection:
        connection.execute(
            "CREATE TABLE turns ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, role TEXT, "
            "source TEXT, text TEXT, embedding BLOB)"
        )
        for index in range(exchanges):
            connection.execute(
                "INSERT INTO turns(ts, role, source, text) VALUES(?, ?, ?, ?)",
                (f"2026-08-22T00:{index:02d}:00", "user", "text", f"user-{index}:" + "u" * chars),
            )
            connection.execute(
                "INSERT INTO turns(ts, role, source, text) VALUES(?, ?, ?, ?)",
                (f"2026-08-22T00:{index:02d}:01", "assistant", "her-v2", f"assistant-{index}:" + "a" * chars),
            )
        connection.commit()


def _valid_invoker(calls: list | None = None):
    async def invoke(route, request, system_prompt, user_prompt):
        if calls is not None:
            calls.append((route, request, system_prompt, user_prompt))
        payload = {
            "format": CAPSULE_FORMAT,
            "source_segment_ids": list(request.source_segment_ids),
            "source_digest": request.source_digest,
            "active_historical_goals": [],
            "decisions_and_constraints": [],
            "completed_work_and_verification": [],
            "unresolved_work_questions_failures": [],
            "evidence_refs": [],
            "preferences_and_definitions": [],
            "omissions_and_uncertainty": [],
            "summary": "validated continuity",
        }
        return SimpleNamespace(text=json.dumps(payload), structured_data=None)

    return invoke


def test_recent_history_merges_turns_and_receipts_by_completion_time(tmp_path):
    runtime = _Runtime(tmp_path)
    _write_turns(runtime, 2, chars=1)
    receipt_epoch = datetime.fromisoformat(
        "2026-08-22T00:00:30+10:00"
    ).timestamp()

    sections, snapshot = install_history_section(
        runtime,
        [],
        cross_session_entries=[
            {
                "kind": "cross_session_receipt",
                "receipt_id": "receipt-middle",
                "sequence": 1,
                "completed_at": receipt_epoch,
                "source": "scheduler",
                "status": "completed",
                "task_status": "completed",
                "delivered": True,
                "user_text": "receipt-user",
                "assistant_text": "receipt-assistant",
            }
        ],
    )

    assert snapshot is not None
    rendered = sections[0][1]
    assert rendered.index("user-0:u") < rendered.index("receipt-user")
    assert rendered.index("receipt-user") < rendered.index("user-1:u")
    assert "+10:00 AEST" in rendered
    assert "turn:1 | recorded_at=2026-08-22T00:00:00+10:00 AEST" in rendered
    assert "turn:2 | recorded_at=2026-08-22T00:00:01+10:00 AEST" in rendered
    immediate = rendered.rindex("IMMEDIATE PREVIOUS")
    assert immediate < rendered.rindex("user-1:u")
    assert "CROSS-SESSION TURN RECEIPTS" not in rendered


def test_recent_history_deduplicates_matching_receipt_and_limits_combined_timeline(tmp_path):
    runtime = _Runtime(tmp_path)
    _write_turns(runtime, 2, chars=1)
    snapshot = ContextCompactionCoordinator(runtime).snapshot()
    base_epoch = datetime.fromisoformat("2026-08-22T00:02:00+10:00").timestamp()
    receipts = [
        {
            "kind": "cross_session_receipt",
            "receipt_id": "receipt-duplicate",
            "sequence": 1,
            "completed_at": base_epoch,
            "source": "scheduler",
            "status": "completed",
            "task_status": "completed",
            "delivered": True,
            "user_text": "user-1:u",
            "assistant_text": "assistant-1:a",
        }
    ]
    for index in range(9):
        receipts.append(
            {
                "kind": "cross_session_receipt",
                "receipt_id": f"receipt-{index}",
                "sequence": index + 2,
                "completed_at": base_epoch + index + 1,
                "source": "scheduler",
                "status": "completed",
                "task_status": "completed",
                "delivered": True,
                "user_text": f"receipt-user-{index}",
                "assistant_text": f"receipt-assistant-{index}",
            }
        )

    rendered = render_history(snapshot, cross_session_entries=receipts)
    recent = rendered.split("RECENT CONVERSATION TIMELINE", 1)[1]

    assert rendered.count("user-1:u") == 1
    assert "merged receipts=receipt-duplicate" in rendered
    assert recent.count("\nUSER:\n") == 10
    assert recent.count("IMMEDIATE PREVIOUS") == 1


def test_durable_primary_timeline_is_used_when_working_turns_are_empty(tmp_path):
    runtime = _Runtime(tmp_path)
    _write_turns(runtime, 0, chars=1)
    health_epoch = datetime.fromisoformat(
        "2026-08-24T14:36:00+10:00"
    ).timestamp()
    correction_epoch = datetime.fromisoformat(
        "2026-08-24T16:42:00+10:00"
    ).timestamp()
    canonical = [
        {
            "kind": "primary_exchange",
            "exchange_id": 8,
            "turn_ids": (),
            "sequence": 8,
            "completed_at": correction_epoch,
            "source": "text",
            "user_text": "Where did we just get to?",
            "assistant_text": "I answered from the wrong older topic.",
            "rows": (
                {
                    "id": 0,
                    "ts": "2026-08-24T16:41:00+10:00",
                    "role": "user",
                    "source": "text",
                    "text": "Where did we just get to?",
                },
                {
                    "id": 0,
                    "ts": "2026-08-24T16:42:00+10:00",
                    "role": "assistant",
                    "source": "her-v2",
                    "text": "I answered from the wrong older topic.",
                },
            ),
            "receipt_entries": [],
        },
        {
            "kind": "primary_exchange",
            "exchange_id": 7,
            "turn_ids": (),
            "sequence": 7,
            "completed_at": health_epoch,
            "source": "text",
            "user_text": "Tell me what is in the Health folder",
            "assistant_text": "The Health folder contains five Excel files.",
            "rows": (
                {
                    "id": 0,
                    "ts": "2026-08-24T14:35:00+10:00",
                    "role": "user",
                    "source": "text",
                    "text": "Tell me what is in the Health folder",
                },
                {
                    "id": 0,
                    "ts": "2026-08-24T14:36:00+10:00",
                    "role": "assistant",
                    "source": "her-v2",
                    "text": "The Health folder contains five Excel files.",
                },
            ),
            "receipt_entries": [],
        },
    ]
    runtime.memory_store.get_completed_exchanges = lambda *, limit: canonical[
        -limit:
    ]
    old_receipt_epoch = datetime.fromisoformat(
        "2026-08-22T18:00:00+10:00"
    ).timestamp()

    sections, snapshot = install_history_section(
        runtime,
        [],
        cross_session_entries=[
            {
                "kind": "cross_session_receipt",
                "receipt_id": "old-receipt",
                "sequence": 99,
                "completed_at": old_receipt_epoch,
                "source": "scheduler",
                "status": "completed",
                "task_status": "completed",
                "delivered": True,
                "user_text": "old scheduled task",
                "assistant_text": "old scheduled result",
            }
        ],
    )

    assert snapshot is not None
    assert snapshot.all_turns == ()
    rendered = sections[0][1]
    assert rendered.index("old scheduled task") < rendered.index("Health folder")
    assert rendered.index("Health folder") < rendered.index("wrong older topic")
    immediate_header = next(
        line for line in rendered.splitlines() if "IMMEDIATE PREVIOUS" in line
    )
    assert "primary exchange id=8" in immediate_header
    assert "primary exchange id=7" in rendered
    assert "exchange:7/user" in rendered


def test_durable_timeline_replaces_core_turn_copy_by_shared_turn_ids(tmp_path):
    runtime = _Runtime(tmp_path)
    _write_turns(runtime, 1, chars=1)
    snapshot = ContextCompactionCoordinator(runtime).snapshot()
    completed_at = datetime.fromisoformat(
        "2026-08-22T00:00:02+10:00"
    ).timestamp()

    rendered = render_history(
        snapshot,
        primary_timeline_entries=[
            {
                "kind": "primary_exchange",
                "exchange_id": 1,
                "turn_ids": (1, 2),
                "sequence": 1,
                "completed_at": completed_at,
                "source": "text",
                "user_text": "user-0:u",
                "assistant_text": "user-visible wrapped answer",
                "rows": (
                    {
                        "id": 1,
                        "ts": "2026-08-22T00:00:00+10:00",
                        "role": "user",
                        "source": "text",
                        "text": "user-0:u",
                    },
                    {
                        "id": 2,
                        "ts": "2026-08-22T00:00:02+10:00",
                        "role": "assistant",
                        "source": "her-v2",
                        "text": "user-visible wrapped answer",
                    },
                ),
                "receipt_entries": [],
            }
        ],
    )

    assert rendered.count("user-0:u") == 1
    assert "user-visible wrapped answer" in rendered
    assert "assistant-0:a" not in rendered


def test_default_route_uses_active_quick_model_high_effort_and_tier_2(tmp_path):
    runtime = _Runtime(tmp_path)

    route = resolve_compact_route(runtime)

    assert route.eligible is True
    assert route.provider == "deepseek-api"
    assert route.model == "deepseek-v4-flash"
    assert route.model == runtime.backend_manager.get_her_v2_configuration().fast_model
    assert route.her_effort == "high"
    assert route.reasoning == "enabled"
    assert route.timeout_tier == "tier_2"
    assert route.capacity.context_window_tokens == 1_000_000
    assert resolve_target_capacity(runtime).context_window_tokens == 1_000_000


def test_migration_default_is_persisted_once_without_overwriting_policy(tmp_path):
    runtime = _Runtime(tmp_path)
    runtime.state_store.value = {
        "context_compaction": {"policy": {"recent_exchanges": 7}}
    }

    assert ensure_route_state(runtime) is True
    assert ensure_route_state(runtime) is False
    block = runtime.state_store.value["context_compaction"]
    assert block["version"] == 2
    assert block["route"]["mode"] == "inherit_quick"
    assert block["route"]["reasoning"] == "high"
    assert block["policy"]["recent_exchanges"] == 7


def test_legacy_route_state_migrates_in_memory_to_active_quick_without_rewrite(tmp_path):
    runtime = _Runtime(tmp_path)
    legacy = {
        "version": 1,
        "route": {
            "mode": "explicit",
            "provider": "openrouter-api",
            "model": "test/compact-model",
            "reasoning": "max",
            "timeout_tier": "tier_3",
            "cross_provider_confirmed": True,
        },
    }
    runtime.state_store.value = {"context_compaction": deepcopy(legacy)}

    configured = load_route_config(runtime)
    route = resolve_compact_route(runtime)

    assert configured.mode == "inherit_quick"
    assert configured.provider is None
    assert configured.model is None
    assert configured.reasoning == "high"
    assert configured.timeout_tier == "tier_3"
    assert route.provider == "deepseek-api"
    assert route.model == "deepseek-v4-flash"
    assert runtime.state_store.value["context_compaction"] == legacy


def test_legacy_off_route_cannot_block_active_quick_compaction(tmp_path):
    runtime = _Runtime(tmp_path)
    legacy = {
        "version": 2,
        "route": {
            "mode": "off",
            "timeout_tier": "auto",
        },
    }
    runtime.state_store.value = {"context_compaction": deepcopy(legacy)}

    configured = load_route_config(runtime)
    route = resolve_compact_route(runtime)

    assert configured.mode == "inherit_quick"
    assert route.eligible is True
    assert route.provider == "deepseek-api"
    assert route.model == "deepseek-v4-flash"
    assert route.lock_reason == ""
    assert runtime.state_store.value["context_compaction"] == legacy


def test_explicit_compact_route_is_rejected_and_cannot_create_third_model_path(tmp_path):
    runtime = _Runtime(tmp_path)

    with pytest.raises(ValueError, match="Quick/Light"):
        configure_route(
            runtime,
            mode="explicit",
            provider="openrouter-api",
            model="test/compact-model",
            reasoning="high",
            confirmed_cross_provider=True,
        )

    assert runtime.state_store.value == {}


def test_active_quick_provider_change_is_followed_without_compact_reconfiguration(tmp_path):
    runtime = _Runtime(tmp_path)
    runtime.backend_manager._selected = SimpleNamespace(
        provider="openrouter-api",
        fast_provider="openrouter-api",
        pro_provider="openrouter-api",
        fast_model="test/compact-model",
        pro_model="test/compact-model",
    )

    route = resolve_compact_route(runtime)

    assert route.provider == "openrouter-api"
    assert route.model == "test/compact-model"
    assert route.eligible is True
    assert route.capacity.context_window_tokens == 1_000_000
    assert route.her_effort == "high"


def test_active_quick_model_does_not_require_an_agent_local_model_grant(tmp_path):
    runtime = _Runtime(tmp_path)
    runtime.backend_manager._selected = SimpleNamespace(
        provider="deepseek-api",
        fast_provider="deepseek-api",
        pro_provider="deepseek-api",
        fast_model="not-granted",
        pro_model="deepseek-v4-pro",
    )

    route = resolve_compact_route(runtime)

    assert route.eligible is True
    assert route.provider == "deepseek-api"
    assert route.model == "not-granted"
    assert route.lock_reason == ""


def test_hashi_api_quick_route_is_ready_without_compaction_declarations_or_capacity(
    tmp_path,
):
    runtime = _Runtime(tmp_path)
    runtime.backend_manager._selected = SimpleNamespace(
        provider="hashi-api",
        fast_provider="hashi-api",
        pro_provider="hashi-api",
        fast_model="gpt-5.6-luna",
        pro_model="gpt-5.6-sol",
    )

    route = resolve_compact_route(runtime)

    assert route.eligible is True
    assert route.provider == "hashi-api"
    assert route.model == "gpt-5.6-luna"
    assert route.her_effort == "high"
    assert route.reasoning == "high"
    assert route.capacity is None
    assert route.lock_reason == ""
    assert route.capabilities == {}
    assert all(
        row["engine"] != "hashi-api"
        for row in runtime.backend_manager.config.allowed_backends
    )


@pytest.mark.asyncio
async def test_successful_compaction_keeps_raw_turns_and_commits_once(tmp_path):
    runtime = _Runtime(tmp_path)
    _write_turns(runtime, 12, chars=1200)
    calls = []
    coordinator = ContextCompactionCoordinator(runtime, invoker=_valid_invoker(calls))

    before_rows = sqlite3.connect(runtime.memory_store.db_path).execute(
        "SELECT COUNT(*) FROM turns"
    ).fetchone()[0]
    outcome = await coordinator.compact(
        trigger="test",
        request_ref="req-test",
        force=True,
    )

    assert outcome.status == "completed"
    assert outcome.changed is True
    assert outcome.after_tokens < outcome.before_tokens
    assert calls
    assert all(call[1].model == "deepseek-v4-flash" for call in calls)
    assert all(call[0].her_effort == "high" for call in calls)
    assert all("no tools" in call[2].lower() for call in calls)
    audit_rows = [
        json.loads(line)
        for line in coordinator.store.audit_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    started = next(row["payload"] for row in audit_rows if row["event"] == "started")
    completed = next(row["payload"] for row in audit_rows if row["event"] == "completed")
    for payload in (started, completed):
        assert payload["trigger"] == "test"
        assert payload["compact_provider"] == "deepseek-api"
        assert payload["compact_model"] == "deepseek-v4-flash"
        assert payload["provider_reasoning"] == "enabled"
        assert payload["her_effort"] == "high"
    assert started["tools_authorised"] is False
    assert started["external_side_effects_authorised"] is False
    assert started["sub_agents_authorised"] is False
    assert completed["commit_outcome"] == "committed"
    state = coordinator.store.read_state()
    assert state["generation"] == 1
    record, archive = coordinator.store.load_active(state)
    assert record["covered_through_turn_id"] == outcome.covered_through_turn_id
    assert archive["selected_turns"]
    after_rows = sqlite3.connect(runtime.memory_store.db_path).execute(
        "SELECT COUNT(*) FROM turns"
    ).fetchone()[0]
    assert after_rows == before_rows == 24

    snapshot = coordinator.snapshot()
    assert snapshot.eligible_turns == ()
    assert len(snapshot.recent_turns) == 20
    sections, installed = install_history_section(runtime, [])
    assert installed is not None
    assert len(sections) == 1
    rendered = sections[0][1]
    assert "COMPACTED HISTORY CAPSULE" in rendered
    assert "user-0:" not in rendered
    assert "user-2:" in rendered


@pytest.mark.asyncio
async def test_manual_compact_is_unnecessary_only_below_64k(tmp_path):
    runtime = _Runtime(tmp_path)
    runtime._last_full_prompt_tokens = DEFAULT_MANUAL_COMPACTION_MIN_TOKENS - 1
    calls = []
    coordinator = ContextCompactionCoordinator(
        runtime,
        invoker=_valid_invoker(calls),
    )

    outcome = await coordinator.compact(
        trigger="manual_command",
        request_ref="manual-below-window",
        force=True,
    )

    assert outcome.status == "not_needed"
    assert outcome.code == "BELOW_MANUAL_COMPACTION_WINDOW"
    assert outcome.before_tokens == DEFAULT_MANUAL_COMPACTION_MIN_TOKENS - 1
    assert f"{DEFAULT_MANUAL_COMPACTION_MIN_TOKENS:,}" in outcome.message
    assert calls == []


@pytest.mark.asyncio
async def test_manual_compact_executes_at_64k_without_recent_guard_block(tmp_path):
    runtime = _Runtime(tmp_path)
    _write_turns(runtime, 2, chars=2_000)
    runtime._last_full_prompt_tokens = DEFAULT_MANUAL_COMPACTION_MIN_TOKENS
    calls = []
    coordinator = ContextCompactionCoordinator(
        runtime,
        invoker=_valid_invoker(calls),
    )

    outcome = await coordinator.compact(
        trigger="manual_command",
        request_ref="manual-at-window",
        force=True,
    )

    assert outcome.status == "completed"
    assert outcome.changed is True
    assert calls
    assert coordinator.store.read_state()["generation"] == 1


@pytest.mark.asyncio
async def test_manual_compact_at_64k_reports_no_history_without_guard_claim(tmp_path):
    runtime = _Runtime(tmp_path)
    runtime._last_full_prompt_tokens = DEFAULT_MANUAL_COMPACTION_MIN_TOKENS
    calls = []
    coordinator = ContextCompactionCoordinator(
        runtime,
        invoker=_valid_invoker(calls),
    )

    outcome = await coordinator.compact(
        trigger="manual_command",
        request_ref="manual-no-history",
        force=True,
    )

    assert outcome.status == "not_needed"
    assert outcome.code == "NO_COMPACTABLE_HISTORY"
    assert outcome.before_tokens == DEFAULT_MANUAL_COMPACTION_MIN_TOKENS
    assert (
        outcome.message
        == "No historical conversation content is available to compact."
    )
    assert "recent guard" not in outcome.message
    assert calls == []


@pytest.mark.asyncio
async def test_invalid_capsule_fails_atomically_without_pointer_change(tmp_path):
    runtime = _Runtime(tmp_path)
    _write_turns(runtime, 12, chars=1000)

    async def invalid(*_args):
        return SimpleNamespace(text="{}", structured_data=None)

    coordinator = ContextCompactionCoordinator(runtime, invoker=invalid)
    outcome = await coordinator.compact(
        trigger="test",
        request_ref="req-invalid",
        force=True,
    )

    assert outcome.status == "failed"
    assert outcome.code == "COMPACTION_SCHEMA_INVALID"
    assert coordinator.store.read_state()["generation"] == 0
    assert coordinator.store.read_state()["active_capsule"] is None


@pytest.mark.asyncio
async def test_no_shrink_capsule_aborts_without_pointer_change(tmp_path):
    runtime = _Runtime(tmp_path)
    _write_turns(runtime, 12, chars=300)

    async def no_shrink(route, request, system_prompt, user_prompt):
        response = await _valid_invoker()(route, request, system_prompt, user_prompt)
        payload = json.loads(response.text)
        payload["summary"] = "not-compact:" + "n" * 20_000
        return SimpleNamespace(text=json.dumps(payload), structured_data=None)

    coordinator = ContextCompactionCoordinator(runtime, invoker=no_shrink)
    outcome = await coordinator.compact(
        trigger="test",
        request_ref="req-no-shrink",
        force=True,
    )

    assert outcome.status == "failed"
    assert outcome.code == "COMPACTION_NO_SHRINK"
    assert coordinator.store.read_state()["generation"] == 0


@pytest.mark.asyncio
async def test_required_evidence_reference_cannot_be_dropped(tmp_path):
    runtime = _Runtime(tmp_path)
    _write_turns(runtime, 12, chars=500)
    with sqlite3.connect(runtime.memory_store.db_path) as connection:
        connection.execute(
            "UPDATE turns SET text=text || ? WHERE id=1",
            (" evidence req-proof-123",),
        )
        connection.commit()
    coordinator = ContextCompactionCoordinator(runtime, invoker=_valid_invoker())

    outcome = await coordinator.compact(
        trigger="test",
        request_ref="req-evidence",
        force=True,
    )

    assert outcome.status == "failed"
    assert outcome.code == "COMPACTION_EVIDENCE_MISSING"
    assert coordinator.store.read_state()["generation"] == 0


@pytest.mark.asyncio
async def test_concurrent_compactors_use_cas_and_only_one_pointer_wins(tmp_path):
    runtime = _Runtime(tmp_path)
    _write_turns(runtime, 12, chars=1100)
    entered = 0
    both_entered = asyncio.Event()

    async def racing(route, request, system_prompt, user_prompt):
        nonlocal entered
        entered += 1
        if entered == 2:
            both_entered.set()
        await asyncio.wait_for(both_entered.wait(), timeout=2)
        return await _valid_invoker()(route, request, system_prompt, user_prompt)

    first = ContextCompactionCoordinator(runtime, invoker=racing)
    second = ContextCompactionCoordinator(runtime, invoker=racing)

    results = await asyncio.gather(
        first.compact(trigger="race", request_ref="req-a", force=True),
        second.compact(trigger="race", request_ref="req-b", force=True),
    )

    assert sorted(result.status for result in results) == ["completed", "failed"]
    failed = next(result for result in results if result.status == "failed")
    assert failed.code == "COMPACTION_CAS_LOST"
    assert first.store.read_state()["generation"] == 1


@pytest.mark.asyncio
async def test_turns_appended_during_compaction_remain_uncovered_and_verbatim(tmp_path):
    runtime = _Runtime(tmp_path)
    _write_turns(runtime, 12, chars=700)
    appended = False

    async def append_then_compact(route, request, system_prompt, user_prompt):
        nonlocal appended
        if not appended:
            appended = True
            with sqlite3.connect(runtime.memory_store.db_path) as connection:
                connection.execute(
                    "INSERT INTO turns(ts, role, source, text) VALUES(?, ?, ?, ?)",
                    ("2026-08-22T01:00:00", "user", "text", "concurrent-user-verbatim"),
                )
                connection.execute(
                    "INSERT INTO turns(ts, role, source, text) VALUES(?, ?, ?, ?)",
                    (
                        "2026-08-22T01:00:01",
                        "assistant",
                        "her-v2",
                        "concurrent-assistant-verbatim",
                    ),
                )
                connection.commit()
        return await _valid_invoker()(route, request, system_prompt, user_prompt)

    coordinator = ContextCompactionCoordinator(
        runtime,
        invoker=append_then_compact,
    )
    outcome = await coordinator.compact(
        trigger="append-race",
        request_ref="req-append-race",
        force=True,
    )

    assert outcome.status == "completed"
    snapshot = coordinator.snapshot()
    assert len(snapshot.all_turns) == 26
    assert max(int(row["id"]) for row in snapshot.all_turns) == 26
    assert snapshot.covered_through_turn_id < 25
    sections, _installed = install_history_section(runtime, [])
    rendered = sections[0][1]
    assert "concurrent-user-verbatim" in rendered
    assert "concurrent-assistant-verbatim" in rendered


@pytest.mark.asyncio
async def test_cancellation_reaps_operation_and_leaves_pointer_unchanged(tmp_path):
    runtime = _Runtime(tmp_path)
    _write_turns(runtime, 12, chars=1100)
    entered = asyncio.Event()

    async def blocked(*_args):
        entered.set()
        await asyncio.Event().wait()

    coordinator = ContextCompactionCoordinator(runtime, invoker=blocked)
    task = asyncio.create_task(
        coordinator.compact(
            trigger="cancel-test",
            request_ref="req-cancel",
            force=True,
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=2)

    assert await coordinator.cancel() is True
    with pytest.raises(asyncio.CancelledError):
        await task
    assert coordinator.store.read_state()["generation"] == 0
    audit = coordinator.store.audit_path.read_text(encoding="utf-8")
    assert '"event":"cancelled"' in audit


@pytest.mark.asyncio
async def test_runtime_cancellation_reaps_all_scheduled_compaction_tasks(tmp_path):
    runtime = _Runtime(tmp_path)
    blockers = [
        asyncio.create_task(asyncio.Event().wait()),
        asyncio.create_task(asyncio.Event().wait()),
    ]
    runtime._context_compaction_tasks = set(blockers)

    assert await cancel_runtime_compaction(runtime) is True
    assert all(task.cancelled() for task in blockers)


@pytest.mark.asyncio
async def test_transient_compactor_failure_gets_exactly_one_fresh_attempt(tmp_path):
    runtime = _Runtime(tmp_path)
    _write_turns(runtime, 12, chars=1000)
    requests = []

    async def retry_once(route, request, system_prompt, user_prompt):
        requests.append(request)
        if request.attempt == 1:
            raise CompactionFailure("PROVIDER_SERVER_ERROR", "temporary", retryable=True)
        return await _valid_invoker()(route, request, system_prompt, user_prompt)

    outcome = await ContextCompactionCoordinator(runtime, invoker=retry_once).compact(
        trigger="test",
        request_ref="req-retry",
        force=True,
    )

    assert outcome.status == "completed"
    assert [request.attempt for request in requests] == [1, 2]
    assert [request.deadline_s for request in requests] == [190.0, 300.0]
    assert "deadline_s" not in StageRequest.__dataclass_fields__
    assert "attempt_timeout" not in StageRequest.__dataclass_fields__


@pytest.mark.asyncio
async def test_compactor_deadline_isolated_prompt_tool_free_and_backend_reaped(tmp_path):
    runtime = _Runtime(tmp_path)

    class Backend:
        def __init__(self):
            self.capabilities = SimpleNamespace(supports_tool_use=True)
            self.tool_registry = object()
            self.config = SimpleNamespace(extra={})
            self.sys_prompt = "constructor persona"
            self.shutdown_calls = 0

        async def initialize(self):
            self.sys_prompt = "agent persona loaded during initialize"
            return True

        async def generate_response(self, *_args, **_kwargs):
            await asyncio.Event().wait()

        async def shutdown(self):
            self.shutdown_calls += 1

    backend = Backend()
    runtime.backend_manager.create_ephemeral_backend = lambda *_args, **_kwargs: backend
    coordinator = ContextCompactionCoordinator(runtime)
    route = ResolvedCompactRoute(
        config=CompactRouteConfig(),
        provider="deepseek-api",
        model="deepseek-v4-pro",
        reasoning="max",
        timeout_tier="tier_2",
        capacity=CapacityProfile(
            provider="deepseek-api",
            model="deepseek-v4-pro",
            context_window_tokens=1_000_000,
            provenance="test",
        ),
        eligible=True,
    )
    request = CompactionRequest(
        compaction_id="cmp-timeout",
        request_ref="req-timeout",
        trigger="test",
        provider=route.provider,
        model=route.model,
        reasoning=route.reasoning,
        her_effort=route.her_effort,
        timeout_tier=route.timeout_tier,
        deadline_s=0.01,
        attempt=1,
        source_digest="sha256:test",
        source_segment_ids=("turn:1",),
    )

    with pytest.raises(CompactionFailure) as caught:
        await coordinator._invoke_model(
            route,
            request,
            "isolated compact system",
            "quoted source",
        )

    assert caught.value.code == "COMPACTION_TIMEOUT"
    assert backend.sys_prompt == "isolated compact system"
    assert backend.tool_registry is None
    assert backend.config.extra["her_effort"] == "high"
    assert backend.config.extra["effort"] == "high"
    assert backend.config.extra["provider_reasoning"] == "max"
    assert backend.config.extra["tools_authorised_for_this_stage"] is False
    assert backend.config.extra["external_side_effects_authorised_for_this_stage"] is False
    assert backend.config.extra["sub_agents_authorised_for_this_stage"] is False
    assert backend.shutdown_calls == 1


@pytest.mark.asyncio
async def test_compactor_does_not_require_capability_or_prompt_isolation_declarations(
    tmp_path,
):
    runtime = _Runtime(tmp_path)

    class Backend:
        def __init__(self):
            self.config = SimpleNamespace(extra={})
            self.shutdown_calls = 0

        async def initialize(self):
            return True

        async def generate_response(self, *_args, **_kwargs):
            return BackendResponse(text="ok", duration_ms=1)

        async def shutdown(self):
            self.shutdown_calls += 1

    backend = Backend()
    runtime.backend_manager.create_ephemeral_backend = lambda *_args, **_kwargs: backend
    route = resolve_compact_route(runtime)
    request = CompactionRequest(
        compaction_id="cmp-direct-fast",
        request_ref="req-direct-fast",
        trigger="test",
        provider=route.provider,
        model=route.model,
        reasoning=route.reasoning,
        her_effort=route.her_effort,
        timeout_tier=route.timeout_tier,
        deadline_s=1,
        attempt=1,
        source_digest="sha256:test",
        source_segment_ids=("turn:1",),
    )

    response = await ContextCompactionCoordinator(runtime)._invoke_model(
        route,
        request,
        "direct compact system",
        "quoted source",
    )

    assert response.text == "ok"
    assert backend.sys_prompt == "direct compact system"
    assert backend.config.extra["tools_authorised_for_this_stage"] is False
    assert backend.shutdown_calls == 1


@pytest.mark.asyncio
async def test_compactor_accounting_failure_is_terminal_and_backend_is_reaped(tmp_path):
    runtime = _Runtime(tmp_path)

    class Backend:
        def __init__(self):
            self.config = SimpleNamespace(extra={})
            self.shutdown_calls = 0

        async def initialize(self):
            return True

        async def generate_response(self, *_args, **_kwargs):
            return BackendResponse(text="ok", duration_ms=1)

        async def shutdown(self):
            self.shutdown_calls += 1

    backend = Backend()

    def reject_usage(_rows):
        raise RuntimeError("durable usage store unavailable")

    runtime.backend_manager.current_backend = SimpleNamespace(
        record_maintenance_provider_requests=reject_usage,
    )
    runtime.backend_manager.create_ephemeral_backend = lambda *_args, **_kwargs: backend
    route = resolve_compact_route(runtime)
    request = CompactionRequest(
        compaction_id="cmp-accounting-failure",
        request_ref="req-accounting-failure",
        trigger="test",
        provider=route.provider,
        model=route.model,
        reasoning=route.reasoning,
        her_effort=route.her_effort,
        timeout_tier=route.timeout_tier,
        deadline_s=1,
        attempt=1,
        source_digest="sha256:test",
        source_segment_ids=("turn:1",),
    )

    with pytest.raises(CompactionFailure) as caught:
        await ContextCompactionCoordinator(runtime)._invoke_model(
            route,
            request,
            "direct compact system",
            "quoted source",
        )

    assert caught.value.code == "COMPACTION_ACCOUNTING_FAILURE"
    assert caught.value.retryable is False
    assert backend.shutdown_calls == 1


@pytest.mark.asyncio
async def test_compactor_records_provider_exception_without_response(tmp_path):
    runtime = _Runtime(tmp_path)
    recorded = []

    class Backend:
        def __init__(self):
            self.config = SimpleNamespace(extra={})
            self.shutdown_calls = 0

        async def initialize(self):
            return True

        async def generate_response(self, *_args, **_kwargs):
            raise RuntimeError("provider transport failed")

        async def shutdown(self):
            self.shutdown_calls += 1

    backend = Backend()
    runtime.backend_manager.current_backend = SimpleNamespace(
        record_maintenance_provider_requests=lambda rows: recorded.extend(rows),
        can_record_maintenance_provider_requests=lambda: True,
    )
    runtime.backend_manager.create_ephemeral_backend = lambda *_args, **_kwargs: backend
    route = resolve_compact_route(runtime)
    request = CompactionRequest(
        compaction_id="cmp-provider-exception",
        request_ref="req-provider-exception",
        trigger="test",
        provider=route.provider,
        model=route.model,
        reasoning=route.reasoning,
        her_effort=route.her_effort,
        timeout_tier=route.timeout_tier,
        deadline_s=1,
        attempt=1,
        source_digest="sha256:test",
        source_segment_ids=("turn:1",),
    )

    with pytest.raises(RuntimeError, match="provider transport failed"):
        await ContextCompactionCoordinator(runtime)._invoke_model(
            route,
            request,
            "direct compact system",
            "quoted source",
        )

    assert len(recorded) == 1
    assert recorded[0]["status"] == "failed_without_receipt"
    assert recorded[0]["token_source"] == "unknown"
    assert backend.shutdown_calls == 1


@pytest.mark.asyncio
async def test_compactor_records_physical_retry_immediately_without_final_duplicates(
    tmp_path,
):
    runtime = _Runtime(tmp_path)
    recorded = []
    physical_calls = [
        {
            "provider_request_id": "compact-wire-1",
            "input": 0,
            "output": 0,
            "thinking": 0,
            "token_source": "unknown",
            "cost_usd": None,
            "attempt": 1,
            "retry_count": 0,
            "recovery_kind": "none",
            "status": "failed_without_receipt",
        },
        {
            "provider_request_id": "compact-wire-2",
            "input": 90,
            "output": 10,
            "thinking": 2,
            "token_source": "provider",
            "thinking_in_output": True,
            "cost_usd": 0.02,
            "attempt": 2,
            "retry_count": 1,
            "recovery_kind": "provider_transport_retry",
            "status": "completed",
        },
    ]

    class Backend:
        def __init__(self):
            self.config = SimpleNamespace(extra={})
            self.shutdown_calls = 0
            self.observer = None

        def set_provider_call_observer(self, observer):
            self.observer = observer

        async def initialize(self):
            return True

        async def generate_response(self, *_args, **_kwargs):
            self.observer(physical_calls[0])
            assert len(recorded) == 1
            self.observer(physical_calls[1])
            assert len(recorded) == 2
            return BackendResponse(
                text="ok",
                duration_ms=1,
                stream_metadata={"meter": {"provider_calls": physical_calls}},
            )

        async def shutdown(self):
            self.shutdown_calls += 1

    backend = Backend()
    runtime.backend_manager.current_backend = SimpleNamespace(
        record_maintenance_provider_requests=lambda rows: recorded.extend(rows),
        can_record_maintenance_provider_requests=lambda: True,
    )
    runtime.backend_manager.create_ephemeral_backend = lambda *_args, **_kwargs: backend
    route = resolve_compact_route(runtime)
    request = CompactionRequest(
        compaction_id="cmp-physical-retry",
        request_ref="req-physical-retry",
        trigger="test",
        provider=route.provider,
        model=route.model,
        reasoning=route.reasoning,
        her_effort=route.her_effort,
        timeout_tier=route.timeout_tier,
        deadline_s=1,
        attempt=1,
        source_digest="sha256:test",
        source_segment_ids=("turn:1",),
    )

    response = await ContextCompactionCoordinator(runtime)._invoke_model(
        route,
        request,
        "direct compact system",
        "quoted source",
    )

    assert response.text == "ok"
    assert [row["provider_request_id"] for row in recorded] == [
        "compact-wire-1",
        "compact-wire-2",
    ]
    assert [row["status"] for row in recorded] == [
        "failed_without_receipt",
        "completed",
    ]
    assert recorded[0]["cost_source"] == "unknown"
    assert backend.shutdown_calls == 1


def test_compactor_does_not_invent_call_from_explicit_empty_meter(tmp_path):
    runtime = _Runtime(tmp_path)
    recorded = []
    runtime.backend_manager.current_backend = SimpleNamespace(
        record_maintenance_provider_requests=lambda rows: recorded.extend(rows),
    )
    route = resolve_compact_route(runtime)
    request = CompactionRequest(
        compaction_id="cmp-before-http",
        request_ref="req-before-http",
        trigger="test",
        provider=route.provider,
        model=route.model,
        reasoning=route.reasoning,
        her_effort=route.her_effort,
        timeout_tier=route.timeout_tier,
        deadline_s=1,
        attempt=1,
        source_digest="sha256:test",
        source_segment_ids=("turn:1",),
    )

    ContextCompactionCoordinator(runtime)._record_provider_usage(
        route,
        request,
        BackendResponse(
            text="",
            duration_ms=1,
            is_success=False,
            stream_metadata={"meter": {"provider_calls": []}},
        ),
        status="failed_response",
    )

    assert recorded == []


@pytest.mark.asyncio
async def test_compactor_blocks_before_provider_when_durable_meter_is_unavailable(
    tmp_path,
):
    runtime = _Runtime(tmp_path)
    created = []
    runtime.backend_manager.current_backend = SimpleNamespace(
        record_maintenance_provider_requests=lambda _rows: None,
        can_record_maintenance_provider_requests=lambda: False,
    )
    runtime.backend_manager.create_ephemeral_backend = lambda *_args, **_kwargs: created.append(True)
    route = resolve_compact_route(runtime)
    request = CompactionRequest(
        compaction_id="cmp-no-meter",
        request_ref="req-no-meter",
        trigger="test",
        provider=route.provider,
        model=route.model,
        reasoning=route.reasoning,
        her_effort=route.her_effort,
        timeout_tier=route.timeout_tier,
        deadline_s=1,
        attempt=1,
        source_digest="sha256:test",
        source_segment_ids=("turn:1",),
    )

    with pytest.raises(CompactionFailure) as caught:
        await ContextCompactionCoordinator(runtime)._invoke_model(
            route,
            request,
            "direct compact system",
            "quoted source",
        )

    assert caught.value.code == "COMPACTION_ACCOUNTING_UNAVAILABLE"
    assert caught.value.retryable is False
    assert created == []


@pytest.mark.asyncio
async def test_hierarchical_compaction_uses_multiple_calls_without_fixed_round_cap(tmp_path):
    runtime = _Runtime(tmp_path, capacity=20_000, headroom=512)
    runtime.state_store.value = {
        "context_compaction": {"policy": {"recent_exchanges": 1}}
    }
    _write_turns(runtime, 26, chars=1800)
    calls = []

    outcome = await ContextCompactionCoordinator(
        runtime,
        invoker=_valid_invoker(calls),
    ).compact(
        trigger="test",
        request_ref="req-hierarchy",
        force=True,
    )

    assert outcome.status == "completed"
    assert len(calls) > 1
    assert outcome.selected_segment_count == 50


@pytest.mark.asyncio
async def test_oversized_record_is_paged_then_committed_with_original_source_ids(tmp_path):
    runtime = _Runtime(tmp_path, capacity=20_000, headroom=512)
    runtime.state_store.value = {
        "context_compaction": {"policy": {"recent_exchanges": 1}}
    }
    _write_turns(runtime, 3, chars=100)
    with sqlite3.connect(runtime.memory_store.db_path) as connection:
        connection.execute(
            "UPDATE turns SET text=? WHERE id=1",
            ("oversized:" + "z" * 100_000,),
        )
        connection.commit()
    calls = []
    coordinator = ContextCompactionCoordinator(
        runtime,
        invoker=_valid_invoker(calls),
    )

    outcome = await coordinator.compact(
        trigger="oversized",
        request_ref="req-oversized",
        force=True,
    )

    assert outcome.status == "completed"
    assert len(calls) > 2
    record, _archive = coordinator.store.load_active()
    assert record["capsule"]["source_segment_ids"] == [
        "turn:1",
        "turn:2",
        "turn:3",
        "turn:4",
    ]
    assert list(record["source_hashes"]) == ["turn:1", "turn:2", "turn:3", "turn:4"]


def test_compaction_window_is_fixed_when_target_capacity_is_unknown(tmp_path):
    runtime = _Runtime(tmp_path)
    for grant in runtime.backend_manager.config.allowed_backends:
        grant.pop("context_window_tokens", None)
    runtime.backend_manager._selected = SimpleNamespace(
        provider="openrouter-api",
        fast_model="test/compact-model",
        pro_model="test/compact-model",
    )
    runtime.backend_manager._effective_her_v2_config = lambda: {
        "profiles": {
            "only": {
                "engine": "openrouter-api",
                "model": "test/compact-model",
                "reasoning": "high",
            }
        }
    }

    assert resolve_target_capacity(runtime) is None
    route = resolve_compact_route(runtime)
    assert route.eligible is True
    assert route.capacity is None
    assert route.lock_reason == ""
    budget = resolve_trigger_budget(runtime)
    assert budget.target is None
    assert budget.is_unknown_capacity_guard is True
    assert budget.high_projected_tokens == DEFAULT_AUTO_COMPACTION_TRIGGER_TOKENS
    assert budget.low_input_tokens == DEFAULT_POST_COMPACTION_TARGET_TOKENS
    assert budget.provenance == "hashi_compaction_window_64k_128k_v1"
    assert DEFAULT_UNKNOWN_COMPACTOR_BUDGET_TOKENS == 32_000


def test_declared_provider_capacity_does_not_move_64k_128k_window(tmp_path):
    runtime = _Runtime(tmp_path, capacity=1_000_000, headroom=16_384)

    budget = resolve_trigger_budget(runtime)

    assert budget.target is not None
    assert budget.target.context_window_tokens == 1_000_000
    assert budget.high_projected_tokens == DEFAULT_AUTO_COMPACTION_TRIGGER_TOKENS
    assert budget.low_input_tokens == DEFAULT_POST_COMPACTION_TARGET_TOKENS
    assert budget.response_headroom_tokens == 0


def test_persisted_threshold_fields_cannot_move_fixed_window(tmp_path):
    runtime = _Runtime(tmp_path)
    runtime.state_store.value = {
        "context_compaction": {
            "policy": {
                "manual_min_tokens": 12_000,
                "auto_trigger_tokens": 24_000,
                "post_compaction_target_tokens": 8_000,
                "unknown_target_high_tokens": 32_000,
                "unknown_target_low_tokens": 16_000,
            }
        }
    }

    policy = load_policy(runtime)

    assert policy.manual_min_tokens == DEFAULT_MANUAL_COMPACTION_MIN_TOKENS
    assert policy.auto_trigger_tokens == DEFAULT_AUTO_COMPACTION_TRIGGER_TOKENS
    assert (
        policy.post_compaction_target_tokens
        == DEFAULT_POST_COMPACTION_TARGET_TOKENS
    )


def test_capacity_error_rendering_is_stable_and_escaped():
    error = ContextCapacityError(
        CONTEXT_PROTECTED_SET_TOO_LARGE,
        "unsafe <value>",
        facts={"provider": "x<y", "protected_tokens": 123},
    )

    rendered = capacity_error_text(error)

    assert CONTEXT_PROTECTED_SET_TOO_LARGE in rendered
    assert "unsafe &lt;value&gt;" in rendered
    assert "x&lt;y" in rendered
    assert "blocked" not in rendered.lower()
    assert "continuing" in rendered


@pytest.mark.asyncio
async def test_build_turn_prompt_never_compacts_or_warns_before_execution(
    tmp_path,
    monkeypatch,
):
    runtime = _Runtime(tmp_path, capacity=1_000, headroom=100)
    runtime.backend_manager.current_backend = SimpleNamespace(
        _session_id=None,
        tool_registry=None,
        capabilities=SimpleNamespace(supports_sessions=False),
    )
    runtime.context_assembler = BridgeContextAssembler(runtime.memory_store, None)
    runtime._consume_session_primer = lambda item: item.prompt
    runtime._workzone_prompt_section = lambda: []

    async def pre_turn(*_args, **_kwargs):
        return []

    runtime._build_pre_turn_context_sections = pre_turn
    runtime.current_request_meta = {}
    runtime._last_prompt_audit = {}
    runtime._thinking_chars_this_req = 0
    runtime._last_full_prompt_tokens = 0
    monkeypatch.setattr(
        runtime_pipeline.runtime_cross_session,
        "prepare_reply_binding",
        lambda _runtime, _item, prompt: prompt,
    )
    monkeypatch.setattr(
        runtime_pipeline.runtime_cross_session,
        "timeline_entries",
        lambda _runtime, _item: [
            {
                "kind": "cross_session_receipt",
                "receipt_id": "receipt-pipeline",
                "sequence": 1,
                "completed_at": datetime.fromisoformat(
                    "2026-08-24T12:00:00+10:00"
                ).timestamp(),
                "source": "scheduler",
                "status": "completed",
                "task_status": "completed",
                "delivered": True,
                "user_text": "scheduled task",
                "assistant_text": "scheduled result",
            }
        ],
    )
    monkeypatch.setattr(
        runtime_pipeline.runtime_retry,
        "prepare_interrupted_task_continuation",
        lambda _runtime, _item, prompt, **_kwargs: prompt,
    )
    monkeypatch.setattr(runtime_pipeline, "is_memory_plus_enabled", lambda _path: False)
    item = SimpleNamespace(
        request_id="req-protected",
        prompt="CURRENT-AUTHORITY:" + "x" * 10_000,
        source="text",
        silent=False,
        skip_memory_injection=False,
    )

    result = await runtime_pipeline.build_turn_prompt(
        runtime,
        item,
        is_bridge_request=False,
    )

    assert "CURRENT-AUTHORITY:" in result.final_prompt
    assert "receipt-pipeline" in result.final_prompt
    assert "IMMEDIATE PREVIOUS" in result.final_prompt
    assert "CROSS-SESSION TURN RECEIPTS" not in result.final_prompt
    assert result.context_warnings == ()
    assert runtime._context_compaction_prompt_tokens[item.request_id] > 0


@pytest.mark.asyncio
async def test_build_turn_prompt_only_measures_context_before_execution(
    tmp_path,
    monkeypatch,
):
    runtime = _Runtime(tmp_path, capacity=20_000, headroom=512)
    runtime.state_store.value = {
        "context_compaction": {"policy": {"recent_exchanges": 1}}
    }
    _write_turns(runtime, 12, chars=4_000)
    runtime.backend_manager.current_backend = SimpleNamespace(
        _session_id=None,
        tool_registry=None,
        capabilities=SimpleNamespace(supports_sessions=False),
    )
    runtime.context_assembler = BridgeContextAssembler(runtime.memory_store, None)
    compact_calls = []
    runtime._context_compaction_coordinator = ContextCompactionCoordinator(
        runtime,
        invoker=_valid_invoker(compact_calls),
    )
    runtime._consume_session_primer = lambda item: item.prompt
    runtime._workzone_prompt_section = lambda: []

    async def pre_turn(*_args, **_kwargs):
        return []

    runtime._build_pre_turn_context_sections = pre_turn
    runtime.current_request_meta = {}
    runtime._last_prompt_audit = {}
    runtime._thinking_chars_this_req = 0
    runtime._last_full_prompt_tokens = 0
    monkeypatch.setattr(
        runtime_pipeline.runtime_cross_session,
        "prepare_reply_binding",
        lambda _runtime, _item, prompt: prompt,
    )
    monkeypatch.setattr(
        runtime_pipeline.runtime_retry,
        "prepare_interrupted_task_continuation",
        lambda _runtime, _item, prompt, **_kwargs: prompt,
    )
    monkeypatch.setattr(runtime_pipeline, "is_memory_plus_enabled", lambda _path: False)
    item = SimpleNamespace(
        request_id="req-auto",
        prompt="CURRENT-AUTHORITY-MUST-SURVIVE",
        source="text",
        silent=False,
        skip_memory_injection=False,
    )

    result = await runtime_pipeline.build_turn_prompt(
        runtime,
        item,
        is_bridge_request=False,
    )

    assert "CURRENT-AUTHORITY-MUST-SURVIVE" in result.final_prompt
    assert "COMPACTED HISTORY CAPSULE" not in result.final_prompt
    assert "user-0:" in result.final_prompt
    assert "user-11:" in result.final_prompt
    assert compact_calls == []
    assert runtime._context_compaction_coordinator.store.read_state()["generation"] == 0
    assert runtime._last_prompt_audit["budget_applied"] is False


@pytest.mark.asyncio
async def test_unknown_capacity_over_128k_compacts_before_triage(
    tmp_path,
    monkeypatch,
):
    runtime = _Runtime(tmp_path)
    for grant in runtime.backend_manager.config.allowed_backends:
        grant.pop("context_window_tokens", None)
        grant.pop("response_headroom_tokens", None)
    runtime.backend_manager._selected = SimpleNamespace(
        provider="openrouter-api",
        fast_model="test/compact-model",
        pro_model="test/compact-model",
    )
    runtime.backend_manager._effective_her_v2_config = lambda: {
        "profiles": {
            "only": {
                "engine": "openrouter-api",
                "model": "test/compact-model",
                "reasoning": "high",
            }
        }
    }
    runtime.state_store.value = {
        "context_compaction": {"policy": {"recent_exchanges": 1}}
    }
    _write_turns(runtime, 14, chars=20_000)
    runtime.backend_manager.current_backend = SimpleNamespace(
        _session_id=None,
        tool_registry=None,
        capabilities=SimpleNamespace(supports_sessions=False),
    )
    runtime.context_assembler = BridgeContextAssembler(runtime.memory_store, None)
    calls = []
    runtime._context_compaction_coordinator = ContextCompactionCoordinator(
        runtime,
        invoker=_valid_invoker(calls),
    )
    runtime._consume_session_primer = lambda item: item.prompt
    runtime._workzone_prompt_section = lambda: []

    async def pre_turn(*_args, **_kwargs):
        return []

    runtime._build_pre_turn_context_sections = pre_turn
    runtime.current_request_meta = {}
    runtime._last_prompt_audit = {}
    runtime._thinking_chars_this_req = 0
    runtime._last_full_prompt_tokens = 0
    monkeypatch.setattr(
        runtime_pipeline.runtime_cross_session,
        "prepare_reply_binding",
        lambda _runtime, _item, prompt: prompt,
    )
    monkeypatch.setattr(
        runtime_pipeline.runtime_retry,
        "prepare_interrupted_task_continuation",
        lambda _runtime, _item, prompt, **_kwargs: prompt,
    )
    monkeypatch.setattr(runtime_pipeline, "is_memory_plus_enabled", lambda _path: False)
    item = SimpleNamespace(
        request_id="req-unknown-auto",
        prompt="CURRENT-UNKNOWN-CAPACITY-AUTHORITY",
        source="text",
        silent=False,
        skip_memory_injection=False,
    )

    result = await runtime_pipeline.build_turn_prompt(
        runtime,
        item,
        is_bridge_request=False,
    )

    assert "CURRENT-UNKNOWN-CAPACITY-AUTHORITY" in result.final_prompt
    assert result.final_prompt.count("CURRENT-UNKNOWN-CAPACITY-AUTHORITY") == 1
    assert "COMPACTED HISTORY CAPSULE" in result.final_prompt
    assert calls
    assert runtime._context_compaction_coordinator.store.read_state()["generation"] == 1
    assert (
        resolve_trigger_budget(runtime).is_unknown_capacity_guard
        is True
    )
    assert estimate_tokens(result.final_prompt) < DEFAULT_AUTO_COMPACTION_TRIGGER_TOKENS


@pytest.mark.asyncio
async def test_compactor_is_not_called_during_prompt_assembly(
    tmp_path,
    monkeypatch,
):
    runtime = _Runtime(tmp_path)
    for grant in runtime.backend_manager.config.allowed_backends:
        grant.pop("context_window_tokens", None)
        grant.pop("response_headroom_tokens", None)
    runtime.backend_manager._selected = SimpleNamespace(
        provider="openrouter-api",
        fast_model="test/compact-model",
        pro_model="test/compact-model",
    )
    runtime.backend_manager._effective_her_v2_config = lambda: {
        "profiles": {
            "only": {
                "engine": "openrouter-api",
                "model": "test/compact-model",
                "reasoning": "high",
            }
        }
    }
    runtime.state_store.value = {
        "context_compaction": {"policy": {"recent_exchanges": 1}}
    }
    _write_turns(runtime, 14, chars=12_000)
    runtime.backend_manager.current_backend = SimpleNamespace(
        _session_id=None,
        tool_registry=None,
        capabilities=SimpleNamespace(supports_sessions=False),
    )
    runtime.context_assembler = BridgeContextAssembler(runtime.memory_store, None)

    compact_calls = []

    async def invalid(*args):
        compact_calls.append(args)
        return SimpleNamespace(text="{}", structured_data=None)

    runtime._context_compaction_coordinator = ContextCompactionCoordinator(
        runtime,
        invoker=invalid,
    )
    runtime._consume_session_primer = lambda item: item.prompt
    runtime._workzone_prompt_section = lambda: []

    async def pre_turn(*_args, **_kwargs):
        return []

    runtime._build_pre_turn_context_sections = pre_turn
    runtime.current_request_meta = {}
    runtime._last_prompt_audit = {}
    runtime._thinking_chars_this_req = 0
    runtime._last_full_prompt_tokens = 0
    monkeypatch.setattr(
        runtime_pipeline.runtime_cross_session,
        "prepare_reply_binding",
        lambda _runtime, _item, prompt: prompt,
    )
    monkeypatch.setattr(
        runtime_pipeline.runtime_retry,
        "prepare_interrupted_task_continuation",
        lambda _runtime, _item, prompt, **_kwargs: prompt,
    )
    monkeypatch.setattr(runtime_pipeline, "is_memory_plus_enabled", lambda _path: False)
    item = SimpleNamespace(
        request_id="req-unknown-continues",
        prompt="CURRENT-MUST-REACH-HER-UNCOMPACTED",
        source="text",
        silent=False,
        skip_memory_injection=False,
    )

    result = await runtime_pipeline.build_turn_prompt(
        runtime,
        item,
        is_bridge_request=False,
    )

    assert "CURRENT-MUST-REACH-HER-UNCOMPACTED" in result.final_prompt
    assert "user-0:" in result.final_prompt
    assert result.context_warnings == ()
    assert compact_calls == []
    assert runtime._context_compaction_coordinator.store.read_state()["generation"] == 0


@pytest.mark.asyncio
async def test_execution_stage_retry_exhaustion_warns_and_does_not_block_model(
    tmp_path,
    monkeypatch,
):
    runtime = _Runtime(tmp_path)
    for grant in runtime.backend_manager.config.allowed_backends:
        grant.pop("context_window_tokens", None)
        grant.pop("response_headroom_tokens", None)
    runtime.backend_manager._selected = SimpleNamespace(
        provider="openrouter-api",
        fast_model="test/compact-model",
        pro_model="test/compact-model",
    )
    runtime.backend_manager._effective_her_v2_config = lambda: {
        "profiles": {
            "only": {
                "engine": "openrouter-api",
                "model": "test/compact-model",
                "reasoning": "high",
            }
        }
    }
    runtime.state_store.value = {
        "context_compaction": {
            "policy": {
                "recent_exchanges": 1,
            }
        }
    }
    _write_turns(runtime, 14, chars=20_000)
    runtime.backend_manager.current_backend = SimpleNamespace(
        _session_id=None,
        tool_registry=None,
        capabilities=SimpleNamespace(supports_sessions=False),
    )
    runtime.context_assembler = BridgeContextAssembler(runtime.memory_store, None)
    compact_calls = []

    async def always_transient(*args):
        compact_calls.append(args)
        raise CompactionFailure(
            "PROVIDER_SERVER_ERROR",
            "temporary compactor failure",
            retryable=True,
        )

    runtime._context_compaction_coordinator = ContextCompactionCoordinator(
        runtime,
        invoker=always_transient,
    )
    runtime._consume_session_primer = lambda item: item.prompt
    runtime._workzone_prompt_section = lambda: []

    async def pre_turn(*_args, **_kwargs):
        return []

    runtime._build_pre_turn_context_sections = pre_turn
    runtime.current_request_meta = {}
    runtime._last_prompt_audit = {}
    runtime._thinking_chars_this_req = 0
    runtime._last_full_prompt_tokens = 0
    runtime._verbose = False
    sent = []

    async def send_long_message(chat_id, text, **kwargs):
        sent.append((chat_id, text, kwargs))
        return 0.0, 1

    runtime.send_long_message = send_long_message
    monkeypatch.setattr(
        runtime_pipeline.runtime_cross_session,
        "prepare_reply_binding",
        lambda _runtime, _item, prompt: prompt,
    )
    monkeypatch.setattr(
        runtime_pipeline.runtime_retry,
        "prepare_interrupted_task_continuation",
        lambda _runtime, _item, prompt, **_kwargs: prompt,
    )
    monkeypatch.setattr(runtime_pipeline, "is_memory_plus_enabled", lambda _path: False)
    item = SimpleNamespace(
        request_id="req-120k-retry-exhausted",
        prompt="CURRENT-REQUEST-MUST-CONTINUE-AFTER-RETRIES",
        source="text",
        silent=False,
        is_retry=False,
        deliver_to_telegram=True,
        chat_id=123,
        skip_memory_injection=False,
    )

    result = await runtime_pipeline.build_turn_prompt(
        runtime,
        item,
        is_bridge_request=False,
    )

    assert estimate_tokens(result.final_prompt) > DEFAULT_AUTO_COMPACTION_TRIGGER_TOKENS
    assert len(compact_calls) == 2
    assert "CURRENT-REQUEST-MUST-CONTINUE-AFTER-RETRIES" in result.final_prompt
    assert "user-0:" in result.final_prompt
    assert result.context_warnings == ()

    scheduled = schedule_execution_stage(
        runtime,
        request_ref=item.request_id,
        prompt_tokens=runtime._context_compaction_prompt_tokens[item.request_id],
        chat_id=item.chat_id,
        deliver_to_telegram=True,
    )
    assert scheduled is True
    assert len(compact_calls) == 2

    model_calls = []

    async def generate(prompt, request_id, **kwargs):
        model_calls.append((prompt, request_id, kwargs))
        return BackendResponse(text="continued", duration_ms=1, is_success=True)

    runtime.backend_manager.generate_response = generate
    runtime.config.extra = {}
    runtime._think = False
    runtime.is_generating = True
    generation = await runtime_pipeline.run_backend_generation(
        runtime,
        item,
        result.final_prompt,
        on_stream_event=None,
        audit_active=False,
    )

    assert generation.response.is_success is True
    assert len(model_calls) == 1
    assert model_calls[0][0] == result.final_prompt
    await asyncio.gather(*tuple(runtime._context_compaction_tasks))
    assert len(compact_calls) == 4
    assert sent
    assert "warning" in sent[0][1].lower()
    assert "continued without waiting" in sent[0][1]

    audit_rows = [
        json.loads(line)
        for line in runtime._context_compaction_coordinator.store.audit_path.read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    failed = [row for row in audit_rows if row["event"] == "failed"][-1]
    assert failed["payload"]["will_continue"] is True
    assert (
        failed["payload"]["continuation_decision"]
        == "continue_original_request_with_warning"
    )


@pytest.mark.asyncio
async def test_context_compaction_warning_delivery_is_mandatory_and_nonblocking():
    sent = []
    runtime = SimpleNamespace(
        _request_meta_by_id={"req-warning": {"request_id": "req-warning"}},
        current_request_meta={"request_id": "req-warning"},
        _background_tasks=set(),
        _verbose=False,
        request_activity=None,
        logger=SimpleNamespace(warning=lambda *_args, **_kwargs: None),
        error_logger=SimpleNamespace(warning=lambda *_args, **_kwargs: None),
    )

    async def send_long_message(chat_id, text, **kwargs):
        sent.append((chat_id, text, kwargs))
        return 0.0, 1

    runtime.send_long_message = send_long_message
    item = SimpleNamespace(
        request_id="req-warning",
        chat_id=123,
        deliver_to_telegram=True,
    )

    runtime_pipeline.surface_context_compaction_warnings(
        runtime,
        item,
        ("⚠️ required compaction warning · request continuing",),
    )
    tasks = tuple(runtime._background_tasks)

    assert tasks
    assert all(not task.done() for task in tasks)
    assert runtime._request_meta_by_id["req-warning"][
        "context_compaction_warnings"
    ]
    await asyncio.gather(*tasks)
    assert sent == [
        (
            123,
            "⚠️ required compaction warning · request continuing",
                {
                    "request_id": "req-warning",
                    "purpose": "context-compaction-warning",
                    "parse_mode": "HTML",
                },
        )
    ]


@pytest.mark.asyncio
async def test_wip_recovery_warning_is_visible_with_verbose_off_and_dedicated_metadata():
    sent = []
    runtime = SimpleNamespace(
        _request_meta_by_id={"req-warning": {"request_id": "req-warning"}},
        current_request_meta={"request_id": "req-warning"},
        _background_tasks=set(),
        _verbose=False,
        request_activity=None,
        logger=SimpleNamespace(warning=lambda *_args, **_kwargs: None),
        error_logger=SimpleNamespace(warning=lambda *_args, **_kwargs: None),
    )

    async def send_long_message(chat_id, text, **kwargs):
        sent.append((chat_id, text, kwargs))
        return 0.0, 1

    runtime.send_long_message = send_long_message
    item = SimpleNamespace(
        request_id="req-warning",
        chat_id=123,
        deliver_to_telegram=True,
    )

    runtime_pipeline.surface_wip_recovery_warning(
        runtime,
        item,
        record_count=7,
        size_bytes=4_096,
        first_request_id="req-<failed>&",
    )
    tasks = tuple(runtime._background_tasks)

    assert tasks
    warnings = runtime._request_meta_by_id["req-warning"][
        "wip_recovery_warnings"
    ]
    warning = warnings[0]
    assert warning.startswith(
        "⚠️ <b>UNFINISHED WORK</b>\n━━━━━━━━━━━━━━━━\n\n"
        "<b>Status</b> · <code>RECOVERY READY</code>"
    )
    assert "<b>Records</b> · <code>7</code>" in warning
    assert "<b>Saved data</b> · <code>4,096 bytes</code>" in warning
    assert "<code>req-&lt;failed&gt;&amp;</code>" in warning
    assert warning.index("<b>Status</b>") < warning.index("<b>Records</b>")
    assert warning.index("raw Journal data stays local") < warning.index(
        "Run <code>/compact</code>"
    )
    assert request_context_warning_fields(runtime, "req-warning") == {
        "wip_recovery_warnings": warnings
    }
    await asyncio.gather(*tasks)
    assert sent[0][2]["purpose"] == "wip-recovery-warning"
    assert sent[0][2]["parse_mode"] == "HTML"
    assert "/compact" in sent[0][1]


@pytest.mark.asyncio
async def test_execution_stage_auto_starts_only_above_128k(tmp_path):
    runtime = _Runtime(tmp_path)
    for grant in runtime.backend_manager.config.allowed_backends:
        grant.pop("context_window_tokens", None)
        grant.pop("response_headroom_tokens", None)
    runtime.backend_manager._selected = SimpleNamespace(
        provider="openrouter-api",
        fast_model="test/compact-model",
        pro_model="test/compact-model",
    )
    runtime.backend_manager._effective_her_v2_config = lambda: {
        "profiles": {
            "only": {
                "engine": "openrouter-api",
                "model": "test/compact-model",
                "reasoning": "high",
            }
        }
    }
    runtime.state_store.value = {
        "context_compaction": {"policy": {"recent_exchanges": 1}}
    }
    _write_turns(runtime, 40, chars=4_000)
    runtime.context_assembler = SimpleNamespace(turns_injection_enabled=True)
    calls = []
    runtime._context_compaction_coordinator = ContextCompactionCoordinator(
        runtime,
        invoker=_valid_invoker(calls),
    )

    scheduled = schedule_execution_stage(
        runtime,
        request_ref="req-at-boundary",
        prompt_tokens=DEFAULT_AUTO_COMPACTION_TRIGGER_TOKENS,
    )
    assert scheduled is False
    assert not getattr(runtime, "_context_compaction_tasks", set())

    scheduled = schedule_execution_stage(
        runtime,
        request_ref="req-above-boundary",
        prompt_tokens=DEFAULT_AUTO_COMPACTION_TRIGGER_TOKENS + 1,
    )
    assert scheduled is True
    duplicate = schedule_execution_stage(
        runtime,
        request_ref="req-above-boundary",
        prompt_tokens=DEFAULT_AUTO_COMPACTION_TRIGGER_TOKENS + 1,
    )
    assert duplicate is False
    tasks = tuple(runtime._context_compaction_tasks)
    assert tasks
    await asyncio.gather(*tasks)

    assert calls
    request = calls[0][1]
    assert request.trigger == "execution_stage_auto"
    assert runtime._context_compaction_coordinator.store.read_state()["generation"] == 1


@pytest.mark.asyncio
async def test_execution_provider_triggers_once_only_for_main_execution():
    invoked = []
    triggered = []

    class BaseProvider:
        async def invoke(self, profile, request):
            invoked.append((profile, request))
            return "ok"

    provider = _ExecutionStageCompactionProvider(
        BaseProvider(),
        lambda: triggered.append("execution"),
    )
    await provider.invoke(None, SimpleNamespace(stage=Stage.TRIAGE, role="lightweight"))
    await provider.invoke(
        None,
        SimpleNamespace(stage=Stage.EXECUTION, role="sub_agent:worker-1"),
    )
    await provider.invoke(None, SimpleNamespace(stage=Stage.EXECUTION, role="premium"))
    await provider.invoke(None, SimpleNamespace(stage=Stage.EXECUTION, role="premium"))

    assert len(invoked) == 4
    assert triggered == ["execution"]


@pytest.mark.asyncio
async def test_execution_stage_compaction_failure_is_visible_with_verbose_off(
    tmp_path,
):
    runtime = _Runtime(tmp_path, capacity=20_000, headroom=512)
    runtime.state_store.value = {
        "context_compaction": {
            "policy": {
                "recent_exchanges": 1,
            }
        }
    }
    _write_turns(runtime, 12, chars=1_000)
    runtime.context_assembler = SimpleNamespace(turns_injection_enabled=True)

    async def invalid(*_args):
        return SimpleNamespace(text="{}", structured_data=None)

    runtime._context_compaction_coordinator = ContextCompactionCoordinator(
        runtime,
        invoker=invalid,
    )
    runtime._verbose = False
    sent = []

    async def send_long_message(chat_id, text, **kwargs):
        sent.append((chat_id, text, kwargs))
        return 0.0, 1

    runtime.send_long_message = send_long_message

    scheduled = schedule_execution_stage(
        runtime,
        request_ref="req-execution-warning",
        prompt_tokens=DEFAULT_AUTO_COMPACTION_TRIGGER_TOKENS + 1,
        chat_id=123,
        deliver_to_telegram=True,
    )
    assert scheduled is True
    tasks = tuple(runtime._context_compaction_tasks)
    assert tasks
    await asyncio.gather(*tasks)

    assert sent
    assert sent[0][0] == 123
    assert "context compaction warning" in sent[0][1].lower()
    assert "continued without waiting" in sent[0][1]
    audit_rows = [
        json.loads(line)
        for line in runtime._context_compaction_coordinator.store.audit_path.read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    warning_rows = [row for row in audit_rows if row["event"] == "capacity_warning"]
    assert warning_rows
    assert warning_rows[-1]["payload"]["will_continue"] is True
    delivery_rows = [
        row for row in audit_rows if row["event"] == "capacity_warning_delivery"
    ]
    assert delivery_rows[-1]["payload"]["delivered"] is True


@pytest.mark.asyncio
async def test_paused_turn_injection_does_not_inject_or_compact_raw_history(
    tmp_path,
    monkeypatch,
):
    runtime = _Runtime(tmp_path, capacity=20_000, headroom=512)
    runtime.state_store.value = {
        "context_compaction": {
            "policy": {
                "recent_exchanges": 1,
            }
        }
    }
    _write_turns(runtime, 12, chars=1_000)
    runtime.backend_manager.current_backend = SimpleNamespace(
        _session_id=None,
        tool_registry=None,
        capabilities=SimpleNamespace(supports_sessions=False),
    )
    runtime.context_assembler = BridgeContextAssembler(runtime.memory_store, None)
    runtime.context_assembler.turns_injection_enabled = False
    calls = []
    runtime._context_compaction_coordinator = ContextCompactionCoordinator(
        runtime,
        invoker=_valid_invoker(calls),
    )
    runtime._consume_session_primer = lambda item: item.prompt
    runtime._workzone_prompt_section = lambda: []

    async def pre_turn(*_args, **_kwargs):
        return []

    runtime._build_pre_turn_context_sections = pre_turn
    runtime.current_request_meta = {}
    runtime._last_prompt_audit = {}
    runtime._thinking_chars_this_req = 0
    runtime._last_full_prompt_tokens = 0
    monkeypatch.setattr(
        runtime_pipeline.runtime_cross_session,
        "prepare_reply_binding",
        lambda _runtime, _item, prompt: prompt,
    )
    monkeypatch.setattr(
        runtime_pipeline.runtime_retry,
        "prepare_interrupted_task_continuation",
        lambda _runtime, _item, prompt, **_kwargs: prompt,
    )
    monkeypatch.setattr(runtime_pipeline, "is_memory_plus_enabled", lambda _path: False)
    item = SimpleNamespace(
        request_id="req-paused-history",
        prompt="CURRENT-WITH-HISTORY-PAUSED",
        source="text",
        silent=False,
        skip_memory_injection=False,
    )

    result = await runtime_pipeline.build_turn_prompt(
        runtime,
        item,
        is_bridge_request=False,
    )
    scheduled = schedule_execution_stage(
        runtime,
        request_ref=item.request_id,
        prompt_tokens=DEFAULT_AUTO_COMPACTION_TRIGGER_TOKENS + 1,
    )

    assert "CURRENT-WITH-HISTORY-PAUSED" in result.final_prompt
    assert "HASHI MANAGED CONVERSATION HISTORY" not in result.final_prompt
    assert "user-0:" not in result.final_prompt
    assert calls == []
    assert scheduled is False
    assert runtime._context_compaction_prompt_states[item.request_id]["inject_memory"] is False
    assert not getattr(runtime, "_context_compaction_tasks", set())


@pytest.mark.asyncio
async def test_prompt_assembly_never_runs_soft_pressure_compactor(
    tmp_path,
    monkeypatch,
):
    runtime = _Runtime(tmp_path, capacity=20_000, headroom=512)
    runtime.state_store.value = {
        "context_compaction": {
            "policy": {
                "recent_exchanges": 1,
            }
        }
    }
    _write_turns(runtime, 12, chars=500)
    runtime.backend_manager.current_backend = SimpleNamespace(
        _session_id=None,
        tool_registry=None,
        capabilities=SimpleNamespace(supports_sessions=False),
    )
    runtime.context_assembler = BridgeContextAssembler(runtime.memory_store, None)

    calls = []

    async def invalid(*args):
        calls.append(args)
        return SimpleNamespace(text="{}", structured_data=None)

    runtime._context_compaction_coordinator = ContextCompactionCoordinator(
        runtime,
        invoker=invalid,
    )
    runtime._consume_session_primer = lambda item: item.prompt
    runtime._workzone_prompt_section = lambda: []

    async def pre_turn(*_args, **_kwargs):
        return []

    runtime._build_pre_turn_context_sections = pre_turn
    runtime.current_request_meta = {}
    runtime._last_prompt_audit = {}
    runtime._thinking_chars_this_req = 0
    runtime._last_full_prompt_tokens = 0
    monkeypatch.setattr(
        runtime_pipeline.runtime_cross_session,
        "prepare_reply_binding",
        lambda _runtime, _item, prompt: prompt,
    )
    monkeypatch.setattr(
        runtime_pipeline.runtime_retry,
        "prepare_interrupted_task_continuation",
        lambda _runtime, _item, prompt, **_kwargs: prompt,
    )
    monkeypatch.setattr(runtime_pipeline, "is_memory_plus_enabled", lambda _path: False)
    item = SimpleNamespace(
        request_id="req-soft-failure",
        prompt="CURRENT-REQUEST-STILL-FITS",
        source="text",
        silent=False,
        skip_memory_injection=False,
    )

    result = await runtime_pipeline.build_turn_prompt(
        runtime,
        item,
        is_bridge_request=False,
    )

    assert "CURRENT-REQUEST-STILL-FITS" in result.final_prompt
    assert "user-0:" in result.final_prompt
    assert "COMPACTED HISTORY CAPSULE" not in result.final_prompt
    assert calls == []
    assert result.context_warnings == ()
    assert runtime._context_compaction_coordinator.store.read_state()["generation"] == 0


def test_typed_capacity_recovery_requires_no_tools_side_effects_or_delivery():
    safe = BackendResponse(
        text="",
        duration_ms=1,
        error="too large",
        is_success=False,
        error_code="CONTEXT_CAPACITY_REJECTED",
        tool_call_count=0,
        tool_loop_count=0,
        side_effects_possible=False,
    )
    assert _typed_capacity_recovery_is_safe(safe) is True

    unsafe_tool = deepcopy(safe)
    unsafe_tool.tool_call_count = 1
    assert _typed_capacity_recovery_is_safe(unsafe_tool) is False

    unsafe_delivery = deepcopy(safe)
    unsafe_delivery.stream_metadata = {
        "her_v2": {"delivery": {"delivery_id": "delivered"}}
    }
    assert _typed_capacity_recovery_is_safe(unsafe_delivery) is False


@pytest.mark.asyncio
async def test_typed_capacity_rejection_compacts_and_retries_exactly_once(tmp_path):
    runtime = _Runtime(tmp_path)
    _write_turns(runtime, 12, chars=1400)
    runtime.context_assembler = BridgeContextAssembler(runtime.memory_store, None)
    coordinator = ContextCompactionCoordinator(
        runtime,
        invoker=_valid_invoker(),
    )
    runtime._context_compaction_coordinator = coordinator
    sections, _snapshot_value = install_history_section(runtime, [])
    initial = runtime.context_assembler.build_prompt_payload(
        "current request",
        "her-v2",
        extra_sections=sections,
    )["final_prompt"]
    from orchestrator.context_compaction import estimate_tokens

    runtime._context_compaction_prompt_states = {
        "req-capacity": {
            "effective_prompt": "current request",
            "base_extra_sections": [],
            "context_profile": None,
            "inject_memory": True,
            "is_bridge_request": False,
            "final_prompt": initial,
            "prompt_tokens": estimate_tokens(initial),
            "capacity_recovery_attempted": False,
        }
    }
    runtime._context_compaction_prompt_tokens = {
        "req-capacity": estimate_tokens(initial)
    }
    retry_calls = []

    async def generate(prompt, request_id, **kwargs):
        retry_calls.append((prompt, request_id, kwargs))
        return BackendResponse(
            text="recovered",
            duration_ms=1,
            is_success=True,
        )

    runtime.backend_manager.generate_response = generate
    runtime.logger = SimpleNamespace(warning=lambda *_args, **_kwargs: None)
    runtime.is_generating = False
    item = SimpleNamespace(
        request_id="req-capacity",
        prompt="current request",
        silent=False,
    )
    rejected = BackendResponse(
        text="",
        duration_ms=1,
        error="too large",
        is_success=False,
        error_code="CONTEXT_CAPACITY_REJECTED",
        side_effects_possible=False,
    )

    recovered = await recover_typed_context_capacity_rejection(
        runtime,
        item,
        rejected,
    )

    assert recovered is not None
    response, retry_prompt = recovered
    assert response.is_success is True
    assert len(retry_calls) == 1
    assert retry_calls[0][1] == "req-capacity"
    assert retry_calls[0][2]["is_retry"] is True
    assert estimate_tokens(retry_prompt) < estimate_tokens(initial)
    assert coordinator.store.read_state()["generation"] == 1
    assert (
        await recover_typed_context_capacity_rejection(runtime, item, rejected)
        is None
    )


def test_her_stage_error_preserves_hashi_capacity_code_without_stage_deadline_fields():
    error = StageInvocationError(
        "context full",
        code="CONTEXT_CAPACITY_REJECTED",
        retryable=False,
    )

    assert error.error_code == "CONTEXT_CAPACITY_REJECTED"
    assert error.terminal_copy("still full", attempts=1).error_code == "CONTEXT_CAPACITY_REJECTED"
    assert "deadline_s" not in StageRequest.__dataclass_fields__

    unknown = StageInvocationError("unknown", code="VENDOR_UNDOCUMENTED")
    assert unknown.error_code == "PROVIDER_UNKNOWN"


def test_status_reports_effective_route_and_pointer(tmp_path):
    runtime = _Runtime(tmp_path)
    _write_turns(runtime, 1, chars=20)

    text = compact_status_text(runtime)

    assert "READY" in text
    assert "inherit_quick" in text
    assert "deepseek-api / deepseek-v4-flash" in text
    assert "HER effort</b> · <code>high" in text
    assert "1,000,000 tokens" in text


@pytest.mark.asyncio
async def test_registered_compact_command_executes_through_local_command_path(tmp_path):
    runtime = _Runtime(tmp_path)

    result = await execute_local_command(runtime, "/compact status")

    assert result["ok"] is True
    assert result["command"] == "compact"
    assert "HASHI Context Compact" in result["messages"][0]["text"]
    assert "deepseek-api / deepseek-v4-flash" in result["messages"][0]["text"]


@pytest.mark.asyncio
async def test_compact_below_64k_preserves_wip_capsule_then_clears_journal(tmp_path):
    runtime = _Runtime(tmp_path)
    runtime._last_full_prompt_tokens = 25_459

    def unavailable_model_route():
        raise RuntimeError("model route intentionally unavailable")

    runtime.backend_manager.get_her_v2_configuration = unavailable_model_route
    session = runtime_session.initialize_runtime_sessions(runtime)
    workspace = runtime_session.ensure_store(runtime).session_workspace(
        session["session_id"], int(session["context_generation"])
    )
    journal = WIPJournal(
        workspace / "backend_state" / "her_v2" / "wip_journal.jsonl"
    )
    journal.begin_turn(
        request_id="req-failed",
        prompt="composed provider prompt",
        request_summary="finish the recovery test",
        session_id=session["session_id"],
        context_generation=int(session["context_generation"]),
    )
    journal.append_audit(
        {
            "event": "stage_attempt_failed",
            "stage": "execution",
            "request_ref": "hashi-request:req-failed",
            "payload": {
                "error_code": "PROVIDER_TIMEOUT",
                "human_description": "provider timed out",
            },
        }
    )
    session_metadata = {
        "session_id": session["session_id"],
        "session_surface": "workbench",
        "session_channel_key": "default",
    }

    status = await execute_local_command(
        runtime,
        "/compact status",
        session_metadata=session_metadata,
    )
    result = await execute_local_command(
        runtime,
        "/compact",
        session_metadata=session_metadata,
    )

    assert "WIP recovery</b> · <code>ACTIVE" in status["messages"][0]["text"]
    text = result["messages"][0]["text"]
    assert "WIP recovery compacted" in text
    assert "WIP_RECOVERY_COMPACTED" in text
    assert "Context compaction not needed" in text
    assert "25,459" in text
    assert journal.snapshot().active is False
    memory = BridgeMemoryStore(workspace)
    with sqlite3.connect(memory.db_path) as connection:
        row = connection.execute(
            "SELECT role, source, text FROM turns WHERE role = 'recovery'"
        ).fetchone()
    assert row is not None
    assert row[0] == "recovery"
    assert row[1].startswith("wip-recovery:sha256:")
    assert "PROVIDER_TIMEOUT" in row[2]


@pytest.mark.asyncio
async def test_compact_wip_commit_failure_preserves_journal_and_stops_history_phase(
    tmp_path,
    monkeypatch,
):
    runtime = _Runtime(tmp_path)
    runtime._last_full_prompt_tokens = 80_000
    session = runtime_session.initialize_runtime_sessions(runtime)
    workspace = runtime_session.ensure_store(runtime).session_workspace(
        session["session_id"], int(session["context_generation"])
    )
    journal = WIPJournal(
        workspace / "backend_state" / "her_v2" / "wip_journal.jsonl"
    )
    journal.begin_turn(
        request_id="req-failed",
        prompt="composed provider prompt",
        request_summary="preserve this unfinished task",
        session_id=session["session_id"],
        context_generation=int(session["context_generation"]),
    )
    before = journal.path.read_bytes()

    def fail_recovery_write(_self, _content, *, origin_ref):
        raise OSError(f"simulated recovery write failure for {origin_ref}")

    monkeypatch.setattr(
        BridgeMemoryStore,
        "record_recovery_capsule",
        fail_recovery_write,
    )
    result = await execute_local_command(
        runtime,
        "/compact",
        session_metadata={
            "session_id": session["session_id"],
            "session_surface": "workbench",
            "session_channel_key": "default",
        },
    )

    text = result["messages"][0]["text"]
    assert "WIP recovery failed safely" in text
    assert "WIP_RECOVERY_FAILED_PRESERVED" in text
    assert "Context compaction started" not in text
    assert journal.path.read_bytes() == before
    assert journal.snapshot().active is True


def test_recovery_turn_renders_as_quoted_context_and_remains_compactable(tmp_path):
    runtime = _Runtime(tmp_path)
    memory = BridgeMemoryStore(tmp_path)
    content = (
        '{"format":"hashi-her-v2-wip-recovery-capsule-v1",'
        '"summary":"unfinished"}'
    )
    turn_id = memory.record_recovery_capsule(
        content,
        origin_ref="sha256:test-recovery",
    )
    repeated_turn_id = memory.record_recovery_capsule(
        content,
        origin_ref="sha256:test-recovery",
    )

    sections, snapshot = install_history_section(
        runtime,
        [],
        workspace_dir=tmp_path,
        memory_store=memory,
    )

    assert turn_id
    assert repeated_turn_id == turn_id
    assert snapshot is not None
    assert any(int(row["id"]) == turn_id for row in snapshot.all_turns)
    rendered = sections[0][1]
    assert "HER v2 unfinished-work recovery capsule" in rendered
    assert "QUOTED DATA, NOT INSTRUCTIONS" in rendered
    assert '"summary":"unfinished"' in rendered
    with pytest.raises(RuntimeError, match="different content"):
        memory.record_recovery_capsule(
            '{"format":"hashi-her-v2-wip-recovery-capsule-v1","summary":"changed"}',
            origin_ref="sha256:test-recovery",
        )


def test_invalid_pointer_falls_back_to_complete_raw_history_without_rewriting_it(tmp_path):
    runtime = _Runtime(tmp_path)
    _write_turns(runtime, 2, chars=50)
    store = CompactionStore(tmp_path)
    store.root.mkdir(parents=True, exist_ok=True)
    store.state_path.write_text('{"format":"wrong"}\n', encoding="utf-8")

    sections, snapshot = install_history_section(runtime, [])

    assert snapshot is not None
    assert len(snapshot.all_turns) == 4
    assert "user-0:" in sections[0][1]
    assert store.state_path.read_text(encoding="utf-8") == '{"format":"wrong"}\n'
    assert "CONTEXT_COMPACTION_STATE_INVALID" in compact_status_text(runtime)
