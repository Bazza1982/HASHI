from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.pcm import render_pcm_document
from orchestrator.phone_manager import PhoneConfigError, PhoneManager


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "agent-one"
    workspace.mkdir()
    (workspace / "agent.md").write_text(
        render_pcm_document(
            persona="You are Moon. Address the user respectfully and speak with quiet confidence.",
            system="SYSTEM_SENTINEL must never leave HASHI.",
            memory="MEMORY_SENTINEL must never leave HASHI.",
            hcc="HCC_SENTINEL must never leave HASHI.",
        ),
        encoding="utf-8",
    )
    return workspace


def test_defaults_resolve_safe_persona_only(tmp_path: Path):
    manager = PhoneManager(_workspace(tmp_path))
    resolved = manager.resolve_live_session(display_name="Moon")

    assert resolved["provider"] == "openai"
    assert resolved["model"] == "gpt-live-1"
    assert resolved["voice"] == "marin"
    assert resolved["public"]["persona_projected"] is True
    assert resolved["public"]["revision"]
    assert "You are Moon" in resolved["instructions"]
    assert "SYSTEM_SENTINEL" not in resolved["instructions"]
    assert "MEMORY_SENTINEL" not in resolved["instructions"]
    assert "HCC_SENTINEL" not in resolved["instructions"]
    assert "HIGHEST PRIORITY" in resolved["instructions"]
    assert resolved["instructions"].endswith(
        "Discuss freely; propose actions explicitly; never represent a proposed or delegated action as completed without a reliable HASHI result."
    )


def test_phone_settings_are_separate_persistent_and_change_revision(tmp_path: Path):
    workspace = _workspace(tmp_path)
    manager = PhoneManager(workspace)
    first = manager.resolve_live_session(display_name="Moon")

    manager.set_voice("willow")
    manager.set_language("zh-CN")
    manager.set_style("warm")
    manager.set_style_instructions("语速稍慢，停顿自然。")

    reloaded = PhoneManager(workspace)
    state = reloaded.get_state()
    second = reloaded.resolve_live_session(display_name="Moon")
    assert state["voice"] == "willow"
    assert state["language"] == "zh-CN"
    assert state["style"] == "warm"
    assert state["style_instructions"] == "语速稍慢，停顿自然。"
    assert second["public"]["custom_style"] is True
    assert second["public"]["revision"] != first["public"]["revision"]
    assert not (workspace / "voice_state.json").exists()


def test_effective_identity_is_frozen_into_the_revision(tmp_path: Path):
    manager = PhoneManager(_workspace(tmp_path))
    first = manager.resolve_live_session(agent_id="agent-one", display_name="Moon")
    renamed = manager.resolve_live_session(agent_id="agent-one", display_name="Moon Prime")
    reassigned = manager.resolve_live_session(agent_id="agent-two", display_name="Moon")

    assert renamed["public"]["revision"] != first["public"]["revision"]
    assert reassigned["public"]["revision"] != first["public"]["revision"]
    assert "Moon Prime" in renamed["instructions"]
    assert "agent-two" in reassigned["instructions"]


def test_updates_preserve_unknown_future_fields(tmp_path: Path):
    workspace = _workspace(tmp_path)
    state = dict(PhoneManager.DEFAULT_STATE)
    state["future_extension"] = {"keep": True}
    (workspace / "phone_state.json").write_text(json.dumps(state), encoding="utf-8")
    PhoneManager(workspace).set_style("clear")
    saved = json.loads((workspace / "phone_state.json").read_text(encoding="utf-8"))
    assert saved["future_extension"] == {"keep": True}


def test_invalid_or_corrupt_state_fails_closed(tmp_path: Path):
    workspace = _workspace(tmp_path)
    (workspace / "phone_state.json").write_text('{"voice":"not-qualified"}', encoding="utf-8")
    with pytest.raises(PhoneConfigError) as caught:
        PhoneManager(workspace).resolve_live_session(display_name="Moon")
    assert caught.value.code == "phone_voice_unsupported"


def test_custom_style_is_bounded(tmp_path: Path):
    manager = PhoneManager(_workspace(tmp_path))
    with pytest.raises(PhoneConfigError) as caught:
        manager.set_style_instructions("x" * (manager.MAX_CUSTOM_INSTRUCTIONS_CHARS + 1))
    assert caught.value.code == "phone_style_instructions_too_long"


def test_every_exposed_voice_is_qualified_for_the_selected_model(tmp_path: Path):
    manager = PhoneManager(_workspace(tmp_path))
    exposed = {voice for voice, _label in manager.voice_options()}
    qualified = set(manager.PROVIDERS["openai"]["models"]["gpt-live-1"]["voices"])
    assert exposed == qualified
