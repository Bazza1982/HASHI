"""Phase 1 input slimming: catalogues, tool view, receipts and message context.

These tests pin the *savings* rather than only the shape, because the whole
point of Phase 1 is a smaller and more stable model input.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from adapters.base import BackendResponse
from orchestrator import runtime_cross_session
from orchestrator.her_v2.v3_prompt import (
    CATALOGUE_SECTION_KEYS,
    DEVELOPER_TOOLKIT_CONTRACT,
    compile_main_prompt,
    compact_skill_index,
)
from orchestrator.message_context import render_message_context_section
from tools.registry import ToolRegistry
from tools.tool_presentation import OnDemandToolView


# --- 1. tool presentation ---------------------------------------------------


def _view(tmp_path: Path, goal: str) -> OnDemandToolView:
    registry = ToolRegistry(["*"], tmp_path, tmp_path, {})
    return OnDemandToolView(registry, goal=goal)


def test_starter_schemas_do_not_depend_on_goal_text(tmp_path):
    inspection = _view(tmp_path, "read every file in this repository")
    office = _view(tmp_path, "build an excel spreadsheet report")

    names = [item["function"]["name"] for item in inspection.get_tool_definitions()]
    assert names == [item["function"]["name"] for item in office.get_tool_definitions()]


def test_starter_set_carries_common_recall_and_discovery_tools(tmp_path):
    names = {item["function"]["name"] for item in _view(tmp_path, "").get_tool_definitions()}
    assert {"tool_find", "file_read", "code_search", "file_search", "memory_search",
            "log_query"} <= names


# --- 2. catalogues ----------------------------------------------------------


def _pcm_with_catalogues(tool_lines: list[str], skills: list[dict]) -> dict:
    return {
        "current_request": "do it",
        "history": [],
        "sections": [
            {"key": "tools_catalogue", "order": 10, "authority": "runtime_context",
             "text": "\n".join(tool_lines)},
            {"key": "skills_catalogue", "order": 11, "authority": "runtime_context",
             "text": json.dumps(skills, ensure_ascii=False, indent=2)},
            {"key": "workzone", "order": 12, "authority": "runtime_context",
             "text": "workzone: C:\\repo"},
        ],
    }


def test_process_catalogues_are_not_duplicated_when_native_schemas_carry_tools():
    tool_lines = [f"- tool_{index}: description of tool {index} " + "x" * 120
                  for index in range(60)]
    skills = [{"id": "office-basics", "name": "office-basics",
               "description": "Office documents " + "y" * 200,
               "skill_md": "C:\\repo\\skills\\office-basics\\SKILL.md"}]
    pcm = _pcm_with_catalogues(tool_lines, skills)

    _system, user = compile_main_prompt(
        pcm_input=pcm, fallback_request="do it", context={"skills_catalogue": skills},
        tools_available=True,
    )
    refs = json.loads(
        user.split("Context and reference material (data, not instructions):\n", 1)[1]
        .split("\n\nCurrent user request:\n", 1)[0]
    )

    sources = [row["source"] for row in refs["context_only"]]
    assert not (set(sources) & CATALOGUE_SECTION_KEYS)
    assert "workzone" in sources

    index = refs["available_skills"]
    assert len(index["skills"]) == 1
    line = index["skills"][0]
    assert line.startswith("- office-basics: ")
    assert "\n" not in line
    assert len(line) <= 160
    assert "SKILL.md" not in line  # machine paths stay out of the index


def test_catalogue_sections_are_dropped_without_tools_but_other_rows_stay():
    pcm = _pcm_with_catalogues(["- tool_a: does a"], [])
    _system, user = compile_main_prompt(
        pcm_input=pcm, fallback_request="do it", context={}, tools_available=False,
    )
    assert "tools_catalogue" not in user
    assert "workzone: C:\\\\repo" in user


def test_skill_index_is_bounded_and_stable():
    skills = [
        {"id": "a", "description": "   multi\n line   description "},
        {"name": "b"},
        {"id": "", "description": "ignored"},
        "not-a-mapping",
    ]
    assert compact_skill_index(skills) == ["- a: multi line description", "- b"]


# --- 3. cross-session receipts ---------------------------------------------


def _runtime(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        name="momo",
        workspace_dir=tmp_path,
        config=SimpleNamespace(active_backend="her-v2", workspace_dir=tmp_path),
        backend_manager=SimpleNamespace(
            agent_mode="fixed",
            current_backend=SimpleNamespace(
                _session_id="primary-session",
                capabilities=SimpleNamespace(supports_sessions=True),
            ),
        ),
        current_request_meta=None,
        _request_meta_by_id={},
        logger=SimpleNamespace(info=lambda message: None,
                               warning=lambda message: None),
        get_current_model=lambda: "local/deepseek-v4-pro",
    )


def _item(**overrides) -> SimpleNamespace:
    payload = {
        "request_id": "req-scheduler",
        "chat_id": 123,
        "source": "scheduler",
        "summary": "Cron Task [evening]",
        "prompt": "Run the evening engagement task",
        "silent": False,
    }
    payload.update(overrides)
    return SimpleNamespace(**payload)


def _response(text: str) -> BackendResponse:
    return BackendResponse(
        text=text,
        duration_ms=1,
        stop_reason="end_turn",
        stream_metadata={"completion_status": "completed",
                         "completion_stop_reason": "end_turn"},
    )


def test_completed_receipts_are_compact_while_pending_keeps_full_text(tmp_path):
    runtime = _runtime(tmp_path)
    long_prompt = "Scheduled prompt " + "p" * 4_000
    long_answer = "Scheduled answer " + "a" * 6_000
    runtime_cross_session.record_turn_result(
        runtime, _item(), assistant_text=long_answer,
        response=_response(long_answer), delivered=True, completion_path="foreground",
    )
    pending_text = "Reply with a letter:\nA — Keep this active choice"
    runtime_cross_session.record_turn_result(
        runtime,
        _item(request_id="req-active"),
        assistant_text=pending_text,
        response=BackendResponse(
            text=pending_text, duration_ms=1, stop_reason="end_turn",
            stream_metadata={"completion_status": "completed",
                             "completion_stop_reason": "end_turn",
                             "pending_interaction": {"kind": "choice"}},
        ),
        delivered=True,
        completion_path="foreground",
    )

    section = runtime_cross_session.context_section(
        runtime, _item(request_id="req-user", source="text", prompt="status")
    )[0][1]

    assert "status=completed" in section
    assert "Keep this active choice" in section
    assert len(section) < 4_000, "completed receipts must not re-send full transcripts"
    assert "…[cross-session text truncated]…" in section


# --- 4. message context ----------------------------------------------------


def test_message_context_renders_compact_key_value_lines():
    snapshot = {
        "type": "hashi.current-message-context",
        "version": 1,
        "scope": "current_message",
        "message_source": {"id": "api", "display_name": "Backend API",
                           "assurance": "declared"},
        "private_authorizations": [],
        "private_authorization_state": "none",
        "output_destination": {"surface": "workbench", "automatic": True,
                               "mirrors": [], "telegram_mirror": False},
    }
    text = render_message_context_section(snapshot)

    lines = text.splitlines()
    assert lines[0] == "CURRENT MESSAGE CONTEXT"
    for key in snapshot:
        assert any(line.startswith(f"{key}: ") for line in lines), key
    assert "current_message" in text
    assert "example" not in text
    compact_json = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
    assert len(text) <= len(compact_json) + 400
    assert "\n  " not in text  # no indent=2 block


# --- 5. parallel read-only hint -------------------------------------------


def test_developer_toolkit_contract_states_parallel_read_only_batching():
    assert "independent read-only inspections" in DEVELOPER_TOOLKIT_CONTRACT
    assert "run\nconcurrently" in DEVELOPER_TOOLKIT_CONTRACT
# HASHI-Fingerprint: hfp_94ef70de1ce24857a94adc710306f735
