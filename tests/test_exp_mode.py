from __future__ import annotations

import json

from orchestrator.exp_mode import build_exp_task_prompt, get_exp_usage_text


def _write_exp_fixture(root):
    exp_dir = root / "barry" / "office_desktop"
    exp_dir.mkdir(parents=True)
    (exp_dir / "manifest.json").write_text(
        json.dumps(
            {
                "id": "barry/office_desktop",
                "type": "exp",
                "summary": "Office desktop expertise.",
                "playbooks": {
                    "powerpoint": "playbooks/powerpoint.exp.md",
                },
            }
        ),
        encoding="utf-8",
    )


def test_exp_prompt_lists_dictionary_and_task(tmp_path):
    _write_exp_fixture(tmp_path)

    prompt = build_exp_task_prompt("make council presentation slides", exp_root=tmp_path)

    assert "EXP GUIDEBOOK REQUEST" in prompt
    assert "make council presentation slides" in prompt
    assert "barry/office_desktop" in prompt
    assert "powerpoint" in prompt
    assert "context-specific" in prompt


def test_exp_usage_mentions_command_and_available_exp(tmp_path):
    _write_exp_fixture(tmp_path)

    text = get_exp_usage_text(exp_root=tmp_path)

    assert "/exp <task>" in text
    assert "barry/office_desktop" in text
