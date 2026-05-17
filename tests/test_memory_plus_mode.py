from __future__ import annotations

import asyncio
import json
import sys
import types
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace


HASHI_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HASHI_ROOT))
sys.modules.setdefault("edge_tts", types.SimpleNamespace(Communicate=object))

from orchestrator.memory_plus_mode import (  # noqa: E402
    MEMORY_PLUS_CLOSE,
    MEMORY_PLUS_OBSERVER_FACTORY,
    MEMORY_PLUS_OPEN,
    MemoryPlusObserver,
    ensure_memory_plus_notepad,
    ensure_memory_plus_observer,
    extract_memory_plus_update,
    extract_memory_plus_update_details,
    memory_plus_should_write,
    memory_plus_write_reason,
    write_memory_plus_diagnostic,
)
from orchestrator.post_turn_observer import TurnContextRequest  # noqa: E402
from orchestrator.runtime_mode import mode_keyboard  # noqa: E402
from orchestrator.runtime_wrapper import apply_wrapper_to_visible_text  # noqa: E402


def test_memory_plus_mode_button_is_available() -> None:
    keyboard = mode_keyboard("memory+")
    labels = [button.text for row in keyboard.inline_keyboard for button in row]
    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]

    assert "✅ Memory+" in labels
    assert "tgl:mode:memory+" in callbacks


def test_ensure_memory_plus_observer_adds_factory(tmp_path: Path) -> None:
    changed = ensure_memory_plus_observer(tmp_path)
    config = json.loads((tmp_path / "post_turn_observers.json").read_text(encoding="utf-8"))

    assert changed is True
    assert {"factory": MEMORY_PLUS_OBSERVER_FACTORY, "enabled": True} in config["observers"]


def test_memory_plus_provider_injects_notepad_and_protocol(tmp_path: Path) -> None:
    workspace = tmp_path / "sakura"
    workspace.mkdir()
    (workspace / "state.json").write_text(json.dumps({"agent_mode": "memory+"}), encoding="utf-8")
    ensure_memory_plus_notepad(workspace)
    today = datetime.now().astimezone().date().isoformat()
    (workspace / "memory" / "memory_plus_notepad.md").write_text(
        f"# Memory+ Notepad\n\nDate: {today}\n\n## Continuity\n\n- Known preference",
        encoding="utf-8",
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

    assert sections[0][0] == "Memory+ Daily Notepad"
    assert "Known preference" in sections[0][1]
    assert MEMORY_PLUS_OPEN in sections[0][1]
    assert "MUST append exactly one machine-readable block" in sections[0][1]
    assert "project nicknames, folder labels, shelf codes" in sections[0][1]
    assert "do not omit the block" in sections[0][1]


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
        backend_manager=SimpleNamespace(agent_mode="memory+"),
        logger=Logger(),
    )
    item = SimpleNamespace(request_id="req-1", source="text", prompt="please remember")

    visible, result = asyncio.run(
        apply_wrapper_to_visible_text(
            runtime,
            item,
            "Done.\n"
            f"{MEMORY_PLUS_OPEN}\n"
            '{"should_write": true, "notes": ["Dad prefers memory+ for light tasks"], "open_items": []}\n'
            f"{MEMORY_PLUS_CLOSE}",
        )
    )

    notepad = (workspace / "memory" / "memory_plus_notepad.md").read_text(encoding="utf-8")
    assert visible == "Done."
    assert result.fallback_reason == "memory_plus"
    assert "Dad prefers memory+ for light tasks" in notepad
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
