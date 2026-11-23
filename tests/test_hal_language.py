"""Focused pure HAL language checks. No live HASHI/Workbench dependencies."""
import pytest

from hal import HALAction, HALEvent, HALSyntaxError, compile_hal, entry_matches, resolve_prompt


def test_wildcard_bound_prompt_and_quote_punctuation():
    program = compile_hal(
        'if @agent_any #turn_finish then @rika@hashi4 $prompt '
        '"@agent_trigger has finished; please review the work.".'
    )
    assert len(program.workflows) == 1
    rule = program.workflows[0][0]
    assert rule.action.kind == "prompt"
    event = HALEvent("#turn_finish", "@lily@hashi4")
    assert entry_matches(rule, event)
    assert resolve_prompt(rule.action, agent_trigger=event.agent_identity) == (
        "@lily@hashi4 has finished; please review the work."
    )
    assert not entry_matches(rule, HALEvent("#turn_error", "@lily@hashi4"))


def test_dependent_rules_do_not_become_independent_entries():
    program = compile_hal(
        "if #system_idle then /hashideploy;"
        "if #hashideploy_finish then /reboot max;"
        'if #reboot_ok then @lily $prompt "重启成功".'
    )
    assert len(program.workflows) == 1
    assert [rule.event for rule in program.workflows[0]] == [
        "#system_idle", "#hashideploy_finish", "#reboot_ok",
    ]
    assert [rule.terminator for rule in program.workflows[0]] == [";", ";", "."]
    assert entry_matches(program.workflows[0][0], HALEvent("#system_idle"))


def test_full_stop_creates_independent_workflows():
    p = compile_hal(
        'if @agent_any #turn_error then @lily $prompt "error".'
        "if #system_idle then /reboot max."
    )
    assert len(p.workflows) == 2


@pytest.mark.parametrize("script", [
    "if #system_idle then /reboot max",  # missing terminator
    "if #unknown then /reboot max.",
    'if @agent_trigger #turn_finish then @lily $prompt "x".',
    "if @agent_any #system_idle then /reboot max.",
    "if #system_idle then /reboot max;",
    'if #reboot_ok then @lily $prompt "yes" else @lily $prompt "no".',
    "if #system_idle then @lily $stop.",
    'if #system_idle then @agent_any $prompt "bad".',
])
def test_unsupported_or_incomplete_fails_explicitly(script):
    with pytest.raises(HALSyntaxError):
        compile_hal(script)


def test_prompt_literal_symbols_and_trigger_boundary():
    p = compile_hal(
        'if @agent_any #turn_commentary then @lily $prompt '
        '"Send $100 to @someone; tag #turn_error; @agent_trigger_extra".'
    )
    assert resolve_prompt(p.workflows[0][0].action, agent_trigger="@rika@hashi4") == (
        "Send $100 to @someone; tag #turn_error; @agent_trigger_extra"
    )
