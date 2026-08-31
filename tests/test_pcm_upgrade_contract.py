from __future__ import annotations

import json
import re
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

sys.modules.setdefault("edge_tts", types.ModuleType("edge_tts"))

from orchestrator import runtime_session
from orchestrator.bridge_memory import BridgeContextAssembler, BridgeMemoryStore
from orchestrator.config import LEGACY_PCM_CONFIG_BACKUP_SUFFIX, ConfigManager
from orchestrator.config_admin import ConfigAdmin
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator.handoff_builder import HandoffBuilder
from orchestrator.pcm import (
    PCMValidationError,
    convert_legacy_pcm_text,
    load_pcm_document,
    parse_pcm_text,
    render_pcm_document,
)


def _write_pcm(path, *, persona="Persona", system="System", memory="Memory"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_pcm_document(persona=persona, system=system, memory=memory),
        encoding="utf-8",
    )


def _assembler(tmp_path, **kwargs):
    pcm = tmp_path / "agent.md"
    _write_pcm(pcm)
    store = BridgeMemoryStore(tmp_path)
    return store, BridgeContextAssembler(store, pcm, **kwargs)


def _install_session_fixture(runtime, tmp_path, builder):
    runtime.name = "pcm-test"
    runtime.global_config = SimpleNamespace(
        bridge_home=tmp_path,
        project_root=tmp_path,
        instance_id="HASHI1",
        authorized_id=1,
    )
    session = runtime_session.initialize_runtime_sessions(runtime)
    workspace = runtime.session_store.session_workspace(
        session["session_id"], session["context_generation"]
    )
    runtime._session_handoff_builders = {str(workspace.resolve()): builder}
    return session


def test_valid_pcm_isolates_persona_system_and_memory(tmp_path):
    path = tmp_path / "agent.md"
    _write_pcm(path, persona="P", system="S", memory="M")
    document = load_pcm_document(path, workspace_dir=tmp_path)
    assert (document.persona, document.system, document.memory) == ("P", "S", "M")


@pytest.mark.parametrize(
    "content",
    [
        "",
        "outside\n[persona]\nP\n[persona_end]\n[sys]\nS\n[sys_end]\n",
        "[persona]\nP\n[persona_end]\n",
        "[persona]\nP\n[persona_end]\n[persona]\nQ\n[persona_end]\n[sys]\nS\n[sys_end]\n",
        "[persona]\n\n[persona_end]\n[sys]\nS\n[sys_end]\n",
        "[persona]\nP\n[sys_end]\n[sys]\nS\n[persona_end]\n",
    ],
)
def test_pcm_rejects_missing_duplicate_empty_mismatched_and_unmarked_content(content):
    with pytest.raises(PCMValidationError):
        parse_pcm_text(content)


def test_pcm_accepts_only_exact_workspace_lowercase_filename(tmp_path):
    upper = tmp_path / "AGENT.md"
    _write_pcm(upper)
    with pytest.raises(PCMValidationError, match="exactly 'agent.md'"):
        load_pcm_document(upper, workspace_dir=tmp_path)

    nested = tmp_path / "nested" / "agent.md"
    _write_pcm(nested)
    with pytest.raises(PCMValidationError, match="canonical agent.md"):
        load_pcm_document(nested, workspace_dir=tmp_path)

    invalid_utf8 = tmp_path / "invalid" / "agent.md"
    invalid_utf8.parent.mkdir()
    invalid_utf8.write_bytes(b"\xff\xfe")
    with pytest.raises(PCMValidationError) as exc_info:
        load_pcm_document(invalid_utf8, workspace_dir=invalid_utf8.parent)
    assert exc_info.value.code == "pcm_invalid_utf8"

    external = tmp_path / "external.md"
    _write_pcm(external)
    linked_workspace = tmp_path / "linked"
    linked_workspace.mkdir()
    linked = linked_workspace / "agent.md"
    linked.symlink_to(external)
    with pytest.raises(PCMValidationError) as exc_info:
        load_pcm_document(linked, workspace_dir=linked_workspace)
    assert exc_info.value.code == "pcm_symlink_forbidden"


def test_legacy_conversion_preserves_unmarked_guidance_without_weakening_blocks():
    converted = convert_legacy_pcm_text(
        "[persona]\nP\n[persona_end]\n\n[sys]\nS\n[sys_end]\n\nKEEP THIS"
    )
    document = parse_pcm_text(converted)
    assert document.persona == "P"
    assert document.system.startswith("S")
    assert "KEEP THIS" in document.system


def test_legacy_pcm_migration_is_atomic_idempotent_and_removes_system_md(tmp_path):
    workspace = tmp_path / "workspaces" / "zelda"
    workspace.mkdir(parents=True)
    legacy = workspace / "AGENT.md"
    legacy.write_text("Legacy Zelda persona", encoding="utf-8")
    config = {
        "global": {"authorized_id": 0},
        "agents": [
            {
                "name": "zelda",
                "type": "flex",
                "workspace_dir": "workspaces/zelda",
                "system_md": "workspaces/zelda/AGENT.md",
                "allowed_backends": ["codex-cli"],
                "active_backend": "codex-cli",
            }
        ],
    }
    config_path = tmp_path / "agents.json"
    secrets_path = tmp_path / "secrets.json"
    original = json.dumps(config)
    config_path.write_text(original, encoding="utf-8")
    secrets_path.write_text("{}", encoding="utf-8")

    _global, agents, _secrets = ConfigManager(
        config_path, secrets_path, bridge_home=tmp_path
    ).load()
    migrated_once = config_path.read_bytes()
    assert agents[0].system_md == workspace / "agent.md"
    assert "system_md" not in json.loads(migrated_once)["agents"][0]
    assert load_pcm_document(workspace / "agent.md").persona == "Legacy Zelda persona"
    assert config_path.with_name(
        config_path.name + LEGACY_PCM_CONFIG_BACKUP_SUFFIX
    ).read_text(encoding="utf-8") == original

    ConfigManager(config_path, secrets_path, bridge_home=tmp_path).load()
    assert config_path.read_bytes() == migrated_once


def test_legacy_pcm_conflict_fails_before_any_write(tmp_path):
    workspace = tmp_path / "workspaces" / "zelda"
    workspace.mkdir(parents=True)
    canonical = workspace / "agent.md"
    legacy = workspace / "legacy.md"
    _write_pcm(canonical, persona="Canonical")
    _write_pcm(legacy, persona="Different")
    config = {
        "global": {"authorized_id": 0},
        "agents": [
            {
                "name": "zelda",
                "type": "flex",
                "workspace_dir": "workspaces/zelda",
                "system_md": "workspaces/zelda/legacy.md",
                "allowed_backends": ["codex-cli"],
                "active_backend": "codex-cli",
            }
        ],
    }
    config_path = tmp_path / "agents.json"
    secrets_path = tmp_path / "secrets.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    secrets_path.write_text("{}", encoding="utf-8")
    before_config = config_path.read_bytes()
    before_pcm = canonical.read_bytes()

    with pytest.raises(PCMValidationError, match="conflicts"):
        ConfigManager(config_path, secrets_path, bridge_home=tmp_path).load()

    assert config_path.read_bytes() == before_config
    assert canonical.read_bytes() == before_pcm


def test_canonical_audit_root_inside_workspace_is_rejected_before_migration(tmp_path):
    workspace = tmp_path / "workspaces" / "zelda"
    workspace.mkdir(parents=True)
    legacy = workspace / "AGENT.md"
    legacy.write_text("Legacy persona", encoding="utf-8")
    config = {
        "global": {
            "authorized_id": 0,
            "canonical_audit": {"root": "workspaces/zelda/raw-audit"},
        },
        "agents": [
            {
                "name": "zelda",
                "type": "flex",
                "workspace_dir": "workspaces/zelda",
                "system_md": "workspaces/zelda/AGENT.md",
                "allowed_backends": ["codex-cli"],
                "active_backend": "codex-cli",
            }
        ],
    }
    config_path = tmp_path / "agents.json"
    secrets_path = tmp_path / "secrets.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    secrets_path.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="outside every mutable Agent workspace"):
        ConfigManager(config_path, secrets_path, bridge_home=tmp_path).load()

    assert not (workspace / "agent.md").exists()
    assert "system_md" in json.loads(config_path.read_text(encoding="utf-8"))["agents"][0]


def test_default_canonical_audit_root_cannot_fall_inside_agent_workspace(tmp_path):
    _write_pcm(tmp_path / "agent.md")
    config_path = tmp_path / "agents.json"
    secrets_path = tmp_path / "secrets.json"
    config_path.write_text(
        json.dumps(
            {
                "global": {"authorized_id": 0},
                "agents": [
                    {
                        "name": "zelda",
                        "type": "flex",
                        "workspace_dir": ".",
                        "allowed_backends": ["codex-cli"],
                        "active_backend": "codex-cli",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    secrets_path.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="outside every mutable Agent workspace"):
        ConfigManager(config_path, secrets_path, bridge_home=tmp_path).load()


def test_config_admin_creates_valid_lowercase_pcm_without_inheriting_tool_grants(tmp_path):
    config_path = tmp_path / "agents.json"
    config_path.write_text(
        json.dumps(
            {
                "global": {"authorized_id": 0},
                "agents": [
                    {
                        "name": "template",
                        "type": "flex",
                        "workspace_dir": "workspaces/template",
                        "active_backend": "codex-cli",
                        "allowed_backends": [
                            {
                                "engine": "codex-cli",
                                "model": "gpt-test",
                                "tools": {"allowed": ["file_write"]},
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    paths = SimpleNamespace(
        config_path=config_path,
        workspaces_root=tmp_path / "workspaces",
    )

    ok, _message = ConfigAdmin(paths).add_agent_to_config(
        "new_agent", "New Agent", token="token-placeholder"
    )

    assert ok is True
    document = load_pcm_document(tmp_path / "workspaces/new_agent/agent.md")
    assert "New Agent" in document.persona
    [created] = [
        row
        for row in json.loads(config_path.read_text(encoding="utf-8-sig"))["agents"]
        if row["name"] == "new_agent"
    ]
    assert "system_md" not in created
    assert created["active_backend"] == "codex-cli"
    assert created["allowed_backends"] == [
        {"engine": "codex-cli", "model": "gpt-test"}
    ]


def test_pcm_envelope_preserves_authority_layers(tmp_path):
    store, assembler = _assembler(tmp_path)
    payload = assembler.build_prompt_payload("Current request", "openrouter-api")
    sections = {item["key"]: item for item in payload["envelope"]["sections"]}
    assert sections["permanent_system"]["rank"] > sections["current_user_request"]["rank"]
    assert sections["current_user_request"]["rank"] > sections["persona"]["rank"]
    assert sections["persona"]["rank"] > sections["permanent_memory"]["rank"]
    assert sections["current_user_request"]["protected"] is True


def test_workzones_use_protected_runtime_envelope_without_rendering_revision(tmp_path):
    _store, assembler = _assembler(tmp_path)
    payload = assembler.build_prompt_payload(
        "Current request",
        "openrouter-api",
        extra_sections=[
            (
                "WORKZONES",
                "Primary working directory: /repo/main\nAttached: /repo/shared",
                {
                    "key": "working_environment.workzones",
                    "protected": True,
                    "schema_version": 2,
                    "scope": "session",
                    "workzone_revision": 18,
                },
            )
        ],
    )
    sections = {item["key"]: item for item in payload["envelope"]["sections"]}
    workzones = sections["working_environment.workzones"]

    assert workzones["authority"] == "runtime_context"
    assert workzones["protected"] is True
    assert workzones["metadata"]["workzone_revision"] == 18
    assert "State revision" not in payload["final_prompt"]
    assert "workzone_revision" not in payload["final_prompt"]


def test_legacy_assembler_caller_injects_latest_ten_completed_exchanges(tmp_path):
    store, assembler = _assembler(tmp_path)
    for index in range(12):
        store.record_completed_exchange(
            f"U{index}",
            f"A{index}",
            "text",
            user_ts=f"2026-08-26T00:{index:02d}:00+00:00",
            assistant_ts=f"2026-08-26T00:{index:02d}:30+00:00",
        )
    store.record_turn("user", "text", "INCOMPLETE")

    payload = assembler.build_prompt_payload("now", "openrouter-api")

    assert payload["audit"]["history_requested"] == 10
    assert "USER: U0\n" not in payload["final_prompt"]
    assert "USER: U1\n" not in payload["final_prompt"]
    assert "USER: U2\n" in payload["final_prompt"]
    assert "USER: U11\n" in payload["final_prompt"]
    assert "INCOMPLETE" not in payload["final_prompt"]
    assert "sequence=" in payload["final_prompt"] and "user_ts=" in payload["final_prompt"]


def test_fixed_incremental_sends_delta_pcm_without_repeating_history(tmp_path):
    store, assembler = _assembler(
        tmp_path,
        skill_catalog_provider=lambda: [{"name": "one", "description": "Skill metadata"}],
        tool_catalog_provider=lambda: [{"name": "web_search", "description": "Search"}],
    )
    store.record_completed_exchange("OLD USER", "OLD ASSISTANT", "text")

    payload = assembler.build_prompt_payload(
        "new request", "codex-cli", incremental=True
    )

    assert "OLD USER" not in payload["final_prompt"]
    for expected in ("System", "Memory", "Persona", "AVAILABLE HASHI SKILLS", "web_search"):
        assert expected in payload["final_prompt"]
    assert payload["audit"]["incremental"] is True


def test_fixed_transport_order_survives_initial_only_section_omissions(tmp_path):
    store, assembler = _assembler(
        tmp_path,
        skill_catalog_provider=lambda: [{"name": "one", "description": "Skill"}],
        tool_catalog_provider=lambda: [{"name": "web_search", "description": "Search"}],
    )
    store.record_completed_exchange("OLD USER", "OLD ASSISTANT", "text")

    initial = assembler.build_prompt_payload("first", "her-v2", incremental=False)
    incremental = assembler.build_prompt_payload("second", "her-v2", incremental=True)
    initial_orders = {
        item["key"]: item["order"] for item in initial["transport_snapshot"]["sections"]
    }
    incremental_orders = {
        item["key"]: item["order"]
        for item in incremental["transport_snapshot"]["sections"]
    }

    assert any(key.startswith("recent_exchange:") for key in initial_orders)
    assert not any(key.startswith("recent_exchange:") for key in incremental_orders)
    common_keys = set(initial_orders) & set(incremental_orders)
    assert {key: initial_orders[key] for key in common_keys} == {
        key: incremental_orders[key] for key in common_keys
    }
    assert incremental_orders["time"] < incremental_orders["skills_catalogue"]
    assert (
        incremental_orders["skills_catalogue"] < incremental_orders["tools_catalogue"]
    )


def test_non_her_budget_removes_oldest_whole_exchanges_first(tmp_path, monkeypatch):
    store, assembler = _assembler(tmp_path)
    for index in range(5):
        store.record_completed_exchange(
            f"USER-{index}-" + (str(index) * 450),
            f"ASSISTANT-{index}-" + (str(index) * 450),
            "text",
        )
    monkeypatch.setitem(assembler.PROMPT_BUDGETS, "openrouter-api", 3100)

    payload = assembler.build_prompt_payload("PROTECTED CURRENT", "openrouter-api")

    assert payload["audit"]["history_omitted"]
    assert "PROTECTED CURRENT" in payload["final_prompt"]
    assert "USER-4-" in payload["final_prompt"]
    omitted = payload["audit"]["history_omitted"]
    assert [item["sequence"] for item in omitted] == sorted(
        item["sequence"] for item in omitted
    )


def test_catalogues_contain_only_metadata_not_skill_body(tmp_path):
    _store, assembler = _assembler(
        tmp_path,
        skill_catalog_provider=lambda: [
            {"name": "memory-search", "description": "Search memory"}
        ],
        tool_catalog_provider=lambda: [
            {"name": "memory_search", "description": "Search authorised memory"}
        ],
    )
    payload = assembler.build_prompt_payload("recall", "openrouter-api")
    assert "memory-search: Search memory" in payload["final_prompt"]
    assert "memory_search: Search authorised memory" in payload["final_prompt"]
    assert "Cross-Agent boundary" not in payload["final_prompt"]


def test_external_turn_time_contains_date_seconds_timezone_name_and_offset(tmp_path):
    _store, assembler = _assembler(tmp_path)
    prompt = assembler.build_prompt_payload("time", "openrouter-api")["final_prompt"]
    assert re.search(
        r"Current local time: \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} .+ \(UTC[+-]\d{2}:\d{2}\)",
        prompt,
    )


def test_handoff_keeps_only_complete_exchanges_and_never_clips_inside_one(tmp_path):
    builder = HandoffBuilder(tmp_path)
    builder.append_transcript("user", "old user")
    builder.append_transcript("assistant", "old assistant")
    builder.append_transcript("user", "new " + "word " * 50)
    builder.append_transcript("assistant", "answer " + "word " * 50)
    builder.append_transcript("user", "unfinished")

    block, count, words = builder.build_recent_context_block(max_rounds=10, max_words=20)

    assert block == "" and count == 0 and words == 0
    assert builder.last_omission_audit["omitted_oldest_exchanges"] == 2
    assert "handoff-clipped" not in block


def test_handoff_preserves_original_sequence_after_oldest_exchange_pruning(tmp_path):
    builder = HandoffBuilder(tmp_path)
    for index in range(12):
        builder.append_transcript("user", f"u-{index}")
        builder.append_transcript("assistant", f"a-{index}")

    block, count, _words = builder.build_recent_context_block(
        max_rounds=10,
        max_words=6000,
    )

    assert count == 10
    assert "Exchange sequence=3;" in block
    assert "Exchange sequence=12;" in block
    assert "Exchange sequence=1;" not in block


def test_transfer_package_cannot_reintroduce_exchanges_omitted_by_word_cap(tmp_path):
    builder = HandoffBuilder(tmp_path)
    builder.append_transcript("user", "old user")
    builder.append_transcript("assistant", "old assistant")
    builder.append_transcript("user", "new user")
    builder.append_transcript("assistant", "new assistant")

    package = builder.build_transfer_package(
        transfer_id="trf-cap",
        source_agent="zelda",
        source_instance="HASHI2",
        target_agent="rika",
        target_instance="HASHI2",
        created_at="2026-08-26T00:00:00Z",
        max_rounds=10,
        max_words=4,
    )

    assert package["exchange_count"] == 1
    assert len(package["recent_rounds"]) == 1
    assert package["last_user_message"] == "new user"
    assert "old user" not in json.dumps(package["recent_rounds"])
    assert package["omission_audit"]["omitted_oldest_exchanges"] == 1


@pytest.mark.asyncio
async def test_backend_plus_delivers_one_continuation_payload_to_fixed_target(tmp_path):
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.workspace_dir = tmp_path
    runtime.config = SimpleNamespace(
        active_backend="codex-cli",
        allowed_backends=[{"engine": "codex-cli"}],
    )
    backend = SimpleNamespace(
        capabilities=SimpleNamespace(supports_sessions=True),
        set_session_mode=Mock(),
        handle_new_session=AsyncMock(),
    )
    runtime.backend_manager = SimpleNamespace(
        current_backend=backend,
        switch_backend=AsyncMock(return_value=True),
    )
    runtime.handoff_builder = SimpleNamespace(
        refresh_recent_context=Mock(),
        build_handoff=Mock(),
        build_session_restore_prompt=Mock(return_value=("RESTORE ONCE", 3, 30)),
    )
    _install_session_fixture(runtime, tmp_path, runtime.handoff_builder)
    runtime.handoff_path = tmp_path / "handoff.md"
    runtime._evaluate_enterprise_policy = lambda *_args, **_kwargs: SimpleNamespace(
        allowed=True,
        decision=SimpleNamespace(value="allow"),
    )
    runtime._backend_busy = lambda: False
    runtime._sync_workzone_to_backend_config = Mock()
    runtime._arm_session_primer = Mock()
    runtime.enqueue_request = AsyncMock()
    runtime.get_current_model = lambda: "gpt-test"
    runtime.get_current_provider = lambda: None
    runtime._get_current_effort = lambda: None

    ok, _message = await FlexibleAgentRuntime._switch_backend_mode(
        runtime,
        42,
        "codex-cli",
        with_context=True,
    )

    assert ok is True
    runtime.enqueue_request.assert_awaited_once_with(
        42,
        "RESTORE ONCE",
        "handoff",
        "Backend continuation [3 exchanges]",
        silent=True,
        deliver_to_telegram=False,
        skip_memory_injection=True,
    )


@pytest.mark.asyncio
async def test_handoff_command_uses_bridge_history_across_sessions(tmp_path):
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.name = "pcm-test"
    runtime.workspace_dir = tmp_path / "workspaces" / "pcm-test"
    runtime.workspace_dir.mkdir(parents=True)
    runtime.global_config = SimpleNamespace(
        bridge_home=tmp_path,
        project_root=tmp_path,
        instance_id="HASHI1",
        authorized_id=1,
    )
    current = runtime_session.initialize_runtime_sessions(runtime)
    historical = runtime.session_store.create_session(
        owner_id="user:1", agent_id="pcm-test", title="Historical"
    )
    accepted = runtime.session_store.accept_run(
        session_id=historical["session_id"],
        owner_id="user:1",
        agent_id="pcm-test",
        request_id="req-historical",
        text="HISTORY FROM ANOTHER SESSION",
        source="text",
        idempotency_key="historical",
    )
    runtime.session_store.mark_request_running(
        accepted.request_id, worker_id="test-worker"
    )
    runtime.session_store.finish_request(
        accepted.request_id,
        success=True,
        assistant_text="historical answer",
        assistant_source="codex-cli",
    )
    runtime.session_store.archive_session(historical["session_id"])
    runtime._is_authorized_user = lambda _user_id: True
    runtime._backend_busy = lambda: False
    runtime._reply_text = AsyncMock()
    runtime._send_text = AsyncMock()
    runtime._arm_session_primer = Mock()
    runtime.handoff_builder = HandoffBuilder(runtime.workspace_dir)
    backend = SimpleNamespace(
        capabilities=SimpleNamespace(supports_sessions=True),
        handle_new_session=AsyncMock(),
    )
    runtime.backend_manager = SimpleNamespace(current_backend=backend)
    runtime.enqueue_request = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=42),
    )

    await FlexibleAgentRuntime.cmd_handoff(runtime, update, SimpleNamespace(args=[]))

    runtime.enqueue_request.assert_awaited_once()
    args, kwargs = runtime.enqueue_request.await_args
    assert args[0] == 42
    assert args[2:] == (
        "handoff",
        "Handoff restore [1 exchanges]",
    )
    assert "HISTORY FROM ANOTHER SESSION" in args[1]
    assert "historical answer" in args[1]
    assert kwargs == {"skip_memory_injection": True}
    backend.handle_new_session.assert_awaited_once_with()
    runtime._arm_session_primer.assert_called_once_with(
        "This is a bridge-managed handoff restore. Review AGENT FYI, then use the recent transcript as continuity context.",
        session_id=current["session_id"],
    )
