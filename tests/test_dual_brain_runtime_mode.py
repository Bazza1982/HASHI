from __future__ import annotations

import json
import asyncio
import sys
import types
from pathlib import Path


HASHI_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HASHI_ROOT))
sys.modules.setdefault("edge_tts", types.SimpleNamespace(Communicate=object))

from orchestrator.dual_brain_mode import (  # noqa: E402
    DUAL_BRAIN_OBSERVER_FACTORY,
    DEFAULT_AFTER_ACTION_PROMPT,
    DEFAULT_LEFT_PROMPT,
    DualBrainObserver,
    LEGACY_DEFAULT_AFTER_ACTION_PROMPT,
    LEGACY_DEFAULT_AFTER_ACTION_PROMPT_V2,
    LEGACY_DEFAULT_LEFT_PROMPT,
    LEGACY_DEFAULT_LEFT_PROMPT_V2,
    dual_brain_block_with,
    ensure_dual_brain_observer,
    load_dual_brain_config,
)
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime  # noqa: E402
from orchestrator.post_turn_observer import TurnContextRequest, TurnObservationRequest  # noqa: E402
from orchestrator.runtime_mode import mode_keyboard  # noqa: E402


def test_mode_keyboard_contains_dual_brain_button() -> None:
    keyboard = mode_keyboard("dual-brain")
    labels = [button.text for row in keyboard.inline_keyboard for button in row]
    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]

    assert "✅ Dual Brain" in labels
    assert "tgl:mode:dual-brain" in callbacks


def test_ensure_dual_brain_observer_adds_factory(tmp_path: Path) -> None:
    changed = ensure_dual_brain_observer(tmp_path)
    config = json.loads((tmp_path / "post_turn_observers.json").read_text(encoding="utf-8"))

    assert changed is True
    assert config["observers"] == [{"factory": DUAL_BRAIN_OBSERVER_FACTORY, "enabled": True}]


def test_dual_brain_config_roundtrip() -> None:
    cfg = load_dual_brain_config({}, current_backend="codex-cli", current_model="gpt-5.5")
    block = dual_brain_block_with(
        cfg,
        left_backend="claude-cli",
        left_model="claude-sonnet-4-6",
        after_action_prompt="Keep useful continuity.",
    )
    loaded = load_dual_brain_config({"dual_brain": block}, current_backend="codex-cli", current_model="gpt-5.5")

    assert loaded.left_backend == "claude-cli"
    assert loaded.left_model == "claude-sonnet-4-6"
    assert loaded.right_backend == "codex-cli"
    assert loaded.right_model == "gpt-5.5"
    assert loaded.after_action_prompt == "Keep useful continuity."


def test_dual_brain_legacy_default_prompts_migrate_to_clearer_defaults() -> None:
    for legacy_left_prompt in (LEGACY_DEFAULT_LEFT_PROMPT, LEGACY_DEFAULT_LEFT_PROMPT_V2):
        loaded = load_dual_brain_config(
            {
                "dual_brain": {
                    "prompts": {
                        "left": legacy_left_prompt,
                        "after_action": LEGACY_DEFAULT_AFTER_ACTION_PROMPT,
                    }
                }
            }
        )

        assert loaded.left_prompt == DEFAULT_LEFT_PROMPT
        assert loaded.after_action_prompt == DEFAULT_AFTER_ACTION_PROMPT

    for legacy_after_prompt in (LEGACY_DEFAULT_AFTER_ACTION_PROMPT, LEGACY_DEFAULT_AFTER_ACTION_PROMPT_V2):
        loaded = load_dual_brain_config(
            {
                "dual_brain": {
                    "prompts": {
                        "after_action": legacy_after_prompt,
                    }
                }
            }
        )

        assert loaded.after_action_prompt == DEFAULT_AFTER_ACTION_PROMPT


def test_dual_brain_menu_uses_backend_then_model_steps() -> None:
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)

    class Config:
        allowed_backends = [
            {"engine": "codex-cli"},
            {"engine": "openrouter-api"},
        ]

    runtime.config = Config()
    cfg = load_dual_brain_config(
        {
            "dual_brain": {
                "left_brain": {"backend": "codex-cli", "model": "gpt-5.4"},
                "right_brain": {
                    "backend": "openrouter-api",
                    "model": "anthropic/claude-sonnet-4.6",
                },
            }
        }
    )

    backend_keyboard = runtime._dual_brain_backend_keyboard(cfg, target="left")
    backend_callbacks = [button.callback_data for row in backend_keyboard.inline_keyboard for button in row]
    assert "bcfg:backend:left:codex-cli" in backend_callbacks
    assert "bcfg:backend:left:openrouter-api" in backend_callbacks

    model_keyboard = runtime._dual_brain_model_keyboard(cfg, target="right", backend="openrouter-api")
    model_callbacks = [button.callback_data for row in model_keyboard.inline_keyboard for button in row]
    assert "bcfg:modelidx:right:openrouter-api:0" in model_callbacks
    assert all("anthropic/claude-sonnet-4.6" not in str(callback) for callback in model_callbacks)


def test_dual_brain_status_labels_default_prompts_as_default() -> None:
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)

    class BackendManager:
        agent_mode = "dual-brain"

    runtime.backend_manager = BackendManager()
    cfg = load_dual_brain_config(
        {
            "dual_brain": {
                "left_brain": {"backend": "codex-cli", "model": "gpt-5.4"},
                "right_brain": {"backend": "codex-cli", "model": "gpt-5.5"},
            }
        }
    )

    text = runtime._dual_brain_status_text(cfg)

    assert "Memory briefing prompt: `default`" in text
    assert "Notepad update prompt: `default`" in text


def test_dual_brain_bypasses_automation_and_internal_sources(tmp_path: Path) -> None:
    workspace = tmp_path / "sakura"
    workspace.mkdir()
    (workspace / "state.json").write_text(json.dumps({"agent_mode": "dual-brain"}), encoding="utf-8")
    observer = DualBrainObserver(
        workspace_dir=workspace,
        backend_invoker=lambda *args, **kwargs: None,
        backend_context_getter=lambda: {"engine": "codex-cli", "model": "gpt-5.4"},
    )

    bypass_sources = [
        "startup",
        "system",
        "scheduler",
        "scheduler-retry",
        "scheduler-skill",
        "loop_skill",
        "retry",
        "session_reset",
        "bridge:hchat",
        "bridge-transfer:handoff",
        "hchat-reply:akane",
        "ticket:123",
        "cos-query:sakura",
    ]
    for source in bypass_sources:
        assert not observer.should_provide(source, is_bridge_request=False)
        assert not observer.should_observe(source, is_bridge_request=False)

    for source in ["text", "voice", "voice_transcript", "photo", "api"]:
        assert observer.should_provide(source, is_bridge_request=False)
        assert observer.should_observe(source, is_bridge_request=False)

    assert not observer.should_provide("text", is_bridge_request=True)
    assert not observer.should_observe("text", is_bridge_request=True)


def test_dual_brain_preflight_does_not_inject_wiki_unless_requested(tmp_path: Path) -> None:
    workspace = tmp_path / "sakura"
    workspace.mkdir()
    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir()
    (wiki_root / "old.md").write_text("old long term memory", encoding="utf-8")
    (workspace / "state.json").write_text(
        json.dumps(
            {
                "agent_mode": "dual-brain",
                "dual_brain": {
                    "left_brain": {"backend": "codex-cli", "model": "gpt-5.4"},
                    "right_brain": {"backend": "codex-cli", "model": "gpt-5.4"},
                },
            }
        ),
        encoding="utf-8",
    )

    prompts: list[str] = []

    class Response:
        is_success = True
        error = ""
        text = json.dumps(
            {
                "useful": False,
                "wiki_needed": False,
                "wiki_query": "",
                "same_day_context": [],
                "open_items": [],
                "notes_for_executor": [],
                "sources": [],
                "confidence": 1.0,
            }
        )

    async def invoker(**kwargs):
        prompts.append(kwargs["prompt"])
        return Response()

    observer = DualBrainObserver(
        workspace_dir=workspace,
        backend_invoker=invoker,
        backend_context_getter=lambda: {"engine": "codex-cli", "model": "gpt-5.4"},
        options={"wiki_roots": [str(wiki_root)]},
    )

    asyncio.run(
        observer.build_context_sections(
            TurnContextRequest(
                request_id="r1",
                source="telegram",
                user_text="hello",
                model_name="gpt-5.4",
            )
        )
    )

    assert len(prompts) == 1
    assert "WIKI_CANDIDATES" not in prompts[0]
    artifact = json.loads((workspace / "memory" / "left_brain_artifacts" / "left_brain_preflight_latest.json").read_text(encoding="utf-8"))
    events = [
        json.loads(line)
        for line in (workspace / "memory" / "left_brain_artifacts" / "left_brain_events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert artifact["meta"]["wiki_used"] is False
    assert artifact["meta"]["wiki_candidates_loaded"] == 0
    assert events[-1]["stage"] == "preflight"
    assert events[-1]["request_id"] == "r1"


def test_dual_brain_after_action_respects_should_write_false(tmp_path: Path) -> None:
    workspace = tmp_path / "sakura"
    workspace.mkdir()
    (workspace / "state.json").write_text(
        json.dumps(
            {
                "agent_mode": "dual-brain",
                "dual_brain": {
                    "left_brain": {"backend": "codex-cli", "model": "gpt-5.4"},
                    "right_brain": {"backend": "codex-cli", "model": "gpt-5.4"},
                },
            }
        ),
        encoding="utf-8",
    )

    class Response:
        is_success = True
        error = ""
        text = json.dumps({"should_write": "false", "continuity_summary": "", "confidence": 1.0})

    async def invoker(**kwargs):
        return Response()

    observer = DualBrainObserver(
        workspace_dir=workspace,
        backend_invoker=invoker,
        backend_context_getter=lambda: {"engine": "codex-cli", "model": "gpt-5.4"},
    )

    asyncio.run(
        observer._run_after_action(
            TurnObservationRequest(
                request_id="r1",
                source="telegram",
                user_text="thanks",
                assistant_text="you are welcome",
                model_name="gpt-5.4",
            )
        )
    )

    continuity_file = workspace / "memory" / "left_brain_continuity.jsonl"
    artifact = json.loads((workspace / "memory" / "left_brain_artifacts" / "left_brain_after_action_latest.json").read_text(encoding="utf-8"))
    events = [
        json.loads(line)
        for line in (workspace / "memory" / "left_brain_artifacts" / "left_brain_events.jsonl").read_text(encoding="utf-8").splitlines()
    ]

    assert not continuity_file.exists()
    assert artifact["written_to_continuity"] is False
    assert events[-1]["stage"] == "after_action"
    assert events[-1]["written_to_continuity"] is False


def test_dual_brain_right_brain_success_clears_pending_without_continuity_write(tmp_path: Path) -> None:
    workspace = tmp_path / "sakura"
    workspace.mkdir()
    (workspace / "state.json").write_text(
        json.dumps(
            {
                "agent_mode": "dual-brain",
                "dual_brain": {
                    "left_brain": {"backend": "codex-cli", "model": "gpt-5.4"},
                    "right_brain": {"backend": "codex-cli", "model": "gpt-5.4"},
                },
            }
        ),
        encoding="utf-8",
    )
    observer = DualBrainObserver(
        workspace_dir=workspace,
        backend_invoker=lambda *args, **kwargs: None,
        backend_context_getter=lambda: {"engine": "codex-cli", "model": "gpt-5.4"},
    )
    started = TurnObservationRequest(
        request_id="r-ok",
        source="text",
        user_text="do the thing",
        assistant_text="",
        model_name="gpt-5.4",
        metadata={"final_prompt": "assembled prompt"},
    )

    observer.on_right_brain_started(started)
    pending_path = workspace / "memory" / "left_brain_artifacts" / "pending_right_brain" / "r-ok.json"
    assert pending_path.exists()

    observer.on_right_brain_completed(
        TurnObservationRequest(
            request_id="r-ok",
            source="text",
            user_text="do the thing",
            assistant_text="done",
            model_name="gpt-5.4",
            metadata={"completion_path": "foreground"},
        )
    )

    assert not pending_path.exists()
    assert not (workspace / "memory" / "left_brain_continuity.jsonl").exists()
    events = [
        json.loads(line)
        for line in (workspace / "memory" / "left_brain_artifacts" / "left_brain_events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [event["stage"] for event in events] == ["right_brain_started", "right_brain_completed"]


def test_dual_brain_interrupted_turn_writes_continuity_once(tmp_path: Path) -> None:
    workspace = tmp_path / "sakura"
    workspace.mkdir()
    (workspace / "state.json").write_text(
        json.dumps(
            {
                "agent_mode": "dual-brain",
                "dual_brain": {
                    "left_brain": {"backend": "codex-cli", "model": "gpt-5.4"},
                    "right_brain": {"backend": "codex-cli", "model": "gpt-5.4"},
                },
            }
        ),
        encoding="utf-8",
    )
    observer = DualBrainObserver(
        workspace_dir=workspace,
        backend_invoker=lambda *args, **kwargs: None,
        backend_context_getter=lambda: {"engine": "codex-cli", "model": "gpt-5.4"},
    )
    started = TurnObservationRequest(
        request_id="r-stop",
        source="text",
        user_text="long task",
        assistant_text="",
        model_name="gpt-5.4",
        metadata={"final_prompt": "assembled prompt"},
    )
    interrupted = TurnObservationRequest(
        request_id="r-stop",
        source="text",
        user_text="long task",
        assistant_text="",
        model_name="gpt-5.4",
        metadata={"reason": "user_stop", "error": "/stop received"},
    )

    observer.on_right_brain_started(started)
    observer.on_right_brain_interrupted(interrupted)
    observer.on_right_brain_interrupted(interrupted)

    continuity_rows = [
        json.loads(line)
        for line in (workspace / "memory" / "left_brain_continuity.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(continuity_rows) == 1
    row = continuity_rows[0]
    assert row["interrupted_turn"] is True
    assert row["interruption_reason"] == "user_stop"
    assert row["continuity_update"]["interruption"]["error"] == "/stop received"
    assert "recover" in row["continuity_update"]["open_items"][0]


def test_dual_brain_preflight_is_visible_when_think_or_verbose_enabled(tmp_path: Path) -> None:
    workspace = tmp_path / "sakura"
    workspace.mkdir()
    (workspace / "state.json").write_text(
        json.dumps(
            {
                "agent_mode": "dual-brain",
                "dual_brain": {
                    "left_brain": {"backend": "codex-cli", "model": "gpt-5.2"},
                    "right_brain": {"backend": "codex-cli", "model": "gpt-5.4"},
                },
            }
        ),
        encoding="utf-8",
    )

    class Response:
        is_success = True
        error = ""
        text = json.dumps(
            {
                "useful": True,
                "wiki_needed": False,
                "same_day_context": ["recent context"],
                "notes_for_executor": ["use the known workflow"],
                "confidence": 0.9,
            }
        )

    async def invoker(**kwargs):
        return Response()

    sent: list[dict[str, object]] = []
    transcripts: list[tuple[str, str, str]] = []

    class Handoff:
        def append_transcript(self, role: str, text: str, source: str = "text") -> None:
            transcripts.append((role, text, source))

    class Runtime:
        _verbose = False
        _think = True
        handoff_builder = Handoff()

        async def send_long_message(self, **kwargs):
            sent.append(kwargs)
            return 0.0, 1

    observer = DualBrainObserver(
        workspace_dir=workspace,
        backend_invoker=invoker,
        backend_context_getter=lambda: {"engine": "codex-cli", "model": "gpt-5.4"},
    )
    observer.attach_runtime(Runtime())

    asyncio.run(
        observer.build_context_sections(
            TurnContextRequest(
                request_id="r-visible",
                source="text",
                user_text="what happened?",
                model_name="gpt-5.4",
                chat_id=123,
            )
        )
    )

    assert sent
    assert sent[-1]["chat_id"] == 123
    assert sent[-1]["purpose"] == "left-brain-visible"
    assert "Left brain preflight" in str(sent[-1]["text"])
    assert "recent context" in str(sent[-1]["text"])
    assert transcripts[-1][0] == "thinking"
    assert transcripts[-1][2] == "think"


def test_dual_brain_after_action_is_not_visible_when_think_and_verbose_disabled(tmp_path: Path) -> None:
    workspace = tmp_path / "sakura"
    workspace.mkdir()
    (workspace / "state.json").write_text(
        json.dumps(
            {
                "agent_mode": "dual-brain",
                "dual_brain": {
                    "left_brain": {"backend": "codex-cli", "model": "gpt-5.2"},
                    "right_brain": {"backend": "codex-cli", "model": "gpt-5.4"},
                },
            }
        ),
        encoding="utf-8",
    )

    class Response:
        is_success = True
        error = ""
        text = json.dumps({"should_write": False, "continuity_summary": "no update", "confidence": 1.0})

    async def invoker(**kwargs):
        return Response()

    sent: list[dict[str, object]] = []

    class Runtime:
        _verbose = False
        _think = False

        async def send_long_message(self, **kwargs):
            sent.append(kwargs)
            return 0.0, 1

    observer = DualBrainObserver(
        workspace_dir=workspace,
        backend_invoker=invoker,
        backend_context_getter=lambda: {"engine": "codex-cli", "model": "gpt-5.4"},
    )
    observer.attach_runtime(Runtime())

    asyncio.run(
        observer._run_after_action(
            TurnObservationRequest(
                request_id="r-hidden",
                source="text",
                user_text="thanks",
                assistant_text="done",
                model_name="gpt-5.4",
                chat_id=123,
            )
        )
    )

    assert sent == []
