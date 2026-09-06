import json
from pathlib import Path

from orchestrator.exp_mode import build_exp_task_prompt, get_exp_usage_text
from orchestrator.admin_local_testing import supported_commands


def _write_exp(tmp_path: Path) -> Path:
    root = tmp_path / "exp"
    domain = root / "sample-user" / "office_desktop"
    domain.mkdir(parents=True)
    (domain / "manifest.json").write_text(
        json.dumps(
            {
                "id": "sample-user/office_desktop",
                "type": "exp",
                "summary": "Context-specific office guidance.",
                "playbooks": {"powerpoint": "playbooks/powerpoint.exp.md"},
            }
        ),
        encoding="utf-8",
    )
    return root


def test_exp_prompt_lists_dictionary_and_task(tmp_path: Path):
    exp_root = _write_exp(tmp_path)
    prompt = build_exp_task_prompt(
        "make council presentation slides",
        exp_root=exp_root,
    )

    assert "EXP GUIDEBOOK REQUEST" in prompt
    assert "make council presentation slides" in prompt
    assert "sample-user/office_desktop" in prompt
    assert "powerpoint" in prompt
    assert "Context-specific" in prompt
    assert "asset-packs.json" in prompt
    assert "restore only the required" in prompt


def test_exp_usage_mentions_command_and_available_exp(tmp_path: Path):
    text = get_exp_usage_text(_write_exp(tmp_path))

    assert "<code>/exp &lt;task&gt;</code>" in text
    assert "sample-user/office_desktop" in text
    assert "install on demand" in text


def test_exp_is_supported_admin_command_when_runtime_has_handler():
    class Runtime:
        async def cmd_exp(self):
            pass

    assert "exp" in supported_commands(Runtime())
