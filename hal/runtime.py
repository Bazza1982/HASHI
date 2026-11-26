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
from hal.event_source import (
    EVENT_SYSTEM_IDLE,
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
                    "entry": stage_index == 0,
                    "event_available": available,
                    "event_source": reason,
                }
            )
            if (
                rule.source_agent
                and rule.source_agent != "@agent_any"
                and "@" not in rule.source_agent[1:]
            ):
                diagnostics.append(
                    {
                        "workflow": index,
                        "kind": "bare_source_name",
                        "stage": stage_index,
                        "message": (
                            f"source {rule.source_agent} has no instance qualifier; "
                            "canonical event identities are @name@instance, so this "
                            "rule may never match"
                        ),
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
            # replays feed history that predates its enablement.
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
                if not entry_matches(entry, _trigger_as_hal_event(trigger)):
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
            if not _dependent_source_matches(rule, trigger):
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

    return HALEvent(name=trigger.event, agent_identity=trigger.agent_identity)


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


def _directive_for(*, exec_id: str, stage: int, action, agent_trigger: str | None) -> DispatchDirective:
    payload = action.value
    unbound_reason = None
    if action.kind == "prompt":
        from hal.language import resolve_prompt

        try:
            payload = resolve_prompt(action, agent_trigger=agent_trigger)
        except ValueError as exc:
            payload = action.value
            unbound_reason = str(exc)
    return DispatchDirective(
        exec_id=exec_id,
        stage=stage,
        kind=action.kind,
        target=action.target,
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


def _dependent_source_matches(rule, trigger: HalTrigger) -> bool:
    """A dependent stage's declared source Agent must match the correlated run.

    Mirrors the entry-rule source semantics: an unrelated Agent carrying the
    same correlation key must never advance (or fail-hint) the workflow. A
    missing declaration means "any source" only for stage 0 entry rules; for a
    dependent stage the correlation key alone is not sufficient proof of origin.
    """
    source_agent = getattr(rule, "source_agent", None)
    if source_agent is None:
        return True
    if source_agent == "@agent_any":
        return bool(trigger.agent_identity)
    return source_agent == trigger.agent_identity


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
            )
            for item in workflow
        ]
        workflows.append(tuple(rules))
    return HALProgram(tuple(workflows))
# HASHI-Fingerprint: hfp_5b372bf9a11a4b429bb29caf7ea11771
