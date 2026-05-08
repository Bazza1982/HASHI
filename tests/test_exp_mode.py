from orchestrator.exp_mode import build_exp_task_prompt, get_exp_usage_text
from orchestrator.admin_local_testing import supported_commands


def test_exp_prompt_lists_dictionary_and_task():
    prompt = build_exp_task_prompt("make council presentation slides")

    assert "EXP GUIDEBOOK REQUEST" in prompt
    assert "make council presentation slides" in prompt
    assert "barry/office_desktop" in prompt
    assert "powerpoint" in prompt
    assert "context-specific" in prompt


def test_exp_usage_mentions_command_and_available_exp():
    text = get_exp_usage_text()

    assert "/exp <task>" in text
    assert "barry/office_desktop" in text


def test_exp_is_supported_admin_command_when_runtime_has_handler():
    class Runtime:
        async def cmd_exp(self):
            pass

    assert "exp" in supported_commands(Runtime())
