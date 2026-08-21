from __future__ import annotations

import asyncio
import json
from collections import defaultdict, deque

import pytest

from orchestrator.her_v2.audit import DurableAuditLog
from orchestrator.her_v2.commentary import RecordingCommentaryPort
from orchestrator.her_v2.config import HERv2Config
from orchestrator.her_v2.interfaces import RecordingDelivery, StageInvocationError
from orchestrator.her_v2.ledger import LedgerStore
from orchestrator.her_v2.models import (
    Effort,
    Stage,
    StageResponse,
    TerminalState,
    TriageClassification,
)
from orchestrator.her_v2.presentation import (
    RenderedRequiredMessage,
    RequiredUserMessage,
)
from orchestrator.her_v2.runtime import HERv2Runtime


def _config(**overrides):
    profile_timeout_s = overrides.pop("profile_timeout_s", 2)
    profile_timeouts = overrides.pop("profile_timeouts", {})
    profiles = {
        name: {
            "engine": "fake-api",
            "model": f"model-{name}",
            "reasoning": f"reasoning-{name}",
            "max_attempts": 1,
            "timeout_s": profile_timeouts.get(name, profile_timeout_s),
        }
        for name in ("lightweight", "triage", "premium", "reviewer", "orchestrator")
    }
    raw = {
        "profiles": profiles,
        "structured_repair_attempts": 1,
        "reporting_attempts": 3,
        "user_idle_timeout_s": 10,
        "hard_timeout_s": 20,
        "shadow_mode": False,
    }
    raw.update(overrides)
    return HERv2Config.from_mapping(raw)


class ScriptedProvider:
    def __init__(self, scripts, *, delays=None):
        self.scripts = {stage: deque(values) for stage, values in scripts.items()}
        self.delays = dict(delays or {})
        self.requests = []
        self.started = defaultdict(asyncio.Event)
        self.cancelled = defaultdict(int)

    async def invoke(self, profile, request):
        self.requests.append((profile, request))
        self.started[request.stage].set()
        delay = self.delays.get(request.stage, 0)
        if delay:
            await asyncio.sleep(delay)
        if request.stage not in self.scripts or not self.scripts[request.stage]:
            raise StageInvocationError(f"no scripted response for {request.stage.value}")
        value = self.scripts[request.stage].popleft()
        try:
            if callable(value):
                value = value(request)
            if asyncio.iscoroutine(value):
                value = await value
        except asyncio.CancelledError:
            self.cancelled[request.stage] += 1
            raise
        if isinstance(value, Exception):
            raise value
        if isinstance(value, StageResponse):
            return value
        return StageResponse(
            text="",
            data=value,
            reasoning_trace=f"trace:{request.stage.value}",
            provider=profile.engine,
            model=profile.model,
        )


class TrackingHabits:
    def __init__(self, *, block_meditation=False):
        self.retrievals = []
        self.meditations = []
        self.meditation_started = asyncio.Event()
        self.release_meditation = asyncio.Event()
        self.block_meditation = block_meditation

    async def retrieve(self, *, goal, turn_id):
        self.retrievals.append((goal, turn_id))
        return ("advisory habit",)

    async def meditate(
        self,
        *,
        turn_id,
        goal,
        summary,
        evidence_refs,
        limitations,
        terminal_state,
    ):
        self.meditation_started.set()
        if self.block_meditation:
            await self.release_meditation.wait()
        self.meditations.append(
            (
                turn_id,
                goal,
                summary,
                tuple(evidence_refs),
                tuple(limitations),
                terminal_state,
            )
        )


class FailingMeditation(TrackingHabits):
    async def meditate(
        self,
        *,
        turn_id,
        goal,
        summary,
        evidence_refs,
        limitations,
        terminal_state,
    ):
        del turn_id, goal, summary, evidence_refs, limitations, terminal_state
        self.meditation_started.set()
        raise RuntimeError("meditation unavailable")


class ExplodingDream:
    def __init__(self):
        self.calls = 0

    async def maintain(self, *, catalogue_ref):
        del catalogue_ref
        self.calls += 1
        raise RuntimeError("dream unavailable")


class RecordingRequiredPersonaRenderer:
    def __init__(self, *, error=None):
        self.error = error
        self.messages: list[RequiredUserMessage] = []

    async def render(self, message):
        self.messages.append(message)
        if self.error is not None:
            raise self.error
        return RenderedRequiredMessage(
            source_event_id=message.event_id,
            kind=message.kind,
            text=f"Persona {message.kind}:\n\n{message.text}",
            provenance="test_persona_renderer",
        )


def _runtime(
    tmp_path,
    provider,
    *,
    config=None,
    delivery=None,
    habits=None,
    meditation=None,
    dream=None,
    audit=None,
    commentary=None,
    required_persona=None,
):
    root = tmp_path / "her-v2"
    return HERv2Runtime(
        config=config or _config(),
        provider=provider,
        ledger_store=LedgerStore(root / "ledgers"),
        audit_log=audit
        or DurableAuditLog(root / "audit.jsonl", root / "audit-fallback.jsonl"),
        delivery=delivery or RecordingDelivery(),
        commentary=commentary,
        required_persona=required_persona,
        habits=habits,
        meditation=meditation if meditation is not None else habits,
        dream=dream,
    )


def _initial(classification, *, triage_goal="interpreted goal", clarification=""):
    return {
        Stage.IMMEDIATE_RESPONSE: [{"message": "I have it."}],
        Stage.TRIAGE: [
            {
                "classification": classification,
                "goal": triage_goal,
                "clarification": clarification,
            }
        ],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "delays",
    [
        {Stage.IMMEDIATE_RESPONSE: 0.005, Stage.TRIAGE: 0.03},
        {Stage.IMMEDIATE_RESPONSE: 0.03, Stage.TRIAGE: 0.005},
    ],
)
async def test_direct_response_race_delivers_exactly_one_answer(tmp_path, delays):
    provider = ScriptedProvider(_initial("DIRECT_RESPONSE"), delays=delays)
    runtime = _runtime(tmp_path, provider)
    result = await runtime.run_turn("Hello", "request-direct", effort=Effort.MAX)

    assert result.terminal_state is TerminalState.COMPLETED
    assert result.classification is TriageClassification.DIRECT_RESPONSE
    assert result.text == "I have it."
    assert result.final_was_immediate is True
    assert len(result.delivery_records) == 1
    assert result.delivery_records[0].text == "I have it."
    if delays[Stage.IMMEDIATE_RESPONSE] < delays[Stage.TRIAGE]:
        assert result.delivery_records[0].kind == "final"
        assert result.final_already_delivered is True
    else:
        assert result.delivery_records[0].kind == "final"
        assert result.final_already_delivered is False
    assert {request.stage for _profile, request in provider.requests} == {
        Stage.IMMEDIATE_RESPONSE,
        Stage.TRIAGE,
    }


@pytest.mark.asyncio
async def test_direct_response_promotes_repaired_immediate_content_without_fallback(
    tmp_path,
):
    scripts = {
        Stage.IMMEDIATE_RESPONSE: [
            StageResponse(
                text='{"message":"First answer line.\n\nSecond answer line."}',
                provider="fake-api",
                model="model-lightweight",
            )
        ],
        Stage.TRIAGE: [{"classification": "DIRECT_RESPONSE"}],
    }
    provider = ScriptedProvider(
        scripts,
        delays={Stage.IMMEDIATE_RESPONSE: 0.01},
    )

    result = await _runtime(tmp_path, provider).run_turn(
        "Answer directly",
        "request-direct-control-char-repair",
        effort="low",
    )

    expected = "First answer line.\n\nSecond answer line."
    assert result.terminal_state is TerminalState.COMPLETED
    assert result.final_was_immediate is True
    assert result.text == expected
    assert [(item.kind, item.text) for item in result.delivery_records] == [
        ("final", expected)
    ]
    rows = [
        json.loads(line)
        for line in (tmp_path / "her-v2" / "audit.jsonl").read_text().splitlines()
    ]
    compatibility = next(
        row
        for row in rows
        if row["event"] == "structured_response_compatibility_applied"
        and row["stage"] == Stage.IMMEDIATE_RESPONSE.value
    )
    assert (
        compatibility["payload"]["validation_source"]
        == "provider_json_control_char_repair"
    )


@pytest.mark.asyncio
async def test_direct_response_is_not_repackaged_by_required_persona_renderer(tmp_path):
    renderer = RecordingRequiredPersonaRenderer()
    result = await _runtime(
        tmp_path,
        ScriptedProvider(_initial("DIRECT_RESPONSE")),
        required_persona=renderer,
    ).run_turn("Hello", "request-direct-no-repackage", effort="low")

    assert result.terminal_state is TerminalState.COMPLETED
    assert result.text == "I have it."
    assert renderer.messages == []


@pytest.mark.asyncio
async def test_triage_first_work_starts_without_waiting_and_repairs_late_immediate(
    tmp_path,
):
    release_immediate = asyncio.Event()
    execution_started = asyncio.Event()
    release_execution = asyncio.Event()
    acknowledgement_delivered = asyncio.Event()

    async def delayed_immediate(_request):
        await release_immediate.wait()
        return StageResponse(
            text='{"message":"I have it.\n\nI will check now."}',
            provider="fake-api",
            model="model-lightweight",
        )

    async def blocked_execution(_request):
        execution_started.set()
        await release_execution.wait()
        return {"disposition": "COMPLETED", "summary": "Checked."}

    class SignallingDelivery(RecordingDelivery):
        async def deliver(self, **kwargs):
            accepted = await super().deliver(**kwargs)
            if kwargs["kind"] == "acknowledgement":
                acknowledgement_delivered.set()
            return accepted

    scripts = {
        Stage.IMMEDIATE_RESPONSE: [delayed_immediate],
        Stage.TRIAGE: [{"classification": "SIMPLE_TASK"}],
        Stage.EXECUTION: [blocked_execution],
        Stage.FINALISATION: [{"report": "Checked and complete."}],
    }
    provider = ScriptedProvider(scripts)
    delivery = SignallingDelivery()
    turn = asyncio.create_task(
        _runtime(tmp_path, provider, delivery=delivery).run_turn(
            "Check it", "request-triage-first-immediate-late", effort="low"
        )
    )

    await asyncio.wait_for(execution_started.wait(), timeout=1)
    assert acknowledgement_delivered.is_set() is False
    release_immediate.set()
    await asyncio.wait_for(acknowledgement_delivered.wait(), timeout=1)
    release_execution.set()
    result = await asyncio.wait_for(turn, timeout=1)

    assert result.terminal_state is TerminalState.COMPLETED
    assert [(item.kind, item.text) for item in delivery.records] == [
        ("acknowledgement", "I have it.\n\nI will check now."),
        ("final", "Checked and complete."),
    ]
    assert provider.cancelled[Stage.IMMEDIATE_RESPONSE] == 0
    rows = [
        json.loads(line)
        for line in (tmp_path / "her-v2" / "audit.jsonl").read_text().splitlines()
    ]
    continued = next(row for row in rows if row["event"] == "optional_stage_continues")
    assert continued["payload"] == {
        "classification": "SIMPLE_TASK",
        "reason": "triage_completed_before_optional_immediate_response",
        "authoritative_path_waited": False,
        "delivery_when_ready": "acknowledgement",
    }
    compatibility = next(
        row
        for row in rows
        if row["event"] == "structured_response_compatibility_applied"
        and row["stage"] == Stage.IMMEDIATE_RESPONSE.value
    )
    assert (
        compatibility["payload"]["validation_source"]
        == "provider_json_control_char_repair"
    )
    assert not any(row["event"] == "optional_stage_degraded" for row in rows)


@pytest.mark.asyncio
async def test_unrepairable_immediate_text_remains_visible_for_work(tmp_path):
    raw_immediate = '{"message":"Visible acknowledgement despite truncation."'
    scripts = {
        Stage.IMMEDIATE_RESPONSE: [
            StageResponse(
                text=raw_immediate,
                provider="fake-api",
                model="model-lightweight",
            )
        ],
        Stage.TRIAGE: [{"classification": "SIMPLE_TASK"}],
        Stage.EXECUTION: [{"disposition": "COMPLETED", "summary": "Done."}],
        Stage.FINALISATION: [{"report": "Done."}],
    }

    result = await _runtime(tmp_path, ScriptedProvider(scripts)).run_turn(
        "Complete the work",
        "request-visible-unrepairable-immediate",
        effort="low",
    )

    assert result.terminal_state is TerminalState.COMPLETED
    assert [(item.kind, item.text) for item in result.delivery_records] == [
        ("acknowledgement", raw_immediate),
        ("final", "Done."),
    ]
    rows = [
        json.loads(line)
        for line in (tmp_path / "her-v2" / "audit.jsonl").read_text().splitlines()
    ]
    compatibility = next(
        row
        for row in rows
        if row["event"] == "structured_response_compatibility_applied"
        and row["stage"] == Stage.IMMEDIATE_RESPONSE.value
    )
    assert compatibility["payload"]["validation_source"] == "provider_plain_text"


@pytest.mark.asyncio
async def test_final_completion_supersedes_a_still_pending_immediate_response(tmp_path):
    immediate_started = asyncio.Event()
    never_release = asyncio.Event()

    async def blocked_immediate(_request):
        immediate_started.set()
        await never_release.wait()
        return {"message": "Too late."}

    scripts = {
        Stage.IMMEDIATE_RESPONSE: [blocked_immediate],
        Stage.TRIAGE: [{"classification": "SIMPLE_TASK"}],
        Stage.EXECUTION: [{"disposition": "COMPLETED", "summary": "Done."}],
        Stage.FINALISATION: [{"report": "Done."}],
    }
    provider = ScriptedProvider(scripts)
    result = await asyncio.wait_for(
        _runtime(tmp_path, provider).run_turn(
            "Do it", "request-final-supersedes-immediate", effort="low"
        ),
        timeout=1,
    )

    assert immediate_started.is_set() is True
    assert result.terminal_state is TerminalState.COMPLETED
    assert [(item.kind, item.text) for item in result.delivery_records] == [
        ("final", "Done.")
    ]
    assert provider.cancelled[Stage.IMMEDIATE_RESPONSE] == 1
    rows = [
        json.loads(line)
        for line in (tmp_path / "her-v2" / "audit.jsonl").read_text().splitlines()
    ]
    superseded = next(
        row
        for row in rows
        if row["event"] == "optional_stage_superseded"
        and row["payload"]["reason"]
        == "final_report_ready_before_immediate_response"
    )
    assert superseded["payload"]["authoritative_path_waited"] is False
    assert not any(row["event"] == "optional_stage_degraded" for row in rows)


@pytest.mark.asyncio
async def test_confirmation_required_never_enters_planning_or_execution(tmp_path):
    scripts = _initial(
        "CONFIRMATION_REQUIRED", clarification="Which account should be changed?"
    )
    provider = ScriptedProvider(scripts)
    result = await _runtime(tmp_path, provider).run_turn(
        "Change the account", "request-confirm", effort=Effort.HIGH
    )

    assert result.terminal_state is TerminalState.PENDING_USER_INPUT
    assert result.text == "Which account should be changed?"
    assert [item.kind for item in result.delivery_records] == [
        "acknowledgement",
        "clarification",
    ]
    assert all(
        request.stage not in {Stage.PLANNING, Stage.EXECUTION}
        for _profile, request in provider.requests
    )


@pytest.mark.asyncio
async def test_triage_clarification_is_persona_rendered_without_changing_authority(
    tmp_path,
):
    renderer = RecordingRequiredPersonaRenderer()
    scripts = _initial(
        "CONFIRMATION_REQUIRED",
        clarification="Which account should be changed?",
    )

    result = await _runtime(
        tmp_path,
        ScriptedProvider(scripts),
        required_persona=renderer,
    ).run_turn("Change the account", "request-rendered-confirmation", effort="high")

    assert result.terminal_state is TerminalState.PENDING_USER_INPUT
    assert result.text == (
        "Persona clarification:\n\nWhich account should be changed?"
    )
    assert [(message.kind, message.text) for message in renderer.messages] == [
        ("clarification", "Which account should be changed?")
    ]
    clarification = next(
        item for item in result.delivery_records if item.kind == "clarification"
    )
    assert clarification.text == result.text
    rows = [
        json.loads(line)
        for line in (tmp_path / "her-v2" / "audit.jsonl").read_text().splitlines()
    ]
    rendered = next(
        row for row in rows if row["event"] == "required_persona_render_completed"
    )
    assert rendered["payload"]["kind"] == "clarification"
    assert rendered["payload"]["provenance"] == "test_persona_renderer"
    assert rendered["payload"]["workflow_authority_changed"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("classification", "clarification"),
    [
        ("SIMPLE_TASK", ""),
        ("CONFIRMATION_REQUIRED", "Which account should be changed?"),
    ],
)
async def test_optional_immediate_failure_does_not_block_authoritative_triage(
    tmp_path,
    classification,
    clarification,
):
    scripts = {
        Stage.IMMEDIATE_RESPONSE: [
            StageInvocationError("invalid Immediate envelope", retryable=False)
        ],
        Stage.TRIAGE: [
            {
                "classification": classification,
                "clarification": clarification,
            }
        ],
    }
    if classification == "SIMPLE_TASK":
        scripts.update(
            {
                Stage.EXECUTION: [
                    {"disposition": "COMPLETED", "summary": "Done."}
                ],
                Stage.FINALISATION: [{"report": "Done."}],
            }
        )
    provider = ScriptedProvider(scripts)

    result = await _runtime(tmp_path, provider).run_turn(
        "Continue through the authoritative path",
        f"request-optional-immediate-{classification.lower()}",
        effort="low",
    )

    expected = (
        TerminalState.COMPLETED
        if classification == "SIMPLE_TASK"
        else TerminalState.PENDING_USER_INPUT
    )
    assert result.terminal_state is expected
    rows = [
        json.loads(line)
        for line in (tmp_path / "her-v2" / "audit.jsonl").read_text().splitlines()
    ]
    degraded = next(row for row in rows if row["event"] == "optional_stage_degraded")
    assert degraded["payload"]["authoritative_path_continued"] is True


@pytest.mark.asyncio
async def test_direct_response_still_requires_valid_immediate_content(tmp_path):
    provider = ScriptedProvider(
        {
            Stage.IMMEDIATE_RESPONSE: [
                StageInvocationError("empty direct answer", retryable=False)
            ],
            Stage.TRIAGE: [{"classification": "DIRECT_RESPONSE"}],
        }
    )

    result = await _runtime(tmp_path, provider).run_turn(
        "Hello", "request-direct-missing-immediate", effort="low"
    )

    assert result.terminal_state is TerminalState.ERROR
    assert "direct response requires a valid Immediate Response" in result.error


@pytest.mark.asyncio
async def test_early_immediate_is_promoted_to_clarification_without_duplication(
    tmp_path,
):
    scripts = _initial(
        "CONFIRMATION_REQUIRED", clarification="Which account should be changed?"
    )
    provider = ScriptedProvider(
        scripts,
        delays={Stage.IMMEDIATE_RESPONSE: 0.005, Stage.TRIAGE: 0.03},
    )

    result = await _runtime(tmp_path, provider).run_turn(
        "Change the account", "request-confirm-early", effort=Effort.HIGH
    )

    assert result.terminal_state is TerminalState.PENDING_USER_INPUT
    assert result.final_already_delivered is True
    assert [(item.kind, item.text) for item in result.delivery_records] == [
        ("clarification", "Which account should be changed?")
    ]


@pytest.mark.asyncio
async def test_medium_turn_preserves_goal_and_routes_tools_only_to_execution(tmp_path):
    scripts = _initial("COMPLEX_TASK", triage_goal="a tempting replacement goal")
    scripts.update(
        {
            Stage.PLANNING: [{"plan": ["inspect", "change", "test"]}],
            Stage.EXECUTION: [
                {
                    "disposition": "COMPLETED",
                    "summary": "Implemented and tested.",
                    "evidence_refs": ["test:passed"],
                }
            ],
            Stage.FINALISATION: [{"report": "Implemented and verified."}],
        }
    )
    provider = ScriptedProvider(scripts)
    request = "Implement the requested feature"

    result = await _runtime(tmp_path, provider).run_turn(
        request, "request-medium", effort=Effort.MEDIUM
    )

    assert result.terminal_state is TerminalState.COMPLETED
    assert result.text == "Implemented and verified."
    assert result.evidence_refs == ("test:passed",)
    assert result.ledger["plan_id"].endswith(":plan:v1")
    assert all(call.goal == request for _profile, call in provider.requests)
    execution_calls = [call for _profile, call in provider.requests if call.stage is Stage.EXECUTION]
    assert len(execution_calls) == 1
    assert execution_calls[0].allow_tools is True
    assert execution_calls[0].allow_side_effects is True
    assert all(
        not call.allow_tools
        for _profile, call in provider.requests
        if call.stage is not Stage.EXECUTION
    )


@pytest.mark.asyncio
async def test_validated_final_report_is_persona_rendered_before_required_delivery(
    tmp_path,
):
    raw_report = (
        "## Result\n\n"
        "- Status: complete\n"
        "- Path: `C:\\\\Work\\\\report.md`\n"
        "- Receipt: `job-42`"
    )
    scripts = _initial("SIMPLE_TASK")
    scripts.update(
        {
            Stage.EXECUTION: [
                {
                    "disposition": "COMPLETED",
                    "summary": "Completed with receipt job-42.",
                }
            ],
            Stage.FINALISATION: [{"report": raw_report}],
        }
    )
    renderer = RecordingRequiredPersonaRenderer()

    result = await _runtime(
        tmp_path,
        ScriptedProvider(scripts),
        required_persona=renderer,
    ).run_turn("Complete it", "request-render-final", effort="low")

    expected = f"Persona final:\n\n{raw_report}"
    assert result.terminal_state is TerminalState.COMPLETED
    assert result.text == expected
    assert [(message.kind, message.text) for message in renderer.messages] == [
        ("final", raw_report)
    ]
    final = next(item for item in result.delivery_records if item.kind == "final")
    assert final.text == expected
    assert result.ledger["status"] == "COMPLETED"
    rows = [
        json.loads(line)
        for line in (tmp_path / "her-v2" / "audit.jsonl").read_text().splitlines()
    ]
    delivery = next(
        row
        for row in rows
        if row["event"] == "delivery_result"
        and row["payload"]["kind"] == "final"
    )
    assert delivery["payload"]["provenance"] == "test_persona_renderer"


@pytest.mark.asyncio
async def test_required_persona_failure_preserves_validated_report_and_terminal_state(
    tmp_path,
):
    scripts = _initial("SIMPLE_TASK")
    scripts.update(
        {
            Stage.EXECUTION: [
                {"disposition": "COMPLETED", "summary": "Completed."}
            ],
            Stage.FINALISATION: [{"report": "Completed with receipt 42."}],
        }
    )
    renderer = RecordingRequiredPersonaRenderer(error=RuntimeError("offline"))

    result = await _runtime(
        tmp_path,
        ScriptedProvider(scripts),
        required_persona=renderer,
    ).run_turn("Complete it", "request-final-render-fallback", effort="low")

    assert result.terminal_state is TerminalState.COMPLETED
    assert result.text == "Completed with receipt 42."
    assert result.delivery_records[-1].text == result.text
    rows = [
        json.loads(line)
        for line in (tmp_path / "her-v2" / "audit.jsonl").read_text().splitlines()
    ]
    rendered = next(
        row for row in rows if row["event"] == "required_persona_render_completed"
    )
    assert rendered["payload"]["fallback"] is True
    assert rendered["payload"]["provenance"] == (
        "required_message_identity_fallback"
    )
    assert rendered["payload"]["error_type"] == "RuntimeError"


@pytest.mark.asyncio
async def test_low_simple_task_skips_planning_and_prefers_lightweight_execution(tmp_path):
    scripts = _initial("SIMPLE_TASK")
    scripts.update(
        {
            Stage.EXECUTION: [
                {"disposition": "COMPLETED", "summary": "Done."}
            ],
            Stage.FINALISATION: [{"report": "Done."}],
        }
    )
    provider = ScriptedProvider(scripts)

    result = await _runtime(tmp_path, provider).run_turn(
        "Do the simple task", "request-low", effort=Effort.LOW
    )

    assert result.terminal_state is TerminalState.COMPLETED
    stages = [request.stage for _profile, request in provider.requests]
    assert Stage.PLANNING not in stages
    profile, execution = next(
        pair for pair in provider.requests if pair[1].stage is Stage.EXECUTION
    )
    assert profile.name == "lightweight"
    assert execution.role == "lightweight"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("classification", "effort", "planning_required", "execution_profile"),
    [
        ("SIMPLE_TASK", "high", True, "lightweight"),
        ("COMPLEX_TASK", "low", False, "premium"),
    ],
)
async def test_representative_routing_matrix_edges(
    tmp_path, classification, effort, planning_required, execution_profile
):
    scripts = _initial(classification)
    if planning_required:
        scripts[Stage.PLANNING] = [{"plan": ["perform the bounded task"]}]
    scripts.update(
        {
            Stage.EXECUTION: [
                {"disposition": "COMPLETED", "summary": "Done."}
            ],
            Stage.FINALISATION: [{"report": "Done."}],
        }
    )
    provider = ScriptedProvider(scripts)

    result = await _runtime(tmp_path, provider).run_turn(
        "Do the work", f"request-matrix-{classification}-{effort}", effort=effort
    )

    stages = [request.stage for _profile, request in provider.requests]
    assert (Stage.PLANNING in stages) is planning_required
    profile, _request = next(
        item for item in provider.requests if item[1].stage is Stage.EXECUTION
    )
    assert profile.name == execution_profile
    assert result.terminal_state is TerminalState.COMPLETED


@pytest.mark.asyncio
async def test_normal_mode_enables_external_side_effect_authority(tmp_path):
    scripts = _initial("SIMPLE_TASK")
    scripts.update(
        {
            Stage.EXECUTION: [
                {"disposition": "COMPLETED", "summary": "Normal execution complete."}
            ],
            Stage.FINALISATION: [{"report": "Normal execution complete."}],
        }
    )
    provider = ScriptedProvider(scripts)
    result = await _runtime(tmp_path, provider).run_turn(
        "Complete the work", "request-normal", effort="low"
    )
    execution = next(
        request
        for _profile, request in provider.requests
        if request.stage is Stage.EXECUTION
    )
    assert execution.context["shadow_mode"] is False
    assert execution.allow_tools is True
    assert execution.allow_side_effects is True
    assert result.terminal_state is TerminalState.COMPLETED


@pytest.mark.asyncio
async def test_high_replanning_creates_new_plan_without_reconsulting_habits(tmp_path):
    scripts = _initial("COMPLEX_TASK")
    scripts.update(
        {
            Stage.PLANNING: [{"plan": ["old approach"]}],
            Stage.EXECUTION: [
                {
                    "disposition": "REPLAN_REQUIRED",
                    "summary": "Constraint discovered.",
                    "replan_reason": "The old API is unavailable.",
                    "evidence_refs": ["evidence:constraint"],
                },
                {
                    "disposition": "COMPLETED",
                    "summary": "Used the supported API.",
                    "evidence_refs": ["test:green"],
                },
            ],
            Stage.REPLANNING: [{"plan": ["supported approach"]}],
            Stage.FINALISATION: [{"report": "Completed with the supported API."}],
        }
    )
    habits = TrackingHabits()
    provider = ScriptedProvider(scripts)

    result = await _runtime(
        tmp_path,
        provider,
        config=_config(meditation_enabled=True),
        habits=habits,
    ).run_turn(
        "Implement safely", "request-replan", effort=Effort.HIGH
    )

    assert result.terminal_state is TerminalState.COMPLETED
    assert result.replan_count == 1
    assert result.ledger["plan_id"].endswith(":plan:v2")
    assert len(habits.retrievals) == 1
    replan_request = next(
        call for _profile, call in provider.requests if call.stage is Stage.REPLANNING
    )
    assert replan_request.context["habits_included"] is False
    assert "habits" not in replan_request.context
    assert replan_request.context["execution_evidence_refs"] == [
        "evidence:constraint"
    ]


@pytest.mark.asyncio
async def test_review_never_receives_habits_or_retrieves_them_again(tmp_path):
    scripts = _initial("COMPLEX_TASK")
    scripts.update(
        {
            Stage.PLANNING: [{"plan": ["complete the requested work"]}],
            Stage.EXECUTION: [
                {"disposition": "COMPLETED", "summary": "Work completed."}
            ],
            Stage.REVIEW: [{"outcome": "PASS", "summary": "Evidence is sufficient."}],
            Stage.FINALISATION: [{"report": "Completed and reviewed."}],
        }
    )
    habits = TrackingHabits()
    provider = ScriptedProvider(scripts)

    result = await _runtime(
        tmp_path,
        provider,
        config=_config(meditation_enabled=True),
        habits=habits,
    ).run_turn(
        "Complete this carefully", "request-review-no-habits", effort=Effort.XHIGH
    )

    assert result.terminal_state is TerminalState.COMPLETED
    assert len(habits.retrievals) == 1
    planning_request = next(
        request
        for _profile, request in provider.requests
        if request.stage is Stage.PLANNING
    )
    assert planning_request.context["habits"] == ["advisory habit"]
    review_request = next(
        request
        for _profile, request in provider.requests
        if request.stage is Stage.REVIEW
    )
    assert review_request.context["habits_included"] is False
    assert "habits" not in review_request.context


@pytest.mark.asyncio
async def test_replan_limit_cannot_turn_incomplete_work_into_success(tmp_path):
    scripts = _initial("COMPLEX_TASK")
    scripts.update(
        {
            Stage.PLANNING: [{"plan": ["attempt the constrained approach"]}],
            Stage.EXECUTION: [
                {
                    "disposition": "REPLAN_REQUIRED",
                    "summary": "The active approach cannot complete the goal.",
                    "replan_reason": "A required route is unavailable.",
                }
            ],
            Stage.FINALISATION: [
                {"report": "Work was abandoned because no Replan was authorised."}
            ],
        }
    )
    config = _config(replan_limits={"high": 0})

    result = await _runtime(
        tmp_path, ScriptedProvider(scripts), config=config
    ).run_turn("Complete the constrained task", "request-no-replan", effort="high")

    assert result.terminal_state is TerminalState.ABANDONED
    assert "Replanning limit" in result.limitations[0]


@pytest.mark.asyncio
async def test_xhigh_review_fail_performs_one_remediation_and_no_second_review(tmp_path):
    scripts = _initial("COMPLEX_TASK")
    scripts.update(
        {
            Stage.PLANNING: [{"plan": ["build"]}],
            Stage.EXECUTION: [
                {"disposition": "COMPLETED", "summary": "Candidate."},
                {"disposition": "COMPLETED", "summary": "Remediated."},
            ],
            Stage.REVIEW: [
                {
                    "outcome": "FAIL",
                    "summary": "A required check is missing.",
                    "findings": ["Add the missing check."],
                }
            ],
            Stage.REPLANNING: [{"plan": ["add check"]}],
            Stage.FINALISATION: [{"report": "Remediated and reported."}],
        }
    )
    provider = ScriptedProvider(scripts)

    result = await _runtime(tmp_path, provider).run_turn(
        "Build and review", "request-xhigh", effort=Effort.XHIGH
    )

    assert result.review_count == 1
    assert result.replan_count == 1
    assert sum(call.stage is Stage.REVIEW for _profile, call in provider.requests) == 1
    review = next(call for _profile, call in provider.requests if call.stage is Stage.REVIEW)
    assert review.allow_tools is False
    assert review.allow_side_effects is False


@pytest.mark.asyncio
async def test_max_review_and_remediation_are_bounded_at_three(tmp_path):
    scripts = _initial("HIGH_VOLUME_TASK")
    scripts.update(
        {
            Stage.PLANNING: [{"plan": ["orchestrate"]}],
            Stage.EXECUTION: [
                {"disposition": "COMPLETED", "summary": f"Candidate {index}."}
                for index in range(4)
            ],
            Stage.REVIEW: [
                {"outcome": "FAIL", "summary": f"Finding {index}."}
                for index in range(3)
            ],
            Stage.REPLANNING: [
                {"plan": [f"remediation {index}"]} for index in range(3)
            ],
            Stage.FINALISATION: [{"report": "Review limit reached honestly."}],
        }
    )
    provider = ScriptedProvider(scripts)

    result = await _runtime(tmp_path, provider).run_turn(
        "Process the large batch", "request-max", effort=Effort.MAX
    )

    assert result.review_count == 3
    assert result.replan_count == 3
    assert sum(call.stage is Stage.REVIEW for _profile, call in provider.requests) == 3
    assert result.terminal_state is TerminalState.COMPLETED_WITH_LIMITATIONS


@pytest.mark.asyncio
async def test_high_volume_subagents_are_bounded_and_cannot_replan_or_finalise(tmp_path):
    scripts = _initial("HIGH_VOLUME_TASK")
    scripts.update(
        {
            Stage.PLANNING: [
                {
                    "plan": ["delegate bounded research", "synthesise"],
                    "sub_agents": [
                        {
                            "id": "research-a",
                            "task": "Inspect source A only",
                            "profile": "lightweight",
                            "tools": ["file_read"],
                        },
                        {
                            "id": "research-b",
                            "task": "Inspect source B only",
                            "profile": "premium",
                            "tools": [],
                        },
                    ],
                }
            ],
            Stage.EXECUTION: [
                {
                    "disposition": "COMPLETED",
                    "summary": "Source A inspected.",
                    "evidence_refs": ["sub:a"],
                },
                {
                    "disposition": "REPLAN_REQUIRED",
                    "summary": "Sub-agent wants a different approach.",
                    "replan_reason": "Source B moved.",
                },
                lambda request: {
                    "disposition": "COMPLETED",
                    "summary": "Primary orchestrator synthesised bounded results.",
                    "evidence_refs": ["primary:synthesis"],
                    "observed_subagents": len(request.context["sub_agent_results"]),
                },
            ],
            Stage.REVIEW: [{"outcome": "PASS", "summary": "Acceptable."}],
            Stage.FINALISATION: [{"report": "Batch completed."}],
        }
    )
    provider = ScriptedProvider(scripts)

    result = await _runtime(tmp_path, provider).run_turn(
        "Process the batch", "request-subagents", effort=Effort.MAX
    )

    sub_requests = [
        request
        for _profile, request in provider.requests
        if request.role.startswith("sub_agent:")
    ]
    assert {request.role for request in sub_requests} == {
        "sub_agent:research-a",
        "sub_agent:research-b",
    }
    assert all(request.context["may_replan"] is False for request in sub_requests)
    assert all(request.context["may_finalise"] is False for request in sub_requests)
    assert all(request.context["may_create_subagents"] is False for request in sub_requests)
    assert next(
        request
        for request in sub_requests
        if request.role == "sub_agent:research-a"
    ).context["delegated_tools"] == ["file_read"]
    assert sum(call.stage is Stage.REPLANNING for _profile, call in provider.requests) == 0
    primary = next(
        request
        for _profile, request in provider.requests
        if request.stage is Stage.EXECUTION and not request.role.startswith("sub_agent:")
    )
    assert primary.role == "orchestrator"
    assert len(primary.context["sub_agent_results"]) == 2
    prohibited = next(
        item
        for item in primary.context["sub_agent_results"]
        if item["assignment_id"] == "research-b"
    )
    assert prohibited["disposition"] == "FAILED"
    assert "prohibited orchestration authority" in prohibited["summary"]
    assert result.terminal_state is TerminalState.COMPLETED
    assert result.evidence_refs == ("sub:a", "primary:synthesis")


@pytest.mark.asyncio
async def test_high_volume_subagent_limit_is_a_hard_boundary(tmp_path):
    scripts = _initial("HIGH_VOLUME_TASK")
    scripts[Stage.PLANNING] = [
        {
            "plan": ["too much delegation"],
            "sub_agents": [
                {"id": f"sub-{index}", "task": f"task {index}"}
                for index in range(3)
            ],
        }
    ]
    provider = ScriptedProvider(scripts)
    config = _config(max_subagents=2)

    result = await _runtime(tmp_path, provider, config=config).run_turn(
        "Large task", "request-subagent-limit", effort=Effort.MAX
    )

    assert result.terminal_state is TerminalState.ERROR
    assert "exceeds the 2 sub-agent limit" in result.error
    assert not any(
        request.role.startswith("sub_agent:") for _profile, request in provider.requests
    )


@pytest.mark.asyncio
async def test_normal_mode_honours_bounded_subagent_side_effect_requests(tmp_path):
    scripts = _initial("HIGH_VOLUME_TASK")
    scripts.update(
        {
            Stage.PLANNING: [
                {
                    "plan": ["delegate one bounded check", "synthesise"],
                    "sub_agents": [
                        {
                            "id": "bounded-check",
                            "task": "Inspect and propose an update",
                            "profile": "lightweight",
                            "tools": ["file_read", "file_write"],
                            "allow_side_effects": True,
                        }
                    ],
                }
            ],
            Stage.EXECUTION: [
                {
                    "disposition": "COMPLETED",
                    "summary": "Read-only inspection completed.",
                },
                {
                    "disposition": "COMPLETED",
                    "summary": "Normal synthesis completed.",
                },
            ],
            Stage.REVIEW: [{"outcome": "PASS", "summary": "Safe."}],
            Stage.FINALISATION: [{"report": "Normal run completed."}],
        }
    )
    provider = ScriptedProvider(scripts)

    result = await _runtime(tmp_path, provider).run_turn(
        "Safely apply a batch update", "request-normal-subagent", effort="max"
    )

    sub_request = next(
        request
        for _profile, request in provider.requests
        if request.role == "sub_agent:bounded-check"
    )
    assert sub_request.context["shadow_mode"] is False
    assert sub_request.allow_tools is True
    assert sub_request.allow_side_effects is True
    assert result.terminal_state is TerminalState.COMPLETED


@pytest.mark.asyncio
async def test_subagent_side_effect_flag_must_be_a_real_boolean(tmp_path):
    scripts = _initial("HIGH_VOLUME_TASK")
    scripts[Stage.PLANNING] = [
        {
            "plan": ["invalid delegation"],
            "sub_agents": [
                {
                    "id": "unsafe",
                    "task": "Attempt an update",
                    "allow_side_effects": "false",
                }
            ],
        }
    ]
    provider = ScriptedProvider(scripts)

    result = await _runtime(tmp_path, provider).run_turn(
        "Process safely", "request-invalid-side-effects", effort="max"
    )

    assert result.terminal_state is TerminalState.ERROR
    assert "allow_side_effects must be a boolean" in result.error
    assert not any(
        request.role.startswith("sub_agent:")
        for _profile, request in provider.requests
    )


@pytest.mark.asyncio
async def test_reporting_exhaustion_preserves_completed_execution(tmp_path):
    scripts = _initial("SIMPLE_TASK")
    scripts.update(
        {
            Stage.EXECUTION: [
                {
                    "disposition": "COMPLETED",
                    "summary": "The side effect completed.",
                    "evidence_refs": ["receipt:123"],
                }
            ],
            Stage.FINALISATION: [
                StageInvocationError("report provider unavailable") for _ in range(3)
            ],
        }
    )
    provider = ScriptedProvider(scripts)

    result = await _runtime(tmp_path, provider).run_turn(
        "Perform once", "request-report", effort=Effort.LOW
    )

    assert result.terminal_state is TerminalState.COMPLETED_WITH_REPORT_PENDING
    assert result.evidence_refs == ("receipt:123",)
    assert sum(call.stage is Stage.EXECUTION for _profile, call in provider.requests) == 1
    assert sum(call.stage is Stage.FINALISATION for _profile, call in provider.requests) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "disposition,terminal",
    [
        ("FAILED", TerminalState.FAILED),
        ("ABANDONED", TerminalState.ABANDONED),
    ],
)
async def test_unsuccessful_and_abandoned_execution_keep_truthful_terminal_states(
    tmp_path, disposition, terminal
):
    scripts = _initial("SIMPLE_TASK")
    scripts.update(
        {
            Stage.EXECUTION: [
                {
                    "disposition": disposition,
                    "summary": "The requested result could not be produced.",
                }
            ],
            Stage.FINALISATION: [{"report": "The requested result was not produced."}],
        }
    )
    result = await _runtime(tmp_path, ScriptedProvider(scripts)).run_turn(
        "Find the missing result", f"request-{disposition.lower()}", effort="low"
    )
    assert result.terminal_state is terminal


@pytest.mark.asyncio
async def test_missing_optional_commentary_does_not_fail_work(tmp_path):
    scripts = _initial("SIMPLE_TASK")
    scripts.update(
        {
            Stage.PLANNING: [{"plan": ["do the task"]}],
            Stage.EXECUTION: [
                {"disposition": "COMPLETED", "summary": "Done."}
            ],
            Stage.FINALISATION: [{"report": "Done."}],
        }
    )
    commentary = RecordingCommentaryPort()
    result = await _runtime(
        tmp_path, ScriptedProvider(scripts), commentary=commentary
    ).run_turn("Do it", "request-commentary", effort="medium")
    assert result.terminal_state is TerminalState.COMPLETED
    assert commentary.records == []


@pytest.mark.asyncio
async def test_only_successful_stage_results_publish_neutral_commentary(
    tmp_path,
):
    scripts = _initial("COMPLEX_TASK")
    scripts.update(
        {
            Stage.PLANNING: [
                {
                    "plan": ["Inspect the current route", "Apply the safe change"],
                    "success_criteria": ["The new route is verified"],
                    "commentary": "The plan is ready for execution.",
                }
            ],
            Stage.EXECUTION: [
                {
                    "disposition": "REPLAN_REQUIRED",
                    "summary": "The original route is unavailable.",
                    "replan_reason": "The API returned a permanent removal response.",
                    "evidence_refs": ["receipt:gone"],
                    "commentary": "The original route is unavailable.",
                },
                {
                    "disposition": "COMPLETED",
                    "summary": "The replacement route completed successfully.",
                    "evidence_refs": ["receipt:replacement"],
                    "commentary": "The replacement route completed successfully.",
                },
            ],
            Stage.REPLANNING: [
                {
                    "plan": ["Use the supported replacement route", "Verify the result"],
                    "success_criteria": ["Replacement receipt is present"],
                    "changed_because": "The original API was permanently removed.",
                    "commentary": "A supported replacement route is now planned.",
                }
            ],
            Stage.REVIEW: [
                {
                    "outcome": "PASS",
                    "summary": "The replacement receipt proves completion.",
                    "findings": [],
                    "commentary": "Independent review passed.",
                }
            ],
            Stage.FINALISATION: [
                {
                    "report": "Completed through the supported route.",
                    "commentary": "Finalisation must not publish commentary.",
                }
            ],
        }
    )
    provider = ScriptedProvider(scripts)
    commentary = RecordingCommentaryPort()

    result = await _runtime(
        tmp_path,
        provider,
        commentary=commentary,
    ).run_turn("Complete the routed task", "request-stage-commentary", effort="xhigh")

    assert result.terminal_state is TerminalState.COMPLETED
    assert [(item.stage, item.text) for item in commentary.records] == [
        (Stage.PLANNING, "The plan is ready for execution."),
        (Stage.EXECUTION, "The original route is unavailable."),
        (Stage.REPLANNING, "A supported replacement route is now planned."),
        (
            Stage.EXECUTION,
            "The replacement route completed successfully.",
        ),
        (Stage.REVIEW, "Independent review passed."),
    ]
    assert len({item.event_id for item in commentary.records}) == 5
    assert all(item.kind != "commentary" for item in result.delivery_records)

    planning_requests = [
        request
        for _profile, request in provider.requests
        if request.stage in {Stage.PLANNING, Stage.REPLANNING}
    ]
    assert planning_requests
    assert all("Persona " not in repr(request.context) for request in planning_requests)
    assert all("persona" not in request.context for request in planning_requests)


@pytest.mark.asyncio
async def test_commentary_port_failure_does_not_block_replanning_or_completion(
    tmp_path,
):
    scripts = _initial("COMPLEX_TASK")
    scripts.update(
        {
            Stage.PLANNING: [
                {
                    "plan": ["Try the primary route"],
                    "commentary": "The primary route will be tried.",
                }
            ],
            Stage.EXECUTION: [
                {
                    "disposition": "REPLAN_REQUIRED",
                    "summary": "The primary route is blocked.",
                    "replan_reason": "The required endpoint no longer exists.",
                    "commentary": "The primary route is blocked.",
                },
                {
                    "disposition": "COMPLETED",
                    "summary": "The alternate route worked.",
                    "commentary": "The alternate route worked.",
                },
            ],
            Stage.REPLANNING: [
                {
                    "plan": ["Use the alternate route"],
                    "changed_because": "The endpoint no longer exists.",
                    "commentary": "The plan now uses the alternate route.",
                }
            ],
            Stage.FINALISATION: [{"report": "Completed with the alternate route."}],
        }
    )
    commentary = RecordingCommentaryPort(fail=True)

    result = await _runtime(
        tmp_path,
        ScriptedProvider(scripts),
        commentary=commentary,
    ).run_turn("Complete the task", "request-renderer-fallback", effort="high")

    assert result.terminal_state is TerminalState.COMPLETED
    assert result.replan_count == 1
    assert commentary.records == []
    assert all(item.kind != "commentary" for item in result.delivery_records)


@pytest.mark.asyncio
async def test_invalid_optional_commentary_does_not_invalidate_stage_result(tmp_path):
    scripts = _initial("SIMPLE_TASK")
    scripts.update(
        {
            Stage.EXECUTION: [
                {
                    "disposition": "COMPLETED",
                    "summary": "Execution is complete.",
                    "commentary": {"invalid": "not a string"},
                }
            ],
            Stage.FINALISATION: [{"report": "Completed."}],
        }
    )
    commentary = RecordingCommentaryPort()

    result = await _runtime(
        tmp_path,
        ScriptedProvider(scripts),
        commentary=commentary,
        config=_config(user_idle_timeout_s=0.03, hard_timeout_s=5),
    ).run_turn("Complete quickly", "request-slow-commentary", effort="low")

    assert result.terminal_state is TerminalState.COMPLETED
    assert [item.kind for item in result.delivery_records].count("final") == 1
    assert commentary.records == []


@pytest.mark.asyncio
async def test_stage_failure_does_not_synthesise_commentary_from_lifecycle_events(
    tmp_path,
):
    scripts = _initial("COMPLEX_TASK")
    scripts[Stage.PLANNING] = [StageInvocationError("planner provider unavailable")]
    commentary = RecordingCommentaryPort()

    result = await _runtime(
        tmp_path,
        ScriptedProvider(scripts),
        commentary=commentary,
        config=_config(structured_repair_attempts=1),
    ).run_turn("Plan and execute", "request-planning-failure", effort="medium")

    assert result.terminal_state is TerminalState.ERROR
    assert commentary.records == []


@pytest.mark.asyncio
async def test_meaningful_progress_keeps_long_execution_alive(tmp_path):
    async def progressing(request):
        for index in range(6):
            request.progress_callback("tool_result", f"new evidence {index}", True)
            await asyncio.sleep(0.02)
        return {"disposition": "COMPLETED", "summary": "Progressed to completion."}

    scripts = _initial("SIMPLE_TASK")
    scripts.update(
        {
            Stage.EXECUTION: [progressing],
            Stage.FINALISATION: [{"report": "Completed."}],
        }
    )
    result = await _runtime(
        tmp_path,
        ScriptedProvider(scripts),
        config=_config(user_idle_timeout_s=0.05, hard_timeout_s=5),
    ).run_turn("Long but active", "request-progress", effort="low")
    assert result.terminal_state is TerminalState.COMPLETED


@pytest.mark.asyncio
async def test_repeated_false_progress_cannot_keep_stalled_execution_alive(tmp_path):
    async def false_progress(request):
        while True:
            request.progress_callback("commentary", "Still working", True)
            await asyncio.sleep(0.015)

    scripts = _initial("SIMPLE_TASK")
    scripts[Stage.EXECUTION] = [false_progress]
    provider = ScriptedProvider(scripts)
    result = await _runtime(
        tmp_path,
        provider,
        config=_config(user_idle_timeout_s=0.05, hard_timeout_s=5),
    ).run_turn("Stalled task", "request-false-progress", effort="low")

    assert result.terminal_state is TerminalState.ERROR
    assert "idle-progress timeout" in result.error
    assert provider.cancelled[Stage.EXECUTION] == 1


@pytest.mark.asyncio
async def test_reviewer_technical_failure_does_not_discard_execution(tmp_path):
    scripts = _initial("COMPLEX_TASK")
    scripts.update(
        {
            Stage.PLANNING: [{"plan": ["work"]}],
            Stage.EXECUTION: [
                {"disposition": "COMPLETED", "summary": "Work completed."}
            ],
            Stage.REVIEW: [StageInvocationError("reviewer offline")],
            Stage.FINALISATION: [{"report": "Completed; reviewer was unavailable."}],
        }
    )
    result = await _runtime(tmp_path, ScriptedProvider(scripts)).run_turn(
        "Complete robustly", "request-review-error", effort=Effort.XHIGH
    )
    assert result.terminal_state is TerminalState.COMPLETED_WITH_LIMITATIONS
    assert any("Review unavailable" in item for item in result.limitations)


@pytest.mark.asyncio
async def test_planning_stage_timeout_is_bounded_and_terminal_error(tmp_path):
    scripts = _initial("COMPLEX_TASK")
    scripts[Stage.PLANNING] = [{"plan": ["too slow"]}]
    provider = ScriptedProvider(scripts, delays={Stage.PLANNING: 0.05})

    result = await _runtime(
        tmp_path,
        provider,
        config=_config(profile_timeouts={"premium": 0.01}),
    ).run_turn("Plan this", "request-plan-timeout", effort="medium")

    assert result.terminal_state is TerminalState.ERROR
    assert "planning timed out" in result.error
    assert sum(
        request.stage is Stage.PLANNING for _profile, request in provider.requests
    ) == 1
    assert not any(
        request.stage is Stage.EXECUTION for _profile, request in provider.requests
    )


@pytest.mark.asyncio
async def test_review_timeout_preserves_execution_and_reaches_finalisation(tmp_path):
    scripts = _initial("COMPLEX_TASK")
    scripts.update(
        {
            Stage.PLANNING: [{"plan": ["complete the work"]}],
            Stage.EXECUTION: [
                {"disposition": "COMPLETED", "summary": "Work completed."}
            ],
            Stage.REVIEW: [{"outcome": "PASS", "summary": "Too late."}],
            Stage.FINALISATION: [
                {"report": "Completed; independent Review timed out."}
            ],
        }
    )
    provider = ScriptedProvider(scripts, delays={Stage.REVIEW: 0.05})

    result = await _runtime(
        tmp_path,
        provider,
        config=_config(profile_timeouts={"reviewer": 0.01}),
    ).run_turn("Complete and review", "request-review-timeout", effort="xhigh")

    assert result.terminal_state is TerminalState.COMPLETED_WITH_LIMITATIONS
    assert result.text == "Completed; independent Review timed out."
    assert result.review_count == 1


@pytest.mark.asyncio
async def test_reporting_timeout_retries_without_repeating_execution(tmp_path):
    scripts = _initial("SIMPLE_TASK")
    scripts.update(
        {
            Stage.EXECUTION: [
                {
                    "disposition": "COMPLETED",
                    "summary": "Side effect completed once.",
                    "evidence_refs": ["receipt:timeout-case"],
                }
            ],
            Stage.FINALISATION: [{"report": "Too late."}],
        }
    )
    provider = ScriptedProvider(scripts, delays={Stage.FINALISATION: 0.05})

    result = await _runtime(
        tmp_path,
        provider,
        config=_config(
            profile_timeouts={"premium": 0.01}, reporting_attempts=2
        ),
    ).run_turn("Perform once", "request-report-timeout", effort="low")

    assert result.terminal_state is TerminalState.COMPLETED_WITH_REPORT_PENDING
    assert result.evidence_refs == ("receipt:timeout-case",)
    assert sum(
        request.stage is Stage.EXECUTION for _profile, request in provider.requests
    ) == 1
    assert sum(
        request.stage is Stage.FINALISATION
        for _profile, request in provider.requests
    ) == 2


@pytest.mark.asyncio
async def test_hard_safety_timeout_cancels_progressing_provider(tmp_path):
    async def never_finishes(request):
        index = 0
        while True:
            request.progress_callback("tool_result", f"evidence-{index}", True)
            index += 1
            await asyncio.sleep(0.005)

    scripts = _initial("SIMPLE_TASK")
    scripts[Stage.EXECUTION] = [never_finishes]
    provider = ScriptedProvider(scripts)

    result = await _runtime(
        tmp_path,
        provider,
        config=_config(user_idle_timeout_s=0.03, hard_timeout_s=0.15),
    ).run_turn("Never finish", "request-hard-timeout", effort="low")

    assert result.terminal_state is TerminalState.ERROR
    assert "hard safety timeout" in result.error
    assert provider.cancelled[Stage.EXECUTION] == 1


@pytest.mark.asyncio
async def test_triage_recovers_unambiguous_control_json_from_reasoning(tmp_path):
    scripts = {
        Stage.IMMEDIATE_RESPONSE: [{"message": "I have it."}],
        Stage.TRIAGE: [
            StageResponse(
                text="",
                reasoning_trace=(
                    "The task requires work.\n"
                    '{"classification":"COMPLEX_TASK","goal":"Diagnose the fault"}'
                ),
                provider="fake-api",
                model="model-triage",
            )
        ],
        Stage.EXECUTION: [
            {"disposition": "COMPLETED", "summary": "Fault diagnosed."}
        ],
        Stage.FINALISATION: [{"report": "Fault diagnosed."}],
    }
    provider = ScriptedProvider(scripts)

    result = await _runtime(tmp_path, provider).run_turn(
        "Diagnose the fault", "request-reasoning-recovery", effort="low"
    )

    assert result.terminal_state is TerminalState.COMPLETED
    assert result.classification is TriageClassification.COMPLEX_TASK
    rows = [
        json.loads(line)
        for line in (tmp_path / "her-v2" / "audit.jsonl").read_text().splitlines()
    ]
    compatibility = next(
        row
        for row in rows
        if row["event"] == "structured_response_compatibility_applied"
        and row["stage"] == "triage"
    )
    assert compatibility["payload"]["validation_source"] == "reasoning_recovery"
    assert compatibility["payload"]["reasoning_exposed_to_user"] is False
    completed = next(
        row
        for row in rows
        if row["event"] == "stage_completed" and row["stage"] == "triage"
    )
    assert completed["payload"]["validation_source"] == "reasoning_recovery"


@pytest.mark.asyncio
async def test_execution_can_pause_for_newly_discovered_user_authority(tmp_path):
    scripts = _initial("SIMPLE_TASK")
    scripts[Stage.EXECUTION] = [
        {
            "disposition": "NEEDS_USER_INPUT",
            "summary": "Two accounts match the supplied name.",
            "clarification": "Which account should be changed?",
            "evidence_refs": ["lookup:accounts"],
        }
    ]
    provider = ScriptedProvider(scripts)

    result = await _runtime(tmp_path, provider).run_turn(
        "Change the account", "request-execution-user-input", effort="low"
    )

    assert result.terminal_state is TerminalState.PENDING_USER_INPUT
    assert result.text == "Which account should be changed?"
    assert result.evidence_refs == ("lookup:accounts",)
    assert result.ledger["terminal_reason"] == "execution_user_input_required"
    assert not any(
        request.stage in {Stage.REVIEW, Stage.FINALISATION}
        for _profile, request in provider.requests
    )


@pytest.mark.asyncio
async def test_execution_discovered_clarification_uses_required_persona_lane(tmp_path):
    scripts = _initial("SIMPLE_TASK")
    scripts[Stage.EXECUTION] = [
        {
            "disposition": "NEEDS_USER_INPUT",
            "summary": "Two accounts match.",
            "clarification": "Which account should be changed?",
        }
    ]
    renderer = RecordingRequiredPersonaRenderer()

    result = await _runtime(
        tmp_path,
        ScriptedProvider(scripts),
        required_persona=renderer,
    ).run_turn("Change the account", "request-execution-rendered-input", effort="low")

    assert result.terminal_state is TerminalState.PENDING_USER_INPUT
    assert result.text == (
        "Persona clarification:\n\nWhich account should be changed?"
    )
    assert [(message.kind, message.text) for message in renderer.messages] == [
        ("clarification", "Which account should be changed?")
    ]
    assert result.delivery_records[-1].kind == "clarification"
    assert result.delivery_records[-1].text == result.text
    assert result.ledger["terminal_reason"] == "execution_user_input_required"


@pytest.mark.asyncio
async def test_simple_classification_can_escalate_execution_capability_without_mutation(
    tmp_path,
):
    scripts = _initial("SIMPLE_TASK")
    scripts.update(
        {
            Stage.PLANNING: [{"plan": ["attempt safely"]}],
            Stage.EXECUTION: [
                {
                    "disposition": "REPLAN_REQUIRED",
                    "summary": "The lightweight route cannot verify the dependency.",
                    "replan_reason": "premium capability required",
                },
                {"disposition": "COMPLETED", "summary": "Verified and completed."},
            ],
            Stage.REPLANNING: [{"plan": ["use the capable verification route"]}],
            Stage.FINALISATION: [{"report": "Verified and completed."}],
        }
    )
    provider = ScriptedProvider(scripts)

    result = await _runtime(tmp_path, provider).run_turn(
        "Complete the simple request", "request-simple-escalation", effort="high"
    )

    assert result.terminal_state is TerminalState.COMPLETED
    assert result.classification is TriageClassification.SIMPLE_TASK
    execution_profiles = [
        profile.name
        for profile, request in provider.requests
        if request.stage is Stage.EXECUTION
    ]
    assert execution_profiles == ["lightweight", "premium"]
    second_execution = [
        request
        for _profile, request in provider.requests
        if request.stage is Stage.EXECUTION
    ][1]
    assert second_execution.classification is TriageClassification.SIMPLE_TASK
    assert second_execution.context["execution_capability_escalated"] is True


@pytest.mark.asyncio
async def test_side_effect_execution_bad_json_uses_tool_free_structure_repair_once(
    tmp_path,
):
    scripts = _initial("SIMPLE_TASK")
    scripts.update(
        {
            Stage.EXECUTION: [
                StageResponse(
                    text="The requested change completed, but this is not JSON.",
                    reasoning_trace="execution reasoning retained before validation",
                    provider="fake-api",
                    model="model-lightweight",
                    evidence_refs=("hashi-tools:receipt-1",),
                )
            ],
            Stage.STRUCTURE_REPAIR: [
                {
                    "disposition": "COMPLETED",
                    "summary": "The requested change completed.",
                    "evidence_refs": ["hashi-tools:receipt-1"],
                }
            ],
            Stage.FINALISATION: [{"report": "The requested change completed."}],
        }
    )
    provider = ScriptedProvider(scripts)
    runtime = _runtime(
        tmp_path,
        provider,
        config=_config(
            structured_repair_attempts=2,
            slot_models={
                "fast": "quick-route-model",
                "pro": "pro-route-model",
            },
            route_model_slots={"structure_repair": "pro"},
            route_reasoning={"structure_repair": "xhigh"},
        ),
    )

    result = await runtime.run_turn(
        "Make the requested change once",
        "request-execution-structure-repair",
        effort="low",
    )

    assert result.terminal_state is TerminalState.COMPLETED
    execution_requests = [
        request
        for _profile, request in provider.requests
        if request.stage is Stage.EXECUTION
    ]
    repair_requests = [
        request
        for _profile, request in provider.requests
        if request.stage is Stage.STRUCTURE_REPAIR
    ]
    assert len(execution_requests) == 1
    assert len(repair_requests) == 1
    assert execution_requests[0].allow_side_effects is True
    assert repair_requests[0].allow_tools is False
    assert repair_requests[0].allow_side_effects is False
    assert repair_requests[0].context["original_execution_must_not_be_replayed"] is True
    repair_profile = next(
        profile
        for profile, request in provider.requests
        if request.stage is Stage.STRUCTURE_REPAIR
    )
    assert repair_profile.model == "pro-route-model"
    assert repair_profile.reasoning == "xhigh"

    rows = [
        json.loads(line)
        for line in (tmp_path / "her-v2" / "audit.jsonl").read_text().splitlines()
    ]
    original_response = next(
        row
        for row in rows
        if row["event"] == "provider_response_received"
        and row["stage"] == "execution"
    )
    assert original_response["payload"]["text"].startswith(
        "The requested change completed"
    )
    assert original_response["payload"]["validation_pending"] is True
    execution_reasoning = next(
        row
        for row in rows
        if row["event"] == "reasoning_trace" and row["stage"] == "execution"
    )
    assert execution_reasoning["payload"]["trace"].startswith("execution reasoning")
    completed = next(
        row
        for row in rows
        if row["event"] == "stage_completed" and row["stage"] == "execution"
    )
    assert completed["payload"]["validation_source"] == "tool_free_structure_repair"
    assert any(row["event"] == "structure_repair_completed" for row in rows)


@pytest.mark.asyncio
async def test_exhausted_execution_structure_repair_requires_reconciliation_without_replay(
    tmp_path,
):
    scripts = _initial("SIMPLE_TASK")
    scripts.update(
        {
            Stage.EXECUTION: [
                StageResponse(
                    text="Non-empty execution reply without a JSON object.",
                    reasoning_trace="available execution reasoning",
                    provider="fake-api",
                    model="model-lightweight",
                    evidence_refs=("hashi-tools:uncertain-1",),
                )
            ],
            Stage.STRUCTURE_REPAIR: [
                StageResponse(text="still malformed"),
                StageResponse(text="also malformed"),
            ],
        }
    )
    provider = ScriptedProvider(scripts)

    result = await _runtime(
        tmp_path,
        provider,
        config=_config(structured_repair_attempts=2),
    ).run_turn(
        "Perform the external action once",
        "request-reconciliation-required",
        effort="low",
    )

    assert result.terminal_state is TerminalState.RECONCILIATION_REQUIRED
    assert result.ledger["status"] == "RECONCILIATION_REQUIRED"
    assert result.ledger["terminal_reason"] == "execution_outcome_unconfirmed"
    assert result.evidence_refs == ("hashi-tools:uncertain-1",)
    assert "was not replayed" in result.error
    assert sum(
        request.stage is Stage.EXECUTION
        for _profile, request in provider.requests
    ) == 1
    assert sum(
        request.stage is Stage.STRUCTURE_REPAIR
        for _profile, request in provider.requests
    ) == 2
    assert not any(
        request.stage is Stage.FINALISATION
        for _profile, request in provider.requests
    )

    rows = [
        json.loads(line)
        for line in (tmp_path / "her-v2" / "audit.jsonl").read_text().splitlines()
    ]
    assert any(row["event"] == "structure_repair_failed" for row in rows)
    reconciliation = next(
        row for row in rows if row["event"] == "execution_reconciliation_required"
    )
    assert reconciliation["payload"]["execution_replayed"] is False
    assert reconciliation["payload"]["automatic_retry_permitted"] is False


@pytest.mark.asyncio
async def test_structured_output_repair_is_bounded_and_preserves_classification(tmp_path):
    scripts = _initial("SIMPLE_TASK")
    scripts[Stage.TRIAGE] = [
        StageResponse(text="not json"),
        StageResponse(
            text='prefix {"classification":"simple-task","goal":"Do it"} suffix',
            reasoning_trace=None,
            provider="fake-api",
            model="model-triage",
        ),
    ]
    scripts.update(
        {
            Stage.EXECUTION: [
                {"disposition": "COMPLETED", "summary": "Done."}
            ],
            Stage.FINALISATION: [{"report": "Done."}],
        }
    )
    config = _config(structured_repair_attempts=2)
    provider = ScriptedProvider(scripts)

    result = await _runtime(tmp_path, provider, config=config).run_turn(
        "Do it", "request-repair", effort=Effort.LOW
    )

    assert result.classification is TriageClassification.SIMPLE_TASK
    triage_requests = [
        call for _profile, call in provider.requests if call.stage is Stage.TRIAGE
    ]
    assert len(triage_requests) == 2
    assert triage_requests[1].context["previous_structure_error"]["attempt"] == 1
    assert "no valid JSON" in triage_requests[1].context[
        "previous_structure_error"
    ]["error"]


@pytest.mark.asyncio
async def test_stop_cancels_active_execution_and_records_stopped(tmp_path):
    execution_started = asyncio.Event()

    async def blocking_execution(_request):
        execution_started.set()
        await asyncio.Event().wait()

    scripts = _initial("COMPLEX_TASK")
    scripts[Stage.EXECUTION] = [blocking_execution]
    provider = ScriptedProvider(scripts)
    runtime = _runtime(tmp_path, provider)
    task = asyncio.create_task(
        runtime.run_turn(
            "Long task", "request-stop", effort=Effort.LOW, turn_id="turn-stop"
        )
    )
    await asyncio.wait_for(execution_started.wait(), timeout=1)

    assert await runtime.stop_turn("turn-stop", reason="USER_STOP") is True
    result = await asyncio.wait_for(task, timeout=1)

    assert result.terminal_state is TerminalState.STOPPED
    assert result.ledger["terminal_reason"] == "USER_STOP"
    assert provider.cancelled[Stage.EXECUTION] == 1


@pytest.mark.asyncio
async def test_stop_cancels_all_active_high_volume_subagents(tmp_path):
    both_started = asyncio.Event()
    started_count = 0

    async def blocking_subagent(_request):
        nonlocal started_count
        started_count += 1
        if started_count == 2:
            both_started.set()
        await asyncio.Event().wait()

    scripts = _initial("HIGH_VOLUME_TASK")
    scripts.update(
        {
            Stage.PLANNING: [
                {
                    "plan": ["parallel work"],
                    "sub_agents": [
                        {"id": "one", "task": "bounded one"},
                        {"id": "two", "task": "bounded two"},
                    ],
                }
            ],
            Stage.EXECUTION: [blocking_subagent, blocking_subagent],
        }
    )
    provider = ScriptedProvider(scripts)
    runtime = _runtime(tmp_path, provider)
    turn = asyncio.create_task(
        runtime.run_turn(
            "Parallel task",
            "request-stop-subagents",
            effort="max",
            turn_id="turn-stop-subagents",
        )
    )
    await asyncio.wait_for(both_started.wait(), timeout=1)

    assert await runtime.stop_turn("turn-stop-subagents") is True
    result = await asyncio.wait_for(turn, timeout=1)

    assert result.terminal_state is TerminalState.STOPPED
    assert provider.cancelled[Stage.EXECUTION] == 2
    assert not any(
        request.stage is Stage.REVIEW for _profile, request in provider.requests
    )


@pytest.mark.asyncio
async def test_steer_stops_old_turn_and_new_turn_gets_fresh_triage(tmp_path):
    execution_started = asyncio.Event()

    async def blocking_execution(_request):
        execution_started.set()
        await asyncio.Event().wait()

    scripts = {
        Stage.IMMEDIATE_RESPONSE: [
            {"message": "Starting old work."},
            {"message": "New answer."},
        ],
        Stage.TRIAGE: [
            {"classification": "COMPLEX_TASK", "goal": "old"},
            {"classification": "DIRECT_RESPONSE", "goal": "new"},
        ],
        Stage.EXECUTION: [blocking_execution],
    }
    provider = ScriptedProvider(scripts)
    runtime = _runtime(tmp_path, provider)
    old_task = asyncio.create_task(
        runtime.run_turn("Old goal", "request-old", effort="low", turn_id="old-turn")
    )
    await asyncio.wait_for(execution_started.wait(), timeout=1)

    new_result = await runtime.steer(
        "old-turn", "New instruction", "request-new", effort="max"
    )
    old_result = await old_task

    assert old_result.terminal_state is TerminalState.STOPPED
    assert old_result.ledger["terminal_reason"] == "STEERED"
    assert new_result.turn_id != old_result.turn_id
    assert new_result.classification is TriageClassification.DIRECT_RESPONSE
    assert new_result.ledger["plan_id"] is None


class _FailWriter:
    def append(self, _record):
        raise OSError("audit offline")


@pytest.mark.asyncio
async def test_total_audit_failure_prevents_provider_and_side_effects(tmp_path):
    provider = ScriptedProvider(_initial("DIRECT_RESPONSE"))
    audit = DurableAuditLog(
        primary_writer=_FailWriter(), fallback_writer=_FailWriter()
    )
    runtime = _runtime(tmp_path, provider, audit=audit)

    result = await runtime.run_turn("Hello", "request-audit-fail")

    assert result.terminal_state is TerminalState.ERROR
    assert provider.requests == []


@pytest.mark.asyncio
async def test_total_audit_failure_obeys_configured_fail_closed_terminal(tmp_path):
    provider = ScriptedProvider(_initial("DIRECT_RESPONSE"))
    audit = DurableAuditLog(
        primary_writer=_FailWriter(), fallback_writer=_FailWriter()
    )
    runtime = _runtime(
        tmp_path,
        provider,
        audit=audit,
        config=_config(audit_failure_terminal="STOPPED"),
    )

    result = await runtime.run_turn("Hello", "request-audit-stop")

    assert result.terminal_state is TerminalState.STOPPED
    assert result.ledger["terminal_reason"].startswith("AUDIT_PERSISTENCE_FAILURE")
    assert provider.requests == []


@pytest.mark.asyncio
async def test_meditation_is_outside_live_completion_path(tmp_path):
    scripts = _initial("SIMPLE_TASK")
    scripts.update(
        {
            Stage.EXECUTION: [
                {"disposition": "COMPLETED", "summary": "Done."}
            ],
            Stage.FINALISATION: [{"report": "Done."}],
        }
    )
    habits = TrackingHabits(block_meditation=True)
    runtime = _runtime(
        tmp_path,
        ScriptedProvider(scripts),
        config=_config(meditation_enabled=True),
        habits=habits,
    )

    result = await runtime.run_turn("Do it", "request-meditate", effort="low")
    await asyncio.wait_for(habits.meditation_started.wait(), timeout=1)

    assert result.terminal_state is TerminalState.COMPLETED
    assert habits.meditations == []
    habits.release_meditation.set()
    await asyncio.sleep(0)
    assert len(habits.meditations) == 1
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_failed_execution_is_not_eligible_for_meditation(tmp_path):
    scripts = _initial("SIMPLE_TASK")
    scripts.update(
        {
            Stage.EXECUTION: [
                {"disposition": "FAILED", "summary": "No valid result."}
            ],
            Stage.FINALISATION: [{"report": "The task did not succeed."}],
        }
    )
    habits = TrackingHabits()
    runtime = _runtime(
        tmp_path,
        ScriptedProvider(scripts),
        config=_config(meditation_enabled=True),
        habits=habits,
    )

    result = await runtime.run_turn("Attempt it", "request-no-meditation", effort="low")
    await asyncio.sleep(0)

    assert result.terminal_state is TerminalState.FAILED
    assert habits.meditation_started.is_set() is False


@pytest.mark.asyncio
async def test_normal_mode_schedules_enabled_habit_writes(tmp_path):
    scripts = _initial("SIMPLE_TASK")
    scripts.update(
        {
            Stage.EXECUTION: [
                {"disposition": "COMPLETED", "summary": "Done."}
            ],
            Stage.FINALISATION: [{"report": "Done."}],
        }
    )
    habits = TrackingHabits()
    runtime = _runtime(
        tmp_path,
        ScriptedProvider(scripts),
        config=_config(meditation_enabled=True),
        habits=habits,
    )

    result = await runtime.run_turn("Do it", "request-normal-write", effort="low")
    await asyncio.wait_for(habits.meditation_started.wait(), timeout=1)

    assert result.terminal_state is TerminalState.COMPLETED
    assert habits.meditation_started.is_set() is True


@pytest.mark.asyncio
async def test_meditation_failure_cannot_change_completed_turn(tmp_path):
    scripts = _initial("SIMPLE_TASK")
    scripts.update(
        {
            Stage.EXECUTION: [
                {"disposition": "COMPLETED", "summary": "Done."}
            ],
            Stage.FINALISATION: [{"report": "Done."}],
        }
    )
    habits = FailingMeditation()
    runtime = _runtime(
        tmp_path,
        ScriptedProvider(scripts),
        config=_config(meditation_enabled=True),
        habits=habits,
    )

    result = await runtime.run_turn("Do it", "request-meditation-fail", effort="low")
    await asyncio.wait_for(habits.meditation_started.wait(), timeout=1)

    assert result.terminal_state is TerminalState.COMPLETED
    assert result.ledger["status"] == "COMPLETED"
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_dream_is_never_a_live_turn_dependency(tmp_path):
    dream = ExplodingDream()
    runtime = _runtime(
        tmp_path,
        ScriptedProvider(_initial("DIRECT_RESPONSE")),
        dream=dream,
    )

    result = await runtime.run_turn("Hello", "request-dream-boundary")

    assert result.terminal_state is TerminalState.COMPLETED
    assert dream.calls == 0


@pytest.mark.asyncio
async def test_disabled_habit_pipeline_preserves_the_plain_planning_path(tmp_path):
    scripts = _initial("COMPLEX_TASK")
    scripts.update(
        {
            Stage.PLANNING: [{"plan": ["complete the request"]}],
            Stage.EXECUTION: [
                {"disposition": "COMPLETED", "summary": "Done."}
            ],
            Stage.FINALISATION: [{"report": "Done."}],
        }
    )

    class ForbiddenLearning:
        async def retrieve(self, **_kwargs):
            raise AssertionError("disabled Habit pipeline was consulted")

        async def meditate(self, **_kwargs):
            raise AssertionError("disabled Meditation was scheduled")

    provider = ScriptedProvider(scripts)
    runtime = _runtime(tmp_path, provider, habits=ForbiddenLearning())
    result = await runtime.run_turn(
        "Complete the request", "request-habits-disabled", effort="medium"
    )

    planning_request = next(
        request
        for _profile, request in provider.requests
        if request.stage is Stage.PLANNING
    )
    assert result.terminal_state is TerminalState.COMPLETED
    assert planning_request.context == {"classification": "COMPLEX_TASK"}
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_habit_retrieval_error_fails_open_without_skip_audit(tmp_path):
    scripts = _initial("COMPLEX_TASK")
    scripts.update(
        {
            Stage.PLANNING: [{"plan": ["complete the request"]}],
            Stage.EXECUTION: [
                {"disposition": "COMPLETED", "summary": "Done."}
            ],
            Stage.FINALISATION: [{"report": "Done."}],
        }
    )

    class BrokenRetrieval:
        def __init__(self):
            self.calls = 0

        async def retrieve(self, **_kwargs):
            self.calls += 1
            raise OSError("injected Habit catalogue failure")

    broken = BrokenRetrieval()
    meditation = TrackingHabits()
    provider = ScriptedProvider(scripts)
    runtime = _runtime(
        tmp_path,
        provider,
        config=_config(meditation_enabled=True),
        habits=broken,
        meditation=meditation,
    )
    result = await runtime.run_turn(
        "Complete the request", "request-habit-retrieval-failure", effort="medium"
    )
    await asyncio.wait_for(meditation.meditation_started.wait(), timeout=1)

    planning_request = next(
        request
        for _profile, request in provider.requests
        if request.stage is Stage.PLANNING
    )
    assert result.terminal_state is TerminalState.COMPLETED
    assert broken.calls == 1
    assert planning_request.context["habits"] == []
    audit_events = {
        json.loads(line)["event"]
        for line in (tmp_path / "her-v2" / "audit.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    }
    assert "habit_planning_skipped" not in audit_events
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_low_effort_meditation_starts_after_final_delivery_boundary(tmp_path):
    scripts = _initial("SIMPLE_TASK")
    scripts.update(
        {
            Stage.EXECUTION: [
                {"disposition": "COMPLETED", "summary": "Done."}
            ],
            Stage.FINALISATION: [{"report": "Done."}],
        }
    )
    order = []
    persisted_states = []

    class OrderedDelivery(RecordingDelivery):
        async def deliver(self, **kwargs):
            accepted = await super().deliver(**kwargs)
            if kwargs["kind"] == "final":
                order.append("final_boundary_accepted")
            return accepted

    class OrderedMeditation:
        def __init__(self):
            self.started = asyncio.Event()

        async def meditate(self, **kwargs):
            persisted_states.append(
                runtime.ledger_store.load(kwargs["turn_id"]).status.value
            )
            order.append("meditation_started")
            self.started.set()

    class ForbiddenLowEffortRetrieval:
        async def retrieve(self, **_kwargs):
            raise AssertionError("low effort must not add an initial Planning read")

    meditation = OrderedMeditation()
    runtime = _runtime(
        tmp_path,
        ScriptedProvider(scripts),
        config=_config(meditation_enabled=True),
        delivery=OrderedDelivery(),
        habits=ForbiddenLowEffortRetrieval(),
        meditation=meditation,
    )
    result = await runtime.run_turn(
        "Do it", "request-low-meditation-order", effort="low"
    )
    await asyncio.wait_for(meditation.started.wait(), timeout=1)

    assert result.terminal_state is TerminalState.COMPLETED
    assert order == ["final_boundary_accepted", "meditation_started"]
    assert persisted_states == ["COMPLETED"]
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_audit_records_trace_or_explicit_unavailability_with_correlation(tmp_path):
    scripts = _initial("DIRECT_RESPONSE")
    scripts[Stage.TRIAGE] = [
        StageResponse(
            text="",
            data={"classification": "DIRECT_RESPONSE", "goal": "Hello"},
            reasoning_trace=None,
            provider="fake-api",
            model="model-triage",
        )
    ]
    runtime = _runtime(tmp_path, ScriptedProvider(scripts))
    result = await runtime.run_turn("Hello", "request-audit")

    rows = [
        json.loads(line)
        for line in (tmp_path / "her-v2" / "audit.jsonl").read_text().splitlines()
    ]
    reasoning = [row for row in rows if row["event"] == "reasoning_trace"]
    assert {row["stage"] for row in reasoning} == {
        "immediate_response",
        "triage",
    }
    triage = next(row for row in reasoning if row["stage"] == "triage")
    assert triage["payload"] == {"availability": "unavailable"}
    assert triage["turn_id"] == result.turn_id
    assert triage["request_ref"] == "hashi-request:request-audit"
    assert triage["provider"] == "fake-api"
    assert triage["model"] == "model-triage"
    assert triage["attempt"] == 1
