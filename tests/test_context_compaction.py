from __future__ import annotations

import asyncio
import json
import sqlite3
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from adapters.base import BackendResponse
from orchestrator import runtime_pipeline
from orchestrator.admin_local_testing import execute_local_command
from orchestrator.bridge_memory import BridgeContextAssembler
from orchestrator.context_compaction import (
    CAPSULE_FORMAT,
    CONTEXT_PROTECTED_SET_TOO_LARGE,
    DEFAULT_UNKNOWN_COMPACTOR_BUDGET_TOKENS,
    DEFAULT_UNKNOWN_TARGET_HIGH_TOKENS,
    DEFAULT_UNKNOWN_TARGET_LOW_TOKENS,
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
    load_route_config,
    resolve_compact_route,
    resolve_target_capacity,
    resolve_trigger_budget,
    schedule_post_turn,
)
from orchestrator.her_v2.interfaces import StageInvocationError
from orchestrator.her_v2.models import StageRequest
from orchestrator.runtime_pipeline import (
    _typed_capacity_recovery_is_safe,
    recover_typed_context_capacity_rejection,
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


def test_missing_active_quick_grant_locks_without_fallback(tmp_path):
    runtime = _Runtime(tmp_path)
    runtime.backend_manager._selected = SimpleNamespace(
        provider="deepseek-api",
        fast_provider="deepseek-api",
        pro_provider="deepseek-api",
        fast_model="not-granted",
        pro_model="deepseek-v4-pro",
    )

    locked = resolve_compact_route(runtime)

    assert locked.eligible is False
    assert locked.model == "not-granted"
    assert "Quick/Light provider/model grant is absent" in locked.lock_reason


def test_hashi_api_quick_route_is_ready_without_compaction_declarations_or_capacity(
    tmp_path,
):
    runtime = _Runtime(tmp_path)
    runtime.backend_manager.config.allowed_backends.append(
        {
            "engine": "hashi-api",
            "models": ["gpt-5.6-luna", "gpt-5.6-sol"],
            "default_model": "gpt-5.6-luna",
            "fast_model": "gpt-5.6-luna",
            "pro_model": "gpt-5.6-sol",
        }
    )
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


def test_unknown_target_capacity_uses_hashi_absolute_auto_guard(tmp_path):
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
    assert budget.high_projected_tokens == DEFAULT_UNKNOWN_TARGET_HIGH_TOKENS
    assert budget.low_input_tokens == DEFAULT_UNKNOWN_TARGET_LOW_TOKENS
    assert budget.provenance == "hashi_unknown_target_guard_v1"
    assert DEFAULT_UNKNOWN_COMPACTOR_BUDGET_TOKENS == 32_000


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
async def test_build_turn_prompt_preserves_oversized_protected_request_and_warns(
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
    assert result.context_warnings
    assert CONTEXT_PROTECTED_SET_TOO_LARGE in result.context_warnings[0]
    assert "continuing" in result.context_warnings[0]


@pytest.mark.asyncio
async def test_build_turn_prompt_auto_compacts_above_watermark_and_preserves_request(
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
    runtime._context_compaction_coordinator = ContextCompactionCoordinator(
        runtime,
        invoker=_valid_invoker(),
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
    assert "COMPACTED HISTORY CAPSULE" in result.final_prompt
    assert "user-0:" not in result.final_prompt
    assert "user-11:" in result.final_prompt
    assert runtime._context_compaction_coordinator.store.read_state()["generation"] == 1
    assert runtime._last_prompt_audit["budget_applied"] is False


@pytest.mark.asyncio
async def test_unknown_capacity_auto_compacts_sunny_sized_history_before_her(
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
    assert "COMPACTED HISTORY CAPSULE" in result.final_prompt
    assert calls
    assert runtime._context_compaction_coordinator.store.read_state()["generation"] == 1
    assert (
        resolve_trigger_budget(runtime).is_unknown_capacity_guard
        is True
    )
    assert estimate_tokens(result.final_prompt) < DEFAULT_UNKNOWN_TARGET_HIGH_TOKENS


@pytest.mark.asyncio
async def test_unknown_capacity_compaction_failure_warns_and_continues_to_her(
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

    async def invalid(*_args):
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
    assert result.context_warnings
    assert "CONTEXT_CAPACITY_EXHAUSTED" in result.context_warnings[0]
    assert f"{DEFAULT_UNKNOWN_TARGET_HIGH_TOKENS}" in result.context_warnings[0]
    assert "continuing" in result.context_warnings[0]
    assert runtime._context_compaction_coordinator.store.read_state()["generation"] == 0


@pytest.mark.asyncio
async def test_120k_retry_exhaustion_warns_and_still_calls_selected_model(
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
                "unknown_target_high_tokens": 120_000,
                "unknown_target_low_tokens": 90_000,
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
        deliver_to_telegram=False,
        skip_memory_injection=False,
    )

    result = await runtime_pipeline.build_turn_prompt(
        runtime,
        item,
        is_bridge_request=False,
    )

    assert estimate_tokens(result.final_prompt) >= 120_000
    assert len(compact_calls) == 2
    assert "CURRENT-REQUEST-MUST-CONTINUE-AFTER-RETRIES" in result.final_prompt
    assert "user-0:" in result.final_prompt
    assert result.context_warnings
    assert "PROVIDER_SERVER_ERROR" in result.context_warnings[0]
    assert "continuing" in result.context_warnings[0]

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
            },
        )
    ]


@pytest.mark.asyncio
async def test_unknown_capacity_post_turn_uses_same_absolute_guard(tmp_path):
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
    _write_turns(runtime, 30, chars=4_000)
    runtime.context_assembler = SimpleNamespace(turns_injection_enabled=True)
    calls = []
    runtime._context_compaction_coordinator = ContextCompactionCoordinator(
        runtime,
        invoker=_valid_invoker(calls),
    )

    schedule_post_turn(
        runtime,
        request_ref="req-unknown-below",
        prompt_tokens=DEFAULT_UNKNOWN_TARGET_HIGH_TOKENS - 1,
    )
    assert not getattr(runtime, "_context_compaction_tasks", set())

    schedule_post_turn(
        runtime,
        request_ref="req-unknown-above",
        prompt_tokens=DEFAULT_UNKNOWN_TARGET_HIGH_TOKENS,
    )
    tasks = tuple(runtime._context_compaction_tasks)
    assert tasks
    await asyncio.gather(*tasks)

    assert calls
    request = calls[0][1]
    assert request.trigger == "post_turn_watermark"
    assert runtime._context_compaction_coordinator.store.read_state()["generation"] == 1


@pytest.mark.asyncio
async def test_post_turn_compaction_failure_is_visible_without_blocking_completion(
    tmp_path,
):
    runtime = _Runtime(tmp_path, capacity=20_000, headroom=512)
    runtime.state_store.value = {
        "context_compaction": {
            "policy": {
                "recent_exchanges": 1,
                "high_watermark": 0.10,
                "low_watermark": 0.05,
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
    sent = []

    async def send_long_message(chat_id, text, **kwargs):
        sent.append((chat_id, text, kwargs))
        return 0.0, 1

    runtime.send_long_message = send_long_message

    schedule_post_turn(
        runtime,
        request_ref="req-post-turn-warning",
        prompt_tokens=3_000,
        chat_id=123,
        deliver_to_telegram=True,
    )
    tasks = tuple(runtime._context_compaction_tasks)
    assert tasks
    await asyncio.gather(*tasks)

    assert sent
    assert sent[0][0] == 123
    assert "context compaction warning" in sent[0][1].lower()
    assert "continuing" in sent[0][1]
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
                "high_watermark": 0.10,
                "low_watermark": 0.05,
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
    schedule_post_turn(
        runtime,
        request_ref=item.request_id,
        prompt_tokens=100_000,
    )

    assert "CURRENT-WITH-HISTORY-PAUSED" in result.final_prompt
    assert "HASHI MANAGED CONVERSATION HISTORY" not in result.final_prompt
    assert "user-0:" not in result.final_prompt
    assert calls == []
    assert runtime._context_compaction_prompt_states[item.request_id]["inject_memory"] is False
    assert not getattr(runtime, "_context_compaction_tasks", set())


@pytest.mark.asyncio
async def test_soft_pressure_compactor_failure_continues_with_unchanged_fitting_prompt(
    tmp_path,
    monkeypatch,
):
    runtime = _Runtime(tmp_path, capacity=20_000, headroom=512)
    runtime.state_store.value = {
        "context_compaction": {
            "policy": {
                "recent_exchanges": 1,
                "high_watermark": 0.10,
                "low_watermark": 0.05,
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
    assert calls
    assert result.context_warnings
    assert "continuing" in result.context_warnings[0]
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
