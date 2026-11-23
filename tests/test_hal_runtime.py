"""Pure HAL runtime engine semantics: no I/O, deterministic intents."""

from hal import compile_hal
from hal.event_source import HalTrigger
from hal.runtime import (
    plan_for_trigger,
    unavailable_wait_detail,
    validate_script,
)


def _trigger(
    *,
    event="#turn_finish",
    agent="lily",
    feed_event_id="evt-1",
    kind="run.completed",
    key=None,
    automation=False,
):
    return HalTrigger(
        event=event,
        agent_identity=f"@{agent}@hashi4" if agent else None,
        feed_event_id=feed_event_id,
        feed_kind=kind,
        run_idempotency_key=key,
        automation_origin=automation,
    )


def _enabled_rule(script, rule_id="r1"):
    return {
        "rule_id": rule_id,
        "revision": 1,
        "name": "test",
        "enabled": True,
        "program": compile_hal(script),
    }


def test_validate_maps_availability_truthfully():
    result = validate_script(
        'if @agent_any #turn_finish then @lily $prompt "ok".\n'
        'if #system_idle then @lily $prompt "idle".\n'
        'if @agent_any #turn_commentary then @lily $prompt "nope".\n'
        'if #hashideploy_finish then @lily $prompt "nope".\n'
        'if @agent_any #turn_finish then @lily $prompt "ok"; '
        'if #reboot_ok then @lily $prompt "later".\n'
    )
    assert result["ok"] is True
    availability = {
        stage["event"]: stage["event_available"]
        for workflow in result["workflows"]
        for stage in workflow["stages"]
    }
    assert availability["#turn_finish"] is True
    assert availability["#system_idle"] is True
    assert availability["#turn_commentary"] is False
    assert availability["#hashideploy_finish"] is False
    kinds = {d["kind"] for d in result["diagnostics"]}
    assert "entry_event_unavailable" in kinds
    assert "stage_event_unavailable" in kinds


def test_validate_syntax_error_reports_line():
    result = validate_script('if #system_idle then @lily $prompt "x"')
    assert result["ok"] is False
    assert result["error"]


def test_entry_match_creates_intent_with_trigger_binding():
    rule = _enabled_rule('if @agent_any #turn_finish then @lily $prompt "hi @agent_trigger".')
    creates, advances, hints = plan_for_trigger(_trigger(), [rule], [], [], set())
    assert len(creates) == 1 and not advances and not hints
    intent = creates[0]
    assert intent.trigger_agent == "@lily@hashi4"
    assert intent.directive.payload == "hi @lily@hashi4"
    assert intent.directive.kind == "prompt"
    assert intent.directive.target == "@lily"
    assert intent.directive.idempotency_key.startswith("hal:hx")


def test_wildcard_does_not_match_system_events():
    rule = _enabled_rule('if @agent_any #turn_finish then @lily $prompt "hi".')
    creates, _, _ = plan_for_trigger(_trigger(event="#system_idle", agent=None, kind="system_idle"), [rule], [], [], set())
    assert creates == []


def test_automation_origin_never_starts_an_entry():
    rule = _enabled_rule('if @agent_any #turn_finish then @lily $prompt "hi".')
    creates, _, _ = plan_for_trigger(
        _trigger(key="hal:hxabc:0", automation=True), [rule], [], [], set()
    )
    assert creates == []


def test_specific_source_requires_matching_agent():
    rule = _enabled_rule('if @zelda@hashi4 #turn_finish then @lily $prompt "hi".')
    creates, _, _ = plan_for_trigger(_trigger(agent="lily"), [rule], [], [], set())
    assert creates == []
    creates, _, _ = plan_for_trigger(_trigger(agent="zelda"), [rule], [], [], set())
    assert len(creates) == 1


def test_disabled_rules_never_match():
    rule = _enabled_rule('if @agent_any #turn_finish then @lily $prompt "hi".')
    rule["enabled"] = False
    creates, _, _ = plan_for_trigger(_trigger(), [rule], [], [], set())
    assert creates == []


def _exec(exec_id="hx1", stage=1, state="active", rule_id="r1", program=None, trigger_agent=None):
    return {
        "exec_id": exec_id,
        "rule_id": rule_id,
        "stage": stage,
        "state": state,
        "workflow_index": 0,
        "trigger_agent": trigger_agent,
        "program": program,
    }


def test_dependent_stage_requires_correlation():
    program = compile_hal(
        'if @agent_any #turn_finish then @lily $prompt "start"; '
        'if @agent_any #turn_finish then @bob $prompt "next".'
    )
    exec_row = _exec(stage=1, program=program)
    receipt = {"exec_id": "hx1", "stage": 0, "idempotency_key": "hal:hx1:0"}

    unrelated = _trigger(key="other-run")
    _, advances, _ = plan_for_trigger(unrelated, [], [exec_row], [receipt], set())
    assert advances == []

    correlated = _trigger(key="hal:hx1:0", automation=True)
    _, advances, _ = plan_for_trigger(correlated, [], [exec_row], [receipt], set())
    assert len(advances) == 1
    assert advances[0].directive.target == "@bob"
    assert advances[0].directive.payload == "next"


def test_dependent_system_event_never_auto_advances():
    program = compile_hal(
        'if #system_idle then /hashideploy; if #hashideploy_finish then /reboot max.'
    )
    exec_row = _exec(stage=1, program=program)
    idle_trigger = _trigger(event="#hashideploy_finish", agent=None, kind="system_idle")
    _, advances, _ = plan_for_trigger(idle_trigger, [], [exec_row], [], set())
    assert advances == []


def test_correlated_failure_hint_is_honest():
    program = compile_hal(
        'if @agent_any #turn_finish then @lily $prompt "start"; '
        'if @agent_any #turn_finish then @bob $prompt "next".'
    )
    exec_row = _exec(stage=1, program=program)
    receipt = {"exec_id": "hx1", "stage": 0, "idempotency_key": "hal:hx1:0"}
    failed = _trigger(event="#turn_error", key="hal:hx1:0", kind="run.failed", automation=True)
    _, advances, hints = plan_for_trigger(failed, [], [exec_row], [receipt], set())
    assert advances == []
    assert hints and "correlated run failed" in hints[0].detail


def test_consumed_event_is_not_replanned():
    program = compile_hal(
        'if @agent_any #turn_finish then @lily $prompt "start"; '
        'if @agent_any #turn_finish then @bob $prompt "next".'
    )
    exec_row = _exec(stage=1, program=program)
    receipt = {"exec_id": "hx1", "stage": 0, "idempotency_key": "hal:hx1:0"}
    correlated = _trigger(key="hal:hx1:0", automation=True, feed_event_id="evt-9")
    _, advances, _ = plan_for_trigger(correlated, [], [exec_row], [receipt], {("hx1", "evt-9")})
    assert advances == []


def test_unbound_trigger_prompt_becomes_unbound_directive():
    rule = _enabled_rule('if #system_idle then @lily $prompt "go @agent_trigger".')
    creates, _, _ = plan_for_trigger(
        _trigger(event="#system_idle", agent=None, kind="system_idle"), [rule], [], [], set()
    )
    assert len(creates) == 1
    assert creates[0].directive.unbound_reason
    assert "no Agent" in creates[0].directive.unbound_reason


def test_unavailable_wait_detail():
    assert unavailable_wait_detail("#turn_finish") is None
    assert "no reboot-completion" in unavailable_wait_detail("#reboot_ok")


def test_multi_workflow_entries_match_independently():
    rule = _enabled_rule(
        'if @agent_any #turn_finish then @lily $prompt "a".\n'
        'if @agent_any #turn_finish then @bob $prompt "b".\n'
    )
    creates, advances, _ = plan_for_trigger(_trigger(), [rule], [], [], set())
    assert not advances
    assert len(creates) == 2
    assert {c.workflow_index for c in creates} == {0, 1}
    assert {c.directive.target for c in creates} == {"@lily", "@bob"}


def test_multi_workflow_exec_id_and_dedupe_include_workflow_index():
    rule = _enabled_rule(
        'if @agent_any #turn_finish then @lily $prompt "a".\n'
        'if @agent_any #turn_finish then @bob $prompt "b".\n'
    )
    creates, _, _ = plan_for_trigger(_trigger(feed_event_id="evt-1"), [rule], [], [], set())
    w0, w1 = sorted(creates, key=lambda c: c.workflow_index)
    # Same rule + same event, two workflows: distinct exec ids, distinct dedupe keys.
    assert w0.exec_id != w1.exec_id
    assert w0.dedupe_key == ("r1", 0, "evt-1")
    assert w1.dedupe_key == ("r1", 1, "evt-1")


def test_directive_for_exec_stage_reconstructs():
    from hal.runtime import directive_for_exec_stage

    program = compile_hal(
        'if @agent_any #turn_finish then @lily $prompt "start @agent_trigger"; '
        'if @agent_any #turn_finish then @bob $prompt "next".'
    )
    import json as _json
    from hal.runtime import program_to_json

    exec_row = _exec(stage=0, program=program, trigger_agent="@lily@hashi4")
    exec_row["program_blob"] = _json.dumps(program_to_json(program))
    directive = directive_for_exec_stage(exec_row)
    assert directive is not None
    assert directive.stage == 0
    assert directive.target == "@lily"
    assert directive.payload == "start @lily@hashi4"
    assert directive.idempotency_key == "hal:hx1:0"

    exec_row2 = _exec(stage=1, program=program, trigger_agent="@lily@hashi4")
    exec_row2["program_blob"] = _json.dumps(program_to_json(program))
    directive2 = directive_for_exec_stage(exec_row2)
    assert directive2 is not None
    assert directive2.target == "@bob"
    assert directive2.payload == "next"
    assert directive2.idempotency_key == "hal:hx1:1"
# HASHI-Fingerprint: hfp_ea3949897443405a8a2ea7bb048c417f
