import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from adapters.base import BackendResponse
from adapters.hashi_api import HashiApiAdapter
from adapters.her_v2_provider import (
    HashiStageProvider,
    _CognitiveControlToolRegistry,
    _DelegatedToolRegistry,
)
from adapters.openrouter_api import _APIResult
from orchestrator.her_v2.cognitive_control import (
    COGNITIVE_DECISION_TOOL,
    StageCognitiveController,
    canonical_tool_arguments,
    canonical_tool_result,
)
from orchestrator.her_v2.config import ProviderProfile
from orchestrator.her_v2.models import Effort, Stage, StageRequest
from orchestrator.her_v2.task_state import (
    HASHI_TASK_DELTA_ARGUMENT,
    HERTaskState,
)
from tools.registry import ToolResult

_TOOLS = ("probe_a", "probe_b", "probe_c", "probe_d", "probe_e")


def _request(stage: Stage, *, task_state=None) -> StageRequest:
    return StageRequest(
        turn_id="turn-cognitive-1",
        request_ref="request-cognitive-1",
        stage=stage,
        role=stage.value,
        attempt=1,
        goal="Finish the task without repeating unchanged inspections.",
        classification=None,
        effort=Effort.MEDIUM,
        allow_tools=True,
        allow_side_effects=True,
        invocation_id=f"turn-cognitive-1:{stage.value}:1",
        task_state=task_state,
    )


class _Registry:
    max_loops = None

    def __init__(self, names=_TOOLS):
        self.names = tuple(names)
        self.calls = []

    def get_tool_definitions(self, tiers=None):
        del tiers
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": f"Run {name}.",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
            for name in self.names
        ]

    def is_allowed(self, name):
        return name in self.names

    def is_read_only(self, name):
        return name in self.names

    async def execute(self, name, arguments, tool_call_id=""):
        self.calls.append((name, arguments, tool_call_id))
        serial = len(self.calls)
        return ToolResult(
            tool_call_id=tool_call_id,
            output=(
                json.dumps(
                    {
                        "status": "success",
                        "effect": "observed",
                        "data": {"tool": name, "value": "unchanged"},
                        "warning": (
                            {"code": "same_result_repeated"}
                            if serial > len(self.names)
                            else None
                        ),
                    },
                    sort_keys=True,
                )
                + "\n\nHASHI_EVIDENCE_RECEIPT: "
                + f"hashi-tool:turn:planning:call:call-{serial}:receipt:{serial}"
            ),
            details={
                "smart_effect": "observed",
                "receipt_serial": serial,
                "evidence_ref": f"receipt-{serial}",
            },
        )


def _tool_names(definitions):
    return [item["function"]["name"] for item in definitions]


def test_semantic_result_ignores_receipts_timestamps_and_repeat_warning():
    first = canonical_tool_result(
        json.dumps(
            {
                "status": "success",
                "effect": "observed",
                "data": {"value": 7},
                "warning": None,
            }
        )
        + "\nHASHI_EVIDENCE_RECEIPT: hashi-tool:a:b:call:c:receipt:1",
        {
            "receipt_serial": 1,
            "evidence_ref": "one",
            "timestamp": "2026-09-01T00:00:00Z",
            "duration_ms": 10,
            "smart_effect": "observed",
        },
    )
    second = canonical_tool_result(
        json.dumps(
            {
                "status": "success",
                "effect": "observed",
                "data": {"value": 7},
                "warning": {"code": "same_result_repeated"},
            }
        )
        + "\nHASHI_EVIDENCE_RECEIPT: hashi-tool:a:b:call:d:receipt:2"
        + '\nHASHI_TASK_STATE: {"version":2,"focus":"different"}',
        {
            "receipt_serial": 2,
            "evidence_ref": "two",
            "timestamp": "2026-09-01T00:00:05Z",
            "duration_ms": 30,
            "smart_effect": "observed",
        },
    )

    assert first == second


def test_semantic_arguments_preserve_target_ids_and_time_ranges():
    first = canonical_tool_arguments(
        {"request_id": "request-a", "since": "2026-09-01T00:00:00Z"}
    )
    second = canonical_tool_arguments(
        {"request_id": "request-b", "since": "2026-09-01T00:05:00Z"}
    )

    assert first != second


@pytest.mark.parametrize(
    "stage",
    [
        Stage.DIRECT,
        Stage.TRIAGE,
        Stage.PLANNING,
        Stage.EXECUTION,
        Stage.REPLANNING,
        Stage.REVIEW,
    ],
)
def test_a_b_c_d_e_cycle_is_detected_in_every_tool_stage(stage):
    controller = StageCognitiveController(stage=stage.value, goal="same goal")
    interrupt = None

    for cycle in range(3):
        for index, tool_name in enumerate(_TOOLS):
            interrupt = controller.observe(
                tool_name=tool_name,
                arguments={"path": f"artifact-{index}"},
                output=json.dumps(
                    {
                        "status": "success",
                        "effect": "observed",
                        "data": {"value": index},
                        "warning": (
                            {"code": "same_result_repeated"} if cycle else None
                        ),
                    }
                )
                + "\nHASHI_EVIDENCE_RECEIPT: "
                + f"hashi-tool:turn:{stage.value}:call:{cycle}-{index}:receipt:{cycle * 5 + index}",
                details={
                    "smart_effect": "observed",
                    "receipt_serial": cycle * 5 + index,
                },
                is_error=False,
            )
            if cycle < 2 or index < 4:
                assert interrupt is None

    assert interrupt is not None
    assert interrupt.code == "NO_NEW_INFORMATION_CYCLE"
    assert interrupt.cycle_period == 5
    assert interrupt.cycle_repetitions == 3
    assert interrupt.cycle_tools == _TOOLS
    assert controller.awaiting_decision is True


def test_same_actions_with_changing_results_are_not_a_dead_cycle():
    controller = StageCognitiveController(stage="execution", goal="run evolving test")

    for cycle in range(4):
        for index, tool_name in enumerate(_TOOLS):
            assert (
                controller.observe(
                    tool_name=tool_name,
                    arguments={"target": index},
                    output=json.dumps({"cycle": cycle, "test_failures": 4 - cycle}),
                    details={"smart_effect": "observed"},
                    is_error=False,
                )
                is None
            )

    assert controller.awaiting_decision is False
    assert controller.snapshot()["interrupt_count"] == 0


def test_unchanged_polling_cycle_remains_available():
    controller = StageCognitiveController(stage="execution", goal="wait for job")

    for cycle in range(5):
        for tool_name in ("background_job_status", "background_job_tail"):
            assert (
                controller.observe(
                    tool_name=tool_name,
                    tool_profile="poll",
                    arguments={"job_id": "job-1"},
                    output=json.dumps({"status": "running", "output": "unchanged"}),
                    details={"smart_effect": "observed"},
                    is_error=False,
                )
                is None
            )

    assert controller.awaiting_decision is False
    assert controller.snapshot()["interrupt_count"] == 0


@pytest.mark.asyncio
async def test_inline_task_delta_is_advertised_stripped_and_evidence_bound():
    task_state = HERTaskState(goal="Resolve the evidence question")
    base = _Registry(("probe_a", "probe_b"))
    registry = _CognitiveControlToolRegistry(
        base,
        _request(Stage.PLANNING, task_state=task_state),
    )

    parameters = registry.get_tool_definitions()[0]["function"]["parameters"]
    assert HASHI_TASK_DELTA_ARGUMENT in parameters["required"]
    assert HASHI_TASK_DELTA_ARGUMENT in parameters["properties"]

    first = await registry.execute(
        "probe_a",
        {
            "path": "first",
            HASHI_TASK_DELTA_ARGUMENT: {
                "delta_id": "planning-1",
                "add_questions": [
                    {"id": "q-source", "question": "Which source is authoritative?"}
                ],
                "set_focus": {
                    "target_id": "q-source",
                    "intent": "Identify the authoritative source",
                    "expected_change": "One source becomes evidence-backed",
                },
            },
        },
        "task-state-1",
    )
    assert base.calls[0][1] == {"path": "first"}
    assert "HASHI_TASK_STATE:" in first.output

    second = await registry.execute(
        "probe_b",
        {
            "path": "second",
            HASHI_TASK_DELTA_ARGUMENT: {
                "delta_id": "planning-2",
                "add_facts": [
                    {
                        "id": "f-source",
                        "claim": "The first source is authoritative.",
                        "evidence_refs": ["receipt-1"],
                    }
                ],
                "resolve_questions": [
                    {"id": "q-source", "evidence_refs": ["receipt-1"]}
                ],
            },
        },
        "task-state-2",
    )

    assert base.calls[1][1] == {"path": "second"}
    assert len(base.calls) == 2
    assert second.details["task_state_delta"]["meaningful_progress"] is True
    snapshot = task_state.snapshot()
    assert snapshot["facts"][0]["id"] == "f-source"
    assert snapshot["open_questions"][0]["status"] == "resolved"
    assert snapshot["model_delta_count"] == 2


@pytest.mark.asyncio
async def test_parallel_calls_with_one_delta_id_count_as_one_cognitive_boundary():
    task_state = HERTaskState(goal="Inspect independent sources in parallel")
    registry = _CognitiveControlToolRegistry(
        _Registry(),
        _request(Stage.EXECUTION, task_state=task_state),
    )
    shared_delta = {
        "delta_id": "parallel-turn-1",
        "set_focus": {
            "target_id": "q-sources",
            "intent": "Inspect the independent sources",
            "expected_change": "At least one source discriminates the options",
        },
    }

    for index, name in enumerate(("probe_a", "probe_b", "probe_c"), start=1):
        result = await registry.execute(
            name,
            {
                "target": index,
                HASHI_TASK_DELTA_ARGUMENT: shared_delta,
            },
            f"parallel-{index}",
        )
        assert "HASHI_COGNITIVE_INTERRUPT" not in result.output

    assert task_state.snapshot()["model_delta_count"] == 1
    assert registry.controller.snapshot()["progress_stall_count"] == 1


@pytest.mark.asyncio
async def test_varied_actions_with_unchanged_task_state_trigger_progress_interrupt():
    task_state = HERTaskState(goal="Find one decisive fact")
    registry = _CognitiveControlToolRegistry(
        _Registry(),
        _request(Stage.EXECUTION, task_state=task_state),
    )

    result = None
    for index, name in enumerate(("probe_a", "probe_b", "probe_c"), start=1):
        result = await registry.execute(
            name,
            {
                "target": index,
                HASHI_TASK_DELTA_ARGUMENT: {
                    "delta_id": f"stalled-{index}",
                    "set_focus": {
                        "target_id": "q-decisive",
                        "intent": "Keep inspecting",
                        "expected_change": "A decisive fact appears",
                    },
                },
            },
            f"stalled-{index}",
        )

    assert result is not None
    assert "HASHI_COGNITIVE_INTERRUPT" in result.output
    interrupt = registry.controller.interrupt_payload()["interrupt"]
    assert interrupt["code"] == "NO_MEANINGFUL_PROGRESS"
    assert interrupt["detection_kind"] == "task_state_stagnation"
    assert interrupt["stall_count"] == 3
    assert interrupt["repeated_after_intervention"] is False
    assert _tool_names(registry.get_tool_definitions()) == [COGNITIVE_DECISION_TOOL]

    decision = await registry.execute(
        COGNITIVE_DECISION_TOOL,
        {
            "decision": "REVISE_DIRECTION",
            "new_focus": "q-alternate",
            "revised_direction": "Inspect the alternate evidence sources.",
            "expected_change": "An evidence-bound fact or discarded path appears.",
            "stop_condition": "Stop after one bounded alternate pass.",
            "requested_tools": list(_TOOLS),
        },
        "stalled-decision",
    )
    assert decision.is_error is False

    for index, name in enumerate(("probe_d", "probe_e", "probe_a"), start=4):
        result = await registry.execute(
            name,
            {
                "target": index,
                HASHI_TASK_DELTA_ARGUMENT: {"delta_id": f"restalled-{index}"},
            },
            f"restalled-{index}",
        )

    terminal = registry.controller.interrupt_payload()["interrupt"]
    assert terminal["code"] == "NO_MEANINGFUL_PROGRESS"
    assert terminal["detection_kind"] == "task_state_stagnation"
    assert terminal["repeated_after_intervention"] is True
    schema = registry.get_tool_definitions()[0]
    assert schema["function"]["parameters"]["properties"]["decision"]["enum"] == [
        "FINALIZE",
        "BLOCKED",
    ]


@pytest.mark.asyncio
async def test_evidence_progress_resets_stall_before_later_stagnation():
    task_state = HERTaskState(goal="Resolve a question, then finish")
    registry = _CognitiveControlToolRegistry(
        _Registry(),
        _request(Stage.EXECUTION, task_state=task_state),
    )

    first = await registry.execute(
        "probe_a",
        {
            "target": 1,
            HASHI_TASK_DELTA_ARGUMENT: {
                "delta_id": "progress-1",
                "add_questions": [{"id": "q1", "question": "What is true?"}],
            },
        },
        "progress-1",
    )
    assert "HASHI_COGNITIVE_INTERRUPT" not in first.output

    progressed = await registry.execute(
        "probe_b",
        {
            "target": 2,
            HASHI_TASK_DELTA_ARGUMENT: {
                "delta_id": "progress-2",
                "add_facts": [
                    {
                        "id": "f1",
                        "claim": "The first probe answered the question.",
                        "evidence_refs": ["receipt-1"],
                    }
                ],
                "resolve_questions": [{"id": "q1", "evidence_refs": ["receipt-1"]}],
            },
        },
        "progress-2",
    )
    assert "HASHI_COGNITIVE_INTERRUPT" not in progressed.output
    assert registry.controller.snapshot()["progress_stall_count"] == 0

    result = None
    for index, name in enumerate(("probe_c", "probe_d", "probe_e"), start=3):
        result = await registry.execute(
            name,
            {
                "target": index,
                HASHI_TASK_DELTA_ARGUMENT: {"delta_id": f"progress-{index}"},
            },
            f"progress-{index}",
        )

    assert result is not None
    interrupt = registry.controller.interrupt_payload()["interrupt"]
    assert interrupt["detection_kind"] == "task_state_stagnation"
    assert interrupt["stall_count"] == 3


@pytest.mark.asyncio
async def test_revised_direction_reopens_only_named_tools_and_repeat_escalates():
    base = _Registry()
    registry = _CognitiveControlToolRegistry(base, _request(Stage.PLANNING))

    for cycle in range(3):
        for index, name in enumerate(_TOOLS):
            result = await registry.execute(
                name,
                {"path": f"artifact-{index}"},
                f"call-{cycle}-{index}",
            )

    assert "HASHI_COGNITIVE_INTERRUPT" in result.output
    assert _tool_names(registry.get_tool_definitions()) == [COGNITIVE_DECISION_TOOL]

    decision = await registry.execute(
        COGNITIVE_DECISION_TOOL,
        {
            "decision": "REVISE_DIRECTION",
            "new_focus": "representation-agreement",
            "revised_direction": "Inspect a different representation.",
            "expected_change": "A representation-specific mismatch.",
            "stop_condition": "Stop after one pass or the first mismatch.",
            "requested_tools": list(_TOOLS),
        },
        "decision-1",
    )

    assert decision.is_error is False
    assert _tool_names(registry.get_tool_definitions()) == list(_TOOLS)

    for cycle in range(3):
        for index, name in enumerate(_TOOLS):
            result = await registry.execute(
                name,
                {"path": f"artifact-{index}"},
                f"repeat-{cycle}-{index}",
            )

    payload = registry.controller.interrupt_payload()
    assert payload["interrupt"]["code"] == "NO_MEANINGFUL_PROGRESS"
    assert payload["interrupt"]["repeated_after_intervention"] is True
    decision_schema = registry.get_tool_definitions()[0]
    assert decision_schema["function"]["parameters"]["properties"]["decision"][
        "enum"
    ] == ["FINALIZE", "BLOCKED"]

    blocked = await registry.execute(
        COGNITIVE_DECISION_TOOL,
        {"decision": "BLOCKED", "summary": "No distinct evidence is available."},
        "decision-2",
    )
    assert blocked.is_error is False
    assert registry.get_tool_definitions() == []
    assert registry.cognitive_final_response_required is True


def test_v1_new_hypothesis_payload_is_normalised_as_inflight_alias():
    controller = StageCognitiveController(stage="execution", goal="compatibility")
    for index in range(3):
        interrupt = controller.observe(
            tool_name="probe_a",
            arguments={"target": "same"},
            output="unchanged",
            details={"smart_effect": "observed"},
            is_error=False,
        )
    assert interrupt is not None

    decision, rejected = controller.decide(
        {
            "decision": "NEW_HYPOTHESIS",
            "hypothesis": "Inspect the alternate representation.",
            "unresolved_question": "Does the alternate representation differ?",
            "expected_distinct_evidence": "A representation-specific mismatch.",
            "stop_condition": "Stop after one alternate probe.",
            "requested_tools": ["probe_a"],
        },
        available_tools=["probe_a"],
    )

    assert rejected is False
    assert decision["decision"] == "REVISE_DIRECTION"
    assert decision["legacy_alias_used"] is True


@pytest.mark.asyncio
async def test_delegated_registry_keeps_authority_narrow_at_cognitive_boundary():
    base = _Registry(("probe_a", "probe_b"))
    delegated = _DelegatedToolRegistry(base, ["probe_a"], read_only=True)
    registry = _CognitiveControlToolRegistry(
        delegated,
        _request(Stage.EXECUTION),
    )

    for cycle in range(3):
        result = await registry.execute(
            "probe_a",
            {"path": "same-artifact"},
            f"delegated-{cycle}",
        )

    assert "HASHI_COGNITIVE_INTERRUPT" in result.output
    assert _tool_names(registry.get_tool_definitions()) == [COGNITIVE_DECISION_TOOL]
    assert [call[0] for call in base.calls] == ["probe_a"] * 3

    rejected = await registry.execute(
        COGNITIVE_DECISION_TOOL,
        {
            "decision": "REVISE_DIRECTION",
            "new_focus": "alternate-probe",
            "revised_direction": "Use the non-delegated probe.",
            "expected_change": "A different probe result.",
            "stop_condition": "One probe.",
            "requested_tools": ["probe_b"],
        },
        "delegated-decision",
    )

    assert rejected.is_error is True
    assert json.loads(rejected.output)["unknown_tools"] == ["probe_b"]
    assert _tool_names(registry.get_tool_definitions()) == [COGNITIVE_DECISION_TOOL]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("decision_payload", "expected_tools", "expected_decision"),
    [
        (
            {
                "decision": "REVISE_DIRECTION",
                "new_focus": "alternate-source",
                "revised_direction": "Inspect the alternate source.",
                "expected_change": "A different value.",
                "stop_condition": "One alternate inspection.",
                "requested_tools": ["probe_a"],
            },
            ["probe_a"],
            "REVISE_DIRECTION",
        ),
        (
            {
                "decision": "FINALIZE",
                "summary": "The repeated probes already answer the question.",
            },
            [],
            "FINALIZE",
        ),
    ],
)
async def test_hashi_gateway_tool_loop_enters_typed_decision_boundary(
    monkeypatch,
    tmp_path,
    decision_payload,
    expected_tools,
    expected_decision,
):
    config = SimpleNamespace(
        name="agent1",
        engine="hashi-api",
        model="gpt-5.6-luna",
        workspace_dir=tmp_path,
        system_md=None,
        extra={"effort": "low"},
    )
    global_config = SimpleNamespace(
        her_providers={
            "providers": {"hashi": {"base_url": "http://127.0.0.1:18805/v1"}}
        }
    )
    adapter = HashiApiAdapter(config, global_config)
    base = _Registry(("probe_a",))
    registry = _CognitiveControlToolRegistry(base, _request(Stage.EXECUTION))
    adapter.tool_registry = registry
    monkeypatch.setattr(adapter, "_ensure_client", lambda: None)

    payloads = []

    async def call_api_once(payload, headers, on_stream_event):
        del headers, on_stream_event
        payloads.append(payload)
        call_number = len(payloads)
        if call_number <= 3:
            return _APIResult(
                text="",
                tool_calls=[
                    {
                        "id": f"probe-{call_number}",
                        "type": "function",
                        "function": {"name": "probe_a", "arguments": "{}"},
                    }
                ],
                finish_reason="tool_calls",
            )
        if call_number == 4:
            assert [item["function"]["name"] for item in payload.get("tools", [])] == [
                COGNITIVE_DECISION_TOOL
            ]
            return _APIResult(
                text="",
                tool_calls=[
                    {
                        "id": "decision-1",
                        "type": "function",
                        "function": {
                            "name": COGNITIVE_DECISION_TOOL,
                            "arguments": json.dumps(decision_payload),
                        },
                    }
                ],
                finish_reason="tool_calls",
            )
        assert [
            item["function"]["name"] for item in payload.get("tools", [])
        ] == expected_tools
        return _APIResult(
            text="completed after a distinct cognitive decision",
            tool_calls=None,
            finish_reason="stop",
        )

    monkeypatch.setattr(adapter, "_call_api_once", call_api_once)

    response = await adapter.generate_response("finish safely", "request-1")

    assert response.is_success is True
    assert response.text == "completed after a distinct cognitive decision"
    assert len(payloads) == 5
    registry.note_provider_completion()
    state = registry.controller.snapshot()
    assert state["last_decision"] == expected_decision
    assert state["mode"] == "completed"
    assert any(
        "HASHI_COGNITIVE_INTERRUPT" in str(message.get("content") or "")
        for message in payloads[3]["messages"]
    )


@pytest.mark.asyncio
async def test_stage_provider_installs_control_and_exports_typed_state():
    base = _Registry()

    class Backend:
        def __init__(self):
            self.config = SimpleNamespace(extra={}, name="agent1", system_md=None)
            self.capabilities = SimpleNamespace(supports_tool_use=True)
            self.tool_registry = None
            self.sys_prompt = "configured agent persona"
            self.shutdown_called = False

        def set_reasoning_enabled(self, enabled):
            del enabled

        async def initialize(self):
            return True

        async def generate_response(
            self,
            prompt,
            request_id,
            is_retry=False,
            silent=False,
            on_stream_event=None,
        ):
            del prompt, request_id, is_retry, silent, on_stream_event
            tool_parameters = self.tool_registry.get_tool_definitions()[0]["function"][
                "parameters"
            ]
            assert HASHI_TASK_DELTA_ARGUMENT in tool_parameters["required"]
            for cycle in range(3):
                for index, name in enumerate(_TOOLS):
                    await self.tool_registry.execute(
                        name,
                        {"path": f"artifact-{index}"},
                        f"provider-{cycle}-{index}",
                    )
            assert _tool_names(self.tool_registry.get_tool_definitions()) == [
                COGNITIVE_DECISION_TOOL
            ]
            return BackendResponse(
                text=json.dumps(
                    {
                        "disposition": "COMPLETED",
                        "summary": "The unchanged evidence is sufficient.",
                    }
                ),
                duration_ms=1,
                structured_data={
                    "disposition": "COMPLETED",
                    "summary": "The unchanged evidence is sufficient.",
                },
            )

        async def shutdown(self):
            self.shutdown_called = True

    class Manager:
        privacy_level = 1

        def __init__(self):
            self.backend = None

        def create_ephemeral_backend(self, engine, target_model=None):
            assert (engine, target_model) == ("openrouter-api", "configured/model")
            self.backend = Backend()
            return self.backend

    manager = Manager()
    provider = HashiStageProvider(
        backend_manager=manager,
        tool_registry=base,
        cognitive_control_enabled=True,
    )

    response = await provider.invoke(
        ProviderProfile("premium", "openrouter-api", "configured/model"),
        _request(Stage.EXECUTION),
    )

    assert response.data["disposition"] == "COMPLETED"
    assert response.cognitive_control["mode"] == "completed"
    assert response.cognitive_control["interrupt_count"] == 1
    assert response.cognitive_control["last_decision"] == "IMPLICIT_FINALIZE"
    assert "HASHI tool-boundary cognitive control" in manager.backend.sys_prompt
    assert "HASHI persistent TaskState" in manager.backend.sys_prompt
    assert (
        response.cognitive_control["task_state"]["goal"]
        == _request(Stage.EXECUTION).goal
    )
    assert manager.backend.shutdown_called is True


@pytest.mark.asyncio
async def test_tool_free_stage_receives_same_task_state_without_control_tool():
    task_state = HERTaskState(goal="Choose the strategic direction")

    class Backend:
        def __init__(self):
            self.config = SimpleNamespace(extra={}, name="agent1", system_md=None)
            self.capabilities = SimpleNamespace(supports_tool_use=True)
            self.tool_registry = None
            self.sys_prompt = "configured agent persona"

        async def initialize(self):
            return True

        async def generate_response(self, prompt, request_id, **kwargs):
            del prompt, request_id, kwargs
            assert self.tool_registry is None
            return BackendResponse(
                text="strategy ready",
                duration_ms=1,
                structured_data={"summary": "strategy ready"},
            )

        async def shutdown(self):
            return None

    class Manager:
        privacy_level = 1

        def __init__(self):
            self.backend = Backend()

        def create_ephemeral_backend(self, engine, target_model=None):
            del engine, target_model
            return self.backend

    manager = Manager()
    provider = HashiStageProvider(
        backend_manager=manager,
        tool_registry=_Registry(),
        cognitive_control_enabled=True,
    )
    request = replace(
        _request(Stage.TRIAGE, task_state=task_state),
        allow_tools=False,
        allow_side_effects=False,
    )

    response = await provider.invoke(
        ProviderProfile("triage", "openrouter-api", "configured/model"),
        request,
    )

    assert "HASHI persistent TaskState" in manager.backend.sys_prompt
    assert "no tool-boundary delta" in manager.backend.sys_prompt
    assert "HASHI tool-boundary cognitive control" not in manager.backend.sys_prompt
    assert (
        response.cognitive_control["task_state"]["goal"]
        == task_state.snapshot()["goal"]
    )
