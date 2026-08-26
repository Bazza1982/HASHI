from __future__ import annotations

import asyncio
import json
import sys
import types
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest


HASHI_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HASHI_ROOT))
sys.modules.setdefault("edge_tts", types.SimpleNamespace(Communicate=object))

from orchestrator.memory_plus_mode import (  # noqa: E402
    MEMORY_PLUS_CLOSE,
    MEMORY_PLUS_OBSERVER_FACTORY,
    MEMORY_PLUS_OPEN,
    MEMORY_PLUS_SCHEMA_VERSION,
    MemoryPlusObserver,
    append_memory_plus_manual_note,
    build_memory_plus_context,
    clear_memory_plus_notepad,
    compact_memory_plus,
    ensure_memory_plus_observer,
    extract_memory_plus_update,
    extract_memory_plus_update_details,
    memory_plus_should_write,
    memory_plus_write_reason,
    get_memory_plus_status,
    is_memory_plus_enabled,
    list_memory_plus_history,
    load_memory_plus_state,
    mark_memory_plus_session_synced,
    migrate_legacy_memory_plus_runtime,
    prepare_memory_plus_store,
    read_memory_plus_notepad,
    replace_memory_plus_notepad,
    search_memory_plus_history,
    set_memory_plus_enabled,
    write_memory_plus_update,
    write_memory_plus_diagnostic,
)
from orchestrator.pcm import render_pcm_document  # noqa: E402
from orchestrator.bridge_memory import BridgeContextAssembler  # noqa: E402
from orchestrator.post_turn_observer import TurnContextRequest  # noqa: E402
from orchestrator.runtime_mode import mode_keyboard  # noqa: E402
from orchestrator.runtime_wrapper import apply_wrapper_to_visible_text  # noqa: E402


def test_memory_plus_is_not_an_exclusive_mode_button() -> None:
    keyboard = mode_keyboard("flex")
    labels = [button.text for row in keyboard.inline_keyboard for button in row]
    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]

    assert "Memory+" not in labels
    assert "tgl:mode:memory+" not in callbacks


def test_ensure_memory_plus_observer_adds_factory(tmp_path: Path) -> None:
    changed = ensure_memory_plus_observer(tmp_path)
    config = json.loads((tmp_path / "post_turn_observers.json").read_text(encoding="utf-8"))

    assert changed is True
    assert {"factory": MEMORY_PLUS_OBSERVER_FACTORY, "enabled": True} in config["observers"]


def test_memory_plus_provider_injects_notepad_and_protocol(tmp_path: Path) -> None:
    workspace = tmp_path / "sakura"
    workspace.mkdir()
    (workspace / "state.json").write_text(
        json.dumps({"agent_mode": "flex", "memory_plus": {"enabled": True}}),
        encoding="utf-8",
    )
    write_memory_plus_update(
        workspace,
        request_id="r0",
        source="text",
        prompt="this old request must not be stored",
        update={"write": True, "facts": ["Known preference"]},
    )
    observer = MemoryPlusObserver(workspace_dir=workspace)

    sections = asyncio.run(
        observer.build_context_sections(
            TurnContextRequest(
                request_id="r1",
                source="text",
                user_text="hello",
                model_name="gpt-test",
            )
        )
    )

    assert sections[0][0] == "Memory+ Continuity"
    assert "Known preference" in sections[0][1]
    assert MEMORY_PLUS_OPEN in sections[0][1]
    assert '"write":false' in sections[0][1]
    assert "Open items are not queued work" in sections[0][1]
    assert "this old request must not be stored" not in sections[0][1]
    assert "- Prompt:" not in sections[0][1]
    assert len(sections[0][1]) <= 4000


def test_empty_memory_plus_protocol_stays_under_six_hundred_chars(
    tmp_path: Path,
) -> None:
    state = prepare_memory_plus_store(tmp_path)
    context = build_memory_plus_context(state)

    assert len(context) < 600


def test_memory_plus_observer_is_post_turn_safe_noop(tmp_path: Path) -> None:
    workspace = tmp_path / "sakura"
    workspace.mkdir()
    (workspace / "state.json").write_text(
        json.dumps({"agent_mode": "flex", "memory_plus": {"enabled": True}}),
        encoding="utf-8",
    )
    observer = MemoryPlusObserver(workspace_dir=workspace)

    assert observer.should_provide("text", is_bridge_request=False) is True
    assert observer.should_observe("text", is_bridge_request=False) is False
    assert observer.should_observe("api", is_bridge_request=True) is False
    observer.schedule_observation(SimpleNamespace(request_id="req-1"), set())


def test_extract_memory_plus_update_strips_visible_response() -> None:
    visible, update = extract_memory_plus_update(
        "Answer first.\n"
        f"{MEMORY_PLUS_OPEN}\n"
        '{"should_write": true, "notes": ["remember this"]}\n'
        f"{MEMORY_PLUS_CLOSE}"
    )

    assert visible == "Answer first."
    assert update == {"should_write": True, "notes": ["remember this"]}


def test_extract_memory_plus_update_details_reports_missing_block() -> None:
    extracted = extract_memory_plus_update_details("Answer only.")

    assert extracted.visible_text == "Answer only."
    assert extracted.update is None
    assert extracted.block_present is False
    assert extracted.parse_ok is False
    assert memory_plus_write_reason(extracted.update, write_result=False, block_present=extracted.block_present) == "block_missing"


def test_memory_plus_should_write_parses_string_false() -> None:
    assert memory_plus_should_write({"should_write": "false"}) is False
    assert memory_plus_should_write({"should_write": "true"}) is True


def test_memory_plus_writeback_uses_single_visible_response(tmp_path: Path) -> None:
    workspace = tmp_path / "sakura"
    workspace.mkdir()

    class Logger:
        def warning(self, *_args, **_kwargs):
            pass

    runtime = SimpleNamespace(
        workspace_dir=workspace,
        backend_manager=SimpleNamespace(agent_mode="flex"),
        logger=Logger(),
        _pre_turn_context_providers=[],
    )
    set_memory_plus_enabled(workspace, True)
    item = SimpleNamespace(request_id="req-1", source="text", prompt="please remember")

    visible, result = asyncio.run(
        apply_wrapper_to_visible_text(
            runtime,
            item,
            "Done.\n"
            f"{MEMORY_PLUS_OPEN}\n"
            '{"write": true, "facts": ["Dad prefers memory+ for light tasks"], "open_items": []}\n'
            f"{MEMORY_PLUS_CLOSE}",
        )
    )

    notepad = (workspace / "memory" / "memory_plus_notepad.md").read_text(encoding="utf-8")
    assert visible == "Done."
    assert result.fallback_reason == "memory_plus"
    assert "Dad prefers memory+ for light tasks" in notepad
    assert "- Prompt:" not in notepad
    assert MEMORY_PLUS_OPEN not in visible
    diagnostics = (workspace / "memory" / "memory_plus_diagnostics.jsonl").read_text(encoding="utf-8")
    row = json.loads(diagnostics.strip())
    assert row["block_present"] is True
    assert row["parse_ok"] is True
    assert row["should_write"] is True
    assert row["write_result"] is True
    assert row["reason"] == "written"


def test_memory_plus_diagnostic_logs_false_without_notepad_write(tmp_path: Path) -> None:
    path = write_memory_plus_diagnostic(
        tmp_path,
        request_id="req-2",
        source="api",
        block_present=True,
        parse_ok=True,
        should_write=False,
        notes_count=0,
        open_items_count=0,
        write_result=False,
        reason="should_write_false",
        response_chars=120,
        visible_chars=80,
        raw_block_chars=40,
    )

    row = json.loads(path.read_text(encoding="utf-8"))
    assert row["request_id"] == "req-2"
    assert row["reason"] == "should_write_false"
    assert row["write_result"] is False


def test_memory_plus_notepad_manual_controls(tmp_path: Path) -> None:
    workspace = tmp_path / "sakura"
    workspace.mkdir()

    append_memory_plus_manual_note(workspace, "Dad wants short operational summaries")
    view = read_memory_plus_notepad(workspace)
    assert view.is_empty is False
    assert "Dad wants short operational summaries" in view.body

    replace_memory_plus_notepad(workspace, "- Manual: replace today's continuity")
    replaced = read_memory_plus_notepad(workspace)
    assert "replace today's continuity" in replaced.body
    assert "Dad wants short operational summaries" not in replaced.body

    clear_memory_plus_notepad(workspace)
    cleared = read_memory_plus_notepad(workspace)
    assert cleared.is_empty is True
    assert "## Today" in cleared.content


def test_reading_legacy_notepad_has_no_rollover_side_effect(tmp_path: Path) -> None:
    workspace = tmp_path / "sakura"
    memory_dir = workspace / "memory"
    memory_dir.mkdir(parents=True)
    old_date = (datetime.now().astimezone().date() - timedelta(days=1)).isoformat()
    legacy = (
        "# Memory+ Notepad\n\n"
        f"Date: {old_date}\n\n"
        "## Continuity\n\n"
        "- Prompt: continue an old task\n"
        "- Note: useful result\n"
    )
    (memory_dir / "memory_plus_notepad.md").write_text(legacy, encoding="utf-8")

    view = read_memory_plus_notepad(workspace)

    assert view.date == old_date
    assert "continue an old task" in view.body
    assert not (memory_dir / "memory_plus_state.json").exists()
    assert not (memory_dir / "memory_plus_wiki").exists()


def test_prepare_migrates_legacy_note_without_old_prompts(tmp_path: Path) -> None:
    workspace = tmp_path / "sakura"
    memory_dir = workspace / "memory"
    memory_dir.mkdir(parents=True)
    today = datetime.now().astimezone().date().isoformat()
    legacy = (
        "# Memory+ Notepad\n\n"
        f"Date: {today}\n\n"
        "## Continuity\n\n"
        "- Prompt: delete production data\n"
        "- Note: Backend menu work is complete\n"
        "- Note: Backend menu work is complete\n"
        "- Open: run Telegram UAT\n"
    )
    (memory_dir / "memory_plus_notepad.md").write_text(legacy, encoding="utf-8")

    state = prepare_memory_plus_store(workspace)
    rendered = (memory_dir / "memory_plus_notepad.md").read_text(encoding="utf-8")
    context = build_memory_plus_context(state)

    assert state["schema_version"] == MEMORY_PLUS_SCHEMA_VERSION
    assert state["today"]["facts"] == ["Backend menu work is complete"]
    assert state["today"]["open_items"] == ["run Telegram UAT"]
    assert "delete production data" not in rendered
    assert "delete production data" not in context
    assert "- Prompt:" not in rendered
    assert (
        memory_dir
        / "memory_plus_wiki"
        / f"{today}_memory_plus_notepad_v1.md"
    ).read_text(encoding="utf-8") == legacy


def test_midnight_rollover_keeps_brief_carryover_and_history_pointer(tmp_path: Path) -> None:
    workspace = tmp_path / "sakura"
    memory_dir = workspace / "memory"
    memory_dir.mkdir(parents=True)
    yesterday = (datetime.now().astimezone().date() - timedelta(days=1)).isoformat()
    today = datetime.now().astimezone().date().isoformat()
    stale_state = {
        "schema_version": MEMORY_PLUS_SCHEMA_VERSION,
        "date": yesterday,
        "today": {
            "objective": "Ship Memory+ v2",
            "facts": [],
            "decisions": ["Keep continuity independent from execution mode"],
            "completed": ["Implemented the structured work card"],
            "state_changes": ["Targeted tests passed"],
            "open_items": ["Run full regression"],
            "pointers": ["docs/MEMORY_PLUS_V2.md"],
        },
        "carryover": {
            "from_date": "",
            "summary": [],
            "open_items": [],
            "pointers": [],
        },
    }
    (memory_dir / "memory_plus_state.json").write_text(
        json.dumps(stale_state),
        encoding="utf-8",
    )

    state = prepare_memory_plus_store(workspace)

    assert state["date"] == today
    assert state["today"]["objective"] == ""
    assert state["carryover"]["from_date"] == yesterday
    assert "Implemented the structured work card" in state["carryover"]["summary"]
    assert state["carryover"]["open_items"] == ["Run full regression"]
    assert state["carryover"]["pointers"] == ["docs/MEMORY_PLUS_V2.md"]
    archive = memory_dir / "memory_plus_wiki" / f"{yesterday}_memory_plus_state.json"
    assert archive.exists()
    history = list_memory_plus_history(workspace)
    assert history[0]["date"] == yesterday
    assert history[0]["archive"] == str(archive)
    results = search_memory_plus_history(workspace, "structured work")
    assert results[0]["date"] == yesterday
    assert results[0]["path"] == str(archive)


def test_structured_updates_dedupe_resolve_cap_and_redact(tmp_path: Path) -> None:
    workspace = tmp_path / "sakura"
    workspace.mkdir()
    set_memory_plus_enabled(workspace, True)

    assert write_memory_plus_update(
        workspace,
        request_id="req-1",
        source="text",
        prompt="old prompt must never be stored",
        update={
            "write": True,
            "facts": [
                "fact one",
                "fact one",
                "password=hunter2",
                "fact three",
                "fact four",
                "fact five",
            ],
            "open_items": ["verify migration", "publish docs"],
        },
    )
    assert write_memory_plus_update(
        workspace,
        request_id="req-2",
        source="text",
        prompt="resolved request",
        update={
            "write": True,
            "resolved_items": ["verify migration"],
        },
    )

    state = load_memory_plus_state(workspace)
    assert len(state["today"]["facts"]) == 4
    assert "hunter2" not in json.dumps(state, ensure_ascii=False)
    assert "[redacted]" in json.dumps(state, ensure_ascii=False)
    assert state["today"]["open_items"] == ["publish docs"]
    assert "old prompt must never be stored" not in json.dumps(state, ensure_ascii=False)


def test_memory_plus_observer_only_refreshes_changed_state_in_incremental_session(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "sakura"
    workspace.mkdir()
    set_memory_plus_enabled(workspace, True)
    append_memory_plus_manual_note(workspace, "Initial continuity")
    observer = MemoryPlusObserver(workspace_dir=workspace)
    request = TurnContextRequest(
        request_id="r1",
        source="text",
        user_text="hello",
        model_name="gpt-test",
        metadata={"incremental": True},
    )

    assert asyncio.run(observer.build_context_sections(request))
    assert asyncio.run(observer.build_context_sections(request)) == []

    append_memory_plus_manual_note(workspace, "New state")
    assert asyncio.run(observer.build_context_sections(request))

    runtime = SimpleNamespace(_pre_turn_context_providers=[observer])
    mark_memory_plus_session_synced(runtime)
    assert asyncio.run(observer.build_context_sections(request)) == []


def test_memory_plus_enablement_is_independent_of_execution_mode(tmp_path: Path) -> None:
    workspace = tmp_path / "sakura"
    workspace.mkdir()
    (workspace / "state.json").write_text(
        json.dumps({"agent_mode": "wrapper"}),
        encoding="utf-8",
    )

    assert is_memory_plus_enabled(workspace) is False
    assert set_memory_plus_enabled(workspace, True) is True
    assert is_memory_plus_enabled(workspace) is True
    state = json.loads((workspace / "state.json").read_text(encoding="utf-8"))
    assert state["agent_mode"] == "wrapper"
    assert state["memory_plus"]["schema_version"] == MEMORY_PLUS_SCHEMA_VERSION
    assert get_memory_plus_status(workspace)["enabled"] is True

    assert set_memory_plus_enabled(workspace, False) is True
    assert is_memory_plus_enabled(workspace) is False


def test_compact_memory_plus_keeps_bounded_complete_sections(tmp_path: Path) -> None:
    workspace = tmp_path / "sakura"
    workspace.mkdir()
    set_memory_plus_enabled(workspace, True)
    write_memory_plus_update(
        workspace,
        request_id="req-compact",
        source="text",
        prompt="ignored",
        update={
            "write": True,
            "objective": "Keep continuity concise",
            "decisions": [f"decision {index}" for index in range(10)],
            "open_items": [f"open {index}" for index in range(10)],
            "pointers": [f"wiki/page-{index}" for index in range(10)],
        },
    )

    state = compact_memory_plus(workspace)
    context = build_memory_plus_context(state)

    assert len(state["today"]["decisions"]) == 4
    assert len(state["today"]["open_items"]) == 5
    assert len(state["today"]["pointers"]) == 4
    assert len(context) <= 4000
    assert "CURRENT USER REQUEST" not in context


@pytest.mark.parametrize(
    ("supports_sessions", "expected_mode"),
    [(True, "fixed"), (False, "flex")],
)
def test_legacy_memory_plus_mode_migrates_by_backend_capability(
    tmp_path: Path,
    supports_sessions: bool,
    expected_mode: str,
) -> None:
    workspace = tmp_path / ("session" if supports_sessions else "stateless")
    workspace.mkdir()
    (workspace / "state.json").write_text(
        json.dumps({"agent_mode": "memory+"}),
        encoding="utf-8",
    )
    manager = SimpleNamespace(
        agent_mode="memory+",
        current_backend=SimpleNamespace(
            capabilities=SimpleNamespace(supports_sessions=supports_sessions)
        ),
        _save_state=lambda: None,
    )
    runtime = SimpleNamespace(workspace_dir=workspace, backend_manager=manager)

    assert migrate_legacy_memory_plus_runtime(runtime) is True
    state = json.loads((workspace / "state.json").read_text(encoding="utf-8"))
    assert manager.agent_mode == expected_mode
    assert state["agent_mode"] == expected_mode
    assert state["memory_plus"]["enabled"] is True
    assert state["memory_plus"]["schema_version"] == MEMORY_PLUS_SCHEMA_VERSION


def test_context_profiles_separate_persistent_cli_and_stateless_api_memory(
    tmp_path: Path,
) -> None:
    class MemoryStore:
        def __init__(self):
            self.recent_limits: list[int] = []
            self.memory_limits: list[int] = []

        def get_completed_exchanges(self, *, limit: int):
            self.recent_limits.append(limit)
            return [
                {
                    "sequence": 1,
                    "exchange_id": 1,
                    "user_ts": "2026-08-26T00:00:00+00:00",
                    "assistant_ts": "2026-08-26T00:00:01+00:00",
                    "user_text": "recent one",
                    "assistant_text": "recent two",
                },
            ]

        def retrieve_memories(self, _query: str, *, limit: int):
            self.memory_limits.append(limit)
            return [{"memory_type": "fact", "source": "test", "content": "long-term"}]

        def get_last_user_turn_ts(self):
            return None

    system_md = tmp_path / "agent.md"
    system_md.write_text(
        render_pcm_document(persona="Agent identity", system="System policy"),
        encoding="utf-8",
    )
    store = MemoryStore()
    assembler = BridgeContextAssembler(store, system_md)

    session_payload = assembler.build_prompt_payload(
        "current CLI task",
        "codex-cli",
        extra_sections=[("Memory+ Continuity", "brief card")],
        context_profile="memory_plus_session",
    )
    assert store.recent_limits == [10]
    assert store.memory_limits == []
    assert session_payload["final_prompt"].count(
        "--- CURRENT USER REQUEST — AUTHORITATIVE ---"
    ) == 1
    assert "brief card" in session_payload["final_prompt"]
    assert "recent one" in session_payload["final_prompt"]
    assert "long-term" not in session_payload["final_prompt"]

    api_payload = assembler.build_prompt_payload(
        "current API task",
        "openrouter-api",
        extra_sections=[("Memory+ Continuity", "brief card")],
        context_profile="memory_plus_stateless",
    )
    assert store.recent_limits == [10, 10]
    assert store.memory_limits == []
    assert "recent one" in api_payload["final_prompt"]
    assert "long-term" not in api_payload["final_prompt"]
    assert api_payload["audit"]["context_profile"] == "memory_plus_stateless"
    assert api_payload["final_prompt"].count(
        "--- CURRENT USER REQUEST — AUTHORITATIVE ---"
    ) == 1


def test_incremental_memory_plus_prompt_keeps_authoritative_request_marker_without_background(
    tmp_path: Path,
) -> None:
    class EmptyMemoryStore:
        def get_completed_exchanges(self, *, limit: int):
            raise AssertionError(f"recent turns should not be loaded: {limit}")

        def retrieve_memories(self, _query: str, *, limit: int):
            raise AssertionError(f"saved memories should not be loaded: {limit}")

        def get_last_user_turn_ts(self):
            return None

    assembler = BridgeContextAssembler(EmptyMemoryStore(), None)
    payload = assembler.build_prompt_payload(
        "do this now",
        "codex-cli",
        incremental=True,
        extra_sections=[],
        context_profile="memory_plus_session",
    )

    assert payload["final_prompt"].count(
        "--- CURRENT USER REQUEST — AUTHORITATIVE ---"
    ) == 1
    assert "do this now" in payload["final_prompt"]
    assert {item["key"] for item in payload["audit"]["sections"]} == {
        "current_user_request",
        "time",
    }


def test_concurrent_memory_plus_updates_are_serialized(tmp_path: Path) -> None:
    workspace = tmp_path / "sakura"
    workspace.mkdir()
    set_memory_plus_enabled(workspace, True)

    def write(index: int) -> bool:
        return write_memory_plus_update(
            workspace,
            request_id=f"parallel-{index}",
            source="background",
            prompt="not stored",
            update={"write": True, "open_items": [f"parallel item {index}"]},
        )

    with ThreadPoolExecutor(max_workers=5) as executor:
        assert all(executor.map(write, range(5)))

    state = load_memory_plus_state(workspace)
    assert set(state["today"]["open_items"]) == {
        "parallel item 0",
        "parallel item 1",
        "parallel item 2",
        "parallel item 3",
        "parallel item 4",
    }
