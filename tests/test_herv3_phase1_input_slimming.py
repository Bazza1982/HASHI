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
from orchestrator.context_compaction import HistorySnapshot, install_history_section
from orchestrator.her_v2.v3_prompt import (
    CATALOGUE_SECTION_KEYS,
    DEVELOPER_TOOLKIT_CONTRACT,
    SKILL_INDEX_NOTE,
    SKILL_INDEX_NOTE_EXPLICIT,
    compile_main_prompt,
    compact_skill_index,
    skill_index_payload,
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


def test_process_catalogues_are_not_duplicated_when_native_schemas_carry_tools(
    tmp_path: Path,
):
    tool_lines = [f"- tool_{index}: description of tool {index} " + "x" * 120
                  for index in range(60)]
    skill_md = tmp_path / "skills" / "office-basics" / "SKILL.md"
    skills = [{"id": "office-basics", "name": "office-basics",
               "description": "Office documents " + "y" * 200,
               "skill_md": str(skill_md)}]
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
    # The description is bounded; the real file location is still reachable.
    assert line.endswith(f"[read {skill_md}]")
    assert index["note"] == SKILL_INDEX_NOTE_EXPLICIT


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


def test_skill_index_sends_one_accurate_root_instead_of_n_per_skill_paths(
    tmp_path: Path,
):
    root = tmp_path / "skills"
    skills = [
        {"id": "alpha", "description": "does alpha",
         "skill_md": str(root / "alpha" / "SKILL.md")},
        {"id": "beta", "description": "does beta",
         "skill_md": str(root / "beta" / "SKILL.md")},
    ]
    payload = skill_index_payload(skills)

    assert payload["root"] == str(root)
    assert payload["note"] == SKILL_INDEX_NOTE
    assert payload["skills"] == ["- alpha: does alpha", "- beta: does beta"]
    # One root plus two short lines: far below the full catalogue it replaces.
    assert sum(len(line) for line in payload["skills"]) < 240


def test_skill_index_keeps_explicit_paths_when_the_shared_root_does_not_fit(
    tmp_path: Path,
):
    shared = tmp_path / "skills" / "alpha" / "SKILL.md"
    elsewhere = tmp_path / "vendor" / "beta-skill" / "SKILL.md"
    payload = skill_index_payload(
        [
            {"id": "alpha", "skill_md": str(shared)},
            {"id": "beta", "skill_md": str(elsewhere)},
        ]
    )

    assert "root" not in payload
    assert payload["note"] == SKILL_INDEX_NOTE_EXPLICIT
    assert payload["skills"] == [
        f"- alpha [read {shared}]",
        f"- beta [read {elsewhere}]",
    ]


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


def _record(runtime, request_id: str, prompt: str, answer: str, *, labels=None):
    metadata = {
        "completion_status": "completed",
        "completion_stop_reason": "end_turn",
    }
    if labels:
        metadata["pending_interaction"] = {"kind": "choice", "labels": labels}
    runtime_cross_session.record_turn_result(
        runtime,
        _item(request_id=request_id, prompt=prompt),
        assistant_text=answer,
        response=BackendResponse(
            text=answer, duration_ms=1, stop_reason="end_turn",
            stream_metadata=metadata,
        ),
        delivered=True,
        completion_path="foreground",
    )


def _snapshot(recent_exchanges: int = 6) -> HistorySnapshot:
    return HistorySnapshot(
        generation=1,
        active_capsule_hash="",
        active_capsule_ref="",
        active_capsule=None,
        active_record=None,
        covered_through_turn_id=0,
        all_turns=(),
        eligible_turns=(),
        recent_turns=(),
        source_digest="",
        recent_exchanges=recent_exchanges,
    )


def _herv3_history(runtime, item, *, primary=()) -> str:
    """Render history through install_history_section, as runtime_pipeline does."""

    runtime._context_compaction_test_coordinator = SimpleNamespace(
        snapshot=lambda: _snapshot()
    )
    sections, _installed = install_history_section(
        runtime,
        [],
        cross_session_entries=runtime_cross_session.timeline_entries(runtime, item),
        primary_timeline_entries=list(primary),
    )
    return sections[0][1]


def test_herv3_history_compacts_ended_receipts_and_keeps_active_ones_full(tmp_path):
    runtime = _runtime(tmp_path)
    _record(
        runtime, "req-done",
        "Scheduled prompt " + "p" * 4_000,
        "ANSWER-HEAD " + "a" * 6_000 + " ANSWER-TAIL",
    )
    # An active receipt has to be longer than the compact budget, otherwise
    # "kept the full text" would pass even if the budget were applied to it.
    active_answer = (
        "Reply with a letter:\n"
        "A — keep this active choice\n"
        "B — drop it\n"
        + "c" * 1_500
        + "\nACTIVE-ANSWER-TAIL"
    )
    _record(
        runtime, "req-active",
        "Active prompt " + "q" * 1_200,
        active_answer,
        labels=["A", "B"],
    )

    rendered = _herv3_history(
        runtime, _item(request_id="req-now", source="text", prompt="status")
    )

    assert "status=completed" in rendered
    assert rendered.count("…[cross-session text truncated]…") == 2
    assert "ANSWER-TAIL" in rendered
    assert "p" * 1_000 not in rendered
    assert "a" * 2_000 not in rendered

    assert "status=awaiting_user" in rendered
    assert active_answer in rendered
    assert len(rendered) < 6_000


def test_herv3_history_dedupes_receipts_before_trimming(tmp_path):
    runtime = _runtime(tmp_path)
    item = _item(request_id="req-now", source="text", prompt="status")
    user = "Long scheduled prompt " + "u" * 1_500
    answer = "Long scheduled answer " + "s" * 2_000
    _record(runtime, "req-dup", user, answer)

    entries = runtime_cross_session.timeline_entries(runtime, item)
    assert len(entries) == 1
    receipt = entries[0]

    runtime._context_compaction_test_coordinator = SimpleNamespace(
        snapshot=lambda: _snapshot()
    )
    sections, _installed = install_history_section(
        runtime,
        [],
        cross_session_entries=entries,
        primary_timeline_entries=[
            {
                "kind": "primary_exchange",
                "exchange_id": 7,
                "turn_ids": (11, 12),
                "sequence": 7,
                "completed_at": receipt["completed_at"],
                "source": "scheduler",
                "user_text": user,
                "assistant_text": answer,
                "rows": (
                    {"id": 11, "ts": "2026-11-25T10:00:00+11:00", "role": "user",
                     "source": "scheduler", "text": user},
                    {"id": 12, "ts": "2026-11-25T10:00:05+11:00",
                     "role": "assistant", "source": "scheduler", "text": answer},
                ),
                "receipt_entries": [],
            }
        ],
    )

    # De-duplication compares the untrimmed text: the receipt collapses into the
    # ledger exchange instead of being replayed as a second trimmed copy.
    rendered = sections[0][1] if sections else ""
    assert rendered
    assert rendered.count(user) == 1
    assert rendered.count(answer) == 1
    assert f"merged receipts={receipt['receipt_id']}" in rendered
    assert "…[cross-session text truncated]…" not in rendered


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
