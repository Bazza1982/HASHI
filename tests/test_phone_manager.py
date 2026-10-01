from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.hcc import HCC_USAGE_PROMPT
from orchestrator.pcm import render_pcm_document
from orchestrator.pcm_voice_projection import build_live_voice_input, build_phone_result_index
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
    assert resolved["context_audit"]["required_message_count"] == 4
    assert resolved["context_audit"]["history_unit_message_counts"] == [2]


def test_completed_activity_index_survives_history_capacity_pressure():
    recent = [
        {"message_id": f"m-{index}", "history_unit_id": f"r-{index}",
         "role": "assistant", "text": f"conversation {index}"}
        for index in range(8)
    ]
    items, audit = build_live_voice_input(
        _pcm_payload(background="TODAY_GMAIL_AND_NEWS_INDEX"), recent,
        message_limit=6,
    )
    text = json.dumps(items, ensure_ascii=False)
    assert "TODAY_GMAIL_AND_NEWS_INDEX" in text
    assert "conversation 7" in text
    assert audit["required_message_count"] == 4
    assert audit["optional_reference_omitted"] is False


def test_sunny_morning_baseline_survives_busy_conversation_at_opening(tmp_path):
    completed = [
        {"message_id": f"msg-older-{index}", "created_at": "2026-10-01T00:00:00Z",
         "session_kind": "agent_activity", "text": f"Older report {index}. " + "detail " * 140}
        for index in range(11)
    ]
    expected = {
        "msg-outlook": "Outlook: 13 messages",
        "msg-gmail": "Gmail: 36 messages",
        "msg-school": "School reconciliation: 16 of 16 modules",
        "msg-property": "Property monthly statement completed",
        "msg-news": "Morning news report completed",
    }
    completed.extend(
        {"message_id": result_id, "created_at": "2026-10-01T00:30:00Z",
         "session_kind": "agent_activity", "text": (
             "Today's two main themes are AI and storage.\n"
             "🔴 **今日重点**\n"
             "**1. Gemini** — model announcement.\n"
             "**2. Micron** — storage cycle.\n"
             "**3. Russia energy strike** — international update.\n"
             + "full detail " * 160
             if result_id == "msg-news" else summary + ". " + "full detail " * 160)}
        for result_id, summary in expected.items()
    )
    index = build_phone_result_index(completed)
    assert set(expected) <= set(index.included_message_ids)
    assert all(summary in index.text for result_id, summary in expected.items()
               if result_id != "msg-news")
    assert "Section 今日重点: 3 numbered entries." in index.text
    assert "1. Gemini" in index.text
    assert "3. Russia energy strike" in index.text
    assert "source excerpts" in index.text
    assert len(index.text) <= 9_000
    assert index.omitted_count == len(index.omitted_message_ids)
    assert set(index.included_message_ids).isdisjoint(index.omitted_message_ids)
    assert len(index.included_message_ids) + index.omitted_count == len(completed)

    history = [
        {"message_id": f"history-{number}", "history_unit_id": f"turn-{number}",
         "role": "assistant", "text": f"Recent conversation turn {number}"}
        for number in range(90)
    ]
    resolved = PhoneManager(_workspace(tmp_path)).resolve_live_session(
        display_name="Moon", pcm_payload=_pcm_payload(background=index.text),
        recent_history=history,
    )
    offered = json.dumps(resolved["input"], ensure_ascii=False)
    assert all(result_id in offered for result_id in expected)
    assert resolved["context_audit"]["required_message_count"] == 4



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


def test_live_input_keeps_completed_report_after_interleaved_call_speech():
    recent = [
        {"message_id": "query", "history_unit_id": "run-news", "role": "user", "text": "Find the news"},
        {"message_id": "during-1", "history_unit_id": "speech-1", "role": "user", "text": "Still searching?"},
        {"message_id": "during-2", "history_unit_id": "speech-2", "role": "assistant", "text": "I am checking"},
        {"message_id": "report", "history_unit_id": "run-news", "role": "assistant", "text": "NEWS REPORT: 23 items"},
        {"message_id": "after-1", "history_unit_id": "speech-3", "role": "user", "text": "Tell me all of them"},
        {"message_id": "after-2", "history_unit_id": "speech-4", "role": "assistant", "text": "I found eight"},
    ]

    items, audit = build_live_voice_input(_pcm_payload(), recent, message_limit=6)

    assert [item["text"] for item in items[-3:]] == [
        "NEWS REPORT: 23 items", "Tell me all of them", "I found eight",
    ]
    assert audit["history_unit_message_counts"] == [1, 1, 1]


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
