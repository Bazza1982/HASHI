from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.hcc import HCC_USAGE_PROMPT
from orchestrator.pcm import render_pcm_document
from orchestrator.pcm_voice_projection import build_live_voice_input
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


def _pcm_payload(*, system: str = "SYSTEM_SENTINEL must govern the call.", hcc: str = "HCC_SENTINEL", memory: str = "MEMORY_SENTINEL", background: str = "") -> dict:
    sections = [
        {"key": "permanent_system", "title": "PERMANENT SYSTEM INSTRUCTIONS", "text": system, "authority": "permanent_system"},
        {"key": "instance_global_sys", "title": "INSTANCE-GLOBAL /sys", "text": "GLOBAL_SYS_SENTINEL", "authority": "global_system"},
        {"key": "agent_local_sys", "title": "AGENT-LOCAL /sys", "text": "LOCAL_SYS_SENTINEL", "authority": "local_system"},
        {"key": "hcc_usage", "title": "HCC USAGE INSTRUCTIONS", "text": HCC_USAGE_PROMPT, "authority": "local_system"},
        {"key": "hcc", "title": "HASHI CONTEXT CACHE", "text": hcc, "authority": "runtime_context"},
        {"key": "permanent_memory", "title": "LONG-TERM MEMORY FROM agent.md", "text": memory, "authority": "memory"},
        {"key": "memory_plus_continuity", "title": "Memory+ Continuity", "text": "MEMORY_PLUS_SENTINEL", "authority": "runtime_context"},
        {"key": "persona", "title": "CURRENT PRESENTATION PERSONA", "text": "You are Moon. Address the user respectfully.", "authority": "persona"},
    ]
    if background:
        sections.insert(
            -1,
            {"key": "recent_background_results", "title": "RECENT COMPLETED BACKGROUND RESULTS", "text": background, "authority": "runtime_context"},
        )
    return {"transport_snapshot": {"version": 1, "sections": sections}}


def test_defaults_resolve_full_authoritative_pcm_and_history(tmp_path: Path):
    manager = PhoneManager(_workspace(tmp_path))
    recent = [
        {"message_id": "m1", "history_unit_id": "r1", "role": "user", "text": "What changed today?"},
        {"message_id": "m2", "history_unit_id": "r1", "role": "assistant", "text": "The phone UI was fixed."},
    ]
    resolved = manager.resolve_live_session(
        display_name="Moon",
        pcm_payload=_pcm_payload(background="BACKGROUND_RESULT_SENTINEL"),
        recent_history=recent,
    )

    assert resolved["provider"] == "openai"
    assert resolved["model"] == "gpt-live-1"
    assert resolved["voice"] == "marin"
    assert resolved["public"]["persona_projected"] is True
    assert resolved["public"]["revision"]
    assert "You are Moon" in resolved["instructions"]
    assert "SYSTEM_SENTINEL" in resolved["instructions"]
    assert "GLOBAL_SYS_SENTINEL" in resolved["instructions"]
    assert "LOCAL_SYS_SENTINEL" in resolved["instructions"]
    assert HCC_USAGE_PROMPT in resolved["instructions"]
    assert "MEMORY_SENTINEL" not in resolved["instructions"]
    assert "HCC_SENTINEL" not in resolved["instructions"]
    input_text = json.dumps(resolved["input"], ensure_ascii=False)
    assert "HCC_SENTINEL" in input_text
    assert "MEMORY_SENTINEL" in input_text
    assert "MEMORY_PLUS_SENTINEL" in input_text
    assert "BACKGROUND_RESULT_SENTINEL" in input_text
    assert "What changed today?" in input_text
    assert "The phone UI was fixed." in input_text
    assert [item["role"] for item in resolved["input"][-2:]] == ["user", "assistant"]
    assert resolved["context_audit"]["provider_exact_count_required"] is True
    assert resolved["context_audit"]["required_message_count"] == 3
    assert resolved["context_audit"]["history_unit_message_counts"] == [1, 2]



def test_live_input_keeps_hcc_and_newest_complete_exchange_then_drops_oldest():
    recent = []
    for index in range(90):
        unit = f"round-{index}"
        recent.extend(
            [
                {"message_id": f"u-{index}", "history_unit_id": unit, "role": "user", "text": f"USER-{index}-" + ("x" * 120)},
                {"message_id": f"a-{index}", "history_unit_id": unit, "role": "assistant", "text": f"ASSISTANT-{index}-" + ("y" * 120)},
            ]
        )
    items, audit = build_live_voice_input(_pcm_payload(hcc="FULL_HCC_SENTINEL"), recent)
    encoded = json.dumps(items, ensure_ascii=False)
    assert "FULL_HCC_SENTINEL" in encoded
    assert "USER-89-" in encoded and "ASSISTANT-89-" in encoded
    assert "USER-0-" not in encoded and "ASSISTANT-0-" not in encoded
    assert len(items) <= 128
    assert audit["provider_exact_count_required"] is True
    assert audit["required_message_count"] + sum(
        audit["history_unit_message_counts"]
    ) == len(items)
    assert audit["history_omitted_units"] > 0


def test_live_input_uses_full_message_capacity_before_provider_exact_count():
    recent = []
    for index in range(90):
        unit = f"round-{index}"
        recent.extend(
            [
                {"message_id": f"u-{index}", "history_unit_id": unit, "role": "user", "text": "U" + ("x" * 120)},
                {"message_id": f"a-{index}", "history_unit_id": unit, "role": "assistant", "text": "A" + ("y" * 120)},
            ]
        )

    items, audit = build_live_voice_input(
        _pcm_payload(hcc="FULL_HCC_SENTINEL"),
        recent,
        token_count=len,
    )

    assert len(items) == 127
    assert audit["provider_tokens_limit"] == 8_192
    assert audit["provider_exact_count_required"] is True
    assert "tokens_est_budget" not in audit
    assert "token_estimator_reserve" not in audit


def test_live_input_preserves_full_hcc_for_provider_exact_count():
    items, audit = build_live_voice_input(_pcm_payload(hcc="汉" * 13000), [])
    assert "汉" * 13000 in json.dumps(items, ensure_ascii=False)
    assert audit["required_message_count"] == 3
    assert audit["provider_exact_count_required"] is True


def test_instruction_limit_is_token_based_not_old_character_cap(tmp_path: Path):
    manager = PhoneManager(_workspace(tmp_path))
    resolved = manager.resolve_live_session(
        display_name="Moon",
        pcm_payload=_pcm_payload(system="S" * 20000),
    )
    assert len(resolved["instructions"]) > 14000


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
    assert second["public"]["voice_presentation"] == "feminine"
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


def test_recovery_freezes_phone_choices_before_reprojecting_current_authority(tmp_path: Path):
    workspace = _workspace(tmp_path)
    manager = PhoneManager(workspace)
    manager.set_language("zh-CN")
    manager.set_style_instructions("ORIGINAL_PHONE_STYLE")
    first = manager.resolve_live_session(display_name="Moon", pcm_payload=_pcm_payload())
    manager.set_voice("willow")
    manager.set_language("en")
    manager.set_style_instructions("NEXT_CALL_PHONE_STYLE")

    recovered = manager.resolve_live_session(
        display_name="Moon Prime",
        pcm_payload=_pcm_payload(system="CURRENT_AUTHORITY_MUST_APPLY"),
        frozen_selection=first["selection"],
    )
    assert recovered["voice"] == "marin"
    assert recovered["public"]["language"] == "zh-CN"
    assert "ORIGINAL_PHONE_STYLE" in recovered["instructions"]
    assert "NEXT_CALL_PHONE_STYLE" not in recovered["instructions"]
    assert "CURRENT_AUTHORITY_MUST_APPLY" in recovered["instructions"]
    assert "Moon Prime" in recovered["instructions"]
    assert manager.get_state()["voice"] == "willow"
    assert "selection" not in recovered["public"]


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


def test_voice_presentations_use_only_official_metadata(tmp_path: Path):
    manager = PhoneManager(_workspace(tmp_path))

    assert manager.voice_presentation("marin") is None
    assert manager.voice_presentation("quartz") == "feminine"
    assert manager.voice_presentation("ripple") == "masculine"
    assert set(manager.VOICE_PRESENTATIONS) == set(manager.VOICE_LABELS) - {"marin"}
    assert set(manager.VOICE_PRESENTATIONS.values()) == {"feminine", "masculine"}
