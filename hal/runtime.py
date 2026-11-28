"""HAL v0.1 runtime engine: deterministic matching and execution intents.

Pure planning layer. It never touches aiohttp, SQLite, commands or agents:
the owning service applies the returned intents to durable state and asks the
injected dispatch adapter to execute actions. Keeping this layer free of I/O
makes every semantics decision unit-testable with real compiled programs.

Author boundary: no new vocabulary, keywords or semantics are invented here.
Anything the author reserved (else, $stop, alias mapping) is already rejected
by ``hal.language`` and is never re-interpreted at runtime.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from hal import compile_hal, entry_matches, HALSyntaxError
from hal.language import resolve_prompt, resolve_role_target, source_matches
from hal.event_source import (
    EVENT_SYSTEM_IDLE,
    EVENT_WHITEBOARD_STAGE,
    SOURCE_WHITEBOARD,
    HalTrigger,
    event_availability,
)

# Exec states.
EXEC_ACTIVE = "active"
EXEC_COMPLETED = "completed"
EXEC_FAILED = "failed"
EXEC_DISABLED = "disabled"
# A native command was accepted but its real completion cannot be observed:
# accepting a reboot/deploy request does not mean the reboot/deploy finished.
EXEC_UNVERIFIED = "unverified"

# Receipt states.
RECEIPT_DISPATCHED = "dispatched"
RECEIPT_FAILED = "failed"


@dataclass(frozen=True)
class DispatchDirective:
    """One action the service must execute through the dispatch adapter."""

    exec_id: str
    stage: int
    kind: str  # "prompt" | "native_command"
    target: str | None
    payload: str
    idempotency_key: str
    # Set when the action cannot be rendered (e.g. unbound @agent_trigger on a
    # system event). The service fails the stage honestly instead of dispatching.
    unbound_reason: str | None = None


@dataclass(frozen=True)
class CreateIntent:
    """Start a new workflow execution from an entry match."""

    rule_id: str
    rule_revision: int
    rule_name: str
    workflow_index: int
    exec_id: str
    trigger_event_id: str
    trigger_agent: str | None
    trigger_kind: str
    directive: DispatchDirective
    # (rule_id, workflow_index, trigger_event_id) — service enforces uniqueness
    # for idempotency, so two workflows of the same rule and event stay distinct.
    dedupe_key: tuple[str, int, str]
    # Event context frozen onto the execution so later stages render the same
    # card data and role routing as the entry stage.
    context: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class AdvanceIntent:
    """Advance an active workflow one stage from a correlated trigger."""

    exec_id: str
    stage: int
    trigger_event_id: str
    directive: DispatchDirective
    dedupe_key: tuple[str, str]


@dataclass(frozen=True)
class Hint:
    """Truthful status hint; never changes execution semantics."""

    exec_id: str
    detail: str


def validate_script(script: str) -> dict[str, Any]:
    """Compile + map every event to its real availability. Never persists."""
    try:
        program = compile_hal(script)
    except HALSyntaxError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "line": exc.line,
            "column": exc.column,
            "workflows": [],
            "diagnostics": [],
        }
    workflows = []
    diagnostics: list[dict[str, Any]] = []
    for index, workflow in enumerate(program.workflows):
        stages = []
        for stage_index, rule in enumerate(workflow):
            available, reason = event_availability(rule.event)
            stages.append(
                {
                    "stage": stage_index,
                    "line": rule.line,
                    "event": rule.event,
                    "source_agent": rule.source_agent,
                    "action_kind": rule.action.kind,
                    "action_target": rule.action.target,
                    "conditions": [{"key": key, "value": value} for key, value in rule.conditions],
                    "entry": stage_index == 0,
                    "event_available": available,
                    "event_source": reason,
                }
            )
        entry_ok = stages[0]["event_available"]
        if not entry_ok:
            diagnostics.append(
                {
                    "workflow": index,
                    "kind": "entry_event_unavailable",
                    "message": (
                        f"workflow entry event {stages[0]['event']} has no real "
                        f"source yet ({stages[0]['event_source']}); this workflow "
                        "cannot start"
                    ),
                }
            )
        for stage in stages:
            if not stage["entry"] and not stage["event_available"]:
                diagnostics.append(
                    {
                        "workflow": index,
                        "kind": "stage_event_unavailable",
                        "stage": stage["stage"],
                        "message": (
                            f"stage {stage['stage']} waits for {stage['event']} "
                            f"({stage['event_source']}); the workflow will stop "
                            "waiting there honestly"
                        ),
                    }
                )
        workflows.append(
            {
                "index": index,
                "entry_available": entry_ok,
                "stages": stages,
            }
        )
    return {
        "ok": True,
        "error": None,
        "line": None,
        "column": None,
        "workflows": workflows,
        "diagnostics": diagnostics,
    }


def _exec_still_waiting(exec_row: Mapping[str, Any], stage: int) -> bool:
    return str(exec_row.get("state")) == EXEC_ACTIVE and int(exec_row.get("stage")) == stage


def plan_for_trigger(
    trigger: HalTrigger,
    rules: Sequence[Mapping[str, Any]],
    execs: Sequence[Mapping[str, Any]],
    receipts: Sequence[Mapping[str, Any]],
    consumed: set[tuple[str, str]],
    *,
    current_instance: str | None = None,
) -> tuple[list[CreateIntent], list[AdvanceIntent], list[Hint]]:
    """Compute deterministic intents for one trigger. No side effects."""
    creates: list[CreateIntent] = []
    advances: list[AdvanceIntent] = []
    hints: list[Hint] = []

    receipt_by_exec_stage = {(str(r["exec_id"]), int(r["stage"])): r for r in receipts}

    # --- Entry matching: automation-origin runs never start a workflow again.
    if not trigger.automation_origin:
        for rule_row in rules:
            if not rule_row.get("enabled"):
                continue
            # Per-rule enable baseline: a rule only matches events committed
            # AFTER the moment it was enabled, so a newly enabled rule never
            # replays history that predates its enablement. Each source keeps
            # its own baseline: the two cursors are unrelated sequences.
            if trigger.source == SOURCE_WHITEBOARD:
                enable_cursor = rule_row.get("wb_enable_cursor")
            else:
                enable_cursor = rule_row.get("enable_cursor")
            if (
                enable_cursor is not None
                and trigger.feed_cursor is not None
                and int(trigger.feed_cursor) <= int(enable_cursor)
            ):
                continue
            program = rule_row["program"]
            for workflow_index, workflow in enumerate(program.workflows):
                entry = workflow[0]
                if not entry_matches(
                    entry,
                    _trigger_as_hal_event(trigger),
                    current_instance=current_instance,
                ):
                    continue
                if trigger.event == EVENT_SYSTEM_IDLE and entry.source_agent is not None:
                    continue  # compiler guarantees this, but stay explicit
                exec_id = _next_exec_id(
                    rule_row["rule_id"], workflow_index, trigger.feed_event_id
                )
                directive = _directive_for(
                    exec_id=exec_id,
                    stage=0,
                    action=entry.action,
                    agent_trigger=trigger.agent_identity,
                    context=trigger.context,
                )
                creates.append(
                    CreateIntent(
                        rule_id=str(rule_row["rule_id"]),
                        rule_revision=int(rule_row["revision"]),
                        rule_name=str(rule_row.get("name") or ""),
                        workflow_index=workflow_index,
                        exec_id=exec_id,
                        trigger_event_id=str(trigger.feed_event_id or ""),
                        trigger_agent=trigger.agent_identity,
                        trigger_kind=_trigger_kind(trigger),
                        directive=directive,
                        dedupe_key=(
                            str(rule_row["rule_id"]),
                            workflow_index,
                            str(trigger.feed_event_id or ""),
                        ),
                        context=trigger.context,
                    )
                )

    # --- Dependent matching: only correlated triggers advance a stage.
    for exec_row in execs:
        if str(exec_row.get("state")) != EXEC_ACTIVE:
            continue
        rule_program = exec_row.get("program")
        if rule_program is None:
            continue
        stage = int(exec_row["stage"])
        workflows = rule_program.workflows
        workflow_index = int(exec_row.get("workflow_index") or 0)
        if workflow_index >= len(workflows):
            continue
        workflow = workflows[workflow_index]
        if stage >= len(workflow):
            continue
        rule = workflow[stage]
        if rule.event != trigger.event:
            # Correlated failure hint: a run dispatched by this workflow died.
            if (
                trigger.event == "#turn_error"
                and rule.event == "#turn_finish"
                and _correlates_with_previous_stage(exec_row, stage, trigger, receipt_by_exec_stage)
            ):
                hints.append(
                    Hint(
                        exec_id=str(exec_row["exec_id"]),
                        detail=f"correlated run failed at stage {stage - 1}; still waiting for {rule.event}",
                    )
                )
            continue
        if trigger.event in _AGENT_EVENTS:
            if not _correlates_with_previous_stage(exec_row, stage, trigger, receipt_by_exec_stage):
                continue
            if not _dependent_source_matches(
                rule,
                trigger,
                current_instance=current_instance,
            ):
                continue
        else:
            # System events carry no per-workflow correlation evidence; an
            # unrelated deployment/reboot must never advance this workflow.
            continue
        dedupe = (str(exec_row["exec_id"]), str(trigger.feed_event_id or ""))
        if dedupe in consumed:
            continue
        directive = _directive_for(
            exec_id=str(exec_row["exec_id"]),
            stage=stage,
            action=rule.action,
            agent_trigger=exec_row.get("trigger_agent"),
            context=_exec_context(exec_row),
        )
        advances.append(
            AdvanceIntent(
                exec_id=str(exec_row["exec_id"]),
                stage=stage,
                trigger_event_id=str(trigger.feed_event_id or ""),
                directive=directive,
                dedupe_key=dedupe,
            )
        )

    return creates, advances, hints


_AGENT_EVENTS = frozenset({"#turn_finish", "#turn_error"})


def _trigger_as_hal_event(trigger: HalTrigger):
    from hal.language import HALEvent

    return HALEvent(
        name=trigger.event,
        agent_identity=trigger.agent_identity,
        context=trigger.context,
    )


def _trigger_kind(trigger: HalTrigger) -> str:
    if trigger.event == EVENT_SYSTEM_IDLE:
        return "system_idle"
    return trigger.feed_kind or "feed"


def _next_exec_id(rule_id: str, workflow_index: int, feed_event_id: str | None) -> str:
    """Deterministic, collision-free exec identity for one (rule, workflow, event)."""
    import hashlib

    material = f"{rule_id}|{workflow_index}|{feed_event_id or ''}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
    return f"hx{digest}"


def _exec_context(exec_row: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The card context frozen on an execution, if any."""
    context = exec_row.get("context")
    if isinstance(context, Mapping):
        return context
    blob = exec_row.get("context_blob")
    if isinstance(blob, str) and blob:
        try:
            loaded = json.loads(blob)
        except (ValueError, TypeError):
            return None
        if isinstance(loaded, dict):
            return loaded
    return None


def _directive_for(
    *,
    exec_id: str,
    stage: int,
    action,
    agent_trigger: str | None,
    context: Mapping[str, Any] | None = None,
) -> DispatchDirective:
    payload = action.value
    target = action.target
    unbound_reason = None
    if action.kind == "prompt":
        # A role target is data, not a guess: no card value means this stage
        # cannot be routed, and the receipt says so instead of mailing a random
        # Agent.
        if target is not None and target.startswith("@{"):
            resolved = resolve_role_target(action, context=context)
            if not resolved:
                unbound_reason = (
                    f"prompt target {target} has no value on card "
                    f"{(context or {}).get('card_id') or 'unknown'}"
                )
            target = resolved or target
        try:
            payload = resolve_prompt(action, agent_trigger=agent_trigger, context=context)
        except ValueError as exc:
            payload = action.value
            unbound_reason = unbound_reason or str(exc)
    return DispatchDirective(
        exec_id=exec_id,
        stage=stage,
        kind=action.kind,
        target=target,
        payload=payload,
        idempotency_key=f"hal:{exec_id}:{stage}",
        unbound_reason=unbound_reason,
    )


def directive_for_exec_stage(exec_row: Mapping[str, Any]) -> DispatchDirective | None:
    """Reconstruct the dispatch directive for an execution's current stage.

    Used for crash recovery when the receipt for the current stage was never
    written. Deterministic: the same exec_id + stage always yields the same
    idempotency key and resolved payload, so a reconstructed dispatch never
    double-executes a side effect that was already recorded.
    """
    program = exec_row.get("program")
    if program is None:
        program_blob = exec_row.get("program_blob")
        if program_blob:
            try:
                program = program_from_json(json.loads(program_blob))
            except (ValueError, KeyError, TypeError):
                program = None
    if program is None:
        return None
    workflow_index = int(exec_row.get("workflow_index") or 0)
    stage = int(exec_row.get("stage"))
    if workflow_index >= len(program.workflows):
        return None
    workflow = program.workflows[workflow_index]
    if stage >= len(workflow):
        return None
    return _directive_for(
        exec_id=str(exec_row["exec_id"]),
        stage=stage,
        action=workflow[stage].action,
        agent_trigger=exec_row.get("trigger_agent"),
        context=_exec_context(exec_row),
    )


def _correlates_with_previous_stage(
    exec_row: Mapping[str, Any],
    stage: int,
    trigger: HalTrigger,
    receipts: dict[tuple[str, int], Mapping[str, Any]],
) -> bool:
    """A dependent stage only advances when its run matches the previous dispatch."""
    if stage < 1:
        return False
    prior = receipts.get((str(exec_row["exec_id"]), stage - 1))
    if prior is None:
        return False
    if not trigger.run_idempotency_key or trigger.run_idempotency_key != prior.get("idempotency_key"):
        return False
    target = str(prior.get("target") or "").lower()
    if target.startswith("@") and "@" in target[1:]:
        return target == str(trigger.agent_identity or "").lower()
    return True


def _dependent_source_matches(
    rule,
    trigger: HalTrigger,
    *,
    current_instance: str | None = None,
) -> bool:
    """A dependent stage's declared source Agent must match the correlated run.

    A declared source uses the same matching rules as an entry stage.
    The caller independently verifies the previous stage's correlation key.
    """
    return source_matches(
        rule.source_agent,
        trigger.agent_identity,
        current_instance=current_instance,
    )


@dataclass(frozen=True)
class HaltIntent:
    """Stop an execution because the board says the work must not continue."""

    exec_id: str
    card_id: str
    reason: str


def whiteboard_halt_reason(trigger: HalTrigger) -> str | None:
    """Why this transition must not start or continue automation, or None.

    The board is the authority: a fused card, a halted-category stage, an
    exhausted round budget or a human-gated stage all stop the chain. Nothing
    here invents a stage name; the values come from the event document.
    """
    if trigger.source != SOURCE_WHITEBOARD:
        return None
    context = trigger.context or {}
    if bool(context.get("fused")):
        return str(context.get("reason") or "whiteboard circuit breaker fired")
    category = str(context.get("category") or "")
    if category == "halted":
        return f"transition into halted stage {context.get('to_stage')}"
    if bool(context.get("human_gate")):
        return f"stage {context.get('to_stage')} requires a human decision"
    # The round budget is deliberately NOT re-derived from round/max_rounds here:
    # the board already fuses (``fused``) only when a rework move would exceed
    # the budget, and a plain forward move at the boundary is legal. Counting
    # rounds locally would block that legal move. The service applies the budget
    # to rework destinations from the board's own stage table instead.
    return None


def whiteboard_stop_is_exceptional(trigger: HalTrigger) -> bool:
    """True when the board itself took a protective action.

    A fused card, a halted-category move or an exhausted round budget is an
    exception the humans must hear about even if no rule was going to fire. A
    plain human-gated move is ordinary workflow traffic, not an automation stop.
    """
    if trigger.source != SOURCE_WHITEBOARD:
        return False
    context = trigger.context or {}
    return bool(context.get("fused")) or str(context.get("category") or "") == "halted"


def entry_rules_match(
    trigger: HalTrigger,
    rules: Sequence[Mapping[str, Any]],
    *,
    current_instance: str | None = None,
) -> bool:
    """Would any enabled rule have started from this trigger?

    Used to decide whether a stop is worth reporting: a human-gated transition
    nobody was going to automate is not an automation stop.
    """
    event = _trigger_as_hal_event(trigger)
    for rule_row in rules:
        if not rule_row.get("enabled"):
            continue
        program = rule_row.get("program")
        if program is None:
            continue
        for workflow in program.workflows:
            if not workflow:
                continue
            entry = workflow[0]
            if entry.event != trigger.event:
                continue
            if entry_matches(entry, event, current_instance=current_instance):
                return True
    return False


def halted_execs_for(
    trigger: HalTrigger,
    execs: Sequence[Mapping[str, Any]],
) -> list[HaltIntent]:
    """Active executions on the halted card that must be settled now."""
    reason = whiteboard_halt_reason(trigger)
    if reason is None:
        return []
    card_id = str((trigger.context or {}).get("card_id") or "")
    if not card_id:
        return []
    halts: list[HaltIntent] = []
    for exec_row in execs:
        if str(exec_row.get("state")) != EXEC_ACTIVE:
            continue
        context = _exec_context(exec_row) or {}
        if str(context.get("card_id") or "") != card_id:
            continue
        halts.append(
            HaltIntent(exec_id=str(exec_row["exec_id"]), card_id=card_id, reason=reason)
        )
    return halts


def stage_after_dispatch(stage: int, workflow_length: int) -> tuple[int, str]:
    """Advance stage, or complete the workflow."""
    next_stage = stage + 1
    if next_stage >= workflow_length:
        return next_stage, EXEC_COMPLETED
    return next_stage, EXEC_ACTIVE


def unavailable_wait_detail(event_name: str) -> str | None:
    available, reason = event_availability(event_name)
    if available:
        return None
    return f"waiting for {event_name} ({reason})"


def rule_waits_for_whiteboard(program) -> bool:
    """True when any entry rule of this program starts on a board transition."""
    if program is None:
        return False
    for workflow in program.workflows:
        if workflow and workflow[0].event == EVENT_WHITEBOARD_STAGE:
            return True
    return False


def program_to_json(program) -> list[Any]:
    """Freeze a compiled program so an execution keeps its creating revision."""
    return [
        [
            {
                "source_agent": rule.source_agent,
                "event": rule.event,
                "action_kind": rule.action.kind,
                "action_target": rule.action.target,
                "action_value": rule.action.value,
                "terminator": rule.terminator,
                "line": rule.line,
                "conditions": [list(pair) for pair in rule.conditions],
            }
            for rule in workflow
        ]
        for workflow in program.workflows
    ]


def program_from_json(blob: Any):
    """Rebuild the frozen program of one workflow execution."""
    from hal.language import HALAction, HALProgram, HALRule

    workflows = []
    for workflow in blob:
        rules = [
            HALRule(
                source_agent=item["source_agent"],
                event=item["event"],
                action=HALAction(
                    kind=item["action_kind"],
                    target=item["action_target"],
                    value=item["action_value"],
                ),
                terminator=item["terminator"],
                line=int(item["line"]),
                conditions=tuple(
                    (str(pair[0]), str(pair[1]))
                    for pair in item.get("conditions") or ()
                ),
            )
            for item in workflow
        ]
        workflows.append(tuple(rules))
    return HALProgram(tuple(workflows))
# HASHI-Fingerprint: hfp_5b372bf9a11a4b429bb29caf7ea11771
