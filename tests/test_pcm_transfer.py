from __future__ import annotations

import os
from pathlib import Path

import pytest

from orchestrator.pcm_transfer import build_agent_continuity_plan


def test_agent_continuity_plan_is_exact_and_does_not_guess_by_name(tmp_path: Path):
    workspace = tmp_path / "workspace"
    wiki = workspace / "memory" / "memory_plus_wiki" / "topic"
    wiki.mkdir(parents=True)
    expected = {
        "agent.md",
        "bridge_memory.sqlite",
        "transcript.jsonl",
        "core_transcript.jsonl",
        "audit_transcript.jsonl",
        "recent_context.jsonl",
        "sys_prompts.json",
        "state.json",
        "post_turn_observers.json",
        "memory/memory_plus_state.json",
        "memory/memory_plus_index.json",
        "memory/memory_plus_notepad.md",
        "memory/memory_plus_wiki/topic/page.md",
    }
    for relative in expected:
        path = workspace / Path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative, encoding="utf-8")
    for relative in (
        "MEMORY.md",
        "memory/continuity.md",
        "random_transcript.jsonl",
        "memory_plus_state.json",
        "handoff.md",
    ):
        path = workspace / Path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not explicit continuity state", encoding="utf-8")

    plan = build_agent_continuity_plan(workspace)

    assert {item.relative_path for item in plan.items} == expected
    assert {item.relative_path for item in plan.items if item.is_control} == {
        "agent.md"
    }


@pytest.mark.skipif(os.name == "nt", reason="symlink creation is not reliable on Windows")
def test_agent_continuity_plan_never_follows_symlinks(tmp_path: Path):
    workspace = tmp_path / "workspace"
    archive = workspace / "memory" / "memory_plus_wiki"
    archive.mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text("private", encoding="utf-8")
    (archive / "linked.md").symlink_to(outside)

    plan = build_agent_continuity_plan(workspace)

    assert not plan.items
    assert {
        "path": "memory/memory_plus_wiki/linked.md",
        "reason": "continuity_symlink_not_followed",
    } in [item.as_dict() for item in plan.excluded]
